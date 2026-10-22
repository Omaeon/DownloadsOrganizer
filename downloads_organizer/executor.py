"""Carry out a plan on Windows: same-volume moves that never overwrite, journaled.

``execute_plan`` trusts nothing in the plan it is given:

1. ``validate_plan`` checks the plan against itself and the extension table without
   touching the filesystem (structure, paths, categories, targets, duplicates,
   directories to create). An invalid plan is refused as a whole: no journal, no
   directory, no move.
2. The volume roots of the source and of the destination are opened and proven to be
   the root of one local NTFS disk volume (``winfileops.prove_volume_root``, also
   used by the journal): a subst or network drive, a UNC path or an unproven volume
   skips every move as ``ROOT_UNVERIFIED`` before any directory is created. Both root
   handles are held; everything below is reached from them. Read-only checks of the
   source and destination directories follow; a run-level problem (source gone,
   destination is a link, other volume, ...) skips every move.
3. Every change is journaled before it is made (``intent``) and after (outcome). If
   the journal cannot be written, no further change is started.
4. Directories are walked from the proven root handle by handle (``winfileops``),
   never following a link or junction, and only directories listed in the plan are
   created, one at a time with ``FILE_CREATE``. Every directory on the way, from the
   volume root down to the source directory, the destination root and every category
   directory, is opened relative to its already checked parent without
   ``FILE_SHARE_DELETE`` and stays open for the whole run: from the moment it is
   opened no directory of either chain can be renamed, deleted or swapped for a
   junction, so a move can never land below a renamed ancestor. A directory someone
   else holds open for deletion is refused (sharing violation), never opened weaker.
5. Each file is opened relative to the source directory handle with ``DELETE``
   access and only ``FILE_SHARE_READ`` (nobody can write, rename or delete it while
   it is open), compared with the plan's snapshot through that handle (file id,
   volume, size, mtime, attributes) and renamed *through the same handle* into the
   category directory handle with ``ReplaceIfExists = FALSE``. An existing target in
   any form makes the filesystem refuse the rename, so a target that appeared after
   the preview is reported as skipped and stays untouched; no other name is chosen.
   Then the target name is opened and its file id compared.

Every handle the run opens is registered until it is released, so that the run can
release all of them even when it is interrupted (``KeyboardInterrupt``, ...): the
interrupt then reaches the caller, after every handle and the journal were tried.

Not handled in R3A: moves to another volume (refused, nothing is copied), undo, and
finishing a run that was interrupted (the journal shows what is uncertain).
"""

from __future__ import annotations

import contextlib
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from downloads_organizer import winfileops
from downloads_organizer.catalog import Category
from downloads_organizer.extensions import ExtensionTable, extension_of
from downloads_organizer.journal import Journal, JournalError, Operation, Status
from downloads_organizer.models import CollisionPolicy, FileInfo, Plan, PlanAction, PlanItem
from downloads_organizer.pathsafety import (
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    DirectoryRelation,
    PathSafetyError,
    check_directory_chain,
    directory_relation,
    file_identity,
    folder_name_problem,
    name_key,
    normalize_dir_path,
    path_key,
)
from downloads_organizer.planner import DEFAULT_DESTINATION_DIR

_DIR_ACCESS = winfileops.FILE_LIST_DIRECTORY | winfileops.FILE_READ_ATTRIBUTES
# Held directories: others may read and add entries, but not rename or delete them.
# FILE_LIST_DIRECTORY is data access, so this share mode is actually enforced.
_HELD_SHARE = winfileops.FILE_SHARE_READ | winfileops.FILE_SHARE_WRITE
_FILE_ACCESS = winfileops.DELETE | winfileops.FILE_READ_ATTRIBUTES
_PROBE_ACCESS = winfileops.FILE_READ_ATTRIBUTES

_UNSAFE_ATTRIBUTES = (
    stat.FILE_ATTRIBUTE_DIRECTORY
    | stat.FILE_ATTRIBUTE_REPARSE_POINT
    | stat.FILE_ATTRIBUTE_HIDDEN
    | stat.FILE_ATTRIBUTE_SYSTEM
    | stat.FILE_ATTRIBUTE_DEVICE
    | stat.FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)
_STATUS_FILE_IS_A_DIRECTORY = 0xC000_00BA


class ItemStatus(StrEnum):
    MOVED = "moved"  # moved and verified at the target
    SKIPPED = "skipped"  # deliberately not moved; the file was left as it was
    FAILED = "failed"  # the move failed; the file was left as it was
    CANCELLED = "cancelled"  # not attempted because the run stopped
    RECOVERY_REQUIRED = "recovery_required"  # outcome unknown or unexpected: check by hand


class ExecReason(StrEnum):
    COMPLETED = "completed"
    PLANNED_SKIP = "planned_skip"  # skipped by the plan itself (see ``item.reason``)
    PLAN_INVALID = "plan_invalid"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    UNSAFE_NAME = "unsafe_name"
    METADATA_UNKNOWN = "metadata_unknown"
    SOURCE_MISSING = "source_missing"
    SOURCE_CHANGED = "source_changed"  # same file, other size, mtime or attributes
    SOURCE_REPLACED = "source_replaced"  # another object under the same name
    SOURCE_UNSAFE = "source_unsafe"  # link, cloud file, hidden, system, ...
    SOURCE_IN_USE = "source_in_use"
    SOURCE_CHECK_FAILED = "source_check_failed"
    SOURCE_DIR_CHANGED = "source_dir_changed"
    DESTINATION_CHANGED = "destination_changed"
    TARGET_DIR_MISSING = "target_dir_missing"
    TARGET_DIR_INVALID = "target_dir_invalid"
    TARGET_DIR_IS_SOURCE = "target_dir_is_source"
    TARGET_DIR_FAILED = "target_dir_failed"
    TARGET_EXISTS = "target_exists"
    VOLUME_UNKNOWN = "volume_unknown"
    ROOT_UNVERIFIED = "root_unverified"  # not proven a local NTFS volume root: subst, UNC, ...
    CROSS_VOLUME = "cross_volume"
    ACCESS_DENIED = "access_denied"
    MOVE_FAILED = "move_failed"
    VERIFY_FAILED = "verify_failed"
    JOURNAL_FAILED = "journal_failed"
    CANCELLED = "cancelled"
    RUN_ABORTED = "run_aborted"
    CLEANUP_FAILED = "cleanup_failed"  # a handle could not be released: stop, report it
    UNEXPECTED_ERROR = "unexpected_error"


_JOURNAL_STATUS = {
    ItemStatus.MOVED: Status.DONE,
    ItemStatus.SKIPPED: Status.SKIPPED,
    ItemStatus.FAILED: Status.FAILED,
    ItemStatus.CANCELLED: Status.CANCELLED,
    ItemStatus.RECOVERY_REQUIRED: Status.RECOVERY_REQUIRED,
}


