"""What the window shows and allows, without any widget (R4-A, R4-C).

The GUI (``gui``) only draws what this module decides. It uses the production
functions unchanged: ``volumeproof.preview_plan`` for the preview, ``runner.run_plan``
for the run, ``journal_v3.read_any_journal`` for the journals.

* ``AppModel`` holds the settings (folders, extension table, selected extensions,
  collision policy) and the last preview. A preview is executable only while the settings
  are exactly the ones it was made for: every change, a change of the custom extensions
  included, drops it (``can_execute`` is false until the next preview). It is used once:
  starting the run drops it as well.
* ``build_preview`` and ``execute`` are what a worker thread runs. ``build_preview``
  changes nothing; it always asks for a move (``cross_copy=False``), so a destination on
  another volume is refused by the preview and never becomes a copy. ``execute`` runs
  only a plan proven to stay on one volume; ``run_plan`` checks it all again.
* Results keep codes, not sentences: the texts are made when they are shown, in the
  language of the window (``i18n``), so the language can change at any time.
* ``BackgroundTask`` runs one job off the UI thread and only puts its result into a
  queue; the GUI reads the queue from the Tk event loop.
* ``list_journals`` reads the journal directory without creating, changing, repairing
  or recovering anything.

Nothing here rolls back a move that was made or recovers a journal.
"""

from __future__ import annotations

import os
import queue
import re
import stat
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from downloads_organizer import i18n
from downloads_organizer.catalog import Category
from downloads_organizer.executor import ExecReason, ExecutionResult, ItemResult, ItemStatus
from downloads_organizer.extensions import ExtensionTable
from downloads_organizer.i18n import DEFAULT_LANG, Lang, pick, tr
from downloads_organizer.journal import (
    JournalError,
    JournalReport,
    OperationState,
    default_log_dir,
)
from downloads_organizer.journal_v2 import EntryV2, JournalReportV2, ReviewFlag, temp_name
from downloads_organizer.journal_v3 import read_any_journal
from downloads_organizer.models import (
    CollisionPolicy,
    Inventory,
    LocationProof,
    OutsideGroup,
    Plan,
    PlanAction,
    PlanError,
    PlanErrorReason,
    PlanItem,
    SkipReason,
    VolumeMode,
    VolumeProof,
)
from downloads_organizer.planner import DEFAULT_DESTINATION_DIR
from downloads_organizer.runner import RunRefusal, run_plan
from downloads_organizer.selection import Selection
from downloads_organizer.volumeproof import display_text, preview_plan

# --- texts ---------------------------------------------------------------------------

RU = Lang.RU
CATEGORY_NAMES = i18n.CATEGORY_TEXT[RU]
PLAN_ERROR_TEXT = i18n.PLAN_ERROR_TEXT[RU]
SKIP_TEXT = i18n.SKIP_TEXT[RU]
ACTION_TEXT = i18n.ACTION_TEXT[RU]
ITEM_STATUS_TEXT = i18n.ITEM_STATUS_TEXT[RU]
EXEC_TEXT = i18n.EXEC_TEXT[RU]
RUN_REFUSAL_TEXT = i18n.RUN_REFUSAL_TEXT[RU]
STATE_TEXT = i18n.STATE_TEXT[RU]
FLAG_TEXT = i18n.FLAG_TEXT[RU]
NO_ROLLBACK = tr("no_rollback", RU)


def category_name(key: str, lang: Lang = DEFAULT_LANG) -> str:
    return pick(i18n.CATEGORY_TEXT, key, lang)


def category_label(category: Category, lang: Lang = DEFAULT_LANG) -> str:
    """``"🖼 Изображения (Images)"``: the name and the folder the files go to."""
    name = category_name(category.key, lang)
    folder = f" ({category.folder})" if name != category.folder else ""
    return f"{category.icon} {name}{folder}"


def _detail(text: str | None, lang: Lang = DEFAULT_LANG) -> str:
    return tr("detail", lang, detail=display_text(text)) if text else ""


def plan_error_text(error: PlanError, lang: Lang = DEFAULT_LANG) -> str:
    text = pick(i18n.PLAN_ERROR_TEXT, error.reason, lang)
    if error.path is not None:
        text += f": {display_text(str(error.path))}"
    if error.reason is PlanErrorReason.CROSS_VOLUME:
        return text  # the text says it all
    return text + _detail(error.detail, lang)


def _skip_text(reason: SkipReason | None, detail: str | None, lang: Lang = DEFAULT_LANG) -> str:
    if reason is None:
        return ""
    return pick(i18n.SKIP_TEXT, reason, lang) + _detail(detail, lang)


_CHANGED = "changed since the preview: "


def _exec_detail(reason: ExecReason, detail: str | None, lang: Lang) -> str:
    """The detail of an execution reason; the fields of SOURCE_CHANGED are translated."""
    if reason is ExecReason.SOURCE_CHANGED and detail and detail.startswith(_CHANGED):
        names = [n.strip() for n in detail.removeprefix(_CHANGED).split(",")]
        if names and all(n in i18n.CHANGED_FIELD_TEXT[lang] for n in names):
            fields = ", ".join(pick(i18n.CHANGED_FIELD_TEXT, n, lang) for n in names)
            return tr("detail", lang, detail=tr("changed_fields", lang, fields=fields))
    return _detail(detail, lang)


