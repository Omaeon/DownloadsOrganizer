"""Journal schema v3: the JSONL journal of a cross-volume COPY run that may create its
category directories (R3B end-to-end integration).

v1 (``journal``) and v2 (``journal_v2``) are unchanged: their writers, readers and
formats stay as they are, and this module only reuses their checks. A copy run writes
v3; the schema is fixed by the writer (``JournalV3``) before run/started is written, and
a journal never mixes versions.

A v3 record is a v2 record (every v2 field and rule, ``journal_v2.record_errors`` on the
record seen as v2) plus three fields that only ``mkdir`` records use:

* ``parent_id``: the 128-bit file id (32 hex digits) of the directory the category is
  created in, proven from its handle before the intent is written; set in every mkdir
  record, equal in its whole block, null in every other record;
* ``dir_state``: what the create call established (``aclcreate.Created``), null in the
  intent and set in its terminal record: ``created`` or ``existing`` (``done``),
  ``refused`` or ``removed`` (``failed``), ``unchecked``, ``left``, ``removal_unknown``
  or ``unknown`` (``recovery_required``); no other mkdir terminal exists in v3;
* ``dir_id``: the new (or existing) directory's file id: set for ``created`` and
  ``existing``, null for ``refused`` and ``unknown`` and in the intent, either for the
  others (it is known only once the object was queried).

A mkdir intent is durable before the create call is made. A mkdir block is open until
its terminal record; nothing else may be written inside it (the v2 grammar), so a
journal cut off after an intent says the directory may exist (``UNCERTAIN``). As in v2,
a damaged, cut-off or out-of-place record distrusts the whole journal. The reader only
informs and never repairs, recovers or removes anything.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC
from enum import StrEnum
from pathlib import Path

from downloads_organizer import journal_v2
from downloads_organizer.journal import (
    _FILE_NAME,
    JournalError,
    JournalProblem,
    JournalReport,
    _pairs,
    _positive,
    _text,
    read_journal,
)
from downloads_organizer.journal_v2 import (
    _HEX32,
    FIELDS_V2,
    SOURCE_ACTION,
    TRANSFER,
    Commit,
    Diagnostics,
    EntryV2,
    JournalReportV2,
    JournalV2,
    OperationV2,
    StatusV2,
    TempState,
    VolumeRef,
    WriterState,
    _Context,
    _Entry,
    _hex,
    _id,
    _member,
    read_journal_v2,
)

SCHEMA_VERSION = 3
DIR_FIELDS = ("parent_id", "dir_state", "dir_id")
FIELDS_V3 = (*FIELDS_V2, *DIR_FIELDS)


class DirState(StrEnum):
    """``aclcreate.Created`` of a category directory, plus ``existing``."""

    CREATED = "created"  # created by this run, held, every check passed
    EXISTING = "existing"  # the name was taken: an existing directory, opened and checked
    REFUSED = "refused"  # proven: nothing was created
    REMOVED = "removed"  # created, a check failed, its removal proven
    UNCHECKED = "unchecked"  # created, its checks cut off: it is there
    LEFT = "left"  # created, a check failed, it is still there
    REMOVAL_UNKNOWN = "removal_unknown"  # created, its removal begun: gone or not, unknown
    UNKNOWN = "unknown"  # what the create call did is not known


_DIR_STATUS = {
    DirState.CREATED: "done",
    DirState.EXISTING: "done",
    DirState.REFUSED: "failed",
    DirState.REMOVED: "failed",
    DirState.UNCHECKED: "recovery_required",
    DirState.LEFT: "recovery_required",
    DirState.REMOVAL_UNKNOWN: "recovery_required",
    DirState.UNKNOWN: "recovery_required",
}
_DIR_ID_SET = frozenset({DirState.CREATED, DirState.EXISTING})
_DIR_ID_NULL = frozenset({DirState.REFUSED, DirState.UNKNOWN})


# --- the record on its own ------------------------------------------------------------


def record_errors(record: object) -> list[str]:
    """What is wrong with the v3 ``record`` taken alone; empty if it is well formed."""
    if not isinstance(record, Mapping):
        return ["not a JSON object"]
    missing = [name for name in FIELDS_V3 if name not in record]
    unknown = sorted(repr(key) for key in record if key not in FIELDS_V3)
    if missing or unknown:
        return [
            *([f"missing fields {', '.join(missing)}"] if missing else []),
            *([f"unknown fields {', '.join(unknown)}"] if unknown else []),
        ]
    schema = record["schema_version"]
    if type(schema) is not int or schema != SCHEMA_VERSION:
        return [f"unsupported schema_version {schema!r}"]
    view = {name: record[name] for name in FIELDS_V2}
    view["schema_version"] = journal_v2.SCHEMA_VERSION
    errors = journal_v2.record_errors(view)
    if errors:
        return errors
    parent_id, state, dir_id = (record[name] for name in DIR_FIELDS)
    if record["operation"] != OperationV2.MKDIR:
        extra = [name for name in DIR_FIELDS if record[name] is not None]
        return [f"{name} {record[name]!r} outside mkdir" for name in extra]
    status = str(record["status"])
    if not _hex(_HEX32, parent_id):
        errors.append(f"invalid parent_id {parent_id!r} in mkdir {status}")
    if dir_id is not None and not _hex(_HEX32, dir_id):
        errors.append(f"invalid dir_id {dir_id!r}")
    if status == "intent":
        if state is not None or dir_id is not None:
            errors.append("dir_state and dir_id are null in a mkdir intent")
        return errors
    if not _member(state, DirState):
        return [*errors, f"invalid dir_state {state!r} in mkdir {status}"]
    dir_state = DirState(str(state))
    if status != _DIR_STATUS[dir_state]:
        errors.append(f"mkdir {status} with dir_state {state!r}")
    if dir_state in _DIR_ID_SET and dir_id is None:
        errors.append(f"dir_id null with dir_state {state!r}")
    if dir_state in _DIR_ID_NULL and dir_id is not None:
        errors.append(f"dir_id set with dir_state {state!r}")
    if status != "done" and not _text(record["reason"]):
        errors.append(f"mkdir {status} without a reason")
    return errors


# --- the record in its journal ----------------------------------------------------------


def _step(context: _Context, record: object) -> tuple[_Context, list[str]]:
    """``journal_v2._step`` with the v3 record checks and, in a mkdir block, the same
    ``parent_id`` as its intent."""
    errors = record_errors(record)
    if not errors:
        assert isinstance(record, Mapping)
        errors = journal_v2._context_errors(context, record)
        block = context.block
        mkdir = bool(block) and block[0]["operation"] == OperationV2.MKDIR
        if not errors and mkdir and record["parent_id"] != block[0]["parent_id"]:
            errors.append(f"parent_id {record['parent_id']!r} differs from its intent's")
        if not errors:
            return journal_v2._advance(context, record), []
    return context, errors


# --- writing ----------------------------------------------------------------------------


class JournalV3(JournalV2):
    """``JournalV2`` writing schema 3: the same transaction (number, build, validate
    against the journal so far, encode, write, flush, ``fsync``, then update), the same
    states, ``create`` and ``close``; ``write`` also takes the mkdir fields."""

    def write(
        self,
        operation: OperationV2,
        status: StatusV2,
        *,
        intent: int | None = None,
        source: Path | None = None,
        target: Path | None = None,
        reason: str | None = None,
        error_code: int | None = None,
        error_message: str | None = None,
        source_volume: VolumeRef | None = None,
        target_volume: VolumeRef | None = None,
        temp: str | None = None,
        size: int | None = None,
        sha256: str | None = None,
        source_id: int | None = None,
        temp_id: int | None = None,
        target_id: int | None = None,
        commit: Commit | None = None,
        temp_state: TempState | None = None,
        diagnostics: Diagnostics | None = None,
        parent_id: int | None = None,
        dir_state: DirState | None = None,
        dir_id: int | None = None,
    ) -> int:
        """Append one record and make it durable; returns its ``sequence``
        (``JournalV2.write``; a record the reader would not accept is refused)."""
        if self._state is not WriterState.READY:
            raise JournalError(f"journal unusable ({self._state}): {self.error or 'closed'}")
        self._state = WriterState.WRITING
        sequence: int | None = None
        bytes_may_be_written = False
        try:
            sequence = self._sequence + 1
            operation = OperationV2(operation)
            record: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "sequence": sequence,
                "timestamp_utc": self._clock().astimezone(UTC).isoformat(timespec="microseconds"),
                "operation": str(operation),
                "status": str(StatusV2(status)),
                "intent": intent,
                "source": None if source is None else str(source),
                "target": None if target is None else str(target),
                "reason": None if reason is None else str(reason),
                "error_code": error_code,
                "error_message": error_message,
                "transfer": TRANSFER,
                "source_volume": None if source_volume is None else source_volume.to_json(),
                "target_volume": None if target_volume is None else target_volume.to_json(),
                "temp": temp,
                "size": size,
                "sha256": sha256,
                "source_id": _id(source_id),
                "temp_id": _id(temp_id),
                "target_id": _id(target_id),
                "source_action": SOURCE_ACTION if operation is OperationV2.COPY else None,
                "commit": None if commit is None else str(Commit(commit)),
                "temp_state": None if temp_state is None else str(TempState(temp_state)),
                "diagnostics": None if diagnostics is None else diagnostics.to_json(),
                "parent_id": _id(parent_id),
                "dir_state": None if dir_state is None else str(DirState(dir_state)),
                "dir_id": _id(dir_id),
            }
            context, errors = _step(self._context, record)
            if errors:
                raise ValueError(f"invalid record: {'; '.join(errors)}")
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            # A lone surrogate (possible in Windows names) becomes a JSON \uXXXX escape.
            data = line.encode("utf-8", errors="backslashreplace")
            stream = self._file._stream
            if stream is None:
                raise ValueError("the journal file is closed")
            bytes_may_be_written = True
            written = stream.write(data)
            # Only the full count proves the write (``JournalV2.write``); nothing is retried.
            if type(written) is not int or written != len(data):
                raise OSError(f"incomplete write: {written!r} of {len(data)} bytes")
            stream.flush()
            os.fsync(stream.fileno())
            self._context = context
            self._sequence = sequence
            self._history.append(record)
            self._state = WriterState.READY  # last: only a complete write leaves it ready
        except Exception as exc:
            self._state = WriterState.FAILED
            self.error = f"{type(exc).__name__}: {exc}"
            self.uncertain_sequence = sequence if bytes_may_be_written else None
            raise JournalError(f"cannot write journal record {sequence}: {exc}") from exc
        except BaseException as exc:  # an interrupt: the record may be anywhere in between
            self._state = WriterState.UNTRUSTED
            self.error = f"interrupted while writing record {sequence}: {type(exc).__name__}"
            self.uncertain_sequence = sequence
            raise
        return sequence

    def open_block(self) -> Mapping[str, object] | None:
        """The intent of the block still open (no terminal record yet), if any."""
        block = self._context.block
        return block[0] if block else None


# --- reading ----------------------------------------------------------------------------

MAY_EXIST = "the category directory may have been created; check it by hand"
_STATE_NOTES = {
    "unchecked": "the category directory was created and not fully checked; it is there",
    "left": "the category directory was created, failed a check and is still there",
    "removal_unknown": "the category directory was created; whether it was removed is unknown",
    "unknown": "what creating the category directory did is unknown; it may exist",
}


@dataclass(frozen=True, slots=True)
class JournalReportV3(JournalReportV2):
    """A ``JournalReportV2`` of a v3 journal (the same entries and properties)."""


def _mkdir_notes(entry: EntryV2) -> EntryV2:
    """A mkdir entry says what may be left of the directory: an open intent, a distrusted
    block or a state that is not proven."""
    if entry.operation != OperationV2.MKDIR:
        return entry
    notes: list[str] = []
    if entry.outcome is None:
        notes.append(MAY_EXIST)
    else:
        note = _STATE_NOTES.get(str(entry.outcome["dir_state"]))
        if note is not None:
            notes.append(note)
    return replace(entry, notes=(*entry.notes, *notes)) if notes else entry


def read_journal_v3(path: Path) -> JournalReportV3:
    """Parse a v3 journal file (``journal_v2.read_journal_v2``, the v3 checks): a line
    that cannot be read, or a last line without its line end, distrusts the journal."""
    numbered: list[tuple[int, object]] = []
    problems: list[JournalProblem] = []
    unreadable: list[tuple[int, bytes]] = []
    with path.open("rb") as stream:
        for number, raw in enumerate(stream, start=1):
            if not raw.endswith(b"\n"):
                problems.append(JournalProblem(number, "incomplete last line, content unknown"))
                unreadable.append((number, raw))
                continue
            try:
                numbered.append((number, json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)))
            except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                problems.append(JournalProblem(number, f"unreadable, content unknown ({exc})"))
                unreadable.append((number, raw))
    match = _FILE_NAME.fullmatch(path.name)
    return _analyze(numbered, problems, match.group(1) if match else None, unreadable)


def analyze_v3(records: Iterable[object], *, run_id: str | None = None) -> JournalReportV3:
    """Check v3 ``records`` (in order) and classify every change of the run."""
    return _analyze(list(enumerate(records, start=1)), [], run_id, [])


def _analyze(
    numbered: list[tuple[int, object]],
    general: list[JournalProblem],
    run_id: str | None,
    unreadable: list[tuple[int, bytes]],
) -> JournalReportV3:
    # ``journal_v2._analyze`` with the v3 ``_step`` and the mkdir notes.
    context = _Context(run_id)
    entries: list[_Entry] = []
    blocks: dict[int, _Entry] = {}  # by the sequence of their intent
    for line, record in numbered:
        context, errors = _step(before := context, record)
        data = record if isinstance(record, Mapping) else {}
        ref, operation = data.get("intent"), data.get("operation")
        if not errors:
            if operation == OperationV2.RUN:
                continue
            if ref is None:  # an intent, or a copy never started
                entry = _Entry(record, line, [record])
                entries.append(entry)
                if data["status"] == "intent":
                    blocks[data["sequence"]] = entry
            else:
                blocks[ref].records.append(record)
            continue
        message = "; ".join(errors)
        general.append(JournalProblem(line, message))
        if _positive(data.get("sequence")):  # judge what follows by its own place
            context = replace(before, sequence=data["sequence"])
        bound = blocks.get(ref) if _positive(ref) else None
        if bound is not None:
            bound.problems.append(f"line {line}: {message}")
        elif operation in (OperationV2.COPY, OperationV2.MKDIR):
            entry = _Entry(record, line, problems=[f"line {line}: {message}"])
            entries.append(entry)
            if data.get("status") == "intent" and _positive(data.get("sequence")):
                blocks.setdefault(data["sequence"], entry)
    if not numbered:
        general.append(JournalProblem(None, "the journal has no records"))
    distrust = bool(general)
    entries.sort(key=lambda e: e.line)
    return JournalReportV3(
        tuple(r for _, r in numbered),
        tuple(_mkdir_notes(e.build(distrust)) for e in entries),
        context.finished and not distrust,
        tuple(general),
        review_whole_journal=distrust,
        unreadable=tuple(unreadable),
    )


def read_any_journal(path: Path) -> JournalReport | JournalReportV2:
    """By the schema of the first record: ``read_journal_v3`` (3), ``read_journal_v2``
    (2), else ``journal.read_journal`` (which distrusts any record that is not v1)."""
    with path.open("rb") as stream:
        first = stream.readline()
    try:
        record = json.loads(first.decode("utf-8"), object_pairs_hook=_pairs)
    except UnicodeDecodeError, ValueError, RecursionError:
        record = None
    schema = record.get("schema_version") if isinstance(record, Mapping) else None
    if type(schema) is int and schema == SCHEMA_VERSION:
        return read_journal_v3(path)
    if type(schema) is int and schema == journal_v2.SCHEMA_VERSION:
        return read_journal_v2(path)
    return read_journal(path)
