"""Journal schema v2 (R3B-2): records, grammar, writer transaction and reader.

The examples V1-V21 (valid) and I1-I61 (invalid) are those of DESIGN-02 FIX-03 §6.10.
Every record of the tests is built here from the matrix of §6.3, independently of the
writer; the writer must write exactly those bytes and refuse exactly what the reader
refuses. Files only in ``tmp_path``.
"""

from __future__ import annotations

import dataclasses
import io
import json
import types
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from downloads_organizer import journal_v2 as v2
from downloads_organizer.journal import (
    Journal,
    JournalError,
    JournalReport,
    Operation,
    OperationState,
    Status,
    analyze,
    read_journal,
)
from downloads_organizer.journal_v2 import (
    FIELDS_V2,
    TARGET_EXISTS,
    TEMP_CREATE_UNCERTAIN,
    TEMP_NAME_TAKEN,
    Commit,
    Diagnostics,
    EntryV2,
    JournalReportV2,
    JournalV2,
    OperationV2,
    ReviewFlag,
    StatusV2,
    TempState,
    VolumeRef,
    WriterState,
    analyze_v2,
    read_any_journal,
    read_journal_v2,
    record_errors,
    temp_name,
)

RUN_ID = "3f2a5c0d9e1b4a7c8d6e5f4a3b2c1d0e"
OTHER_RUN = "ffffffffffffffffffffffffffffffff"
NAME = f"run-20260101T000000000000Z-{RUN_ID}.jsonl"
STAMP = "2026-01-01T00:00:00.000000+00:00"
SV = VolumeRef("\\Device\\HarddiskVolume3", 0xA1B2C3D4)
TV = VolumeRef("\\Device\\HarddiskVolume5", 0xE5F60718)
OTHER_VOLUME = VolumeRef("\\Device\\HarddiskVolume9", 0x99)
SRC, DST = Path("D:/Inbox/a.pdf"), Path("E:/Sorted/PDF/a.pdf")
OTHER = Path("D:/Inbox/other.pdf")
SIZE = 1048576
SOURCE_ID = 0x0123456789ABCDEF0123456789ABCDEF
TEMP_ID = 0x00000000000000000001000000000042
SHA = "ab" * 32
DIAG = Diagnostics(temp_lookup="present", cleanup="delete failed: 5")

O, S = OperationV2, StatusV2  # noqa: E741
NA, ATT, NC = Commit.NOT_ATTEMPTED, Commit.ATTEMPTED, Commit.NOT_COMMITTED
CU, CC = Commit.UNKNOWN, Commit.COMMITTED
NONE, PRESENT, REMOVED = TempState.NONE, TempState.PRESENT, TempState.REMOVED
LEFT, UNKNOWN, TC = TempState.LEFT, TempState.UNKNOWN, TempState.COMMITTED
RRS = S.RECOVERY_REQUIRED
DONE, FAILED, SKIPPED, CANCELLED, RR, UNCERTAIN = (
    OperationState.DONE,
    OperationState.FAILED,
    OperationState.SKIPPED,
    OperationState.CANCELLED,
    OperationState.RECOVERY_REQUIRED,
    OperationState.UNCERTAIN,
)

Step = tuple[OperationV2, StatusV2, dict[str, object]]


def clock() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


# --- building the examples (independently of the writer) -------------------------------


class Run:
    """The writer calls of one run; it keeps track of the open copy block."""

    def __init__(self, *, started: bool = True) -> None:
        self.steps: list[Step] = []
        self.intent: int | None = None
        self.staged = self.written = False
        if started:
            self.add(
                O.RUN,
                S.STARTED,
                source=Path("D:/Inbox"),
                target=Path("E:/Sorted"),
                reason="items=1 copies=1",
                source_volume=SV,
                target_volume=TV,
            )

    def add(self, operation: OperationV2, status: StatusV2, **kw: object) -> int:
        self.steps.append((operation, status, kw))
        return len(self.steps)

    def finished(self, **kw: object) -> Run:
        volumes = {"source_volume": SV, "target_volume": TV}
        self.add(O.RUN, S.FINISHED, reason="completed", **(volumes | kw))
        return self

    def mkdir(self, target: Path = Path("E:/Sorted/PDF")) -> Run:
        seq = self.add(O.MKDIR, S.INTENT, target=target)
        self.add(O.MKDIR, S.DONE, intent=seq, target=target)
        return self

    def base(self, seq: int) -> dict[str, object]:
        return {
            "source": SRC,
            "target": DST,
            "source_volume": SV,
            "target_volume": TV,
            "temp": temp_name(RUN_ID, seq),
            "size": SIZE,
            "source_id": SOURCE_ID,
        }

    def copy_intent(self, **over: object) -> Run:
        seq = len(self.steps) + 1
        self.add(O.COPY, S.INTENT, **({**self.base(seq), "commit": NA, "temp_state": NONE} | over))
        self.intent, self.staged, self.written = seq, False, False
        return self

    def copy(self, status: StatusV2, **over: object) -> Run:
        assert self.intent is not None
        self.staged |= status is S.STAGED
        self.written |= status is S.WRITTEN
        kw: dict[str, object] = {
            **self.base(self.intent),
            "intent": self.intent,
            "temp_id": TEMP_ID if self.staged else None,
            "sha256": SHA if self.written else None,
        }
        if status in (S.STAGED, S.WRITTEN, S.VERIFIED):
            kw |= {"commit": NA, "temp_state": PRESENT}
        elif status is S.COMMIT_ATTEMPTED:
            kw |= {"commit": ATT, "temp_state": PRESENT}
        elif status is S.DONE:
            kw |= {"target_id": TEMP_ID, "commit": CC, "temp_state": TC}
        self.add(O.COPY, status, **(kw | over))
        return self

    def phases(self, *statuses: StatusV2) -> Run:
        for status in statuses:
            self.copy(status)
        return self

    def through_commit(self, **over: object) -> Run:
        self.copy_intent(**over)
        return self.phases(S.STAGED, S.WRITTEN, S.VERIFIED, S.COMMIT_ATTEMPTED)

    def end(
        self, status: StatusV2, commit: Commit, state: TempState, reason: str | None, **kw: object
    ) -> Run:
        diagnostics = DIAG if state in (LEFT, UNKNOWN) else None
        defaults = {
            "commit": commit,
            "temp_state": state,
            "reason": reason,
            "diagnostics": diagnostics,
        }
        return self.copy(status, **(defaults | kw))

    def unstarted(self, reason: str = "unsupported_streams", **kw: object) -> Run:
        defaults = {
            "source": SRC,
            "target": DST,
            "source_volume": SV,
            "target_volume": TV,
            "commit": NA,
            "temp_state": NONE,
            "reason": reason,
        }
        self.add(O.COPY, S.SKIPPED, **(defaults | kw))
        return self