def exec_text(reason: ExecReason, detail: str | None, lang: Lang = DEFAULT_LANG) -> str:
    return pick(i18n.EXEC_TEXT, reason, lang) + _exec_detail(reason, detail, lang)


def _location_line(label: str, proof: LocationProof, lang: Lang) -> str:
    state = (
        "" if proof.exists else tr("volume.missing", lang, deepest=display_text(str(proof.deepest)))
    )
    return tr(
        "volume.location",
        lang,
        label=label,
        where=display_text(str(proof.path)),
        state=state,
        fs=display_text(proof.filesystem),
        device=display_text(proof.device),
        serial=f"{proof.volume_serial:#x}",
    )


def volume_lines(proof: VolumeProof | None, lang: Lang = DEFAULT_LANG) -> tuple[str, ...]:
    """``volumeproof.describe_volume`` in ``lang``; every path goes through ``display_text``."""
    if proof is None:
        return (tr("volume.unchecked", lang),)
    lines = [pick(i18n.VOLUME_MODE_TEXT, proof.mode, lang)]
    for key, location in (
        ("volume.source", proof.source),
        ("volume.destination", proof.destination),
    ):
        if location is not None:
            lines.append(_location_line(tr(key, lang), location, lang))
    for problem in proof.problems:
        detail = f": {display_text(problem.detail)}" if problem.detail else ""
        path = display_text(str(problem.path))
        lines.append(tr("volume.problem", lang, reason=problem.reason, path=path, detail=detail))
    return tuple(lines)


def shorten_middle(text: str, limit: int) -> str:
    """``text`` cut in the middle to ``limit`` characters (``…``); for display only."""
    if limit < 3 or len(text) <= limit:
        return text
    head = (limit - 1) // 2
    tail = limit - 1 - head
    return f"{text[:head]}…{text[len(text) - tail :]}"


# --- settings and preview --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything a plan depends on; a preview belongs to exactly one value of it."""

    source: str
    destination: str | None
    selected: frozenset[str]
    policy: CollisionPolicy
    custom: tuple[tuple[str, str], ...] = ()  # the custom extensions of the table, sorted


@dataclass(frozen=True, slots=True)
class PreviewRow:
    name: str
    category: str
    target: str
    action: str
    reason: str
    skipped: bool
    renamed: bool
    source: str = ""


class PreviewFailure(StrEnum):
    NO_SOURCE = "no_source"
    EXCEPTION = "exception"


OUTSIDE_SHOWN = 8  # groups listed in the short line; every group is in the details


@dataclass(frozen=True, slots=True)
class PreviewResult:
    """A preview for ``settings``: the plan and whether it may be run; texts on request."""

    settings: Settings
    plan: Plan | None
    table: ExtensionTable | None = None
    failure: PreviewFailure | None = None
    failure_detail: str | None = None
    release_problem: bool = False
    inventory: Inventory | None = None

    @property
    def total(self) -> int:
        return len(self.plan.items) if self.plan is not None else 0

    @property
    def moves(self) -> int:
        plan = self.plan
        return sum(1 for i in plan.moves if i.action is PlanAction.MOVE) if plan else 0

    @property
    def renames(self) -> int:
        plan = self.plan
        return sum(1 for i in plan.moves if i.action is PlanAction.RENAME) if plan else 0

    @property
    def skipped(self) -> int:
        return len(self.plan.skipped) if self.plan is not None else 0

    @property
    def has_errors(self) -> bool:
        return (
            self.failure is not None
            or self.release_problem
            or (self.plan is not None and bool(self.plan.errors))
        )

    @property
    def executable(self) -> bool:
        """Errors-free, at least one move, proven one volume, a move and not a copy."""
        plan = self.plan
        return (
            plan is not None
            and plan.ok
            and not self.has_errors
            and bool(plan.moves)
            and plan.cross_copy is False
            and plan.volume is not None
            and plan.volume.mode is VolumeMode.SAME
        )

    def rows(self, lang: Lang = DEFAULT_LANG) -> tuple[PreviewRow, ...]:
        if self.plan is None:
            return ()
        table = self.table if self.table is not None else ExtensionTable()
        return tuple(_row(i, table, lang) for i in self.plan.items)

    def errors(self, lang: Lang = DEFAULT_LANG) -> tuple[str, ...]:
        if self.failure is PreviewFailure.NO_SOURCE:
            return (tr("preview.no_source", lang),)
        if self.failure is PreviewFailure.EXCEPTION:
            return (tr("preview.failed", lang, error=self.failure_detail),)
        errors = [plan_error_text(e, lang) for e in self.plan.errors] if self.plan else []
        if self.release_problem:
            errors.append(tr("preview.release", lang))
        return tuple(errors)

    def volume_lines(self, lang: Lang = DEFAULT_LANG) -> tuple[str, ...]:
        plan = self.plan
        if plan is None or not (plan.ok or plan.volume):
            return ()
        return volume_lines(plan.volume, lang)

    def headline(self, lang: Lang = DEFAULT_LANG) -> str:
        if self.has_errors:
            return tr("preview.headline.errors", lang)
        if not self.moves + self.renames:
            return tr("preview.headline.nothing", lang)
        return tr("preview.headline.ready", lang)

    def counts(self, lang: Lang = DEFAULT_LANG) -> str:
        return tr(
            "preview.counts",
            lang,
            total=self.total,
            moves=self.moves + self.renames,
            renames=self.renames,
            skipped=self.skipped,
        )

    def summary_counts(self, lang: Lang = DEFAULT_LANG) -> str:
        """The counts line: the files of the folder, then the plan, «В плане» only once."""
        found = self.inventory_counts(lang)
        if found is None:
            return self.counts(lang)
        if self.inventory is None or (self.plan is not None and self.plan.errors):
            return f"{found} {self.counts(lang)}"  # the inventory has no «В плане» then
        moves = tr(
            "preview.counts_moves",
            lang,
            moves=self.moves + self.renames,
            renames=self.renames,
            skipped=self.skipped,
        )
        return f"{found} {moves}"

    def inventory_counts(self, lang: Lang = DEFAULT_LANG) -> str | None:
        """All files of the folder, in the plan and outside it; ``None`` without a source.

        A plan refused after the listing (the volume check) has no items: then only the
        number of files, so that «В плане» is not shown with two different numbers.
        """
        if self.failure is PreviewFailure.NO_SOURCE:
            return None
        if self.inventory is not None and self.plan is not None and self.plan.errors:
            return tr("inventory.files_only", lang, files=self.inventory.files)
        return inventory_counts(self.inventory, lang)

    def outside_lines(
        self, lang: Lang = DEFAULT_LANG, limit: int = OUTSIDE_SHOWN
    ) -> tuple[str, ...]:
        """The files outside the plan by extension (the first ``limit`` groups), and a hint."""
        return outside_lines(self.inventory, lang, limit)

    def inventory_details(self, lang: Lang = DEFAULT_LANG) -> tuple[str, ...]:
        """What was counted and how: every extension outside the plan, folders, links."""
        inventory = self.inventory
        if inventory is None:
            return ()
        lines = [tr("inventory.scope", lang)]
        if inventory.folders:
            lines.append(tr("inventory.folders", lang, count=inventory.folders))
        if inventory.links:
            lines.append(tr("inventory.links", lang, count=inventory.links))
        if inventory.outside:
            lines.append(tr("inventory.outside_list", lang))
            off = tr("inventory.type_off", lang)
            lines.extend(
                f"  {_group_text(g, lang)}" + (f" ({off})" if g.known else "")
                for g in inventory.outside
            )
        return tuple(lines)


