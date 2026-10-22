"""Journal schema v2: the JSONL journal of a cross-volume COPY run (R3B-2).

Not used by the executor yet: no COPY exists, and same-volume runs keep writing schema
v1 (``journal``), whose format, writer and reader are unchanged. The schema is chosen
explicitly by using this module's ``JournalV2`` instead of ``journal.Journal``, before
the first record (run/started) is written; a journal never mixes versions.

Every v2 record has every field of v1 plus ``transfer`` (always ``"cross_volume_copy"``),
``source_action`` (``"keep"`` in every copy record, null otherwise), ``source_volume``,
``target_volume``, ``temp``, ``size``, ``sha256``, ``source_id``, ``temp_id``,
``target_id``, ``commit``, ``temp_state`` and ``diagnostics``; a missing or unknown field
makes a record invalid. A copy is one uninterrupted block: ``intent``, then the phases
``staged`` → ``written`` → ``verified`` → ``commit_attempted`` (each at most once, none
skipped), then at most one terminal record (``done``, ``skipped``, ``failed``,
``cancelled`` or ``recovery_required``) allowed after its last phase. Every record of
the block repeats the intent's paths, temporary name, size, source identity, action
and volumes; ``temp_id`` never changes once staged, ``sha256`` never once written, and
``target_id`` (only in ``done`` or a committed ``recovery_required``) is the temporary
file's id. ``commit_attempted`` means the copy reached the point where the commit may
have been made, not that a rename was done.

The writer refuses to write any record the reader would not accept as the next one.
After a write fails or is interrupted it writes nothing more. The reader trusts no
record: anything that breaks the grammar, a damaged or cut-off line, a gap or a
repeated sequence makes the whole journal untrusted (every change
``RECOVERY_REQUIRED``). It only informs and never repairs or recovers anything.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from downloads_organizer import winfileops
from downloads_organizer.journal import (
    _FILE_NAME,
    FIELDS,
    Journal,
    JournalError,
    JournalProblem,
    JournalReport,
    OperationState,
    _pairs,
    _positive,
    _text,
    _utc_now,
    _utc_timestamp,
    read_journal,
)
from downloads_organizer.models import LocationProof

SCHEMA_VERSION = 2
TRANSFER = "cross_volume_copy"
SOURCE_ACTION = "keep"
FIELDS_V2 = (
    *FIELDS,
    "transfer",
    "source_volume",
    "target_volume",
    "temp",
    "size",
    "sha256",
    "source_id",
    "temp_id",
    "target_id",
    "source_action",
    "commit",
    "temp_state",
    "diagnostics",
)


class OperationV2(StrEnum):
    RUN = "run"
    MKDIR = "mkdir"
    COPY = "copy"  # there is no move in v2


class StatusV2(StrEnum):
    STARTED = "started"  # run only
    FINISHED = "finished"  # run only
    INTENT = "intent"
    STAGED = "staged"  # copy: the temporary file was created and is owned
    WRITTEN = "written"  # copy: its content was written; sha256 of what was written
    VERIFIED = "verified"  # copy: the content read back matches
    COMMIT_ATTEMPTED = "commit_attempted"  # copy: the commit may be made from here on
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECOVERY_REQUIRED = "recovery_required"


class Commit(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    ATTEMPTED = "attempted"
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    UNKNOWN = "unknown"


class TempState(StrEnum):
    NONE = "none"  # the temporary file was never created
    PRESENT = "present"
    REMOVED = "removed"
    LEFT = "left"  # it is still there
    UNKNOWN = "unknown"
    COMMITTED = "committed"  # it became the target


# Reasons with a meaning in the grammar; any other reason is free text.
TEMP_NAME_TAKEN = "temp_name_taken"  # skipped after the intent: the name belongs to another
TARGET_EXISTS = "target_exists"  # skipped after commit_attempted, or without an intent
TEMP_CREATE_UNCERTAIN = "temp_create_uncertain"  # failed: it may exist, not owned by us

DIAGNOSTIC_TEXTS = (
    "rename_error",
    "ntstatus",
    "name_query",
    "final_lookup",
    "temp_lookup",
    "temp_handle",
    "cleanup",
    "temp_id_observed",
    "metadata_violation",
)
DIAGNOSTIC_FLAGS = ("delete_pending", "close_may_complete_delete")

_PHASES = ("intent", "staged", "written", "verified", "commit_attempted")
_TERMINALS = frozenset({"done", "skipped", "failed", "cancelled", "recovery_required"})
_TERMINALS_AFTER = {
    "intent": frozenset({"failed", "skipped"}),
    "staged": frozenset({"failed", "cancelled"}),
    "written": frozenset({"failed", "cancelled"}),
    "verified": frozenset({"failed", "cancelled"}),
    "commit_attempted": frozenset({"done", "skipped", "failed", "recovery_required"}),
}
_VOLUMES = ("source_volume", "target_volume")
# Equal in every record of a copy block to its intent (run_id is checked for all).
_BOUND = ("source", "target", "temp", "size", "source_id", "source_action", *_VOLUMES, "transfer")
_HEX16 = re.compile(r"[0-9a-f]{16}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_TEMP = re.compile(r"~DownloadsOrganizer\.[0-9a-f]{12}\.[1-9][0-9]*\.partial")


def temp_name(run_id: str, intent_sequence: int) -> str:
    """The temporary file name of the copy whose intent has ``intent_sequence``."""
    return f"~DownloadsOrganizer.{run_id[:12]}.{intent_sequence}.partial"


@dataclass(frozen=True, slots=True)
class VolumeRef:
    """A volume as journaled: its device, its 64-bit serial number and (if known) GUID."""

    device: str
    serial: int
    guid: str | None = None

    def __post_init__(self) -> None:
        if not _text(self.device):
            raise ValueError(f"invalid device {self.device!r}")
        if type(self.serial) is not int or not 0 <= self.serial < 1 << 64:
            raise ValueError(f"invalid volume serial {self.serial!r}")
        if self.guid is not None and not _text(self.guid):
            raise ValueError(f"invalid volume guid {self.guid!r}")

    @classmethod
    def of(cls, proof: LocationProof) -> VolumeRef:
        return cls(proof.device, proof.volume_serial)

    def to_json(self) -> dict[str, object]:
        return {"device": self.device, "serial": f"{self.serial:016x}", "guid": self.guid}


@dataclass(frozen=True, slots=True)
class Diagnostics:
    """What a copy record says beyond its state (only where the grammar wants it)."""

    rename_error: str | None = None
    ntstatus: str | None = None
    name_query: str | None = None
    final_lookup: str | None = None
    temp_lookup: str | None = None
    temp_handle: str | None = None
    cleanup: str | None = None
    temp_id_observed: str | None = None
    metadata_violation: str | None = None
    delete_pending: bool | None = None
    close_may_complete_delete: bool | None = None

    def to_json(self) -> dict[str, object]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _id(value: int | None) -> str | None:
    """A 128-bit file id as 32 lowercase hex digits (an invalid one fails validation)."""
    if value is None:
        return None
    if type(value) is not int or not 0 <= value < 1 << 128:
        raise ValueError(f"invalid file id {value!r}")
    return f"{value:032x}"


# --- the record on its own ------------------------------------------------------------

_SET = object()  # in a row: the field must not be null (its type is checked elsewhere)
_TERMINAL_STATES = frozenset({"none", "removed", "left", "unknown"})
_NULL_V2 = {name: None for name in FIELDS_V2[len(FIELDS) :] if name != "transfer"}
_RUN_ROW = {**_NULL_V2, "intent": None, "source_volume": _SET, "target_volume": _SET}
_MKDIR_ROW = {**_NULL_V2, "source": None, "target": _SET}
_COPY = {"source": _SET, "target": _SET, **dict.fromkeys(_VOLUMES, _SET), "source_action": "keep"}
_BLOCK = {**_COPY, "intent": _SET, "temp": _SET, "size": _SET, "source_id": _SET}
_PHASE = {**_BLOCK, "target_id": None, "temp_state": "present", "reason": None, "diagnostics": None}
_COPY_ROWS: dict[str, dict[str, object]] = {
    "intent": {
        **_BLOCK,
        "intent": None,
        **dict.fromkeys(("temp_id", "sha256", "target_id", "reason", "diagnostics")),
        "commit": "not_attempted",
        "temp_state": "none",
    },
    "staged": {**_PHASE, "temp_id": _SET, "sha256": None, "commit": "not_attempted"},
    "written": {**_PHASE, "temp_id": _SET, "sha256": _SET, "commit": "not_attempted"},
    "verified": {**_PHASE, "temp_id": _SET, "sha256": _SET, "commit": "not_attempted"},
    "commit_attempted": {**_PHASE, "temp_id": _SET, "sha256": _SET, "commit": "attempted"},
    "done": {
        **_BLOCK,
        **dict.fromkeys(("temp_id", "sha256", "target_id"), _SET),
        "commit": "committed",
        "temp_state": "committed",
        "reason": None,
        "diagnostics": None,
    },
    "recovery_required": {
        **_BLOCK,
        **dict.fromkeys(("temp_id", "sha256", "reason", "diagnostics"), _SET),
        "commit": frozenset({"committed", "unknown"}),
        "temp_state": frozenset({"committed", "unknown"}),
    },
}
# skipped / failed / cancelled with an intent (after the intent, a phase or the attempt)
_ENDED_ROW = {
    **_BLOCK,
    "target_id": None,
    "commit": frozenset({"not_attempted", "not_committed"}),
    "temp_state": _TERMINAL_STATES,
    "reason": _SET,
}
# skipped / failed / cancelled of a copy never started (no intent)
_UNSTARTED_ROW = {
    **_COPY,
    "intent": None,
    **dict.fromkeys(("temp", "size", "source_id", "temp_id", "sha256", "target_id")),
    "commit": "not_attempted",
    "temp_state": "none",
    "reason": _SET,
    "diagnostics": None,
}


def _row_errors(kind: str, row: Mapping[str, object], record: Mapping[str, object]) -> list[str]:
    errors = []
    for name, rule in row.items():
        value = record[name]
        if rule is None:
            ok = value is None
        elif rule is _SET:
            ok = _text(value) if name in ("source", "target", "reason") else value is not None
        elif isinstance(rule, frozenset):
            ok = isinstance(value, str) and value in rule
        else:
            ok = value == rule
        if not ok:
            errors.append(f"{name} {value!r} in {kind}, expected {_expected(rule)}")
    return errors


def _expected(rule: object) -> str:
    if rule is None:
        return "null"
    if rule is _SET:
        return "set"
    if isinstance(rule, frozenset):
        return " or ".join(sorted(map(repr, rule)))
    return repr(rule)


def _volume_errors(name: str, value: object) -> list[str]:
    if not isinstance(value, Mapping) or set(value) != {"device", "serial", "guid"}:
        return [f"invalid {name} {value!r}"]
    serial, guid = value["serial"], value["guid"]
    ok = (
        _text(value["device"])
        and isinstance(serial, str)
        and _HEX16.fullmatch(serial) is not None
        and (guid is None or _text(guid))
    )
    return [] if ok else [f"invalid {name} {value!r}"]


def _diagnostics_errors(value: object) -> list[str]:
    if not isinstance(value, Mapping) or set(value) != {*DIAGNOSTIC_TEXTS, *DIAGNOSTIC_FLAGS}:
        return [f"invalid diagnostics {value!r}"]
    bad = [n for n in DIAGNOSTIC_TEXTS if value[n] is not None and not isinstance(value[n], str)]
    bad += [n for n in DIAGNOSTIC_FLAGS if value[n] is not None and type(value[n]) is not bool]
    return [f"invalid diagnostics {', '.join(bad)}"] if bad else []


def _hex(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _member(value: object, kind: type[StrEnum]) -> bool:
    """``value`` is one of ``kind``'s values (a JSON array or object is unhashable)."""
    return isinstance(value, str) and value in set(kind)


