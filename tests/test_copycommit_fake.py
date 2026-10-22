"""The COPY state machine (R3B-4) over a scripted ``Ops``: no NT call, no file.

The fake models one target directory (names -> files), the handles to them and every
call the machine makes; interrupts and failures are injected at named events. The
journal is a real ``JournalV2`` on an in-memory stream, so every record the machine
writes is checked by the v2 grammar; ``analyze_v2`` reads it back.
"""

from __future__ import annotations

import io
import itertools
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from downloads_organizer import copycommit as cc
from downloads_organizer import journal_v2 as v2
from downloads_organizer.copycommit import (
    Closed,
    CopyStatus,
    Probe,
    Reason,
    Seen,
    SourceFacts,
    Stop,
    Verdict,
    cl_outcome,
    copy_item,
    judge_commit,
    meta_violation,
)
from downloads_organizer.journal import JournalError
from downloads_organizer.journal_v2 import (
    Commit,
    JournalV2,
    OperationV2,
    StatusV2,
    TempState,
    VolumeRef,
    analyze_v2,
    temp_name,
)
from downloads_organizer.winfileops import (
    HandleCloseError,
    HandleInfo,
    HandleState,
    HandleUnknownError,
    OpenOutcome,
    _require,
    signed32,
)
from downloads_organizer.winprims import (
    FALLBACK_STATUSES,
    STATUS_CANNOT_DELETE,
    Disposal,
    Disposed,
    Lookup,
    LookupKind,
    RenameAttempt,
    Renamed,
    Stream,
    _Call,
)

RUN_ID = "3f2a5c0d9e1b4a7c8d6e5f4a3b2c1d0e"
SV = VolumeRef("\\Device\\HarddiskVolume3", 0xA1B2C3D4)
TV = VolumeRef("\\Device\\HarddiskVolume5", 0xE5F60718)
INBOX, ROOT = Path("D:/Inbox"), Path("E:/Sorted")
SOURCE, TARGET = INBOX / "a.pdf", ROOT / "a.pdf"
SOURCE_ID = 0x0123456789ABCDEF0123456789ABCDEF
DATA = bytes(range(256)) * 10 + b"tail"
MTIME = 1_767_225_600_123_456_700  # a multiple of 100 ns
TEMP = temp_name(RUN_ID, 2)  # the intent is the second record, after run/started
STATUS_PENDING = 0x0000_0103
STATUS_ACCESS_DENIED = 0xC000_0022
STATUS_COLLISION = 0xC000_0035
S = StatusV2


class Boom(KeyboardInterrupt):
    """The injected interrupt (a ``BaseException``, never an ``Exception``)."""


@dataclass(eq=False)
class FakeFile:
    file_id: int
    data: bytearray = field(default_factory=bytearray)
    attributes: int = 0x20
    mtime_ns: int = 0
    links: int = 1
    delete_pending: bool = False
    extra_streams: tuple[Stream, ...] = ()
    ea: int = 0
    serial: int = TV.serial
    reparse_tag: int = 0
    directory: bool = False

    def info(self) -> HandleInfo:
        return HandleInfo(
            self.serial,
            self.file_id,
            self.attributes,
            self.reparse_tag,
            len(self.data),
            self.mtime_ns,
            self.links,
            self.delete_pending,
            self.directory,
        )


class FakeHandle:
    """A handle as the machine sees one: ``state``, ``outcome``, ``close`` like
    ``winfileops.Handle`` (never a real ``Handle`` with a made-up raw value)."""

    def __init__(self, fs: FakeFs, path: Path, role: str, file: FakeFile | None = None) -> None:
        self.fs = fs
        self.path = path
        self.role = role
        self.file = file
        self.state = HandleState.NEW if file is None else HandleState.OPEN
        self.outcome: OpenOutcome | None = None
        self.unreliable = False
        self.close_error: HandleCloseError | None = None
        self.close_calls = 0  # CloseHandle calls
        self.close_attempts = 0  # close() calls

    def close(self) -> None:
        self.close_attempts += 1
        if self.state is HandleState.NEW:
            self.state = HandleState.NOT_OPENED
            return
        if self.state in (HandleState.CLOSE_FAILED, HandleState.UNKNOWN):
            assert self.close_error is not None
            raise self.close_error
        if self.state is not HandleState.OPEN:
            return
        self.close_calls += 1
        self.fs.log.append(("close", self.role))
        self.state = HandleState.CLOSE_FAILED
        self.close_error = _close_error(self.path, None)
        self.fs.event(f"close:{self.role}")
        mode = self.fs.close_mode.get(self.role)
        if mode == "error":
            self.close_error = _close_error(self.path, 6)
            raise self.close_error
        self.state = HandleState.CLOSED
        self.close_error = None
        self.fs.closed(self)


def _close_error(path: Path, winerror: int | None) -> HandleCloseError:
    exc = HandleCloseError(0, f"CloseHandle failed ({winerror})", str(path), winerror or 0)
    exc.handle_value = 0x44
    return exc


def _unknown_error(path: Path) -> HandleUnknownError:
    exc = HandleUnknownError(0, "handle unknown", str(path), 0)
    exc.handle_value = 0
    return exc


class FakeFs:
    """One target directory, its handles, the calls made on them, and the scripted
    events (``at``: ``name`` for every call, ``name@n`` for the n-th)."""

    def __init__(self) -> None:
        self.entries: dict[str, FakeFile] = {}
        self.ids = itertools.count(0x42)
        self.log: list[tuple[str, ...]] = []
        self.counts: dict[str, int] = {}
        self.at: dict[str, Callable[[], None]] = {}
        self.handles: list[FakeHandle] = []
        self.close_mode: dict[str, str] = {}
        self.source = FakeFile(
            SOURCE_ID, bytearray(DATA), attributes=0x20, mtime_ns=MTIME, serial=SV.serial
        )

    def event(self, name: str) -> None:
        count = self.counts[name] = self.counts.get(name, 0) + 1
        for key in (name, f"{name}@{count}"):
            action = self.at.get(key)
            if action is not None:
                action()

    def name_of(self, file: FakeFile) -> str | None:
        return next((n for n, f in self.entries.items() if f is file), None)

    def closed(self, handle: FakeHandle) -> None:
        file = handle.file
        if file is None or not file.delete_pending or handle.role == "H_LOOKUP":
            return
        others = [
            h
            for h in self.handles
            if h is not handle and h.file is file and h.state is HandleState.OPEN
        ]
        if not others:  # delete on the last close (POSIX: the name stays until then)
            name = self.name_of(file)
            if name is not None:
                del self.entries[name]

    def new_file(self, **kw: object) -> FakeFile:
        return FakeFile(next(self.ids) | (1 << 64), **kw)  # type: ignore[arg-type]

    def calls(self, kind: str) -> list[tuple[str, ...]]:
        return [entry for entry in self.log if entry[0] == kind]


def raiser(exc: BaseException) -> Callable[[], None]:
    def action() -> None:
        raise exc

    return action


