"""Windows primitives for a future cross-volume COPY (R3B-3, FIX-03 §2-§3).

Nothing here is wired to the executor or the GUI: the same-volume MOVE of R3A does
not use this module, and no COPY is done by the program yet. It only provides the
pieces the COPY state machine (R3B-4) is to be built from, each tested on its own.

* ``open_into`` / ``create_into`` open a handle into an owner made by ``Handle.new``
  (registered by the caller *before* the call); the result is classified only from the
  captured ``NTSTATUS`` (``Handle.classify``). A status that was not captured or is not
  standard leaves the handle ``UNKNOWN``: it is never closed, and its buffers are kept
  alive in ``winfileops._UNRESOLVED``.
* I/O at explicit offsets (``read_at``, ``write_at``), ``flush``, ``query_space``,
  ``set_mtime``, ``query_times`` (with ``ChangeTime``), ``query_streams``,
  ``query_ea_size``, ``query_name``.
* ``lookup_entry``: one directory query for one exact name (FIX-03 §3.4).
* ``dispose``: the delete disposition, POSIX first with the limited fallback to the
  classic one (§3.6, CL-1); ``rename_once``: one ``FileRenameInformation`` with
  ``ReplaceIfExists = FALSE`` and its status captured (§3.5, R-3).

Every NT call here is made on a handle opened with ``FILE_SYNCHRONOUS_IO_NONALERT``
(assumption A-SYNC: it has completed when the call returns; not proven, W-21). A
captured ``STATUS_PENDING`` or any other non-standard status is therefore never taken
as success, and the memory of such a call is kept alive (``_UNRESOLVED``).

Documented APIs (winternl.h / the WDK): ``NtCreateFile``, ``NtReadFile``,
``NtWriteFile``, ``NtFlushBuffersFile``, ``NtQueryInformationFile``
(``FileNameInformation``, ``FileEaInformation``, ``FileStreamInformation``),
``NtSetInformationFile`` (``FileBasicInformation``, ``FileRenameInformation``,
``FileDispositionInformation``, ``FileDispositionInformationEx``),
``NtQueryDirectoryFile`` (``FileIdExtdDirectoryInformation``),
``NtQueryVolumeInformationFile`` (``FileFsFullSizeInformation``).
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from downloads_organizer import winfileops
from downloads_organizer.winfileops import (
    _FILE_CREATE,
    _FILE_DIRECTORY_FILE,
    _FILE_NON_DIRECTORY_FILE,
    _FILE_OPEN,
    _FILE_OPEN_REPARSE_POINT,
    _FILE_SYNCHRONOUS_IO_NONALERT,
    _OBJ_CASE_INSENSITIVE,
    DELETE,
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_LIST_DIRECTORY,
    FILE_READ_ATTRIBUTES,
    FILE_SHARE_READ,
    SHARE_ALL,
    STATUS_OBJECT_NAME_COLLISION,
    SYNCHRONIZE,
    Handle,
    HandleState,
    OpenOutcome,
    _pin,
    _require,
    _single_component,
    release_after,
)

FILE_READ_DATA = 0x0001
FILE_WRITE_DATA = 0x0002
FILE_WRITE_ATTRIBUTES = 0x0100
READ_CONTROL = 0x0002_0000  # ``aclproof.READ_CONTROL`` (that module imports this one)

STATUS_SUCCESS = 0
STATUS_BUFFER_OVERFLOW = 0x8000_0005
STATUS_NO_MORE_FILES = 0x8000_0006
STATUS_INVALID_PARAMETER = 0xC000_000D
STATUS_NO_SUCH_FILE = 0xC000_000F
STATUS_END_OF_FILE = 0xC000_0011
STATUS_ACCESS_DENIED = 0xC000_0022
STATUS_BUFFER_TOO_SMALL = 0xC000_0023
STATUS_SHARING_VIOLATION = 0xC000_0043
STATUS_CANNOT_DELETE = 0xC000_0121
STATUS_INVALID_INFO_CLASS = 0xC000_0003
STATUS_NOT_IMPLEMENTED = 0xC000_0002
STATUS_NOT_SUPPORTED = 0xC000_00BB
# CL-1b: the classic disposition only after a POSIX one was refused with one of these.
FALLBACK_STATUSES = frozenset(
    {STATUS_INVALID_INFO_CLASS, STATUS_NOT_IMPLEMENTED, STATUS_NOT_SUPPORTED}
)

_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_FULL_SIZE_INFORMATION = 7  # FS_INFORMATION_CLASS
_FILE_BASIC_INFORMATION = 4  # FILE_INFORMATION_CLASS
_FILE_NAME_INFORMATION = 9
_FILE_EA_INFORMATION = 7
_FILE_RENAME_INFORMATION = 10
_FILE_DISPOSITION_INFORMATION = 13
_FILE_STREAM_INFORMATION = 22
_FILE_ID_EXTD_DIRECTORY_INFORMATION = 60
_FILE_DISPOSITION_INFORMATION_EX = 64
_FILE_DISPOSITION_DELETE = 0x1
_FILE_DISPOSITION_POSIX_SEMANTICS = 0x2

_LOOKUP_BUFFER = 64 * 1024
_MAX_COMPONENT = 255  # UTF-16 units
_LOOKUP_REJECTED = frozenset('*?<>"\\/:\0')
_MAX_INFO_BUFFER = 1 << 20


@dataclass(frozen=True, slots=True)
class Profile:
    """How a role's handle is opened (FIX-03 §2.1)."""

    access: int
    share: int
    disposition: int
    options: int
    attributes: int = 0