def _type_errors(record: Mapping[str, object]) -> list[str]:
    """Every field's type and value, each taken alone."""
    errors: list[str] = []
    get = record.get
    if type(get("schema_version")) is not int or get("schema_version") != SCHEMA_VERSION:
        errors.append(f"unsupported schema_version {get('schema_version')!r}")
    if not _hex(_HEX32, get("run_id")):
        errors.append(f"invalid run_id {get('run_id')!r}")
    if not _positive(get("sequence")):
        errors.append(f"invalid sequence {get('sequence')!r}")
    if not _utc_timestamp(get("timestamp_utc")):
        errors.append(f"invalid timestamp_utc {get('timestamp_utc')!r}")
    if not _member(get("operation"), OperationV2):
        errors.append(f"unknown operation {get('operation')!r}")
    if not _member(get("status"), StatusV2):
        errors.append(f"unknown status {get('status')!r}")
    if get("intent") is not None and not _positive(get("intent")):
        errors.append(f"invalid intent reference {get('intent')!r}")
    for name in ("source", "target"):
        if get(name) is not None and not _text(get(name)):
            errors.append(f"invalid {name} {get(name)!r}")
    for name in ("reason", "error_message"):
        if get(name) is not None and not isinstance(get(name), str):
            errors.append(f"invalid {name} {get(name)!r}")
    if get("error_code") is not None and type(get("error_code")) is not int:
        errors.append(f"invalid error_code {get('error_code')!r}")
    if get("transfer") != TRANSFER:
        errors.append(f"transfer {get('transfer')!r} is not {TRANSFER!r}")
    for name in _VOLUMES:
        if get(name) is not None:
            errors += _volume_errors(name, get(name))
    checks: tuple[tuple[str, Callable[[object], bool]], ...] = (
        ("temp", lambda v: _hex(_TEMP, v)),
        ("size", lambda v: type(v) is int and v >= 0),
        ("sha256", lambda v: _hex(_HEX64, v)),
        ("source_id", lambda v: _hex(_HEX32, v)),
        ("temp_id", lambda v: _hex(_HEX32, v)),
        ("target_id", lambda v: _hex(_HEX32, v)),
        ("source_action", lambda v: v == SOURCE_ACTION),
        ("commit", lambda v: _member(v, Commit)),
        ("temp_state", lambda v: _member(v, TempState)),
    )
    for name, valid in checks:
        if get(name) is not None and not valid(get(name)):
            errors.append(f"invalid {name} {get(name)!r}")
    if get("diagnostics") is not None:
        errors += _diagnostics_errors(get("diagnostics"))
    return errors


