"""``runner.run_plan`` with the real ``copy_item`` and the real primitives on the NTFS volume
of ``tmp_path`` (R3B-5). Source and destination are both under ``tmp_path``: one volume,
proven ``CROSS`` by a faked destination proof as in ``test_runner_fake``. So this proves
the integration (journal v3, handles, the copy, the reactions), not a copy between two
real volumes. Those are the ``test_real_second_volume_*`` tests, opt-in, NOT_TESTED without them.

A spy on ``NtSetInformationFile`` records the information classes set: 4 (basic: the
mtime), 10 (rename), 13 / 64 (disposition).
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO

import pytest
import test_runner_fake
from test_executor import TABLE, fingerprints, touch
from test_runner_fake import Journals, Permit, cross_plan
from test_volumeproof import EXTS

from downloads_organizer import aclcreate, aclproof, features
from downloads_organizer import journal as journal_v1
from downloads_organizer import winfileops as w
from downloads_organizer import winprims as p
from downloads_organizer.copycommit import CopyStatus, Reason, WinOps
from downloads_organizer.executor import ExecReason
from downloads_organizer.journal import OperationState
from downloads_organizer.journal_v2 import Commit, JournalReportV2, TempState
from downloads_organizer.journal_v3 import (
    JournalReportV3,
    JournalV3,
    analyze_v3,
    read_any_journal,
)
from downloads_organizer.models import Plan, VolumeMode
from downloads_organizer.runner import CopyOptions, CopyRunResult, run_plan
from downloads_organizer.volumeproof import preview_plan

pytestmark = [pytest.mark.windows_only, pytest.mark.real_ntfs]
journals = test_runner_fake.journals  # the fixture

BASIC, RENAME, DISPOSITION, DISPOSITION_EX = 4, 10, 13, 64
STATUS_ACCESS_DENIED = 0xC000_0022
STATUS_PENDING = 0x0000_0103
SECOND_VOLUME = "DOWNLOADSORGANIZER_SECOND_VOLUME"
SECOND_VOLUME_DIRECTORY = r"D:\FOR_TEST"  # the owner's, exactly
KEPT_NAME = r"DownloadsOrganizer-test-[0-9a-f]{32}"  # what ``Pinned.make_here`` makes there


@pytest.fixture
def src(tmp_path: Path) -> Path:
    path = tmp_path / "area" / "Downloads"
    path.mkdir(parents=True)
    return path


class Spy:
    def __init__(self) -> None:
        self.classes: list[int] = []
        self.after: dict[int, Callable[[], None]] = {}


@pytest.fixture
def spy() -> Iterator[Spy]:
    seen = Spy()
    ntdll = p._prims().ntdll
    real = ntdll.NtSetInformationFile

    def call(*args: object) -> int:
        info_class = int(getattr(args[4], "value", args[4]))  # type: ignore[call-overload]
        seen.classes.append(info_class)
        status = real(*args)
        action = seen.after.get(info_class)
        if action is not None:
            action()
        return status

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ntdll, "NtSetInformationFile", call)
        yield seen


def run(plan: Plan, **options: object) -> CopyRunResult:
    outcome = run_plan(plan, TABLE, copy=CopyOptions(acl=Permit(), **options))  # type: ignore[arg-type]
    assert isinstance(outcome, CopyRunResult), outcome
    return outcome


def by_name(outcome: CopyRunResult) -> dict[str, object]:
    return {r.item.source.name: r for r in outcome.items}


def target_of(plan: Plan, name: str) -> Path:
    (item,) = (i for i in plan.moves if i.source.name == name)
    return item.target


def partials(plan: Plan) -> list[Path]:
    root = plan.destination_root
    assert root is not None
    return sorted(root.rglob("*.partial")) if root.exists() else []


def report_of(outcome: CopyRunResult) -> JournalReportV2:
    assert outcome.journal_path is not None
    report = read_any_journal(outcome.journal_path)
    assert isinstance(report, JournalReportV2)
    return report


def copy_statuses(outcome: CopyRunResult, journal: JournalV3) -> list[tuple[str, str]]:
    return [
        (Path(str(r["source"])).name, str(r["status"]))
        for r in journal.history
        if r["operation"] == "copy"
    ]


# --- the copy -------------------------------------------------------------------------


def test_ntfs_copy_run_copies_and_keeps_every_source(
    src: Path, monkeypatch: pytest.MonkeyPatch, spy: Spy, journals: Journals
) -> None:
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    plan = cross_plan(monkeypatch, src)
    outcome = run(plan, block_size=7)
    assert outcome.complete and outcome.stopped is None, outcome.problems
    assert fingerprints(src) == before  # the sources: same files, same content
    for name in ("a.pdf", "b.jpg"):
        target = target_of(plan, name)
        assert target.read_bytes() == (src / name).read_bytes()
        assert target.stat().st_mtime_ns == (src / name).stat().st_mtime_ns
        assert target.stat().st_ino != (src / name).stat().st_ino  # a copy, not a move
    assert spy.classes.count(RENAME) == 2 and DISPOSITION not in spy.classes
    assert DISPOSITION_EX not in spy.classes and partials(plan) == []
    (journal,) = journals.v2
    assert journals.v1 == []
    assert analyze_v3(journal.history, run_id=journal.run_id).problems == ()
    assert report_of(outcome).problems == ()
    phases = [s for n, s in copy_statuses(outcome, journal) if n == "a.pdf"]
    assert phases == ["intent", "staged", "written", "verified", "commit_attempted", "done"]
    assert all(r.transfer is not None for r in outcome.items)
    assert all(r.transfer.commit is Commit.COMMITTED for r in outcome.items)  # type: ignore[union-attr]


def test_ntfs_target_exists_is_skipped_and_the_run_goes_on(
    src: Path, monkeypatch: pytest.MonkeyPatch, spy: Spy
) -> None:
    touch(src, "a.pdf", "b.jpg")
    target_of(cross_plan(monkeypatch, src), "a.pdf").parent.mkdir(parents=True)
    plan = cross_plan(monkeypatch, src)  # the folders exist before this preview: fresh
    taken = target_of(plan, "a.pdf")
    taken.write_bytes(b"theirs")  # after the preview
    outcome = run(plan)
    a, b = by_name(outcome)["a.pdf"], by_name(outcome)["b.jpg"]
    assert (a.status, a.reason) == (CopyStatus.SKIPPED, Reason.TARGET_EXISTS)  # type: ignore[attr-defined]
    assert a.transfer.temp_state is TempState.REMOVED  # type: ignore[attr-defined]
    assert b.status is CopyStatus.COPIED and outcome.stopped is None  # type: ignore[attr-defined]
    assert taken.read_bytes() == b"theirs" and (src / "a.pdf").exists()
    assert spy.classes.count(RENAME) == 2 and partials(plan) == []
    assert report_of(outcome).problems == ()


class RenameStatus(WinOps):
    """The rename returns ``status`` without renaming anything (scripted)."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.renames = 0

    def rename_once(
        self, handle: w.Handle, directory: w.Handle, name: str, attempt: p.RenameAttempt
    ) -> p.Renamed:
        self.renames += 1
        attempt.call = p._Call((), w.signed32(self.status))
        attempt.spent = True
        result = attempt.result
        assert result is not None
        return result