_FILE_OPTIONS = _FILE_NON_DIRECTORY_FILE | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT
PROFILES: dict[str, Profile] = {
    # READ_CONTROL: the source's descriptor is read through it (the copy's DACL, R3B)
    "H_SRC": Profile(
        FILE_READ_DATA | FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE, FILE_SHARE_READ,
        _FILE_OPEN, _FILE_OPTIONS,
    ),
    "H_TMP": Profile(
        FILE_READ_DATA | FILE_WRITE_DATA | FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES
        | DELETE | SYNCHRONIZE,
        0, _FILE_CREATE, _FILE_OPTIONS, _FILE_ATTRIBUTE_NORMAL,
    ),
    "H_PROBE": Profile(FILE_READ_ATTRIBUTES | SYNCHRONIZE, SHARE_ALL, _FILE_OPEN, _FILE_OPTIONS),
    "H_LOOKUP": Profile(
        FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE, SHARE_ALL, _FILE_OPEN,
        _FILE_DIRECTORY_FILE | _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT,
    ),
}  # fmt: skip


class StatusError(OSError):
    """An NT call returned an error status; ``ntstatus`` is that status (unsigned)."""

    ntstatus: int


class UnsupportedStatusError(StatusError):
    """An NT call returned a status that is neither success nor an error (``STATUS_PENDING``,
    another success, information, a warning): what it did is not established."""


class MalformedInfoError(OSError):
    """An information buffer did not have the expected shape: nothing is taken from it."""


def status_class(status: int) -> str:
    """``ok`` (0), ``error`` (severity error) or ``nonstandard`` (anything else)."""
    status &= 0xFFFF_FFFF
    if status == STATUS_SUCCESS:
        return "ok"
    return "error" if status >> 30 == 3 else "nonstandard"


@dataclass(eq=False)
class _Call:
    """The status and the memory of one NT call (kept alive if it did not complete)."""

    keep: tuple[object, ...]
    status: int | None = None  # signed, as returned; None if never captured


def _invoke(call: _Call, function: object, *args: object) -> int:
    """Make the call; capture its status in one statement. Unsigned status returned.

    Interrupted before the status was captured, or with a non-standard status: the
    memory of the call is pinned (``_UNRESOLVED``) and the interrupt is raised as is.
    """
    try:
        call.status = function(*args)  # type: ignore[operator]
    except BaseException:
        if call.status is None:
            _pin(call)
        raise
    status = call.status & 0xFFFF_FFFF
    if status_class(status) == "nonstandard":
        _pin(call)
    return status


def _status_error(status: int, path: Path | None) -> StatusError:
    api = _prims()
    winerror = api.ntdll.RtlNtStatusToDosError(winfileops.signed32(status))
    message = ctypes.FormatError(winerror).strip()
    kind = UnsupportedStatusError if status_class(status) == "nonstandard" else StatusError
    exc = kind(
        0, f"{message} (NTSTATUS {status:#010x})", None if path is None else str(path), winerror
    )
    exc.ntstatus = status
    return exc


def _check(status: int, path: Path | None) -> None:
    if status != STATUS_SUCCESS:
        raise _status_error(status, path)