def record_errors(record: object) -> list[str]:
    """What is wrong with the v2 ``record`` taken alone (fields, types, its row of the
    record matrix); empty if it is well formed. Its place in the journal is not checked."""
    if not isinstance(record, Mapping):
        return ["not a JSON object"]
    missing = [name for name in FIELDS_V2 if name not in record]
    unknown = sorted(repr(key) for key in record if key not in FIELDS_V2)
    if missing or unknown:
        return [
            *([f"missing fields {', '.join(missing)}"] if missing else []),
            *([f"unknown fields {', '.join(unknown)}"] if unknown else []),
        ]
    errors = _type_errors(record)
    if errors:
        return errors
    operation, status = str(record["operation"]), str(record["status"])
    kind = f"{operation} {status}"
    if operation == OperationV2.RUN:
        if status not in ("started", "finished"):
            return [f"status {status!r} in a run record"]
        return _row_errors(kind, _RUN_ROW, record)
    if status in ("started", "finished"):
        return [f"status {status!r} outside a run record"]
    if operation == OperationV2.MKDIR:
        if status not in ("intent", *_TERMINALS):
            return [f"status {status!r} in a mkdir record"]
        row = {**_MKDIR_ROW, "intent": None if status == "intent" else _SET}
        return _row_errors(kind, row, record)
    if status in _COPY_ROWS:
        errors = _row_errors(kind, _COPY_ROWS[status], record)
        if status == "recovery_required" and not errors:
            if record["temp_state"] != record["commit"]:
                errors.append(f"temp_state {record['temp_state']!r} is not the commit's")
            if (record["target_id"] is not None) != (record["commit"] == "committed"):
                errors.append("target_id is set exactly when the copy was committed")
        return errors
    if record["intent"] is None:  # a copy never started
        errors = _row_errors(f"{kind} without an intent", _UNSTARTED_ROW, record)
        if record["reason"] == TEMP_NAME_TAKEN:
            errors.append(f"{TEMP_NAME_TAKEN} without an intent")
        return errors
    errors = _row_errors(kind, _ENDED_ROW, record)
    if errors:
        return errors
    commit, temp_state, reason = record["commit"], record["temp_state"], record["reason"]
    if commit == "not_committed" and temp_state == "none":
        errors.append("not_committed with temp_state 'none'")
    if status == "cancelled" and commit != "not_attempted":
        errors.append(f"cancelled with commit {commit!r}")
    if status == "skipped":
        if reason == TEMP_NAME_TAKEN:
            if (commit, temp_state) != ("not_attempted", "none"):
                errors.append(f"{TEMP_NAME_TAKEN} with {commit}/{temp_state}")
        elif reason == TARGET_EXISTS:
            if commit != "not_committed":
                errors.append(f"{TARGET_EXISTS} with commit {commit!r}")
        else:
            errors.append(f"skipped after an intent with reason {reason!r}")
    if (record["diagnostics"] is not None) != (temp_state in ("left", "unknown")):
        errors.append(f"diagnostics {record['diagnostics']!r} with temp_state {temp_state!r}")
    return errors