def _group_text(group: OutsideGroup, lang: Lang) -> str:
    ext = (
        display_text(group.extension)
        if group.extension is not None
        else tr("inventory.no_extension", lang)
    )
    return tr("inventory.group", lang, ext=ext, count=group.count)


def inventory_counts(inventory: Inventory | None, lang: Lang = DEFAULT_LANG) -> str:
    """``inventory.counts``, or that nothing could be counted."""
    if inventory is None:
        return tr("inventory.unknown", lang)
    return tr(
        "inventory.counts",
        lang,
        files=inventory.files,
        in_plan=inventory.in_plan,
        outside=inventory.outside_count,
    )


def outside_lines(
    inventory: Inventory | None, lang: Lang = DEFAULT_LANG, limit: int = OUTSIDE_SHOWN
) -> tuple[str, ...]:
    """Files outside the plan: without a rule, then of a type not chosen; and a hint."""
    if inventory is None or not inventory.outside:
        return ()
    lines = []
    for known, key in ((False, "inventory.no_rule"), (True, "inventory.not_selected")):
        groups = [g for g in inventory.outside if g.known is known]
        if not groups:
            continue
        shown = [_group_text(g, lang) for g in groups[:limit]]
        rest = groups[limit:]
        if rest:
            shown.append(
                tr("inventory.more", lang, groups=len(rest), count=sum(g.count for g in rest))
            )
        lines.append(tr(key, lang, groups=", ".join(shown)))
    lines.append(tr("inventory.hint", lang))
    return tuple(lines)


def effective_destination(source: str, destination: str | None, lang: Lang = DEFAULT_LANG) -> str:
    """Where files go, as R1 asks: ``<source>\\DEL\\<категория>`` without a destination."""
    source = source.strip()
    if destination is not None and destination.strip():
        base = destination.strip()
    elif source:
        base = str(Path(source) / DEFAULT_DESTINATION_DIR)
    else:
        return tr("dest.no_source", lang)
    return display_text(str(Path(base) / tr("dest.category", lang)))


def _row(item: PlanItem, table: ExtensionTable, lang: Lang) -> PreviewRow:
    try:
        category = category_label(table.category(item.category_key), lang)
    except KeyError:
        category = display_text(item.category_key)
    return PreviewRow(
        name=display_text(item.source.name),
        category=category,
        target=display_text(str(item.target)),
        action=pick(i18n.ACTION_TEXT, item.action, lang),
        reason=_skip_text(item.reason, item.detail, lang),
        skipped=item.action is PlanAction.SKIP,
        renamed=item.action is PlanAction.RENAME,
        source=display_text(str(item.source)),
    )