class _Prims:
    """ctypes prototypes of this module: its own ntdll object, so the prototypes of
    ``winfileops`` stay as they are."""

    def __init__(self) -> None:
        from ctypes import wintypes

        base = winfileops._api()
        self.base = base
        self.wintypes = wintypes
        self.ntdll = ctypes.WinDLL("ntdll")
        handle, ulong, ptr = wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p
        iosb = ctypes.POINTER(base.IoStatusBlock)
        n = self.ntdll
        n.NtCreateFile.argtypes = [
            ctypes.POINTER(handle), wintypes.DWORD, ctypes.POINTER(base.ObjectAttributes), iosb,
            ptr, ulong, ulong, ulong, ulong, ptr, ulong,
        ]  # fmt: skip
        n.NtCreateFile.restype = ctypes.c_long
        for name in ("NtReadFile", "NtWriteFile"):
            function = getattr(n, name)
            function.argtypes = [
                handle, handle, ptr, ptr, iosb, ptr, ulong, ctypes.POINTER(ctypes.c_int64), ptr
            ]  # fmt: skip
            function.restype = ctypes.c_long
        n.NtFlushBuffersFile.argtypes = [handle, iosb]
        n.NtFlushBuffersFile.restype = ctypes.c_long
        for name in ("NtQueryInformationFile", "NtSetInformationFile"):
            function = getattr(n, name)
            function.argtypes = [handle, iosb, ptr, ulong, ctypes.c_int]
            function.restype = ctypes.c_long
        n.NtQueryVolumeInformationFile.argtypes = [handle, iosb, ptr, ulong, ctypes.c_int]
        n.NtQueryVolumeInformationFile.restype = ctypes.c_long
        n.NtQueryDirectoryFile.argtypes = [
            handle, handle, ptr, ptr, iosb, ptr, ulong, ctypes.c_int, wintypes.BOOLEAN,
            ctypes.POINTER(base.UnicodeString), wintypes.BOOLEAN,
        ]  # fmt: skip
        n.NtQueryDirectoryFile.restype = ctypes.c_long
        n.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
        n.RtlNtStatusToDosError.restype = ulong

        class FullSizeInfo(ctypes.Structure):
            _fields_ = (
                ("TotalAllocationUnits", ctypes.c_int64),
                ("CallerAvailableAllocationUnits", ctypes.c_int64),
                ("ActualAvailableAllocationUnits", ctypes.c_int64),
                ("SectorsPerAllocationUnit", ulong),
                ("BytesPerSector", ulong),
            )

        class DispositionInfoEx(ctypes.Structure):
            _fields_ = (("Flags", ulong),)

        class DispositionInfo(ctypes.Structure):
            _fields_ = (("DeleteFile", wintypes.BOOLEAN),)

        class EaInfo(ctypes.Structure):
            _fields_ = (("EaSize", ulong),)

        class NameInfo(ctypes.Structure):
            _fields_ = (("FileNameLength", ulong), ("FileName", wintypes.WCHAR * 1))

        class StreamInfo(ctypes.Structure):
            _fields_ = (
                ("NextEntryOffset", ulong),
                ("StreamNameLength", ulong),
                ("StreamSize", ctypes.c_int64),
                ("StreamAllocationSize", ctypes.c_int64),
                ("StreamName", wintypes.WCHAR * 1),
            )

        self.FullSizeInfo = FullSizeInfo
        self.DispositionInfoEx = DispositionInfoEx
        self.DispositionInfo = DispositionInfo
        self.EaInfo = EaInfo
        self.NameInfo = NameInfo
        self.StreamInfo = StreamInfo
        self.DirInfo = DirInfo


class DirInfo(ctypes.Structure):
    """``FILE_ID_EXTD_DIR_INFORMATION``; its layout is checked by ``_check_layout``."""

    _fields_ = (
        ("NextEntryOffset", ctypes.c_uint32),
        ("FileIndex", ctypes.c_uint32),
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("EndOfFile", ctypes.c_int64),
        ("AllocationSize", ctypes.c_int64),
        ("FileAttributes", ctypes.c_uint32),
        ("FileNameLength", ctypes.c_uint32),
        ("EaSize", ctypes.c_uint32),
        ("ReparsePointTag", ctypes.c_uint32),
        ("FileId", ctypes.c_ubyte * 16),
        ("FileName", ctypes.c_wchar * 1),
    )


_PRIMS: _Prims | None = None


def _prims() -> _Prims:
    global _PRIMS
    if _PRIMS is None:
        _PRIMS = _Prims()
    return _PRIMS


# --- opening into an owner (FIX-03 §2.4-§2.6) -----------------------------------------


def _nt_open_into(handle: Handle, parent: Handle, name: str, profile: Profile) -> OpenOutcome:
    _require(handle.state is HandleState.NEW, f"open into a {handle.state} handle")
    _require(
        bool(profile.options & _FILE_SYNCHRONOUS_IO_NONALERT),
        "an open without FILE_SYNCHRONOUS_IO_NONALERT",
    )
    api = _prims()
    base = api.base
    buffer = ctypes.create_unicode_buffer(name)
    length = len(name.encode("utf-16-le", "surrogatepass"))
    uname = base.UnicodeString(length, length + 2, ctypes.addressof(buffer) if name else None)
    attributes = base.ObjectAttributes(
        ctypes.sizeof(base.ObjectAttributes), parent.raw, ctypes.pointer(uname),
        _OBJ_CASE_INSENSITIVE, None, None,
    )  # fmt: skip
    handle._keep = (buffer, uname, attributes)
    handle._out = base.wintypes.HANDLE(0)
    handle._iosb = base.IoStatusBlock(0, 0xFFFF_FFFF)  # a sentinel: proves nothing
    handle._status = None
    handle.state = HandleState.CALLING
    try:
        handle._status = api.ntdll.NtCreateFile(
            ctypes.byref(handle._out),
            profile.access,
            ctypes.byref(attributes),
            ctypes.byref(handle._iosb),
            None,
            profile.attributes,
            profile.share,
            profile.disposition,
            profile.options,
            None,
            0,
        )
    except BaseException:
        if handle._status is None:  # row 2: what the call did is not known
            handle.classify(profile.disposition)
        raise
    return handle.classify(profile.disposition)