# --- the record in its journal ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Context:
    """What the records so far fix for the next one."""

    run_id: str | None  # the run every record belongs to (file name or run/started)
    sequence: int = 0  # of the last record
    run: Mapping[str, object] | None = None  # the run/started record
    block: tuple[Mapping[str, object], ...] = ()  # the open block: its intent, then phases
    finished: bool = False


def _same(
    record: Mapping[str, object], other: Mapping[str, object], names: Iterable[str], what: str
) -> list[str]:
    return [
        f"{name} {record[name]!r} differs from {what}'s {other[name]!r}"
        for name in names
        if record[name] != other[name]
    ]


def _block_errors(
    block: tuple[Mapping[str, object], ...], record: Mapping[str, object]
) -> list[str]:
    """Why ``record`` cannot be the next record of the open ``block``."""
    first, last = block[0], str(block[-1]["status"])
    kind = f"{record['operation']} {record['status']}"
    if record["intent"] != first["sequence"] or record["operation"] != first["operation"]:
        return [f"{kind} inside the unfinished block of intent {first['sequence']}"]
    if first["operation"] == OperationV2.MKDIR:
        return _same(record, first, ("target",), "its intent")
    errors = _same(record, first, _BOUND, "its intent")
    status = str(record["status"])
    if status in _PHASES:
        expected = _PHASES[len(block)] if len(block) < len(_PHASES) else "a terminal record"
        if status != expected:
            errors.append(f"{status} after {last}, expected {expected}")
    elif status not in _TERMINALS_AFTER[last]:
        errors.append(f"{status} after {last}")
    else:
        attempted = last == "commit_attempted"
        if status in ("skipped", "failed"):
            want = "not_committed" if attempted else "not_attempted"
            if record["commit"] != want:
                errors.append(f"{status} after {last} with commit {record['commit']!r}")
        ended = status in ("skipped", "failed", "cancelled")
        if ended and last != "intent" and record["temp_state"] == "none":
            errors.append(f"{status} after {last} with temp_state 'none'")
        reason = TARGET_EXISTS if attempted else TEMP_NAME_TAKEN
        if status == "skipped" and record["reason"] != reason:
            errors.append(f"skipped after {last} with reason {record['reason']!r}")
    staged = next((r for r in block if r["status"] == "staged"), None)
    written = next((r for r in block if r["status"] == "written"), None)
    if status != "staged":
        want = None if staged is None else staged["temp_id"]
        if record["temp_id"] != want:
            errors.append(f"temp_id {record['temp_id']!r} differs from the staged {want!r}")
    if status != "written":
        want = None if written is None else written["sha256"]
        if record["sha256"] != want:
            errors.append(f"sha256 {record['sha256']!r} differs from the written {want!r}")
    if record["target_id"] is not None and record["target_id"] != record["temp_id"]:
        errors.append(f"target_id {record['target_id']!r} is not the temp_id")
    return errors