def build_preview(settings: Settings, table: ExtensionTable) -> PreviewResult:
    """The preview of ``settings`` (``preview_plan``, always a move); changes nothing."""
    source = settings.source.strip()
    if not source:
        return PreviewResult(settings, None, table, PreviewFailure.NO_SOURCE)
    destination = settings.destination.strip() if settings.destination else None
    try:
        preview = preview_plan(
            source,
            destination or None,
            table,
            settings.selected,
            settings.policy,
            cross_copy=False,
        )
    except Exception as exc:  # bad input that build_plan raises for: no plan
        return PreviewResult(settings, None, table, PreviewFailure.EXCEPTION, _describe(exc))
    return PreviewResult(
        settings,
        preview.plan,
        table,
        release_problem=bool(preview.release_errors),
        inventory=preview.inventory,
    )


class AppModel:
    """Settings and the last preview of the window; used from the UI thread only."""

    def __init__(self, table: ExtensionTable | None = None) -> None:
        self.table = ExtensionTable() if table is None else table
        self.selection = Selection(self.table)
        self._source = ""
        self._destination: str | None = None
        self._policy = CollisionPolicy.RENAME
        self._preview: PreviewResult | None = None
        self._running = False

    # --- settings: every change drops a preview made for other settings ---

    @property
    def source(self) -> str:
        return self._source

    @property
    def destination(self) -> str | None:
        return self._destination

    @property
    def policy(self) -> CollisionPolicy:
        return self._policy

    def settings(self) -> Settings:
        return Settings(
            self._source,
            self._destination,
            self.selection.selected_extensions,
            self._policy,
            tuple(sorted(self.table.custom.items())),
        )

    def set_source(self, text: str) -> bool:
        self._source = text
        return self._changed()

    def set_destination(self, text: str | None) -> bool:
        """``None`` or blank: no destination, files go to ``<source>\\DEL``."""
        self._destination = text if text is not None and text.strip() else None
        return self._changed()

    def set_policy(self, policy: CollisionPolicy) -> bool:
        self._policy = CollisionPolicy(policy)
        return self._changed()

    def set_table(self, table: ExtensionTable) -> bool:
        """New custom extensions: the selection keeps what still exists, new ones are off."""
        if self._running:
            raise RuntimeError("the table cannot change during a run")
        self.table = table
        self.selection.rebind(table)
        return self._changed()

    def set_extension(self, ext: str, selected: bool) -> bool:
        self.selection.set_extension(ext, selected)
        return self._changed()

    def set_category(self, key: str, selected: bool) -> bool:
        self.selection.set_category(key, selected)
        return self._changed()

    def set_all(self, selected: bool) -> bool:
        self.selection.set_all(selected)
        return self._changed()

    def _changed(self) -> bool:
        """Drop a preview that no longer matches; whether one was dropped."""
        if self._preview is not None and self._preview.settings != self.settings():
            self._preview = None
            return True
        return False

    def effective_destination(self, lang: Lang = DEFAULT_LANG) -> str:
        return effective_destination(self._source, self._destination, lang)

    # --- preview and run ---

    @property
    def preview(self) -> PreviewResult | None:
        return self._preview

    @property
    def running(self) -> bool:
        return self._running

    def accept_preview(self, result: PreviewResult) -> bool:
        """Keep ``result`` if it was made for the current settings (else it is stale)."""
        if self._running or result.settings != self.settings():
            return False
        self._preview = result
        return True

    def clear_preview(self) -> None:
        self._preview = None

    @property
    def can_execute(self) -> bool:
        preview = self._preview
        return (
            not self._running
            and preview is not None
            and preview.executable
            and preview.settings == self.settings()
        )

    def start_run(self) -> Plan:
        """The plan to run now; the preview is used up. ``RuntimeError`` if not allowed."""
        if not self.can_execute:
            raise RuntimeError("no executable preview for the current settings")
        assert self._preview is not None and self._preview.plan is not None
        plan = self._preview.plan
        self._preview = None
        self._running = True
        return plan

    def finish_run(self) -> None:
        self._running = False


# --- run -------------------------------------------------------------------------------


