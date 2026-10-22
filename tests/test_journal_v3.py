"""Journal schema v3 (R3B integration): the mkdir fields, the writer and the reader.

A v3 record is a v2 record plus ``parent_id``, ``dir_state`` and ``dir_id`` (mkdir
records only). The writer must refuse what the reader refuses; a cut-off mkdir block
says the directory may exist. v1 and v2 are read as before. Files only in ``tmp_path``.
"""

from __future__ import annotations

import io
import json
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from downloads_organizer import journal_v2 as v2
from downloads_organizer import journal_v3 as v3
from downloads_organizer.journal import JournalError, JournalReport, OperationState
from downloads_organizer.journal_v2 import (
    FIELDS_V2,
    JournalReportV2,
    JournalV2,
    OperationV2,
    StatusV2,
    VolumeRef,
    WriterState,
)
from downloads_organizer.journal_v3 import (
    DIR_FIELDS,
    FIELDS_V3,
    MAY_EXIST,
    DirState,
    JournalReportV3,
    JournalV3,
    analyze_v3,
    read_any_journal,
    read_journal_v3,
    record_errors,
)

RUN_ID = "3f2a5c0d9e1b4a7c8d6e5f4a3b2c1d0e"
SV = VolumeRef("\\Device\\HarddiskVolume3", 0xA1B2C3D4)
TV = VolumeRef("\\Device\\HarddiskVolume5", 0xE5F60718)
ROOT, CAT = Path("C:/Sorted"), Path("C:/Sorted/PDF")
PARENT = 0x0123456789ABCDEF0123456789ABCDEF
DIR_ID = 0x00000000000000000001000000000042
O, S = OperationV2, StatusV2  # noqa: E741
DONE, FAILED, RR, UNCERTAIN = (
    OperationState.DONE,
    OperationState.FAILED,
    OperationState.RECOVERY_REQUIRED,
    OperationState.UNCERTAIN,
)


def clock() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


class Stream(io.BytesIO):
    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        return 7


@pytest.fixture(autouse=True)
def no_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v3, "os", types.SimpleNamespace(fsync=lambda _fd: None))
    monkeypatch.setattr(v2, "os", types.SimpleNamespace(fsync=lambda _fd: None))


def writer(stream: io.BytesIO | None = None) -> JournalV3:
    return JournalV3(stream or Stream(), run_id=RUN_ID, clock=clock)


def started(journal: JournalV3) -> JournalV3:
    journal.write(
        O.RUN,
        S.STARTED,
        source=Path("C:/Inbox"),
        target=ROOT,
        reason="items=1 copies=1",
        source_volume=SV,
        target_volume=TV,
    )
    return journal


def finished(journal: JournalV3) -> None:
    journal.write(O.RUN, S.FINISHED, reason="completed", source_volume=SV, target_volume=TV)


def mkdir(
    journal: JournalV3,
    status: StatusV2,
    state: DirState,
    dir_id: int | None,
    reason: str | None = None,
) -> int:
    seq = journal.write(O.MKDIR, S.INTENT, target=CAT, parent_id=PARENT)
    assert journal.open_block() is not None
    journal.write(
        O.MKDIR,
        status,
        intent=seq,
        target=CAT,
        reason=reason,
        parent_id=PARENT,
        dir_state=state,
        dir_id=dir_id,
    )
    assert journal.open_block() is None
    return seq


def records(stream: io.BytesIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().decode().splitlines()]


# (status, state, dir_id, reason, the entry's state, a note)
OUTCOMES = [
    (S.DONE, DirState.CREATED, DIR_ID, None, DONE, None),
    (S.DONE, DirState.EXISTING, DIR_ID, None, DONE, None),
    (S.FAILED, DirState.REFUSED, None, "target_dir_failed", FAILED, None),
    (S.FAILED, DirState.REMOVED, DIR_ID, "check failed", FAILED, None),
    (S.FAILED, DirState.REMOVED, None, "check failed", FAILED, None),
    (S.RECOVERY_REQUIRED, DirState.UNCHECKED, DIR_ID, "exception", RR, "not fully checked"),
    (S.RECOVERY_REQUIRED, DirState.LEFT, DIR_ID, "identity", RR, "still there"),
    (S.RECOVERY_REQUIRED, DirState.REMOVAL_UNKNOWN, None, "exception", RR, "removed is unknown"),
    (S.RECOVERY_REQUIRED, DirState.UNKNOWN, None, "exception", RR, "may exist"),
]