def open_into(handle: Handle, parent: Handle, name: str) -> OpenOutcome:
    """Open the existing ``name`` in the directory ``parent`` into ``handle``.

    ``handle`` is a ``Handle.new`` of role ``H_SRC``, ``H_PROBE`` (a file) or
    ``H_LOOKUP`` (a directory; ``name == ""`` opens ``parent`` itself again), opened as
    that role's ``PROFILES`` entry with ``FILE_OPEN``. Returns the outcome; an error
    status is not raised (``open_error`` makes one), an interrupt is.
    """
    _require(handle.role in ("H_SRC", "H_PROBE", "H_LOOKUP"), f"open_into for {handle.role}")
    if not (handle.role == "H_LOOKUP" and name == ""):
        _single_component(name)
    return _nt_open_into(handle, parent, name, PROFILES[handle.role])  # type: ignore[index]


def create_into(handle: Handle, parent: Handle, name: str) -> OpenOutcome:
    """Atomically create the new file ``name`` in ``parent`` into ``handle`` (``H_TMP``):
    ``FILE_CREATE``, share 0, ``FILE_ATTRIBUTE_NORMAL``; an existing name is never
    opened (``NAME_TAKEN``)."""
    _require(handle.role == "H_TMP", f"create_into for {handle.role}")
    _single_component(name)
    return _nt_open_into(handle, parent, name, PROFILES["H_TMP"])


def open_error(handle: Handle) -> OSError:
    """The ``OSError`` for a handle that was not opened (its captured status)."""
    _require(handle.status is not None, f"no captured status for {handle.path}")
    return _status_error(handle.status, handle.path)  # type: ignore[arg-type]


# --- I/O and queries on an open handle --------------------------------------------------


_MAX_OFFSET = 2**63 - 1  # LARGE_INTEGER


def _require_range(what: str, offset: object, size: object) -> None:
    """``offset`` and ``size`` are ints (not bools), ``0 <= offset``, ``0 < size <= 2**32-1``
    and the whole range fits in a signed 64-bit offset: checked before any ctypes object
    exists, so a bad argument never reaches the API (nor wraps in ``c_int64``)."""
    _require(
        type(offset) is int
        and type(size) is int
        and 0 <= offset <= _MAX_OFFSET
        and 0 < size <= 0xFFFF_FFFF
        and offset + size - 1 <= _MAX_OFFSET,
        f"{what}({offset!r}, {size!r})",
    )


def read_at(handle: Handle, offset: int, size: int) -> bytes:
    """Up to ``size`` bytes at ``offset``; ``b""`` at the end of the file."""
    _require_range("read_at", offset, size)
    api = _prims()
    buffer = ctypes.create_string_buffer(size)
    iosb = api.base.IoStatusBlock()
    position = ctypes.c_int64(offset)
    call = _Call((buffer, iosb, position))
    status = _invoke(
        call,
        api.ntdll.NtReadFile,
        handle.raw,
        None,
        None,
        None,
        ctypes.byref(iosb),
        buffer,
        size,
        ctypes.byref(position),
        None,
    )
    if status == STATUS_END_OF_FILE:
        return b""
    _check(status, handle.path)
    if iosb.Information > size:
        raise MalformedInfoError(f"{handle.path}: read {iosb.Information} of {size} bytes")
    return buffer.raw[: iosb.Information]


def write_at(handle: Handle, offset: int, data: bytes) -> None:
    """Write all of ``data`` at ``offset``; a short write raises ``OSError``."""
    _require(isinstance(data, bytes | bytearray | memoryview), f"write_at of {type(data)}")
    _require_range("write_at", offset, len(data))
    api = _prims()
    buffer = ctypes.create_string_buffer(bytes(data), len(data))
    iosb = api.base.IoStatusBlock()
    position = ctypes.c_int64(offset)
    call = _Call((buffer, iosb, position))
    status = _invoke(
        call,
        api.ntdll.NtWriteFile,
        handle.raw,
        None,
        None,
        None,
        ctypes.byref(iosb),
        buffer,
        len(data),
        ctypes.byref(position),
        None,
    )
    _check(status, handle.path)
    if iosb.Information != len(data):
        raise OSError(f"{handle.path}: short write, {iosb.Information} of {len(data)} bytes")


def flush(handle: Handle) -> None:
    """``NtFlushBuffersFile``: the data of the file is on the disk (not its directory)."""
    api = _prims()
    iosb = api.base.IoStatusBlock()
    call = _Call((iosb,))
    _check(_invoke(call, api.ntdll.NtFlushBuffersFile, handle.raw, ctypes.byref(iosb)), handle.path)


def _query_info(
    handle: Handle, struct: type[ctypes.Structure], info_class: int, *, volume: bool = False
) -> ctypes.Structure:
    """A fixed-size information class (``NtQueryVolumeInformationFile`` if ``volume``).

    Only a reply of exactly ``sizeof(struct)`` bytes (``IOSB.Information``) is taken: a
    shorter one (0 included: the IOSB starts zeroed) is ``MalformedInfoError``, never a
    zero-filled structure (an ``EaSize`` of 0 would claim "no EA")."""
    api = _prims()
    result = struct()
    iosb = api.base.IoStatusBlock()
    call = _Call((result, iosb))
    function = (
        api.ntdll.NtQueryVolumeInformationFile if volume else api.ntdll.NtQueryInformationFile
    )
    status = _invoke(
        call,
        function,
        handle.raw,
        ctypes.byref(iosb),
        ctypes.byref(result),
        ctypes.sizeof(result),
        info_class,
    )
    _check(status, handle.path)
    if iosb.Information != ctypes.sizeof(result):
        raise MalformedInfoError(
            f"{handle.path}: {iosb.Information} bytes of class {info_class}, "
            f"{ctypes.sizeof(result)} expected"
        )
    return result


