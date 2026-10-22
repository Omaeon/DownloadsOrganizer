"""Append-only execution journal: one JSON object per line, written before and after
every filesystem change.

A run writes ``run``/``started``, then for each change an ``intent`` record *before*
the change and an outcome record (``done``, ``skipped``, ``failed``, ``cancelled`` or
``recovery_required``) *after* it; the outcome refers to its intent by ``intent``
(the intent's ``sequence``). Items that are never attempted get only an outcome
(``skipped`` or ``cancelled``, ``intent`` ``null``). The run ends with
``run``/``finished``. Every record is flushed and ``fsync``-ed before the caller
continues; a failed write raises ``JournalError`` and the journal refuses all later
writes, so no further change may be started.

``fsync`` asks the OS to write the file to the device; it does not make the log
proof against every power loss (that depends on the device and its write cache).
After a crash, an ``intent`` without an outcome means the result of that change is
unknown and has to be checked by hand (``read_journal``/``analyze``). The reader
accepts an outcome as the result of a change only if the journal as a whole is
intact (a valid start, no unreadable or unbindable record, no gap) and both records
are valid records of this run and the outcome answers exactly one earlier intent of
the same operation and paths; anything else leaves the change uncertain or in need of
recovery, never done.

Journals contain full user paths. They are kept in the per-user
``%LOCALAPPDATA%\\DownloadsOrganizer\\logs`` directory, outside the repository. Only a
drive letter proven to be the root of a local NTFS disk volume is accepted (no subst,
network or other mapped drive). The directory is opened (and its missing tail
created) handle by handle from that root without following links or junctions, every
directory of the path stays held until the journal is closed, and the file is created
relative to the handle of the last one. Closing checks every ``CloseHandle``; a
failure is a ``JournalCloseError``, never silently dropped.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from downloads_organizer import winfileops
from downloads_organizer.pathsafety import PathSafetyError, normalize_dir_path

SCHEMA_VERSION = 1
APP_DIR_NAME = "DownloadsOrganizer"
FIELDS = (
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


class Operation(StrEnum):
    RUN = "run"
    MKDIR = "mkdir"
    MOVE = "move"


class Status(StrEnum):
    STARTED = "started"  # run only
    FINISHED = "finished"  # run only
    INTENT = "intent"  # a change is about to be made
    DONE = "done"  # the change was made and verified (mkdir: also "already existed")
    SKIPPED = "skipped"  # not done; the filesystem was left as it was
    FAILED = "failed"  # attempted and failed; nothing changed
    CANCELLED = "cancelled"  # not attempted because the run stopped
    RECOVERY_REQUIRED = "recovery_required"  # outcome unknown or partial: check by hand


OUTCOMES = frozenset(
    {Status.DONE, Status.SKIPPED, Status.FAILED, Status.CANCELLED, Status.RECOVERY_REQUIRED}
)


class JournalError(Exception):
    """The journal could not be created or written; no further change may start.

    The message is the original failure. ``release_errors`` are handles that could not
    be released afterwards (each as raised, e.g. a ``winfileops.HandleCloseError`` with
    ``winerror``, ``handle_value`` and the path): separate from the message, never
    hidden by it, and such a handle may still be open.
    """

    def __init__(self, message: str, release_errors: Iterable[BaseException] = ()) -> None:
        super().__init__(message)
        self.release_errors = tuple(release_errors)


def default_log_dir(environ: Mapping[str, str] | None = None) -> Path:
    """``%LOCALAPPDATA%\\DownloadsOrganizer\\logs``; ``JournalError`` if it is unset."""
    env = os.environ if environ is None else environ
    base = env.get("LOCALAPPDATA")
    if not base or not Path(base).is_absolute():
        raise JournalError("LOCALAPPDATA is not set to an absolute path")
    return Path(base) / APP_DIR_NAME / "logs"


def _utc_now() -> datetime:
    return datetime.now(UTC)


_DIR_ACCESS = winfileops.FILE_LIST_DIRECTORY | winfileops.FILE_READ_ATTRIBUTES
# Held directories: others may read and add entries, but not rename or delete them.
# FILE_LIST_DIRECTORY is data access, so this share mode is actually enforced.
_HELD_SHARE = winfileops.FILE_SHARE_READ | winfileops.FILE_SHARE_WRITE
_FILE_ACCESS = winfileops.FILE_GENERIC_WRITE | winfileops.FILE_READ_ATTRIBUTES


class JournalCloseError(JournalError):
    """Releasing a handle of the journal (its file or a directory) was not confirmed.

    ``errors`` holds every failure, none hidden by another. A handle behind a
    ``winfileops.HandleCloseError`` may still be open; the error has its value.
    """

    def __init__(self, message: str, errors: Iterable[BaseException]) -> None:
        self.errors = tuple(errors)
        super().__init__(f"{message}: " + "; ".join(_describe(e) for e in self.errors), self.errors)


_describe = winfileops.describe_error


def _raise_failures(message: str, errors: list[BaseException]) -> None:
    """Raise ``JournalCloseError`` for ``errors`` (if any); a non-``Exception`` such as
    ``KeyboardInterrupt`` is raised itself, after every handle was tried, with every
    other failure in its ``release_errors`` (as objects) and a note."""
    if not errors:
        return
    for exc in errors:
        if not isinstance(exc, Exception):
            exc.add_note(f"{message}: " + "; ".join(_describe(e) for e in errors))
            winfileops.attach_release_errors(exc, errors)
            raise exc
    raise JournalCloseError(message, errors)


class _DirectoryChain:
    """Owns the handles of a directory and of every ancestor up to the volume root.

    Each directory below the root is held without ``FILE_SHARE_DELETE`` while the
    chain is open, so none of them can be renamed, deleted or swapped for a junction:
    the path from the root to the last handle stays the path the walk checked.
    ``drive`` and ``device`` are the verified volume (``C:`` and its
    ``\\Device\\HarddiskVolumeN``), set once the root was proven to be its root.
    """

    def __init__(self) -> None:
        self._handles: list[winfileops.Handle] = []
        self.drive = ""
        self.device = ""

    @property
    def last(self) -> winfileops.Handle:
        return self._handles[-1]

    def push(self, handle: winfileops.Handle) -> None:
        self._handles.append(handle)

    def take_last(self) -> winfileops.Handle:
        """Hand the last handle over to the caller; the chain no longer closes it."""
        return self._handles.pop()

    def release(self) -> list[BaseException]:
        """Close every handle, innermost first, each even if closing another failed.

        Returns every failure. The chain gives all its handles up either way: a failed
        ``CloseHandle`` is reported, never retried.
        """
        handles, self._handles = self._handles, []
        errors: list[BaseException] = []
        for handle in reversed(handles):
            try:
                handle.close()
            except BaseException as exc:  # close the rest first, then report
                errors.append(exc)
        return errors

    def close(self) -> None:
        """``release``; ``JournalCloseError`` with every failure if there was one."""
        _raise_failures("cannot release the journal directories", self.release())


def _open_log_dir(chain: _DirectoryChain, directory: Path, existing: int) -> None:
    """Open ``directory`` by handles from its volume root and hold them all in ``chain``.

    ``directory.parts[:existing]`` must exist; later components are created if
    missing. The volume root must be proven to be the root of a local NTFS disk volume
    (``_open_volume_root``) before anything else is opened or created. Every component
    is checked through its own handle before anything is opened or created below it,
    and stays held until the chain is released; the caller releases it on any error.
    """
    _open_volume_root(chain, directory)
    for position, part in enumerate(directory.parts[1:], start=1):
        _open_log_component(chain, part, may_create=position >= existing)


def _open_volume_root(chain: _DirectoryChain, directory: Path) -> None:
    """Open the volume root of ``directory`` into ``chain`` and prove that it is one.

    ``winfileops.volume_root`` and ``winfileops.prove_volume_root``, the proof the
    executor uses as well: the drive letter is defined as exactly one disk volume
    device and the opened object is that device's root directory on a local NTFS disk;
    the mapping is read again afterwards. Anything unproven is an
    ``UnverifiedRootError`` (the caller makes it a ``JournalError``).
    """
    root = winfileops.volume_root(directory)
    chain.push(winfileops.open_anchor(root.anchor))
    winfileops.prove_volume_root(chain.last, root)
    chain.drive, chain.device = root.drive, root.device


def _open_log_component(chain: _DirectoryChain, name: str, *, may_create: bool) -> None:
    parent = chain.last

    def open_existing() -> winfileops.Handle:
        handle, _ = winfileops.open_relative(
            parent, name, directory=True, access=_DIR_ACCESS, share=_HELD_SHARE
        )
        return handle

    try:
        handle = open_existing()
    except FileNotFoundError:
        if not may_create:
            raise
        try:
            handle, _ = winfileops.open_relative(
                parent, name, directory=True, access=_DIR_ACCESS, share=_HELD_SHARE, create=True
            )
        except FileExistsError:
            handle = open_existing()  # created concurrently: check that object instead
    chain.push(handle)  # released with the chain from here on, also if the check fails
    info = winfileops.query(handle)
    if info.reparse_point:
        raise JournalError(
            f"cannot create journal: {handle.path} is a link or junction"
            f" (tag {info.reparse_tag:#x})"
        )
    if not info.directory:
        raise JournalError(f"cannot create journal: {handle.path} is not a directory")


def _create_log_file(chain: _DirectoryChain, path: Path) -> tuple[BinaryIO, tuple[int, int]]:
    """Create the journal file exclusively inside the last directory of ``chain``.

    ``path`` is the same file by name. With the chain held it cannot stop leading to
    the created file; that it does (and that its drive letter still is the verified
    volume) is checked anyway, and refused if not. Returns the stream, which owns the
    file handle, and the file's ``(volume serial, file id)``.
    """
    handle = winfileops.create_file(
        chain.last, path.name, access=_FILE_ACCESS, share=winfileops.FILE_SHARE_READ
    )
    chain.push(handle)  # released with the chain if a check below fails
    info = winfileops.query(handle)
    if info.reparse_point or info.directory or info.links != 1:
        raise JournalError(f"cannot create journal: {path} is not a new plain file")
    if not winfileops.final_nt_path(handle).upper().startswith(chain.device.upper() + "\\"):
        raise JournalError(f"cannot create journal: {path} is not on {chain.device}")
    winfileops.check_mapping(winfileops.VolumeRoot(chain.drive, chain.device))
    identity = (info.volume_serial, info.file_id)
    st = path.lstat()
    if (st.st_dev, st.st_ino) != identity:
        raise JournalError(f"cannot create journal: {path} does not lead to the new file")
    # If open_stream fails before the C runtime took the handle over, it stays in the
    # chain (released and checked with it); after that the stream owns it.
    stream = winfileops.open_stream(chain.last)
    chain.take_last()
    return stream, identity


def _location_problem(chain: _DirectoryChain, path: Path, identity: tuple[int, int]) -> str | None:
    """Why ``path`` may no longer name the journal file, or ``None`` if it still does."""
    try:
        if winfileops.query_dos_device(chain.drive) != (chain.device,):
            return f"{chain.drive} is no longer {chain.device}: {path} may not be the journal"
        st = path.lstat()
    except OSError as exc:
        return f"cannot check that {path} still is the journal: {_describe(exc)}"
    if (st.st_dev, st.st_ino) != identity:
        return f"{path} no longer leads to the journal file"
    return None


class Journal:
    """Writes the records of one run to ``stream`` (a binary file opened for writing)."""

    def __init__(
        self,
        stream: BinaryIO,
        path: Path | None = None,
        run_id: str | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = path
        self.run_id = run_id or uuid.uuid4().hex
        self._stream: BinaryIO | None = stream
        self._clock = clock
        self._sequence = 0
        self._chain: _DirectoryChain | None = None  # the log directory (Journal.create)
        self._identity: tuple[int, int] | None = None  # of the file at ``path`` (create)
        self._close_error: JournalCloseError | None = None
        self.error: str | None = None

    @classmethod
    def create(cls, directory: Path | None = None) -> Journal:
        """A new journal file ``run-<UTC time>-<run id>.jsonl`` in ``directory``.

        Only a directory on a drive letter that is proven to be the root of a local
        NTFS disk volume is accepted (``_open_volume_root``): subst and network drives,
        UNC paths and other volumes are a ``JournalError`` before anything is created.
        The directory is walked from that root handle by handle (``winfileops``), never
        following a link or junction; a component that is one is refused before
        anything is created below it. Only the tail may be created: ``DownloadsOrganizer``
        and ``logs`` for the default ``default_log_dir()`` (``%LOCALAPPDATA%`` itself
        must exist), the last component for an explicit ``directory``. Every directory
        of the path stays open without ``FILE_SHARE_DELETE`` from the moment it is
        checked until ``close``, so none can be renamed or swapped meanwhile. The file
        is created with ``FILE_CREATE`` relative to the handle of the last one; it is
        never reused. On any error every handle opened so far is released; a failure to
        release one is in ``JournalError.release_errors``, next to the original error.
        """
        if not winfileops.AVAILABLE:
            raise JournalError("cannot create journal: only supported on Windows")
        chain = _DirectoryChain()
        try:
            try:
                if directory is None:
                    directory = normalize_dir_path(default_log_dir())
                    existing = len(directory.parts) - 2  # %LOCALAPPDATA% must exist
                else:
                    directory = normalize_dir_path(directory)
                    existing = len(directory.parts) - 1
                run_id = uuid.uuid4().hex
                name = f"run-{_utc_now():%Y%m%dT%H%M%S%fZ}-{run_id}.jsonl"
                _open_log_dir(chain, directory, existing)
                stream, identity = _create_log_file(chain, directory / name)
            except winfileops.ReleaseError as exc:  # the stream could not be built
                raise JournalError(f"cannot create journal: {exc}", exc.releases) from exc.error
            except (OSError, ValueError, PathSafetyError, winfileops.UnverifiedRootError) as exc:
                raise JournalError(f"cannot create journal: {exc}") from exc
        except BaseException as exc:
            errors = chain.release()
            if not errors:
                raise
            interrupts = [e for e in (exc, *errors) if not isinstance(e, Exception)]
            if interrupts:  # an interrupt stays itself and is never kept as a mere error
                interrupts[0].add_note(
                    f"while creating the journal ({_describe(exc)}) releasing its handles"
                    " failed: " + "; ".join(map(_describe, errors))
                )
                earlier = getattr(exc, "release_errors", ())  # e.g. the stream's file
                winfileops.attach_release_errors(interrupts[0], [exc, *earlier, *errors])
                if interrupts[0] is exc:
                    raise
                raise interrupts[0] from exc
            if isinstance(exc, JournalError):
                exc.release_errors += tuple(errors)
                raise
            raise JournalError(f"cannot create journal: {_describe(exc)}", errors) from exc
        journal = cls(stream, directory / name, run_id)
        journal._chain, journal._identity = chain, identity
        return journal

    @property
    def failed(self) -> bool:
        return self.error is not None

    def write(
        self,
        operation: Operation,
        status: Status,
        *,
        intent: int | None = None,
        source: Path | None = None,
        target: Path | None = None,
        reason: str | None = None,
        error_code: int | None = None,
        error_message: str | None = None,
    ) -> int:
        """Append one record and make it durable; returns its ``sequence``."""
        if self.error is not None:
            raise JournalError(f"journal unusable after an earlier error: {self.error}")
        if self._stream is None:
            raise JournalError("journal is closed")
        sequence = self._sequence + 1
        try:
            record = {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "sequence": sequence,
                "timestamp_utc": self._clock().astimezone(UTC).isoformat(timespec="microseconds"),
                "operation": str(Operation(operation)),
                "status": str(Status(status)),
                "intent": intent,
                "source": None if source is None else str(source),
                "target": None if target is None else str(target),
                "reason": None if reason is None else str(reason),
                "error_code": error_code,
                "error_message": error_message,
            }
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            # A lone surrogate (possible in Windows names) becomes a JSON \uXXXX escape.
            data = line.encode("utf-8", errors="backslashreplace")
            written = self._stream.write(data)
            if written is not None and written != len(data):
                raise OSError(f"short write: {written} of {len(data)} bytes")
            self._stream.flush()
            os.fsync(self._stream.fileno())
        except (OSError, ValueError, TypeError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            raise JournalError(f"cannot write journal record {sequence}: {exc}") from exc
        self._sequence = sequence
        return sequence

    def close(self) -> None:
        """Close the file, then release the directory chain, each even if another fails.

        A journal from ``create`` first checks that its path still names its file on
        the verified volume (the drive letter could have been redefined). Every failure
        raises one ``JournalCloseError`` with all of them; nothing is closed twice, and a
        later ``close`` raises the same error again. An interrupt among them is raised
        itself instead, after everything was tried, with the other failures in its
        ``release_errors``.
        """
        if self._close_error is not None:
            raise self._close_error
        stream, self._stream = self._stream, None
        chain, self._chain = self._chain, None
        errors: list[BaseException] = []
        try:
            if chain is not None and self.path is not None and self._identity is not None:
                try:
                    problem = _location_problem(chain, self.path, self._identity)
                except BaseException as exc:  # the file and the chain are released all the same
                    errors.append(exc)
                else:
                    if problem is not None:
                        errors.append(JournalError(problem))
            if stream is not None:
                try:
                    stream.close()
                except BaseException as exc:  # the chain is released all the same
                    errors.append(exc)
        finally:
            if chain is not None:
                errors.extend(chain.release())
        try:
            _raise_failures(f"cannot close journal {self.path}", errors)
        except JournalCloseError as exc:
            self._close_error = exc
            raise

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, kind: object, error: BaseException | None, tb: object) -> None:
        if error is None:
            self.close()
        else:  # a failed close never replaces the error leaving the block
            winfileops.release_after(error, self.close)


# --- reading ------------------------------------------------------------------------
#
# The reader trusts no record. Each record is first checked on its own (every field,
# its type and value, the combination of operation, status, intent reference and
# paths), then against the journal (one run_id, unique sequence numbers in order, an
# outcome refers to an earlier intent of the same operation with the same paths).
# Only a record that passes everything can confirm an operation; any other record can
# only make the operations it refers to less certain, never more.
#
# A problem that belongs to no single operation (an unreadable line, a record that
# cannot be bound to an intent, no valid run/started, a gap in the sequence, a damaged
# run record) means the journal may have lost or garbled any record, including an
# outcome that contradicts one that looks fine. Then the journal confirms nothing:
# every operation is RECOVERY_REQUIRED and the run does not count as finished. A
# journal that merely stops (no run/finished, nothing damaged) keeps what it proved.


class OperationState(StrEnum):
    """What a journal says about one change, as far as it can say."""

    UNCERTAIN = "uncertain"  # intent without outcome: in progress, or a crash hit it
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECOVERY_REQUIRED = "recovery_required"  # also: the journal cannot be trusted here


_STATES = {
    Status.DONE: OperationState.DONE,
    Status.SKIPPED: OperationState.SKIPPED,
    Status.FAILED: OperationState.FAILED,
    Status.CANCELLED: OperationState.CANCELLED,
    Status.RECOVERY_REQUIRED: OperationState.RECOVERY_REQUIRED,
}
_OPERATIONS = frozenset(str(o) for o in Operation)
_STATUSES = frozenset(str(s) for s in Status)
_RUN_STATUSES = frozenset({str(Status.STARTED), str(Status.FINISHED)})
# Outcomes the executor writes for items it never started (no intent).
_UNATTEMPTED = frozenset({str(Status.SKIPPED), str(Status.FAILED), str(Status.CANCELLED)})
_FILE_NAME = re.compile(r"run-\d{8}T\d{12}Z-([0-9a-f]{32})\.jsonl")


@dataclass(frozen=True, slots=True)
class JournalProblem:
    """One finding; ``line`` is the record's line (1-based), ``None`` for the journal."""

    line: int | None
    message: str

    def __str__(self) -> str:
        return self.message if self.line is None else f"line {self.line}: {self.message}"


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One operation, as far as the journal can tell.

    ``intent`` is its intent record and ``outcome`` the single outcome record accepted
    as its result. ``conflicts`` are records that refer to it (or stand alone for it)
    but cannot confirm anything: invalid, of another run, another operation or other
    paths, a second answer, or any outcome of a journal that cannot be trusted as a
    whole. Any conflict or problem makes the state ``RECOVERY_REQUIRED``.
    ``attempted`` is true when an intent was (or, judging by a reference, seems to have
    been) written: the change may have been started.
    """

    operation: str
    source: str | None
    target: str | None
    state: OperationState
    attempted: bool
    intent: Mapping[str, object] | None
    outcome: Mapping[str, object] | None
    conflicts: tuple[object, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JournalReport:
    records: tuple[object, ...]  # every parsed record, as read, valid or not
    entries: tuple[JournalEntry, ...]
    finished: bool  # a valid run/finished of this run ends a journal trusted as a whole
    problems: tuple[JournalProblem, ...]
    # A problem that belongs to no single operation: the journal confirms nothing.
    review_whole_journal: bool
    unreadable: tuple[tuple[int, bytes], ...] = ()  # (line, raw bytes) of lost lines

    @property
    def uncertain(self) -> tuple[JournalEntry, ...]:
        return tuple(e for e in self.entries if e.state is OperationState.UNCERTAIN)

    @property
    def recovery_needed(self) -> tuple[JournalEntry, ...]:
        """Changes whose result has to be checked by hand."""
        return tuple(
            e
            for e in self.entries
            if e.state in (OperationState.UNCERTAIN, OperationState.RECOVERY_REQUIRED)
        )

    @property
    def needs_review(self) -> bool:
        """Anything at all to check by hand: problems, open operations, no final record."""
        return bool(self.problems or self.recovery_needed) or not self.finished


def read_journal(path: Path) -> JournalReport:
    """Parse a journal file; a damaged line (e.g. cut off by a crash) is a problem.

    An unreadable line is kept (``unreadable``) and, since it may have said anything,
    makes the whole journal untrusted. The run id in a file name written by
    ``Journal.create`` is the run every record must belong to.
    """
    numbered: list[tuple[int, object]] = []
    problems: list[JournalProblem] = []
    unreadable: list[tuple[int, bytes]] = []
    with path.open("rb") as stream:
        for number, raw in enumerate(stream, start=1):
            try:
                numbered.append((number, json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)))
            except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                problems.append(JournalProblem(number, f"unreadable, content unknown ({exc})"))
                unreadable.append((number, raw))
    match = _FILE_NAME.fullmatch(path.name)
    return _analyze(numbered, problems, match.group(1) if match else None, unreadable)


def analyze(records: Iterable[object], *, run_id: str | None = None) -> JournalReport:
    """Pair intents with outcomes and classify every operation of one run.

    ``run_id`` is the run the records must belong to; by default that of the first
    record, and only if it is a valid run/started.
    """
    return _analyze(list(enumerate(records, start=1)), [], run_id, [])


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("a key occurs twice in one object")
    return result


def _positive(value: object) -> bool:
    return type(value) is int and value > 0  # bool is not an int here


def _text(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _utc_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        offset = datetime.fromisoformat(value).utcoffset()
    except ValueError:
        return False
    return offset is not None and not offset


def _record_errors(record: object) -> list[str]:
    """What is wrong with ``record`` taken alone; empty if it is well formed."""
    if not isinstance(record, Mapping):
        return ["not a JSON object"]
    errors: list[str] = []
    missing = [name for name in FIELDS if name not in record]
    if missing:
        errors.append(f"missing fields {', '.join(missing)}")
    unknown = sorted(repr(key) for key in record if key not in FIELDS)
    if unknown:
        errors.append(f"unknown fields {', '.join(unknown)}")
    get = record.get
    schema, intent = get("schema_version"), get("intent")
    if type(schema) is not int or schema != SCHEMA_VERSION:
        errors.append(f"unsupported schema_version {schema!r}")
    if not _text(get("run_id")):
        errors.append(f"invalid run_id {get('run_id')!r}")
    if not _positive(get("sequence")):
        errors.append(f"invalid sequence {get('sequence')!r}")
    if not _utc_timestamp(get("timestamp_utc")):
        errors.append(f"invalid timestamp_utc {get('timestamp_utc')!r}")
    operation, status = get("operation"), get("status")
    known = isinstance(operation, str) and operation in _OPERATIONS
    if not known:
        errors.append(f"unknown operation {operation!r}")
    if not (isinstance(status, str) and status in _STATUSES):
        errors.append(f"unknown status {status!r}")
        known = False
    if intent is not None and not _positive(intent):
        errors.append(f"invalid intent reference {intent!r}")
    for name in ("source", "target"):
        if get(name) is not None and not _text(get(name)):
            errors.append(f"invalid {name} {get(name)!r}")
    for name in ("reason", "error_message"):
        if get(name) is not None and not isinstance(get(name), str):
            errors.append(f"invalid {name} {get(name)!r}")
    if get("error_code") is not None and type(get("error_code")) is not int:
        errors.append(f"invalid error_code {get('error_code')!r}")
    if known:
        errors += _shape_errors(str(operation), str(status), record)
    return errors


def _shape_errors(operation: str, status: str, record: Mapping[object, object]) -> list[str]:
    """The combination of operation, status, intent reference and paths."""
    source, target, intent = record.get("source"), record.get("target"), record.get("intent")
    if operation == Operation.RUN:
        if status not in _RUN_STATUSES:
            return [f"status {status!r} in a run record"]
        return [] if intent is None else ["a run record refers to an intent"]
    if status in _RUN_STATUSES:
        return [f"status {status!r} outside a run record"]
    errors: list[str] = []
    if status == Status.INTENT and intent is not None:
        errors.append("an intent refers to an intent")
    if operation == Operation.MKDIR:
        if source is not None:
            errors.append("mkdir record with a source")
        if not _text(target):
            errors.append("mkdir record without a target")
        if status != Status.INTENT and intent is None:
            errors.append(f"mkdir {status} without an intent")
    else:
        if not _text(source):
            errors.append("move record without a source")
        if status == Status.INTENT and not _text(target):
            errors.append("move intent without a target")
        if status != Status.INTENT and intent is None and status not in _UNATTEMPTED:
            errors.append(f"move {status} without an intent")
    return errors


@dataclass(slots=True)
class _Record:
    line: int
    data: object
    errors: list[str]

    @property
    def valid(self) -> bool:
        return not self.errors

    def get(self, key: str) -> object:
        return self.data.get(key) if isinstance(self.data, Mapping) else None

    @property
    def operation_like(self) -> bool:
        """It is about a single change (move or mkdir), whatever else is wrong with it."""
        operation = self.get("operation")
        return operation == Operation.MOVE or operation == Operation.MKDIR


@dataclass(slots=True)
class _Operation:
    first: _Record  # its intent, or the record that stands alone for it
    is_intent: bool
    attempted: bool
    answers: list[_Record] = field(default_factory=list)  # valid, matching outcomes
    conflicts: list[_Record] = field(default_factory=list)
    problems: list[JournalProblem] = field(default_factory=list)

    def conflict(self, record: _Record, why: str) -> None:
        self.conflicts.append(record)
        self.problems.append(JournalProblem(record.line, why))

    def entry(self, distrust: str | None) -> JournalEntry:
        """The entry; ``distrust`` is why the journal as a whole confirms nothing."""
        first, outcome = self.first, None
        conflicts = [*self.answers, *self.conflicts]
        if self.is_intent and len(self.answers) > 1:
            self.problems.append(
                JournalProblem(first.line, "the intent is answered more than once")
            )
        problems = [str(p) for p in self.problems]
        if distrust is not None:
            problems.append(distrust)
            if not self.is_intent and all(c is not first for c in conflicts):
                conflicts.insert(0, first)  # it stands for the operation, unconfirmed
        elif not problems:
            outcome = (self.answers[0] if self.answers else None) if self.is_intent else first
            conflicts = []
        if problems:  # nothing this journal says about the change can be trusted
            state = OperationState.RECOVERY_REQUIRED
        elif outcome is None:
            state = OperationState.UNCERTAIN
        else:
            state = _STATES[Status(str(outcome.get("status")))]
        source, target = first.get("source"), first.get("target")
        return JournalEntry(
            str(first.get("operation")),
            source if _text(source) else None,  # type: ignore[arg-type]
            target if _text(target) else None,  # type: ignore[arg-type]
            state,
            self.attempted,
            first.data if self.is_intent and isinstance(first.data, Mapping) else None,
            outcome.data if outcome is not None else None,  # type: ignore[arg-type]
            tuple(r.data for r in conflicts),
            tuple(problems),
        )


def _mismatch(referred: list[_Operation], record: _Record) -> str | None:
    """Why the valid outcome ``record`` cannot answer its intent; ``None`` if it can."""
    ref = record.get("intent")
    if not referred:
        return f"refers to intent {ref}, which is no intent of this journal"
    if len(referred) > 1:
        return f"refers to sequence {ref}, which several intents use"
    intent = referred[0].first
    if not intent.valid:
        return f"refers to the invalid intent on line {intent.line}"
    if not (intent.line < record.line and intent.get("sequence") < record.get("sequence")):  # type: ignore[operator]
        return f"comes before its intent on line {intent.line}"
    for name in ("operation", "source", "target"):
        if record.get(name) != intent.get(name):
            return f"{name} {record.get(name)!r} differs from its intent's {intent.get(name)!r}"
    return None


def _is_start(record: _Record) -> bool:
    """A well-formed run/started in the first place, before it is checked against the run."""
    return (
        not _record_errors(record.data)
        and record.get("operation") == Operation.RUN
        and record.get("status") == Status.STARTED
        and record.get("sequence") == 1
    )


def _analyze(
    numbered: list[tuple[int, object]],
    general: list[JournalProblem],
    run_id: str | None,
    unreadable: list[tuple[int, bytes]],
) -> JournalReport:
    records = [_Record(line, data, _record_errors(data)) for line, data in numbered]
    # ``general``: problems that belong to no single operation; any of them means the
    # journal as a whole confirms nothing.
    if run_id is None and records and _is_start(records[0]):
        run_id = str(records[0].get("run_id"))  # never that of any other first record
    for record in records:
        rid = record.get("run_id")
        if not _text(rid):
            continue
        if run_id is None:
            record.errors.append(f"run_id {rid!r} cannot be checked: no valid run/started")
        elif rid != run_id:
            record.errors.append(f"run_id {rid!r} is not this run's {run_id!r}")

    counts = Counter(r.get("sequence") for r in records if _positive(r.get("sequence")))
    expected = 1
    for record in records:
        sequence = record.get("sequence")
        if not _positive(sequence):
            expected += 1
            continue
        if counts[sequence] > 1:
            record.errors.append(f"sequence {sequence} is used by {counts[sequence]} records")
        if sequence != expected:
            message = f"sequence {sequence} where {expected} was expected"
            record.errors.append(message)  # a record is missing or out of place before it
            general.append(JournalProblem(record.line, message))
        expected = sequence + 1  # type: ignore[operator]

    if not records:
        general.append(JournalProblem(None, "the journal has no records"))
    elif not (records[0].valid and _is_start(records[0])):
        why = "; ".join(records[0].errors) or "it is no run/started"
        general.append(
            JournalProblem(records[0].line, f"does not start with a valid run/started: {why}")
        )
    finished = False
    for record in records:
        if record.get("operation") != Operation.RUN:
            continue
        if record.get("status") == Status.STARTED and record is not records[0]:
            general.append(
                JournalProblem(record.line, "a run/started that is not the first record")
            )
        elif not record.valid:
            message = f"invalid run record: {'; '.join(record.errors)}"
            general.append(JournalProblem(record.line, message))
        elif record.get("status") == Status.FINISHED:
            if finished:
                general.append(JournalProblem(record.line, "a second run/finished"))
            if record is not records[-1]:
                general.append(JournalProblem(record.line, "records follow run/finished"))
            finished = True
    if finished:  # nothing may follow the final record; what does is not trusted
        last = min(
            i for i, r in enumerate(records) if r.valid and r.get("status") == Status.FINISHED
        )
        for record in records[last + 1 :]:
            record.errors.append("follows run/finished")

    operations: list[_Operation] = []
    by_sequence: dict[int, list[_Operation]] = {}
    for record in records:
        if record.get("status") == Status.INTENT and record.operation_like:
            operation = _Operation(record, is_intent=True, attempted=True)
            if not record.valid:
                why = f"invalid intent: {'; '.join(record.errors)}"
                operation.problems.append(JournalProblem(record.line, why))
            operations.append(operation)
            sequence = record.get("sequence")
            if _positive(sequence):
                by_sequence.setdefault(sequence, []).append(operation)  # type: ignore[arg-type]
    # Every record that refers to an intent is bound to it, whatever else it is (a run
    # record, an intent, an unknown operation): it may be the answer that contradicts
    # one that looks fine. Only a reference is checked before the kind of record.
    for record in records:
        ref = record.get("intent")
        own_intent = record.get("status") == Status.INTENT and record.operation_like
        if ref is None and (own_intent or record.get("operation") == Operation.RUN):
            continue  # an intent, or a run record: they answer nothing
        referred = by_sequence.get(ref, []) if _positive(ref) else []  # type: ignore[call-overload]
        if record.valid:
            if ref is None:  # decided without touching the filesystem
                operations.append(_Operation(record, is_intent=False, attempted=False))
                continue
            why = _mismatch(referred, record)
            if why is None:
                referred[0].answers.append(record)
                continue
        else:
            why = "; ".join(record.errors)
        if referred:
            for operation in referred:
                operation.conflict(record, why)
            continue
        # Its intent cannot be found (no reference, a damaged one, a dangling one): it
        # may belong to any operation of the journal.
        if ref is None:
            general.append(JournalProblem(record.line, f"belongs to no operation: {why}"))
        else:
            message = f"its intent reference {ref!r} cannot be resolved: {why}"
            general.append(JournalProblem(record.line, message))
        if record.operation_like and not own_intent:
            operation = _Operation(record, is_intent=False, attempted=ref is not None)
            operation.conflict(record, why)
            operations.append(operation)

    distrust = (
        "the journal as a whole cannot be trusted, so it confirms no result" if general else None
    )
    operations.sort(key=lambda o: o.first.line)
    entries = tuple(o.entry(distrust) for o in operations)
    problems = [*general, *(p for o in operations for p in o.problems)]
    return JournalReport(
        tuple(r.data for r in records),
        entries,
        finished and not general,
        tuple(problems),
        review_whole_journal=bool(general),
        unreadable=tuple(unreadable),
    )