@dataclass(frozen=True, slots=True)
class ItemResult:
    """What happened to one plan item; ``error_code`` is a Windows error code."""

    item: PlanItem
    status: ItemStatus
    reason: ExecReason
    detail: str | None = None
    error_code: int | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome of ``execute_plan``: one ``ItemResult`` per plan item, in plan order.

    ``stopped`` is why the run ended before handling every item normally (``None``
    if it did not); ``problems`` are run-level messages (invalid plan, journal or
    cancel errors). ``directories_created`` are the directories this run created;
    they are never removed again, also not after an error. ``cleanup_problems`` are
    handles (directories, files, the journal) whose release was not confirmed: the
    item results stay what they were (a made move stays ``MOVED``), but such a run is
    never ``complete``. ``cleanup_errors`` are the same failures as objects (e.g. a
    ``winfileops.HandleCloseError`` with ``winerror``, ``handle_value`` and the path);
    ``winfileops.release_failures`` finds the ones nested inside them.
    """

    plan: Plan
    items: tuple[ItemResult, ...]
    problems: tuple[str, ...] = ()
    directories_created: tuple[Path, ...] = ()
    run_id: str | None = None
    journal_path: Path | None = None
    stopped: ExecReason | None = None
    stop_detail: str | None = None
    cleanup_problems: tuple[str, ...] = ()
    cleanup_errors: tuple[BaseException, ...] = ()

    def count(self, status: ItemStatus) -> int:
        return sum(1 for r in self.items if r.status is status)

    @property
    def counts(self) -> dict[ItemStatus, int]:
        return {status: self.count(status) for status in ItemStatus}

    @property
    def changed_anything(self) -> bool:
        """Something may have changed on disk (moves, new directories, unknown outcomes)."""
        return bool(self.directories_created) or any(
            r.status in (ItemStatus.MOVED, ItemStatus.RECOVERY_REQUIRED) for r in self.items
        )

    @property
    def complete(self) -> bool:
        """Every planned move was made and verified, without any problem."""
        return (
            not self.problems
            and not self.cleanup_problems
            and self.stopped is None
            and all(
                r.status is ItemStatus.MOVED
                or (r.status is ItemStatus.SKIPPED and r.reason is ExecReason.PLANNED_SKIP)
                for r in self.items
            )
        )

    @property
    def partial(self) -> bool:
        """Some, but not all planned changes were made: never report this as success."""
        return self.changed_anything and not self.complete


# --- static validation ---------------------------------------------------------------


def _normalized(path: object) -> bool:
    if not isinstance(path, Path):
        return False
    try:
        return str(normalize_dir_path(str(path))) == str(path)
    except PathSafetyError, TypeError:
        return False


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_file_info(info: object) -> str | None:
    if not isinstance(info, FileInfo):
        return "no metadata snapshot"
    size, mtime, attributes = info.size, info.mtime_ns, info.attributes
    if not (_int(size) and _int(mtime) and _int(attributes)):
        return "incomplete metadata snapshot"
    assert size is not None and attributes is not None
    if size < 0 or attributes < 0:
        return "invalid metadata snapshot"
    for value in (info.file_id, info.device):
        if value is not None and (not _int(value) or value <= 0):
            return "invalid file id or volume in the snapshot"
    return None


def validate_plan(plan: object, table: ExtensionTable) -> tuple[str, ...]:
    """Problems that make ``plan`` unusable, found without touching the filesystem.

    Checks what ``Plan`` checks on construction again (a plan may have been built
    around ``__post_init__``) and more: normalized absolute paths, the default
    ``DEL`` root, existing categories with matching extensions and safe folder names,
    targets exactly ``<root>/<folder>/<name>`` and never in the source directory, no
    duplicate sources or targets (case-insensitively), complete snapshots for moves,
    and directories to create that are exactly the missing tail of the root chain
    plus category folders that planned moves use, parents first. An empty result means
    the plan is consistent, not that the filesystem still matches it.
    """
    try:
        return tuple(_validate(plan, table))
    except Exception as exc:  # a malformed plan must be refused, never half-checked
        return (f"malformed plan: {type(exc).__name__}: {exc}",)


def _validate(plan: object, table: ExtensionTable) -> list[str]:
    if not isinstance(plan, Plan):
        return [f"not a Plan: {type(plan).__name__}"]
    if not isinstance(table, ExtensionTable):
        return [f"not an ExtensionTable: {type(table).__name__}"]
    if not isinstance(plan.errors, tuple) or plan.errors:
        return ["the plan has errors"]
    problems: list[str] = []
    if not isinstance(plan.policy, CollisionPolicy):
        problems.append(f"unknown collision policy {plan.policy!r}")
    if not isinstance(plan.explicit_destination, bool):
        problems.append("explicit_destination is not a bool")
    if not isinstance(plan.items, tuple) or not all(isinstance(i, PlanItem) for i in plan.items):
        problems.append("items are not a tuple of PlanItem")
    if not isinstance(plan.directories_to_create, tuple) or not all(
        isinstance(d, Path) for d in plan.directories_to_create
    ):
        problems.append("directories_to_create is not a tuple of paths")
    source, root = plan.source, plan.destination_root
    if not _normalized(source):
        problems.append(f"source is not a normalized absolute path: {source!r}")
    if not _normalized(root):
        problems.append(f"destination root is not a normalized absolute path: {root!r}")
    if problems:
        return problems
    assert isinstance(source, Path) and isinstance(root, Path)
    if path_key(root) == path_key(source):
        problems.append("destination root is the source directory")
    if plan.explicit_destination is False and str(root) != str(source / DEFAULT_DESTINATION_DIR):
        problems.append(f"default destination root must be {source / DEFAULT_DESTINATION_DIR}")

    sources: set[str] = set()
    targets: set[tuple[str, str]] = set()
    used_dirs: dict[str, Path] = {}
    for item in plan.items:
        problems.extend(f"{item.source}: {p}" for p in _validate_item(item, plan, table))
        key = name_key(item.source.name)
        if key in sources:
            problems.append(f"{item.source}: source listed more than once")
        sources.add(key)
        if item.action is PlanAction.SKIP:
            continue
        target = (path_key(item.target.parent), name_key(item.target.name))
        if target in targets:
            problems.append(f"{item.target}: target used more than once")
        targets.add(target)
        used_dirs[path_key(item.target.parent)] = item.target.parent
    if problems:
        return problems
    problems.extend(_validate_directories(plan.directories_to_create, source, root, used_dirs))
    return problems


def _validate_item(item: PlanItem, plan: Plan, table: ExtensionTable) -> list[str]:
    problems: list[str] = []
    if not isinstance(item.action, PlanAction):
        return [f"unknown action {item.action!r}"]
    if not isinstance(item.source, Path) or not isinstance(item.target, Path):
        return ["source or target is not a path"]
    if str(item.source.parent) != str(plan.source) or item.source.name in ("", ".", ".."):
        problems.append("not a file directly in the source directory")
    if (item.action is PlanAction.SKIP) != (item.reason is not None):
        problems.append("reason must be set exactly for skipped items")
    if item.action is PlanAction.SKIP:
        return problems
    root = plan.destination_root
    assert root is not None
    if not isinstance(item.category_key, str) or not isinstance(item.extension, str):
        return [*problems, "category or extension is not a str"]
    try:
        category = table.category(item.category_key)
    except KeyError:
        return [*problems, f"unknown category {item.category_key!r}"]
    folder_problem = folder_name_problem(category.folder)
    if folder_problem is not None:
        problems.append(f"unsafe category folder {category.folder!r}: {folder_problem}")
    ext = extension_of(item.source.name)
    if ext is None or ext != item.extension or extension_of(item.target.name) != ext:
        problems.append("extension does not match the source and target names")
    else:
        mapped = table.category_for(ext)
        if mapped is None or mapped.key != category.key:
            problems.append(f"extension {ext} does not belong to category {category.key}")
    if str(item.target.parent) != str(root / category.folder):
        problems.append(f"target is not directly in {root / category.folder}")
    if path_key(item.target.parent) == path_key(plan.source):
        problems.append("target directory is the source directory")
    if item.target.name in ("", ".", ".."):
        problems.append("target does not name a file")
    if (item.action is PlanAction.MOVE) != (item.target.name == item.source.name):
        problems.append("move keeps the file name, rename changes it")
    info_problem = _check_file_info(item.file)
    if info_problem is not None:
        problems.append(info_problem)
    return problems


def _validate_directories(
    directories: tuple[Path, ...], source: Path, root: Path, used_dirs: dict[str, Path]
) -> list[str]:
    problems: list[str] = []
    if directories and not used_dirs:
        return ["directories to create without planned moves"]
    chain = [p for p in reversed(root.parents) if str(p) != root.anchor]
    chain.append(root)  # ancestors of the root below the volume root, top down, then root
    chain_keys = [path_key(p) for p in chain]
    existing = {path_key(source), *(path_key(p) for p in source.parents)}
    allowed = {path_key(p): str(p) for p in chain} | {k: str(p) for k, p in used_dirs.items()}
    index: dict[str, int] = {}
    for position, directory in enumerate(directories):
        key = path_key(directory)
        if key in index:
            problems.append(f"{directory}: listed more than once")
        elif key in existing:
            problems.append(f"{directory}: the source directory or its parent exists already")
        elif key not in allowed or allowed[key] != str(directory):
            problems.append(f"{directory}: not a directory the planned moves need")
        index.setdefault(key, position)
    for directory in directories:
        parent = index.get(path_key(directory.parent))
        if parent is not None and parent > index[path_key(directory)]:
            problems.append(f"{directory}: listed before its parent")
    listed_chain = [k for k in chain_keys if k in index]
    if listed_chain:
        first = chain_keys.index(listed_chain[0])
        if any(k not in index for k in chain_keys[first:]):
            problems.append("directories to create skip a level below the destination root")
        missing = [str(p) for k, p in used_dirs.items() if k not in index]
        if missing:
            problems.append(f"destination root is created but not {', '.join(sorted(missing))}")
    return problems


# --- execution -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Refusal:
    status: ItemStatus
    reason: ExecReason
    detail: str | None = None
    error_code: int | None = None


def _refused(
    plan: object,
    reason: ExecReason,
    problems: tuple[str, ...],
    move_status: ItemStatus = ItemStatus.SKIPPED,
) -> ExecutionResult:
    """Result for a run that changes nothing at all."""
    items = getattr(plan, "items", ())
    results: list[ItemResult] = []
    if isinstance(items, tuple) and all(isinstance(i, PlanItem) for i in items):
        for item in items:
            if reason is not ExecReason.PLAN_INVALID and item.action is PlanAction.SKIP:
                results.append(ItemResult(item, ItemStatus.SKIPPED, ExecReason.PLANNED_SKIP))
            else:
                results.append(ItemResult(item, move_status, reason))
    return ExecutionResult(
        plan,  # type: ignore[arg-type]
        tuple(results),
        problems,
        stopped=reason,
        stop_detail="; ".join(problems) or None,
    )


def _error_code(exc: OSError) -> int | None:
    return getattr(exc, "winerror", None) or exc.errno or None


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


_describe_cleanup = winfileops.describe_error  # with the notes: nothing of it is dropped


def execute_plan(
    plan: Plan,
    table: ExtensionTable,
    *,
    cancel: Callable[[], bool] | None = None,
    journal: Callable[[], Journal] | None = None,
) -> ExecutionResult:
    """Execute ``plan`` (from ``build_plan``, after the user confirmed the preview).

    ``cancel`` is asked before each file; once it returns true the remaining files are
    cancelled, the file being moved is finished first. ``journal`` creates the run's
    journal (default ``Journal.create()`` in ``%LOCALAPPDATA%``). Never raises for
    filesystem or journal problems; they end up in the result.

    An interrupt (``KeyboardInterrupt``, ``SystemExit``, any non-``Exception``) is
    raised to the caller, never turned into a result: nothing is started after it,
    every handle and the journal are released first, and its notes say what is known
    (verified moves, the journal, a move whose outcome is unknown, cleanup problems).
    """
    problems = validate_plan(plan, table)
    if problems:
        return _refused(plan, ExecReason.PLAN_INVALID, problems)
    if not winfileops.AVAILABLE:
        return _refused(
            plan, ExecReason.UNSUPPORTED_PLATFORM, ("moves are only supported on Windows",)
        )
    try:
        log = (journal or Journal.create)()
    except JournalError as exc:
        refused = _refused(plan, ExecReason.JOURNAL_FAILED, (str(exc),), ItemStatus.CANCELLED)
        released = tuple(
            f"cannot release journal: {_describe_cleanup(e)}" for e in exc.release_errors
        )
        return replace(refused, cleanup_problems=released, cleanup_errors=exc.release_errors)
    try:
        run = _Run(plan, table, log, cancel)
    except BaseException as exc:
        try:
            log.close()  # the run never took it over
        except BaseException as close_exc:
            exc.add_note(f"then closing the journal failed: {_describe_cleanup(close_exc)}")
            winfileops.attach_release_errors(exc, [close_exc])
            if isinstance(exc, Exception) and not isinstance(close_exc, Exception):
                winfileops.attach_release_errors(close_exc, [exc])  # kept as an object too
                raise close_exc from exc  # an interrupt is never dropped
        raise
    try:
        try:
            run.execute()
        except Exception as exc:  # not expected: every step handles its own errors
            run.abort(exc)
    except BaseException as exc:  # an interrupt: release everything, then let it through
        run.close(interrupted=exc)
        raise
    run.close()  # raises an interrupt that came during the cleanup itself, after it
    return run.result()


class _Run:
    def __init__(
        self,
        plan: Plan,
        table: ExtensionTable,
        journal: Journal,
        cancel: Callable[[], bool] | None,
    ) -> None:
        assert plan.destination_root is not None
        self.plan = plan
        self.root = plan.destination_root
        self.table = table
        self.journal = journal
        self.cancel = cancel
        self.listed = {path_key(d) for d in plan.directories_to_create}
        self.results: dict[int, ItemResult] = {}
        self.created: list[Path] = []
        self.problems: list[str] = []
        self.cleanup_problems: list[str] = []
        self.cleanup_errors: list[BaseException] = []  # the same, as raised
        self.stopped: ExecReason | None = None
        self.stop_status = ItemStatus.CANCELLED
        self.stop_detail: str | None = None
        # Every handle the run opened and has not released yet, oldest first.
        self.owned: dict[int, winfileops.Handle] = {}
        self.roots: list[winfileops.VolumeRoot] = []  # proven: source's, destination's
        self.root_anchor: winfileops.Handle | None = None  # destination volume root
        self.source_dir: winfileops.Handle | None = None
        self.volume = 0
        self.target_volume = 0  # of the destination root; the source's, for a move
        self.source_id: tuple[int, int] = (0, 0)
        self.root_dir: winfileops.Handle | _Refusal | None = None
        self.category_dirs: dict[str, winfileops.Handle | _Refusal] = {}
        self.current_intent: int | None = None  # intent of the move in progress
        self.current_item: PlanItem | None = None  # its item, while ``current_intent`` is set

    # --- bookkeeping ---

    def result(self) -> ExecutionResult:
        items = tuple(self.results[i] for i in range(len(self.plan.items)))
        return ExecutionResult(
            self.plan,
            items,
            tuple(self.problems),
            tuple(self.created),
            self.journal.run_id,
            self.journal.path,
            self.stopped,
            self.stop_detail,
            tuple(self.cleanup_problems),
            tuple(self.cleanup_errors),
        )

    def abort(self, exc: Exception) -> None:
        """Mark what is left after an unexpected error; never report it as success."""
        detail = _describe(exc)
        self.problems.append(f"run aborted: {detail}")
        self.stop(ExecReason.RUN_ABORTED, ItemStatus.CANCELLED, detail)
        for index, item in enumerate(self.plan.items):
            if index in self.results:
                continue
            status = ItemStatus.CANCELLED
            if item.action is not PlanAction.SKIP and self.current_intent is not None:
                status, self.current_intent = ItemStatus.RECOVERY_REQUIRED, None
            self.results[index] = ItemResult(item, status, ExecReason.RUN_ABORTED, detail)

    def close(self, interrupted: BaseException | None = None) -> None:
        """Release every handle the run still owns (newest first), then the journal.

        Each is tried exactly once, whatever happens to the others, also on an
        interrupt: a failure is recorded in ``cleanup_problems`` (the item results are
        kept). ``interrupted`` is the interrupt the run is ending with: it gets notes
        and the caller raises it. Without one, an interrupt raised by the cleanup itself
        is raised here, after everything was tried. The interrupt that reaches the
        caller carries every cleanup failure of the run as an object in
        ``release_errors`` (``winfileops.release_failures`` also finds those nested in
        a secondary interrupt or a ``JournalCloseError``), and each as a note.
        """
        pending: BaseException | None = None
        while self.owned:
            _, handle = self.owned.popitem()  # given up before closing: never closed twice
            try:
                handle.close()
            except BaseException as exc:  # one failure must not keep the others open
                self.cleanup_failed(f"cannot close {handle.path}", exc)
                if pending is None and not isinstance(exc, Exception):
                    pending = exc
        try:
            self.journal.close()
        except JournalError as exc:
            self.cleanup_problems.append(str(exc))
            self.cleanup_errors.append(exc)
        except BaseException as exc:  # e.g. the journal's own release was interrupted
            self.cleanup_failed("cannot close journal", exc)
            if pending is None and not isinstance(exc, Exception):
                pending = exc
        raised = interrupted if interrupted is not None else pending
        if raised is not None:
            winfileops.attach_release_errors(raised, self.cleanup_errors)
            self.add_notes(raised)
        if interrupted is None and pending is not None:
            raise pending

    def cleanup_failed(self, what: str, exc: BaseException) -> None:
        """Keep a failed release: as a message (with its notes) and as the object."""
        self.cleanup_problems.append(f"{what}: {_describe_cleanup(exc)}")
        self.cleanup_errors.append(exc)

    def add_notes(self, interrupt: BaseException) -> None:
        """What a caller that gets ``interrupt`` instead of a result has to know."""
        moved = sum(1 for r in self.results.values() if r.status is ItemStatus.MOVED)
        interrupt.add_note(
            f"DownloadsOrganizer: run {self.journal.run_id} interrupted after {moved} verified"
            f" move(s); nothing was started after the interrupt; journal: {self.journal.path}"
        )
        item = self.current_item
        if self.current_intent is not None and item is not None:
            interrupt.add_note(
                f"outcome unknown, check by hand: move of {item.source} to {item.target}"
                f" (journal intent {self.current_intent} has no outcome)"
            )
        for problem in self.cleanup_problems:
            interrupt.add_note(f"cleanup: {problem}")

    def own(self, handle: winfileops.Handle) -> winfileops.Handle:
        """Register a handle just opened: ``release`` or ``close`` releases it."""
        self.owned[id(handle)] = handle
        return handle

    def release(self, handle: winfileops.Handle, after: BaseException | None = None) -> None:
        """Close a handle the run no longer needs; raises nothing but an interrupt.

        If ``CloseHandle`` is not confirmed the handle may still be open: that is
        recorded and the run stops before the next file (the current result stays).
        The handle is given up either way and never closed again. An interrupt is
        recorded the same way and raised.

        ``after`` is the exception the handle is released because of, the very object
        the caller caught (never ``sys.exception()``, which may be an outer one). If it
        is an interrupt, it stays the one raised: a failure here, an interrupt included,
        is attached to it (``winfileops.prevailing``) and the caller re-raises ``after``.
        An interrupt here after an ``Exception`` is raised with ``after`` attached.
        """
        self.owned.pop(id(handle), None)
        try:
            handle.close()
        except BaseException as exc:
            self.cleanup_failed(f"cannot close {handle.path}", exc)
            self.stop(ExecReason.CLEANUP_FAILED, ItemStatus.CANCELLED, self.cleanup_problems[-1])
            if after is not None and not isinstance(after, Exception):
                winfileops.prevailing(after, exc)  # ``after`` now carries ``exc``
                return  # and the caller raises ``after``, the very object
            if not isinstance(exc, Exception):
                if after is not None:
                    winfileops.prevailing(after, exc)  # the interrupt carries ``after``
                raise

    @contextlib.contextmanager
    def holding(self, handle: winfileops.Handle) -> Iterator[winfileops.Handle]:
        """Release ``handle`` however the block ends: an exception leaving the block is
        released ``after`` and stays the one raised (see ``release``)."""
        try:
            yield handle
        except BaseException as exc:
            self.release(handle, after=exc)
            raise
        self.release(handle)

    def stop(self, reason: ExecReason, status: ItemStatus, detail: str | None) -> None:
        if self.stopped is None:
            self.stopped, self.stop_status, self.stop_detail = reason, status, detail

    def log(self, operation: Operation, status: Status, **fields: object) -> int | None:
        """Write a journal record; on failure stop the run and return ``None``."""
        if self.journal.failed:
            return None
        try:
            return self.journal.write(operation, status, **fields)  # type: ignore[arg-type]
        except JournalError as exc:
            self.problems.append(str(exc))
            self.stop(ExecReason.JOURNAL_FAILED, ItemStatus.CANCELLED, str(exc))
            return None

    def finish(
        self,
        index: int,
        status: ItemStatus,
        reason: ExecReason,
        detail: str | None = None,
        error_code: int | None = None,
        intent: int | None = None,
    ) -> None:
        item = self.plan.items[index]
        self.results[index] = ItemResult(item, status, reason, detail, error_code)
        if intent is not None and intent == self.current_intent:
            self.current_intent = None  # answered: no longer in progress
        self.log(
            Operation.MOVE,
            _JOURNAL_STATUS[status],
            intent=intent,
            source=item.source,
            target=item.target,
            reason=str(reason),
            error_code=error_code,
            error_message=detail,
        )

    def refuse(self, index: int, refusal: _Refusal, intent: int | None = None) -> None:
        self.finish(
            index, refusal.status, refusal.reason, refusal.detail, refusal.error_code, intent
        )

    def cancel_requested(self) -> bool:
        if self.cancel is None:
            return False
        try:
            return bool(self.cancel())
        except Exception as exc:  # a broken cancel callback means: stop
            self.problems.append(f"cancel callback failed: {_describe(exc)}")
            return True

    # --- run ---

    def execute(self) -> None:
        plan = self.plan
        moves = len(plan.moves)
        self.log(
            Operation.RUN,
            Status.STARTED,
            source=plan.source,
            target=self.root,
            reason=f"items={len(plan.items)} moves={moves}",
        )
        if self.stopped is None and moves:
            try:
                self.check_run()
            except Exception as exc:  # nothing was changed yet: refuse the whole run
                self.problems.append(f"run check failed: {_describe(exc)}")
                self.stop(ExecReason.RUN_ABORTED, ItemStatus.SKIPPED, _describe(exc))
        for index, item in enumerate(plan.items):
            if item.action is PlanAction.SKIP:
                self.finish(index, ItemStatus.SKIPPED, ExecReason.PLANNED_SKIP, str(item.reason))
                continue
            if self.stopped is not None:
                self.finish(index, self.stop_status, self.stopped, self.stop_detail)
                continue
            if self.cancel_requested():
                self.stop(ExecReason.CANCELLED, ItemStatus.CANCELLED, "cancelled before this file")
                self.finish(index, ItemStatus.CANCELLED, ExecReason.CANCELLED)
                continue
            self.current_intent = None
            try:
                self.move(index, item)
            except Exception as exc:  # fail closed: never continue after the unexpected
                detail = _describe(exc)
                self.problems.append(f"{item.source}: {detail}")
                if index not in self.results:
                    attempted = self.current_intent is not None
                    status = ItemStatus.RECOVERY_REQUIRED if attempted else ItemStatus.FAILED
                    self.finish(
                        index, status, ExecReason.UNEXPECTED_ERROR, detail,
                        intent=self.current_intent,
                    )  # fmt: skip
                self.stop(ExecReason.RUN_ABORTED, ItemStatus.CANCELLED, detail)
        counts = " ".join(
            f"{s}={sum(1 for r in self.results.values() if r.status is s)}" for s in ItemStatus
        )
        self.log(
            Operation.RUN,
            Status.FINISHED,
            reason=str(self.stopped or ExecReason.COMPLETED),
            error_message=f"{counts} directories_created={len(self.created)}"
            + (f" stop: {self.stop_detail}" if self.stop_detail else ""),
        )

    def check_run(self) -> None:
        """Prove and hold both volume roots, read-only run-level checks, then open and
        hold the source directory and every directory above it. Nothing is created or
        moved before this passed."""
        plan = self.plan
        source_root = self.open_root(plan.source)
        if isinstance(source_root, _Refusal):
            return self.stop(source_root.reason, ItemStatus.SKIPPED, source_root.detail)
        root_anchor = self.open_root(self.root)
        if isinstance(root_anchor, _Refusal):
            return self.stop(root_anchor.reason, ItemStatus.SKIPPED, root_anchor.detail)
        self.root_anchor = root_anchor
        try:
            source_chain = check_directory_chain(plan.source)
        except PathSafetyError as exc:
            return self.stop(ExecReason.SOURCE_DIR_CHANGED, ItemStatus.SKIPPED, str(exc))
        source_identity = file_identity(source_chain.deepest_stat)
        if not source_chain.exists:
            return self.stop(
                ExecReason.SOURCE_DIR_CHANGED, ItemStatus.SKIPPED, f"{plan.source} is missing"
            )
        if source_identity is None:
            return self.stop(
                ExecReason.VOLUME_UNKNOWN, ItemStatus.SKIPPED, "source directory id unknown"
            )
        try:
            root_chain = check_directory_chain(self.root)
        except PathSafetyError as exc:
            return self.stop(ExecReason.DESTINATION_CHANGED, ItemStatus.SKIPPED, str(exc))
        unplanned = [str(p) for p in root_chain.missing if path_key(p) not in self.listed]
        if unplanned:
            detail = f"missing and not planned for creation: {', '.join(unplanned)}"
            return self.stop(ExecReason.TARGET_DIR_MISSING, ItemStatus.SKIPPED, detail)
        root_identity = file_identity(root_chain.deepest_stat)
        if root_identity is None:
            detail = f"volume of {root_chain.deepest} unknown"
            return self.stop(ExecReason.VOLUME_UNKNOWN, ItemStatus.SKIPPED, detail)
        conflict = self.volume_conflict(source_identity[0], root_identity[0])
        if conflict is not None:
            return self.stop(ExecReason.CROSS_VOLUME, ItemStatus.SKIPPED, conflict)
        if root_chain.exists:
            relation = directory_relation(
                plan.source, source_chain.deepest_stat, self.root, root_chain.deepest_stat
            )
            if relation is DirectoryRelation.SAME:
                return self.stop(ExecReason.TARGET_DIR_IS_SOURCE, ItemStatus.SKIPPED, None)
            if relation is DirectoryRelation.UNKNOWN:
                detail = "destination root cannot be told apart from the source"
                return self.stop(ExecReason.VOLUME_UNKNOWN, ItemStatus.SKIPPED, detail)
        outcome = self.walk(source_root, plan.source, create=False)
        if isinstance(outcome, _Refusal):
            return self.stop(ExecReason.SOURCE_DIR_CHANGED, ItemStatus.SKIPPED, outcome.detail)
        self.source_dir = outcome
        info = winfileops.query(outcome)
        if (info.volume_serial, info.file_id) != source_identity:
            detail = f"{plan.source} was replaced during the checks"
            return self.stop(ExecReason.SOURCE_DIR_CHANGED, ItemStatus.SKIPPED, detail)
        # The path checks above went through the drive letters: still the proven ones?
        for root in self.roots:
            try:
                winfileops.check_mapping(root)
            except winfileops.UnverifiedRootError as exc:
                return self.stop(ExecReason.ROOT_UNVERIFIED, ItemStatus.SKIPPED, str(exc))
        self.volume, self.source_id = info.volume_serial, (info.volume_serial, info.file_id)
        self.target_volume = root_identity[0]
        return None

    def volume_conflict(self, source_serial: int, root_serial: int) -> str | None:
        """Why the destination root's volume does not fit the source's (``None``: fits)."""
        if root_serial != source_serial:
            return f"{self.root} is on another volume than {self.plan.source}"
        return None

    def root_conflict(self, path: Path, root: winfileops.VolumeRoot) -> str | None:
        """Why the proven volume root of ``path`` does not fit the roots proven before."""
        if self.roots and root.device.upper() != self.roots[0].device.upper():
            return f"{path} is on {root.device}, not on {self.roots[0].device}"
        return None

    def dir_access(self, path: Path) -> int:
        """The access a directory of the run is opened with."""
        return _DIR_ACCESS

    # --- directories ---

    def dir_problem(self, handle: winfileops.Handle, *, final: bool) -> _Refusal | None:
        info = winfileops.query(handle)
        if info.reparse_point:
            detail = f"{handle.path} is a link or junction (tag {info.reparse_tag:#x})"
            return _Refusal(ItemStatus.SKIPPED, ExecReason.TARGET_DIR_INVALID, detail)
        if not info.directory:
            detail = f"{handle.path} is not a directory"
            return _Refusal(ItemStatus.SKIPPED, ExecReason.TARGET_DIR_INVALID, detail)
        if final and self.target_volume and info.volume_serial != self.target_volume:
            detail = f"{handle.path} is on another volume"
            return _Refusal(ItemStatus.SKIPPED, ExecReason.CROSS_VOLUME, detail)
        if final and (info.volume_serial, info.file_id) == self.source_id:
            return _Refusal(ItemStatus.SKIPPED, ExecReason.TARGET_DIR_IS_SOURCE, str(handle.path))
        return None

    def open_root(self, path: Path) -> winfileops.Handle | _Refusal:
        """Open the volume root of ``path`` and prove it (``prove_volume_root``).

        Proven means: the drive letter is defined as a disk volume (no subst, network
        or other mapped drive, no UNC path), and the object the handle refers to is the
        root of that volume, on local NTFS, as the drive letter still says afterwards.
        Every root of a run must be on the same volume. The handle is owned by the run.
        """
        try:
            root = winfileops.volume_root(path)
        except winfileops.UnverifiedRootError as exc:
            return _Refusal(ItemStatus.SKIPPED, ExecReason.ROOT_UNVERIFIED, str(exc))
        try:
            handle = self.own(winfileops.open_anchor(root.anchor))
        except OSError as exc:
            detail = f"cannot open {root.anchor}: {_describe(exc)}"
            return _Refusal(
                ItemStatus.SKIPPED, ExecReason.ROOT_UNVERIFIED, detail, _error_code(exc)
            )
        try:
            winfileops.prove_volume_root(handle, root)
        except winfileops.UnverifiedRootError as exc:
            self.release(handle, after=exc)
            return _Refusal(ItemStatus.SKIPPED, ExecReason.ROOT_UNVERIFIED, str(exc))
        except BaseException as exc:
            self.release(handle, after=exc)
            raise
        conflict = self.root_conflict(path, root)
        if conflict is not None:
            self.release(handle)
            return _Refusal(ItemStatus.SKIPPED, ExecReason.CROSS_VOLUME, conflict)
        self.roots.append(root)
        return handle

    def walk(
        self, handle: winfileops.Handle, path: Path, *, create: bool
    ) -> winfileops.Handle | _Refusal:
        """Walk ``path`` down from ``handle``, its proven volume root.

        Each directory of the chain is opened relative to its parent's handle without
        ``FILE_SHARE_DELETE`` and checked (no link, a directory) before its child is
        opened; every handle of the chain, the root's included, stays owned until the
        run is closed. A parent is never released while its child is in use: nobody can
        rename, delete or replace any directory of the chain from the moment it was
        opened. The handle of ``path`` is returned. On a refusal the directories this
        walk opened are released, newest first, and the refusal is returned.
        """
        current = Path(path.anchor)
        parts = path.parts[1:]
        opened: list[winfileops.Handle] = []
        for position, part in enumerate(parts):
            current = current / part
            final = position == len(parts) - 1
            outcome = self.open_dir(handle, current, final=final, create=create)
            if isinstance(outcome, _Refusal):
                for held in reversed(opened):  # the chain is not used: no handle stays
                    self.release(held)
                return outcome
            opened.append(outcome)
            handle = outcome
        return handle

    def open_dir(
        self, parent: winfileops.Handle, path: Path, *, final: bool, create: bool
    ) -> winfileops.Handle | _Refusal:
        """Open directory ``path`` below ``parent``; create it if planned and missing.

        Always without ``FILE_SHARE_DELETE`` (``_HELD_SHARE``): an intermediate
        directory is held like the final one. If someone else has it open for deletion
        or renaming, the open fails with a sharing violation and the directory is
        refused; it is never opened again with a weaker share mode.
        """
        try:
            handle = self.own(
                winfileops.open_relative(
                    parent,
                    path.name,
                    directory=True,
                    access=self.dir_access(path),
                    share=_HELD_SHARE,
                )[0]
            )
        except FileNotFoundError:
            if not create or path_key(path) not in self.listed:
                detail = f"{path} does not exist and is not planned for creation"
                return _Refusal(ItemStatus.SKIPPED, ExecReason.TARGET_DIR_MISSING, detail)
            return self.mkdir(parent, path, final=final)
        except NotADirectoryError as exc:
            detail = f"{path} is not a directory"
            return _Refusal(
                ItemStatus.SKIPPED, ExecReason.TARGET_DIR_INVALID, detail, _error_code(exc)
            )
        except OSError as exc:
            return _Refusal(
                ItemStatus.FAILED, ExecReason.TARGET_DIR_FAILED, _describe(exc), _error_code(exc)
            )
        cause: OSError | None = None  # turned into the refusal: kept if releasing fails
        try:
            problem = self.dir_problem(handle, final=final)
        except OSError as exc:
            cause = exc
            problem = _Refusal(
                ItemStatus.FAILED, ExecReason.TARGET_DIR_FAILED, _describe(exc), _error_code(exc)
            )
        except BaseException as exc:
            self.release(handle, after=exc)  # not handed to anyone yet
            raise
        if problem is not None:
            self.release(handle, after=cause)
            return problem
        return handle

    def mkdir(
        self, parent: winfileops.Handle, path: Path, *, final: bool
    ) -> winfileops.Handle | _Refusal:
        intent = self.log(Operation.MKDIR, Status.INTENT, target=path)
        if intent is None:
            return _Refusal(ItemStatus.CANCELLED, ExecReason.JOURNAL_FAILED, self.stop_detail)

        def outcome(status: Status, reason: str, exc: OSError | None = None) -> None:
            self.log(
                Operation.MKDIR, status, intent=intent, target=path, reason=reason,
                error_code=None if exc is None else _error_code(exc),
                error_message=None if exc is None else _describe(exc),
            )  # fmt: skip

        try:
            handle, created = winfileops.open_relative(
                parent, path.name, directory=True, access=self.dir_access(path),
                share=_HELD_SHARE, create=True,
            )  # fmt: skip
            self.own(handle)
        except FileExistsError:
            # Created by someone else since the check: verify it like any existing one.
            try:
                handle, created = winfileops.open_relative(
                    parent, path.name, directory=True, access=self.dir_access(path),
                    share=_HELD_SHARE,
                )  # fmt: skip
                self.own(handle)
            except OSError as exc:
                outcome(Status.FAILED, str(ExecReason.TARGET_DIR_INVALID), exc)
                return _Refusal(
                    ItemStatus.SKIPPED, ExecReason.TARGET_DIR_INVALID,
                    f"{path} appeared and is not a usable directory: {_describe(exc)}",
                    _error_code(exc),
                )  # fmt: skip
        except OSError as exc:
            outcome(Status.FAILED, str(ExecReason.TARGET_DIR_FAILED), exc)
            detail = f"cannot create {path}: {_describe(exc)}"
            return _Refusal(
                ItemStatus.FAILED, ExecReason.TARGET_DIR_FAILED, detail, _error_code(exc)
            )
        try:
            if created:
                self.created.append(path)
            cause: OSError | None = None  # turned into the refusal: kept if releasing fails
            try:
                problem = self.dir_problem(handle, final=final)
            except OSError as exc:
                cause = exc
                problem = _Refusal(
                    ItemStatus.FAILED, ExecReason.VERIFY_FAILED, _describe(exc), _error_code(exc)
                )
            if problem is not None:
                self.release(handle, after=cause)
                if created:  # we made something we cannot vouch for: stop
                    outcome(Status.RECOVERY_REQUIRED, str(ExecReason.VERIFY_FAILED))
                    self.problems.append(f"{path}: created but not verified: {problem.detail}")
                    self.stop(ExecReason.RUN_ABORTED, ItemStatus.CANCELLED, problem.detail)
                    return _Refusal(ItemStatus.CANCELLED, ExecReason.RUN_ABORTED, problem.detail)
                outcome(Status.FAILED, str(problem.reason))
                return problem
            outcome(Status.DONE, "created" if created else "already_existed")
        except BaseException as exc:
            if id(handle) in self.owned:  # not given up above already: never released twice
                self.release(handle, after=exc)  # not handed to anyone yet; intent unanswered
            raise
        if self.stopped is not None:  # the outcome could not be journaled
            self.release(handle)
            return _Refusal(ItemStatus.CANCELLED, self.stopped, self.stop_detail)
        return handle

    def root_handle(self) -> winfileops.Handle | _Refusal:
        if self.root_dir is None:
            anchor, self.root_anchor = self.root_anchor, None
            if anchor is None:  # never proven: nothing may be created there
                detail = f"the volume root of {self.root} was not proven"
                self.root_dir = _Refusal(ItemStatus.SKIPPED, ExecReason.ROOT_UNVERIFIED, detail)
            else:
                self.root_dir = self.walk(anchor, self.root, create=True)
        return self.root_dir

    def category_handle(self, category: Category) -> winfileops.Handle | _Refusal:
        path = self.root / category.folder
        key = path_key(path)
        if key not in self.category_dirs:
            root = self.root_handle()
            if isinstance(root, _Refusal):
                return root
            self.category_dirs[key] = self.open_dir(root, path, final=True, create=True)
        return self.category_dirs[key]

    # --- files ---

    def move(self, index: int, item: PlanItem) -> None:
        assert self.source_dir is not None
        name_problem = folder_name_problem(item.source.name) or folder_name_problem(
            item.target.name
        )
        if name_problem is not None:
            return self.finish(index, ItemStatus.SKIPPED, ExecReason.UNSAFE_NAME, name_problem)
        snapshot = item.file
        if snapshot.file_id is None or snapshot.device is None:
            detail = "file id or volume unknown in the plan"
            return self.finish(index, ItemStatus.SKIPPED, ExecReason.METADATA_UNKNOWN, detail)
        if snapshot.device != self.volume:
            detail = "the plan's snapshot is from another volume"
            return self.finish(index, ItemStatus.SKIPPED, ExecReason.SOURCE_REPLACED, detail)
        category_dir = self.category_handle(self.table.category(item.category_key))
        if isinstance(category_dir, _Refusal):
            return self.refuse(index, category_dir)
        if self.stopped is not None:  # e.g. the journal failed while creating directories
            return self.finish(index, self.stop_status, self.stopped, self.stop_detail)
        try:
            handle, _ = winfileops.open_relative(
                self.source_dir,
                item.source.name,
                directory=False,
                access=_FILE_ACCESS,
                share=winfileops.FILE_SHARE_READ,
            )
            self.own(handle)
        except OSError as exc:
            return self.refuse(index, _open_refusal(exc))
        # A failed close stops the run; it does not change what happened to the file.
        with self.holding(handle):
            mismatch = _source_mismatch(winfileops.query(handle), snapshot)
            if mismatch is not None:
                return self.refuse(index, mismatch)
            intent = self.log(Operation.MOVE, Status.INTENT, source=item.source, target=item.target)
            if intent is None:
                return self.finish(index, ItemStatus.CANCELLED, ExecReason.JOURNAL_FAILED)
            self.current_intent, self.current_item = intent, item
            identity = (snapshot.device, snapshot.file_id)
            try:
                winfileops.rename_into(handle, category_dir, item.target.name)
            except OSError as exc:
                refusal = _rename_refusal(exc)
                where = self.locate(item, category_dir, identity)
                if where != "source":
                    detail = f"rename failed ({_describe(exc)}), file is at: {where}"
                    refusal = _Refusal(
                        ItemStatus.RECOVERY_REQUIRED, ExecReason.MOVE_FAILED, detail,
                        _error_code(exc),
                    )  # fmt: skip
                    self.stop(ExecReason.RUN_ABORTED, ItemStatus.CANCELLED, detail)
                return self.refuse(index, refusal, intent)
        where = self.locate(item, category_dir, identity)
        if where != "target":
            detail = f"renamed, but the file is at: {where}"
            self.finish(
                index, ItemStatus.RECOVERY_REQUIRED, ExecReason.VERIFY_FAILED, detail,
                intent=intent,
            )  # fmt: skip
            return self.stop(ExecReason.RUN_ABORTED, ItemStatus.CANCELLED, detail)
        return self.finish(index, ItemStatus.MOVED, ExecReason.COMPLETED, intent=intent)

    def locate(
        self, item: PlanItem, category_dir: winfileops.Handle, identity: tuple[int, int]
    ) -> str:
        """Where the file with ``identity`` is now: "source", "target", "both" or "unknown"."""
        assert self.source_dir is not None
        at_source = self.has_identity(self.source_dir, item.source.name, identity)
        at_target = self.has_identity(category_dir, item.target.name, identity)
        if at_source is None or at_target is None or at_source == at_target:
            return "both" if at_source and at_target else "unknown"
        return "source" if at_source else "target"

    def has_identity(
        self, directory: winfileops.Handle, name: str, identity: tuple[int, int]
    ) -> bool | None:
        """Whether ``name`` in ``directory`` is the file ``identity``; ``None`` if unknowable."""
        try:
            handle, _ = winfileops.open_relative(
                directory, name, directory=False, access=_PROBE_ACCESS, share=winfileops.SHARE_ALL
            )
            self.own(handle)
        except FileNotFoundError:
            return False
        except OSError as exc:
            if getattr(exc, "ntstatus", None) == _STATUS_FILE_IS_A_DIRECTORY:
                return False  # a directory or junction: certainly not the moved file
            return None
        with self.holding(handle):
            try:
                info = winfileops.query(handle)
            except OSError:
                return None
        return (info.volume_serial, info.file_id) == identity


def _open_refusal(exc: OSError) -> _Refusal:
    code = _error_code(exc)
    if isinstance(exc, FileNotFoundError):
        return _Refusal(ItemStatus.SKIPPED, ExecReason.SOURCE_MISSING, _describe(exc), code)
    if getattr(exc, "ntstatus", None) == _STATUS_FILE_IS_A_DIRECTORY:
        detail = "a directory now has the file's name"
        return _Refusal(ItemStatus.SKIPPED, ExecReason.SOURCE_REPLACED, detail, code)
    if code == winfileops.ERROR_SHARING_VIOLATION:
        return _Refusal(ItemStatus.SKIPPED, ExecReason.SOURCE_IN_USE, _describe(exc), code)
    if isinstance(exc, PermissionError):
        return _Refusal(ItemStatus.FAILED, ExecReason.ACCESS_DENIED, _describe(exc), code)
    return _Refusal(ItemStatus.FAILED, ExecReason.SOURCE_CHECK_FAILED, _describe(exc), code)


def _rename_refusal(exc: OSError) -> _Refusal:
    code = _error_code(exc)
    if isinstance(exc, FileExistsError):
        detail = "the target name exists now; the existing file was not touched"
        return _Refusal(ItemStatus.SKIPPED, ExecReason.TARGET_EXISTS, detail, code)
    if code == winfileops.ERROR_NOT_SAME_DEVICE:
        return _Refusal(ItemStatus.FAILED, ExecReason.CROSS_VOLUME, _describe(exc), code)
    if isinstance(exc, PermissionError):
        return _Refusal(ItemStatus.FAILED, ExecReason.ACCESS_DENIED, _describe(exc), code)
    return _Refusal(ItemStatus.FAILED, ExecReason.MOVE_FAILED, _describe(exc), code)


def _source_mismatch(info: winfileops.HandleInfo, snapshot: FileInfo) -> _Refusal | None:
    """Why the opened file is not the one the plan snapshot describes, if it is not."""
    if info.delete_pending:
        return _Refusal(ItemStatus.SKIPPED, ExecReason.SOURCE_MISSING, "file is being deleted")
    if info.directory:
        return _Refusal(ItemStatus.SKIPPED, ExecReason.SOURCE_REPLACED, "now a directory")
    if (info.volume_serial, info.file_id) != (snapshot.device, snapshot.file_id):
        detail = "another file has the name now (file id differs)"
        return _Refusal(ItemStatus.SKIPPED, ExecReason.SOURCE_REPLACED, detail)
    if info.reparse_point or info.attributes & _UNSAFE_ATTRIBUTES:
        detail = f"attributes {info.attributes:#x}, reparse tag {info.reparse_tag:#x}"
        return _Refusal(ItemStatus.SKIPPED, ExecReason.SOURCE_UNSAFE, detail)
    changed = [
        name
        for name, now, then in (
            ("size", info.size, snapshot.size),
            ("mtime", info.mtime_ns, snapshot.mtime_ns),
            ("attributes", info.attributes, snapshot.attributes),
        )
        if now != then
    ]
    if changed:
        detail = f"changed since the preview: {', '.join(changed)}"
        return _Refusal(ItemStatus.SKIPPED, ExecReason.SOURCE_CHANGED, detail)
    return None