def test_ntfs_rename_error_is_failed_cleaned_and_the_run_goes_on(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    ops = RenameStatus(STATUS_ACCESS_DENIED)
    outcome = run(plan, ops=ops)
    assert ops.renames == 2  # one per file, never a second one for the same file
    for result in outcome.items:
        assert result.status is CopyStatus.FAILED and result.transfer is not None
        assert result.transfer.commit is Commit.NOT_COMMITTED
        assert result.transfer.temp_state is TempState.REMOVED
        assert not result.item.target.exists()
    assert outcome.stopped is None and partials(plan) == []
    assert report_of(outcome).problems == ()


def test_ntfs_rename_not_established_requires_recovery_and_stops(
    src: Path, monkeypatch: pytest.MonkeyPatch, spy: Spy
) -> None:
    """A scripted ``STATUS_PENDING`` (that the real call never returned one here proves
    nothing about it): recovery required, nothing cleaned, no next file."""
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    plan = cross_plan(monkeypatch, src)
    ops = RenameStatus(STATUS_PENDING)
    outcome = run(plan, ops=ops)
    assert ops.renames == 1 and outcome.stopped is ExecReason.RUN_ABORTED
    first = next(r for r in outcome.items if r.transfer is not None)
    second = next(r for r in outcome.items if r is not first)
    assert first.status is CopyStatus.RECOVERY_REQUIRED
    assert first.reason == Reason.RENAME_NOT_ESTABLISHED
    assert (second.status, second.reason) == (CopyStatus.CANCELLED, ExecReason.RUN_ABORTED)
    assert len(partials(plan)) == 1 and not second.item.target.exists()  # left for a person
    assert DISPOSITION_EX not in spy.classes and DISPOSITION not in spy.classes
    assert fingerprints(src) == before
    assert any("check by hand" in problem for problem in outcome.problems)
    report = report_of(outcome)
    assert report.problems == () and report.recovery_needed


def test_ntfs_rename_status_lost_raises_and_starts_nothing_more(
    src: Path, monkeypatch: pytest.MonkeyPatch, spy: Spy, journals: Journals
) -> None:
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    plan = cross_plan(monkeypatch, src)

    def interrupt() -> None:
        raise KeyboardInterrupt

    spy.after[RENAME] = interrupt
    with pytest.raises(KeyboardInterrupt) as raised:
        run(plan)
    (state,) = raised.value.transfer_states  # type: ignore[attr-defined]
    assert state.rename is p.Renamed.LOST and state.commit is Commit.COMMITTED
    assert spy.classes.count(RENAME) == 1  # never a second rename
    assert state.target.exists() and fingerprints(src) == before
    others = [i.target for i in plan.moves if i.target != state.target]
    assert others and not any(t.exists() for t in others)
    (journal,) = journals.v2
    assert journal.state == "closed"
    assert journal.history[-1]["status"] == "commit_attempted"  # its outcome: by hand
    assert any("every source was kept" in note for note in raised.value.__notes__)


class FailOn:
    """A journal stream whose write of a record with ``marker`` fails (scripted)."""

    def __init__(self, stream: BinaryIO, marker: bytes) -> None:
        self.stream, self.marker = stream, marker

    def write(self, data: bytes) -> int:
        if self.marker in data:
            raise OSError(28, "scripted: no space left for the journal")
        return self.stream.write(data)

    def __getattr__(self, name: str) -> object:
        return getattr(self.stream, name)


def test_ntfs_journal_failure_before_commit_attempted_renames_nothing(
    src: Path, monkeypatch: pytest.MonkeyPatch, spy: Spy, tmp_path: Path
) -> None:
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    plan = cross_plan(monkeypatch, src)
    path = tmp_path / "journal.jsonl"
    stream = FailOn(path.open("w+b"), b'"status":"verified"')
    made: list[JournalV3] = []

    def journal() -> JournalV3:
        made.append(log := JournalV3(stream, path))  # type: ignore[arg-type]
        return log

    outcome = run_plan(plan, TABLE, journal_v3=journal, copy=CopyOptions(acl=Permit()))
    assert isinstance(outcome, CopyRunResult)
    assert RENAME not in spy.classes and outcome.stopped is ExecReason.JOURNAL_FAILED
    assert not any(i.target.exists() for i in plan.moves) and fingerprints(src) == before
    first = next(r for r in outcome.items if r.transfer is not None)
    assert first.transfer.commit is Commit.NOT_ATTEMPTED  # type: ignore[union-attr]
    assert all(r.transfer is None for r in outcome.items if r is not first)
    (log,) = made
    assert log.failed or log.state == "closed"
    assert [r["status"] for r in log.history][-1] == "written"


def test_ntfs_a_file_the_gate_refuses_gets_no_temporary_file(
    src: Path, monkeypatch: pytest.MonkeyPatch, spy: Spy
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    gate = Permit(refuse=frozenset({"a.pdf", "b.jpg"}))
    outcome = run_plan(plan, TABLE, copy=CopyOptions(acl=gate))
    assert isinstance(outcome, CopyRunResult)
    assert all(r.status is CopyStatus.SKIPPED and r.transfer is None for r in outcome.items)
    assert spy.classes == [] and partials(plan) == []
    assert not any(i.target.parent.exists() for i in plan.moves)
    assert report_of(outcome).problems == ()


# --- what a copy of the unnamed stream would lose (D2 / D5 undecided: refused) -------


class _StreamData(ctypes.Structure):
    _fields_ = (("size", ctypes.c_int64), ("name", ctypes.c_wchar * 296))


def streams_of(path: Path) -> tuple[p.Stream, ...]:
    """The data streams of ``path`` as ``FindFirstStreamW`` lists them, independently of
    the program's ``query_streams``."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.FindFirstStreamW.restype = ctypes.c_void_p
    kernel32.FindFirstStreamW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
    )
    kernel32.FindNextStreamW.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    kernel32.FindClose.argtypes = (ctypes.c_void_p,)
    data = _StreamData()
    find = kernel32.FindFirstStreamW(str(path), 0, ctypes.byref(data), 0)
    if find in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        found = [p.Stream(data.name, data.size)]
        while kernel32.FindNextStreamW(find, ctypes.byref(data)):
            found.append(p.Stream(data.name, data.size))
    finally:
        kernel32.FindClose(find)
    return tuple(found)


def assert_only_b_copied(outcome: CopyRunResult, plan: Plan, reason: str) -> None:
    a, b = by_name(outcome)["a.pdf"], by_name(outcome)["b.jpg"]
    assert (a.status, a.reason, a.transfer) == (CopyStatus.SKIPPED, reason, None)  # type: ignore[attr-defined]
    assert not target_of(plan, "a.pdf").exists() and partials(plan) == []
    assert b.status is CopyStatus.COPIED and outcome.stopped is None  # type: ignore[attr-defined]
    assert report_of(outcome).problems == ()


@pytest.mark.parametrize(
    ("stream", "content"),
    [
        ("Zone.Identifier", "[ZoneTransfer]\r\nZoneId=3\r\n"),  # the mark of the web
        ("notes", "a stream of the user"),
    ],
)
def test_ntfs_a_source_with_another_data_stream_is_refused(
    src: Path, monkeypatch: pytest.MonkeyPatch, spy: Spy, stream: str, content: str
) -> None:
    """Before D2: a refusal, never a copy of the unnamed stream alone (B3)."""
    touch(src, "a.pdf", "b.jpg")
    with Path(f"{src / 'a.pdf'}:{stream}").open("w", newline="") as marked:
        marked.write(content)
    before, streams = fingerprints(src), streams_of(src / "a.pdf")
    assert len(streams) == 2  # the unnamed one and this one
    plan = cross_plan(monkeypatch, src)
    outcome = run(plan)
    assert_only_b_copied(outcome, plan, "source_streams")
    detail = by_name(outcome)["a.pdf"].detail  # type: ignore[attr-defined]
    assert f":{stream}:$DATA ({len(content)} bytes)" in detail
    assert spy.classes.count(RENAME) == 1  # b.jpg only
    assert fingerprints(src) == before and streams_of(src / "a.pdf") == streams
    assert Path(f"{src / 'a.pdf'}:{stream}").read_text(newline="") == content
    assert streams_of(target_of(plan, "b.jpg")) == streams_of(src / "b.jpg")  # one, unnamed


def test_ntfs_a_read_only_source_is_refused(
    src: Path, monkeypatch: pytest.MonkeyPatch, spy: Spy
) -> None:
    """READONLY is outside the allowlist (D5 undecided): refused, not copied without it."""
    touch(src, "a.pdf", "b.jpg")
    (src / "a.pdf").chmod(0o444)
    try:
        before = fingerprints(src)
        plan = cross_plan(monkeypatch, src)
        outcome = run(plan)
        assert_only_b_copied(outcome, plan, "source_attributes")
        assert "0x1 not allowed" in by_name(outcome)["a.pdf"].detail  # type: ignore[attr-defined]
        assert spy.classes.count(RENAME) == 1 and fingerprints(src) == before
        assert not os.access(src / "a.pdf", os.W_OK)  # still read-only
    finally:
        (src / "a.pdf").chmod(0o666)


class Scripted(WinOps):
    """The real operations, but the queries of ``a.pdf`` answer as scripted."""

    def __init__(self, ea_size: int = 0, error: OSError | None = None) -> None:
        self.ea_size, self.error = ea_size, error

    def query_streams(self, handle: w.Handle) -> tuple[p.Stream, ...]:
        if self.error is not None and handle.path.name == "a.pdf":
            raise self.error
        return super().query_streams(handle)

    def query_ea_size(self, handle: w.Handle) -> int:
        if handle.path.name == "a.pdf":
            return self.ea_size
        return super().query_ea_size(handle)


def test_ntfs_a_source_with_extended_attributes_is_refused(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scripted: setting EAs needs NtSetEaFile, which the program never calls."""
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    outcome = run(plan, ops=Scripted(ea_size=7))
    assert_only_b_copied(outcome, plan, "source_ea")


def test_ntfs_a_failed_stream_query_fails_only_that_file(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    plan = cross_plan(monkeypatch, src)
    outcome = run(plan, ops=Scripted(error=PermissionError(13, "scripted: denied")))
    a, b = by_name(outcome)["a.pdf"], by_name(outcome)["b.jpg"]
    assert (a.status, a.reason, a.transfer) == (  # type: ignore[attr-defined]
        CopyStatus.FAILED,
        ExecReason.SOURCE_CHECK_FAILED,
        None,
    )
    assert b.status is CopyStatus.COPIED and outcome.stopped is None  # type: ignore[attr-defined]
    assert not target_of(plan, "a.pdf").exists() and partials(plan) == []
    assert fingerprints(src) == before


# --- opt-in: two real volumes ---------------------------------------------------------
#
# ``DOWNLOADSORGANIZER_SECOND_VOLUME`` names ``SECOND_VOLUME_DIRECTORY`` (no other), an
# existing, disposable directory on another local NTFS volume than ``tmp_path``, chosen by
# the owner. Each test makes its own new directory in it, by handles (``Pinned``), and
# removes nothing: every such directory is kept, with its path in the test's report
# sections and ``user_properties``. Nothing is faked about the volumes:
# no ``cross_plan``, no patched proof; the volumes are read again here, independently of
# the program, and a test is skipped (NOT_TESTED, never passed) unless they are two
# different NTFS volumes. Only
# ``features.CROSS_VOLUME_COPY`` is switched on for the test and the gate is ``Permit``,
# reachable from no CLI or GUI path.


def volume_facts(path: Path) -> tuple[str, int, str]:
    """``(device, serial, filesystem)`` of the volume of ``path``, read with
    ``QueryDosDeviceW`` and ``GetVolumeInformationW``, not with the program's code."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    device = ctypes.create_unicode_buffer(1024)
    if not kernel32.QueryDosDeviceW(path.drive, device, len(device)):
        raise ctypes.WinError(ctypes.get_last_error())
    serial, filesystem = ctypes.c_ulong(), ctypes.create_unicode_buffer(64)
    ok = kernel32.GetVolumeInformationW(
        path.anchor, None, 0, ctypes.byref(serial), None, None, filesystem, len(filesystem)
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return device.value, serial.value, filesystem.value


class FileIdInfo(ctypes.Structure):
    _fields_ = (("VolumeSerialNumber", ctypes.c_uint64), ("FileId", ctypes.c_ubyte * 16))


def volume_serial_64(path: Path) -> int:
    """The full 64-bit ``FILE_ID_INFO.VolumeSerialNumber`` of a handle on the directory
    ``path``, read with ``GetFileInformationByHandleEx(FileIdInfo)``, not with the program's
    code: the program proves this serial, ``GetVolumeInformationW`` gives its low 32 bits."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # its own function objects
    handle_t, dword = ctypes.c_void_p, ctypes.c_ulong
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        dword,
        dword,
        ctypes.c_void_p,
        dword,
        dword,
        handle_t,
    ]
    kernel32.CreateFileW.restype = handle_t
    kernel32.GetFileInformationByHandleEx.argtypes = [
        handle_t,
        ctypes.c_int,
        ctypes.c_void_p,
        dword,
    ]
    kernel32.GetFileInformationByHandleEx.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [handle_t]
    read_attributes, share_all, open_existing = 0x80, 0x7, 3
    backup_semantics, open_reparse_point, file_id_info = 0x0200_0000, 0x0020_0000, 18
    handle = kernel32.CreateFileW(
        str(path),
        read_attributes,
        share_all,
        None,
        open_existing,
        backup_semantics | open_reparse_point,
        None,
    )
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = FileIdInfo()
        if not kernel32.GetFileInformationByHandleEx(
            handle, file_id_info, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)
    return int(info.VolumeSerialNumber)


HELD_SHARE = w.FILE_SHARE_READ | w.FILE_SHARE_WRITE  # never FILE_SHARE_DELETE
DIR_ACCESS = w.FILE_LIST_DIRECTORY | w.FILE_READ_ATTRIBUTES  # data access: share enforced
FILE_ACCESS = w.FILE_GENERIC_WRITE | w.FILE_READ_ATTRIBUTES


class Refused(Exception):
    """The base directory is not the one it must be: nothing was created."""


def key(path: Path) -> str:
    return os.path.normcase(str(path))


class Pinned:
    """``SECOND_VOLUME_DIRECTORY`` and this test's directories below it, opened by handles
    from the proven root of its volume (``_DirectoryChain`` and ``_open_volume_root`` of
    the journal, the proof the executor uses too), each relative to its parent's handle,
    a final link opened as itself and refused, never by a path. Each is held without
    ``FILE_SHARE_DELETE`` until the test ends: none can be renamed, removed or swapped
    for a junction meanwhile, so what the test creates lands where it is checked to be
    and nowhere else. Nothing is ever removed, only the handles are closed."""

    here: Path  # this test's own new directory

    def __init__(self) -> None:
        self.chain = journal_v1._DirectoryChain()
        self.held: dict[str, tuple[w.Handle, str]] = {}  # key -> handle, its NT path
        self.volume = 0

    def pin_base(self, base: Path) -> None:
        """Open ``base`` and each ancestor, all existing; ``Refused`` unless the root is
        the proven root of a local NTFS disk and each a plain directory on it."""
        try:
            journal_v1._open_volume_root(self.chain, base)
            self.volume = w.query(self.chain.last).volume_serial
            self.held[key(Path(base.anchor))] = self.chain.last, self.chain.device + "\\"
            for count in range(2, len(base.parts) + 1):
                self.open(Path(*base.parts[:count]), create=False)
            w.check_mapping(w.VolumeRoot(self.chain.drive, self.chain.device))
        except (OSError, w.UnverifiedRootError) as exc:
            raise Refused(f"{type(exc).__name__}: {exc}") from exc

    def make_here(self, base: Path) -> Path:
        self.here = base / f"DownloadsOrganizer-test-{uuid.uuid4().hex}"
        self.open(self.here, create=True)  # FileExistsError if it exists: never reused
        return self.here

    def open(self, path: Path, *, create: bool) -> None:
        """Open, or with ``create`` create (``FILE_CREATE``: an existing name in any form
        is ``FileExistsError``, never used), the directory ``path`` relative to the held
        handle of its parent; check it through its own handle and hold it."""
        parent, parent_nt = self.held[key(path.parent)]  # only below a held directory
        handle, created = w.open_relative(
            parent, path.name, directory=True, access=DIR_ACCESS, share=HELD_SHARE, create=create
        )
        self.chain.push(handle)  # held, and closed with the chain, from here on
        nt = parent_nt.rstrip("\\") + "\\" + path.name
        problem = self.problem(handle, nt, directory=True)
        if problem is None and created is not create:
            problem = f"created: {created}"
        if problem is not None and create:
            raise AssertionError(f"{path} was created but is not as expected (kept): {problem}")
        if problem is not None:
            raise Refused(f"{path}: {problem}")
        self.held[key(path)] = handle, nt

    def problem(self, handle: w.Handle, nt: str, *, directory: bool) -> str | None:
        info = w.query(handle)
        if info.reparse_point:
            return f"a link or junction (tag {info.reparse_tag:#x})"
        if info.directory is not directory:
            return "a directory" if info.directory else "not a directory"
        if info.volume_serial != self.volume:
            return f"on volume {info.volume_serial:#x}, not {self.volume:#x}"
        if not directory and (info.links, info.size) != (1, 0):
            return f"not a new file ({info.links} links, {info.size} bytes)"
        final = w.final_nt_path(handle)
        if final.upper() != nt.upper():
            return f"opened {final!r}, not {nt!r}"
        return None

    def mkdirs(self, path: Path) -> None:
        """``path.mkdir(parents=True)`` below ``here``, by handles: each new directory as
        ``open(..., create=True)``."""
        current = self.here
        for part in path.relative_to(self.here).parts:  # ValueError if not below it
            current = current / part
            if key(current) not in self.held:
                self.open(current, create=True)

    def make_category(self, path: Path, source_dir: Path) -> None:
        """The category ``path`` (its parent held) made as the runner makes one:
        ``aclcreate.create_category``, one ``FILE_CREATE`` relative to the parent's handle
        with the protected DACL derived from ``source_dir`` given to that very call, never
        an inherited one (a plain mkdir on D: inherits the root's generic ACEs, which the
        copy refuses: ``unknown_rights``). Its security is checked as the copy checks it
        (``category_not_wider``), then it is held like the others. The creating handle
        (with ``DELETE``) is closed first, or the runner could not open the category; the
        one held then is proven the directory created (its file id)."""
        parent, _ = self.held[key(path.parent)]  # only below a held directory
        ops, creator = aclcreate.NtOps(), aclproof.creator_sid()
        source_sd = aclproof.Descriptor.parse(aclproof.read_descriptor_at(source_dir))
        expected = aclcreate.Identity.of(w.query(parent))
        creation = aclcreate.create_category(ops, parent, expected, path.name, source_sd, creator)
        assert creation.state is aclcreate.Created.CREATED, creation.describe()
        assert creation.handle is not None and creation.identity is not None
        try:
            made = aclproof.Descriptor.parse(ops.read_descriptor(creation.handle))
            assert made.protected and aclproof.supported(made, str(path)) is None
            assert aclcreate.category_not_wider(ops, creation.handle, source_sd, creator) == (
                None,
                None,
            )
        finally:
            ops.close(creation.handle)  # only the handle: the directory is kept
        self.open(path, create=False)
        assert aclcreate.Identity.of(w.query(self.held[key(path)][0])) == creation.identity

    def create_new(self, path: Path, data: bytes) -> None:
        """A new file ``path`` with ``data`` in a held directory, created relative to its
        handle with ``FILE_CREATE``: an existing name in any form (a file, a directory, a
        link) is ``FileExistsError``, never opened, followed or changed."""
        parent, parent_nt = self.held[key(path.parent)]  # only in a held directory
        with w.create_file(parent, path.name, access=FILE_ACCESS, share=w.FILE_SHARE_READ) as h:
            problem = self.problem(h, parent_nt.rstrip("\\") + "\\" + path.name, directory=False)
            assert problem is None, f"{path} was created but is not as expected (kept): {problem}"
            stream = w.open_stream(h)  # owns the handle from here on
        with stream:
            stream.write(data)

    def close(self) -> None:
        """Close the handles, innermost first; nothing on the volume is touched."""
        self.held.clear()
        self.chain.close()


@pytest.fixture
def second(
    src: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[Pinned]:
    base = os.environ.get(SECOND_VOLUME)
    if not base:
        pytest.skip(f"NOT_TESTED: no second volume ({SECOND_VOLUME} not set)")
    if os.path.normcase(base) != os.path.normcase(SECOND_VOLUME_DIRECTORY):
        pytest.skip(f"NOT_TESTED: {SECOND_VOLUME}={base!r} is not {SECOND_VOLUME_DIRECTORY}")
    pinned = Pinned()
    try:
        try:
            pinned.pin_base(Path(base))
        except Refused as exc:
            pytest.skip(f"NOT_TESTED: {SECOND_VOLUME}={base!r} is refused: {exc}")
        mine, theirs = volume_facts(src), volume_facts(Path(base))
        if mine[0] == theirs[0] or mine[1] == theirs[1] or "NTFS" not in (mine[2], theirs[2]):
            pytest.skip(f"NOT_TESTED: not two different volumes: {mine} and {theirs}")
        if mine[2] != "NTFS" or theirs[2] != "NTFS":
            pytest.skip(f"NOT_TESTED: not two NTFS volumes: {mine} and {theirs}")
        assert src.stat().st_dev != pinned.volume
        # What earlier runs kept there is never required gone, opened or reused: only
        # their kind is checked (this module's directories), and at the end they are all
        # still there, next to this test's own new one and nothing else.
        others = sorted(p.name for p in Path(base).iterdir())
        unknown = [n for n in others if not re.fullmatch(KEPT_NAME, n)]
        if unknown:
            pytest.skip(f"NOT_TESTED: {base} has entries not made by these tests: {unknown}")
        here = pinned.make_here(Path(base))
        # Nothing on the second volume is removed, after a pass or not: what is left (a
        # final file, a ``.partial`` kept for recovery) is for a person, at this path.
        kept = f"KEPT on the second volume, not removed: {here}"
        request.node.user_properties.append(("kept", str(here)))
        request.node.add_report_section("setup", "kept", kept)
        monkeypatch.setattr(features, "CROSS_VOLUME_COPY", True)
        yield pinned
        request.node.add_report_section("teardown", "kept", kept)
        assert sorted(p.name for p in Path(base).iterdir()) == sorted([*others, here.name])
    finally:
        pinned.close()


def real_plan(src: Path, here: Path) -> Plan:
    """A preview of the real volumes, checked against them as read here."""
    plan = preview_plan(src, here / "Sorted", TABLE, EXTS, cross_copy=True).plan
    assert plan.ok and plan.cross_copy, plan.errors
    proof = plan.volume
    assert proof is not None and proof.mode is VolumeMode.CROSS
    assert proof.source is not None and proof.destination is not None
    for proven, path in ((proof.source, src), (proof.destination, here)):
        device, serial_32, filesystem = volume_facts(path)
        serial_64 = volume_serial_64(path)
        # the two independent readings are one serial; if not, stop here and show both
        assert serial_64 & 0xFFFF_FFFF == serial_32, (path, hex(serial_64), hex(serial_32))
        assert serial_64 == path.stat().st_dev, (path, hex(serial_64), hex(path.stat().st_dev))
        assert (proven.device, proven.volume_serial, proven.filesystem) == (
            device,
            serial_64,
            filesystem,
        )
        assert filesystem == "NTFS"
    assert proof.source.device != proof.destination.device
    assert proof.source.volume_serial != proof.destination.volume_serial
    return plan


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_real_second_volume_copies_two_files_and_keeps_both_sources(
    src: Path, second: Pinned, spy: Spy, journals: Journals
) -> None:
    touch(src, "a.pdf", "b.jpg")
    (src / "b.jpg").write_bytes(os.urandom(300_000))  # more than one block
    before = fingerprints(src)
    plan = real_plan(src, second.here)
    outcome = run(plan, block_size=65_536)
    assert outcome.complete and outcome.stopped is None, outcome.problems
    assert fingerprints(src) == before
    for name in ("a.pdf", "b.jpg"):
        source, target = src / name, target_of(plan, name)
        assert sha256(target) == sha256(source)
        assert target.stat().st_size == source.stat().st_size
        assert target.stat().st_mtime_ns == source.stat().st_mtime_ns
        assert target.stat().st_dev != source.stat().st_dev  # on the other volume
        assert streams_of(target) == streams_of(source)
    assert spy.classes.count(RENAME) == 2 and partials(plan) == []
    assert DISPOSITION not in spy.classes and DISPOSITION_EX not in spy.classes
    (journal,) = journals.v2
    assert journals.v1 == []
    assert analyze_v3(journal.history, run_id=journal.run_id).problems == ()
    assert report_of(outcome).problems == ()
    assert all(r.transfer.commit is Commit.COMMITTED for r in outcome.items)  # type: ignore[union-attr]


def test_real_second_volume_never_overwrites_a_final_name(
    src: Path, second: Pinned, spy: Spy, journals: Journals
) -> None:
    """``a.pdf`` taken in its category on D: before the run: SKIPPED ``target_exists``
    (the rename never replaces), the other file unchanged, ``b.jpg`` copied. The category
    is made with its security as the runner makes it (``Pinned.make_category``), so the
    copy reaches the rename instead of refusing the category first."""
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    category = target_of(real_plan(src, second.here), "a.pdf").parent
    second.mkdirs(category.parent)
    second.make_category(category, src)
    plan = real_plan(src, second.here)
    taken = target_of(plan, "a.pdf")
    assert taken.parent == category
    second.create_new(taken, b"theirs")  # FILE_CREATE, in the held category
    outcome = run(plan)
    a, b = by_name(outcome)["a.pdf"], by_name(outcome)["b.jpg"]
    assert (a.status, a.reason) == (CopyStatus.SKIPPED, Reason.TARGET_EXISTS)  # type: ignore[attr-defined]
    assert a.transfer.commit is Commit.NOT_COMMITTED  # type: ignore[attr-defined]
    assert a.transfer.temp_state is TempState.REMOVED  # type: ignore[attr-defined]
    assert b.status is CopyStatus.COPIED and outcome.stopped is None  # type: ignore[attr-defined]
    assert b.transfer.commit is Commit.COMMITTED  # type: ignore[attr-defined]
    assert taken.read_bytes() == b"theirs" and sorted(category.iterdir()) == [taken]
    assert sha256(target_of(plan, "b.jpg")) == sha256(src / "b.jpg")
    assert fingerprints(src) == before
    assert spy.classes.count(RENAME) == 2 and partials(plan) == []
    (journal,) = journals.v2
    assert journals.v1 == []
    assert analyze_v3(journal.history, run_id=journal.run_id).problems == ()
    report = report_of(outcome)
    assert isinstance(report, JournalReportV3)
    assert report.problems == () and report.recovery_needed == ()
    ours = [
        (r["status"], r["commit"])
        for r in journal.history
        if r["operation"] == "copy" and Path(str(r["source"])).name == "a.pdf"
    ]
    assert ours[-1] == ("skipped", "not_committed")
    assert all(s != "done" and c != "committed" for s, c in ours)  # no false done
    phases = [s for n, s in copy_statuses(outcome, journal) if n == "b.jpg"]
    assert phases == ["intent", "staged", "written", "verified", "commit_attempted", "done"]
    made = [Path(str(r["target"])).name for r in journal.history if r["operation"] == "mkdir"]
    assert "PDF" not in made  # the category existed: the runner created no other


def test_real_second_volume_not_committed_removes_only_its_own_temporary_file(
    src: Path, second: Pinned
) -> None:
    """Both renames refused: nothing committed, each copy's own temporary file removed by
    the copy, another program's ``.partial`` and the sources kept. The category of
    ``a.pdf`` is made as the runner makes it (``Pinned.make_category``), so ``a.pdf``
    reaches the rename instead of refusing the category first."""
    touch(src, "a.pdf", "b.jpg")
    category = target_of(real_plan(src, second.here), "a.pdf").parent
    second.mkdirs(category.parent)
    second.make_category(category, src)
    plan = real_plan(src, second.here)
    assert target_of(plan, "a.pdf").parent == category
    foreign = category / "a.pdf.someone-else.partial"
    second.create_new(foreign, b"not ours")  # after the preview, like another program's
    before = fingerprints(src)
    ops = RenameStatus(STATUS_ACCESS_DENIED)
    outcome = run(plan, ops=ops)
    assert ops.renames == 2 and outcome.stopped is None
    for result in outcome.items:
        assert result.status is CopyStatus.FAILED and result.transfer is not None
        assert result.transfer.commit is Commit.NOT_COMMITTED
        assert result.transfer.temp_state is TempState.REMOVED
        assert not result.item.target.exists()
    assert partials(plan) == [foreign] and foreign.read_bytes() == b"not ours"
    assert fingerprints(src) == before and report_of(outcome).problems == ()


def test_real_second_volume_rename_not_established_stops_for_recovery(
    src: Path, second: Pinned, spy: Spy, journals: Journals
) -> None:
    """Scripted ``STATUS_PENDING``: recovery required, nothing cleaned, no next file."""
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    plan = real_plan(src, second.here)
    ops = RenameStatus(STATUS_PENDING)
    outcome = run(plan, ops=ops)
    assert ops.renames == 1 and outcome.stopped is ExecReason.RUN_ABORTED
    first = next(r for r in outcome.items if r.transfer is not None)
    other = next(r for r in outcome.items if r is not first)
    assert first.status is CopyStatus.RECOVERY_REQUIRED
    assert first.reason == Reason.RENAME_NOT_ESTABLISHED
    assert (other.status, other.reason) == (CopyStatus.CANCELLED, ExecReason.RUN_ABORTED)
    assert len(partials(plan)) == 1 and not other.item.target.exists()  # left for a person
    assert DISPOSITION not in spy.classes and DISPOSITION_EX not in spy.classes
    assert fingerprints(src) == before
    (journal,) = journals.v2
    ours = [
        (r["status"], r["commit"])
        for r in journal.history
        if r["operation"] == "copy" and Path(str(r["source"])) == first.item.source
    ]
    assert ours[-2:] == [("commit_attempted", "attempted"), ("recovery_required", Commit.UNKNOWN)]
    report = report_of(outcome)
    assert report.problems == () and report.recovery_needed


def test_real_second_volume_source_security_change_is_never_committed(
    src: Path, second: Pinned, spy: Spy, journals: Journals
) -> None:
    """The source on C: (this test's own file in ``tmp_path``, the only DACL changed)
    loses SYSTEM's READ after its descriptor was read for the copy, right before the
    commit: nothing is committed on D:, no rename at all, no false ``done``; the
    temporary file is removed, the source's bytes and identity are kept."""
    # here, not at the top: test_runner_acl_ntfs imports from this module
    from test_aclproof import SYSTEM, descriptor, set_sddl
    from test_runner_acl_ntfs import FILE_READ_DATA, ONLY_OWNER, SourceOps, file_id

    from downloads_organizer import aclproof as a

    touch(src, "a.pdf")
    source = src / "a.pdf"
    plan = real_plan(src, second.here)
    before, identity = fingerprints(src), file_id(source)
    assert a.effective_access(descriptor(source).raw, SYSTEM) & FILE_READ_DATA
    seen: list[tuple[int, ...]] = []

    def revoke(temp: Path) -> None:
        set_sddl(source, ONLY_OWNER)
        both = (descriptor(temp).raw, descriptor(source).raw)
        seen.append(tuple(a.effective_access(d, SYSTEM) & FILE_READ_DATA for d in both))

    ops = SourceOps(source, at=2, change=revoke)
    outcome = run(plan, acl_ops=ops)
    result = by_name(outcome)["a.pdf"]
    assert seen == [(FILE_READ_DATA, 0)]  # the temporary file still with the old DACL
    assert (result.status, result.reason) == (CopyStatus.FAILED, "verify_failed")  # type: ignore[attr-defined]
    assert result.transfer.commit is Commit.NOT_ATTEMPTED  # type: ignore[attr-defined]
    assert result.transfer.temp_state is TempState.REMOVED  # type: ignore[attr-defined]
    assert result.transfer.rename_spent is False  # type: ignore[attr-defined]
    assert RENAME not in spy.classes  # no rename, so never a second one
    assert not target_of(plan, "a.pdf").exists() and partials(plan) == []
    assert ops.source_reads == 3  # before the copy, at C12, before the commit
    assert fingerprints(src) == before and file_id(source) == identity
    (journal,) = journals.v2
    assert journals.v1 == [] and any(r["operation"] == "mkdir" for r in journal.history)
    copies = [r for r in journal.history if r["operation"] == "copy"]
    assert [(r["status"], r["temp_state"]) for r in copies] == [
        ("intent", "none"),
        ("staged", "present"),
        ("written", "present"),
        ("verified", "present"),
        ("failed", "removed"),
    ]
    assert all(r["commit"] == "not_attempted" for r in copies)  # no C14, no done
    assert "changed after the copy began" in str(copies[-1])
    assert analyze_v3(journal.history, run_id=journal.run_id).problems == ()
    assert outcome.journal_path is not None and outcome.stopped is None
    report = read_any_journal(outcome.journal_path)
    assert isinstance(report, JournalReportV3) and report.problems == ()
    assert [e.state for e in report.entries if e.operation == "copy"] == [OperationState.FAILED]


def test_real_second_volume_journal_failure_stops_before_any_rename(
    src: Path, second: Pinned, spy: Spy, tmp_path: Path
) -> None:
    """The real chain (runner, the category created with its security, a JSONL v3
    journal, the copy to D:) with the journal's write of ``verified`` failing (scripted):
    the run stops, nothing is renamed, no final file, no next file, no recovery."""
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    plan = real_plan(src, second.here)
    path = tmp_path / "journal.jsonl"  # on C:, this test's own
    stream = FailOn(path.open("w+b"), b'"status":"verified"')
    made: list[JournalV3] = []

    def journal() -> JournalV3:
        made.append(log := JournalV3(stream, path))  # type: ignore[arg-type]
        return log

    outcome = run_plan(plan, TABLE, journal_v3=journal, copy=CopyOptions(acl=Permit()))
    assert isinstance(outcome, CopyRunResult)
    assert RENAME not in spy.classes and outcome.stopped is ExecReason.JOURNAL_FAILED
    assert not any(i.target.exists() for i in plan.moves) and fingerprints(src) == before
    first = next(r for r in outcome.items if r.transfer is not None)
    assert (first.status, first.reason) == (CopyStatus.CANCELLED, Reason.JOURNAL_FAILED)
    assert first.transfer.commit is Commit.NOT_ATTEMPTED  # type: ignore[union-attr]
    others = [r for r in outcome.items if r is not first]
    assert others and all(r.transfer is None for r in others)  # no next file
    assert all(r.status is CopyStatus.CANCELLED for r in others)
    assert partials(plan) == []  # its own temporary file, removed
    (log,) = made
    assert any(r["operation"] == "mkdir" for r in log.history)
    copies = [str(r["status"]) for r in log.history if r["operation"] == "copy"]
    assert copies == ["intent", "staged", "written"]  # nothing after the failed write