def _context_errors(context: _Context, record: Mapping[str, object]) -> list[str]:
    """Why the well-formed ``record`` cannot follow the records ``context`` stands for."""
    errors = []
    if context.finished:
        errors.append("follows run/finished")
    if record["sequence"] != context.sequence + 1:
        errors.append(f"sequence {record['sequence']} where {context.sequence + 1} was expected")
    run_id = context.run_id if context.run is None else context.run["run_id"]
    if run_id is not None and record["run_id"] != run_id:
        errors.append(f"run_id {record['run_id']!r} is not this run's {run_id!r}")
    operation, status = record["operation"], record["status"]
    kind = f"{operation} {status}"
    if context.run is None:
        if kind != "run started":
            errors.append(f"{kind} before run/started")
        return errors
    if kind == "run started":
        return [*errors, "a second run/started"]
    if context.block:
        return errors + _block_errors(context.block, record)
    if kind == "run finished":
        errors += _same(record, context.run, _VOLUMES, "run/started")
    elif operation == OperationV2.COPY and record["intent"] is None:  # intent or unstarted
        errors += _same(record, context.run, _VOLUMES, "run/started")
        want = temp_name(str(record["run_id"]), record["sequence"])
        if status == "intent" and record["temp"] != want:
            errors.append(f"temp {record['temp']!r} is not {want!r}")
    elif status != "intent":
        errors.append(f"{kind} refers to intent {record['intent']}, which is no open block")
    return errors