# --- valid journals ---------------------------------------------------------------------


@pytest.mark.parametrize(("status", "state", "dir_id", "reason", "entry_state", "note"), OUTCOMES)
def test_every_mkdir_outcome_is_written_and_read_back(
    status: StatusV2,
    state: DirState,
    dir_id: int | None,
    reason: str | None,
    entry_state: OperationState,
    note: str | None,
) -> None:
    stream = Stream()
    journal = started(writer(stream))
    mkdir(journal, status, state, dir_id, reason)
    finished(journal)
    written = records(stream)
    assert all(tuple(r) == FIELDS_V3 and r["schema_version"] == 3 for r in written)
    intent, outcome = written[1], written[2]
    assert intent["parent_id"] == outcome["parent_id"] == f"{PARENT:032x}"
    assert intent["dir_state"] is None and intent["dir_id"] is None
    assert outcome["dir_state"] == str(state)
    assert outcome["dir_id"] == (None if dir_id is None else f"{dir_id:032x}")
    assert all(r[name] is None for r in (written[0], written[3]) for name in DIR_FIELDS)
    report = analyze_v3(written, run_id=RUN_ID)
    assert isinstance(report, JournalReportV3) and report.problems == ()
    assert report.finished and not report.review_whole_journal
    (entry,) = report.entries
    assert entry.operation == "mkdir" and entry.state is entry_state
    if note is None:
        assert entry.notes == ()
    else:
        assert any(note in n for n in entry.notes)


def test_created_journal_file_is_read_back(tmp_path: Path) -> None:
    journal = started(JournalV3.create(tmp_path / "logs"))
    try:
        mkdir(journal, S.DONE, DirState.CREATED, DIR_ID)
        finished(journal)
    finally:
        journal.close()
    assert journal.path is not None
    report = read_journal_v3(journal.path)
    assert report.problems == () and report.finished
    assert [e.state for e in report.entries] == [DONE]
    assert isinstance(read_any_journal(journal.path), JournalReportV3)


# --- invalid records: the reader refuses, and so does the writer ---------------------------


def _valid() -> list[dict[str, object]]:
    stream = Stream()
    journal = started(writer(stream))
    mkdir(journal, S.DONE, DirState.CREATED, DIR_ID)
    finished(journal)
    return records(stream)


def _changed(index: int, **fields: object) -> list[dict[str, object]]:
    written = _valid()
    written[index] = written[index] | fields
    return written


BAD_RECORDS = {
    "intent without parent_id": (1, {"parent_id": None}, "invalid parent_id"),
    "terminal without parent_id": (2, {"parent_id": None}, "invalid parent_id"),
    "parent_id not hex": (1, {"parent_id": "zz"}, "invalid parent_id"),
    "parent_id differs in the block": (2, {"parent_id": "f" * 32}, "differs from its intent"),
    "intent with a dir_state": (1, {"dir_state": "created"}, "null in a mkdir intent"),
    "intent with a dir_id": (1, {"dir_id": "1" * 32}, "null in a mkdir intent"),
    "terminal without dir_state": (2, {"dir_state": None}, "invalid dir_state"),
    "unknown dir_state": (2, {"dir_state": "maybe"}, "invalid dir_state"),
    "created without dir_id": (2, {"dir_id": None}, "dir_id null"),
    "done with a failed state": (2, {"dir_state": "refused", "dir_id": None}, "mkdir done with"),
    "dir_id not hex": (2, {"dir_id": "x" * 32}, "invalid dir_id"),
    "parent_id on run": (0, {"parent_id": "1" * 32}, "outside mkdir"),
    "dir_state on run": (3, {"dir_state": "created"}, "outside mkdir"),
    "schema 2 record": (1, {"schema_version": 2}, "unsupported schema_version"),
}