def build(steps: list[Step], run_id: str = RUN_ID) -> list[dict[str, object]]:
    """The records the matrix of §6.3 prescribes for ``steps``."""
    records = []
    for sequence, (operation, status, kw) in enumerate(steps, start=1):
        record: dict[str, object] = dict.fromkeys(FIELDS_V2)
        record |= {
            "schema_version": 2,
            "run_id": run_id,
            "sequence": sequence,
            "timestamp_utc": STAMP,
            "operation": str(operation),
            "status": str(status),
            "transfer": "cross_volume_copy",
            "source_action": "keep" if operation == "copy" else None,
        }
        for name, value in kw.items():
            if isinstance(value, VolumeRef):
                value = {
                    "device": value.device,
                    "serial": f"{value.serial:016x}",
                    "guid": value.guid,
                }
            elif isinstance(value, Diagnostics):
                value = dataclasses.asdict(value)
            elif name.endswith("_id") and isinstance(value, int):
                value = f"{value:032x}"
            record[name] = str(value) if isinstance(value, (Path, str)) else value
        records.append(record)
    return records


def encode(records: list[dict[str, object]]) -> bytes:
    lines = [json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in records]
    return "".join(lines).encode()


class Stream(io.BytesIO):
    """An in-memory journal file; ``journal_v2.os.fsync`` is replaced by ``fsyncs``."""

    def __init__(self) -> None:
        super().__init__()
        self.writes = 0
        self.flushes = 0

    def write(self, data: object) -> int:  # type: ignore[override]
        self.writes += 1
        return super().write(data)  # type: ignore[arg-type]

    def flush(self) -> None:
        self.flushes += 1

    def fileno(self) -> int:
        return 7


