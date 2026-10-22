"""Thin boundary to the Windows file API: handle-relative opens, queries and renames.

All ``ctypes`` use of the project lives here. Paths are only parsed for a volume
root (``CreateFileW`` of the anchor); every further component is opened with
``NtCreateFile`` relative to the handle of its parent directory and with
``FILE_OPEN_REPARSE_POINT``, so no symlink or junction is ever followed and a
component swapped after it was opened cannot redirect the next step.

Documented APIs used: ``CreateFileW``, ``GetFileInformationByHandleEx``
(``FileBasicInfo``, ``FileStandardInfo``, ``FileAttributeTagInfo``, ``FileIdInfo``) and
``CloseHandle``, ``QueryDosDeviceW`` and ``GetFinalPathNameByHandleW`` from kernel32;
``NtCreateFile``, ``NtSetInformationFile`` (``FileRenameInformation``),
``NtQueryVolumeInformationFile``
(``FileFsDeviceInformation``, ``FileFsAttributeInformation``) and
``RtlNtStatusToDosError`` from ntdll (winternl.h / the WDK). The rename goes through
``NtSetInformationFile`` because kernel32's ``SetFileInformationByHandle(FileRenameInfo)``
rejects a non-NULL ``RootDirectory`` with ``ERROR_INVALID_PARAMETER`` (verified on
Windows 11), and a rename relative to the already opened target directory handle is
the point of this module. New files
(the journal) are created the same way, relative to a directory handle with
``FILE_CREATE``, and handed to Python through ``msvcrt.open_osfhandle``.

A rename never replaces an existing file: ``ReplaceIfExists`` is always ``FALSE`` and
the filesystem itself fails the call if the target name exists (no check-then-act).
It never copies either: a rename to another volume fails with
``ERROR_NOT_SAME_DEVICE``. Errors are raised as ``OSError`` with ``winerror`` set; for
``NtCreateFile`` the NTSTATUS is kept in the ``ntstatus`` attribute.

A handle is closed exactly once and the result of ``CloseHandle`` is checked: a
``FALSE`` raises ``HandleCloseError`` (with ``GetLastError``) and leaves the handle in
the ``CLOSE_FAILED`` state, never reported as released and never retried. When an
operation fails and releasing what it held fails as well, ``ReleaseError`` keeps both:
the original error and every release failure as separate objects.

A volume root is only trusted once it is proven (``volume_root``, then
``prove_volume_root`` on the opened handle): the drive letter is defined as exactly one
``\\Device\\HarddiskVolumeN`` (no ``subst``, network or other mapped drive), the
opened object is a directory on a local NTFS disk, not a reparse point, MFT record 5,
its final NT path is ``<that device>\\``, and the mapping is still the same after all
of that. Anything unproven is an ``UnverifiedRootError``; nothing is resolved from a
virtual drive to a real path. The journal and the executor share this one proof.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from io import BufferedWriter, FileIO
from pathlib import Path
from typing import BinaryIO

AVAILABLE = sys.platform == "win32"

DELETE = 0x0001_0000
SYNCHRONIZE = 0x0010_0000
FILE_LIST_DIRECTORY = 0x0001
FILE_ADD_FILE = 0x0002
FILE_ADD_SUBDIRECTORY = 0x0004
FILE_READ_ATTRIBUTES = 0x0080
# FILE_GENERIC_WRITE: write/append data, write attributes and EA, READ_CONTROL, SYNCHRONIZE.
FILE_GENERIC_WRITE = 0x0012_0116

FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4
SHARE_ALL = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_NORMAL = 0x80

_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x0200_0000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x0020_0000

_FILE_OPEN = 1
_FILE_CREATE = 2
_FILE_DIRECTORY_FILE = 0x0000_0001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x0000_0020
_FILE_NON_DIRECTORY_FILE = 0x0000_0040
_FILE_OPEN_REPARSE_POINT = 0x0020_0000
_OBJ_CASE_INSENSITIVE = 0x40
_FILE_OPENED = 1
_FILE_CREATED = 2

_FILE_BASIC_INFO = 0
_FILE_STANDARD_INFO = 1
_FILE_ATTRIBUTE_TAG_INFO = 9
_FILE_ID_INFO = 18
_FILE_RENAME_INFORMATION = 10  # FILE_INFORMATION_CLASS for NtSetInformationFile
_FILE_FS_DEVICE_INFORMATION = 4  # FS_INFORMATION_CLASS for NtQueryVolumeInformationFile
_FILE_FS_ATTRIBUTE_INFORMATION = 5
_VOLUME_NAME_NT = 0x2  # GetFinalPathNameByHandleW: \Device\HarddiskVolumeN\...

FILE_DEVICE_DISK = 0x07  # FILE_FS_DEVICE_INFORMATION.DeviceType
FILE_REMOVABLE_MEDIA = 0x01  # FILE_FS_DEVICE_INFORMATION.Characteristics
FILE_REMOTE_DEVICE = 0x10

_EPOCH_AS_FILETIME = 116_444_736_000_000_000  # 1970-01-01 in 100 ns units since 1601

ERROR_ALREADY_EXISTS = 183
ERROR_FILE_EXISTS = 80
ERROR_NOT_SAME_DEVICE = 17
ERROR_SHARING_VIOLATION = 32
ERROR_INVALID_HANDLE = 6
ERROR_INSUFFICIENT_BUFFER = 122
STATUS_OBJECT_NAME_COLLISION = 0xC000_0035

# The only volumes a root is proven on: a local NTFS disk volume.
VERIFIED_FILESYSTEMS = frozenset({"NTFS"})
# A drive letter root such as C:\; UNC shares and \\?\ device paths are not supported.
_DRIVE_ROOT = re.compile(r"[A-Za-z]:\\\Z")
# What the drive letter of a disk volume is defined as (QueryDosDeviceW). A subst drive
# is \??\C:\dir (or \??\C: for a whole volume), a network drive
# \Device\LanmanRedirector\...; both are refused.
_VOLUME_DEVICE = re.compile(r"\\Device\\HarddiskVolume[0-9]+\Z", re.IGNORECASE)
# NTFS keeps its root directory in MFT record 5; the file id is sequence << 48 | record.
_NTFS_ROOT_RECORD = 5
_MFT_RECORD_MASK = (1 << 48) - 1


@dataclass(frozen=True, slots=True)
class HandleInfo:
    """What a handle refers to, read from the handle itself (not from a path).

    ``file_id`` is the 128-bit ``FILE_ID_INFO`` id, ``volume_serial`` its volume; both
    are what ``os.stat`` reports as ``st_ino`` and ``st_dev``. ``mtime_ns`` uses the
    same epoch and unit as ``st_mtime_ns``.
    """

    volume_serial: int
    file_id: int
    attributes: int
    reparse_tag: int
    size: int
    mtime_ns: int
    links: int
    delete_pending: bool
    directory: bool

    @property
    def reparse_point(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_REPARSE_POINT) or self.reparse_tag != 0


@dataclass(frozen=True, slots=True)
class VolumeInfo:
    """The volume a handle is on, read from the handle: e.g. ``NTFS``, local or not."""

    filesystem: str
    device_type: int
    characteristics: int

    @property
    def remote(self) -> bool:
        return bool(self.characteristics & FILE_REMOTE_DEVICE)

    @property
    def removable(self) -> bool:
        return bool(self.characteristics & FILE_REMOVABLE_MEDIA)


class HandleState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"  # CloseHandle returned TRUE
    DETACHED = "detached"  # ownership was handed over (``detach``); not closed here
    CLOSE_FAILED = "close failed"  # CloseHandle did not confirm it: may still be open
    # Only for a handle made by ``Handle.new`` (``winprims``, R3B-3):
    NEW = "new"  # registered, no call made yet
    CALLING = "calling"  # the open call was entered; its status is not classified yet
    NOT_OPENED = "not opened"  # the call failed (or was never made): nothing to close
    UNKNOWN = "unknown"  # what the call did is not known: never closed, kept alive


class HandleCloseError(OSError):
    """``CloseHandle`` did not confirm that a handle was released.

    ``winerror`` is ``GetLastError`` read right after the call (0 if the call itself
    never returned); ``handle_value`` is the raw handle, for diagnostics only.
    """

    handle_value: int


class HandleUnknownError(HandleCloseError):
    """A handle of ``Handle.new`` is ``UNKNOWN``: its open call may have opened something
    whose status was never captured (or not trusted), so it is not closed at all.

    ``winerror`` is 0; ``handle_value`` is the observed output value or 0 and only for
    diagnostics: it is never passed to ``CloseHandle``.
    """


class OpenOutcome(StrEnum):
    """What one ``NtCreateFile`` of a ``Handle.new`` handle did (FIX-03 §2.5)."""

    NOT_CALLED = "not called"  # row 1
    UNCERTAIN = "uncertain"  # row 2: the status was not captured
    CREATED_OWNED = "created owned"  # row 3
    OPENED_OWNED = "opened owned"  # rows 4, 7, 8 (``unreliable`` for 4 and 8)
    NAME_TAKEN = "name taken"  # row 5
    NOT_CREATED_CLAIMED = "not created claimed"  # row 6
    NOT_OPENED = "not opened"  # row 9
    ANOMALY = "anomaly"  # rows 10, 11
    UNPROVEN = "unproven"  # row 12: STATUS_PENDING, another success, information, warning


# H_DIR: a category directory created by ``aclcreate`` (R3B-7A, isolated prototype)
ROLES = frozenset({"H_SRC", "H_TMP", "H_LOOKUP", "H_PROBE", "H_DIR"})
STATUS_PENDING = 0x0000_0103
# Objects whose memory the kernel may still write to (a status that was not captured or
# not standard): kept here until the process exits, never cleared (FIX-03 §2.4).
_UNRESOLVED: list[object] = []


def _pin(obj: object) -> None:
    if not any(o is obj for o in _UNRESOLVED):
        _UNRESOLVED.append(obj)


def _require(condition: bool, message: str) -> None:
    """A precondition that also holds under ``python -O`` (asserts would be stripped)."""
    if not condition:
        raise AssertionError(message)


def signed32(value: int) -> int:
    value &= 0xFFFF_FFFF
    return value - (1 << 32) if value & 0x8000_0000 else value


def _close_error(raw: int, path: Path, winerror: int | None) -> HandleCloseError:
    if winerror is None:
        reason = "the call did not complete"
    else:
        reason = ctypes.FormatError(winerror).strip() if AVAILABLE else f"error {winerror}"
    exc = HandleCloseError(
        0, f"CloseHandle failed for handle {raw:#x}: {reason}", str(path), winerror or 0
    )
    exc.handle_value = raw
    return exc


class ReleaseError(Exception):
    """An operation failed, and releasing what it held failed too.

    ``error`` is the original failure (also ``__cause__``) and stays the message;
    ``releases`` are the release failures as they were raised (a ``HandleCloseError``
    has ``winerror``, ``handle_value`` and the path), none hidden by another.
    """

    def __init__(self, error: Exception, releases: Iterable[BaseException]) -> None:
        self.error = error
        self.releases = tuple(releases)
        self.release_errors = self.releases  # what ``release_failures`` follows
        super().__init__(f"{type(error).__name__}: {error}")


# Release failures are kept as objects, never only as text: an exception that had to
# be raised while others failed carries them in ``release_errors`` (a tuple, like
# ``journal.JournalError.release_errors``), each of which may carry more in turn. An
# interrupt raised in place of another error (see ``prevailing``) carries that one too.


def attach_release_errors(exc: BaseException, errors: Iterable[BaseException]) -> BaseException:
    """Add ``errors`` to ``exc.release_errors`` (kept in order, each object once, never
    ``exc`` itself); returns ``exc``. Works on any exception, interrupts included."""
    current = tuple(getattr(exc, "release_errors", ()))
    seen = {id(exc), *(id(e) for e in current)}
    added: list[BaseException] = []
    for error in errors:
        if id(error) not in seen:
            seen.add(id(error))
            added.append(error)
    exc.release_errors = current + tuple(added)  # type: ignore[attr-defined]
    return exc


def release_failures(exc: BaseException) -> list[BaseException]:
    """Every release failure kept on ``exc``, nested ones included (depth first, each
    object once, ``exc`` itself not): a ``HandleCloseError`` inside a ``SystemExit``
    inside the ``KeyboardInterrupt`` a run ended with is found as well.

    The operation error of a ``ReleaseError`` (``error``) is not a release failure, but
    what it carries is, and it was released before: ``prevailing`` applied twice gives a
    ``ReleaseError`` around a ``ReleaseError``, and both release failures are found, in
    the order they happened. Objects are compared by identity; a cycle ends the walk.
    """
    found: list[BaseException] = []
    listed = {id(exc)}  # in ``found`` (``exc`` never)
    walked: set[int] = set()  # what they carry is on the stack already
    # (error, is a release failure); a stack, not recursion: no depth limit
    pending: list[tuple[BaseException, bool]] = [(exc, False)]
    while pending:
        error, failure = pending.pop()
        if failure:
            if id(error) in listed:
                continue
            listed.add(id(error))
            found.append(error)
        if id(error) in walked:
            continue
        walked.add(id(error))
        nested = [(e, True) for e in tuple(getattr(error, "release_errors", ()))]
        if isinstance(error, ReleaseError):
            nested.insert(0, (error.error, False))  # its earlier releases come first
        pending.extend(reversed(nested))
    return found


def describe_error(exc: BaseException) -> str:
    """Type, message and notes of ``exc``, for a report (diagnostics are never parsed
    back: the objects themselves are kept, see ``release_failures``)."""
    text = f"{type(exc).__name__}: {exc}"
    notes = getattr(exc, "__notes__", None)
    if notes:
        text += " [notes: " + " | ".join(str(note) for note in notes) + "]"
    return text


class UnverifiedRootError(Exception):
    """A volume root could not be proven to be the root of a local NTFS disk volume.

    Not an ``OSError``: it says that nothing may be done on that volume, not that an
    operation failed. ``__cause__`` is the ``OSError`` behind it, if there was one.
    """


def prevailing(error: BaseException, release_error: BaseException) -> BaseException:
    """Which of ``error`` (an operation failed or was interrupted) and ``release_error``
    (releasing what it held failed afterwards) is to be raised; the other one is kept on
    it as an object (``release_errors``) and in a note.

    An interrupted ``error`` (``KeyboardInterrupt``, ``SystemExit``, any non-``Exception``)
    prevails, the very object, over any ``release_error``, another interrupt included.
    After an ``Exception`` an interrupted release prevails: an interrupt is never
    dropped. Two ``Exception``\\s give a new ``ReleaseError`` carrying both.
    """
    if not isinstance(error, Exception):
        error.add_note(f"then releasing failed: {describe_error(release_error)}")
        attach_release_errors(error, [release_error])
        return error
    if not isinstance(release_error, Exception):
        release_error.add_note(f"while releasing after {describe_error(error)}")
        attach_release_errors(release_error, [error])
        return release_error
    released = ReleaseError(error, [release_error])
    released.__cause__ = error
    return released


def release_after(error: BaseException, release: Callable[[], object]) -> None:
    """Call ``release`` while ``error`` is being raised; keep a failure of it as well.

    The caller re-raises ``error`` itself; this raises only what ``prevailing`` puts
    in its place: an interrupt of the release after an ``Exception``, or ``ReleaseError``.
    """
    try:
        release()
    except BaseException as release_error:
        raised = prevailing(error, release_error)
        if raised is release_error:
            raise
        if raised is not error:
            raise raised from error


class Handle:
    """An owned file or directory handle; close it with ``close`` or ``with``.

    States: ``OPEN`` -> ``CLOSED`` (CloseHandle returned TRUE), ``DETACHED`` (handed
    over by ``detach``) or ``CLOSE_FAILED``. ``CloseHandle`` is called at most once:
    after a success ``close`` is a no-op, after a failure it raises the same
    ``HandleCloseError`` again without retrying. Only an ``OPEN`` handle has ``raw``.

    ``Handle(raw, path)`` owns a handle that is already open (the R3A paths).
    ``Handle.new(path, role)`` makes the owner before the open call (COPY, R3B-3):
    ``NEW`` -> ``CALLING`` -> ``classify``: ``OPEN`` only through ``_bind``, else
    ``NOT_OPENED`` or ``UNKNOWN``. Closing a ``NEW`` one makes it ``NOT_OPENED``;
    closing a ``CALLING`` one makes it ``UNKNOWN``; an ``UNKNOWN`` one is never given to
    ``CloseHandle``, ``close`` raises the same ``HandleUnknownError`` every time.
    """

    # Class defaults, set per object only by ``new`` (an R3A handle keeps these).
    role: str | None = None
    outcome: OpenOutcome | None = None
    unreliable = False
    _status: int | None = None
    _out: ctypes.c_void_p | None = None
    _iosb: ctypes.Structure | None = None
    _keep: object = None

    def __init__(self, raw: int, path: Path) -> None:
        self._raw: int | None = raw
        self.path = path  # the path it was opened as, for messages only
        self.state = HandleState.OPEN
        self.close_error: HandleCloseError | None = None

    @classmethod
    def new(cls, path: Path, role: str) -> Handle:
        """An owner without a handle yet (``NEW``), to be registered before the call."""
        _require(role in ROLES, f"unknown handle role {role!r}")
        handle = cls.__new__(cls)  # not __init__: that one binds an open raw handle
        handle._raw = None
        handle.path = path
        handle.state = HandleState.NEW
        handle.close_error = None
        handle.role = role  # for diagnostics and tests only
        handle.outcome = None  # OpenOutcome, set once by ``classify``
        handle.unreliable = False  # rows 4 and 8: opened, but the IOSB disagrees
        handle._status = None  # the captured NTSTATUS (signed), None if not captured
        handle._out = None  # the HANDLE buffer the call writes to
        handle._iosb = None  # its IO_STATUS_BLOCK
        handle._keep = None  # the other buffers of the call
        return handle

    def _bind(self, raw: int) -> None:
        """The only CALLING -> OPEN transition: once per object."""
        _require(
            self.state is HandleState.CALLING and self._raw is None,
            f"_bind of a {self.state} handle for {self.path}",
        )
        self._raw = raw
        self.state = HandleState.OPEN

    def _become_unknown(self, outcome: OpenOutcome, reason: str, *, pin: bool) -> None:
        observed = 0 if self._out is None else self._out.value or 0
        self.state = HandleState.UNKNOWN
        self.outcome = outcome
        if pin:
            _pin(self)
        error = HandleUnknownError(
            0,
            f"handle {observed:#x} ({reason}) may be open and is not closed",
            str(self.path),
            0,
        )
        error.handle_value = observed
        self.close_error = error

    def classify(self, disposition: int) -> OpenOutcome:
        """The outcome of the open call (FIX-03 §2.5), from the captured status only.

        Idempotent; reads ``_status``, ``_out`` and ``_iosb``; binds through ``_bind``
        (rows 3, 4, 7, 8) and nothing else. The IOSB never stands in for the status.
        """
        _require(disposition in (_FILE_CREATE, _FILE_OPEN), f"disposition {disposition}")
        if self.outcome is not None:
            return self.outcome
        if self.state is HandleState.NEW:  # row 1
            self.state = HandleState.NOT_OPENED
            self.outcome = OpenOutcome.NOT_CALLED
            return self.outcome
        _require(self.state is HandleState.CALLING, f"classify of a {self.state} handle")
        if self._status is None:  # row 2
            self._become_unknown(OpenOutcome.UNCERTAIN, "status not captured", pin=True)
            return OpenOutcome.UNCERTAIN
        status = self._status & 0xFFFF_FFFF
        out = self._out.value or 0
        invalid = out in (0, ctypes.c_void_p(-1).value)
        information = self._iosb.Information
        iosb_status = signed32(self._iosb.Status or 0)
        if status == 0:
            if invalid:  # row 11
                self._become_unknown(OpenOutcome.ANOMALY, "success without a handle", pin=False)
                return OpenOutcome.ANOMALY
            self._bind(out)
            expected = _FILE_CREATED if disposition == _FILE_CREATE else _FILE_OPENED
            consistent = information == expected and iosb_status == 0
            if disposition == _FILE_CREATE and consistent:  # row 3
                self.outcome = OpenOutcome.CREATED_OWNED
            else:  # rows 4, 7, 8
                self.outcome = OpenOutcome.OPENED_OWNED
                self.unreliable = not consistent
            return self.outcome
        if status >> 30 == 3:  # the error class
            if out != 0:  # row 10
                self._become_unknown(
                    OpenOutcome.ANOMALY, f"error {status:#x} with a handle", pin=True
                )
                return OpenOutcome.ANOMALY
            self.state = HandleState.NOT_OPENED
            if disposition == _FILE_OPEN:  # row 9
                self.outcome = OpenOutcome.NOT_OPENED
            elif status == STATUS_OBJECT_NAME_COLLISION:  # row 5
                self.outcome = OpenOutcome.NAME_TAKEN
            else:  # row 6
                self.outcome = OpenOutcome.NOT_CREATED_CLAIMED
            return self.outcome
        # row 12: STATUS_PENDING, another success or information, a warning
        self._become_unknown(OpenOutcome.UNPROVEN, f"status {status:#x}", pin=True)
        return OpenOutcome.UNPROVEN

    @property
    def status(self) -> int | None:
        """The captured NTSTATUS of the open call (unsigned), None if not captured."""
        return None if self._status is None else self._status & 0xFFFF_FFFF

    @property
    def raw(self) -> int:
        if self._raw is None:
            raise ValueError(f"handle for {self.path} is {self.state}")
        return self._raw

    def close(self) -> None:
        """Close the handle once; raises ``HandleCloseError`` if that is not confirmed."""
        if self.state is HandleState.NEW:
            self.state = HandleState.NOT_OPENED
            return
        if self.state is HandleState.CALLING:  # the call was left without a status
            self._become_unknown(OpenOutcome.UNCERTAIN, "status not captured", pin=True)
        if self.state in (HandleState.CLOSE_FAILED, HandleState.UNKNOWN):
            assert self.close_error is not None
            raise self.close_error
        if self.state is not HandleState.OPEN:
            return
        raw, self._raw = self.raw, None
        # Until CloseHandle returns TRUE the handle counts as not released.
        self.state = HandleState.CLOSE_FAILED
        self.close_error = _close_error(raw, self.path, None)
        if not _api().kernel32.CloseHandle(raw):
            self.close_error = _close_error(raw, self.path, ctypes.get_last_error())
            raise self.close_error
        self.state = HandleState.CLOSED
        self.close_error = None

    def detach(self) -> int:
        """Give up ownership: return the raw handle; ``close`` will not close it."""
        raw = self.raw
        self._raw = None
        self.state = HandleState.DETACHED
        return raw

    def __enter__(self) -> Handle:
        return self

    def __exit__(self, kind: object, error: BaseException | None, tb: object) -> None:
        if error is None:
            self.close()
        else:  # a failed close never replaces the error leaving the block
            release_after(error, self.close)

    def __repr__(self) -> str:
        return f"<Handle {self.state} {self.path}>"


def _single_component(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or any(c in name for c in "\\/:\0")
    ):
        raise ValueError(f"not a single path component: {name!r}")
    return name


def _os_error(winerror: int, path: Path | None, ntstatus: int | None = None) -> OSError:
    message = ctypes.FormatError(winerror).strip() if AVAILABLE else f"error {winerror}"
    # With winerror set, Python picks the matching subclass (FileExistsError, ...).
    exc = OSError(0, message, None if path is None else str(path), winerror)
    exc.ntstatus = ntstatus  # type: ignore[attr-defined]
    return exc


class _Api:
    """ctypes prototypes, built on first use (Windows only)."""

    def __init__(self) -> None:
        from ctypes import wintypes

        self.wintypes = wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll")
        handle, dword, bool_ = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL

        k = self.kernel32
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, dword, dword, ctypes.c_void_p, dword, dword, handle
        ]  # fmt: skip
        k.CreateFileW.restype = handle
        k.CloseHandle.argtypes = [handle]
        k.CloseHandle.restype = bool_
        k.GetFileInformationByHandleEx.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, dword]
        k.GetFileInformationByHandleEx.restype = bool_
        k.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, dword]
        k.QueryDosDeviceW.restype = dword
        k.GetFinalPathNameByHandleW.argtypes = [handle, wintypes.LPWSTR, dword, dword]
        k.GetFinalPathNameByHandleW.restype = dword

        class UnicodeString(ctypes.Structure):
            _fields_ = (
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p),
            )

        class ObjectAttributes(ctypes.Structure):
            _fields_ = (
                ("Length", wintypes.ULONG),
                ("RootDirectory", handle),
                ("ObjectName", ctypes.POINTER(UnicodeString)),
                ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", ctypes.c_void_p),
                ("SecurityQualityOfService", ctypes.c_void_p),
            )

        class IoStatusBlock(ctypes.Structure):
            _fields_ = (("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t))

        self.UnicodeString = UnicodeString
        self.ObjectAttributes = ObjectAttributes
        self.IoStatusBlock = IoStatusBlock
        n = self.ntdll
        n.NtCreateFile.argtypes = [
            ctypes.POINTER(handle), dword, ctypes.POINTER(ObjectAttributes),
            ctypes.POINTER(IoStatusBlock), ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG,
            wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
        ]  # fmt: skip
        n.NtCreateFile.restype = ctypes.c_long
        n.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
        n.RtlNtStatusToDosError.restype = wintypes.ULONG
        n.NtSetInformationFile.argtypes = [
            handle, ctypes.POINTER(IoStatusBlock), ctypes.c_void_p, wintypes.ULONG, ctypes.c_int
        ]  # fmt: skip
        n.NtSetInformationFile.restype = ctypes.c_long
        n.NtQueryVolumeInformationFile.argtypes = [
            handle, ctypes.POINTER(IoStatusBlock), ctypes.c_void_p, wintypes.ULONG, ctypes.c_int
        ]  # fmt: skip
        n.NtQueryVolumeInformationFile.restype = ctypes.c_long

        class FsDeviceInfo(ctypes.Structure):
            _fields_ = (("DeviceType", wintypes.ULONG), ("Characteristics", wintypes.ULONG))

        class FsAttributeInfo(ctypes.Structure):
            _fields_ = (
                ("FileSystemAttributes", wintypes.ULONG),
                ("MaximumComponentNameLength", wintypes.LONG),
                ("FileSystemNameLength", wintypes.ULONG),  # in bytes
                ("FileSystemName", wintypes.WCHAR * 1),
            )

        self.FsDeviceInfo = FsDeviceInfo
        self.FsAttributeInfo = FsAttributeInfo

        class BasicInfo(ctypes.Structure):
            _fields_ = (
                ("CreationTime", ctypes.c_int64),
                ("LastAccessTime", ctypes.c_int64),
                ("LastWriteTime", ctypes.c_int64),
                ("ChangeTime", ctypes.c_int64),
                ("FileAttributes", dword),
            )

        class StandardInfo(ctypes.Structure):
            _fields_ = (
                ("AllocationSize", ctypes.c_int64),
                ("EndOfFile", ctypes.c_int64),
                ("NumberOfLinks", dword),
                ("DeletePending", wintypes.BOOLEAN),
                ("Directory", wintypes.BOOLEAN),
            )

        class AttributeTagInfo(ctypes.Structure):
            _fields_ = (("FileAttributes", dword), ("ReparseTag", dword))

        class IdInfo(ctypes.Structure):
            _fields_ = (("VolumeSerialNumber", ctypes.c_uint64), ("FileId", ctypes.c_ubyte * 16))

        class RenameInfo(ctypes.Structure):
            _fields_ = (
                ("ReplaceIfExists", dword),  # union with Flags; always 0 here
                ("RootDirectory", handle),
                ("FileNameLength", dword),
                ("FileName", wintypes.WCHAR * 1),
            )

        self.BasicInfo = BasicInfo
        self.StandardInfo = StandardInfo
        self.AttributeTagInfo = AttributeTagInfo
        self.IdInfo = IdInfo
        self.RenameInfo = RenameInfo
        self.invalid_handle = ctypes.c_void_p(-1).value


_API: _Api | None = None


def _api() -> _Api:
    global _API
    if not AVAILABLE:
        raise OSError("Windows file operations are not available on this platform")
    if _API is None:
        _API = _Api()
    return _API


def open_anchor(anchor: Path) -> Handle:
    """Open a volume root such as ``C:\\`` or ``\\\\server\\share\\`` (a directory)."""
    if str(anchor) != anchor.anchor or not anchor.anchor:
        raise ValueError(f"not a volume root: {anchor}")
    api = _api()
    raw = api.kernel32.CreateFileW(
        str(anchor),
        FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if raw is None or raw == api.invalid_handle:
        raise _os_error(ctypes.get_last_error(), anchor)
    return Handle(raw, anchor)


def query_dos_device(name: str) -> tuple[str, ...]:
    """The targets the DOS device ``name`` (such as ``C:``) is defined as, current first.

    E.g. ``("\\Device\\HarddiskVolume3",)`` for a volume's drive letter, or
    ``("\\??\\C:\\dir",)`` for a ``subst`` drive. An undefined name raises
    ``FileNotFoundError``; any other failure ``OSError``.
    """
    if not name or any(c in name for c in "\\/\0"):
        raise ValueError(f"not a DOS device name: {name!r}")
    api = _api()
    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        count = api.kernel32.QueryDosDeviceW(name, buffer, size)
        if count:
            break
        error = ctypes.get_last_error()
        if error != ERROR_INSUFFICIENT_BUFFER or size >= 1 << 20:
            raise _os_error(error, Path(name))
        size *= 4
    return tuple(target for target in buffer[:count].split("\0") if target)


def final_nt_path(handle: Handle) -> str:
    """The normalized NT path of the object ``handle`` refers to, read from the handle.

    E.g. ``\\Device\\HarddiskVolume3\\`` for the root of that volume: the device the
    object is on and its path inside that device, whatever name it was opened by.
    """
    api = _api()
    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        count = api.kernel32.GetFinalPathNameByHandleW(handle.raw, buffer, size, _VOLUME_NAME_NT)
        if not count:
            raise _os_error(ctypes.get_last_error(), handle.path)
        if count < size:
            return buffer[:count]
        size = count + 1  # count is the size needed, terminator included


@dataclass(frozen=True, slots=True)
class VolumeRoot:
    """A drive letter (``C:``) and the disk volume device it was defined as."""

    drive: str
    device: str  # e.g. \\Device\\HarddiskVolume3

    @property
    def anchor(self) -> Path:
        return Path(self.drive + "\\")


def drive_device(drive: str) -> str:
    """The disk volume device the drive letter ``drive`` (``C:``) is defined as.

    ``UnverifiedRootError`` for anything else: a subst (also of a whole volume) or
    network drive, an undefined, an ambiguous (several definitions) or an unreadable
    mapping.
    """
    try:
        targets = query_dos_device(drive)
    except (OSError, ValueError) as exc:
        raise UnverifiedRootError(
            f"cannot read what {drive} is mapped to ({type(exc).__name__}: {exc})"
        ) from exc
    if len(targets) != 1 or not _VOLUME_DEVICE.match(targets[0]):
        raise UnverifiedRootError(
            f"{drive} is not the drive letter of a disk volume"
            f" (defined as {', '.join(map(repr, targets)) or 'nothing'});"
            " subst and other mapped drives are not supported"
        )
    return targets[0]


def volume_root(path: Path) -> VolumeRoot:
    """The drive letter of ``path`` and the disk volume it is defined as, read now.

    Only the first step of the proof: the object a later ``open_anchor`` opens still
    has to be checked with ``prove_volume_root``. UNC and device paths are refused.
    """
    if not _DRIVE_ROOT.match(path.anchor):
        raise UnverifiedRootError(
            f"{path}: only a directory on a local drive letter (like C:\\) is supported"
        )
    return VolumeRoot(path.drive, drive_device(path.drive))


def check_mapping(root: VolumeRoot) -> None:
    """``UnverifiedRootError`` unless ``root.drive`` still is ``root.device``."""
    if drive_device(root.drive) != root.device:
        raise UnverifiedRootError(f"{root.drive} was redefined while it was checked")


def prove_volume_root(handle: Handle, root: VolumeRoot) -> None:
    """Prove that ``handle`` (``open_anchor(root.anchor)``) is the root of ``root.device``.

    Read from the handle itself: a local (not remote, not removable) NTFS disk volume,
    a directory that is no reparse point, MFT record 5 and the final NT path
    ``<device>\\``; then the mapping is read again, since it could have changed while
    the anchor was opened. Raises ``UnverifiedRootError`` (an ``OSError`` of a query
    as its ``__cause__``); the caller still owns and closes ``handle``.
    """
    try:
        volume = query_volume(handle)
        if (
            volume.filesystem not in VERIFIED_FILESYSTEMS
            or volume.device_type != FILE_DEVICE_DISK
            or volume.remote
            or volume.removable
        ):
            kind = "remote" if volume.remote else "removable" if volume.removable else "local"
            raise UnverifiedRootError(
                f"{handle.path}: {kind} {volume.filesystem or '?'} volume"
                f" (device type {volume.device_type:#x}); only a local NTFS disk is supported"
            )
        info = query(handle)
        if (
            not info.directory
            or info.reparse_point
            or info.file_id >> 64
            or info.file_id & _MFT_RECORD_MASK != _NTFS_ROOT_RECORD
        ):
            raise UnverifiedRootError(
                f"{root.anchor} is not the root directory of its volume"
                f" (file id {info.file_id:#x}); subst and other mapped drives are not supported"
            )
        final = final_nt_path(handle)
    except OSError as exc:
        raise UnverifiedRootError(
            f"cannot check that {handle.path} is the root of {root.device}:"
            f" {type(exc).__name__}: {exc}"
        ) from exc
    if final.upper() != (root.device + "\\").upper():
        raise UnverifiedRootError(
            f"{root.anchor} opened {final!r}, not the root of {root.device!r}"
        )
    check_mapping(root)


def open_relative(
    parent: Handle,
    name: str,
    *,
    directory: bool,
    access: int,
    share: int,
    create: bool = False,
) -> tuple[Handle, bool]:
    """Open (or with ``create`` atomically create) child ``name`` of ``parent``.

    The name is looked up in the directory ``parent`` refers to, case-insensitively as
    Win32 does, and a final reparse point is opened as itself, never followed.
    ``directory`` demands a directory (``True``) or a non-directory (``False``).
    ``create`` creates a new directory and fails with ``FileExistsError`` if the name
    exists in any form. Returns the handle and whether it was created.
    """
    options = _FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE
    if create:
        if not directory:
            raise ValueError("open_relative creates directories only; use create_file")
        return _nt_create(
            parent, name, access, share, _FILE_CREATE, options, FILE_ATTRIBUTE_DIRECTORY
        )
    return _nt_create(parent, name, access, share, _FILE_OPEN, options, 0)


def create_file(parent: Handle, name: str, *, access: int, share: int) -> Handle:
    """Atomically create the new regular file ``name`` in the directory ``parent``.

    ``FILE_CREATE``: an existing ``name`` in any form (file, directory, link) fails with
    ``FileExistsError``; nothing is opened, followed or truncated.
    """
    handle, created = _nt_create(
        parent, name, access, share, _FILE_CREATE, _FILE_NON_DIRECTORY_FILE,
        _FILE_ATTRIBUTE_NORMAL,
    )  # fmt: skip
    if not created:  # FILE_CREATE either creates or fails; never reached
        handle.close()
        raise OSError(f"{handle.path} was not created")
    return handle


def open_stream(handle: Handle) -> BinaryIO:
    """A buffered binary stream that takes over ``handle`` (opened for writing).

    Ownership of the OS handle moves exactly once, and it is never closed twice:

    * ``open_osfhandle`` fails: nothing took the handle over. ``handle`` stays ``OPEN``
      and stays the caller's, who closes it (and reports a failed ``CloseHandle``).
    * Then the C runtime descriptor owns it and ``handle`` is ``DETACHED``. If the
      stream cannot be built on it, the descriptor (or the raw file holding it) is
      closed here, once; if that fails as well, ``ReleaseError`` carries both errors.
    * On success the stream owns it: closing the stream closes it.
    """
    import msvcrt

    fd = msvcrt.open_osfhandle(handle.raw, os.O_WRONLY | os.O_BINARY)  # type: ignore[attr-defined]
    handle.detach()  # from here on the descriptor owns the OS handle
    try:
        raw = FileIO(fd, "wb")  # from an int fd: a failing FileIO leaves the fd open
    except BaseException as exc:
        release_after(exc, lambda: os.close(fd))
        raise
    try:
        return BufferedWriter(raw)  # type: ignore[return-value]
    except BaseException as exc:
        release_after(exc, raw.close)  # the raw file owns the descriptor now
        raise


def _nt_create(
    parent: Handle,
    name: str,
    access: int,
    share: int,
    disposition: int,
    options: int,
    attributes_on_create: int,
) -> tuple[Handle, bool]:
    _single_component(name)
    api = _api()
    buffer = ctypes.create_unicode_buffer(name)
    length = len(name.encode("utf-16-le", "surrogatepass"))  # UTF-16 units, not code points
    uname = api.UnicodeString(length, length + 2, ctypes.addressof(buffer))
    attributes = api.ObjectAttributes(
        ctypes.sizeof(api.ObjectAttributes), parent.raw, ctypes.pointer(uname),
        _OBJ_CASE_INSENSITIVE, None, None,
    )  # fmt: skip
    iosb = api.IoStatusBlock()
    raw = api.wintypes.HANDLE()
    status = api.ntdll.NtCreateFile(
        ctypes.byref(raw),
        access | SYNCHRONIZE,
        ctypes.byref(attributes),
        ctypes.byref(iosb),
        None,
        attributes_on_create,
        share,
        disposition,
        options | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT,
        None,
        0,
    )
    path = parent.path / name
    if status < 0:
        unsigned = status & 0xFFFF_FFFF
        raise _os_error(api.ntdll.RtlNtStatusToDosError(status), path, unsigned)
    return Handle(raw.value or 0, path), iosb.Information == _FILE_CREATED


def _query(handle: Handle, info_class: int, struct: type[ctypes.Structure]) -> ctypes.Structure:
    api = _api()
    result = struct()
    if not api.kernel32.GetFileInformationByHandleEx(
        handle.raw, info_class, ctypes.byref(result), ctypes.sizeof(result)
    ):
        raise _os_error(ctypes.get_last_error(), handle.path)
    return result


def query(handle: Handle) -> HandleInfo:
    """Identity, attributes, size and mtime of what ``handle`` refers to."""
    api = _api()
    basic = _query(handle, _FILE_BASIC_INFO, api.BasicInfo)
    standard = _query(handle, _FILE_STANDARD_INFO, api.StandardInfo)
    tag = _query(handle, _FILE_ATTRIBUTE_TAG_INFO, api.AttributeTagInfo)
    ids = _query(handle, _FILE_ID_INFO, api.IdInfo)
    return HandleInfo(
        volume_serial=ids.VolumeSerialNumber,  # type: ignore[attr-defined]
        file_id=int.from_bytes(bytes(ids.FileId), "little"),  # type: ignore[attr-defined]
        attributes=tag.FileAttributes | basic.FileAttributes,  # type: ignore[attr-defined]
        reparse_tag=tag.ReparseTag,  # type: ignore[attr-defined]
        size=standard.EndOfFile,  # type: ignore[attr-defined]
        mtime_ns=(basic.LastWriteTime - _EPOCH_AS_FILETIME) * 100,  # type: ignore[attr-defined]
        links=standard.NumberOfLinks,  # type: ignore[attr-defined]
        delete_pending=bool(standard.DeletePending),  # type: ignore[attr-defined]
        directory=bool(standard.Directory),  # type: ignore[attr-defined]
    )


def _query_volume(handle: Handle, info_class: int, buffer: ctypes.Array[ctypes.c_char]) -> None:
    api = _api()
    iosb = api.IoStatusBlock()
    status = api.ntdll.NtQueryVolumeInformationFile(
        handle.raw, ctypes.byref(iosb), buffer, len(buffer), info_class
    )
    if status < 0:
        unsigned = status & 0xFFFF_FFFF
        raise _os_error(api.ntdll.RtlNtStatusToDosError(status), handle.path, unsigned)


def query_volume(handle: Handle) -> VolumeInfo:
    """Filesystem name and device characteristics of the volume ``handle`` is on."""
    api = _api()
    device_buffer = ctypes.create_string_buffer(ctypes.sizeof(api.FsDeviceInfo))
    _query_volume(handle, _FILE_FS_DEVICE_INFORMATION, device_buffer)
    device = api.FsDeviceInfo.from_buffer(device_buffer)
    attribute_buffer = ctypes.create_string_buffer(1024)
    _query_volume(handle, _FILE_FS_ATTRIBUTE_INFORMATION, attribute_buffer)
    attributes = api.FsAttributeInfo.from_buffer(attribute_buffer)
    offset = api.FsAttributeInfo.FileSystemName.offset
    length = min(attributes.FileSystemNameLength, len(attribute_buffer) - offset)
    name = attribute_buffer.raw[offset : offset + length].decode("utf-16-le", "replace")
    return VolumeInfo(name, device.DeviceType, device.Characteristics)


def rename_into(handle: Handle, directory: Handle, name: str) -> None:
    """Rename the file of ``handle`` to ``name`` inside ``directory``; never replaces.

    ``FILE_RENAME_INFORMATION.ReplaceIfExists`` is ``FALSE``: an existing ``name`` (any case,
    8.3 alias, link or directory) makes the filesystem fail the call with
    ``FileExistsError``. Different volumes fail with ``ERROR_NOT_SAME_DEVICE``;
    nothing is copied.
    """
    _single_component(name)
    api = _api()
    offset = api.RenameInfo.FileName.offset
    name_bytes = len(name.encode("utf-16-le", "surrogatepass"))
    buffer = ctypes.create_string_buffer(
        max(offset + name_bytes + 2, ctypes.sizeof(api.RenameInfo))
    )
    info = api.RenameInfo.from_buffer(buffer)
    info.ReplaceIfExists = 0  # never MOVEFILE_REPLACE_EXISTING semantics
    info.RootDirectory = directory.raw
    info.FileNameLength = name_bytes
    ctypes.memmove(
        ctypes.addressof(buffer) + offset, ctypes.create_unicode_buffer(name), name_bytes
    )
    iosb = api.IoStatusBlock()
    status = api.ntdll.NtSetInformationFile(
        handle.raw, ctypes.byref(iosb), buffer, offset + name_bytes, _FILE_RENAME_INFORMATION
    )
    if status < 0:
        unsigned = status & 0xFFFF_FFFF
        raise _os_error(api.ntdll.RtlNtStatusToDosError(status), directory.path / name, unsigned)