class FakeOps:
    """``copycommit.Ops`` over ``FakeFs``; the semantics of the primitives it replaces
    (order of ``spent`` and the call, fallback only after its statuses, ...)."""

    def __init__(self, fs: FakeFs) -> None:
        self.fs = fs
        self.create_mode = "auto"
        self.claimed_file = False  # NOT_CREATED_CLAIMED: something was created anyway
        self.rename_status: int | None = None  # None: what the directory says
        self.rename_moves: bool | None = None  # None: as the status says
        self.posix_status = 0
        self.classic_status = 0
        self.lookup_unknown: set[str] = set()
        self.after_rename: Callable[[FakeFile], None] | None = None
        self.rename_entered = 0  # rename_once calls, before any check of its own

    def _handle(self, handle: FakeHandle) -> FakeFile:
        assert isinstance(handle, FakeHandle)
        _require(handle.state is HandleState.OPEN, f"{handle.role} is {handle.state}")
        assert handle.file is not None
        return handle.file

    def _side(self, handle: FakeHandle) -> str:
        return "src" if handle.role == "H_SRC" else "tmp"

    def new(self, path: Path, role: str) -> FakeHandle:
        handle = FakeHandle(self.fs, path, role)
        self.fs.handles.append(handle)
        return handle

    def create_into(self, handle: FakeHandle, parent: FakeHandle, name: str) -> OpenOutcome:
        _require(handle.role == "H_TMP" and handle.state is HandleState.NEW, "create_into")
        self.fs.log.append(("create", name))
        self.fs.event("create")
        mode = self.create_mode
        if mode == "auto":
            mode = "taken" if name in self.fs.entries else "created"
        if mode == "lost":  # interrupted, the status never captured (row 2)
            self.fs.entries[name] = self.fs.new_file()
            handle.state, handle.outcome = HandleState.UNKNOWN, OpenOutcome.UNCERTAIN
            handle.close_error = _unknown_error(handle.path)
            raise Boom
        if mode in ("created", "unreliable", "opened"):
            file = self.fs.entries.get(name) or self.fs.new_file()
            self.fs.entries[name] = file
            handle.file, handle.state = file, HandleState.OPEN
            handle.outcome = OpenOutcome.OPENED_OWNED
            if mode == "created":
                handle.outcome = OpenOutcome.CREATED_OWNED
            handle.unreliable = mode == "unreliable"
        elif mode == "taken":
            handle.state, handle.outcome = HandleState.NOT_OPENED, OpenOutcome.NAME_TAKEN
        elif mode == "claimed":
            if self.claimed_file:
                self.fs.entries[name] = self.fs.new_file()
            handle.state = HandleState.NOT_OPENED
            handle.outcome = OpenOutcome.NOT_CREATED_CLAIMED
        elif mode == "not_called":
            handle.state, handle.outcome = HandleState.NOT_OPENED, OpenOutcome.NOT_CALLED
        else:  # anomaly, unproven: maybe opened, never closed
            self.fs.entries[name] = self.fs.new_file()
            handle.state, handle.outcome = HandleState.UNKNOWN, OpenOutcome(mode)
            handle.close_error = _unknown_error(handle.path)
        return handle.outcome

    def open_error(self, handle: FakeHandle) -> OSError:
        return OSError(0, "STATUS_ACCESS_DENIED", str(handle.path), 5)

    def query(self, handle: FakeHandle) -> HandleInfo:
        file = self._handle(handle)
        self.fs.log.append(("query", self._side(handle)))
        self.fs.event(f"query:{self._side(handle)}")
        return file.info()

    def read_at(self, handle: FakeHandle, offset: int, size: int) -> bytes:
        file = self._handle(handle)
        self.fs.log.append(("read", self._side(handle)))
        self.fs.event(f"read:{self._side(handle)}")
        return bytes(file.data[offset : offset + size])

    def write_at(self, handle: FakeHandle, offset: int, data: bytes) -> None:
        file = self._handle(handle)
        self.fs.log.append(("write", str(offset)))
        self.fs.event("write")
        file.data[offset : offset + len(data)] = data

    def set_mtime(self, handle: FakeHandle, mtime_ns: int) -> None:
        file = self._handle(handle)
        self.fs.log.append(("set", "basic"))
        self.fs.event("set_mtime")
        file.mtime_ns = mtime_ns // 100 * 100

    def flush(self, handle: FakeHandle) -> None:
        self._handle(handle)
        self.fs.log.append(("flush",))
        self.fs.event("flush")

    def query_streams(self, handle: FakeHandle) -> tuple[Stream, ...]:
        file = self._handle(handle)
        self.fs.log.append(("streams",))
        self.fs.event("streams")
        return (Stream(cc.MAIN_STREAM, len(file.data)), *file.extra_streams)

    def query_ea_size(self, handle: FakeHandle) -> int:
        file = self._handle(handle)
        self.fs.log.append(("ea",))
        self.fs.event("ea")
        return file.ea

    def query_name(self, handle: FakeHandle) -> str:
        file = self._handle(handle)
        self.fs.log.append(("name",))
        self.fs.event("name")
        return f"\\Sorted\\{self.fs.name_of(file) or '?'}"

    def lookup_entry(self, handle: FakeHandle, directory: FakeHandle, name: str) -> Lookup:
        _require(handle.role == "H_LOOKUP" and handle.state is HandleState.NEW, "lookup_entry")
        self.fs.log.append(("lookup", name))
        handle.state = HandleState.OPEN
        try:
            self.fs.event("lookup")
        except BaseException:
            handle.close()
            raise
        handle.close()
        if name in self.lookup_unknown:
            return Lookup(LookupKind.UNKNOWN, reason="scripted")
        file = self.fs.entries.get(name)
        if file is None:
            return Lookup(LookupKind.ABSENT)
        return Lookup(LookupKind.FOUND, file.file_id)

    def rename_once(
        self, handle: FakeHandle, directory: FakeHandle, name: str, attempt: RenameAttempt
    ) -> Renamed:
        self.rename_entered += 1
        _require(not attempt.spent, "the rename was spent")
        file = self._handle(handle)
        _require(handle.role == "H_TMP", "rename of another role")
        self.fs.event("rename:before")  # an interrupt here: no call
        attempt.call = _Call(())
        attempt.spent = True
        self.fs.log.append(("rename", name))
        exists = name in self.fs.entries
        status = self.rename_status
        if status is None:
            status = STATUS_COLLISION if exists else 0
        moves = self.rename_moves if self.rename_moves is not None else status == 0
        if moves:
            old = self.fs.name_of(file)
            if old is not None:
                del self.fs.entries[old]
            self.fs.entries[name] = file
        if self.after_rename is not None:
            self.after_rename(file)
        self.fs.event("rename:after")  # an interrupt here: done, status lost (LOST)
        attempt.call.status = signed32(status)
        result = attempt.result
        assert result is not None
        return result

    def _disposition(self, file: FakeFile, call: _Call, status: int, kind: str) -> int:
        self.fs.log.append(("dispose", kind))
        self.fs.event(f"dispose:{kind}")
        if status == 0 or (status & 0xFFFF_FFFF) >> 30 not in (2, 3):
            file.delete_pending = True  # success, or a non-standard status that did it
        call.status = signed32(status)
        return status & 0xFFFF_FFFF

    def dispose(self, handle: FakeHandle, disposal: Disposal) -> Disposed:
        _require(
            handle.role == "H_TMP"
            and handle.outcome is OpenOutcome.CREATED_OWNED
            and handle.state is HandleState.OPEN,
            "dispose",
        )
        _require(not (disposal.posix_spent or disposal.classic_spent), "a disposition was spent")
        file = self._handle(handle)
        disposal.posix = _Call(())
        disposal.posix_spent = True
        status = self._disposition(file, disposal.posix, self.posix_status, "posix")
        if status not in FALLBACK_STATUSES:
            return disposal.result
        disposal.classic = _Call(())
        disposal.classic_spent = True
        self._disposition(file, disposal.classic, self.classic_status, "classic")
        return disposal.result

    def close(self, handle: FakeHandle) -> None:
        handle.close()


class FlakyJournal:
    """A real ``JournalV2`` whose ``write`` fails as scripted for one status."""

    def __init__(self, journal: JournalV2) -> None:
        self.journal = journal
        self.fail: dict[StatusV2, str] = {}
        self.statuses_at_close: list[StatusV2] = []

    @property
    def run_id(self) -> str:
        return self.journal.run_id

    @property
    def next_sequence(self) -> int:
        return self.journal.next_sequence

    @property
    def history(self) -> tuple[object, ...]:
        return self.journal.history

    def write(self, operation: OperationV2, status: StatusV2, **kw: object) -> int:
        mode = self.fail.get(status)
        if mode == "error":
            raise JournalError(f"scripted failure on {status}")
        if mode == "interrupt_before":
            raise Boom
        sequence = self.journal.write(operation, status, **kw)  # type: ignore[arg-type]
        if mode == "interrupt_after":
            raise Boom
        return sequence


def _clock() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


class Stream_(io.BytesIO):
    def fileno(self) -> int:
        return 7