def _advance(context: _Context, record: Mapping[str, object]) -> _Context:
    sequence = record["sequence"]
    assert type(sequence) is int
    if context.run is None:
        return replace(context, sequence=sequence, run=record, run_id=str(record["run_id"]))
    status = record["status"]
    if record["operation"] == OperationV2.RUN:
        return replace(context, sequence=sequence, finished=True)
    if status == "intent":
        return replace(context, sequence=sequence, block=(record,))
    if status in _PHASES:
        return replace(context, sequence=sequence, block=(*context.block, record))
    return replace(context, sequence=sequence, block=())  # a terminal, or a copy never started


def _step(context: _Context, record: object) -> tuple[_Context, list[str]]:
    """The context after ``record``, or the unchanged context and why it cannot follow."""
    errors = record_errors(record)
    if not errors:
        assert isinstance(record, Mapping)
        errors = _context_errors(context, record)
        if not errors:
            return _advance(context, record), []
    return context, errors


# --- writing ----------------------------------------------------------------------------


class WriterState(StrEnum):
    READY = "ready"
    WRITING = "writing"  # a write is under way, or was interrupted while it was handled
    FAILED = "failed"  # a write failed: no further record
    UNTRUSTED = "untrusted"  # a write was interrupted: no further record
    CLOSED = "closed"


class JournalV2:
    """Writes the v2 records of one run to ``stream`` (a binary file opened for writing).

    Each ``write`` is one transaction: number, build, validate against the journal so
    far (``record_errors`` and the grammar), encode, write, flush, ``fsync``, then
    update the sequence and the history. The journal is ``READY`` again only after all
    of it succeeded. An ordinary failure anywhere makes it ``FAILED`` and raises
    ``JournalError`` from the failure; an interrupt (``KeyboardInterrupt``,
    ``SystemExit``) makes it ``UNTRUSTED`` and propagates as itself. Either way nothing
    is retried and no further record is accepted; ``uncertain_sequence`` is the record
    that may or may not be on disk. ``close`` is allowed in every state.
    """

    def __init__(
        self,
        stream: BinaryIO,
        path: Path | None = None,
        run_id: str | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._setup(Journal(stream, path, run_id, clock), clock)

    def _setup(self, file: Journal, clock: Callable[[], datetime]) -> None:
        # The v1 journal owns the file: its stream and, from ``create``, the directory
        # chain and the checks on close. Its own ``write`` is never used.
        self._file = file
        self._clock = clock
        self._sequence = 0
        self._history: list[dict[str, object]] = []
        self._context = _Context(file.run_id)
        self._state = WriterState.READY
        self.error: str | None = None
        self.uncertain_sequence: int | None = None

    @classmethod
    def create(cls, directory: Path | None = None) -> JournalV2:
        """A new journal file, created exactly as ``journal.Journal.create`` creates one
        (same directory, checks and name); nothing is written to it yet."""
        journal = cls.__new__(cls)
        journal._setup(Journal.create(directory), _utc_now)
        return journal

    @property
    def path(self) -> Path | None:
        return self._file.path

    @property
    def run_id(self) -> str:
        return self._file.run_id

    @property
    def state(self) -> WriterState:
        return self._state

    @property
    def failed(self) -> bool:
        return self._state in (WriterState.WRITING, WriterState.FAILED, WriterState.UNTRUSTED)

    @property
    def next_sequence(self) -> int:
        return self._sequence + 1

    @property
    def history(self) -> tuple[Mapping[str, object], ...]:
        """The records written, each confirmed by ``fsync``."""
        return tuple(self._history)

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
    ) -> int:
        """Append one record and make it durable; returns its ``sequence``.

        ``transfer`` and ``source_action`` are set by the writer. A record the reader
        would not accept here is refused before anything is written (``FAILED``).
        """
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
            # Only the full count proves the write: None (a raw stream that wrote nothing
            # yet), a bool or a short count is a failure, and nothing is retried.
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

    def close(self) -> None:
        """Close the file (``journal.Journal.close``: the same checks and errors); no
        record is added. A later ``close`` raises the same error again."""
        if self._state is WriterState.READY:  # FAILED / UNTRUSTED / WRITING stay as they are
            self._state = WriterState.CLOSED
        self._file.close()

    def __enter__(self) -> JournalV2:
        return self

    def __exit__(self, kind: object, error: BaseException | None, tb: object) -> None:
        if error is None:
            self.close()
        else:  # a failed close never replaces the error leaving the block
            winfileops.release_after(error, self.close)