@pytest.fixture
def fsyncs(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(v2, "os", types.SimpleNamespace(fsync=calls.append))
    return calls


def writer(stream: io.BytesIO | None = None) -> JournalV2:
    return JournalV2(stream or Stream(), run_id=RUN_ID, clock=clock)


def replay(journal: JournalV2, steps: list[Step]) -> None:
    for operation, status, kw in steps:
        journal.write(operation, status, **kw)  # type: ignore[arg-type]


def last_entry(report: JournalReportV2) -> EntryV2:
    return report.entries[-1]


# --- the valid examples V1-V21 -------------------------------------------------------------


def _v1() -> Run:
    return Run().mkdir().through_commit().copy(S.DONE).finished()


def _v4() -> Run:
    return Run().through_commit().end(RRS, CU, UNKNOWN, "rename_unknown").finished()


def _v8() -> Run:
    return Run().through_commit().end(RRS, CC, TC, "metadata", target_id=TEMP_ID, diagnostics=DIAG)


def _v12() -> Run:
    return Run().copy_intent().end(S.FAILED, NA, UNKNOWN, TEMP_CREATE_UNCERTAIN).finished()


def _v13() -> Run:
    diagnostics = Diagnostics(delete_pending=True, close_may_complete_delete=True)
    return Run().through_commit().end(RRS, CU, UNKNOWN, "unknown", diagnostics=diagnostics)


def _v14() -> Run:
    diagnostics = Diagnostics(metadata_violation="stream")
    return (
        Run().through_commit().end(RRS, CC, TC, "meta", target_id=TEMP_ID, diagnostics=diagnostics)
    )


def _v17() -> Run:
    run = Run().mkdir().mkdir(Path("E:/Sorted/Docs")).unstarted()
    assert len(run.steps) == 6  # the intent is record 7
    return run.copy_intent().copy(S.STAGED).end(S.FAILED, NA, LEFT, "copy_failed")


def _v18() -> Run:
    run = Run().copy_intent().phases(S.STAGED, S.WRITTEN, S.VERIFIED)
    return run.end(S.CANCELLED, NA, REMOVED, "cancelled")


# name -> (run, state of the last change, its flags, whether it has a note)
VALID: dict[str, tuple[Callable[[], Run], OperationState, set[ReviewFlag], bool]] = {
    "V1": (_v1, DONE, set(), False),
    "V2": (
        lambda: Run().through_commit().end(S.SKIPPED, NC, REMOVED, TARGET_EXISTS),
        SKIPPED,
        set(),
        False,
    ),
    "V3": (
        lambda: Run().copy_intent().copy(S.STAGED).end(S.FAILED, NA, REMOVED, "copy_failed"),
        FAILED,
        set(),
        False,
    ),
    "V4": (_v4, RR, {ReviewFlag.COMMIT_UNKNOWN, ReviewFlag.TEMP_LEFT}, False),
    "V5": (lambda: Run().unstarted(), SKIPPED, set(), False),
    "V6": (
        lambda: Run().copy_intent().end(S.SKIPPED, NA, NONE, TEMP_NAME_TAKEN),
        SKIPPED,
        set(),
        False,
    ),
    "V7": (
        lambda: Run().copy_intent().phases(S.STAGED, S.WRITTEN),
        UNCERTAIN,
        {ReviewFlag.TEMP_LEFT},
        False,
    ),
    "V8": (_v8, RR, set(), False),
    "V9": (
        lambda: Run().copy_intent().end(S.FAILED, NA, UNKNOWN, "temp_query_failed"),
        FAILED,
        {ReviewFlag.TEMP_LEFT},
        False,
    ),
    "V10": (
        lambda: Run().copy_intent().copy(S.STAGED).end(S.CANCELLED, NA, REMOVED, "cancelled"),
        CANCELLED,
        set(),
        False,
    ),
    "V11": (lambda: Run().copy_intent(), UNCERTAIN, {ReviewFlag.TEMP_MAY_EXIST}, False),
    "V12": (_v12, FAILED, {ReviewFlag.TEMP_LEFT}, True),
    "V13": (_v13, RR, {ReviewFlag.COMMIT_UNKNOWN, ReviewFlag.TEMP_LEFT}, True),
    "V14": (_v14, RR, set(), False),
    "V15": (
        lambda: Run().copy_intent().phases(S.STAGED, S.WRITTEN, S.VERIFIED),
        UNCERTAIN,
        {ReviewFlag.TEMP_LEFT},
        False,
    ),
    "V16": (lambda: Run().mkdir().finished(), DONE, set(), False),
    "V17": (_v17, FAILED, {ReviewFlag.TEMP_LEFT}, False),
    "V18": (_v18, CANCELLED, set(), False),
    "V19": (
        lambda: Run().through_commit().end(S.FAILED, NC, REMOVED, "commit_failed"),
        FAILED,
        set(),
        False,
    ),
    "V20": (
        lambda: Run().through_commit().end(RRS, CU, UNKNOWN, "rename_unknown").finished(),
        RR,
        {ReviewFlag.COMMIT_UNKNOWN, ReviewFlag.TEMP_LEFT},
        False,
    ),
    "V21": (
        lambda: Run().through_commit(),
        UNCERTAIN,
        {ReviewFlag.COMMIT_UNKNOWN, ReviewFlag.TEMP_MAY_EXIST},
        False,
    ),
}


@pytest.mark.parametrize("name", list(VALID))
def test_valid_example_is_accepted_by_the_reader(name: str) -> None:
    make, state, flags, note = VALID[name]
    run = make()
    records = build(run.steps)
    assert all(record_errors(r) == [] for r in records)
    report = analyze_v2(records, run_id=RUN_ID)
    assert report.problems == ()
    assert not report.review_whole_journal
    entry = last_entry(report)
    assert entry.state is state
    assert entry.flags == frozenset(flags)
    assert bool(entry.notes) is note
    assert entry.problems == ()
    assert report.finished is (run.steps[-1][:2] == (O.RUN, S.FINISHED))
    assert report.post_crash_unverified is not report.finished
    if state is UNCERTAIN:
        assert entry.outcome is None and entry.phase == run.steps[-1][1]
    else:
        assert entry.outcome == records[-2 if report.finished else -1]


@pytest.mark.parametrize("name", list(VALID))
def test_valid_example_is_written_byte_for_byte(name: str, fsyncs: list[int]) -> None:
    run = VALID[name][0]()
    stream = Stream()
    journal = writer(stream)
    replay(journal, run.steps)
    assert stream.getvalue() == encode(build(run.steps))
    assert journal.state is WriterState.READY and not journal.failed
    assert journal.next_sequence == len(run.steps) + 1
    assert [dict(r) for r in journal.history] == build(run.steps)
    assert len(fsyncs) == len(run.steps) == stream.flushes == stream.writes


def test_notes_say_what_may_have_happened() -> None:
    entry = last_entry(analyze_v2(build(_v12().steps)))
    assert entry.notes == ("the temporary file may have been created, but it was never owned",)
    entry = last_entry(analyze_v2(build(_v13().steps)))
    assert entry.notes == ("closing the temporary file may have completed its deletion",)


def test_delete_pending_makes_even_a_committed_copy_unknown() -> None:  # §6.7
    diagnostics = Diagnostics(delete_pending=True)
    run = Run().through_commit().end(RRS, CC, TC, "x", target_id=TEMP_ID, diagnostics=diagnostics)
    entry = last_entry(analyze_v2(build(run.steps)))
    assert entry.state is RR and entry.flags == {ReviewFlag.COMMIT_UNKNOWN}
    assert entry.notes == ("closing the temporary file may have completed its deletion",)


def test_intent_record_has_the_design_shape() -> None:
    record = build(Run().mkdir().unstarted().copy_intent().steps)[-1]
    assert record["sequence"] == 5
    assert record["temp"] == "~DownloadsOrganizer.3f2a5c0d9e1b.5.partial"
    assert record["source_volume"] == {
        "device": "\\Device\\HarddiskVolume3",
        "serial": "00000000a1b2c3d4",
        "guid": None,
    }
    assert (record["intent"], record["commit"], record["temp_state"]) == (
        None,
        "not_attempted",
        "none",
    )
    assert list(record) == list(FIELDS_V2)
    assert record_errors(record) == []


# --- the invalid examples I1-I61 and further rules ----------------------------------------


@dataclasses.dataclass(frozen=True)
class Change:
    """A change of the built records that the writer cannot express; ``index`` is the
    record it makes invalid, so the first one the reader has to reject."""

    index: int
    apply: Callable[[list[dict[str, object]]], None]


def mutate(index: int, **changes: object) -> Change:
    return Change(index, lambda records: records[index].update(changes))


def drop(index: int, name: str) -> Change:
    return Change(index, lambda records: records[index].pop(name))


def appended(index: int, value: object) -> Change:
    def apply(records: list[dict[str, object]]) -> None:
        assert len(records) == index
        records.append(value)  # type: ignore[arg-type]

    return Change(index, apply)


def full() -> Run:
    return Run().through_commit()


def verified() -> Run:
    return Run().copy_intent().phases(S.STAGED, S.WRITTEN, S.VERIFIED)


def staged() -> Run:
    return Run().copy_intent().copy(S.STAGED)


def written() -> Run:
    return Run().copy_intent().phases(S.STAGED, S.WRITTEN)


def mkdir_split() -> Run:
    run = Run()
    run.add(O.MKDIR, S.INTENT, target=Path("E:/Sorted/PDF"))
    return run.copy_intent()


def mkdir_other_target() -> Run:
    run = Run()
    seq = run.add(O.MKDIR, S.INTENT, target=Path("E:/Sorted/PDF"))
    run.add(O.MKDIR, S.DONE, intent=seq, target=Path("E:/Sorted/X"))
    return run


def after_finished() -> Run:
    run = Run().finished()
    run.add(O.MKDIR, S.INTENT, target=Path("E:/Sorted/PDF"))
    return run


def mkdir_outcome_alone() -> Run:
    run = Run()
    run.add(O.MKDIR, S.DONE, intent=2, target=Path("E:/Sorted/PDF"))
    return run


RUN_JSON = {"source_volume": SV.to_json(), "target_volume": TV.to_json()}
BAD_SERIAL = {"device": "d", "serial": "a1b2c3d4", "guid": None}
BAD_FLAG = {**DIAG.to_json(), "delete_pending": "yes"}
# name -> (run, a change of the built records, or None if the writer can produce them:
# then the last writer call of the run is the invalid one)
INVALID: dict[str, tuple[Run, Change | None]] = {
    "I1": (verified().copy(S.DONE), None),
    "I2": (Run().copy_intent().phases(S.STAGED, S.VERIFIED), None),
    "I3": (Run().copy_intent().phases(S.STAGED, S.STAGED), None),
    "I4": (staged().end(S.FAILED, NA, REMOVED, "x").copy(S.DONE), None),
    "I5": (full().end(S.FAILED, ATT, REMOVED, "x"), None),
    "I7": (full().copy(S.DONE, target_id=SOURCE_ID), None),
    "I8": (Run().copy_intent().copy_intent(source=OTHER), None),
    "I9": (full().copy(S.DONE), mutate(3, schema_version=1)),
    "I10": (Run().copy_intent(), mutate(1, operation="move")),
    "I12": (full().copy(S.DONE, commit=NC), None),
    "I13": (full().end(RRS, CU, UNKNOWN, "x", diagnostics=None), None),
    "I14": (Run().copy_intent().end(S.FAILED, NA, REMOVED, "x", temp_id=TEMP_ID), None),
    "I15": (Run().copy_intent().end(S.FAILED, NA, LEFT, "x", diagnostics=None), None),
    "I16": (full().end(RRS, CU, REMOVED, "x", diagnostics=DIAG), None),
    "I17": (written(), mutate(3, run_id=OTHER_RUN)),
    "I18": (full().copy(S.DONE).copy_intent().copy(S.STAGED, intent=2), None),
    "I19": (Run().copy_intent().copy(S.STAGED, source=OTHER), None),
    "I20": (verified().copy(S.COMMIT_ATTEMPTED, target=Path("E:/x.pdf")), None),
    "I21": (written().copy(S.VERIFIED, temp=temp_name(RUN_ID, 3)), None),
    "I22": (staged().copy(S.WRITTEN, size=SIZE + 1), None),
    "I23": (full().copy(S.DONE, source_id=SOURCE_ID + 1), None),
    "I24": (staged(), mutate(2, source_action="move")),
    "I25": (Run().copy_intent().copy(S.STAGED, target_volume=OTHER_VOLUME), None),
    "I26": (full().end(RRS, CU, UNKNOWN, "x", source_volume=OTHER_VOLUME), None),
    "I27": (Run().copy_intent(), mutate(1, transfer="copy")),
    "I28": (staged().copy(S.WRITTEN, temp_id=TEMP_ID + 1), None),
    "I29": (written().copy(S.VERIFIED, sha256="cd" * 32), None),
    "I30": (Run().copy_intent().copy(S.STAGED, sha256=SHA), None),
    "I31": (full(), mutate(5, target_id=f"{TEMP_ID:032x}")),
    "I31w": (verified().copy(S.COMMIT_ATTEMPTED, target_id=TEMP_ID), None),
    "I32": (staged(), mutate(2, operation="move")),
    "I33": (Run().copy_intent(), drop(1, "temp_state")),
    "I34": (Run().copy_intent(), mutate(1, extra=None)),
    "I35_serial": (Run().copy_intent(), mutate(1, source_volume=BAD_SERIAL)),
    "I35_short_id": (Run().copy_intent(), mutate(1, source_id="0123")),
    "I35_upper_id": (Run().copy_intent(), mutate(1, source_id=f"{SOURCE_ID:032X}")),
    "I35_upper_sha": (staged().copy(S.WRITTEN, sha256="AB" * 32), None),
    "I36": (staged(), mutate(2, sequence=2)),
    "I38": (full().copy(S.DONE).copy(S.DONE), None),
    "I39": (Run().mkdir(), mutate(1, transfer=None)),
    "I40": (Run().mkdir(), mutate(0, transfer=None)),
    "I41": (Run().mkdir(), mutate(1, source_action="keep")),
    "I42": (Run().mkdir().finished(), mutate(3, source_action="keep")),
    "I43": (Run().copy_intent(intent=2), None),
    "I44": (Run().mkdir().copy_intent(intent=2), None),
    "I45": (Run().copy_intent().copy(S.STAGED, intent=None), None),
    "I46": (staged().end(S.FAILED, NA, REMOVED, "x", intent=None), None),
    "I47": (full().end(S.CANCELLED, NC, REMOVED, "x"), None),
    "I48": (full().end(S.CANCELLED, NA, REMOVED, "x"), None),
    "I49": (Run().copy_intent().end(RRS, CU, UNKNOWN, "x"), None),
    "I50": (staged().end(RRS, CU, UNKNOWN, "x"), None),
    "I51": (written().end(RRS, CC, TC, "x", target_id=TEMP_ID, diagnostics=DIAG), None),
    "I52": (verified().end(RRS, CU, UNKNOWN, "x"), None),
    "I53": (full().end(RRS, NC, REMOVED, "x", diagnostics=DIAG), None),
    "I54": (Run().copy_intent().end(S.CANCELLED, NA, NONE, "cancelled"), None),
    "I55": (Run().copy_intent().end(S.SKIPPED, NA, NONE, TARGET_EXISTS), None),
    "I55_not_committed": (Run().copy_intent().end(S.SKIPPED, NC, REMOVED, TARGET_EXISTS), None),
    "I56": (staged().end(S.SKIPPED, NA, REMOVED, TEMP_NAME_TAKEN), None),
    "I57": (staged().end(S.FAILED, NA, REMOVED, "x", diagnostics=DIAG), None),
    "I57_none": (Run().copy_intent().end(S.FAILED, NA, NONE, "x", diagnostics=DIAG), None),
    "I58": (Run().copy_intent().end(S.SKIPPED, NA, REMOVED, TEMP_NAME_TAKEN), None),
    "I59": (full().end(S.SKIPPED, NC, REMOVED, TEMP_NAME_TAKEN), None),
    "I60_next_intent": (staged().copy_intent(source=OTHER), None),
    "I60_run_finished": (staged().finished(), None),
    "I61": (Run().unstarted(size=1), None),
    "first_not_run_started": (Run(started=False).copy_intent(), None),
    "second_run_started": (
        Run().mkdir(),
        mutate(2, operation="run", status="started", intent=None, target=None, **RUN_JSON),
    ),
    "after_run_finished": (after_finished(), None),
    "run_finished_twice": (Run().finished().finished(), None),
    "run_without_volumes": (Run(), mutate(0, source_volume=None)),
    "run_finished_other_volume": (Run().finished(target_volume=OTHER_VOLUME), None),
    "intent_volume_not_run": (Run().copy_intent(source_volume=OTHER_VOLUME), None),
    "unstarted_volume_not_run": (Run().unstarted(target_volume=OTHER_VOLUME), None),
    "temp_of_other_sequence": (Run().copy_intent(temp=temp_name(RUN_ID, 9)), None),
    "temp_of_other_run": (Run().copy_intent(temp=temp_name(OTHER_RUN, 2)), None),
    "temp_bad_pattern": (Run().copy_intent(temp="a.partial"), None),
    "intent_without_size": (Run().copy_intent(size=None), None),
    "negative_size": (Run().copy_intent(size=-1), None),
    "bool_size": (Run().copy_intent(), mutate(1, size=True)),
    "intent_with_reason": (Run().copy_intent(reason="x"), None),
    "phase_with_reason": (Run().copy_intent().copy(S.STAGED, reason="x"), None),
    "phase_with_diagnostics": (Run().copy_intent().copy(S.STAGED, diagnostics=DIAG), None),
    "staged_without_temp_id": (Run().copy_intent().copy(S.STAGED, temp_id=None), None),
    "staged_temp_state_none": (Run().copy_intent().copy(S.STAGED, temp_state=NONE), None),
    "done_with_reason": (full().copy(S.DONE, reason="x"), None),
    "verified_twice": (verified().copy(S.VERIFIED), None),
    "phase_after_commit_attempted": (full().copy(S.VERIFIED), None),
    "failed_after_staged_temp_none": (staged().end(S.FAILED, NA, NONE, "x"), None),
    "failed_not_committed_before_commit": (staged().end(S.FAILED, NC, REMOVED, "x"), None),
    "failed_not_attempted_after_commit": (full().end(S.FAILED, NA, REMOVED, "x"), None),
    "failed_without_reason": (Run().copy_intent().end(S.FAILED, NA, REMOVED, None), None),
    "failed_without_written_sha": (written().end(S.FAILED, NA, REMOVED, "x", sha256=None), None),
    "rr_committed_without_target_id": (full().end(RRS, CC, TC, "x", diagnostics=DIAG), None),
    "rr_unknown_with_target_id": (full().end(RRS, CU, UNKNOWN, "x", target_id=TEMP_ID), None),
    "rr_without_reason": (full().end(RRS, CU, UNKNOWN, None), None),
    "skipped_other_reason": (Run().copy_intent().end(S.SKIPPED, NA, NONE, "other"), None),
    "unstarted_temp_name_taken": (Run().unstarted(reason=TEMP_NAME_TAKEN), None),
    "unstarted_with_temp": (Run().unstarted(temp=temp_name(RUN_ID, 2)), None),
    "unstarted_without_reason": (Run().unstarted(), mutate(1, reason=None)),
    "unstarted_left": (Run().unstarted(temp_state=LEFT, diagnostics=DIAG), None),
    "copy_without_source": (Run().copy_intent(source=None), None),
    "mkdir_with_volume": (Run().mkdir(), mutate(1, target_volume=TV.to_json())),
    "mkdir_with_commit": (Run().mkdir(), mutate(2, commit="committed")),
    # stricter than v1: a mkdir intent is followed by its own outcome at once
    "mkdir_split_by_copy": (mkdir_split(), None),
    "mkdir_other_target": (mkdir_other_target(), None),
    "mkdir_outcome_without_intent": (mkdir_outcome_alone(), None),
    "diagnostics_unknown_key": (
        Run().copy_intent().end(S.FAILED, NA, LEFT, "x"),
        mutate(2, diagnostics={"x": None}),
    ),
    "diagnostics_bad_flag": (
        Run().copy_intent().end(S.FAILED, NA, LEFT, "x"),
        mutate(2, diagnostics=BAD_FLAG),
    ),
    "run_id_not_hex": (Run(), mutate(0, run_id="run")),
    "unknown_status": (Run().copy_intent(), mutate(1, status="moved")),
    "not_an_object": (Run(), appended(1, [1])),
}
# Changed after they were built: the writer cannot produce these at all.
READER_ONLY = {name for name, (_, change) in INVALID.items() if change is not None}


def invalid_records(name: str) -> list[dict[str, object]]:
    run, change = INVALID[name]
    records = build(run.steps)
    if change is not None:
        change.apply(records)
    return records


def first_bad(name: str) -> int:
    """The index of the record that makes example ``name`` invalid, fixed by how it is
    built (not by the reader): the changed record, else the last writer call."""
    run, change = INVALID[name]
    return len(run.steps) - 1 if change is None else change.index


@pytest.mark.parametrize("name", list(INVALID))
def test_invalid_example_distrusts_the_whole_journal(name: str) -> None:
    records = invalid_records(name)
    bad = first_bad(name)
    # A4: the records before it are a valid journal, the first problem is its line
    assert bad == 0 or analyze_v2(records[:bad], run_id=RUN_ID).problems == ()
    report = analyze_v2(records, run_id=RUN_ID)
    assert report.problems and report.problems[0].line == bad + 1
    assert report.review_whole_journal and report.needs_review
    assert not report.finished
    for entry in report.entries:
        assert entry.state is OperationState.RECOVERY_REQUIRED
        assert entry.outcome is None
        assert entry.problems
        if entry.operation == "copy":
            assert {ReviewFlag.TEMP_MAY_EXIST, ReviewFlag.COMMIT_UNKNOWN} <= entry.flags


@pytest.mark.parametrize("name", sorted(set(INVALID) - READER_ONLY))
def test_writer_refuses_exactly_where_the_reader_does(name: str, fsyncs: list[int]) -> None:
    steps = INVALID[name][0].steps
    records = build(steps)
    bad = first_bad(name)
    assert bad == len(steps) - 1
    stream = Stream()
    journal = writer(stream)
    replay(journal, steps[:bad])
    with pytest.raises(JournalError, match="invalid record") as info:
        replay(journal, steps[bad : bad + 1])
    assert isinstance(info.value.__cause__, ValueError)
    assert stream.getvalue() == encode(records[:bad])  # nothing of the refused record
    assert journal.state is WriterState.FAILED and journal.failed
    assert journal.uncertain_sequence is None
    assert len(journal.history) == bad
    writes = stream.writes
    with pytest.raises(JournalError, match="unusable"):
        replay(journal, steps[:1])
    assert stream.writes == writes and len(fsyncs) == bad


def test_record_errors_name_the_rule() -> None:
    assert record_errors(invalid_records("I33")[1]) == ["missing fields temp_state"]
    assert record_errors(invalid_records("I34")[1]) == ["unknown fields 'extra'"]
    assert record_errors([]) == ["not a JSON object"]
    assert any("transfer" in e for e in record_errors(invalid_records("I27")[1]))


ENUM_FIELDS = ("operation", "status", "commit", "temp_state")
WRONG_TYPES = [[], {}, ["copy"], {"copy": 1}, 1, 1.5, True, None, ""]


@pytest.mark.parametrize("value", WRONG_TYPES, ids=repr)
@pytest.mark.parametrize("name", ENUM_FIELDS)
@pytest.mark.parametrize("index", [1, 2], ids=["intent", "staged"])
def test_wrong_type_of_an_enum_field_distrusts_the_journal(
    index: int, name: str, value: object, tmp_path: Path
) -> None:  # R3B2-M01: an array or object is unhashable and must not raise TypeError
    records = build(staged().steps)
    records[index][name] = value
    errors = record_errors(records[index])
    assert errors and all(isinstance(e, str) for e in errors)
    report = analyze_v2(records, run_id=RUN_ID)
    assert report.review_whole_journal and report.problems[0].line == index + 1
    for entry in report.entries:
        assert entry.state is OperationState.RECOVERY_REQUIRED and entry.outcome is None
    from_file = read_journal_v2(write_file(tmp_path, encode(records)))
    assert from_file.review_whole_journal and from_file.problems == report.problems
    assert all(e.state is OperationState.RECOVERY_REQUIRED for e in from_file.entries)


@pytest.mark.parametrize("value", [[], {}, 1], ids=repr)
def test_wrong_type_in_a_matrix_choice_is_refused(value: object) -> None:
    records = build(full().end(RRS, CU, UNKNOWN, "x").steps)
    records[-1]["temp_state"] = value  # a frozenset choice of the recovery_required row
    assert record_errors(records[-1])
    assert analyze_v2(records, run_id=RUN_ID).review_whole_journal


def test_v1_reader_rejects_copy_and_v2_records() -> None:  # I11, §6.1
    copy_v1 = dict.fromkeys(v2.FIELDS) | {
        "schema_version": 1,
        "run_id": RUN_ID,
        "sequence": 2,
        "timestamp_utc": STAMP,
        "operation": "copy",
        "status": "intent",
        "source": "D:\\a",
        "target": "E:\\a",
    }
    started = {**copy_v1, "sequence": 1, "operation": "run", "status": "started"}
    assert analyze([started]).problems == ()
    assert analyze([started, copy_v1]).problems
    report = analyze(build(_v1().steps))
    assert report.review_whole_journal and report.problems


def test_run_id_comes_from_run_started_without_a_file_name() -> None:
    records = build(_v1().steps, run_id=OTHER_RUN)
    for record in records:
        if isinstance(record["temp"], str):
            record["temp"] = record["temp"].replace(RUN_ID[:12], OTHER_RUN[:12])
    assert analyze_v2(records).problems == ()
    assert analyze_v2(records, run_id=RUN_ID).problems


def test_first_problem_is_the_first_invalid_record() -> None:
    records = build(_v1().steps)
    records[1]["reason"] = "free text"  # a mkdir reason is free, as in v1
    records[2]["target"] = "E:\\elsewhere"
    report = analyze_v2(records)
    assert report.problems[0].line == 3
    assert "target 'E:\\\\elsewhere' differs" in report.problems[0].message


def test_untrusted_journal_never_says_the_temporary_file_is_gone() -> None:
    records = build(VALID["V6"][0]().steps)
    records.append(records[-1])  # a repeated sequence (I36)
    entry = last_entry(analyze_v2(records))
    assert entry.state is OperationState.RECOVERY_REQUIRED
    assert entry.outcome is None
    assert {ReviewFlag.TEMP_MAY_EXIST, ReviewFlag.COMMIT_UNKNOWN} <= entry.flags


# --- the writer: one transaction ------------------------------------------------------------


def good_steps() -> list[Step]:
    """run started, copy intent, staged: the failure is injected into record 2."""
    return Run().copy_intent().phases(S.STAGED).steps


class Boom(Exception):
    pass


class FailingFlush(Stream):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def flush(self) -> None:
        if self.writes > 1 and not self.closed:
            raise self.error


class FailingWrite(Stream):
    """The first write succeeds; later ones write a prefix and fail."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def write(self, data: object) -> int:  # type: ignore[override]
        self.writes += 1
        if self.writes == 1:
            return io.BytesIO.write(self, data)  # type: ignore[arg-type]
        io.BytesIO.write(self, bytes(data)[:5])  # type: ignore[call-overload]
        raise self.error


class OddWrite(Stream):
    """The first write succeeds; a later one stores ``kept`` bytes of its data (all if
    None) and returns ``result(data)`` instead of the count."""

    def __init__(self, kept: int | None, result: Callable[[bytes], object]) -> None:
        super().__init__()
        self.kept = kept
        self.result = result

    def write(self, data: object) -> int:  # type: ignore[override]
        self.writes += 1
        if self.writes == 1:
            return io.BytesIO.write(self, data)  # type: ignore[arg-type]
        data = bytes(data)  # type: ignore[call-overload]
        io.BytesIO.write(self, data if self.kept is None else data[: self.kept])
        return self.result(data)  # type: ignore[return-value]


class FailingHistory(list):  # type: ignore[type-arg]
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def append(self, item: object) -> None:
        if len(self) >= 1:
            raise self.error
        super().append(item)


Prepared = tuple[JournalV2, Stream, bool]  # the journal after record 1, bytes may be out


def _ready(stream: Stream) -> JournalV2:
    journal = writer(stream)
    replay(journal, good_steps()[:1])
    return journal


def at_build(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> Prepared:
    stream = Stream()
    journal = _ready(stream)

    def failing() -> datetime:
        raise error

    journal._clock = failing
    return journal, stream, False


def at_validate(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> Prepared:
    stream = Stream()
    journal = _ready(stream)

    def failing(*args: object) -> object:
        raise error

    monkeypatch.setattr(v2, "_step", failing)
    return journal, stream, False


def at_encode(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> Prepared:
    stream = Stream()
    journal = _ready(stream)

    def failing(*args: object, **kw: object) -> str:
        raise error

    monkeypatch.setattr(v2, "json", types.SimpleNamespace(dumps=failing))
    return journal, stream, False


def at_write(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> Prepared:
    stream = FailingWrite(error)
    return _ready(stream), stream, True


def at_flush(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> Prepared:
    stream = FailingFlush(error)
    return _ready(stream), stream, True


def at_fsync(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> Prepared:
    stream = Stream()
    journal = _ready(stream)

    def failing(fd: int) -> None:
        raise error

    monkeypatch.setattr(v2, "os", types.SimpleNamespace(fsync=failing))
    return journal, stream, True


def at_history(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> Prepared:
    stream = Stream()
    journal = writer(stream)
    journal._history = FailingHistory(error)
    replay(journal, good_steps()[:1])
    return journal, stream, True


POINTS: dict[str, Callable[[pytest.MonkeyPatch, BaseException], Prepared]] = {
    "build": at_build,
    "validate": at_validate,
    "encode": at_encode,
    "write": at_write,
    "flush": at_flush,
    "fsync": at_fsync,
    "history": at_history,
}
ERRORS = [OSError(28, "disk full"), ValueError("v"), RuntimeError("r"), MemoryError()]


@pytest.mark.parametrize("point", list(POINTS))
@pytest.mark.parametrize("error", ERRORS, ids=lambda e: type(e).__name__)
def test_ordinary_failure_makes_the_writer_failed(
    point: str, error: Exception, monkeypatch: pytest.MonkeyPatch, fsyncs: list[int]
) -> None:
    journal, stream, bytes_out = POINTS[point](monkeypatch, error)
    with pytest.raises(JournalError, match="cannot write journal record 2") as info:
        replay(journal, good_steps()[1:])
    assert info.value.__cause__ is error
    assert journal.state is WriterState.FAILED and journal.failed
    assert journal.error is not None and journal.error.startswith(type(error).__name__)
    assert journal.uncertain_sequence == (2 if bytes_out else None)
    assert len(journal.history) == 1
    if not bytes_out:
        assert stream.getvalue() == encode(build(good_steps()[:1]))
    writes, flushes, synced = stream.writes, stream.flushes, len(fsyncs)
    with pytest.raises(JournalError, match="unusable"):
        replay(journal, good_steps()[1:])
    assert (stream.writes, stream.flushes, len(fsyncs)) == (writes, flushes, synced)


@pytest.mark.parametrize("point", list(POINTS))
@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
def test_interrupt_makes_the_writer_untrusted_and_propagates_itself(
    point: str, kind: type[BaseException], monkeypatch: pytest.MonkeyPatch, fsyncs: list[int]
) -> None:
    interrupt = kind()
    journal, stream, _ = POINTS[point](monkeypatch, interrupt)
    with pytest.raises(kind) as info:
        replay(journal, good_steps()[1:])
    assert info.value is interrupt
    assert journal.state is WriterState.UNTRUSTED and journal.failed
    assert journal.uncertain_sequence == 2
    assert journal.error == f"interrupted while writing record 2: {kind.__name__}"
    writes, flushes, synced = stream.writes, stream.flushes, len(fsyncs)
    with pytest.raises(JournalError, match="unusable"):
        replay(journal, good_steps()[1:])
    assert (stream.writes, stream.flushes, len(fsyncs)) == (writes, flushes, synced)


# (bytes stored, what write returns): only ``len(data)`` as an int proves the write
INCOMPLETE = {
    "nothing_returns_0": (0, lambda data: 0),
    "prefix_returns_3": (3, lambda data: 3),
    "all_returns_3": (None, lambda data: 3),
    "nothing_returns_none": (0, lambda data: None),  # R3B2-M02: no data, yet no error
    "all_returns_none": (None, lambda data: None),
    "all_returns_true": (None, lambda data: True),
    "all_returns_false": (None, lambda data: False),
    "all_returns_float": (None, lambda data: float(len(data))),
    "all_returns_text": (None, lambda data: str(len(data))),
    "all_returns_more": (None, lambda data: len(data) + 1),
}


@pytest.mark.parametrize("case", list(INCOMPLETE))
def test_incomplete_write_fails_without_retry(case: str, fsyncs: list[int]) -> None:
    kept, result = INCOMPLETE[case]
    stream = OddWrite(kept, result)
    journal = _ready(stream)
    first = encode(build(good_steps()[:1]))
    with pytest.raises(JournalError, match="incomplete write") as info:
        replay(journal, good_steps()[1:])
    assert isinstance(info.value.__cause__, OSError)
    assert (stream.writes, stream.flushes, len(fsyncs)) == (2, 1, 1)  # no flush, no fsync
    assert journal.state is WriterState.FAILED and journal.failed
    assert journal.uncertain_sequence == 2 and len(journal.history) == 1
    if kept == 0:
        assert stream.getvalue() == first  # nothing of record 2 reached the stream
    with pytest.raises(JournalError, match="unusable"):
        replay(journal, good_steps()[1:])
    assert (stream.writes, stream.flushes, len(fsyncs)) == (2, 1, 1)


def test_writer_is_not_ready_while_a_write_is_under_way(fsyncs: list[int]) -> None:
    seen: list[tuple[WriterState, bool]] = []
    journal = _ready(Stream())

    def fsync(fd: int) -> None:
        seen.append((journal.state, journal.failed))

    v2.os = types.SimpleNamespace(fsync=fsync)  # restored by the fsyncs fixture
    replay(journal, good_steps()[1:])
    assert seen == [(WriterState.WRITING, True)] * 2
    assert journal.state is WriterState.READY


def test_wrong_values_fail_before_any_byte(fsyncs: list[int]) -> None:
    bad_values: list[dict[str, object]] = [
        {"source_id": -1},
        {"temp_id": "x"},
        {"source_volume": "C:"},
        {"commit": "maybe"},
        {"temp_state": "gone"},
    ]
    for kw in bad_values:
        stream = Stream()
        journal = _ready(stream)
        operation, status, base = good_steps()[1]
        with pytest.raises(JournalError):
            journal.write(operation, status, **(base | kw))  # type: ignore[arg-type]
        assert journal.state is WriterState.FAILED and journal.uncertain_sequence is None
        assert stream.writes == 1
    with pytest.raises(ValueError):
        VolumeRef("", 1)
    with pytest.raises(ValueError):
        VolumeRef("d", 1 << 64)


def test_move_is_refused_by_the_writer(fsyncs: list[int]) -> None:  # I10 / I32
    journal = _ready(Stream())
    with pytest.raises(JournalError):
        journal.write("move", S.INTENT, source=SRC, target=DST)  # type: ignore[arg-type]
    assert journal.state is WriterState.FAILED


def test_close_is_allowed_in_every_state(
    fsyncs: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = writer()
    journal.close()
    assert journal.state is WriterState.CLOSED and not journal.failed
    with pytest.raises(JournalError, match="closed"):
        replay(journal, good_steps()[:1])
    for error, state in ((OSError("f"), WriterState.FAILED), (SystemExit(), WriterState.UNTRUSTED)):
        journal, stream, _ = at_flush(monkeypatch, error)
        with pytest.raises((JournalError, SystemExit)):
            replay(journal, good_steps()[1:])
        journal.close()
        assert journal.state is state and stream.closed


def test_context_manager_closes_and_keeps_the_leaving_error(fsyncs: list[int]) -> None:
    stream = Stream()
    with pytest.raises(Boom), JournalV2(stream, run_id=RUN_ID, clock=clock) as journal:
        replay(journal, good_steps()[:1])
        raise Boom
    assert stream.closed


def test_created_journal_is_read_back(tmp_path: Path) -> None:
    journal = JournalV2.create(tmp_path / "logs")
    try:
        assert journal.path is not None and journal.path.parent == tmp_path / "logs"
        assert journal.run_id in journal.path.name
        for sequence, (operation, status, kw) in enumerate(_v1().steps, start=1):
            if "temp" in kw:
                intent = kw.get("intent") or sequence
                kw = kw | {"temp": temp_name(journal.run_id, intent)}  # type: ignore[arg-type]
            journal.write(operation, status, **kw)  # type: ignore[arg-type]
    finally:
        journal.close()
    report = read_journal_v2(journal.path)
    assert report.problems == () and report.finished and not report.needs_review
    assert [e.state for e in report.entries] == [OperationState.DONE, OperationState.DONE]
    assert isinstance(read_any_journal(journal.path), JournalReportV2)
    old = read_journal(journal.path)  # the v1 reader trusts no v2 record
    assert old.review_whole_journal and old.problems


# --- reading files --------------------------------------------------------------------------


def write_file(tmp_path: Path, data: bytes, name: str = NAME) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def assert_untrusted(report: JournalReportV2) -> None:
    assert report.review_whole_journal and not report.finished and report.needs_review
    assert all(e.state is OperationState.RECOVERY_REQUIRED for e in report.entries)


def test_file_of_a_valid_journal(tmp_path: Path) -> None:
    report = read_journal_v2(write_file(tmp_path, encode(build(_v4().steps))))
    assert report.problems == () and report.finished and report.unreadable == ()
    assert report.recovery_needed == report.entries


@pytest.mark.parametrize("cut", [1, 40, -2, -1])
def test_cut_off_last_line_is_unreadable(tmp_path: Path, cut: int) -> None:
    data = encode(build(_v1().steps))
    report = read_journal_v2(write_file(tmp_path, data[:cut]))
    assert report.unreadable[-1][1] == data[:cut].rsplit(b"\n", 1)[-1]
    assert_untrusted(report)


def test_last_line_without_line_end_is_unreadable_even_as_json(tmp_path: Path) -> None:
    data = encode(build(VALID["V11"][0]().steps))
    report = read_journal_v2(write_file(tmp_path, data.rstrip(b"\n")))
    assert [p.line for p in report.problems] == [2]
    assert len(report.records) == 1
    assert_untrusted(report)


@pytest.mark.parametrize("line", [b"\n", b"{not json}\n", b"\xff\xfe\n", b'{"a":1,"a":2}\n'])
def test_damaged_line_in_the_middle(tmp_path: Path, line: bytes) -> None:
    lines = encode(build(_v1().steps)).splitlines(keepends=True)
    report = read_journal_v2(write_file(tmp_path, b"".join([*lines[:3], line, *lines[3:]])))
    assert report.unreadable == ((4, line),)
    assert_untrusted(report)


def test_empty_file(tmp_path: Path) -> None:
    report = read_journal_v2(write_file(tmp_path, b""))
    assert [p.message for p in report.problems] == ["the journal has no records"]
    assert report.review_whole_journal and report.post_crash_unverified


def test_file_name_fixes_the_run(tmp_path: Path) -> None:
    data = encode(build(_v1().steps))
    name = f"run-20260101T000000000000Z-{OTHER_RUN}.jsonl"
    assert_untrusted(read_journal_v2(write_file(tmp_path, data, name)))
    assert read_journal_v2(write_file(tmp_path, data, "copied.jsonl")).problems == ()


def test_v2_reader_on_a_v1_journal(tmp_path: Path) -> None:
    path = tmp_path / NAME
    with path.open("wb") as stream:
        log = Journal(stream, run_id=RUN_ID, clock=clock)
        log.write(Operation.RUN, Status.STARTED, source=Path("C:/In"))
        log.write(Operation.RUN, Status.FINISHED)
    assert_untrusted(read_journal_v2(path))
    assert isinstance(read_any_journal(path), JournalReport)


FIRST_LINES = [
    b"",
    b"garbage\n",
    b'{"schema_version":true}\n',
    b'{"schema_version":"2"}\n',
    b'{"schema_version":2.0}\n',
]


@pytest.mark.parametrize("first", FIRST_LINES)
def test_dispatcher_falls_back_to_v1(tmp_path: Path, first: bytes) -> None:
    assert isinstance(read_any_journal(write_file(tmp_path, first)), JournalReport)