class Rig:
    def __init__(self) -> None:
        self.fs = FakeFs()
        self.ops = FakeOps(self.fs)
        self.journal = FlakyJournal(JournalV2(Stream_(), run_id=RUN_ID, clock=_clock))
        self.journal.journal.write(
            OperationV2.RUN,
            S.STARTED,
            source=INBOX,
            target=ROOT,
            reason="items=1 copies=1",
            source_volume=SV,
            target_volume=TV,
        )
        self.src = FakeHandle(self.fs, SOURCE, "H_SRC", self.fs.source)
        self.src.outcome = OpenOutcome.OPENED_OWNED
        self.directory = FakeHandle(self.fs, ROOT, "H_DIR", FakeFile(1, directory=True))
        self.fs.handles += [self.src, self.directory]
        self.cancel = False

    def facts(self) -> SourceFacts:
        return SourceFacts(SOURCE_ID, len(DATA), MTIME, 0x20, SV.serial)

    def run(self, **kw: object) -> cc.CopyResult:
        kw.setdefault("block_size", 1000)
        kw.setdefault("cancelled", lambda: self.cancel)
        return copy_item(
            self.journal,  # type: ignore[arg-type]
            source=SOURCE,
            target=TARGET,
            src=self.src,  # type: ignore[arg-type]
            directory=self.directory,  # type: ignore[arg-type]
            facts=self.facts(),
            source_volume=SV,
            target_volume=TV,
            ops=self.ops,  # type: ignore[arg-type]
            **kw,  # type: ignore[arg-type]
        )

    def interrupted(self, exc: BaseException | None = None, **kw: object) -> BaseException:
        with pytest.raises(BaseException) as info:
            self.run(**kw)
        if exc is not None:
            assert info.value is exc
        return info.value

    # what the journal says

    def statuses(self) -> list[str]:
        return [r["status"] for r in self.journal.history]  # type: ignore[index]

    def last(self) -> dict[str, object]:
        return dict(self.journal.history[-1])  # type: ignore[arg-type]

    def report(self) -> v2.JournalReportV2:
        report = analyze_v2(self.journal.history, run_id=RUN_ID)
        assert report.problems == ()
        return report

    # what the fake saw

    @property
    def tmp(self) -> FakeHandle:
        handles = [h for h in self.fs.handles if h.role == "H_TMP"]
        assert len(handles) == 1
        return handles[0]

    def renames(self) -> int:
        return len(self.fs.calls("rename"))

    def disposals(self) -> list[str]:
        return [entry[1] for entry in self.fs.calls("dispose")]

    def after_rename(self) -> list[tuple[str, ...]]:
        index = self.fs.log.index(next(e for e in self.fs.log if e[0] == "rename"))
        return self.fs.log[index + 1 :]

    def single_closes(self) -> None:
        assert self.src.close_calls == 1
        for handle in self.fs.handles:
            assert handle.close_calls <= 1, handle.role
            if handle.role in ("H_TMP", "H_SRC"):
                assert handle.close_attempts <= 1, handle.role  # R-9: one close() call


@pytest.fixture(autouse=True)
def fsyncs(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(v2, "os", types.SimpleNamespace(fsync=calls.append))
    return calls


@pytest.fixture
def rig() -> Rig:
    return Rig()


def written(rig: Rig) -> list[str]:
    return rig.statuses()[1:]  # after run/started


PHASES = ["intent", "staged", "written", "verified"]
THROUGH_CA = [*PHASES, "commit_attempted"]


# --- the copy that works ----------------------------------------------------------------


def test_copies_through_every_phase(rig: Rig) -> None:
    result = rig.run()
    assert result.status is CopyStatus.COPIED
    assert (result.reason, result.stop, result.journal_error) == (None, None, None)
    assert result.terminal is S.DONE
    assert written(rig) == [*THROUGH_CA, "done"]
    copied = rig.fs.entries[TARGET.name]
    assert bytes(copied.data) == DATA and copied.mtime_ns == MTIME
    assert TEMP not in rig.fs.entries
    record = rig.last()
    assert record["temp"] == TEMP
    assert record["temp_id"] == record["target_id"] == f"{copied.file_id:032x}"
    assert record["sha256"] == __import__("hashlib").sha256(DATA).hexdigest()
    assert (record["commit"], record["temp_state"]) == ("committed", "committed")
    assert rig.renames() == 1 and rig.disposals() == []
    rig.single_closes()
    assert rig.tmp.close_calls == 1 and rig.directory.close_calls == 0
    ts = result.transfer
    assert ts.phase is cc.Phase.COMMITTED and ts.temp_state is TempState.COMMITTED
    assert ts.commit is Commit.COMMITTED and ts.permit and ts.rename_spent
    entry = rig.report().entries[-1]
    assert entry.outcome is not None and entry.outcome["status"] == "done"


def test_blocks_are_read_hashed_and_written_in_order(rig: Rig) -> None:
    rig.run(block_size=1000)
    writes = [int(e[1]) for e in rig.fs.calls("write")]
    assert writes == list(range(0, len(DATA), 1000))


def test_an_empty_file_copies(rig: Rig) -> None:
    rig.fs.source.data = bytearray()
    facts = SourceFacts(SOURCE_ID, 0, MTIME, 0x20, SV.serial)
    rig.facts = lambda: facts  # type: ignore[method-assign]
    result = rig.run()
    assert result.status is CopyStatus.COPIED
    assert rig.fs.calls("write") == [] and bytes(rig.fs.entries[TARGET.name].data) == b""


def test_after_the_rename_only_reads_and_one_close(rig: Rig) -> None:
    rig.run()
    kinds = {entry[0] for entry in rig.after_rename()}
    assert kinds <= {"query", "streams", "ea", "name", "lookup", "close"}  # R-7, R-11
    assert rig.after_rename().count(("close", "H_TMP")) == 1


def test_cancellation_is_not_asked_after_the_commit_attempt(rig: Rig) -> None:
    rig.fs.at["rename:before"] = lambda: setattr(rig, "cancel", True)
    assert rig.run().status is CopyStatus.COPIED


def test_a_cancel_once_commit_attempted_is_written_is_ignored(rig: Rig) -> None:
    result = rig.run(cancelled=lambda: "commit_attempted" in rig.statuses())
    assert result.status is CopyStatus.COPIED and rig.renames() == 1
    assert "cancelled" not in rig.statuses()


def test_the_source_handle_closes_after_the_temporary_file(rig: Rig) -> None:
    rig.run()
    closes = rig.fs.calls("close")
    assert closes[-2:] == [("close", "H_TMP"), ("close", "H_SRC")]


# --- §3.2: creating the temporary file --------------------------------------------------


def test_row3_name_taken_skips_without_touching_it(rig: Rig) -> None:
    other = rig.fs.new_file(data=bytearray(b"theirs"))
    rig.fs.entries[TEMP] = other
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.SKIPPED, Reason.TEMP_NAME_TAKEN)
    assert rig.fs.entries[TEMP] is other and bytes(other.data) == b"theirs"
    record = rig.last()
    assert (record["status"], record["temp_state"], record["commit"]) == (
        "skipped",
        "none",
        "not_attempted",
    )
    assert record["diagnostics"] is None and record["temp_id"] is None
    assert rig.disposals() == [] and rig.renames() == 0
    assert rig.tmp.close_calls == 0 and result.stop is None


def test_row1_create_not_called(rig: Rig) -> None:
    rig.ops.create_mode = "not_called"
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.FAILED, Reason.TEMP_CREATE_FAILED)
    assert rig.last()["temp_state"] == "none"


def test_an_exception_before_the_call_is_temp_create_failed(rig: Rig) -> None:
    rig.fs.at["create"] = raiser(OSError(22, "bad name"))
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.FAILED, Reason.TEMP_CREATE_FAILED)
    assert rig.last()["temp_state"] == "none" and "bad name" in str(rig.last()["error_message"])
    assert result.transfer.temp_state is TempState.NONE


def test_row4_claimed_and_absent_failed_none(rig: Rig) -> None:
    rig.ops.create_mode = "claimed"
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.FAILED, Reason.TEMP_CREATE_FAILED)
    record = rig.last()
    assert record["temp_state"] == "none" and record["error_code"] == 5
    assert rig.fs.calls("lookup") == [("lookup", TEMP)] and result.stop is None


