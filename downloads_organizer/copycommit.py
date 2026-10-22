"""The cross-volume COPY of one file as a state machine (FIX-03 §3, R3B-4).

On its own: neither the executor nor the GUI uses it, and nothing here moves or deletes
a source file. One ``copy_item`` call copies the open ``H_SRC`` into a new temporary file
of the target directory (C6), writes and verifies it (C8..C12), journals each phase
(``JournalV2``), and commits it with at most one rename (C14, R-1..R-11), whose effect is
then established from the handle and the directory (C15, §3.3), never from its status
alone. A temporary file is removed only through its own handle and only while it is
proven to be ours and not committed (CL, §3.6).

Interrupts (``KeyboardInterrupt``, ``SystemExit``) and unexpected exceptions are not
journaled: the read-only resolve and CL still run where allowed, the handles are closed
once, and the very object is raised again, carrying the ``TransferState``
(``transfer_states``) and the release failures (``release_errors``).

R3B integration, with ``acl`` (``AclCopy``): the temporary file is created by
``aclcreate.create_temp`` (relative to the held category, the source's protected DACL
given to ``NtCreateFile`` itself) and the record after its intent is written by
``aclcreate.journal_temp``. C12 and C15 also read its descriptor through the handle
(``aclcreate.verify``: exactly as created, no principal wider than at the source) and
prove the category again from its handle (``aclcreate.prove_parent``). A failed check
before the commit is CL and ``verify_failed``; after the commit it is
``metadata_violation`` (recovery required, the run stops). A create that left an object
or an unknown outcome stops the run, and so does a refusal of the category itself;
nothing is removed but what the same call created and proved its own. Without ``acl``
the temporary file is created as before (``create_into``, the directory's DACL).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from downloads_organizer import aclcreate, aclproof, winfileops, winprims
from downloads_organizer.aclcreate import Created, CreateReason, Creation, Identity
from downloads_organizer.aclproof import Descriptor, Sid
from downloads_organizer.journal import JournalError
from downloads_organizer.journal_v2 import (
    TARGET_EXISTS,
    TEMP_CREATE_UNCERTAIN,
    TEMP_NAME_TAKEN,
    Commit,
    Diagnostics,
    JournalV2,
    OperationV2,
    StatusV2,
    TempState,
    VolumeRef,
    temp_name,
)
from downloads_organizer.winfileops import (
    Handle,
    HandleInfo,
    HandleState,
    OpenOutcome,
    _require,
    attach_release_errors,
    describe_error,
    prevailing,
)
from downloads_organizer.winprims import (
    Disposal,
    Disposed,
    Lookup,
    LookupKind,
    RenameAttempt,
    Renamed,
    Stream,
    last_component,
)

FILE_ATTRIBUTE_ARCHIVE = 0x20
FILE_ATTRIBUTE_NORMAL = 0x80
# The attributes a temporary file may have (OWNER_DECISION D5; the default proposed).
DEFAULT_ALLOWED_ATTRIBUTES = FILE_ATTRIBUTE_ARCHIVE | FILE_ATTRIBUTE_NORMAL
STATUS_NOT_SAME_DEVICE = 0xC000_00D4  # ERROR_NOT_SAME_DEVICE (17)
MAIN_STREAM = "::$DATA"


class Reason(StrEnum):
    """The ``reason`` of a terminal record or result."""

    TEMP_NAME_TAKEN = TEMP_NAME_TAKEN
    TARGET_EXISTS = TARGET_EXISTS
    TEMP_CREATE_UNCERTAIN = TEMP_CREATE_UNCERTAIN
    TEMP_CREATE_FAILED = "temp_create_failed"
    TEMP_QUERY_FAILED = "temp_query_failed"
    TEMP_TAMPERED = "temp_tampered"
    READ_FAILED = "read_failed"
    WRITE_FAILED = "write_failed"
    SOURCE_CHANGED = "source_changed"
    METADATA_FAILED = "metadata_failed"
    FLUSH_FAILED = "flush_failed"
    VERIFY_FAILED = "verify_failed"
    CANCELLED = "cancelled"
    COMMIT_FAILED = "commit_failed"
    CROSS_VOLUME = "cross_volume"
    JOURNAL_FAILED = "journal_failed"
    # recovery_required (PROPOSED wording)
    METADATA_VIOLATION = "metadata_violation"
    RENAME_OK_NOT_COMMITTED = "rename_ok_not_committed"
    RENAME_NOT_ESTABLISHED = "rename_not_established"
    COMMITTED_DESPITE_RENAME_ERROR = "committed_despite_rename_error"
    COMMIT_UNKNOWN = "commit_unknown"
    DELETE_PENDING = "delete_pending"


class Probe(StrEnum):
    """P of §3.6 (CL-2): is the temporary file delete pending."""

    TRUE = "true"
    FALSE = "false"
    ERR = "err"
    SKIP = "skip"


class Closed(StrEnum):
    """C of §3.6 (CL-3)."""

    CLOSED = "closed"
    FAILED = "failed"


class Seen(StrEnum):
    """L of §3.6 (CL-4): the temporary name in the directory after the close."""

    ABSENT = "absent"
    OURS = "ours"
    OTHER = "other"
    UNK = "unk"
    SKIP = "skip"


class Verdict(StrEnum):
    """The effect of the commit as established by C15 (§3.3)."""

    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    UNKNOWN = "unknown"


class CopyStatus(StrEnum):
    COPIED = "copied"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECOVERY_REQUIRED = "recovery_required"


class Stop(StrEnum):
    """Why the run must not go on after this copy (the first reason is kept)."""

    JOURNAL_FAILED = "journal_failed"
    RUN_ABORTED = "run_aborted"
    CLEANUP_FAILED = "cleanup_failed"


class Phase(StrEnum):
    PREPARED = "prepared"
    CREATE_ATTEMPTED = "create_attempted"
    CREATE_UNCERTAIN = "create_uncertain"
    NAME_TAKEN = "name_taken"
    TEMP_CREATED_ID_UNKNOWN = "temp_created_id_unknown"
    STAGED = "staged"
    WRITTEN = "written"
    VERIFIED = "verified"
    COMMIT_ATTEMPTED = "commit_attempted"
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    UNKNOWN = "unknown"


# --- the operations it uses (the real ones, or a fake in the tests) ------------------


class Ops(Protocol):
    def new(self, path: Path, role: str) -> Handle: ...
    def create_into(self, handle: Handle, parent: Handle, name: str) -> OpenOutcome: ...
    def open_error(self, handle: Handle) -> OSError: ...
    def query(self, handle: Handle) -> HandleInfo: ...
    def read_at(self, handle: Handle, offset: int, size: int) -> bytes: ...
    def write_at(self, handle: Handle, offset: int, data: bytes) -> None: ...
    def set_mtime(self, handle: Handle, mtime_ns: int) -> None: ...
    def flush(self, handle: Handle) -> None: ...
    def query_streams(self, handle: Handle) -> tuple[Stream, ...]: ...
    def query_ea_size(self, handle: Handle) -> int: ...
    def query_name(self, handle: Handle) -> str: ...
    def lookup_entry(self, handle: Handle, directory: Handle, name: str) -> Lookup: ...
    def rename_once(
        self, handle: Handle, directory: Handle, name: str, attempt: RenameAttempt
    ) -> Renamed: ...
    def dispose(self, handle: Handle, disposal: Disposal) -> Disposed: ...
    def close(self, handle: Handle) -> None: ...


class WinOps:
    """The R3B-3 primitives (``query`` is the kernel32 one of R3A)."""

    def new(self, path: Path, role: str) -> Handle:
        return Handle.new(path, role)

    def create_into(self, handle: Handle, parent: Handle, name: str) -> OpenOutcome:
        return winprims.create_into(handle, parent, name)

    def open_error(self, handle: Handle) -> OSError:
        return winprims.open_error(handle)

    def query(self, handle: Handle) -> HandleInfo:
        return winfileops.query(handle)

    def read_at(self, handle: Handle, offset: int, size: int) -> bytes:
        return winprims.read_at(handle, offset, size)

    def write_at(self, handle: Handle, offset: int, data: bytes) -> None:
        winprims.write_at(handle, offset, data)

    def set_mtime(self, handle: Handle, mtime_ns: int) -> None:
        winprims.set_mtime(handle, mtime_ns)

    def flush(self, handle: Handle) -> None:
        winprims.flush(handle)

    def query_streams(self, handle: Handle) -> tuple[Stream, ...]:
        return winprims.query_streams(handle)

    def query_ea_size(self, handle: Handle) -> int:
        return winprims.query_ea_size(handle)

    def query_name(self, handle: Handle) -> str:
        return winprims.query_name(handle)

    def lookup_entry(self, handle: Handle, directory: Handle, name: str) -> Lookup:
        return winprims.lookup_entry(handle, directory, name)

    def rename_once(
        self, handle: Handle, directory: Handle, name: str, attempt: RenameAttempt
    ) -> Renamed:
        return winprims.rename_once(handle, directory, name, attempt)

    def dispose(self, handle: Handle, disposal: Disposal) -> Disposed:
        return winprims.dispose(handle, disposal)

    def close(self, handle: Handle) -> None:
        handle.close()


# --- what is known -------------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False)
class AclCopy:
    """How the temporary file gets its security (R3B): ``create_temp`` in the category
    held as the copy's ``directory`` (proven to be ``directory`` here), with the protected
    DACL derived from ``file_sd`` (the source's descriptor, read through ``H_SRC``), only
    if the category gives no principal more than ``source_dir`` (the source directory's).
    At C12 and again right before C14 the source's descriptor is read anew through
    ``H_SRC`` and must still be ``file_sd`` (``_source_now``)."""

    ops: aclcreate.Ops
    file_sd: Descriptor
    source_dir: Descriptor
    creator: Sid
    directory: Identity


# A refusal of the held category itself: the run does not go on with it.
_PARENT_REFUSALS = frozenset(
    {
        CreateReason.PARENT_UNREADABLE,
        CreateReason.PARENT_NOT_DIRECTORY,
        CreateReason.PARENT_REPARSE,
        CreateReason.PARENT_DELETE_PENDING,
        CreateReason.PARENT_VOLUME,
        CreateReason.PARENT_IDENTITY,
    }
)


@dataclass(frozen=True, slots=True)
class SourceFacts:
    """The source as planned, read from ``H_SRC``; C12 requires it unchanged."""

    file_id: int
    size: int
    mtime_ns: int
    attributes: int
    volume_serial: int

    @classmethod
    def of(cls, info: HandleInfo) -> SourceFacts:
        return cls(info.file_id, info.size, info.mtime_ns, info.attributes, info.volume_serial)


def _lookup_text(found: Lookup | None) -> str | None:
    if found is None:
        return None
    text = str(found.kind)
    if found.file_id is not None:
        text += f" {found.file_id:032x}"
    return f"{text} ({found.reason})" if found.reason else text


def _info_text(info: HandleInfo | None) -> str | None:
    if info is None:
        return None
    return (
        f"id {info.file_id:032x} serial {info.volume_serial:#x} attributes {info.attributes:#x}"
        f" size {info.size} links {info.links} delete_pending {info.delete_pending}"
    )


@dataclass(eq=False)
class Cleanup:
    """CL of §3.6: what each step observed (D, P, C, L, K, X) and its outcome."""

    disposal: Disposal = field(default_factory=Disposal)
    disposed: Disposed = Disposed.NONE  # D
    pending: Probe = Probe.SKIP  # P
    closed: Closed | None = None  # C
    seen: Seen = Seen.SKIP  # L
    lookup: Lookup | None = None
    known: bool = False  # K
    interrupted: bool = False  # X
    errors: list[BaseException] = field(default_factory=list)
    outcome: TempState | None = None
    hints: tuple[str, ...] = ()
    close_may_complete_delete: bool = False

    def describe(self) -> str:
        posix, classic = self.disposal.statuses
        statuses = ", ".join("none" if s is None else f"{s:#010x}" for s in (posix, classic))
        text = (
            f"D={self.disposed} P={self.pending} C={self.closed} L={self.seen}"
            f" K={'yes' if self.known else 'no'} X={'yes' if self.interrupted else 'no'}"
            f" statuses=({statuses}) -> {self.outcome}"
        )
        if self.hints:
            text += f" hints={','.join(self.hints)}"
        if self.errors:
            text += " errors=" + " | ".join(describe_error(e) for e in self.errors)
        return text


def cl_outcome(
    d: Disposed, p: Probe, c: Closed, seen: Seen, known: bool, interrupted: bool
) -> tuple[TempState, tuple[str, ...], bool]:
    """CL-R1..CL-R3 of §3.6: the temporary file's state, the hints of an ``unknown`` one,
    and ``close_may_complete_delete``."""
    if (
        not interrupted
        and d is Disposed.OK
        and p is Probe.TRUE
        and c is Closed.CLOSED
        and seen is Seen.ABSENT
    ):
        return TempState.REMOVED, (), False  # CL-R1
    if (
        not interrupted
        and d is Disposed.REFUSED
        and p is Probe.FALSE
        and c is Closed.CLOSED
        and seen is Seen.OURS
        and known
    ):
        return TempState.LEFT, (), False  # CL-R2
    may = c is Closed.FAILED and (d is Disposed.OK or p is Probe.TRUE)
    hints = []  # CL-R3: whatever the hints say, the state is unknown
    if seen is Seen.OURS and p is not Probe.TRUE:
        hints.append("probably_left")
    if seen is Seen.ABSENT and p is Probe.TRUE and d in (Disposed.OK, Disposed.UNSURE):
        hints.append("probably_removed")
    if seen is Seen.OTHER:
        hints.append("name_other_object")
    if may:
        hints.append("close_may_complete_delete")
    return TempState.UNKNOWN, tuple(hints), may


@dataclass(eq=False)
class Evidence:
    """C15 (§3.3): P1 (the handle), P2 (its name), P3 (both names in the directory)."""

    p1: HandleInfo | None = None
    p1_error: str | None = None
    meta: str | None = None  # the P1-META violation, None if it holds
    name: str | None = None
    name_error: str | None = None
    streams_error: str | None = None  # why the stream query of P1-META failed
    ea_error: str | None = None  # why the EA query of P1-META failed
    final: Lookup | None = None
    temp: Lookup | None = None
    verdict: Verdict = Verdict.UNKNOWN
    interrupted: bool = False


def judge_commit(
    p1: HandleInfo | None,
    *,
    serial: int,
    temp_id: int | None,
    name: str | None,
    final_name: str,
    temp_name: str,
    final: Lookup | None,
    temp: Lookup | None,
) -> Verdict:
    """§3.3: COMMITTED or NOT_COMMITTED only when P1-ID, P2 and P3 all agree; one FOUND
    is never enough, and any contradiction is UNKNOWN."""
    if p1 is None or temp_id is None or name is None or final is None or temp is None:
        return Verdict.UNKNOWN
    p1_id = (
        p1.file_id == temp_id
        and p1.volume_serial == serial
        and not p1.delete_pending
        and p1.links == 1
        and not p1.directory
    )
    if not p1_id:
        return Verdict.UNKNOWN
    here = last_component(name)

    def found(lookup: Lookup) -> bool:
        return lookup.kind is LookupKind.FOUND and lookup.file_id == temp_id

    def other(lookup: Lookup) -> bool:
        return (
            lookup.kind is LookupKind.FOUND
            and lookup.file_id is not None
            and lookup.file_id != temp_id
        )

    if here == final_name and found(final) and temp.kind is LookupKind.ABSENT:
        return Verdict.COMMITTED
    if here == temp_name and found(temp) and (final.kind is LookupKind.ABSENT or other(final)):
        return Verdict.NOT_COMMITTED
    return Verdict.UNKNOWN


def meta_violation(
    p1: HandleInfo,
    streams: tuple[Stream, ...] | None,
    ea_size: int | None,
    *,
    size: int,
    mtime_ns: int,
    attributes: int | None,
    allowed: int,
    streams_error: str | None = None,
    ea_error: str | None = None,
) -> str | None:
    """P1-META of §3.3 against what C12 verified; None if it holds. It does not stand in
    for the content check (SHA-256, C12), and the change time is not used. A failed
    query (None) is a violation; its reason, if given, is kept in the text."""
    problems = []
    if attributes is None or p1.attributes & ~allowed or p1.attributes != attributes:
        verified = "none" if attributes is None else f"{attributes:#x}"
        problems.append(f"attributes {p1.attributes:#x} (verified {verified})")
    if p1.mtime_ns != mtime_ns:
        problems.append(f"mtime {p1.mtime_ns} (verified {mtime_ns})")
    if p1.size != size:
        problems.append(f"size {p1.size} (verified {size})")
    if streams is None:
        problems.append("stream unknown")
        if streams_error is not None:
            problems[-1] += f" ({streams_error})"
    elif streams != (Stream(MAIN_STREAM, size),):
        problems.append(f"stream {', '.join(f'{s.name}={s.size}' for s in streams)}")
    if ea_size is None:
        problems.append("ea unknown")
        if ea_error is not None:
            problems[-1] += f" ({ea_error})"
    elif ea_size != 0:
        problems.append(f"ea {ea_size}")
    return "; ".join(problems) or None


@dataclass(eq=False)
class TransferState:
    """Where one copy is; kept on the result and on an interrupt (``transfer_states``)."""

    source: Path
    target: Path
    temp: str | None = None
    phase: Phase = Phase.PREPARED
    intent: int | None = None
    create_outcome: OpenOutcome | None = None
    create_lookup: Lookup | None = None
    temp_id: int | None = None  # as staged (C7a), None if not established
    temp_id_observed: int | None = None
    sha256: str | None = None  # as journaled (WRITTEN)
    temp_state: TempState = TempState.NONE
    commit: Commit = Commit.NOT_ATTEMPTED
    permit: bool = False  # R-2: COMMIT_ATTEMPTED returned its sequence
    rename_spent: bool = False  # R-3: set right before the one rename
    rename: Renamed | None = None
    rename_status: int | None = None
    evidence: Evidence | None = None
    cleanup: Cleanup | None = None
    handle_state: HandleState | None = None
    terminal: StatusV2 | None = None

    def describe(self) -> str:
        verdict = None if self.evidence is None else self.evidence.verdict
        cleanup = None if self.cleanup is None else self.cleanup.describe()
        return (
            f"phase {self.phase}, temp_state {self.temp_state}, commit {self.commit},"
            f" permit {self.permit}, rename_spent {self.rename_spent}, rename {self.rename},"
            f" verdict {verdict}, handle {self.handle_state}, cleanup {cleanup}"
        )


def attach_transfer_state(exc: BaseException, state: TransferState) -> BaseException:
    """Add ``state`` to ``exc.transfer_states`` (each object once) and a note."""
    current = tuple(getattr(exc, "transfer_states", ()))
    if all(s is not state for s in current):
        exc.transfer_states = (*current, state)  # type: ignore[attr-defined]
        exc.add_note(f"copy {state.target}: {state.describe()}")
    return exc


@dataclass(frozen=True, slots=True, eq=False)
class CopyResult:
    status: CopyStatus
    reason: str | None
    stop: Stop | None  # set: the run must stop after this copy
    transfer: TransferState
    cleanup_errors: tuple[BaseException, ...]  # release failures (the objects)
    journal_error: JournalError | None
    terminal: StatusV2 | None  # the terminal record written, None if none was
    error: BaseException | None = None


_ENDED = {
    StatusV2.DONE: CopyStatus.COPIED,
    StatusV2.SKIPPED: CopyStatus.SKIPPED,
    StatusV2.FAILED: CopyStatus.FAILED,
    StatusV2.CANCELLED: CopyStatus.CANCELLED,
    StatusV2.RECOVERY_REQUIRED: CopyStatus.RECOVERY_REQUIRED,
}


def _error_code(error: BaseException | None) -> int | None:
    for name in ("winerror", "errno"):
        value = getattr(error, name, None)
        if type(value) is int and value:
            return value
    return None


# --- the machine ---------------------------------------------------------------------


class _Copy:
    def __init__(
        self,
        journal: JournalV2,
        *,
        source: Path,
        target: Path,
        src: Handle,
        directory: Handle,
        facts: SourceFacts,
        source_volume: VolumeRef,
        target_volume: VolumeRef,
        ops: Ops,
        cancelled: Callable[[], bool],
        block_size: int,
        allowed: int,
        acl: AclCopy | None = None,
    ) -> None:
        self.journal = journal
        self.source, self.target = source, target
        self.src, self.directory = src, directory
        self.facts = facts
        self.source_volume, self.target_volume = source_volume, target_volume
        self.ops = ops
        self.cancelled = cancelled
        self.block_size = block_size
        self.allowed = allowed
        self.ts = TransferState(source, target)
        self.tmp: Handle | None = None
        self.tmp_closed = self.src_closed = False
        self.attempt = RenameAttempt()
        self.staged_id: int | None = None
        self.sha: str | None = None
        self.c12_attributes: int | None = None
        self.interrupt: BaseException | None = None
        self.cleanup_errors: list[BaseException] = []
        self.stop: Stop | None = None
        self.journal_error: JournalError | None = None
        self.acl = acl
        self.creation: Creation | None = None  # of ``create_temp`` (with ``acl``)

    # -- the run and its unwinding --

    def run(self) -> CopyResult:
        try:
            result = self._run()
            if self.interrupt is None:
                return result
            raise self.interrupt
        except BaseException as exc:  # an interrupt, or a bug: nothing more is journaled
            self._note_interrupt(exc)
            self._unwind()
            assert self.interrupt is not None
            raise self.interrupt  # noqa: B904 - the very object, with its own context

    def _note_interrupt(self, exc: BaseException) -> None:
        if self.interrupt is None:
            self.interrupt = exc
        elif exc is not self.interrupt:
            self.interrupt = prevailing(self.interrupt, exc)

    def _check_interrupt(self) -> None:
        if self.interrupt is not None:
            raise self.interrupt

    def _unwind(self) -> None:
        """After an interrupt: R-11 resolve, CL only under O (after the permit only on a
        proven NC), each handle closed once; then the state goes onto the interrupt."""
        ts = self.ts
        try:
            if ts.permit:
                if ts.evidence is None and self._tmp_open():
                    ts.evidence = self._resolve()
                proven = ts.evidence is not None and ts.evidence.verdict is Verdict.NOT_COMMITTED
                if proven and ts.cleanup is None and self._owned_open():
                    self._cleanup()
            elif ts.cleanup is None and self._owned_open():
                self._cleanup()
        except BaseException as exc:
            self._note_interrupt(exc)
        for close in (self._close_tmp, self._close_src):
            try:
                close()
            except BaseException as exc:
                self._note_interrupt(exc)
        self._sync()
        assert self.interrupt is not None
        attach_transfer_state(self.interrupt, ts)
        if self.cleanup_errors:
            attach_release_errors(self.interrupt, self.cleanup_errors)
            self.interrupt.add_note(
                "release failures: " + " | ".join(describe_error(e) for e in self.cleanup_errors)
            )

    # -- C5..C16 --

    def _run(self) -> CopyResult:
        ts = self.ts
        ts.temp = temp_name(self.journal.run_id, self.journal.next_sequence)
        # C5
        intent = self._write(
            StatusV2.INTENT, commit=Commit.NOT_ATTEMPTED, temp_state=TempState.NONE
        )
        if intent is None:
            return self._result(CopyStatus.CANCELLED, Reason.JOURNAL_FAILED, self.journal_error)
        ts.intent = intent
        # C6, C7a, C7b
        ts.phase = Phase.CREATE_ATTEMPTED
        ended = self._create_acl() if self.acl is not None else self._create_plain()
        if ended is not None:
            return ended
        self.staged_id = ts.temp_id
        ts.phase = Phase.STAGED
        # C8..C10
        ended = self._write_content()
        if ended is not None:
            return ended
        # C11
        written = self._write(
            StatusV2.WRITTEN,
            temp_id=self.staged_id,
            sha256=self.sha,
            commit=Commit.NOT_ATTEMPTED,
            temp_state=TempState.PRESENT,
        )
        if written is None:
            return self._clean_after_journal_error()
        ts.sha256 = self.sha
        ts.phase = Phase.WRITTEN
        # C12
        ended = self._verify()
        if ended is not None:
            return ended
        # C13
        verified = self._write(
            StatusV2.VERIFIED,
            temp_id=self.staged_id,
            sha256=self.sha,
            commit=Commit.NOT_ATTEMPTED,
            temp_state=TempState.PRESENT,
        )
        if verified is None:
            return self._clean_after_journal_error()
        ts.phase = Phase.VERIFIED
        if self.cancelled():  # the last point a copy can be cancelled
            return self._clean_and_end(StatusV2.CANCELLED, Reason.CANCELLED)
        # The security once more, the last check before C14 and the rename: the source's
        # descriptor now (not only the one read before the copy), the temporary file's,
        # the category. Not atomic with the rename; it closes the stale descriptor.
        problem, error = self._acl_problem(source=True)
        if problem is not None:
            failed = StatusV2.FAILED
            return self._clean_and_end(failed, Reason.VERIFY_FAILED, error=error, detail=problem)
        # C14
        attempted = self._write(
            StatusV2.COMMIT_ATTEMPTED,
            temp_id=self.staged_id,
            sha256=self.sha,
            commit=Commit.ATTEMPTED,
            temp_state=TempState.PRESENT,
        )
        if attempted is None:  # 0 renames
            return self._clean_after_journal_error()
        ts.permit = True  # R-2
        ts.commit = Commit.ATTEMPTED
        ts.phase = Phase.COMMIT_ATTEMPTED
        self._commit_once()
        # C15 (R-11)
        ts.evidence = self._resolve()
        self._check_interrupt()
        self._sync()
        # C16
        return self._decide()

    def _create_plain(self) -> CopyResult | None:
        """C6, C7a, C7b by ``create_into`` (without ``acl``); None: staged."""
        ts = self.ts
        assert ts.temp is not None
        try:
            self.tmp = self.ops.new(self.directory.path / ts.temp, "H_TMP")
            outcome = self.ops.create_into(self.tmp, self.directory, ts.temp)
        except Exception as exc:
            if self.tmp is None or self.tmp.state in (HandleState.NEW, HandleState.NOT_OPENED):
                return self._end(StatusV2.FAILED, Reason.TEMP_CREATE_FAILED, error=exc)
            raise
        ts.create_outcome = outcome
        if outcome is OpenOutcome.NOT_CALLED:  # row 1
            return self._end(StatusV2.FAILED, Reason.TEMP_CREATE_FAILED)
        if outcome is OpenOutcome.NAME_TAKEN:  # row 3: someone else's name
            ts.phase = Phase.NAME_TAKEN
            return self._end(StatusV2.SKIPPED, Reason.TEMP_NAME_TAKEN)
        if outcome is OpenOutcome.NOT_CREATED_CLAIMED:  # rows 4, 5
            error = self.ops.open_error(self.tmp)
            ts.create_lookup = self._lookup(ts.temp)
            if ts.create_lookup.kind is LookupKind.ABSENT:
                return self._end(StatusV2.FAILED, Reason.TEMP_CREATE_FAILED, error=error)
            return self._create_uncertain(error)
        if outcome is not OpenOutcome.CREATED_OWNED or self.tmp.unreliable:  # rows 6, 7
            return self._create_uncertain(None)
        # C7a (row 8 on): the file is ours; is it what was just created?
        ts.phase = Phase.TEMP_CREATED_ID_UNKNOWN
        try:
            info = self.ops.query(self.tmp)
        except OSError as exc:  # row 9
            return self._clean_and_end(StatusV2.FAILED, Reason.TEMP_QUERY_FAILED, error=exc)
        ts.temp_id_observed = info.file_id
        problem = self._temp_problem(info, size=0, mtime_ns=None)
        if problem is not None:  # row 11: temp_id stays unknown
            return self._clean_and_end(StatusV2.FAILED, Reason.TEMP_TAMPERED, detail=problem)
        ts.temp_id = info.file_id
        # C7b
        staged = self._write(
            StatusV2.STAGED,
            temp_id=ts.temp_id,
            commit=Commit.NOT_ATTEMPTED,
            temp_state=TempState.PRESENT,
        )
        if staged is None:  # row 12
            return self._clean_after_journal_error()
        return None

    def _create_acl(self) -> CopyResult | None:
        """C6 by ``aclcreate.create_temp``, C7a, C7b by ``journal_temp``; None: staged.

        Only a ``CREATED`` file is held here (and removed, if at all, by CL); any other
        state is what ``create_temp`` established, journaled as it is. An object left, an
        unknown outcome or a refused category stops the run; nothing here removes or
        recovers it."""
        ts, acl = self.ts, self.acl
        assert acl is not None and ts.temp is not None and ts.intent is not None
        try:
            creation = aclcreate.create_temp(
                acl.ops, self.directory, acl.directory, ts.temp, acl.file_sd, acl.source_dir,
                acl.creator,
            )  # fmt: skip
        except BaseException as exc:
            self._took(exc)
            raise
        self.creation = creation
        for error in creation.release_errors:
            self._release_error(error)
        ts.create_outcome = creation.outcome
        identity = creation.identity
        if identity is not None:
            ts.temp_id_observed = identity.file_id
        if creation.state is Created.CREATED:  # C7a: still the one created and checked?
            assert creation.handle is not None and identity is not None
            self.tmp = creation.handle
            ts.phase = Phase.TEMP_CREATED_ID_UNKNOWN
            try:
                info = self.ops.query(self.tmp)
            except OSError as exc:
                return self._clean_and_end(StatusV2.FAILED, Reason.TEMP_QUERY_FAILED, error=exc)
            problem = self._temp_problem(info, size=0, mtime_ns=None)
            if problem is None and info.file_id != identity.file_id:
                problem = f"file id {info.file_id:032x} is not {identity.file_id:032x}"
            if problem is not None:
                return self._clean_and_end(StatusV2.FAILED, Reason.TEMP_TAMPERED, detail=problem)
        journaled = aclcreate.journal_temp(
            self.journal, creation, intent=ts.intent, source=self.source, target=self.target,
            source_volume=self.source_volume, target_volume=self.target_volume, temp=ts.temp,
            size=self.facts.size, source_id=self.facts.file_id,
        )  # fmt: skip
        record = journaled.record
        if journaled.journal_error is not None:  # row 12, or no record of what is there
            self.journal_error = journaled.journal_error
            self._stop(Stop.JOURNAL_FAILED)
            if self.tmp is not None:
                return self._clean_after_journal_error()
            if record.temp_state in (TempState.LEFT, TempState.UNKNOWN):
                self._stop(Stop.RUN_ABORTED)
            gone = record.temp_state in (TempState.NONE, TempState.REMOVED)
            status = CopyStatus.CANCELLED if gone else CopyStatus.FAILED
            return self._result(status, Reason.JOURNAL_FAILED, self.journal_error)
        if journaled.may_continue:
            assert identity is not None
            ts.temp_id = identity.file_id
            return None
        ts.terminal = record.status
        if creation.outcome is OpenOutcome.NAME_TAKEN:
            ts.phase = Phase.NAME_TAKEN
        if record.temp_state is TempState.UNKNOWN:
            ts.phase = Phase.CREATE_UNCERTAIN
        refusal = creation.refusal
        parent = refusal is not None and refusal.reason in _PARENT_REFUSALS
        if parent or record.temp_state in (TempState.LEFT, TempState.UNKNOWN):
            self._stop(Stop.RUN_ABORTED)
        error = creation.raised if creation.raised is not None else creation.error
        return self._result(_ENDED[record.status], record.reason, error)

    def _took(self, exc: BaseException) -> None:
        """Keep the ``Creation`` that an exception leaving ``create_temp`` carries."""
        for creation in getattr(exc, "creations", ()):
            if creation.role == "H_TMP" and creation.name == self.ts.temp:
                self.creation = creation

    def _acl_problem(self, *, source: bool = False) -> tuple[str | None, OSError | None]:
        """With ``acl``: the temporary file's descriptor exactly as created and no wider
        than the source's, and the category still the proven one, both by handle. With
        ``source`` the source's descriptor is read again first (``_source_now``) and the
        temporary file is judged against that one, never only against ``acl.file_sd``."""
        acl, creation = self.acl, self.creation
        if acl is None:
            return None, None
        assert creation is not None and creation.derived is not None and self.tmp is not None
        original = acl.file_sd
        if source:
            current, problem, error = self._source_now()
            if current is None:
                return problem, error
            original = current
        refusal, error = aclcreate.verify(
            acl.ops, self.tmp, creation.derived, original, acl.creator,
            aclproof.FILE_ALL_ACCESS,
        )  # fmt: skip
        if refusal is None:
            refusal, error = aclcreate.prove_parent(acl.ops, self.directory, acl.directory)
        if refusal is None:
            return None, None
        return f"acl {refusal.reason}: {refusal.detail}", error

    def _source_now(self) -> tuple[Descriptor | None, str | None, OSError | None]:
        """The source's descriptor read now through ``H_SRC`` (still the planned source),
        supported, and byte for byte ``acl.file_sd``, the one the temporary file's DACL was
        derived from: any change is a refusal (no finer comparison of what changed). It,
        or None and why not; an unreadable descriptor is a refusal, never a pass."""
        acl = self.acl
        assert acl is not None
        try:
            now = SourceFacts.of(self.ops.query(self.src))
        except OSError as exc:
            return None, f"the source: {describe_error(exc)}", exc
        if now != self.facts:
            return None, f"the source now {now}", None
        try:
            current = Descriptor.parse(acl.ops.read_descriptor(self.src))
        except aclproof.MalformedDescriptor as exc:
            return None, f"the source's descriptor: {exc}", None
        except OSError as exc:
            return None, f"the source's descriptor: {describe_error(exc)}", exc
        refusal = aclproof.supported(current, "the source")
        if refusal is not None:
            return None, f"acl {refusal.reason}: {refusal.detail}", None
        if current.raw != acl.file_sd.raw:
            return None, "the source's descriptor changed after the copy began", None
        return current, None, None

    def _create_uncertain(self, error: OSError | None) -> CopyResult:
        """Rows 5..7: a file may exist that is not ours: no CL, only the close."""
        ts = self.ts
        assert self.tmp is not None and ts.temp is not None
        ts.phase = Phase.CREATE_UNCERTAIN
        self._stop(Stop.RUN_ABORTED)
        if ts.create_lookup is None:
            ts.create_lookup = self._lookup(ts.temp)
        handle = f"{self.tmp.outcome} {self.tmp.state}"
        self._close_tmp()
        self._check_interrupt()
        diagnostics = Diagnostics(
            temp_lookup=_lookup_text(ts.create_lookup),
            temp_handle=f"{handle} -> {self.tmp.state}",
        )
        return self._end(
            StatusV2.FAILED,
            Reason.TEMP_CREATE_UNCERTAIN,
            temp_state=TempState.UNKNOWN,
            diagnostics=diagnostics,
            error=error,
        )

    def _temp_problem(self, info: HandleInfo, *, size: int, mtime_ns: int | None) -> str | None:
        """What makes the temporary file not the one this copy made (C7a, C12)."""
        ts = self.ts
        if info.directory:
            return "a directory"
        if info.reparse_point:
            return f"a reparse point ({info.reparse_tag:#x})"
        if info.links != 1:
            return f"{info.links} links"
        if info.delete_pending:
            return "delete pending"
        if info.volume_serial != self.target_volume.serial:
            return f"volume serial {info.volume_serial:#x} is not the target's"
        if ts.temp_id is not None and info.file_id != ts.temp_id:
            return f"file id {info.file_id:032x} is not {ts.temp_id:032x}"
        if info.size != size:
            return f"size {info.size}, expected {size}"
        if info.attributes & ~self.allowed:
            return f"attributes {info.attributes:#x} outside {self.allowed:#x}"
        if mtime_ns is not None and info.mtime_ns != mtime_ns:
            return f"mtime {info.mtime_ns}, expected {mtime_ns}"
        return None

    def _before_write(self) -> None:
        # R-7: no write and no set-information on the temporary file after the flag
        _require(not self.ts.permit and not self.ts.rename_spent, "a write after the permit")

    def _write_content(self) -> CopyResult | None:
        """C8 (blocks: read, hash, write; cancel between blocks), C9, C10."""
        assert self.tmp is not None
        size = self.facts.size
        digest = hashlib.sha256()
        offset = 0
        while offset < size:
            if self.cancelled():
                return self._clean_and_end(StatusV2.CANCELLED, Reason.CANCELLED)
            count = min(self.block_size, size - offset)
            try:
                data = self.ops.read_at(self.src, offset, count)
            except OSError as exc:
                return self._clean_and_end(StatusV2.FAILED, Reason.READ_FAILED, error=exc)
            if len(data) != count:
                detail = f"read {len(data)} of {count} bytes at {offset}"
                return self._clean_and_end(StatusV2.FAILED, Reason.SOURCE_CHANGED, detail=detail)
            digest.update(data)
            self._before_write()
            try:
                self.ops.write_at(self.tmp, offset, data)
            except OSError as exc:
                return self._clean_and_end(StatusV2.FAILED, Reason.WRITE_FAILED, error=exc)
            offset += count
        self._before_write()
        try:
            self.ops.set_mtime(self.tmp, self.facts.mtime_ns)
        except OSError as exc:
            return self._clean_and_end(StatusV2.FAILED, Reason.METADATA_FAILED, error=exc)
        self._before_write()
        try:
            self.ops.flush(self.tmp)
        except OSError as exc:
            return self._clean_and_end(StatusV2.FAILED, Reason.FLUSH_FAILED, error=exc)
        self.sha = digest.hexdigest()
        return None

    def _verify(self) -> CopyResult | None:
        """C12: the source unchanged; the temporary file read back, queried, one stream,
        no EA, with ``acl`` its security and the category. Its attributes are kept for
        P1-META."""
        assert self.tmp is not None
        size = self.facts.size
        failed = StatusV2.FAILED
        try:
            now = SourceFacts.of(self.ops.query(self.src))
            if now != self.facts:
                return self._clean_and_end(failed, Reason.SOURCE_CHANGED, detail=f"now {now}")
            digest = hashlib.sha256()
            offset = 0
            while offset < size:
                count = min(self.block_size, size - offset)
                data = self.ops.read_at(self.tmp, offset, count)
                if len(data) != count:
                    detail = f"read back {len(data)} of {count} bytes at {offset}"
                    return self._clean_and_end(failed, Reason.VERIFY_FAILED, detail=detail)
                digest.update(data)
                offset += count
            if self.ops.read_at(self.tmp, size, 1) != b"":
                return self._clean_and_end(failed, Reason.VERIFY_FAILED, detail="longer")
            if digest.hexdigest() != self.sha:
                return self._clean_and_end(failed, Reason.VERIFY_FAILED, detail="sha256 differs")
            info = self.ops.query(self.tmp)
            problem = self._temp_problem(info, size=size, mtime_ns=self.facts.mtime_ns // 100 * 100)
            if problem is not None:
                return self._clean_and_end(failed, Reason.VERIFY_FAILED, detail=problem)
            streams = self.ops.query_streams(self.tmp)
            if streams != (Stream(MAIN_STREAM, size),):
                detail = f"streams {streams}"
                return self._clean_and_end(failed, Reason.VERIFY_FAILED, detail=detail)
            ea_size = self.ops.query_ea_size(self.tmp)
            if ea_size != 0:
                return self._clean_and_end(failed, Reason.VERIFY_FAILED, detail=f"ea {ea_size}")
        except OSError as exc:
            return self._clean_and_end(failed, Reason.VERIFY_FAILED, error=exc)
        problem, error = self._acl_problem(source=True)
        if problem is not None:
            return self._clean_and_end(failed, Reason.VERIFY_FAILED, error=error, detail=problem)
        self.c12_attributes = info.attributes
        return None

    def _commit_once(self) -> None:
        """R-3: once, only with the permit; the flag is set right before the call."""
        ts = self.ts
        _require(ts.permit and not ts.rename_spent, "a second rename, or one without permit")
        assert self.tmp is not None
        ts.rename_spent = True
        # Whatever it raises is an interrupt here (R-6: never again; R-11: resolve).
        self.ops.rename_once(self.tmp, self.directory, self.target.name, self.attempt)
        ts.rename, ts.rename_status = self.attempt.result, self.attempt.status

    def _resolve(self) -> Evidence:
        """C15 (§3.3, R-11): read only; an interrupt makes it UNKNOWN."""
        ts = self.ts
        evidence = Evidence()
        ts.evidence = evidence  # UNKNOWN until it is complete
        assert self.tmp is not None and ts.temp is not None
        try:
            try:
                evidence.p1 = self.ops.query(self.tmp)
            except OSError as exc:
                evidence.p1_error = describe_error(exc)
            if evidence.p1 is not None:
                streams: tuple[Stream, ...] | None
                ea_size: int | None
                try:
                    streams = self.ops.query_streams(self.tmp)
                except OSError as exc:
                    streams = None
                    evidence.streams_error = describe_error(exc)
                try:
                    ea_size = self.ops.query_ea_size(self.tmp)
                except OSError as exc:
                    ea_size = None
                    evidence.ea_error = describe_error(exc)
                evidence.meta = meta_violation(
                    evidence.p1,
                    streams,
                    ea_size,
                    size=self.facts.size,
                    mtime_ns=self.facts.mtime_ns // 100 * 100,
                    attributes=self.c12_attributes,
                    allowed=self.allowed,
                    streams_error=evidence.streams_error,
                    ea_error=evidence.ea_error,
                )
                acl, error = self._acl_problem()
                if acl is not None and error is not None:
                    acl += f" ({describe_error(error)})"
                if acl is not None:
                    evidence.meta = acl if evidence.meta is None else f"{evidence.meta}; {acl}"
            try:
                evidence.name = self.ops.query_name(self.tmp)
            except OSError as exc:
                evidence.name_error = describe_error(exc)
            evidence.final = self._lookup(self.target.name)
            evidence.temp = self._lookup(ts.temp)
            evidence.verdict = judge_commit(
                evidence.p1,
                serial=self.target_volume.serial,
                temp_id=self.staged_id,
                name=evidence.name,
                final_name=self.target.name,
                temp_name=ts.temp,
                final=evidence.final,
                temp=evidence.temp,
            )
        except BaseException as exc:
            evidence.interrupted = True
            evidence.verdict = Verdict.UNKNOWN
            self._note_interrupt(exc)
        return evidence

    def _decide(self) -> CopyResult:
        """C16 (§3.5.2)."""
        ts = self.ts
        evidence = ts.evidence
        assert evidence is not None
        renamed = self.attempt.result
        if (
            renamed is Renamed.OK
            and self.attempt.status == 0
            and evidence.verdict is Verdict.COMMITTED
            and evidence.meta is None
        ):  # R-10
            return self._done()
        refused = renamed in (Renamed.EXISTS, Renamed.ERROR)
        if evidence.verdict is Verdict.NOT_COMMITTED and refused:  # R-8
            cleanup = self._cleanup()
            self._check_interrupt()
            extra = self._rename_diagnostics()
            if renamed is Renamed.EXISTS:
                return self._end_after_cleanup(
                    cleanup, StatusV2.SKIPPED, Reason.TARGET_EXISTS, extra=extra
                )
            reason = Reason.COMMIT_FAILED
            if self.attempt.status == STATUS_NOT_SAME_DEVICE:
                reason = Reason.CROSS_VOLUME
                self._stop(Stop.RUN_ABORTED)
            return self._end_after_cleanup(
                cleanup,
                StatusV2.FAILED,
                reason,
                extra=extra,
                detail=self._rename_text(),
                code=17 if reason is Reason.CROSS_VOLUME else None,
            )
        return self._recovery()

    def _rename_text(self) -> str:
        status = self.attempt.status
        text = str(self.attempt.result or "not called")
        if status is not None:
            text += f" (NTSTATUS {status:#010x})"
        return text

    def _rename_diagnostics(self) -> dict[str, object]:
        evidence = self.ts.evidence
        status = self.attempt.status
        return {
            "rename_error": self._rename_text(),
            "ntstatus": None if status is None else f"{status:#010x}",
            "name_query": None if evidence is None else (evidence.name or evidence.name_error),
            "final_lookup": None if evidence is None else _lookup_text(evidence.final),
        }

    def _recovery(self) -> CopyResult:
        """RR (§3.5.2): written before the one close of the temporary file; nothing is
        changed after it."""
        ts = self.ts
        evidence = ts.evidence
        assert evidence is not None and self.staged_id is not None
        renamed = self.attempt.result
        p1 = evidence.p1
        pending = p1 is not None and p1.delete_pending
        committed = evidence.verdict is Verdict.COMMITTED
        if pending:
            reason = Reason.DELETE_PENDING
        elif committed and renamed is Renamed.OK:
            reason = Reason.METADATA_VIOLATION
        elif renamed is Renamed.OK and evidence.verdict is Verdict.NOT_COMMITTED:
            reason = Reason.RENAME_OK_NOT_COMMITTED
        elif renamed is Renamed.NOT_ESTABLISHED:
            reason = Reason.RENAME_NOT_ESTABLISHED
        elif committed:
            reason = Reason.COMMITTED_DESPITE_RENAME_ERROR
        else:
            reason = Reason.COMMIT_UNKNOWN
        aborting = (
            Reason.DELETE_PENDING,
            Reason.METADATA_VIOLATION,
            Reason.RENAME_OK_NOT_COMMITTED,
            Reason.RENAME_NOT_ESTABLISHED,
        )
        if reason in aborting:
            self._stop(Stop.RUN_ABORTED)
        state = TempState.COMMITTED if committed else TempState.UNKNOWN
        diagnostics = Diagnostics(
            **self._rename_diagnostics(),  # type: ignore[arg-type]
            temp_lookup=_lookup_text(evidence.temp),
            temp_handle=_info_text(p1) or evidence.p1_error,
            temp_id_observed=None if p1 is None else f"{p1.file_id:032x}",
            metadata_violation=evidence.meta,
            delete_pending=None if p1 is None else p1.delete_pending,
            close_may_complete_delete=None if p1 is None else pending,
        )
        ts.temp_state = state
        sequence = self._write(
            StatusV2.RECOVERY_REQUIRED,
            temp_id=self.staged_id,
            sha256=self.sha,
            target_id=self.staged_id if committed else None,
            commit=Commit.COMMITTED if committed else Commit.UNKNOWN,
            temp_state=state,
            reason=str(reason),
            diagnostics=diagnostics,
        )
        ts.terminal = None if sequence is None else StatusV2.RECOVERY_REQUIRED
        return self._result(CopyStatus.RECOVERY_REQUIRED, reason, None)

    def _done(self) -> CopyResult:
        ts = self.ts
        ts.temp_state = TempState.COMMITTED
        sequence = self._write(
            StatusV2.DONE,
            temp_id=self.staged_id,
            sha256=self.sha,
            target_id=self.staged_id,
            commit=Commit.COMMITTED,
            temp_state=TempState.COMMITTED,
        )
        if sequence is None:  # committed, but the journal does not say so
            return self._result(
                CopyStatus.RECOVERY_REQUIRED, Reason.JOURNAL_FAILED, self.journal_error
            )
        ts.terminal = StatusV2.DONE
        return self._result(CopyStatus.COPIED, None, None)

    # -- terminal records and results --

    def _end(
        self,
        status: StatusV2,
        reason: Reason,
        *,
        temp_state: TempState = TempState.NONE,
        diagnostics: Diagnostics | None = None,
        error: BaseException | None = None,
        detail: str | None = None,
        code: int | None = None,
    ) -> CopyResult:
        """A failed, skipped or cancelled record, then the result."""
        message = detail if error is None else describe_error(error)
        if error is not None and detail is not None:
            message = f"{detail}: {message}"
        self.ts.temp_state = temp_state
        sequence = self._write(
            status,
            temp_id=self.staged_id,
            sha256=self.sha,
            reason=str(reason),
            error_code=code if code is not None else _error_code(error),
            error_message=message,
            commit=Commit.NOT_COMMITTED if self.ts.permit else Commit.NOT_ATTEMPTED,
            temp_state=temp_state,
            diagnostics=diagnostics,
        )
        self.ts.terminal = None if sequence is None else status
        return self._result(_ENDED[status], reason, error)

    def _result(
        self, status: CopyStatus, reason: str | None, error: BaseException | None
    ) -> CopyResult:
        """C17: the temporary file's handle closed once, then the source's."""
        self._close_tmp()
        self._close_src()
        self._check_interrupt()
        self._sync()
        return CopyResult(
            status=status,
            reason=None if reason is None else str(reason),
            stop=self.stop,
            transfer=self.ts,
            cleanup_errors=tuple(self.cleanup_errors),
            journal_error=self.journal_error,
            terminal=self.ts.terminal,
            error=error,
        )

    def _write(self, status: StatusV2, **fields: object) -> int | None:
        """One copy record; a ``JournalError`` stops the run (None returned)."""
        intent = None if status is StatusV2.INTENT else self.ts.intent
        try:
            return self.journal.write(
                OperationV2.COPY,
                status,
                intent=intent,
                source=self.source,
                target=self.target,
                source_volume=self.source_volume,
                target_volume=self.target_volume,
                temp=self.ts.temp,
                size=self.facts.size,
                source_id=self.facts.file_id,
                **fields,  # type: ignore[arg-type]
            )
        except JournalError as exc:
            self.journal_error = exc
            self._stop(Stop.JOURNAL_FAILED)
            return None

    def _stop(self, stop: Stop) -> None:
        if self.stop is None:
            self.stop = stop

    def _clean_and_end(
        self,
        status: StatusV2,
        reason: Reason,
        *,
        error: BaseException | None = None,
        detail: str | None = None,
    ) -> CopyResult:
        cleanup = self._cleanup()
        self._check_interrupt()
        return self._end_after_cleanup(cleanup, status, reason, error=error, detail=detail)

    def _end_after_cleanup(
        self,
        cleanup: Cleanup,
        status: StatusV2,
        reason: Reason,
        *,
        extra: dict[str, object] | None = None,
        error: BaseException | None = None,
        detail: str | None = None,
        code: int | None = None,
    ) -> CopyResult:
        assert cleanup.outcome is not None
        diagnostics = None
        if cleanup.outcome in (TempState.LEFT, TempState.UNKNOWN):
            observed = self.ts.temp_id_observed
            pending = {Probe.TRUE: True, Probe.FALSE: False}.get(cleanup.pending)
            diagnostics = Diagnostics(
                **(extra or {}),  # type: ignore[arg-type]
                temp_lookup=_lookup_text(cleanup.lookup),
                temp_handle=None if self.tmp is None else str(self.tmp.state),
                cleanup=cleanup.describe(),
                temp_id_observed=None if observed is None else f"{observed:032x}",
                delete_pending=pending,
                close_may_complete_delete=cleanup.close_may_complete_delete,
            )
        return self._end(
            status,
            reason,
            temp_state=cleanup.outcome,
            diagnostics=diagnostics,
            error=error,
            detail=detail,
            code=code,
        )

    def _clean_after_journal_error(self) -> CopyResult:
        """Row 12 and the later phases: CL, no record; CANCELLED only if removed."""
        cleanup = self._cleanup()
        self._check_interrupt()
        removed = cleanup.outcome is TempState.REMOVED
        status = CopyStatus.CANCELLED if removed else CopyStatus.FAILED
        return self._result(status, Reason.JOURNAL_FAILED, self.journal_error)

    # -- CL (§3.6) --

    def _tmp_open(self) -> bool:
        return self.tmp is not None and not self.tmp_closed and self.tmp.state is HandleState.OPEN

    def _owned_open(self) -> bool:
        """O without the permit part."""
        return (
            self._tmp_open()
            and self.tmp is not None
            and self.tmp.role == "H_TMP"
            and self.tmp.outcome is OpenOutcome.CREATED_OWNED
        )

    def _cleanup(self) -> Cleanup:
        ts = self.ts
        tmp = self.tmp
        _require(ts.cleanup is None, "CL a second time")
        _require(self._owned_open(), "CL of a temporary file not created and held here")
        proven = ts.evidence is not None and ts.evidence.verdict is Verdict.NOT_COMMITTED
        _require(not ts.permit or proven, "CL after the permit without a proven NC")
        assert tmp is not None and ts.temp is not None
        known_id = ts.temp_id
        cleanup = Cleanup(known=known_id is not None)
        ts.cleanup = cleanup
        # CL-1: through the handle only; CL-1b only after a fallback status (winprims)
        try:
            self.ops.dispose(tmp, cleanup.disposal)
        except Exception as exc:
            cleanup.errors.append(exc)
        except BaseException as exc:
            self._note_interrupt(exc)
            spent = cleanup.disposal.posix_spent or cleanup.disposal.classic_spent
            cleanup.interrupted = spent
        cleanup.disposed = cleanup.disposal.result
        # CL-2
        if not cleanup.interrupted:
            try:
                info = self.ops.query(tmp)
            except OSError as exc:
                cleanup.pending = Probe.ERR
                cleanup.errors.append(exc)
            except BaseException as exc:
                self._note_interrupt(exc)
                cleanup.interrupted = True
            else:
                if known_id is None:
                    known_id = info.file_id
                    cleanup.known = True
                if info.file_id != known_id:
                    cleanup.pending = Probe.ERR
                else:
                    cleanup.pending = Probe.TRUE if info.delete_pending else Probe.FALSE
        # CL-3: one close, whatever happened
        if self._close_tmp():
            cleanup.interrupted = True
        closed = tmp.state is HandleState.CLOSED
        cleanup.closed = Closed.CLOSED if closed else Closed.FAILED
        # CL-4
        if not cleanup.interrupted:
            try:
                cleanup.lookup = self._lookup(ts.temp)
            except BaseException as exc:
                self._note_interrupt(exc)
                cleanup.interrupted = True
            else:
                found = cleanup.lookup
                if found.kind is LookupKind.ABSENT:
                    cleanup.seen = Seen.ABSENT
                elif found.kind is LookupKind.UNKNOWN:
                    cleanup.seen = Seen.UNK
                elif cleanup.known and found.file_id == known_id:
                    cleanup.seen = Seen.OURS
                else:
                    cleanup.seen = Seen.OTHER
        cleanup.outcome, cleanup.hints, cleanup.close_may_complete_delete = cl_outcome(
            cleanup.disposed,
            cleanup.pending,
            cleanup.closed,
            cleanup.seen,
            cleanup.known,
            cleanup.interrupted,
        )
        ts.temp_state = cleanup.outcome
        return cleanup

    # -- handles --

    def _release_error(self, error: BaseException) -> None:
        if all(e is not error for e in self.cleanup_errors):
            self.cleanup_errors.append(error)
        self._stop(Stop.CLEANUP_FAILED)

    def _close_tmp(self) -> bool:
        """The one close of the temporary file's handle; True if it was interrupted."""
        tmp = self.tmp
        if tmp is None or self.tmp_closed:
            return False
        self.tmp_closed = True
        interrupted = False
        try:
            self.ops.close(tmp)
        except Exception as exc:
            self._release_error(exc)
        except BaseException as exc:
            self._note_interrupt(exc)
            interrupted = True
        failed = tmp.state in (HandleState.CLOSE_FAILED, HandleState.UNKNOWN)
        if failed and tmp.close_error is not None:
            self._release_error(tmp.close_error)
        return interrupted

    def _close_src(self) -> None:
        if self.src_closed:
            return
        self.src_closed = True
        try:
            self.ops.close(self.src)
        except Exception as exc:
            self._release_error(exc)
        except BaseException as exc:
            self._note_interrupt(exc)
        failed = self.src.state in (HandleState.CLOSE_FAILED, HandleState.UNKNOWN)
        if failed and self.src.close_error is not None:
            self._release_error(self.src.close_error)

    def _lookup(self, name: str) -> Lookup:
        """P3 / CL-4: one ``lookup_entry`` on a fresh ``H_LOOKUP``; read only."""
        handle = self.ops.new(self.directory.path, "H_LOOKUP")
        try:
            found = self.ops.lookup_entry(handle, self.directory, name)
        except Exception as exc:
            if isinstance(exc, winfileops.HandleCloseError):
                self._release_error(exc)
            return Lookup(LookupKind.UNKNOWN, reason=describe_error(exc))
        if handle.state is HandleState.UNKNOWN and handle.close_error is not None:
            self._release_error(handle.close_error)
        return found

    # -- the state as seen from outside --

    def _sync(self) -> None:
        ts = self.ts
        ts.handle_state = None if self.tmp is None else self.tmp.state
        ts.rename, ts.rename_status = self.attempt.result, self.attempt.status
        if ts.permit:
            verdict = None if ts.evidence is None else ts.evidence.verdict
            ts.commit = {
                Verdict.COMMITTED: Commit.COMMITTED,
                Verdict.NOT_COMMITTED: Commit.NOT_COMMITTED,
            }.get(verdict, Commit.UNKNOWN)  # type: ignore[arg-type]
            ts.phase = {
                Verdict.COMMITTED: Phase.COMMITTED,
                Verdict.NOT_COMMITTED: Phase.NOT_COMMITTED,
            }.get(verdict, Phase.UNKNOWN)  # type: ignore[arg-type]
        ts.temp_state = self._temp_state_now()

    def _temp_state_now(self) -> TempState:
        ts = self.ts
        if ts.cleanup is not None and ts.cleanup.outcome is not None:
            return ts.cleanup.outcome
        if ts.evidence is not None and ts.evidence.verdict is Verdict.COMMITTED:
            return TempState.COMMITTED
        tmp = self.tmp
        if tmp is None and self.acl is not None:  # what ``create_temp`` established
            if self.creation is not None:
                return aclcreate._TEMP_STATES[self.creation.state]
            return TempState.UNKNOWN if ts.phase is Phase.CREATE_ATTEMPTED else TempState.NONE
        if tmp is None:
            return TempState.NONE
        never = (None, OpenOutcome.NOT_CALLED, OpenOutcome.NAME_TAKEN)
        if tmp.state in (HandleState.NEW, HandleState.NOT_OPENED) and tmp.outcome in never:
            return TempState.NONE
        absent = ts.create_lookup is not None and ts.create_lookup.kind is LookupKind.ABSENT
        if tmp.outcome is OpenOutcome.NOT_CREATED_CLAIMED and absent:
            return TempState.NONE
        if tmp.state is HandleState.OPEN and ts.terminal is None and self.interrupt is None:
            return TempState.PRESENT
        return TempState.UNKNOWN


def copy_item(
    journal: JournalV2,
    *,
    source: Path,
    target: Path,
    src: Handle,
    directory: Handle,
    facts: SourceFacts,
    source_volume: VolumeRef,
    target_volume: VolumeRef,
    ops: Ops | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    block_size: int = 1 << 20,
    allowed_attributes: int = DEFAULT_ALLOWED_ATTRIBUTES,
    acl: AclCopy | None = None,
) -> CopyResult:
    """Copy the file of ``src`` (``H_SRC``, read from offset 0) to ``target`` in the
    target directory held as ``directory``; journal every step in ``journal`` (a run
    already started with these volumes). With ``acl`` the temporary file is created and
    checked with its security (``AclCopy``, see the module docstring).

    ``src`` is closed here (C17); ``directory`` stays the caller's. The source is never
    changed. Returns the result of a copy that ended; an interrupt is raised (see the
    module docstring).
    """
    _require(target.parent == directory.path, "target is not in the directory held")
    _require(type(block_size) is int and block_size > 0, f"block size {block_size!r}")
    _require(
        type(allowed_attributes) is int and allowed_attributes & ~DEFAULT_ALLOWED_ATTRIBUTES == 0,
        f"allowed attributes {allowed_attributes!r}",
    )
    machine = _Copy(
        journal,
        source=source,
        target=target,
        src=src,
        directory=directory,
        facts=facts,
        source_volume=source_volume,
        target_volume=target_volume,
        ops=WinOps() if ops is None else ops,
        cancelled=cancelled,
        block_size=block_size,
        allowed=allowed_attributes,
        acl=acl,
    )
    return machine.run()