@pytest.mark.parametrize("case", sorted(BAD_RECORDS))
def test_reader_distrusts_a_bad_mkdir_record(case: str) -> None:
    index, fields, message = BAD_RECORDS[case]
    report = analyze_v3(_changed(index, **fields), run_id=RUN_ID)
    assert report.review_whole_journal and not report.finished
    assert any(message in p.message for p in report.problems), report.problems
    assert all(e.state is not DONE for e in report.entries)


def test_record_without_the_v3_fields_is_refused() -> None:
    record = {k: v for k, v in _valid()[1].items() if k not in DIR_FIELDS}
    assert any("missing fields" in e for e in record_errors(record))
    assert any("unknown fields" in e for e in record_errors(_valid()[1] | {"extra": 1}))
    assert record_errors([]) == ["not a JSON object"]


TERMINAL_MISMATCHES = [
    (S.FAILED, DirState.CREATED, DIR_ID, "r", "mkdir failed with"),
    (S.RECOVERY_REQUIRED, DirState.REFUSED, None, "r", "mkdir recovery_required with"),
    (S.DONE, DirState.UNKNOWN, None, None, "mkdir done with"),
    (S.DONE, DirState.EXISTING, None, None, "dir_id null"),
    (S.FAILED, DirState.REFUSED, DIR_ID, "r", "dir_id set"),
    (S.RECOVERY_REQUIRED, DirState.UNKNOWN, DIR_ID, "r", "dir_id set"),
    (S.FAILED, DirState.REFUSED, None, None, "without a reason"),
    (S.RECOVERY_REQUIRED, DirState.LEFT, DIR_ID, None, "without a reason"),
]


@pytest.mark.parametrize(("status", "state", "dir_id", "reason", "message"), TERMINAL_MISMATCHES)
def test_writer_refuses_what_the_reader_refuses(
    status: StatusV2, state: DirState, dir_id: int | None, reason: str | None, message: str
) -> None:
    stream = Stream()
    journal = started(writer(stream))
    seq = journal.write(O.MKDIR, S.INTENT, target=CAT, parent_id=PARENT)
    before = stream.getvalue()
    kw = {"intent": seq, "target": CAT, "reason": reason, "parent_id": PARENT}
    with pytest.raises(JournalError, match=message):
        journal.write(O.MKDIR, status, dir_state=state, dir_id=dir_id, **kw)  # type: ignore[arg-type]
    assert stream.getvalue() == before  # nothing was written
    assert journal.state is WriterState.FAILED
    # The same record, built by hand, is refused by the reader.
    built = records(stream)
    terminal = built[-1] | {
        "sequence": seq + 1,
        "status": str(status),
        "intent": seq,
        "reason": reason,
        "dir_state": str(state),
        "dir_id": None if dir_id is None else f"{dir_id:032x}",
    }
    report = analyze_v3([*built, terminal], run_id=RUN_ID)
    assert any(message in p.message for p in report.problems)


@pytest.mark.parametrize(
    "kw",
    [
        {"parent_id": None},
        {"parent_id": PARENT, "dir_state": DirState.CREATED},
        {"parent_id": PARENT, "dir_id": DIR_ID},
    ],
)
def test_writer_refuses_a_bad_mkdir_intent(kw: dict[str, object]) -> None:
    journal = started(writer())
    with pytest.raises(JournalError):
        journal.write(O.MKDIR, S.INTENT, target=CAT, **kw)  # type: ignore[arg-type]
    assert journal.open_block() is None


def test_writer_refuses_dir_fields_outside_mkdir() -> None:
    journal = writer()
    with pytest.raises(JournalError, match="outside mkdir"):
        journal.write(
            O.RUN,
            S.STARTED,
            source=Path("C:/Inbox"),
            target=ROOT,
            reason="items=1 copies=1",
            source_volume=SV,
            target_volume=TV,
            parent_id=PARENT,
        )