@pytest.mark.parametrize("seen", ["found", "unknown"])
def test_row5_claimed_but_present_is_uncertain(rig: Rig, seen: str) -> None:
    rig.ops.create_mode = "claimed"
    rig.ops.claimed_file = seen == "found"
    if seen == "unknown":
        rig.ops.lookup_unknown.add(TEMP)
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.FAILED, Reason.TEMP_CREATE_UNCERTAIN)
    assert result.stop is Stop.RUN_ABORTED
    record = rig.last()
    assert record["temp_state"] == "unknown" and record["diagnostics"] is not None
    assert rig.disposals() == [] and rig.renames() == 0  # no CL
    assert result.transfer.temp_state is TempState.UNKNOWN
    if seen == "found":
        assert TEMP in rig.fs.entries  # whatever it is, it is left


@pytest.mark.parametrize("mode", ["anomaly", "unproven"])
def test_row6_unknown_handle_is_never_closed(rig: Rig, mode: str) -> None:
    rig.ops.create_mode = mode
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.FAILED, Reason.TEMP_CREATE_UNCERTAIN)
    assert result.stop is Stop.RUN_ABORTED
    assert [type(e) for e in result.cleanup_errors] == [HandleUnknownError]
    assert result.cleanup_errors[0] is rig.tmp.close_error
    assert rig.tmp.close_calls == 0 and rig.disposals() == []
    assert rig.last()["temp_state"] == "unknown"
    rig.report()


@pytest.mark.parametrize("mode", ["opened", "unreliable"])
def test_row7_opened_not_created_is_closed_only(rig: Rig, mode: str) -> None:
    existing = rig.fs.new_file(data=bytearray(b"x"))
    rig.fs.entries[TEMP] = existing
    rig.ops.create_mode = mode
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.FAILED, Reason.TEMP_CREATE_UNCERTAIN)
    assert rig.tmp.close_calls == 1 and rig.disposals() == [] and rig.fs.calls("write") == []
    assert rig.fs.entries[TEMP] is existing and result.stop is Stop.RUN_ABORTED


def test_row2_status_lost_on_create_is_an_interrupt(rig: Rig) -> None:
    rig.ops.create_mode = "lost"
    exc = rig.interrupted()
    assert isinstance(exc, Boom)
    assert written(rig) == ["intent"]  # nothing journaled after an interrupt
    (state,) = exc.transfer_states  # type: ignore[attr-defined]
    assert state.temp_state is TempState.UNKNOWN and rig.tmp.close_calls == 0
    assert [type(e) for e in exc.release_errors] == [HandleUnknownError]  # type: ignore[attr-defined]
    assert rig.src.close_calls == 1


def test_row9_query_failure_is_cleaned(rig: Rig) -> None:
    rig.fs.at["query:tmp@1"] = raiser(OSError(5, "denied"))
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.FAILED, Reason.TEMP_QUERY_FAILED)
    assert rig.last()["temp_state"] == "removed" and rig.last()["temp_id"] is None
    assert TEMP not in rig.fs.entries and rig.disposals() == ["posix"]
    rig.report()


def test_row10_query_interrupted_is_cleaned_without_record(rig: Rig) -> None:
    boom = Boom()
    rig.fs.at["query:tmp@1"] = raiser(boom)
    rig.interrupted(boom)
    assert written(rig) == ["intent"] and TEMP not in rig.fs.entries
    assert rig.disposals() == ["posix"] and rig.tmp.close_calls == 1
    (state,) = boom.transfer_states  # type: ignore[attr-defined]
    assert state.temp_state is TempState.REMOVED  # CL-R1: K from the CL-2 query


TAMPERS: dict[str, Callable[[FakeFile], None]] = {
    "links": lambda f: setattr(f, "links", 2),
    "delete_pending": lambda f: setattr(f, "delete_pending", True),
    "serial": lambda f: setattr(f, "serial", 0x77),
    "attributes": lambda f: setattr(f, "attributes", 0x21),
    "size": lambda f: setattr(f, "data", bytearray(b"!")),
    "directory": lambda f: setattr(f, "directory", True),
    "reparse": lambda f: setattr(f, "reparse_tag", 0xA000_000C),
}


@pytest.mark.parametrize("tamper", sorted(TAMPERS))
def test_row11_a_tampered_temporary_file_is_not_staged(rig: Rig, tamper: str) -> None:
    rig.fs.at["query:tmp@1"] = lambda: TAMPERS[tamper](rig.tmp.file)  # type: ignore[arg-type]
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.FAILED, Reason.TEMP_TAMPERED)
    record = rig.last()
    assert record["temp_id"] is None and written(rig) == ["intent", "failed"]
    assert result.transfer.temp_id is None
    assert result.transfer.temp_id_observed == rig.tmp.file.file_id  # type: ignore[union-attr]
    diagnostics = record["diagnostics"]
    if diagnostics is not None:
        assert diagnostics["temp_id_observed"] == f"{rig.tmp.file.file_id:032x}"  # type: ignore[index,union-attr]
    assert rig.renames() == 0
    rig.report()


@pytest.mark.parametrize(("posix", "removed"), [(0, True), (STATUS_CANNOT_DELETE, False)])
def test_row12_journal_error_on_staged(rig: Rig, posix: int, removed: bool) -> None:
    rig.journal.fail[S.STAGED] = "error"
    rig.ops.posix_status = posix
    result = rig.run()
    expected = CopyStatus.CANCELLED if removed else CopyStatus.FAILED
    assert (result.status, result.reason) == (expected, Reason.JOURNAL_FAILED)
    assert result.stop is Stop.JOURNAL_FAILED and isinstance(result.journal_error, JournalError)
    assert written(rig) == ["intent"] and result.terminal is None
    assert (TEMP in rig.fs.entries) is not removed


def test_a_journal_error_on_intent_creates_nothing(rig: Rig) -> None:
    rig.journal.fail[S.INTENT] = "error"
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.CANCELLED, Reason.JOURNAL_FAILED)
    assert rig.fs.calls("create") == [] and written(rig) == [] and rig.src.close_calls == 1


# --- §3.5.2: STAGED .. VERIFIED ---------------------------------------------------------


FAILURES = {
    "read:src@2": Reason.READ_FAILED,
    "write@1": Reason.WRITE_FAILED,
    "set_mtime": Reason.METADATA_FAILED,
    "flush": Reason.FLUSH_FAILED,
    "query:src@1": Reason.VERIFY_FAILED,
    "read:tmp@1": Reason.VERIFY_FAILED,
    "query:tmp@2": Reason.VERIFY_FAILED,
    "streams@1": Reason.VERIFY_FAILED,
    "ea@1": Reason.VERIFY_FAILED,
}


@pytest.mark.parametrize("event", sorted(FAILURES))
def test_an_io_error_before_the_commit_fails_and_cleans(rig: Rig, event: str) -> None:
    rig.fs.at[event] = raiser(OSError(5, "scripted"))
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.FAILED, FAILURES[event])
    record = rig.last()
    assert (record["commit"], record["temp_state"]) == ("not_attempted", "removed")
    assert rig.renames() == 0 and TEMP not in rig.fs.entries and TARGET.name not in rig.fs.entries
    rig.single_closes()
    rig.report()


CHANGES: dict[str, tuple[str, Callable[[Rig], None]]] = {
    "source_mtime": ("query:src@1", lambda r: setattr(r.fs.source, "mtime_ns", 1)),
    "source_size": ("query:src@1", lambda r: r.fs.source.data.extend(b"+")),
    "temp_content": ("read:tmp@1", lambda r: r.tmp.file.data.__setitem__(0, 0xFF)),  # type: ignore[union-attr]
    "temp_longer": ("flush", lambda r: r.tmp.file.data.extend(b"+")),  # type: ignore[union-attr]
    "temp_stream": ("flush", lambda r: setattr(r.tmp.file, "extra_streams", (Stream(":x", 1),))),
    "temp_ea": ("flush", lambda r: setattr(r.tmp.file, "ea", 8)),
    "temp_mtime": ("flush", lambda r: setattr(r.tmp.file, "mtime_ns", 5)),
}


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_a_change_before_verified_fails_and_cleans(rig: Rig, change: str) -> None:
    event, action = CHANGES[change]
    rig.fs.at[event] = lambda: action(rig)
    result = rig.run()
    assert result.status is CopyStatus.FAILED
    assert result.reason in (Reason.SOURCE_CHANGED, Reason.VERIFY_FAILED)
    assert rig.renames() == 0 and rig.last()["temp_state"] == "removed"