# --- reading ----------------------------------------------------------------------------


class ReviewFlag(StrEnum):
    """What a copy entry leaves to be checked by hand; it never says something is gone."""

    TEMP_MAY_EXIST = "temp_may_exist"  # the temporary file may have been created
    TEMP_LEFT = "temp_left"  # the temporary file may still be there
    COMMIT_UNKNOWN = "commit_unknown"  # the target may or may not have been created


_STATES = {
    "done": OperationState.DONE,
    "skipped": OperationState.SKIPPED,
    "failed": OperationState.FAILED,
    "cancelled": OperationState.CANCELLED,
    "recovery_required": OperationState.RECOVERY_REQUIRED,
}
_DISTRUST = "the journal as a whole cannot be trusted, so it confirms no result"


@dataclass(frozen=True, slots=True)
class EntryV2:
    """One change of a v2 journal, as far as the journal can tell.

    ``phase`` is the last phase written for a copy with an intent (``intent`` …
    ``commit_attempted``), ``None`` for a record that stands alone. ``outcome`` is its
    terminal record, accepted only from a journal trusted as a whole.
    """

    operation: str
    source: str | None
    target: str | None
    state: OperationState
    attempted: bool
    phase: str | None
    outcome: Mapping[str, object] | None
    records: tuple[object, ...]
    flags: frozenset[ReviewFlag] = frozenset()
    notes: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JournalReportV2:
    records: tuple[object, ...]  # every parsed record, as read, valid or not
    entries: tuple[EntryV2, ...]
    finished: bool  # a valid run/finished ends a journal trusted as a whole
    problems: tuple[JournalProblem, ...]
    review_whole_journal: bool  # the journal confirms nothing
    unreadable: tuple[tuple[int, bytes], ...] = ()

    @property
    def post_crash_unverified(self) -> bool:
        """No trusted run/finished: the run may have been cut off at any point."""
        return not self.finished

    @property
    def uncertain(self) -> tuple[EntryV2, ...]:
        return tuple(e for e in self.entries if e.state is OperationState.UNCERTAIN)

    @property
    def recovery_needed(self) -> tuple[EntryV2, ...]:
        """Changes whose result has to be checked by hand."""
        return tuple(
            e
            for e in self.entries
            if e.state in (OperationState.UNCERTAIN, OperationState.RECOVERY_REQUIRED) or e.flags
        )

    @property
    def needs_review(self) -> bool:
        return bool(self.problems or self.recovery_needed) or not self.finished