class Level(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ResultRow:
    name: str
    status: str
    reason: str
    source: str
    target: str
    attention: bool
    status_code: ItemStatus | None = None
    reason_code: ExecReason | None = None


@dataclass(frozen=True, slots=True)
class RunReport:
    """What a run did, for the result view (in one language)."""

    headline: str
    level: Level
    lines: tuple[str, ...] = ()
    counts: Mapping[ItemStatus, int] = field(default_factory=dict)
    rows: tuple[ResultRow, ...] = ()
    journal_path: Path | None = None
    changed_anything: bool = False
    source_changed: int = 0  # skipped: the file changed after the preview

    @property
    def not_moved(self) -> int:
        """Files of the plan that are where they were (skipped, failed or cancelled)."""
        return sum(n for status, n in self.counts.items() if status is not ItemStatus.MOVED)


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What ``execute`` ended with, as codes: the result of ``run_plan``, or why none."""

    value: object = None
    forbidden: bool = False
    error: str | None = None


def _describe(exc: BaseException) -> str:
    return display_text(f"{type(exc).__name__}: {exc}")


def _refusal_report(refusal: RunRefusal, lang: Lang) -> RunReport:
    text = pick(i18n.RUN_REFUSAL_TEXT, refusal.reason, lang)
    lines = [tr("run.refused.reason", lang, reason=text), tr("run.refused.unchanged", lang)]
    if refusal.detail:
        lines.append(tr("run.details", lang, detail=display_text(refusal.detail)))
    return RunReport(tr("run.refused", lang, reason=text), Level.ERROR, tuple(lines))


def _result_row(result: ItemResult, lang: Lang) -> ResultRow:
    item, status, reason, detail = result.item, result.status, result.reason, result.detail
    if reason is ExecReason.PLANNED_SKIP:
        text = _skip_text(item.reason, item.detail, lang) or pick(i18n.EXEC_TEXT, reason, lang)
    elif reason is ExecReason.COMPLETED:
        text = ""
    else:
        text = exec_text(reason, detail, lang)
    return ResultRow(
        name=display_text(item.source.name),
        status=pick(i18n.ITEM_STATUS_TEXT, status, lang),
        reason=text,
        source=display_text(str(item.source)),
        target=display_text(str(item.target)),
        attention=status in (ItemStatus.FAILED, ItemStatus.RECOVERY_REQUIRED),
        status_code=status,
        reason_code=reason,
    )


def _run_counts(counts: Mapping[ItemStatus, int], inventory: Inventory | None, lang: Lang) -> str:
    """One line: moved, skipped, errors; cancelled and to check only when there are any;
    the files outside the plan as the preview counted them, when it counted them."""
    parts = [
        tr(
            "run.counts",
            lang,
            moved=counts[ItemStatus.MOVED],
            skipped=counts[ItemStatus.SKIPPED],
            failed=counts[ItemStatus.FAILED],
        )
    ]
    if counts[ItemStatus.CANCELLED]:
        parts.append(tr("run.counts.cancelled", lang, count=counts[ItemStatus.CANCELLED]))
    if counts[ItemStatus.RECOVERY_REQUIRED]:
        parts.append(tr("run.counts.recovery", lang, count=counts[ItemStatus.RECOVERY_REQUIRED]))
    if inventory is not None:
        parts.append(tr("run.counts.outside", lang, count=inventory.outside_count))
    return " ".join(parts)


def _execution_report(
    result: ExecutionResult, lang: Lang, inventory: Inventory | None = None
) -> RunReport:
    counts = result.counts
    moved, recovery = counts[ItemStatus.MOVED], counts[ItemStatus.RECOVERY_REQUIRED]
    failed = counts[ItemStatus.FAILED]
    skipped = counts[ItemStatus.SKIPPED]
    changed = sum(1 for r in result.items if r.reason is ExecReason.SOURCE_CHANGED)
    no_rollback = tr("no_rollback", lang)
    lines = [_run_counts(counts, inventory, lang)]
    if result.directories_created:
        lines.append(tr("run.dirs_created", lang, count=len(result.directories_created)))
    if result.stopped is ExecReason.CANCELLED:
        headline, level = tr("run.cancelled", lang, moved=moved), Level.WARNING
        lines.append(f"{tr('run.cancelled.note', lang)} {no_rollback}")
    elif result.complete:
        headline, level = tr("run.done", lang, moved=moved), Level.OK
    elif not result.changed_anything:
        headline, level = tr("run.nothing", lang), Level.ERROR if failed else Level.WARNING
    else:
        headline, level = tr("run.partial", lang, moved=moved), Level.WARNING
        lines.append(no_rollback)
    if result.stopped is not None and result.stopped is not ExecReason.CANCELLED:
        level = Level.ERROR
        reason = exec_text(result.stopped, result.stop_detail, lang)
        lines.append(tr("run.stopped", lang, reason=reason))
    if recovery:
        level = Level.ERROR
        lines.append(tr("run.recovery", lang, count=recovery))
    if changed:
        lines.append(tr("run.source_changed", lang, count=changed))
    elif skipped:
        lines.append(tr("run.skipped_note", lang))
    lines.extend(tr("run.problem", lang, text=display_text(p)) for p in result.problems)
    lines.extend(
        tr("run.not_released", lang, text=display_text(p)) for p in result.cleanup_problems
    )
    if result.cleanup_problems:
        level = Level.ERROR
    return RunReport(
        headline,
        level,
        tuple(lines),
        counts,
        tuple(_result_row(r, lang) for r in result.items),
        result.journal_path,
        result.changed_anything,
        changed,
    )


def summarize_outcome(
    outcome: object, lang: Lang = DEFAULT_LANG, inventory: Inventory | None = None
) -> RunReport:
    """The report of what ``run_plan`` returned; ``inventory``: the preview's count."""
    if isinstance(outcome, RunRefusal):
        return _refusal_report(outcome, lang)
    if isinstance(outcome, ExecutionResult):
        return _execution_report(outcome, lang, inventory)
    # a copy result (or anything else) cannot come from a move plan: say so, do not hide it
    path = getattr(outcome, "journal_path", None)
    return RunReport(
        tr("run.unexpected", lang),
        Level.ERROR,
        (tr("run.unexpected.note", lang),),
        journal_path=path,
        changed_anything=bool(getattr(outcome, "changed_anything", True)),
    )


def crash_report(error: str, lang: Lang = DEFAULT_LANG) -> RunReport:
    """A run that raised: something may have been moved; the journal says what."""
    lines = (error, f"{tr('run.check_journal', lang)} {tr('no_rollback', lang)}")
    return RunReport(tr("run.crashed", lang), Level.ERROR, lines, changed_anything=True)


def run_report(
    outcome: RunOutcome, lang: Lang = DEFAULT_LANG, inventory: Inventory | None = None
) -> RunReport:
    """The report of ``outcome`` in ``lang``; ``inventory`` of the preview that was run."""
    if outcome.forbidden:
        lines = (tr("run.forbidden.note", lang),)
        return RunReport(tr("run.forbidden", lang), Level.ERROR, lines)
    if outcome.error is not None:
        return crash_report(outcome.error, lang)
    return summarize_outcome(outcome.value, lang, inventory)


def execute(plan: Plan, table: ExtensionTable, cancel: Callable[[], bool]) -> RunOutcome:
    """Run ``plan`` (a preview the user confirmed) with ``runner.run_plan``.

    Only a move proven to stay on one volume is handed over; anything else is refused
    here before ``run_plan`` is called. ``cancel`` is asked before each file.
    """
    volume = plan.volume
    if plan.cross_copy is not False or volume is None or volume.mode is not VolumeMode.SAME:
        return RunOutcome(forbidden=True)
    try:
        value = run_plan(plan, table, cancel=cancel)
    except Exception as exc:  # run_plan returns its problems; this is not expected
        return RunOutcome(error=_describe(exc))
    return RunOutcome(value)


# --- background work -------------------------------------------------------------------


class TaskKind(StrEnum):
    PREVIEW = "preview"
    RUN = "run"
    JOURNALS = "journals"


@dataclass(frozen=True, slots=True)
class TaskDone:
    """Put into the queue when a task ends: its value, or the error it raised."""

    kind: TaskKind
    value: object = None
    error: str | None = None


class BackgroundTask:
    """Runs one job in a thread; only the queue connects it to the UI thread.

    ``cancel`` is a flag the job may ask (``cancelled``); it never interrupts anything.
    A run is a non-daemon thread: the process does not end in the middle of it.
    """

    def __init__(
        self,
        kind: TaskKind,
        job: Callable[[Callable[[], bool]], object],
        events: queue.Queue[TaskDone],
    ) -> None:
        self.kind = kind
        self._job = job
        self._events = events
        self._cancel = threading.Event()
        self._thread = threading.Thread(
            target=self._main, name=f"DownloadsOrganizer-{kind}", daemon=kind is not TaskKind.RUN
        )

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _main(self) -> None:
        try:
            value = self._job(self.cancelled)
        except BaseException as exc:  # the UI must always learn that the task ended
            self._events.put(TaskDone(self.kind, error=_describe(exc)))
            return
        self._events.put(TaskDone(self.kind, value))


# --- journals --------------------------------------------------------------------------

_JOURNAL_NAME = re.compile(r"run-(\d{8}T\d{12})Z-([0-9a-f]{32})\.jsonl")
_PYTEST_PATH = re.compile(r"\\pytest-of-[^\\]+\\pytest-\d+\\", re.IGNORECASE)
_FILE_OPERATIONS = frozenset({"move", "copy"})


class JournalKind(StrEnum):
    MOVE = "move"
    COPY = "copy"
    UNKNOWN_FORMAT = "unknown_format"
    UNKNOWN = "unknown"


class JournalResult(StrEnum):
    FINISHED = "finished"
    FAILED = "failed"
    REVIEW = "review"
    UNFINISHED = "unfinished"
    DAMAGED = "damaged"


class JournalIssue(StrEnum):
    UNREADABLE = "unreadable"
    UNAVAILABLE = "unavailable"
    NOT_A_FILE = "not_a_file"


class ListingNote(StrEnum):
    NO_LOCALAPPDATA = "no_localappdata"
    NOT_CREATED = "not_created"
    UNAVAILABLE = "unavailable"
    NOT_A_DIRECTORY = "not_a_directory"
    UNREADABLE = "unreadable"


def journal_kind_text(kind: JournalKind, lang: Lang = DEFAULT_LANG) -> str:
    return tr(f"journal.kind.{kind}", lang)


def journal_result_text(result: JournalResult | None, lang: Lang = DEFAULT_LANG) -> str:
    return tr(f"journal.result.{result}", lang) if result is not None else ""


def journal_issue_text(issue: JournalIssue, detail: str | None, lang: Lang = DEFAULT_LANG) -> str:
    return tr(f"journal.issue.{issue}", lang, detail=detail or "")


def listing_note_text(note: ListingNote, detail: str | None, lang: Lang = DEFAULT_LANG) -> str:
    return tr(f"journal.note.{note}", lang, detail=detail or "")


def operation_text(operation: str, lang: Lang = DEFAULT_LANG) -> str:
    """A journal operation (``move``, ``copy``, ``mkdir``) in ``lang``; others as they are."""
    return pick(i18n.OPERATION_TEXT, operation, lang)


@dataclass(frozen=True, slots=True)
class EntryView:
    """One change recorded in a journal, with what a read-only check found on disk."""

    operation: str
    source: str | None
    target: str | None
    state: OperationState
    phase: str | None = None
    flags: tuple[ReviewFlag, ...] = ()
    notes: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    needs_review: bool = False
    missing: tuple[str, ...] = ()  # recorded paths that do not exist now
    present: tuple[str, ...] = ()  # recorded paths that exist now


@dataclass(frozen=True, slots=True)
class JournalSummary:
    path: Path
    name: str
    started: datetime | None
    schema: int | None = None
    kind: JournalKind = JournalKind.UNKNOWN
    result: JournalResult | None = None
    states: Mapping[OperationState, int] = field(default_factory=dict)
    recovery: int = 0
    problems: tuple[str, ...] = ()
    test_journal: bool = False
    missing_objects: bool = False
    entries: tuple[EntryView, ...] = ()
    issue: JournalIssue | None = None
    issue_detail: str | None = None

    @property
    def attention(self) -> bool:
        return (
            self.issue is not None
            or bool(self.recovery or self.problems)
            or self.result is not JournalResult.FINISHED
        )

    @property
    def file_count(self) -> int:
        """Files the journal records a change for (folders created are not counted)."""
        return sum(self.states.values())

    @property
    def done_count(self) -> int:
        return self.states.get(OperationState.DONE, 0)

    def error(self, lang: Lang = DEFAULT_LANG) -> str | None:
        """Why the journal could not be read, if it could not."""
        if self.issue is None:
            return None
        return journal_issue_text(self.issue, self.issue_detail, lang)


@dataclass(frozen=True, slots=True)
class JournalListing:
    directory: Path | None
    journals: tuple[JournalSummary, ...] = ()
    note: ListingNote | None = None
    note_detail: str | None = None

    def note_text(self, lang: Lang = DEFAULT_LANG) -> str | None:
        return listing_note_text(self.note, self.note_detail, lang) if self.note else None


def _started(name: str) -> datetime | None:
    match = _JOURNAL_NAME.fullmatch(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%f").replace(tzinfo=UTC)
    except ValueError:
        return None


def _exists(path: str) -> bool | None:
    """``lstat`` only (nothing is opened or followed at the end); ``None``: unknown."""
    if not Path(path).is_absolute():
        return None
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return True


def _temp_path(entry: EntryV2, run_id: object) -> str | None:
    """Where the temporary file of a copy entry would be (``journal_v2.temp_name``)."""
    if not entry.target or not entry.records or not isinstance(run_id, str):
        return None
    first = entry.records[0]
    sequence = first.get("sequence") if isinstance(first, Mapping) else None
    if type(sequence) is not int or first.get("status") != "intent":
        return None
    return str(Path(entry.target).parent / temp_name(run_id, sequence))


def _entry_view(entry: object, review: bool, run_id: object) -> EntryView:
    flags: frozenset[ReviewFlag] = getattr(entry, "flags", frozenset())
    state: OperationState = entry.state  # type: ignore[attr-defined]
    source: str | None = entry.source  # type: ignore[attr-defined]
    target: str | None = entry.target  # type: ignore[attr-defined]
    missing: list[str] = []
    present: list[str] = []
    if review:
        candidates = [p for p in (source, target) if p]
        if isinstance(entry, EntryV2) and flags & {ReviewFlag.TEMP_LEFT, ReviewFlag.TEMP_MAY_EXIST}:
            temp = _temp_path(entry, run_id)
            if temp is not None:
                candidates.append(temp)
        for path in candidates:
            found = _exists(path)
            if found is True:
                present.append(display_text(path))
            elif found is False:
                missing.append(display_text(path))
    return EntryView(
        operation=str(entry.operation),  # type: ignore[attr-defined]
        source=display_text(source) if source else None,
        target=display_text(target) if target else None,
        state=state,
        phase=getattr(entry, "phase", None),
        flags=tuple(sorted(flags)),
        notes=tuple(display_text(n) for n in getattr(entry, "notes", ())),
        problems=tuple(display_text(p) for p in entry.problems),  # type: ignore[attr-defined]
        needs_review=review,
        missing=tuple(missing),
        present=tuple(present),
    )


def _paths(records: Iterable[object]) -> list[str]:
    found: list[str] = []
    for record in records:
        if isinstance(record, Mapping):
            found.extend(v for k in ("source", "target") if isinstance(v := record.get(k), str))
    return found


def summarize_journal(path: Path) -> JournalSummary:
    """Read one journal with ``read_any_journal`` (opened for reading only)."""
    name, started = path.name, _started(path.name)
    try:
        report: JournalReport | JournalReportV2 = read_any_journal(path)
    except (OSError, ValueError) as exc:
        return JournalSummary(
            path, name, started, issue=JournalIssue.UNREADABLE, issue_detail=_describe(exc)
        )
    first = report.records[0] if report.records else None
    schema = first.get("schema_version") if isinstance(first, Mapping) else None
    schema = schema if type(schema) is int else None
    run_id = first.get("run_id") if isinstance(first, Mapping) else None
    review = {id(e) for e in report.recovery_needed}
    entries = tuple(_entry_view(e, id(e) in review, run_id) for e in report.entries)
    files = [e for e in entries if e.operation in _FILE_OPERATIONS]
    states = Counter(e.state for e in files)
    if isinstance(report, JournalReportV2):
        kind = JournalKind.COPY
    elif schema == 1:
        kind = JournalKind.MOVE
    else:
        kind = JournalKind.UNKNOWN_FORMAT
    if report.review_whole_journal:
        result = JournalResult.DAMAGED
    elif not report.finished:
        result = JournalResult.UNFINISHED
    elif review:
        result = JournalResult.REVIEW
    elif states[OperationState.FAILED]:
        result = JournalResult.FAILED
    else:
        result = JournalResult.FINISHED
    paths = _paths(report.records)
    return JournalSummary(
        path,
        name,
        started,
        schema,
        kind,
        result,
        dict(states),
        len(review),
        tuple(display_text(str(p)) for p in report.problems),
        test_journal=bool(paths) and all(_PYTEST_PATH.search(p) for p in paths),
        missing_objects=any(e.missing for e in entries),
        entries=entries,
    )


def _is_link(st: os.stat_result) -> bool:
    attributes = getattr(st, "st_file_attributes", 0)
    return stat.S_ISLNK(st.st_mode) or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def journal_directory() -> Path | None:
    try:
        return default_log_dir()
    except JournalError:
        return None


def list_journals(directory: Path | None = None) -> JournalListing:
    """Every ``*.jsonl`` in the journal directory, newest first; reads only.

    Nothing is created (a missing directory is reported, not made), nothing is
    changed, repaired, recovered, hidden or removed. A link or junction, as the
    directory or as a file in it, is listed and not read.
    """
    directory = journal_directory() if directory is None else directory
    if directory is None:
        return JournalListing(None, note=ListingNote.NO_LOCALAPPDATA)
    try:
        st = os.lstat(directory)
    except FileNotFoundError:
        return JournalListing(directory, note=ListingNote.NOT_CREATED)
    except OSError as exc:
        return JournalListing(directory, note=ListingNote.UNAVAILABLE, note_detail=_describe(exc))
    if _is_link(st) or not stat.S_ISDIR(st.st_mode):
        return JournalListing(directory, note=ListingNote.NOT_A_DIRECTORY)
    journals: list[JournalSummary] = []
    try:
        with os.scandir(directory) as it:
            entries = sorted(
                (e for e in it if e.name.lower().endswith(".jsonl")),
                key=lambda e: e.name,
                reverse=True,
            )
    except OSError as exc:
        return JournalListing(directory, note=ListingNote.UNREADABLE, note_detail=_describe(exc))
    for entry in entries:
        path = Path(entry.path)
        try:
            entry_st = entry.stat(follow_symlinks=False)
        except OSError as exc:
            journals.append(
                JournalSummary(
                    path,
                    entry.name,
                    _started(entry.name),
                    issue=JournalIssue.UNAVAILABLE,
                    issue_detail=_describe(exc),
                )
            )
            continue
        if _is_link(entry_st) or not stat.S_ISREG(entry_st.st_mode):
            journals.append(
                JournalSummary(
                    path, entry.name, _started(entry.name), issue=JournalIssue.NOT_A_FILE
                )
            )
            continue
        journals.append(summarize_journal(path))
    return JournalListing(directory, tuple(journals))


def journal_details(summary: JournalSummary, lang: Lang = DEFAULT_LANG) -> str:
    """The text of the detail view of one journal."""
    lines = [tr("journal.path", lang, path=display_text(str(summary.path)))]
    if summary.started is not None:
        lines.append(tr("journal.started", lang, time=format_time(summary.started, lang)))
    kind = journal_kind_text(summary.kind, lang)
    schema = tr("journal.schema", lang, schema=summary.schema) if summary.schema else ""
    lines.append(tr("journal.kind", lang, kind=kind) + schema)
    error = summary.error(lang)
    if error:
        lines.append(error)
        return "\n".join(lines)
    lines.append(tr("journal.result", lang, result=journal_result_text(summary.result, lang)))
    if summary.test_journal:
        lines.append(tr("journal.test", lang))
    lines.append(tr("journal.read_only", lang))
    lines.extend(tr("journal.problem", lang, text=p) for p in summary.problems)
    for index, entry in enumerate(summary.entries, start=1):
        lines.append("")
        lines.append(
            tr(
                "journal.entry",
                lang,
                mark="⚠ " if entry.needs_review else "",
                index=index,
                operation=operation_text(entry.operation, lang),
                state=pick(i18n.STATE_TEXT, entry.state, lang),
            )
        )
        if entry.source:
            lines.append(tr("journal.entry.from", lang, path=entry.source))
        if entry.target:
            lines.append(tr("journal.entry.to", lang, path=entry.target))
        if entry.phase:
            lines.append(tr("journal.entry.phase", lang, phase=entry.phase))
        if entry.flags:
            flags = "; ".join(pick(i18n.FLAG_TEXT, f, lang) for f in entry.flags)
            lines.append(tr("journal.entry.check", lang, flags=flags))
        lines.extend(tr("journal.entry.note", lang, text=n) for n in entry.notes)
        lines.extend(tr("journal.entry.problem", lang, text=p) for p in entry.problems)
        lines.extend(tr("journal.entry.missing", lang, path=m) for m in entry.missing)
        lines.extend(tr("journal.entry.present", lang, path=p) for p in entry.present)
    return "\n".join(lines)


_TIME_FORMAT = {Lang.RU: "%d.%m.%Y %H:%M:%S", Lang.EN: "%Y-%m-%d %H:%M:%S"}


def format_time(moment: datetime, lang: Lang = DEFAULT_LANG) -> str:
    return moment.astimezone().strftime(_TIME_FORMAT[Lang(lang)])