def test_a_short_read_is_a_changed_source(rig: Rig) -> None:
    rig.fs.at["read:src@2"] = lambda: rig.fs.source.data.__delitem__(slice(1500, None))
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.FAILED, Reason.SOURCE_CHANGED)


@pytest.mark.parametrize("event", ["write@2", "flush", "streams@1"])
def test_cancelled_before_the_commit(rig: Rig, event: str) -> None:
    rig.fs.at[event] = lambda: setattr(rig, "cancel", True)
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.CANCELLED, Reason.CANCELLED)
    assert rig.renames() == 0 and rig.last()["temp_state"] == "removed"
    assert rig.last()["status"] == "cancelled" and rig.last()["commit"] == "not_attempted"
    assert "commit_attempted" not in rig.statuses()


INTERRUPTS = [
    "read:src@2",
    "write@1",
    "set_mtime",
    "flush",
    "query:src@1",
    "read:tmp@1",
    "query:tmp@2",
    "streams@1",
]


@pytest.mark.parametrize("event", INTERRUPTS)
def test_an_interrupt_before_the_commit_cleans_without_record(rig: Rig, event: str) -> None:
    boom = Boom()
    rig.fs.at[event] = raiser(boom)
    rig.interrupted(boom)
    assert rig.statuses()[-1] in PHASES and rig.renames() == 0
    assert rig.disposals() == ["posix"] and TEMP not in rig.fs.entries
    rig.single_closes()
    (state,) = boom.transfer_states  # type: ignore[attr-defined]
    assert state.temp_state is TempState.REMOVED and not state.permit


def test_an_unexpected_exception_is_an_interrupt(rig: Rig) -> None:
    bug = RuntimeError("bug")
    rig.fs.at["write@1"] = raiser(bug)
    rig.interrupted(bug)
    assert rig.statuses()[-1] == "staged" and TEMP not in rig.fs.entries


# --- T26: the journal around the commit attempt (J1..J8) --------------------------------


def test_j1_journal_error_on_commit_attempted_renames_nothing(rig: Rig) -> None:
    rig.journal.fail[S.COMMIT_ATTEMPTED] = "error"
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.CANCELLED, Reason.JOURNAL_FAILED)
    assert rig.renames() == 0 and not result.transfer.permit
    assert TEMP not in rig.fs.entries and written(rig) == PHASES


@pytest.mark.parametrize("mode", ["interrupt_before", "interrupt_after"])
def test_j2_j3_interrupt_in_commit_attempted_renames_nothing(rig: Rig, mode: str) -> None:
    rig.journal.fail[S.COMMIT_ATTEMPTED] = mode
    exc = rig.interrupted()
    assert isinstance(exc, Boom) and rig.renames() == 0
    assert rig.disposals() == ["posix"] and TEMP not in rig.fs.entries
    (state,) = exc.transfer_states  # type: ignore[attr-defined]
    assert not state.permit and not state.rename_spent


def test_j4_journal_error_on_done_is_recovery(rig: Rig) -> None:
    rig.journal.fail[S.DONE] = "error"
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.RECOVERY_REQUIRED, Reason.JOURNAL_FAILED)
    assert result.stop is Stop.JOURNAL_FAILED and result.terminal is None
    assert TARGET.name in rig.fs.entries and rig.disposals() == []
    assert result.transfer.temp_state is TempState.COMMITTED


def test_j5_journal_error_on_recovery_required(rig: Rig) -> None:
    rig.ops.rename_status = STATUS_PENDING
    rig.journal.fail[S.RECOVERY_REQUIRED] = "error"
    result = rig.run()
    assert result.status is CopyStatus.RECOVERY_REQUIRED and result.terminal is None
    assert result.stop is Stop.RUN_ABORTED and result.journal_error is not None
    assert rig.disposals() == [] and rig.tmp.close_calls == 1


def test_j6_journal_error_on_the_ended_record(rig: Rig) -> None:
    rig.fs.entries[TARGET.name] = rig.fs.new_file()
    rig.journal.fail[S.SKIPPED] = "error"
    result = rig.run()
    assert (result.status, result.terminal) == (CopyStatus.SKIPPED, None)
    assert result.stop is Stop.JOURNAL_FAILED and TEMP not in rig.fs.entries


def test_j7_interrupt_writing_done_leaves_the_commit(rig: Rig) -> None:
    rig.journal.fail[S.DONE] = "interrupt_before"
    exc = rig.interrupted()
    assert TARGET.name in rig.fs.entries and rig.disposals() == [] and rig.renames() == 1
    (state,) = exc.transfer_states  # type: ignore[attr-defined]
    assert state.temp_state is TempState.COMMITTED and state.commit is Commit.COMMITTED


def test_j8_interrupt_writing_intent(rig: Rig) -> None:
    rig.journal.fail[S.INTENT] = "interrupt_before"
    exc = rig.interrupted()
    assert isinstance(exc, Boom) and rig.fs.calls("create") == [] and rig.src.close_calls == 1


# --- §3.5.2: the rename and what C15 establishes ----------------------------------------


def test_target_exists_skips_and_cleans(rig: Rig) -> None:
    theirs = rig.fs.new_file(data=bytearray(b"theirs"))
    rig.fs.entries[TARGET.name] = theirs
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.SKIPPED, Reason.TARGET_EXISTS)
    record = rig.last()
    assert (record["commit"], record["temp_state"]) == ("not_committed", "removed")
    assert rig.fs.entries[TARGET.name] is theirs and bytes(theirs.data) == b"theirs"
    assert rig.renames() == 1 and TEMP not in rig.fs.entries
    assert result.transfer.evidence.verdict is Verdict.NOT_COMMITTED  # type: ignore[union-attr]
    rig.report()


def test_rename_error_fails_and_cleans(rig: Rig) -> None:
    rig.ops.rename_status = STATUS_ACCESS_DENIED
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.FAILED, Reason.COMMIT_FAILED)
    assert result.stop is None and rig.renames() == 1 and TEMP not in rig.fs.entries
    assert "0xc0000022" in str(rig.last()["error_message"])


def test_cross_volume_aborts_the_run(rig: Rig) -> None:
    rig.ops.rename_status = cc.STATUS_NOT_SAME_DEVICE
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.FAILED, Reason.CROSS_VOLUME)
    assert result.stop is Stop.RUN_ABORTED and rig.last()["error_code"] == 17
    assert rig.renames() == 1


@pytest.mark.parametrize("status", [STATUS_ACCESS_DENIED, STATUS_COLLISION])
def test_an_error_that_committed_is_recovery_committed(rig: Rig, status: int) -> None:
    rig.ops.rename_status, rig.ops.rename_moves = status, True
    result = rig.run()
    assert result.status is CopyStatus.RECOVERY_REQUIRED
    assert result.reason == Reason.COMMITTED_DESPITE_RENAME_ERROR
    record = rig.last()
    assert (record["commit"], record["temp_state"]) == ("committed", "committed")
    assert record["target_id"] == record["temp_id"]
    assert rig.disposals() == [] and TARGET.name in rig.fs.entries and rig.renames() == 1
    rig.report()


def test_ok_but_not_committed_is_never_cleaned(rig: Rig) -> None:
    rig.ops.rename_status, rig.ops.rename_moves = 0, False
    result = rig.run()
    assert (result.status, result.reason) == (
        CopyStatus.RECOVERY_REQUIRED,
        Reason.RENAME_OK_NOT_COMMITTED,
    )
    record = rig.last()
    assert (record["commit"], record["temp_state"]) == ("unknown", "unknown")
    assert record["target_id"] is None and result.stop is Stop.RUN_ABORTED
    assert rig.disposals() == [] and TEMP in rig.fs.entries and rig.renames() == 1


@pytest.mark.parametrize("moves", [False, True])
def test_not_established_is_recovery_and_aborts(rig: Rig, moves: bool) -> None:
    rig.ops.rename_status, rig.ops.rename_moves = STATUS_PENDING, moves
    result = rig.run()
    assert (result.status, result.reason) == (
        CopyStatus.RECOVERY_REQUIRED,
        Reason.RENAME_NOT_ESTABLISHED,
    )
    assert result.stop is Stop.RUN_ABORTED and rig.disposals() == [] and rig.renames() == 1
    assert rig.last()["commit"] == ("committed" if moves else "unknown")
    assert result.transfer.rename is Renamed.NOT_ESTABLISHED