@dataclass(slots=True)
class _Entry:
    first: object  # its intent, or the record that stands alone for it
    line: int
    records: list[object] = field(default_factory=list)  # valid records of its block
    problems: list[str] = field(default_factory=list)

    def build(self, distrust: bool) -> EntryV2:
        first = self.first if isinstance(self.first, Mapping) else {}
        operation = str(first.get("operation"))
        is_intent = first.get("status") == "intent"
        source, target = first.get("source"), first.get("target")
        last = self.records[-1] if self.records else None
        terminal = last if isinstance(last, Mapping) and last.get("status") in _TERMINALS else None
        phases = [r for r in self.records if isinstance(r, Mapping) and r["status"] in _PHASES]
        phase = str(phases[-1]["status"]) if phases else None
        problems = list(self.problems)
        flags: set[ReviewFlag] = set()
        notes: list[str] = []
        if distrust or problems:
            problems.append(_DISTRUST)
            state = OperationState.RECOVERY_REQUIRED
            if operation == OperationV2.COPY:  # nothing it says can be relied on
                flags |= {ReviewFlag.TEMP_MAY_EXIST, ReviewFlag.COMMIT_UNKNOWN}
            terminal = None
        elif terminal is None:
            state = OperationState.UNCERTAIN
            if operation == OperationV2.COPY:
                flags |= _OPEN_FLAGS[str(phase)]
        else:
            state = _STATES[str(terminal["status"])]
            if operation == OperationV2.COPY:
                flags, notes = _terminal_review(terminal)
        return EntryV2(
            operation,
            source if _text(source) else None,
            target if _text(target) else None,
            state,
            is_intent,
            phase,
            terminal,
            tuple(self.records) if self.records else (self.first,),
            frozenset(flags),
            tuple(notes),
            tuple(problems),
        )


# A copy block that ends without a terminal record (the run stopped right there).
_OPEN_FLAGS = {
    "intent": {ReviewFlag.TEMP_MAY_EXIST},
    "staged": {ReviewFlag.TEMP_LEFT},
    "written": {ReviewFlag.TEMP_LEFT},
    "verified": {ReviewFlag.TEMP_LEFT},
    # The commit may or may not have been made, so the temporary file may still exist.
    "commit_attempted": {ReviewFlag.COMMIT_UNKNOWN, ReviewFlag.TEMP_MAY_EXIST},
}


def _terminal_review(record: Mapping[str, object]) -> tuple[set[ReviewFlag], list[str]]:
    flags: set[ReviewFlag] = set()
    notes: list[str] = []
    if record["temp_state"] in ("left", "unknown"):
        flags.add(ReviewFlag.TEMP_LEFT)
    if record["reason"] == TEMP_CREATE_UNCERTAIN:
        notes.append("the temporary file may have been created, but it was never owned")
    if record["commit"] == "unknown":
        flags.add(ReviewFlag.COMMIT_UNKNOWN)
    diagnostics = record["diagnostics"]
    if isinstance(diagnostics, Mapping) and diagnostics["delete_pending"] is True:
        flags.add(ReviewFlag.COMMIT_UNKNOWN)
        notes.append("closing the temporary file may have completed its deletion")
    return flags, notes


def read_journal_v2(path: Path) -> JournalReportV2:
    """Parse a v2 journal file. A line that cannot be read, or a last line without its
    line end (cut off by a crash), is kept in ``unreadable`` and distrusts the journal."""
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


def analyze_v2(records: Iterable[object], *, run_id: str | None = None) -> JournalReportV2:
    """Check v2 ``records`` (in order) and classify every change of the run.

    ``run_id`` is the run they must belong to; by default that of a valid run/started.
    """
    return _analyze(list(enumerate(records, start=1)), [], run_id, [])


def _analyze(
    numbered: list[tuple[int, object]],
    general: list[JournalProblem],
    run_id: str | None,
    unreadable: list[tuple[int, bytes]],
) -> JournalReportV2:
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
    return JournalReportV2(
        tuple(r for _, r in numbered),
        tuple(e.build(distrust) for e in entries),
        context.finished and not distrust,
        tuple(general),
        review_whole_journal=distrust,
        unreadable=tuple(unreadable),
    )


def read_any_journal(path: Path) -> JournalReport | JournalReportV2:
    """``read_journal_v2`` if the first record says schema 2, else ``journal.read_journal``
    (which distrusts any record that is not v1)."""
    with path.open("rb") as stream:
        first = stream.readline()
    try:
        record = json.loads(first.decode("utf-8"), object_pairs_hook=_pairs)
    except UnicodeDecodeError, ValueError, RecursionError:
        record = None
    schema = record.get("schema_version") if isinstance(record, Mapping) else None
    if type(schema) is int and schema == SCHEMA_VERSION:
        return read_journal_v2(path)
    return read_journal(path)
