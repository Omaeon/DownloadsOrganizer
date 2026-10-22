"""The COPY state machine (R3B-4) with the real primitives on the NTFS volume of
``tmp_path``. Both the source and the target directory are under ``tmp_path`` (one
volume: the cross-volume part is the copy itself, never a rename between volumes).

A spy on ``NtSetInformationFile`` records every information class the machine sets:
4 (basic: the mtime), 10 (rename), 13 / 64 (disposition). The ownership stand
(``acquisitions``) checks every bind and every ``CloseHandle``.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import Acquisitions
from test_winfileops import walk

from downloads_organizer import winfileops as w
from downloads_organizer import winprims as p
from downloads_organizer.copycommit import (
    CopyResult,
    CopyStatus,
    Reason,
    SourceFacts,
    WinOps,
    copy_item,
)
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
from downloads_organizer.winfileops import HandleState, OpenOutcome

pytestmark = [pytest.mark.windows_only, pytest.mark.real_ntfs]

RUN_ID = "3f2a5c0d9e1b4a7c8d6e5f4a3b2c1d0e"
DIR_ACCESS = w.FILE_LIST_DIRECTORY | w.FILE_READ_ATTRIBUTES | w.FILE_ADD_FILE
DIR_SHARE = w.FILE_SHARE_READ | w.FILE_SHARE_WRITE
DATA = os.urandom(3 * 4096 + 17)
MTIME = 1_700_000_000_123_456_700  # a multiple of 100 ns
BASIC, RENAME, DISPOSITION, DISPOSITION_EX = 4, 10, 13, 64
TEMP = temp_name(RUN_ID, 2)  # after run/started


def _clock() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class Stand:
    inbox: Path
    room: Path
    directory: w.Handle
    journal: JournalV2
    volume: VolumeRef
    classes: list[int]
    after: dict[int, Callable[[], None]]  # info class -> run after the real call

    def source(self) -> tuple[w.Handle, SourceFacts]:
        inbox = walk(self.inbox, access=DIR_ACCESS, share=DIR_SHARE)
        try:
            src = w.Handle.new(self.inbox / "a.pdf", "H_SRC")
            assert p.open_into(src, inbox, "a.pdf") is OpenOutcome.OPENED_OWNED
        finally:
            inbox.close()
        return src, SourceFacts.of(w.query(src))

    def run(self, ops: object | None = None, **kw: object) -> CopyResult:
        src, facts = self.source()
        return copy_item(
            self.journal,
            source=self.inbox / "a.pdf",
            target=self.room / "a.pdf",
            src=src,
            directory=self.directory,
            facts=facts,
            source_volume=self.volume,
            target_volume=self.volume,
            ops=ops or WinOps(),  # type: ignore[arg-type]
            block_size=4096,
            **kw,  # type: ignore[arg-type]
        )

    def statuses(self) -> list[str]:
        return [record["status"] for record in self.journal.history]  # type: ignore[index]

    def names(self) -> list[str]:
        return sorted(entry.name for entry in self.room.iterdir())


@pytest.fixture
def stand(tmp_path: Path, acquisitions: Acquisitions) -> Iterator[Stand]:
    inbox, room = tmp_path / "inbox", tmp_path / "room"
    inbox.mkdir()
    room.mkdir()
    (inbox / "a.pdf").write_bytes(DATA)
    os.utime(inbox / "a.pdf", ns=(MTIME, MTIME))
    directory = walk(room, access=DIR_ACCESS, share=DIR_SHARE)
    volume = VolumeRef("\\Device\\TmpPathVolume", w.query(directory).volume_serial)
    stream = (tmp_path / "journal.jsonl").open("w+b")
    journal = JournalV2(stream, run_id=RUN_ID, clock=_clock)
    journal.write(
        OperationV2.RUN,
        StatusV2.STARTED,
        source=inbox,
        target=room,
        reason="items=1 copies=1",
        source_volume=volume,
        target_volume=volume,
    )
    classes: list[int] = []
    after: dict[int, Callable[[], None]] = {}
    ntdll = p._prims().ntdll
    real = ntdll.NtSetInformationFile

    def spy(*args: object) -> int:
        info_class = int(getattr(args[4], "value", args[4]))  # type: ignore[call-overload]
        classes.append(info_class)
        status = real(*args)
        action = after.get(info_class)
        if action is not None:
            action()
        return status

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ntdll, "NtSetInformationFile", spy)
        yield Stand(inbox, room, directory, journal, volume, classes, after)
    directory.close()
    stream.close()
    assert (inbox / "a.pdf").read_bytes() == DATA  # the source is only ever read


def test_ntfs_copy_commits(stand: Stand, acquisitions: Acquisitions) -> None:
    result = stand.run()
    assert result.status is CopyStatus.COPIED and result.stop is None
    assert stand.names() == ["a.pdf"]
    target = stand.room / "a.pdf"
    assert target.read_bytes() == DATA and target.stat().st_mtime_ns == MTIME
    assert stand.classes == [BASIC, RENAME]  # no disposition, nothing after the rename
    assert stand.statuses()[1:] == [
        "intent",
        "staged",
        "written",
        "verified",
        "commit_attempted",
        "done",
    ]
    record = stand.journal.history[-1]
    assert record["sha256"] == hashlib.sha256(DATA).hexdigest()  # type: ignore[index]
    assert record["temp"] == TEMP and record["target_id"] == record["temp_id"]  # type: ignore[index]
    assert analyze_v2(stand.journal.history, run_id=RUN_ID).problems == ()
    assert result.transfer.commit is Commit.COMMITTED
    assert result.transfer.handle_state is HandleState.CLOSED


def test_ntfs_target_exists_is_skipped_and_cleaned(stand: Stand) -> None:
    (stand.room / "a.pdf").write_bytes(b"theirs")
    result = stand.run()
    assert (result.status, result.reason) == (CopyStatus.SKIPPED, Reason.TARGET_EXISTS)
    assert stand.names() == ["a.pdf"] and (stand.room / "a.pdf").read_bytes() == b"theirs"
    assert stand.classes == [BASIC, RENAME, DISPOSITION_EX]  # one rename, one disposition
    record = stand.journal.history[-1]
    assert (record["commit"], record["temp_state"]) == ("not_committed", "removed")  # type: ignore[index]
    assert analyze_v2(stand.journal.history, run_id=RUN_ID).problems == ()


def test_ntfs_temp_name_taken_is_left_alone(stand: Stand) -> None:
    (stand.room / TEMP).write_bytes(b"someone")
    result = stand.run()
    assert (result.status, result.reason) == (CopyStatus.SKIPPED, Reason.TEMP_NAME_TAKEN)
    assert stand.names() == [TEMP] and (stand.room / TEMP).read_bytes() == b"someone"
    assert stand.classes == []


class FailingWrite(WinOps):
    def __init__(self) -> None:
        self.writes = 0

    def write_at(self, handle: w.Handle, offset: int, data: bytes) -> None:
        self.writes += 1
        if self.writes == 2:
            raise OSError(112, "scripted: disk full")
        super().write_at(handle, offset, data)


def test_ntfs_failure_before_the_commit_removes_the_temporary_file(stand: Stand) -> None:
    result = stand.run(FailingWrite())
    assert (result.status, result.reason) == (CopyStatus.FAILED, Reason.WRITE_FAILED)
    assert stand.names() == [] and stand.classes == [DISPOSITION_EX]
    record = stand.journal.history[-1]
    assert record["temp_state"] == "removed" and record["commit"] == "not_attempted"  # type: ignore[index]
    assert result.transfer.cleanup is not None
    assert result.transfer.cleanup.outcome is TempState.REMOVED


def test_ntfs_cancel_before_the_commit(stand: Stand) -> None:
    calls = iter(range(100))
    result = stand.run(cancelled=lambda: next(calls) >= 2)
    assert (result.status, result.reason) == (CopyStatus.CANCELLED, Reason.CANCELLED)
    assert stand.names() == [] and RENAME not in stand.classes


def test_ntfs_rename_done_status_lost(stand: Stand) -> None:
    """T62 on NTFS: the real rename happens, the interrupt comes before its status is
    captured. No second rename, no disposition, no record after COMMIT_ATTEMPTED; C15
    still runs and finds the commit."""

    def interrupt() -> None:
        raise KeyboardInterrupt

    stand.after[RENAME] = interrupt
    with pytest.raises(KeyboardInterrupt) as raised:
        stand.run()
    assert stand.names() == ["a.pdf"] and (stand.room / "a.pdf").read_bytes() == DATA
    assert stand.classes == [BASIC, RENAME]
    assert stand.statuses()[-1] == "commit_attempted"
    (state,) = raised.value.transfer_states  # type: ignore[attr-defined]
    assert state.rename is p.Renamed.LOST and state.commit is Commit.COMMITTED
    assert state.temp_state is TempState.COMMITTED and state.handle_state is HandleState.CLOSED


def test_ntfs_interrupt_before_the_rename_call_cleans(stand: Stand) -> None:
    class Interrupted(WinOps):
        def rename_once(self, *args: object) -> p.Renamed:  # type: ignore[override]
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt) as raised:
        stand.run(Interrupted())
    assert stand.names() == [] and stand.classes == [BASIC, DISPOSITION_EX]
    (state,) = raised.value.transfer_states  # type: ignore[attr-defined]
    assert state.rename_spent and state.commit is Commit.NOT_COMMITTED
    assert state.temp_state is TempState.REMOVED