META: dict[str, Callable[[FakeFile], None]] = {
    "stream": lambda f: setattr(f, "extra_streams", (Stream(":Zone.Identifier:$DATA", 9),)),
    "ea": lambda f: setattr(f, "ea", 12),
    "attributes": lambda f: setattr(f, "attributes", 0x80),
    "mtime": lambda f: setattr(f, "mtime_ns", MTIME + 100),
}


@pytest.mark.parametrize("change", sorted(META))
def test_committed_with_metadata_violation(rig: Rig, change: str) -> None:
    rig.ops.after_rename = META[change]
    result = rig.run()
    assert (result.status, result.reason) == (
        CopyStatus.RECOVERY_REQUIRED,
        Reason.METADATA_VIOLATION,
    )
    record = rig.last()
    assert (record["commit"], record["temp_state"]) == ("committed", "committed")
    assert record["diagnostics"]["metadata_violation"]  # type: ignore[index]
    assert result.stop is Stop.RUN_ABORTED and rig.disposals() == []


@pytest.mark.parametrize(("query", "unknown"), [("streams", "stream"), ("ea", "ea")])
def test_a_failed_post_rename_meta_query_keeps_its_reason(
    rig: Rig, query: str, unknown: str
) -> None:
    def fail() -> None:
        raise OSError(5, f"scripted: {query} query denied")

    rig.fs.at[f"{query}@2"] = fail  # @1 is C12, @2 is C15 after the rename
    result = rig.run()
    assert (result.status, result.reason) == (
        CopyStatus.RECOVERY_REQUIRED,
        Reason.METADATA_VIOLATION,
    )
    text = f"{unknown} unknown (OSError: [Errno 5] scripted: {query} query denied)"
    assert text in rig.last()["diagnostics"]["metadata_violation"]  # type: ignore[index]
    evidence = result.transfer.evidence
    assert evidence is not None and evidence.verdict is Verdict.COMMITTED
    errors = {"streams": evidence.streams_error, "ea": evidence.ea_error}
    assert errors[query] == f"OSError: [Errno 5] scripted: {query} query denied"
    assert result.stop is Stop.RUN_ABORTED and rig.disposals() == [] and rig.renames() == 1


@pytest.mark.parametrize("status", [STATUS_ACCESS_DENIED, STATUS_COLLISION])
def test_a_refused_rename_with_unknown_evidence_is_not_cleaned(rig: Rig, status: int) -> None:
    rig.ops.rename_status, rig.ops.rename_moves = status, False
    rig.ops.lookup_unknown.add(TARGET.name)
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.RECOVERY_REQUIRED, Reason.COMMIT_UNKNOWN)
    assert rig.disposals() == [] and TEMP in rig.fs.entries  # R-8: CL only on a proven NC
    assert rig.last()["commit"] == "unknown" and rig.last()["temp_state"] == "unknown"


def test_a_final_lookup_unknown_is_commit_unknown(rig: Rig) -> None:
    rig.ops.lookup_unknown.add(TARGET.name)
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.RECOVERY_REQUIRED, Reason.COMMIT_UNKNOWN)
    assert rig.last()["commit"] == "unknown" and rig.disposals() == []


def test_delete_pending_after_the_rename_is_journaled_before_the_close(rig: Rig) -> None:
    rig.ops.after_rename = lambda f: setattr(f, "delete_pending", True)
    seen: list[str] = []
    rig.fs.at["close:H_TMP"] = lambda: seen.append(rig.statuses()[-1])
    result = rig.run()
    assert (result.status, result.reason) == (CopyStatus.RECOVERY_REQUIRED, Reason.DELETE_PENDING)
    assert seen == ["recovery_required"]  # invariant 3: RR before CloseHandle
    diagnostics = rig.last()["diagnostics"]
    assert diagnostics["delete_pending"] is True  # type: ignore[index]
    assert diagnostics["close_may_complete_delete"] is True  # type: ignore[index]
    assert rig.last()["commit"] == "unknown" and result.stop is Stop.RUN_ABORTED
    assert rig.disposals() == []


@pytest.mark.parametrize("reason", ["rr", "done"])
def test_terminal_record_precedes_the_close_of_the_temporary_file(rig: Rig, reason: str) -> None:
    if reason == "rr":
        rig.ops.rename_status = STATUS_PENDING
    seen: list[str] = []
    rig.fs.at["close:H_TMP"] = lambda: seen.append(rig.statuses()[-1])
    rig.run()
    assert seen == ["recovery_required" if reason == "rr" else "done"]


# --- T62: an interrupt around the rename ------------------------------------------------


def test_t62a_interrupt_before_the_call_makes_no_call(rig: Rig) -> None:
    boom = Boom()
    rig.fs.at["rename:before"] = raiser(boom)
    rig.interrupted(boom)
    assert rig.renames() == 0  # R-5
    (state,) = boom.transfer_states  # type: ignore[attr-defined]
    assert state.permit and state.rename_spent and state.rename is None  # the flag first
    assert state.evidence.verdict is Verdict.NOT_COMMITTED  # R-11
    assert rig.disposals() == ["posix"] and TEMP not in rig.fs.entries  # R-8
    assert state.temp_state is TempState.REMOVED and written(rig) == THROUGH_CA


def test_t62b_rename_done_status_lost(rig: Rig) -> None:
    boom = Boom()
    rig.fs.at["rename:after"] = raiser(boom)
    rig.interrupted(boom)
    assert rig.renames() == 1 and rig.disposals() == []  # R-6, no CL on a commit
    assert written(rig) == THROUGH_CA  # LOST: no record
    (state,) = boom.transfer_states  # type: ignore[attr-defined]
    assert state.rename is Renamed.LOST and state.commit is Commit.COMMITTED
    assert state.temp_state is TempState.COMMITTED and TARGET.name in rig.fs.entries
    assert rig.tmp.close_calls == 1
    assert {e[0] for e in rig.after_rename()} <= {
        "query",
        "streams",
        "ea",
        "name",
        "lookup",
        "close",
    }


def test_t62c_status_lost_and_not_done_is_cleaned(rig: Rig) -> None:
    boom = Boom()
    rig.ops.rename_moves = False
    rig.ops.rename_status = STATUS_ACCESS_DENIED
    rig.fs.at["rename:after"] = raiser(boom)
    rig.interrupted(boom)
    (state,) = boom.transfer_states  # type: ignore[attr-defined]
    assert state.rename is Renamed.LOST and state.commit is Commit.NOT_COMMITTED
    assert rig.disposals() == ["posix"] and TEMP not in rig.fs.entries


def test_t62c2_status_lost_and_unknown_is_not_cleaned(rig: Rig) -> None:
    boom = Boom()
    rig.ops.lookup_unknown.add(TARGET.name)
    rig.ops.rename_moves, rig.ops.rename_status = False, STATUS_ACCESS_DENIED
    rig.fs.at["rename:after"] = raiser(boom)
    rig.interrupted(boom)
    (state,) = boom.transfer_states  # type: ignore[attr-defined]
    assert state.commit is Commit.UNKNOWN and rig.disposals() == [] and TEMP in rig.fs.entries


def test_t62d_interrupt_in_the_resolve_is_unknown(rig: Rig) -> None:
    boom = Boom()
    rig.fs.at["query:tmp@3"] = raiser(boom)  # C7a, C12, then P1
    rig.interrupted(boom)
    (state,) = boom.transfer_states  # type: ignore[attr-defined]
    assert state.evidence.interrupted and state.commit is Commit.UNKNOWN
    assert rig.disposals() == [] and rig.tmp.close_calls == 1 and rig.renames() == 1
    assert written(rig) == THROUGH_CA


def test_t62e_second_interrupt_in_the_unwind_keeps_the_first(rig: Rig) -> None:
    first, second = Boom(), Boom()
    rig.fs.at["rename:before"] = raiser(first)
    rig.fs.at["dispose:posix"] = raiser(second)
    exc = rig.interrupted()
    assert exc is first  # invariant 9
    (state,) = first.transfer_states  # type: ignore[attr-defined]
    assert state.cleanup.interrupted and state.temp_state is TempState.UNKNOWN
    assert rig.disposals() == ["posix"] and rig.tmp.close_calls == 1  # never repeated