def test_writer_refuses_a_parent_id_that_differs_from_its_intent() -> None:
    journal = started(writer())
    seq = journal.write(O.MKDIR, S.INTENT, target=CAT, parent_id=PARENT)
    with pytest.raises(JournalError, match="differs from its intent"):
        journal.write(
            O.MKDIR,
            S.DONE,
            intent=seq,
            target=CAT,
            parent_id=PARENT + 1,
            dir_state=DirState.CREATED,
            dir_id=DIR_ID,
        )


# --- a cut-off or damaged journal ---------------------------------------------------------


def test_journal_cut_off_after_a_mkdir_intent_says_the_directory_may_exist() -> None:
    stream = Stream()
    journal = started(writer(stream))
    journal.write(O.MKDIR, S.INTENT, target=CAT, parent_id=PARENT)
    assert journal.open_block() is not None
    report = analyze_v3(records(stream), run_id=RUN_ID)
    assert not report.finished
    (entry,) = report.entries
    assert entry.state is UNCERTAIN and MAY_EXIST in entry.notes
    assert entry.outcome is None


def test_cut_off_last_line_distrusts_the_whole_journal(tmp_path: Path) -> None:
    stream = Stream()
    journal = started(writer(stream))
    mkdir(journal, S.DONE, DirState.CREATED, DIR_ID)
    finished(journal)
    data = stream.getvalue()
    path = tmp_path / f"run-20260101T000000000000Z-{RUN_ID}.jsonl"
    path.write_bytes(data[:-10])  # the run/finished record is cut off
    report = read_journal_v3(path)
    assert report.review_whole_journal and not report.finished
    assert report.unreadable and report.entries
    assert all(e.state is RR for e in report.entries)
    assert all(e.outcome is None for e in report.entries)
    (entry,) = report.entries
    assert MAY_EXIST in entry.notes  # a distrusted block accepts no outcome


def test_record_inside_an_open_mkdir_block_distrusts_the_journal() -> None:
    stream = Stream()
    journal = started(writer(stream))
    journal.write(O.MKDIR, S.INTENT, target=CAT, parent_id=PARENT)
    with pytest.raises(JournalError):
        journal.write(O.MKDIR, S.INTENT, target=CAT, parent_id=PARENT)
    built = records(stream)
    second = built[-1] | {"sequence": len(built) + 1}
    report = analyze_v3([*built, second], run_id=RUN_ID)
    assert report.review_whole_journal and report.problems


# --- v1 and v2 are unchanged ---------------------------------------------------------------


def test_read_any_journal_dispatches_by_schema(tmp_path: Path) -> None:
    v3_journal = started(JournalV3.create(tmp_path / "v3"))
    v2_journal = JournalV2.create(tmp_path / "v2")
    try:
        finished(v3_journal)
        v2_journal.write(
            O.RUN,
            S.STARTED,
            source=Path("C:/Inbox"),
            target=ROOT,
            reason="items=0 copies=0",
            source_volume=SV,
            target_volume=TV,
        )
        v2_journal.write(O.RUN, S.FINISHED, reason="completed", source_volume=SV, target_volume=TV)
    finally:
        v3_journal.close()
        v2_journal.close()
    assert v3_journal.path is not None and v2_journal.path is not None
    third = read_any_journal(v3_journal.path)
    second = read_any_journal(v2_journal.path)
    assert type(third) is JournalReportV3 and third.finished
    assert type(second) is JournalReportV2 and second.finished
    assert json.loads(v2_journal.path.read_text().splitlines()[0])["schema_version"] == 2
    assert tuple(json.loads(v2_journal.path.read_text().splitlines()[0])) == FIELDS_V2
    v1 = tmp_path / "v1.jsonl"
    v1.write_text("not json\n")
    assert isinstance(read_any_journal(v1), JournalReport)


def test_the_v2_reader_trusts_no_v3_record(tmp_path: Path) -> None:
    journal = started(JournalV3.create(tmp_path / "logs"))
    try:
        finished(journal)
    finally:
        journal.close()
    assert journal.path is not None
    old = v2.read_journal_v2(journal.path)
    assert old.review_whole_journal and old.problems