def _query_variable(handle: Handle, info_class: int, start: int) -> bytes:
    """A variable-length information class: the buffer grows on BUFFER_OVERFLOW."""
    api = _prims()
    size = start
    while True:
        buffer = (ctypes.c_uint64 * (size // 8))()  # 8-aligned
        iosb = api.base.IoStatusBlock()
        call = _Call((buffer, iosb))
        status = _invoke(
            call,
            api.ntdll.NtQueryInformationFile,
            handle.raw,
            ctypes.byref(iosb),
            buffer,
            size,
            info_class,
        )
        if status in (STATUS_BUFFER_OVERFLOW, STATUS_BUFFER_TOO_SMALL) and size < _MAX_INFO_BUFFER:
            size *= 4
            continue
        _check(status, handle.path)
        if iosb.Information > size:
            raise MalformedInfoError(f"{handle.path}: {iosb.Information} bytes of {size}")
        return bytes(buffer)[: iosb.Information]


@dataclass(frozen=True, slots=True)
class Space:
    """Space of the volume a handle is on, in bytes (``FileFsFullSizeInformation``)."""

    available: int  # to the caller (quotas included)
    total: int


def query_space(handle: Handle) -> Space:
    api = _prims()
    result = _query_info(handle, api.FullSizeInfo, _FILE_FULL_SIZE_INFORMATION, volume=True)
    unit = result.SectorsPerAllocationUnit * result.BytesPerSector
    return Space(result.CallerAvailableAllocationUnits * unit, result.TotalAllocationUnits * unit)


@dataclass(frozen=True, slots=True)
class Times:
    """``FILE_BASIC_INFO`` of a handle: FILETIMEs (100 ns since 1601) and attributes."""

    creation: int
    last_access: int
    last_write: int
    change: int
    attributes: int

    @property
    def mtime_ns(self) -> int:
        return (self.last_write - winfileops._EPOCH_AS_FILETIME) * 100


def query_times(handle: Handle) -> Times:
    """Times (``ChangeTime`` included) and attributes, read from the handle
    (``FileBasicInformation``, a reply of exactly its size)."""
    basic = _query_info(handle, _prims().base.BasicInfo, _FILE_BASIC_INFORMATION)
    return Times(
        basic.CreationTime,  # type: ignore[attr-defined]
        basic.LastAccessTime,  # type: ignore[attr-defined]
        basic.LastWriteTime,  # type: ignore[attr-defined]
        basic.ChangeTime,  # type: ignore[attr-defined]
        basic.FileAttributes,  # type: ignore[attr-defined]
    )


def set_mtime(handle: Handle, mtime_ns: int) -> None:
    """Set only ``LastWriteTime`` (to ``mtime_ns`` in 100 ns units, truncated); the other
    times and the attributes are 0 in ``FILE_BASIC_INFORMATION``: left as they are."""
    filetime = mtime_ns // 100 + winfileops._EPOCH_AS_FILETIME
    _require(filetime > 0, f"mtime {mtime_ns} before 1601")
    api = _prims()
    info = api.base.BasicInfo(0, 0, filetime, 0, 0)
    iosb = api.base.IoStatusBlock()
    call = _Call((info, iosb))
    status = _invoke(
        call,
        api.ntdll.NtSetInformationFile,
        handle.raw,
        ctypes.byref(iosb),
        ctypes.byref(info),
        ctypes.sizeof(info),
        _FILE_BASIC_INFORMATION,
    )
    _check(status, handle.path)


def query_ea_size(handle: Handle) -> int:
    """``FileEaInformation.EaSize``: 0 if the file has no extended attributes."""
    return _query_info(handle, _prims().EaInfo, _FILE_EA_INFORMATION).EaSize  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class Stream:
    name: str  # e.g. "::$DATA", ":Zone.Identifier:$DATA"
    size: int


def query_streams(handle: Handle) -> tuple[Stream, ...]:
    """The data streams of the file (``FileStreamInformation``), every entry checked."""
    api = _prims()
    data = _query_variable(handle, _FILE_STREAM_INFORMATION, 4096)
    head = api.StreamInfo.StreamName.offset
    if not data:  # a file has at least its unnamed stream: an empty reply proves nothing
        raise MalformedInfoError(f"{handle.path}: empty stream information")
    streams: list[Stream] = []
    offset = 0
    while data:
        if offset % 8 or offset + head > len(data) or len(streams) > len(data) // head:
            raise MalformedInfoError(f"{handle.path}: stream entry at {offset}")
        entry = api.StreamInfo.from_buffer_copy(data, offset)
        length = entry.StreamNameLength
        if length == 0 or length % 2 or offset + head + length > len(data):
            raise MalformedInfoError(f"{handle.path}: stream name length {length}")
        name = data[offset + head : offset + head + length].decode("utf-16-le", "surrogatepass")
        streams.append(Stream(name, entry.StreamSize))
        following = entry.NextEntryOffset
        if following == 0:
            break
        if following < head + length or following % 8 or offset + following >= len(data):
            raise MalformedInfoError(f"{handle.path}: next stream entry {following}")
        offset += following
    return tuple(streams)


def query_name(handle: Handle) -> str:
    """The path of the file inside its volume (``FileNameInformation``), e.g.
    ``\\dir\\file.txt``, as the filesystem has it now (after a rename: the new one)."""
    api = _prims()
    data = _query_variable(handle, _FILE_NAME_INFORMATION, 1024)
    head = api.NameInfo.FileName.offset
    if len(data) < head:
        raise MalformedInfoError(f"{handle.path}: name information of {len(data)} bytes")
    length = int.from_bytes(data[:4], "little")
    if length % 2 or head + length > len(data):
        raise MalformedInfoError(f"{handle.path}: name length {length}")
    return data[head : head + length].decode("utf-16-le", "surrogatepass")


def last_component(nt_path: str) -> str:
    return nt_path.rpartition("\\")[2]


# --- lookup_entry (FIX-03 §3.4) ------------------------------------------------------


class LookupKind(StrEnum):
    FOUND = "found"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Lookup:
    kind: LookupKind
    file_id: int | None = None  # 128 bits, only with FOUND
    reason: str = ""


@dataclass(frozen=True, slots=True)
class DirEntry:
    name: bytes  # UTF-16-LE, as the filesystem returned it
    attributes: int
    reparse_tag: int
    file_id: int


@dataclass(eq=False)
class Listing:
    """One ``NtQueryDirectoryFile``: its status and the entries, if they could be read."""

    status: int | None
    entries: tuple[DirEntry, ...] | None
    reason: str
    information: int | None = None


_EXPECTED_LAYOUT = {
    "NextEntryOffset": 0,
    "FileAttributes": 56,
    "FileNameLength": 60,
    "ReparsePointTag": 68,
    "FileId": 72,
    "FileName": 88,
}
_LAYOUTS: dict[type, bool] = {}  # struct -> whether it matched, checked once each


def _check_layout(struct: type[ctypes.Structure]) -> bool:
    """Whether ``struct`` has the SDK layout of ``FILE_ID_EXTD_DIR_INFORMATION``."""
    if struct not in _LAYOUTS:
        try:
            ok = (
                all(getattr(struct, f).offset == o for f, o in _EXPECTED_LAYOUT.items())
                and struct.FileId.size == 16  # type: ignore[attr-defined]
                and ctypes.alignment(struct) == 8
            )
        except AttributeError:
            ok = False
        _LAYOUTS[struct] = ok
    return _LAYOUTS[struct]


def lookup_name_error(name: str) -> str | None:
    """Why ``name`` cannot be looked up (step 1), or None."""
    if not isinstance(name, str) or not name:
        return "empty name"
    try:
        units = len(name.encode("utf-16-le", "surrogatepass")) // 2
    except UnicodeEncodeError:
        return "not encodable"
    if units > _MAX_COMPONENT:
        return "name too long"
    if any(c in _LOOKUP_REJECTED for c in name):
        return "wildcard or separator in name"
    return None


def query_directory(handle: Handle, name: str) -> Listing:
    """One ``NtQueryDirectoryFile(FileIdExtdDirectoryInformation, ReturnSingleEntry=FALSE,
    FileName=name, RestartScan=TRUE)`` on the open directory ``handle``, every entry
    checked (steps 3-6). ``entries`` is None unless the status is 0 and all is sound."""
    api = _prims()
    struct = api.DirInfo
    if not _check_layout(struct):
        return Listing(None, None, "layout_mismatch")
    head = struct.FileName.offset
    buffer = (ctypes.c_uint64 * (_LOOKUP_BUFFER // 8))()
    text = ctypes.create_unicode_buffer(name)
    length = len(name.encode("utf-16-le", "surrogatepass"))
    pattern = api.base.UnicodeString(length, length + 2, ctypes.addressof(text))
    iosb = api.base.IoStatusBlock(0, 0xFFFF_FFFF)
    call = _Call((buffer, text, pattern, iosb))
    status = _invoke(
        call,
        api.ntdll.NtQueryDirectoryFile,
        handle.raw,
        None,
        None,
        None,
        ctypes.byref(iosb),
        buffer,
        _LOOKUP_BUFFER,
        _FILE_ID_EXTD_DIRECTORY_INFORMATION,
        False,
        ctypes.byref(pattern),
        True,
    )
    information = iosb.Information
    if status in (STATUS_NO_SUCH_FILE, STATUS_NO_MORE_FILES):
        return Listing(status, (), "absent", information)
    if status != STATUS_SUCCESS:
        return Listing(status, None, f"status {status:#010x}", information)
    if not head + 2 <= information <= _LOOKUP_BUFFER:
        return Listing(status, None, f"information {information}", information)
    raw = bytes(buffer)
    entries: list[DirEntry] = []
    offset = 0
    for _ in range(_LOOKUP_BUFFER // head):
        if offset % 8 or offset + head > information:
            return Listing(status, None, f"entry at {offset}", information)
        entry = struct.from_buffer_copy(raw, offset)
        name_length = entry.FileNameLength
        if name_length == 0 or name_length % 2 or offset + head + name_length > information:
            return Listing(status, None, f"name length {name_length} at {offset}", information)
        entries.append(
            DirEntry(
                raw[offset + head : offset + head + name_length],
                entry.FileAttributes,
                entry.ReparsePointTag,
                int.from_bytes(bytes(entry.FileId), "little"),
            )
        )
        following = entry.NextEntryOffset
        if following == 0:
            return Listing(status, tuple(entries), "", information)
        if following < head + name_length or following % 8 or offset + following >= information:
            return Listing(status, None, f"next entry {following} at {offset}", information)
        offset += following
    return Listing(status, None, "too many entries", information)


def judge(listing: Listing, name: str) -> Lookup:
    """The lookup result of one listing for ``name`` (steps 4, 7, 8)."""
    if listing.entries is None:
        return Lookup(LookupKind.UNKNOWN, reason=listing.reason)
    if listing.status in (STATUS_NO_SUCH_FILE, STATUS_NO_MORE_FILES):
        return Lookup(LookupKind.ABSENT, reason="absent")
    wanted = name.encode("utf-16-le", "surrogatepass")
    if len(listing.entries) != 1:
        return Lookup(LookupKind.UNKNOWN, reason=f"{len(listing.entries)} entries")
    (entry,) = listing.entries
    if entry.name != wanted:  # no case folding, normalization or 8.3 alias
        return Lookup(LookupKind.UNKNOWN, reason="the entry has another name")
    if entry.attributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT):
        return Lookup(LookupKind.UNKNOWN, reason=f"attributes {entry.attributes:#x}")
    if entry.reparse_tag != 0:
        return Lookup(LookupKind.UNKNOWN, reason=f"reparse tag {entry.reparse_tag:#x}")
    return Lookup(LookupKind.FOUND, entry.file_id)


def lookup_entry(handle: Handle, directory: Handle, name: str) -> Lookup:
    """Whether the directory ``directory`` has an entry named exactly ``name`` and its id.

    ``handle`` is a ``Handle.new(..., "H_LOOKUP")`` the caller registered; it is opened
    here on ``directory`` itself, checked to be the same directory, queried once and
    closed (a failed close raises; an ``UNKNOWN`` one is left as it is). FOUND only for
    exactly one entry, byte for byte ``name``, no directory and no reparse point; ABSENT
    only for the "no such file" statuses; anything else is UNKNOWN. An interrupt is
    raised (after closing).
    """
    reason = lookup_name_error(name)
    if reason is not None:
        handle.close()  # NEW -> NOT_OPENED
        return Lookup(LookupKind.UNKNOWN, reason=reason)
    try:
        result = _lookup_open(handle, directory, name)
    except BaseException as exc:
        release_after(exc, handle.close)
        raise
    if handle.state is HandleState.UNKNOWN:  # never closed; the caller still has it
        return Lookup(LookupKind.UNKNOWN, reason=f"H_LOOKUP {handle.outcome}")
    handle.close()
    return result


def _lookup_open(handle: Handle, directory: Handle, name: str) -> Lookup:
    outcome = open_into(handle, directory, "")
    if outcome is not OpenOutcome.OPENED_OWNED or handle.unreliable:
        return Lookup(LookupKind.UNKNOWN, reason=f"H_LOOKUP {outcome}")
    try:
        mine, theirs = winfileops.query(handle), winfileops.query(directory)
    except OSError as exc:
        return Lookup(LookupKind.UNKNOWN, reason=f"query: {exc}")
    if (mine.file_id, mine.volume_serial) != (theirs.file_id, theirs.volume_serial):
        return Lookup(LookupKind.UNKNOWN, reason="H_LOOKUP is another directory")
    return judge(query_directory(handle, name), name)


# --- disposition (FIX-03 §3.6, CL-1) ---------------------------------------------------


class Disposed(StrEnum):
    """D of §3.6."""

    NONE = "none"  # no disposition was called
    OK = "ok"  # CL-1a or CL-1b returned 0
    REFUSED = "refused"  # an error status was captured, and no fallback succeeded
    UNSURE = "unsure"  # a status was not captured or is not standard


@dataclass(eq=False)
class Disposal:
    """The disposition calls of one temporary file: each kind at most once."""

    posix_spent: bool = False
    classic_spent: bool = False
    posix: _Call | None = None
    classic: _Call | None = None

    @property
    def result(self) -> Disposed:
        if self.classic_spent:
            last = self.classic
        elif self.posix_spent:
            last = self.posix
        else:
            return Disposed.NONE
        if last is None or last.status is None:
            return Disposed.UNSURE
        kind = status_class(last.status)
        if kind == "ok":
            return Disposed.OK
        return Disposed.REFUSED if kind == "error" else Disposed.UNSURE

    @property
    def statuses(self) -> tuple[int | None, int | None]:
        def unsigned(call: _Call | None) -> int | None:
            return None if call is None or call.status is None else call.status & 0xFFFF_FFFF

        return unsigned(self.posix), unsigned(self.classic)


def _set_disposition(
    handle: Handle, info: ctypes.Structure, info_class: int
) -> tuple[_Call, tuple[object, ...]]:
    iosb = _prims().base.IoStatusBlock()
    args = (handle.raw, ctypes.byref(iosb), ctypes.byref(info), ctypes.sizeof(info), info_class)
    return _Call((info, iosb)), args


def dispose(handle: Handle, disposal: Disposal, *, posix: bool = True) -> Disposed:
    """Mark the temporary file of ``handle`` for deletion: CL-1a, then CL-1b only after a
    captured ``FALLBACK_STATUSES`` refusal. Each flag is set before its call; nothing is
    ever repeated (a second ``dispose`` on the same ``disposal`` is refused).

    Only a handle this program created (``CREATED_OWNED``, ``H_TMP``, ``OPEN``).
    ``posix=False`` skips CL-1a (the "classic only" test switch of W-12b).
    """
    _require(
        handle.role == "H_TMP"
        and handle.outcome is OpenOutcome.CREATED_OWNED
        and handle.state is HandleState.OPEN,
        f"dispose of {handle.role} {handle.outcome} {handle.state}",
    )
    _require(not (disposal.posix_spent or disposal.classic_spent), "a disposition was spent")
    api = _prims()
    if posix:
        info = api.DispositionInfoEx(_FILE_DISPOSITION_DELETE | _FILE_DISPOSITION_POSIX_SEMANTICS)
        call, args = _set_disposition(handle, info, _FILE_DISPOSITION_INFORMATION_EX)
        disposal.posix = call
        disposal.posix_spent = True
        status = _invoke(call, api.ntdll.NtSetInformationFile, *args)
        if status not in FALLBACK_STATUSES:
            return disposal.result
    classic = api.DispositionInfo(True)
    call, args = _set_disposition(handle, classic, _FILE_DISPOSITION_INFORMATION)
    disposal.classic = call
    disposal.classic_spent = True
    _invoke(call, api.ntdll.NtSetInformationFile, *args)
    return disposal.result


# --- rename_once (FIX-03 §3.5, R-3) -----------------------------------------------------


class Renamed(StrEnum):
    OK = "ok"
    EXISTS = "exists"
    ERROR = "error"
    NOT_ESTABLISHED = "not established"  # STATUS_PENDING or another non-standard status
    LOST = "lost"  # the status was not captured (or the call was never entered)


@dataclass(eq=False)
class RenameAttempt:
    """The one rename of a temporary file: ``spent`` is set right before the call."""

    spent: bool = False
    call: _Call | None = field(default=None)

    @property
    def result(self) -> Renamed | None:
        if not self.spent:
            return None
        if self.call is None or self.call.status is None:
            return Renamed.LOST
        status = self.call.status & 0xFFFF_FFFF
        kind = status_class(status)
        if kind == "ok":
            return Renamed.OK
        if kind == "nonstandard":
            return Renamed.NOT_ESTABLISHED
        return Renamed.EXISTS if status == STATUS_OBJECT_NAME_COLLISION else Renamed.ERROR

    @property
    def status(self) -> int | None:
        if self.call is None or self.call.status is None:
            return None
        return self.call.status & 0xFFFF_FFFF


def rename_once(handle: Handle, directory: Handle, name: str, attempt: RenameAttempt) -> Renamed:
    """Rename the file of ``handle`` (``H_TMP``) to ``name`` in ``directory``, once.

    ``ReplaceIfExists = FALSE``; every status is classified (``Renamed``), none is taken
    as success but 0. The buffer and IOSB belong to ``attempt`` and are pinned if the
    status is lost or not standard. A second call on the same ``attempt`` is refused.
    """
    _require(not attempt.spent, "the rename was spent")
    _require(handle.role == "H_TMP" and handle.state is HandleState.OPEN, f"rename of {handle}")
    _single_component(name)
    api = _prims()
    rename = api.base.RenameInfo
    offset = rename.FileName.offset
    name_bytes = name.encode("utf-16-le", "surrogatepass")
    buffer = (
        ctypes.c_uint64 * ((max(offset + len(name_bytes) + 2, ctypes.sizeof(rename)) + 7) // 8)
    )()
    info = rename.from_buffer(buffer)
    info.ReplaceIfExists = 0  # never replace
    info.RootDirectory = directory.raw
    info.FileNameLength = len(name_bytes)
    ctypes.memmove(ctypes.addressof(buffer) + offset, name_bytes, len(name_bytes))
    iosb = api.base.IoStatusBlock(0, 0xFFFF_FFFF)
    raw = handle.raw
    attempt.call = _Call((buffer, iosb))
    attempt.spent = True  # right before the call
    _invoke(
        attempt.call,
        api.ntdll.NtSetInformationFile,
        raw,
        ctypes.byref(iosb),
        buffer,
        offset + len(name_bytes),
        _FILE_RENAME_INFORMATION,
    )
    return attempt.result  # type: ignore[return-value]