def test_t50_a_second_rename_is_refused(rig: Rig) -> None:
    machine = cc._Copy(
        rig.journal,  # type: ignore[arg-type]
        source=SOURCE,
        target=TARGET,
        src=rig.src,  # type: ignore[arg-type]
        directory=rig.directory,  # type: ignore[arg-type]
        facts=rig.facts(),
        source_volume=SV,
        target_volume=TV,
        ops=rig.ops,  # type: ignore[arg-type]
        cancelled=lambda: False,
        block_size=1000,
        allowed=cc.DEFAULT_ALLOWED_ATTRIBUTES,
    )
    machine.tmp = rig.ops.new(ROOT / TEMP, "H_TMP")  # type: ignore[assignment]
    rig.ops.create_into(machine.tmp, rig.directory, TEMP)  # type: ignore[arg-type]
    with pytest.raises(AssertionError):
        machine._commit_once()  # no permit
    assert rig.renames() == 0
    machine.ts.permit = True
    machine._commit_once()
    machine.attempt = RenameAttempt()  # even a fresh attempt: the machine refuses itself
    with pytest.raises(AssertionError):
        machine._commit_once()
    assert rig.renames() == 1 and rig.ops.rename_entered == 1


@pytest.mark.parametrize(
    "status", [STATUS_ACCESS_DENIED, STATUS_COLLISION, STATUS_PENDING, cc.STATUS_NOT_SAME_DEVICE]
)
def test_one_rename_whatever_its_status(rig: Rig, status: int) -> None:
    rig.ops.rename_status = status
    rig.run()
    assert rig.renames() == 1
    assert all(e[0] not in ("rename", "write", "set", "flush") for e in rig.after_rename())


# --- §3.6: CL through the machine -------------------------------------------------------


def test_cl_refused_leaves_ours(rig: Rig) -> None:
    rig.ops.posix_status = STATUS_CANNOT_DELETE  # no fallback on this one
    rig.fs.at["write@1"] = raiser(OSError(5, "scripted"))
    result = rig.run()
    assert rig.disposals() == ["posix"]
    record = rig.last()
    assert record["temp_state"] == "left" and record["diagnostics"] is not None
    assert result.transfer.cleanup.outcome is TempState.LEFT  # type: ignore[union-attr]
    assert TEMP in rig.fs.entries
    rig.report()


@pytest.mark.parametrize("fallback", sorted(FALLBACK_STATUSES))
def test_cl_falls_back_to_classic_only_after_its_statuses(rig: Rig, fallback: int) -> None:
    rig.ops.posix_status = fallback
    rig.fs.at["write@1"] = raiser(OSError(5, "scripted"))
    rig.run()
    assert rig.disposals() == ["posix", "classic"]
    assert rig.last()["temp_state"] == "removed" and TEMP not in rig.fs.entries


def test_cl_close_failure_is_unknown(rig: Rig) -> None:
    rig.fs.close_mode["H_TMP"] = "error"
    rig.fs.at["write@1"] = raiser(OSError(5, "scripted"))
    result = rig.run()
    assert rig.last()["temp_state"] == "unknown"
    diagnostics = rig.last()["diagnostics"]
    assert diagnostics["close_may_complete_delete"] is True  # type: ignore[index]
    assert result.stop is Stop.CLEANUP_FAILED
    assert result.cleanup_errors == (rig.tmp.close_error,)
    assert rig.tmp.close_calls == 1


def test_cl_unsure_is_unknown_probably_removed(rig: Rig) -> None:
    rig.ops.posix_status = STATUS_PENDING
    rig.fs.at["write@1"] = raiser(OSError(5, "scripted"))
    result = rig.run()
    cleanup = result.transfer.cleanup
    assert cleanup.disposed is Disposed.UNSURE and cleanup.outcome is TempState.UNKNOWN  # type: ignore[union-attr]
    assert cleanup.hints == ("probably_removed",)  # type: ignore[union-attr]


def test_cl_interrupt_in_the_probe_skips_the_rest(rig: Rig) -> None:
    boom = Boom()
    rig.fs.at["write@1"] = raiser(OSError(5, "scripted"))
    rig.fs.at["query:tmp@2"] = raiser(boom)  # C7a, then CL-2
    rig.interrupted(boom)
    (state,) = boom.transfer_states  # type: ignore[attr-defined]
    cleanup = state.cleanup
    assert cleanup.interrupted and cleanup.pending is Probe.SKIP and cleanup.seen is Seen.SKIP
    assert cleanup.closed is Closed.CLOSED and cleanup.outcome is TempState.UNKNOWN
    assert written(rig) == ["intent", "staged"]


def test_cl_another_file_under_the_name_is_other(rig: Rig) -> None:
    def swap() -> None:  # after the close, someone else's file takes the name
        rig.fs.entries[TEMP] = rig.fs.new_file()

    rig.ops.posix_status = STATUS_CANNOT_DELETE
    rig.fs.at["write@1"] = raiser(OSError(5, "scripted"))
    rig.fs.at["close:H_TMP"] = swap
    result = rig.run()
    cleanup = result.transfer.cleanup
    assert cleanup.seen is Seen.OTHER and cleanup.outcome is TempState.UNKNOWN  # type: ignore[union-attr]
    assert "name_other_object" in cleanup.hints  # type: ignore[union-attr]


def test_cl_probe_of_another_file_is_err(rig: Rig) -> None:
    def swap() -> None:  # the handle now reports another file id
        rig.tmp.file.file_id ^= 0xFF  # type: ignore[union-attr]

    rig.fs.at["write@1"] = raiser(OSError(5, "scripted"))
    rig.fs.at["dispose:posix"] = swap
    result = rig.run()
    cleanup = result.transfer.cleanup
    assert cleanup.pending is Probe.ERR and cleanup.outcome is TempState.UNKNOWN  # type: ignore[union-attr]
    assert rig.last()["temp_state"] == "unknown"


def test_cl_is_by_handle_only(rig: Rig) -> None:
    rig.fs.at["write@1"] = raiser(OSError(5, "scripted"))
    rig.run()
    kinds = [e[0] for e in rig.fs.log]
    assert "rename" not in kinds and kinds.count("dispose") == 1


# --- T57: the CL table (640 combinations) -----------------------------------------------


def expected_cl(d: Disposed, p: Probe, c: Closed, seen: Seen, k: bool, x: bool) -> TempState:
    if not x and (d, p, c, seen) == (Disposed.OK, Probe.TRUE, Closed.CLOSED, Seen.ABSENT):
        return TempState.REMOVED
    if not x and k and (d, p, c, seen) == (Disposed.REFUSED, Probe.FALSE, Closed.CLOSED, Seen.OURS):
        return TempState.LEFT
    return TempState.UNKNOWN


COMBINATIONS = list(itertools.product(Disposed, Probe, Closed, Seen, [False, True], [False, True]))


def test_t57_every_combination() -> None:
    assert len(COMBINATIONS) == 640
    outcomes = {state: 0 for state in TempState}
    for d, p, c, seen, k, x in COMBINATIONS:
        state, hints, may = cl_outcome(d, p, c, seen, k, x)
        assert state is expected_cl(d, p, c, seen, k, x), (d, p, c, seen, k, x)
        assert may == (c is Closed.FAILED and (d is Disposed.OK or p is Probe.TRUE))
        if state is not TempState.UNKNOWN:
            assert hints == ()
        else:
            assert ("probably_left" in hints) == (seen is Seen.OURS and p is not Probe.TRUE)
            assert ("name_other_object" in hints) == (seen is Seen.OTHER)
            assert ("close_may_complete_delete" in hints) == may
            assert ("probably_removed" in hints) == (
                seen is Seen.ABSENT and p is Probe.TRUE and d in (Disposed.OK, Disposed.UNSURE)
            )
        outcomes[state] += 1
    assert outcomes[TempState.REMOVED] == 2 and outcomes[TempState.LEFT] == 1  # K free in R1


