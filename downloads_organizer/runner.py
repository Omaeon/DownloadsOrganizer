"""One entry point for a previewed plan: the R3A move or the cross-volume COPY (R3B-5).

``run_plan`` decides the kind of run from the plan alone, before any journal exists:

* a plan proven ``SAME`` (``cross_copy`` false) is the R3A move: ``execute_plan`` as it
  is, with the v1 journal;
* a ``cross_copy`` plan proven ``CROSS`` is a copy that keeps every source, journaled
  in one v3 journal (``journal_v3.JournalV3``), each file by ``copycommit.copy_item``;
* anything else (``AMBIGUOUS``, ``UNVERIFIED``, no proof, a forged mix) is refused.

A move is never turned into a copy, also not after an error. Before anything is
created, both kinds prove the volumes again (``check_plan_fresh``); a copy also needs
barrier 1 (``features.cross_volume_copy_enabled``) and barrier 2, the ACL policy
(``AclGate``). D3 is not decided, so the production gate (``UndecidedAcl``) refuses
every copy before its journal exists, and again for each file before its target
directory and its temporary file are created. A refusal changes nothing and writes
no journal.

The copy run reuses the R3A run (``executor._Run``): the same proven volume roots,
the same directory walk from them, handle by handle, the same held directories. What
differs: each root must be the volume the preview proved for its place, the category
directories are held with ``FILE_ADD_FILE`` (the temporary file is created there) and
``READ_CONTROL``, and each file is opened as ``H_SRC`` (read only, shared for reading
only) and handed to ``copy_item``. Nothing here deletes, renames or writes a source.
After a copy whose outcome is not established (recovery required, commit unknown, a
temporary file left or unknown, a failed journal, a failed release) no further file is
started; nothing is recovered automatically.

R3B integration, security (``aclcreate``): after the run checks, the source directory's
descriptor (through its held handle) and the creator SID are read once; a failure
refuses every file. A missing directory is created by ``aclcreate.create_category``
(one ``NtCreateFile`` relative to the held parent, the protected DACL derived from the
source directory) inside a v3 ``mkdir`` block: the intent, with the parent's file id, is
durable before the call, and the terminal record says what the call established
(``journal_v3.DirState``) and the directory's id. A name taken meanwhile is opened and
checked like any existing directory (``existing``); a directory created and not proven,
or an outcome not known, stops the run (recovery required, never removed here). Each
file's descriptor is read through ``H_SRC`` and handed to ``copy_item`` (``AclCopy``):
its temporary file is created by ``aclcreate.create_temp`` in the category proven to be
the one held, and its security is checked again before the commit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from downloads_organizer import aclcreate, aclproof, features, winfileops, winprims
from downloads_organizer.aclcreate import Created, CreateReason, Creation, Identity
from downloads_organizer.aclproof import Descriptor, MalformedDescriptor, Sid
from downloads_organizer.copycommit import (
    DEFAULT_ALLOWED_ATTRIBUTES,
    AclCopy,
    CopyResult,
    CopyStatus,
    Ops,
    SourceFacts,
    Stop,
    TransferState,
    WinOps,
    copy_item,
)
from downloads_organizer.executor import (
    _DIR_ACCESS,
    _HELD_SHARE,
    _STATUS_FILE_IS_A_DIRECTORY,
    ExecReason,
    ExecutionResult,
    ItemStatus,
    _describe,
    _describe_cleanup,
    _Refusal,
    _Run,
    _source_mismatch,
    execute_plan,
    validate_plan,
)
from downloads_organizer.extensions import ExtensionTable
from downloads_organizer.journal import Journal, JournalError, Operation, Status
from downloads_organizer.journal_v2 import (
    Commit,
    OperationV2,
    StatusV2,
    TempState,
    VolumeRef,
)
from downloads_organizer.journal_v3 import DirState, JournalV3
from downloads_organizer.models import Plan, PlanAction, PlanItem, VolumeMode
from downloads_organizer.pathsafety import folder_name_problem, path_key
from downloads_organizer.volumeproof import FreshnessCheck, FreshnessReason, check_plan_fresh
from downloads_organizer.winfileops import HandleState, OpenOutcome


class RunRefusalReason(StrEnum):
    PLAN_INVALID = "plan_invalid"
    NO_VOLUME_PROOF = "no_volume_proof"
    VOLUME_REFUSED = "volume_refused"  # not SAME for a move, not CROSS for a copy
    COPY_DISABLED = "copy_disabled"  # barrier 1
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    ACL_UNDECIDED = "acl_undecided"  # barrier 2
    PLAN_STALE = "plan_stale"
    JOURNAL_FAILED = "journal_failed"


ACL_REFUSED = "acl_refused"  # the reason of a file the ACL gate refused
# what a copy of the unnamed stream alone would lose (D2, D5 open): the file is skipped
SOURCE_ATTRIBUTES = "source_attributes"  # an attribute outside the allowlist
SOURCE_STREAMS = "source_streams"  # another data stream: Zone.Identifier (MOTW), ...
SOURCE_EA = "source_ea"  # extended attributes
ACL_UNDECIDED_DETAIL = (
    "ACL policy D3 for the temporary file and the final file is not decided:"
    " no cross-volume copy may be made"
)
_OPEN_PHASES = frozenset({"intent", "staged", "written", "verified", "commit_attempted"})
_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND, _ERROR_ACCESS_DENIED = 2, 3, 5


@dataclass(frozen=True, slots=True)
class RunRefusal:
    """``run_plan`` refused the plan before anything was opened for writing, created or
    journaled; ``freshness`` is the failed ``check_plan_fresh``, if it ran."""

    plan: object
    reason: RunRefusalReason
    detail: str | None = None
    problems: tuple[str, ...] = ()
    freshness: FreshnessCheck | None = None
    release_errors: tuple[BaseException, ...] = field(default=(), compare=False)

    changed_anything = False
    complete = False
    partial = False


class AclGate(Protocol):
    """Barrier 2: whether the access rights the copy would give its files are agreed.

    ``check_run`` before the run's journal exists, ``check_item`` before a file's
    target directory and temporary file are created; a text is the refusal."""

    def check_run(self, plan: Plan) -> str | None: ...

    def check_item(self, source: Path, target: Path) -> str | None: ...


class UndecidedAcl:
    """The production gate while OWNER_DECISION D3 is open: every copy is refused."""

    def check_run(self, plan: Plan) -> str | None:
        return ACL_UNDECIDED_DETAIL

    def check_item(self, source: Path, target: Path) -> str | None:
        return ACL_UNDECIDED_DETAIL


@dataclass(frozen=True, slots=True)
class CopyOptions:
    """How a copy run is made. The defaults are the production ones."""

    acl: AclGate = field(default_factory=UndecidedAcl)
    ops: Ops | None = None  # ``copycommit.WinOps`` unless a test scripts one
    acl_ops: aclcreate.Ops | None = None  # ``aclcreate.NtOps`` unless a test scripts one
    creator: Callable[[], Sid] = field(default=aclproof.creator_sid)  # owner of new objects
    block_size: int = 1 << 20
    allowed_attributes: int = DEFAULT_ALLOWED_ATTRIBUTES


@dataclass(frozen=True, slots=True, eq=False)
class CopyItemResult:
    """What happened to one plan item of a copy run (the source is always kept).

    ``transfer`` is the copy's state if ``copy_item`` ran (``None`` before it);
    ``cleanup_errors`` are its release failures as raised."""

    item: PlanItem
    status: CopyStatus
    reason: str
    detail: str | None = None
    error_code: int | None = None
    transfer: TransferState | None = None
    cleanup_errors: tuple[BaseException, ...] = ()


@dataclass(frozen=True, slots=True, eq=False)
class CopyRunResult:
    """Outcome of a copy run, like ``ExecutionResult``: one result per item, in order."""

    plan: Plan
    items: tuple[CopyItemResult, ...]
    problems: tuple[str, ...] = ()
    directories_created: tuple[Path, ...] = ()
    run_id: str | None = None
    journal_path: Path | None = None
    stopped: ExecReason | None = None
    stop_detail: str | None = None
    cleanup_problems: tuple[str, ...] = ()
    cleanup_errors: tuple[BaseException, ...] = ()

    def count(self, status: CopyStatus) -> int:
        return sum(1 for r in self.items if r.status is status)

    @property
    def counts(self) -> dict[CopyStatus, int]:
        return {status: self.count(status) for status in CopyStatus}

    @property
    def changed_anything(self) -> bool:
        """Something may be on disk now: copies, directories, a temporary file left."""
        return bool(self.directories_created) or any(
            r.status in (CopyStatus.COPIED, CopyStatus.RECOVERY_REQUIRED)
            or (r.transfer is not None and r.transfer.temp_state in _UNCERTAIN_TEMP)
            for r in self.items
        )

    @property
    def complete(self) -> bool:
        """Every planned copy was made and verified, without any problem."""
        return (
            not self.problems
            and not self.cleanup_problems
            and self.stopped is None
            and all(
                r.status is CopyStatus.COPIED
                or (r.status is CopyStatus.SKIPPED and r.reason == ExecReason.PLANNED_SKIP)
                for r in self.items
            )
        )

    @property
    def partial(self) -> bool:
        return self.changed_anything and not self.complete


_UNCERTAIN_TEMP = frozenset({TempState.LEFT, TempState.UNKNOWN})
_COPY_STATUS = {
    ItemStatus.MOVED: CopyStatus.COPIED,
    ItemStatus.SKIPPED: CopyStatus.SKIPPED,
    ItemStatus.FAILED: CopyStatus.FAILED,
    ItemStatus.CANCELLED: CopyStatus.CANCELLED,
    ItemStatus.RECOVERY_REQUIRED: CopyStatus.RECOVERY_REQUIRED,
}
_UNSTARTED = {
    CopyStatus.SKIPPED: StatusV2.SKIPPED,
    CopyStatus.FAILED: StatusV2.FAILED,
    CopyStatus.CANCELLED: StatusV2.CANCELLED,
}
_STOP_REASON = {
    Stop.JOURNAL_FAILED: ExecReason.JOURNAL_FAILED,
    Stop.RUN_ABORTED: ExecReason.RUN_ABORTED,
    Stop.CLEANUP_FAILED: ExecReason.CLEANUP_FAILED,
}


def run_plan(
    plan: Plan,
    table: ExtensionTable,
    *,
    cancel: Callable[[], bool] | None = None,
    journal: Callable[[], Journal] | None = None,
    journal_v3: Callable[[], JournalV3] | None = None,
    copy: CopyOptions | None = None,
) -> RunRefusal | ExecutionResult | CopyRunResult:
    """Run a plan from ``volumeproof.preview_plan`` after the user confirmed it.

    The kind of run and the journal version are fixed here, from the plan, before any
    journal is created (module docstring). ``journal`` creates the v1 journal of a
    move, ``journal_v3`` the v3 journal of a copy; ``copy`` only matters for a copy.
    Never raises for filesystem or journal problems; an interrupt is raised as
    ``execute_plan`` raises it.
    """
    problems = validate_plan(plan, table)
    if problems:
        return RunRefusal(plan, RunRefusalReason.PLAN_INVALID, "; ".join(problems), problems)
    volume = plan.volume
    if volume is None:
        detail = "the plan was not previewed with a volume proof"
        return RunRefusal(plan, RunRefusalReason.NO_VOLUME_PROOF, detail)
    if plan.cross_copy is False and volume.mode is VolumeMode.SAME:
        copying = False
    elif plan.cross_copy is True and volume.mode is VolumeMode.CROSS:
        copying = True
    else:
        detail = f"volumes are {volume.mode}, cross_copy is {plan.cross_copy!r}"
        return RunRefusal(plan, RunRefusalReason.VOLUME_REFUSED, detail)
    if copying:
        if not features.cross_volume_copy_enabled():
            detail = "cross-volume copy is not enabled"
            return RunRefusal(plan, RunRefusalReason.COPY_DISABLED, detail)
        if not winfileops.AVAILABLE:
            detail = "copies are only supported on Windows"
            return RunRefusal(plan, RunRefusalReason.UNSUPPORTED_PLATFORM, detail)
        options = CopyOptions() if copy is None else copy
        acl = options.acl.check_run(plan)
        if acl is not None:
            return RunRefusal(plan, RunRefusalReason.ACL_UNDECIDED, acl)
    fresh = check_plan_fresh(plan)
    if not fresh.fresh:
        reason = {
            FreshnessReason.NO_VOLUME_PROOF: RunRefusalReason.NO_VOLUME_PROOF,
            FreshnessReason.VOLUME_REFUSED: RunRefusalReason.VOLUME_REFUSED,
        }.get(fresh.reason, RunRefusalReason.PLAN_STALE)  # type: ignore[arg-type]
        return RunRefusal(plan, reason, fresh.detail, (), fresh, fresh.release_errors)
    if not copying:
        return execute_plan(plan, table, cancel=cancel, journal=journal)
    return _copy_plan(plan, table, cancel, journal_v3 or JournalV3.create, options)


def _expected_volumes(plan: Plan) -> tuple[VolumeRef, VolumeRef]:
    """The source and destination volumes the preview proved (and ``check_plan_fresh``
    proved again): the roots the run opens must be these."""
    proof = plan.volume
    assert proof is not None and proof.mode is VolumeMode.CROSS
    assert proof.source is not None and proof.destination is not None
    return VolumeRef.of(proof.source), VolumeRef.of(proof.destination)


def _copy_plan(
    plan: Plan,
    table: ExtensionTable,
    cancel: Callable[[], bool] | None,
    journal: Callable[[], JournalV3],
    options: CopyOptions,
) -> RunRefusal | CopyRunResult:
    volumes = _expected_volumes(plan)
    try:
        log = journal()
    except JournalError as exc:
        return RunRefusal(
            plan, RunRefusalReason.JOURNAL_FAILED, str(exc), (str(exc),), None, exc.release_errors
        )
    try:
        run = _CopyRun(plan, table, log, cancel, options, volumes)
    except BaseException as exc:
        try:
            log.close()  # the run never took it over
        except BaseException as close_exc:
            exc.add_note(f"then closing the journal failed: {_describe_cleanup(close_exc)}")
            winfileops.attach_release_errors(exc, [close_exc])
            if isinstance(exc, Exception) and not isinstance(close_exc, Exception):
                winfileops.attach_release_errors(close_exc, [exc])
                raise close_exc from exc
        raise
    try:
        try:
            run.execute()
        except Exception as exc:  # not expected: every step handles its own errors
            run.abort(exc)
    except BaseException as exc:  # an interrupt: release everything, then let it through
        run.close(interrupted=exc)
        raise
    run.close()
    return run.copy_result()


def _error_code(error: BaseException | None) -> int | None:
    for name in ("winerror", "errno"):
        value = getattr(error, name, None)
        if type(value) is int and value:
            return value
    return None


def _source_refusal(exc: OSError) -> _Refusal:
    """Why ``H_SRC`` could not be opened (``winprims.open_error``: a ``StatusError``)."""
    code = _error_code(exc)
    if code in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
        return _Refusal(ItemStatus.SKIPPED, ExecReason.SOURCE_MISSING, _describe(exc), code)
    if getattr(exc, "ntstatus", None) == _STATUS_FILE_IS_A_DIRECTORY:
        detail = "a directory now has the file's name"
        return _Refusal(ItemStatus.SKIPPED, ExecReason.SOURCE_REPLACED, detail, code)
    if code == winfileops.ERROR_SHARING_VIOLATION:
        return _Refusal(ItemStatus.SKIPPED, ExecReason.SOURCE_IN_USE, _describe(exc), code)
    if code == _ERROR_ACCESS_DENIED:
        return _Refusal(ItemStatus.FAILED, ExecReason.ACCESS_DENIED, _describe(exc), code)
    return _Refusal(ItemStatus.FAILED, ExecReason.SOURCE_CHECK_FAILED, _describe(exc), code)


def _block_open(journal: JournalV3) -> bool:
    """A block (a copy's or a mkdir's) is open: its intent has no terminal record yet."""
    if journal.open_block() is not None:
        return True
    history = journal.history  # also when the journal failed in a block's last write
    if not history:
        return False
    last = history[-1]
    return (
        str(last["operation"]) in (OperationV2.COPY, OperationV2.MKDIR)
        and str(last["status"]) in _OPEN_PHASES
    )


_DIR_STATES = {
    Created.CREATED: DirState.CREATED,
    Created.REFUSED: DirState.REFUSED,
    Created.REMOVED: DirState.REMOVED,
    Created.UNCHECKED: DirState.UNCHECKED,
    Created.LEFT: DirState.LEFT,
    Created.REMOVAL_UNKNOWN: DirState.REMOVAL_UNKNOWN,
    Created.UNKNOWN: DirState.UNKNOWN,
}
_DIR_STATUS = {
    DirState.CREATED: StatusV2.DONE,
    DirState.EXISTING: StatusV2.DONE,
    DirState.REFUSED: StatusV2.FAILED,
    DirState.REMOVED: StatusV2.FAILED,
}  # every other state: recovery required
_PARENT_REFUSALS = frozenset({
    CreateReason.PARENT_UNREADABLE, CreateReason.PARENT_NOT_DIRECTORY,
    CreateReason.PARENT_REPARSE, CreateReason.PARENT_DELETE_PENDING,
    CreateReason.PARENT_VOLUME, CreateReason.PARENT_IDENTITY,
})  # fmt: skip


def _dir_creation(exc: BaseException, name: str) -> Creation | None:
    """The ``Creation`` of the directory ``name`` attached to ``exc``, if any."""
    found = [c for c in getattr(exc, "creations", ()) if c.role == "H_DIR" and c.name == name]
    return found[-1] if found else None


class _CopyRun(_Run):
    """The R3A run with ``copy`` instead of ``move`` and the v3 journal (module docstring)."""

    journal: JournalV3  # type: ignore[assignment]

    def __init__(
        self,
        plan: Plan,
        table: ExtensionTable,
        journal: JournalV3,
        cancel: Callable[[], bool] | None,
        options: CopyOptions,
        volumes: tuple[VolumeRef, VolumeRef],
    ) -> None:
        super().__init__(plan, table, journal, cancel)  # type: ignore[arg-type]
        self.options = options
        self.volumes = volumes  # expected: source's, destination's
        self.copies: dict[int, CopyItemResult] = {}
        self.acl_ops: aclcreate.Ops = options.acl_ops or aclcreate.NtOps()
        self.source_sd: Descriptor | None = None  # the source directory's, read once
        self.creator: Sid | None = None
        # The identity of every directory handle proven by ``dir_problem``, by handle.
        self.identities: dict[int, tuple[winfileops.Handle, Identity]] = {}

    # --- what differs from a move ---

    def root_conflict(self, path: Path, root: winfileops.VolumeRoot) -> str | None:
        if len(self.roots) >= len(self.volumes):
            return f"{path}: a third volume root in one run"
        expected = self.volumes[len(self.roots)]
        if root.device.upper() != expected.device.upper():
            return f"{path} is on {root.device}, the preview proved {expected.device}"
        return None

    def volume_conflict(self, source_serial: int, root_serial: int) -> str | None:
        source, destination = self.volumes
        if source_serial != source.serial:
            return f"{self.plan.source} is on volume {source_serial:#x}, not {source.serial:#x}"
        if root_serial != destination.serial:
            return f"{self.root} is on volume {root_serial:#x}, not {destination.serial:#x}"
        return None

    def dir_access(self, path: Path) -> int:
        if path_key(path) == path_key(self.plan.source):  # its descriptor is read once
            return _DIR_ACCESS | aclproof.READ_CONTROL
        if path_key(path.parent) == path_key(self.root):  # the temporary file is created here
            return _DIR_ACCESS | winfileops.FILE_ADD_FILE | aclproof.READ_CONTROL
        return _DIR_ACCESS

    def check_run(self) -> None:
        """``_Run.check_run``, then the source directory's descriptor (through its held
        handle) and the creator SID; raises what reading them raises (the run is refused)."""
        super().check_run()
        if self.stopped is not None or self.source_dir is None:
            return
        self.source_sd = Descriptor.parse(self.acl_ops.read_descriptor(self.source_dir))
        self.creator = self.options.creator()

    def dir_problem(self, handle: winfileops.Handle, *, final: bool) -> _Refusal | None:
        """``_Run.dir_problem``; a directory that passes keeps its proven identity."""
        problem = super().dir_problem(handle, final=final)
        if problem is None:
            self.identities[id(handle)] = (handle, Identity.of(winfileops.query(handle)))
        return problem

    def identity(self, handle: winfileops.Handle) -> Identity | None:
        held = self.identities.get(id(handle))
        return held[1] if held is not None and held[0] is handle else None

    def log(  # type: ignore[override]
        self, operation: Operation | OperationV2, status: Status | StatusV2, **fields: object
    ) -> int | None:
        if str(operation) == OperationV2.RUN:
            fields["source_volume"], fields["target_volume"] = self.volumes
        return super().log(operation, status, **fields)  # type: ignore[arg-type]

    # --- bookkeeping ---

    def copy_result(self) -> CopyRunResult:
        return CopyRunResult(
            self.plan,
            tuple(self.copies[i] for i in range(len(self.plan.items))),
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
        detail = _describe(exc)
        self.problems.append(f"run aborted: {detail}")
        self.stop(ExecReason.RUN_ABORTED, ItemStatus.CANCELLED, detail)
        for index, item in enumerate(self.plan.items):
            if index not in self.copies:
                self.copies[index] = CopyItemResult(
                    item, CopyStatus.CANCELLED, str(ExecReason.RUN_ABORTED), detail
                )

    def add_notes(self, interrupt: BaseException) -> None:
        copied = sum(1 for r in self.copies.values() if r.status is CopyStatus.COPIED)
        interrupt.add_note(
            f"DownloadsOrganizer: copy run {self.journal.run_id} interrupted after {copied}"
            f" verified cop(ies); every source was kept; nothing was started after the"
            f" interrupt; journal: {self.journal.path}"
        )
        for problem in self.cleanup_problems:
            interrupt.add_note(f"cleanup: {problem}")

    def finish(  # type: ignore[override]
        self,
        index: int,
        status: ItemStatus,
        reason: ExecReason | str,
        detail: str | None = None,
        error_code: int | None = None,
        intent: int | None = None,
    ) -> None:
        """A file that ``copy_item`` never got: its result and its unstarted record."""
        assert intent is None, "a copy's intent is journaled by copy_item"
        item = self.plan.items[index]
        copy_status = _COPY_STATUS[status]
        self.copies[index] = CopyItemResult(item, copy_status, str(reason), detail, error_code)
        if copy_status not in _UNSTARTED or _block_open(self.journal):
            return  # no record can say it; the result and the journal's open block do
        self.log(
            OperationV2.COPY,
            _UNSTARTED[copy_status],
            source=item.source,
            target=item.target,
            reason=str(reason),
            error_code=error_code,
            error_message=detail,
            source_volume=self.volumes[0],
            target_volume=self.volumes[1],
            commit=Commit.NOT_ATTEMPTED,
            temp_state=TempState.NONE,
        )

    # --- run ---

    def execute(self) -> None:
        plan = self.plan
        copies = len(plan.moves)
        self.log(
            Operation.RUN,
            Status.STARTED,
            source=plan.source,
            target=self.root,
            reason=f"items={len(plan.items)} copies={copies}",
        )
        if self.stopped is None and copies:
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
            try:
                self.copy(index, item)
            except Exception as exc:  # fail closed: never continue after the unexpected
                self.unexpected(index, item, exc)
        if _block_open(self.journal):
            self.problems.append("run/finished not journaled: a block has no outcome")
            return
        counts = " ".join(
            f"{s}={sum(1 for r in self.copies.values() if r.status is s)}" for s in CopyStatus
        )
        self.log(
            Operation.RUN,
            Status.FINISHED,
            reason=str(self.stopped or ExecReason.COMPLETED),
            error_message=f"{counts} directories_created={len(self.created)}"
            + (f" stop: {self.stop_detail}" if self.stop_detail else ""),
        )

    def unexpected(self, index: int, item: PlanItem, exc: Exception) -> None:
        """Fail closed on an unexpected error: the run stops with it as the cause, and the
        release failures it carries (``copy_item`` attaches them) are kept as cleanup."""
        detail = _describe(exc)
        self.problems.append(f"{item.source}: {detail}")
        failures = tuple(winfileops.release_failures(exc))
        for error in failures:
            self.cleanup_failed(f"copy to {item.target}: cannot release", error)
        if index not in self.copies:
            states: tuple[TransferState, ...] = tuple(getattr(exc, "transfer_states", ()))
            started = [s for s in states if s.intent is not None]
            if started:  # the copy began: its outcome is the journal's open block, by hand
                self.copies[index] = CopyItemResult(
                    item, CopyStatus.RECOVERY_REQUIRED, str(ExecReason.UNEXPECTED_ERROR),
                    f"{detail}; {started[-1].describe()}", _error_code(exc), started[-1],
                    failures,
                )  # fmt: skip
            else:
                self.finish(index, ItemStatus.FAILED, ExecReason.UNEXPECTED_ERROR, detail)
                self.copies[index] = replace(self.copies[index], cleanup_errors=failures)
        self.stop(ExecReason.RUN_ABORTED, ItemStatus.CANCELLED, detail)

    # --- directories (v3) ---

    def mkdir(
        self, parent: winfileops.Handle, path: Path, *, final: bool
    ) -> winfileops.Handle | _Refusal:
        """Create the missing directory ``path`` in the held ``parent`` with its security
        (``aclcreate.create_category``) in a v3 mkdir block (module docstring)."""
        if self.source_sd is None or self.creator is None:  # never read: nothing is created
            detail = f"{path}: the source directory's security was not read"
            return _Refusal(ItemStatus.FAILED, ExecReason.TARGET_DIR_FAILED, detail)
        try:
            expected = Identity.of(self.acl_ops.query(parent))
        except OSError as exc:
            detail = f"{parent.path}: {_describe(exc)}"
            code = _error_code(exc)
            return _Refusal(ItemStatus.FAILED, ExecReason.TARGET_DIR_FAILED, detail, code)
        parent_id = expected.file_id
        intent = self.log(OperationV2.MKDIR, StatusV2.INTENT, target=path, parent_id=parent_id)
        if intent is None:
            return _Refusal(ItemStatus.CANCELLED, ExecReason.JOURNAL_FAILED, self.stop_detail)

        def outcome(
            state: DirState, reason: str, dir_id: int | None, error: BaseException | None
        ) -> None:
            self.log(
                OperationV2.MKDIR, _DIR_STATUS.get(state, StatusV2.RECOVERY_REQUIRED),
                intent=intent, target=path, reason=reason, error_code=_error_code(error),
                error_message=None if error is None else _describe(error),
                parent_id=parent_id, dir_state=state, dir_id=dir_id,
            )  # fmt: skip

        try:
            creation = aclcreate.create_category(
                self.acl_ops, parent, expected, path.name, self.source_sd, self.creator
            )
        except Exception as exc:  # the call was cut off: say what it established, then stop
            found = _dir_creation(exc, path.name)
            state = DirState.UNKNOWN if found is None else _DIR_STATES[found.state]
            if state is DirState.CREATED:  # never vouched for once the call was cut off
                state = DirState.UNCHECKED
            dir_id = None if found is None or found.identity is None else found.identity.file_id
            if state in (DirState.REFUSED, DirState.UNKNOWN):
                dir_id = None
            if state not in (DirState.REFUSED, DirState.REMOVED):
                self.created.append(path)  # it may be there
            outcome(state, str(CreateReason.EXCEPTION), dir_id, exc)
            self.problems.append(f"{path}: creating it raised: {_describe(exc)}")
            self.stop(ExecReason.RUN_ABORTED, ItemStatus.CANCELLED, _describe(exc))
            raise
        for error in creation.release_errors:
            self.cleanup_failed(f"creating {path}: cannot release", error)
            self.stop(ExecReason.CLEANUP_FAILED, ItemStatus.CANCELLED, self.cleanup_problems[-1])
        return self.created_dir(parent, path, final, creation, outcome)

    def created_dir(
        self,
        parent: winfileops.Handle,
        path: Path,
        final: bool,
        creation: Creation,
        outcome: Callable[[DirState, str, int | None, BaseException | None], None],
    ) -> winfileops.Handle | _Refusal:
        """Journal what ``create_category`` established and hold the directory it proved."""
        state, refusal = creation.state, creation.refusal
        reason = str(refusal.reason) if refusal is not None else str(state)
        dir_id = None if creation.identity is None else creation.identity.file_id
        if state is Created.CREATED:
            handle = creation.handle
            assert handle is not None and dir_id is not None
            self.own(handle)
            self.created.append(path)
            try:
                try:
                    problem = self.dir_problem(handle, final=final)
                except OSError as exc:
                    problem = _Refusal(ItemStatus.FAILED, ExecReason.VERIFY_FAILED, _describe(exc))
                if problem is None and self.identity(handle) != creation.identity:
                    detail = f"{path}: not the directory just created"
                    problem = _Refusal(ItemStatus.FAILED, ExecReason.VERIFY_FAILED, detail)
                if problem is not None:  # created, not vouched for: left, the run stops
                    self.release(handle)
                    outcome(DirState.LEFT, str(problem.reason), dir_id, None)
                    self.problems.append(f"{path}: created but not verified: {problem.detail}")
                    self.stop(ExecReason.RUN_ABORTED, ItemStatus.CANCELLED, problem.detail)
                    return _Refusal(ItemStatus.CANCELLED, ExecReason.RUN_ABORTED, problem.detail)
                outcome(DirState.CREATED, "created", dir_id, None)
            except BaseException as exc:
                if id(handle) in self.owned:  # never released twice; the intent unanswered
                    self.release(handle, after=exc)
                raise
            if self.stopped is not None:  # the outcome was not journaled, or a release failed
                self.release(handle)
                return _Refusal(ItemStatus.CANCELLED, self.stopped, self.stop_detail)
            return handle
        taken = refusal is not None and refusal.reason is CreateReason.NAME_TAKEN
        if state is Created.REFUSED and taken:
            return self.existing_dir(parent, path, final, outcome)
        if state in (Created.REFUSED, Created.REMOVED):  # proven: nothing of it is left
            kept_id = None if state is Created.REFUSED else dir_id
            outcome(_DIR_STATES[state], reason, kept_id, creation.error)
            detail = f"cannot create {path}: {reason}: {getattr(refusal, 'detail', None)}"
            if refusal is not None and refusal.reason in _PARENT_REFUSALS:
                self.problems.append(detail)
                self.stop(ExecReason.RUN_ABORTED, ItemStatus.CANCELLED, detail)
            code = _error_code(creation.error)
            return _Refusal(ItemStatus.FAILED, ExecReason.TARGET_DIR_FAILED, detail, code)
        # UNCHECKED, LEFT, REMOVAL_UNKNOWN, UNKNOWN: it is or may be there; never removed here
        dir_state = _DIR_STATES[state]
        if dir_state is DirState.UNKNOWN:
            dir_id = None
        self.created.append(path)
        outcome(dir_state, reason, dir_id, creation.error)
        detail = f"{path}: {creation.describe()}; check by hand"
        self.problems.append(detail)
        self.stop(ExecReason.RUN_ABORTED, ItemStatus.CANCELLED, detail)
        return _Refusal(ItemStatus.CANCELLED, ExecReason.RUN_ABORTED, detail)

    def existing_dir(
        self,
        parent: winfileops.Handle,
        path: Path,
        final: bool,
        outcome: Callable[[DirState, str, int | None, BaseException | None], None],
    ) -> winfileops.Handle | _Refusal:
        """The name was taken since the walk: open and check it like any existing one."""
        try:
            handle = self.own(
                winfileops.open_relative(
                    parent, path.name, directory=True, access=self.dir_access(path),
                    share=_HELD_SHARE,
                )[0]
            )  # fmt: skip
        except OSError as exc:
            outcome(DirState.REFUSED, str(ExecReason.TARGET_DIR_INVALID), None, exc)
            detail = f"{path} appeared and is not a usable directory: {_describe(exc)}"
            code = _error_code(exc)
            return _Refusal(ItemStatus.SKIPPED, ExecReason.TARGET_DIR_INVALID, detail, code)
        try:
            cause: OSError | None = None
            try:
                problem = self.dir_problem(handle, final=final)
            except OSError as exc:
                cause = exc
                problem = _Refusal(
                    ItemStatus.FAILED, ExecReason.TARGET_DIR_FAILED, _describe(exc),
                    _error_code(exc),
                )  # fmt: skip
            identity = self.identity(handle)
            if problem is None and identity is None:
                detail = f"{path}: its identity is not proven"
                problem = _Refusal(ItemStatus.FAILED, ExecReason.TARGET_DIR_FAILED, detail)
            if problem is not None:
                self.release(handle, after=cause)
                outcome(DirState.REFUSED, str(problem.reason), None, cause)
                return problem
            assert identity is not None
            outcome(DirState.EXISTING, "existing", identity.file_id, None)
        except BaseException as exc:
            if id(handle) in self.owned:
                self.release(handle, after=exc)
            raise
        if self.stopped is not None:
            self.release(handle)
            return _Refusal(ItemStatus.CANCELLED, self.stopped, self.stop_detail)
        return handle

    # --- files ---

    def copy(self, index: int, item: PlanItem) -> None:
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
        acl = self.options.acl.check_item(item.source, item.target)
        if acl is not None:  # before the target directory and the temporary file exist
            return self.finish(index, ItemStatus.SKIPPED, ACL_REFUSED, acl)
        category_dir = self.category_handle(self.table.category(item.category_key))
        if isinstance(category_dir, _Refusal):
            return self.refuse(index, category_dir)
        if self.stopped is not None:  # e.g. the journal failed while creating directories
            return self.finish(index, self.stop_status, self.stopped, self.stop_detail)
        if item.target.parent != category_dir.path:
            detail = f"{category_dir.path} is not the directory of {item.target}"
            return self.finish(index, ItemStatus.FAILED, ExecReason.TARGET_DIR_INVALID, detail)
        src = self.own(winfileops.Handle.new(item.source, "H_SRC"))
        try:
            opened = winprims.open_into(src, self.source_dir, item.source.name)
        except BaseException as exc:
            self.release(src, after=exc)
            raise
        if opened is OpenOutcome.NOT_OPENED:
            error = winprims.open_error(src)
            self.release(src)
            return self.refuse(index, _source_refusal(error))
        if (
            opened is not OpenOutcome.OPENED_OWNED
            or src.unreliable
            or src.state is not HandleState.OPEN
        ):
            detail = f"opening {item.source} for reading: {opened}, unreliable {src.unreliable}"
            self.release(src)
            self.problems.append(detail)
            self.finish(index, ItemStatus.FAILED, ExecReason.SOURCE_CHECK_FAILED, detail)
            return self.stop(ExecReason.RUN_ABORTED, ItemStatus.CANCELLED, detail)
        try:
            info = winfileops.query(src)
        except OSError as exc:
            self.release(src, after=exc)
            detail, code = _describe(exc), _error_code(exc)
            refusal = _Refusal(ItemStatus.FAILED, ExecReason.SOURCE_CHECK_FAILED, detail, code)
            return self.refuse(index, refusal)
        except BaseException as exc:
            self.release(src, after=exc)
            raise
        mismatch = _source_mismatch(info, snapshot)
        if mismatch is not None:
            self.release(src)
            return self.refuse(index, mismatch)
        try:
            lost = self.source_loss(src, info)
        except OSError as exc:
            self.release(src, after=exc)
            detail, code = _describe(exc), _error_code(exc)
            refusal = _Refusal(ItemStatus.FAILED, ExecReason.SOURCE_CHECK_FAILED, detail, code)
            return self.refuse(index, refusal)
        except BaseException as exc:
            self.release(src, after=exc)
            raise
        if lost is not None:  # before the temporary file exists
            self.release(src)
            return self.finish(index, ItemStatus.SKIPPED, *lost)
        if self.stopped is not None:
            self.release(src)
            return self.finish(index, self.stop_status, self.stopped, self.stop_detail)
        expected = self.identity(category_dir)
        if self.source_sd is None or self.creator is None or expected is None:
            self.release(src)
            detail = "the security of the source directory or the category is not proven"
            return self.finish(index, ItemStatus.FAILED, ExecReason.TARGET_DIR_INVALID, detail)
        try:
            file_sd = Descriptor.parse(self.acl_ops.read_descriptor(src))
        except (OSError, MalformedDescriptor) as exc:
            self.release(src, after=exc)
            detail, code = _describe(exc), _error_code(exc)
            refusal = _Refusal(ItemStatus.FAILED, ExecReason.SOURCE_CHECK_FAILED, detail, code)
            return self.refuse(index, refusal)
        except BaseException as exc:
            self.release(src, after=exc)
            raise
        acl = AclCopy(self.acl_ops, file_sd, self.source_sd, self.creator, expected)
        self.owned.pop(id(src), None)  # copy_item closes it (C17)
        source, destination = self.volumes
        try:
            result = copy_item(
                self.journal,
                source=item.source,
                target=item.target,
                src=src,
                directory=category_dir,
                facts=SourceFacts.of(info),
                source_volume=source,
                target_volume=destination,
                ops=self.options.ops,
                cancelled=self.cancel_requested,
                block_size=self.options.block_size,
                allowed_attributes=self.options.allowed_attributes,
                acl=acl,
            )
        except BaseException as exc:
            if src.state is HandleState.OPEN:  # never taken over: still the run's
                self.release(src, after=exc)
            raise
        return self.copied(index, item, result)

    def source_loss(
        self, src: winfileops.Handle, info: winfileops.HandleInfo
    ) -> tuple[str, str] | None:
        """What of the source a copy of its unnamed stream would lose: an attribute outside
        the allowlist (READONLY, ENCRYPTED, SPARSE, ...: D5), any other data stream (the
        Zone.Identifier mark of the web too) or extended attributes (D2). Until D2 and D5
        are decided such a file is refused, never copied without them. Raises what the
        queries raise."""
        allowed = self.options.allowed_attributes
        if info.attributes & ~allowed:
            extra = info.attributes & ~allowed
            return SOURCE_ATTRIBUTES, f"attributes {info.attributes:#x}: {extra:#x} not allowed"
        ops = self.options.ops or WinOps()
        streams = ops.query_streams(src)
        if streams != (winprims.Stream("::$DATA", info.size),):
            names = ", ".join(f"{s.name} ({s.size} bytes)" for s in streams)
            return SOURCE_STREAMS, f"data streams {names}: only the unnamed one would be copied"
        ea_size = ops.query_ea_size(src)
        if ea_size:
            return SOURCE_EA, f"{ea_size} bytes of extended attributes would not be copied"
        return None

    def copied(self, index: int, item: PlanItem, result: CopyResult) -> None:
        """Keep what ``copy_item`` established; stop if anything of it is uncertain."""
        transfer = result.transfer
        for error in result.cleanup_errors:
            self.cleanup_failed(f"copy to {item.target}: cannot release", error)
        detail = None if result.error is None else _describe(result.error)
        reason = str(result.reason or ExecReason.COMPLETED)
        self.copies[index] = CopyItemResult(
            item, result.status, reason, detail, _error_code(result.error), transfer,
            result.cleanup_errors,
        )  # fmt: skip
        if result.journal_error is not None:
            self.problems.append(str(result.journal_error))
        uncertain = (
            result.status is CopyStatus.RECOVERY_REQUIRED
            or result.terminal is None
            or transfer.commit is Commit.UNKNOWN
            or transfer.temp_state in _UNCERTAIN_TEMP
        )
        if uncertain:
            self.problems.append(
                f"copy of {item.source} to {item.target}: {result.status} ({reason}),"
                f" check by hand: {transfer.describe()}"
            )
        if uncertain or result.stop is not None or result.cleanup_errors:
            stop = _STOP_REASON.get(result.stop, ExecReason.RUN_ABORTED)  # type: ignore[arg-type]
            if result.cleanup_errors and result.stop is None:
                stop = ExecReason.CLEANUP_FAILED
            self.stop(stop, ItemStatus.CANCELLED, f"after the copy of {item.source}: {reason}")
