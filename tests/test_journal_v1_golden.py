"""Golden bytes of schema v1: the R3A format must not change with schema v2 (R3B-2)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from downloads_organizer.journal import (
    FIELDS,
    SCHEMA_VERSION,
    Journal,
    Operation,
    OperationState,
    Status,
    read_journal,
)
from downloads_organizer.journal_v2 import read_any_journal

RUN_ID = "0123456789abcdef0123456789abcdef"
NAME = f"run-20260101T000000000000Z-{RUN_ID}.jsonl"
HEAD = '{"schema_version":1,"run_id":"0123456789abcdef0123456789abcdef","sequence":'
STAMP = '"timestamp_utc":"2026-01-01T00:00:00.000000+00:00"'
GOLDEN = (
    HEAD + "1," + STAMP + ',"operation":"run","status":"started","intent":null,'
    '"source":"C:\\\\In","target":"C:\\\\Out","reason":"items=3 moves=2",'
    '"error_code":null,"error_message":null}\n'
    + HEAD + "2," + STAMP + ',"operation":"mkdir","status":"intent","intent":null,'
    '"source":null,"target":"C:\\\\Out\\\\PDF","reason":null,'
    '"error_code":null,"error_message":null}\n'
    + HEAD + "3," + STAMP + ',"operation":"mkdir","status":"done","intent":2,'
    '"source":null,"target":"C:\\\\Out\\\\PDF","reason":null,'
    '"error_code":null,"error_message":null}\n'
    + HEAD + "4," + STAMP + ',"operation":"move","status":"intent","intent":null,'
    '"source":"C:\\\\In\\\\ä.pdf","target":"C:\\\\Out\\\\PDF\\\\ä.pdf","reason":null,'
    '"error_code":null,"error_message":null}\n'
    + HEAD + "5," + STAMP + ',"operation":"move","status":"done","intent":4,'
    '"source":"C:\\\\In\\\\ä.pdf","target":"C:\\\\Out\\\\PDF\\\\ä.pdf","reason":null,'
    '"error_code":null,"error_message":null}\n'
    + HEAD + "6," + STAMP + ',"operation":"move","status":"skipped","intent":null,'
    '"source":"C:\\\\In\\\\bad\\udc80.txt","target":null,"reason":"target_exists",'
    '"error_code":183,"error_message":"exists"}\n'
    + HEAD + "7," + STAMP + ',"operation":"run","status":"finished","intent":null,'
    '"source":null,"target":null,"reason":"completed","error_code":null,"error_message":null}\n'
).encode()  # fmt: skip


def write_golden(path: Path) -> None:
    clock = lambda: datetime(2026, 1, 1, tzinfo=UTC)  # noqa: E731
    with path.open("wb") as stream:
        log = Journal(stream, run_id=RUN_ID, clock=clock)
        log.write(
            Operation.RUN,
            Status.STARTED,
            source=Path("C:/In"),
            target=Path("C:/Out"),
            reason="items=3 moves=2",
        )
        seq = log.write(Operation.MKDIR, Status.INTENT, target=Path("C:/Out/PDF"))
        log.write(Operation.MKDIR, Status.DONE, intent=seq, target=Path("C:/Out/PDF"))
        seq = log.write(
            Operation.MOVE,
            Status.INTENT,
            source=Path("C:/In/ä.pdf"),
            target=Path("C:/Out/PDF/ä.pdf"),
        )
        log.write(
            Operation.MOVE,
            Status.DONE,
            intent=seq,
            source=Path("C:/In/ä.pdf"),
            target=Path("C:/Out/PDF/ä.pdf"),
        )
        log.write(
            Operation.MOVE,
            Status.SKIPPED,
            source=Path("C:/In/bad\udc80.txt"),
            reason="target_exists",
            error_code=183,
            error_message="exists",
        )
        log.write(Operation.RUN, Status.FINISHED, reason="completed")


def test_v1_constants_are_unchanged() -> None:
    assert SCHEMA_VERSION == 1
    assert FIELDS == (
        "schema_version",
        "run_id",
        "sequence",
        "timestamp_utc",
        "operation",
        "status",
        "intent",
        "source",
        "target",
        "reason",
        "error_code",
        "error_message",
    )
    assert [str(o) for o in Operation] == ["run", "mkdir", "move"]


def test_v1_writer_produces_the_golden_bytes(tmp_path: Path) -> None:
    path = tmp_path / NAME
    write_golden(path)
    assert path.read_bytes() == GOLDEN


def test_v1_reader_accepts_the_golden_journal(tmp_path: Path) -> None:
    path = tmp_path / NAME
    path.write_bytes(GOLDEN)
    report = read_journal(path)
    assert report.problems == ()
    assert report.finished and not report.review_whole_journal
    assert [e.state for e in report.entries] == [
        OperationState.DONE,
        OperationState.DONE,
        OperationState.SKIPPED,
    ]
    assert report.records[5]["source"] == "C:\\In\\bad\udc80.txt"  # type: ignore[index]


def test_dispatcher_reads_a_v1_journal_with_the_v1_reader(tmp_path: Path) -> None:
    path = tmp_path / NAME
    path.write_bytes(GOLDEN)
    assert read_any_journal(path) == read_journal(path)