@pytest.mark.parametrize(
    ("args", "state"),
    [
        ((Disposed.OK, Probe.TRUE, Closed.CLOSED, Seen.ABSENT, True, False), TempState.REMOVED),
        ((Disposed.OK, Probe.TRUE, Closed.CLOSED, Seen.ABSENT, True, True), TempState.UNKNOWN),
        ((Disposed.OK, Probe.TRUE, Closed.FAILED, Seen.ABSENT, True, False), TempState.UNKNOWN),
        ((Disposed.UNSURE, Probe.TRUE, Closed.CLOSED, Seen.ABSENT, True, False), TempState.UNKNOWN),
        ((Disposed.OK, Probe.TRUE, Closed.CLOSED, Seen.UNK, True, False), TempState.UNKNOWN),
        ((Disposed.OK, Probe.ERR, Closed.CLOSED, Seen.ABSENT, True, False), TempState.UNKNOWN),
        ((Disposed.REFUSED, Probe.FALSE, Closed.CLOSED, Seen.OURS, True, False), TempState.LEFT),
        (
            (Disposed.REFUSED, Probe.FALSE, Closed.CLOSED, Seen.OURS, False, False),
            TempState.UNKNOWN,
        ),
        ((Disposed.REFUSED, Probe.FALSE, Closed.FAILED, Seen.OURS, True, False), TempState.UNKNOWN),
        ((Disposed.OK, Probe.FALSE, Closed.CLOSED, Seen.OURS, True, False), TempState.UNKNOWN),
        ((Disposed.NONE, Probe.FALSE, Closed.CLOSED, Seen.OURS, True, False), TempState.UNKNOWN),
        ((Disposed.REFUSED, Probe.FALSE, Closed.CLOSED, Seen.OURS, True, True), TempState.UNKNOWN),
    ],
)
def test_t57_explicit_cases(args: tuple[object, ...], state: TempState) -> None:
    assert cl_outcome(*args)[0] is state  # type: ignore[arg-type]


# --- §3.3: the verdict and P1-META ------------------------------------------------------


def info(**kw: object) -> HandleInfo:
    values: dict[str, object] = {
        "volume_serial": TV.serial,
        "file_id": 0x42,
        "attributes": 0x20,
        "reparse_tag": 0,
        "size": 10,
        "mtime_ns": MTIME,
        "links": 1,
        "delete_pending": False,
        "directory": False,
    }
    values.update(kw)
    return HandleInfo(**values)  # type: ignore[arg-type]


FOUND = Lookup(LookupKind.FOUND, 0x42)
FOUND_OTHER = Lookup(LookupKind.FOUND, 0x99)
ABSENT = Lookup(LookupKind.ABSENT)
UNK = Lookup(LookupKind.UNKNOWN, reason="x")


def verdict(p1: HandleInfo | None = None, **kw: object) -> Verdict:
    args: dict[str, object] = {
        "serial": TV.serial,
        "temp_id": 0x42,
        "name": "\\Sorted\\a.pdf",
        "final_name": "a.pdf",
        "temp_name": TEMP,
        "final": FOUND,
        "temp": ABSENT,
    }
    args.update(kw)
    return judge_commit(info() if p1 is None else p1, **args)  # type: ignore[arg-type]


def test_judge_committed_needs_all_three() -> None:
    assert verdict() is Verdict.COMMITTED
    assert verdict(temp=UNK) is Verdict.UNKNOWN
    assert verdict(temp=FOUND) is Verdict.UNKNOWN
    assert verdict(final=FOUND_OTHER) is Verdict.UNKNOWN
    assert verdict(name=f"\\Sorted\\{TEMP}") is Verdict.UNKNOWN
    assert verdict(name=None) is Verdict.UNKNOWN


def test_judge_not_committed_needs_all_three() -> None:
    kw: dict[str, object] = {"name": f"\\Sorted\\{TEMP}", "temp": FOUND}
    assert verdict(**kw, final=ABSENT) is Verdict.NOT_COMMITTED
    assert verdict(**kw, final=FOUND_OTHER) is Verdict.NOT_COMMITTED
    assert verdict(**kw, final=UNK) is Verdict.UNKNOWN
    assert verdict(**kw, final=FOUND) is Verdict.UNKNOWN
    assert verdict(name=f"\\Sorted\\{TEMP}", temp=FOUND_OTHER, final=ABSENT) is Verdict.UNKNOWN


@pytest.mark.parametrize(
    "p1",
    [
        info(file_id=0x43),
        info(volume_serial=1),
        info(delete_pending=True),
        info(links=2),
        info(directory=True),
    ],
)
def test_judge_p1_id_failure_is_unknown(p1: HandleInfo) -> None:
    assert verdict(p1) is Verdict.UNKNOWN
    assert verdict(p1, name=f"\\Sorted\\{TEMP}", temp=FOUND, final=ABSENT) is Verdict.UNKNOWN


def test_judge_without_p1_or_temp_id_is_unknown() -> None:
    assert (
        judge_commit(
            None,
            serial=TV.serial,
            temp_id=0x42,
            name="\\Sorted\\a.pdf",
            final_name="a.pdf",
            temp_name=TEMP,
            final=FOUND,
            temp=ABSENT,
        )
        is Verdict.UNKNOWN
    )
    assert verdict(temp_id=None) is Verdict.UNKNOWN


def meta(p1: HandleInfo | None = None, **kw: object) -> str | None:
    args: dict[str, object] = {
        "streams": (Stream(cc.MAIN_STREAM, 10),),
        "ea_size": 0,
        "size": 10,
        "mtime_ns": MTIME,
        "attributes": 0x20,
        "allowed": cc.DEFAULT_ALLOWED_ATTRIBUTES,
    }
    args.update(kw)
    streams, ea_size = args.pop("streams"), args.pop("ea_size")
    return meta_violation(info() if p1 is None else p1, streams, ea_size, **args)  # type: ignore[arg-type]


def test_meta_holds_only_for_what_was_verified() -> None:
    assert meta() is None
    assert meta(info(attributes=0x80)) is not None  # allowed, but not what C12 saw
    assert meta(info(attributes=0x21), attributes=0x21) is not None  # outside the allowlist
    assert meta(info(mtime_ns=MTIME + 100)) is not None
    assert meta(info(size=11)) is not None
    assert meta(streams=None) is not None
    assert meta(streams=()) is not None
    assert meta(streams=(Stream(cc.MAIN_STREAM, 10), Stream(":x:$DATA", 1))) is not None
    assert meta(ea_size=None) is not None and meta(ea_size=4) is not None
    assert meta(attributes=None) is not None


# --- the entry point --------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"block_size": 0},
        {"block_size": True},
        {"allowed_attributes": 0x21},
    ],
)
def test_copy_item_refuses_bad_arguments(rig: Rig, kw: dict[str, object]) -> None:
    with pytest.raises(AssertionError):
        rig.run(**kw)
    assert written(rig) == []


def test_copy_item_refuses_a_target_outside_the_directory(rig: Rig) -> None:
    with pytest.raises(AssertionError):
        copy_item(
            rig.journal,  # type: ignore[arg-type]
            source=SOURCE,
            target=ROOT / "sub" / "a.pdf",
            src=rig.src,  # type: ignore[arg-type]
            directory=rig.directory,  # type: ignore[arg-type]
            facts=rig.facts(),
            source_volume=SV,
            target_volume=TV,
            ops=rig.ops,  # type: ignore[arg-type]
        )


def test_a_source_close_failure_is_reported_not_hidden(rig: Rig) -> None:
    rig.fs.close_mode["H_SRC"] = "error"
    result = rig.run()
    assert result.status is CopyStatus.COPIED and result.stop is Stop.CLEANUP_FAILED
    assert result.cleanup_errors == (rig.src.close_error,)


def test_the_source_is_only_read(rig: Rig) -> None:
    rig.run()
    assert bytes(rig.fs.source.data) == DATA and rig.fs.source.mtime_ns == MTIME
    assert all(e[1] != "src" for e in rig.fs.log if e[0] in ("write", "set"))


def test_the_interrupt_carries_the_release_failures(rig: Rig) -> None:
    boom = Boom()
    rig.fs.close_mode["H_TMP"] = "error"
    rig.fs.at["write@1"] = raiser(boom)
    rig.interrupted(boom)
    assert boom.release_errors == (rig.tmp.close_error,)  # type: ignore[attr-defined]
    assert any("copy" in note for note in boom.__notes__)
