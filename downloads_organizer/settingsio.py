"""The settings folder by handles (R4-C FIX-01): pinned, locked, read and replaced.

``usersettings`` reads and writes ``settings.json`` only through this module. Nothing here
resolves a path a second time: the folder is walked from its proven volume root one
component at a time (``winfileops.open_relative``, which never follows a link or a
junction), each component is checked through its own handle and stays open without
``FILE_SHARE_DELETE`` until the folder is closed, so none of them can be renamed, deleted
or swapped for a junction meanwhile (the pattern of ``journal._DirectoryChain``). Every
file is then opened, created and renamed relative to the handle of the folder.

* ``lock`` takes ``settings.json.lock`` with no sharing at all: one save at a time, also
  across processes. The lock file is never written and never deleted.
* ``rename`` never replaces (``winfileops.rename_into``); ``delete`` marks a file of an
  open handle for deletion (``FileDispositionInformation``), nothing by name.
"""

from __future__ import annotations

import ctypes
import time
from pathlib import Path

from downloads_organizer import winfileops, winprims

_DIR_ACCESS = winfileops.FILE_LIST_DIRECTORY | winfileops.FILE_READ_ATTRIBUTES
# Others may read and add entries, but not rename or delete a held directory.
# FILE_LIST_DIRECTORY is data access, so this share mode is actually enforced.
_HELD_SHARE = winfileops.FILE_SHARE_READ | winfileops.FILE_SHARE_WRITE
# Data access with no sharing: a second lock (any process) fails with a sharing violation.
_LOCK_ACCESS = winprims.FILE_READ_DATA | winprims.FILE_WRITE_DATA | winfileops.FILE_READ_ATTRIBUTES
_READ_ACCESS = winprims.FILE_READ_DATA | winfileops.FILE_READ_ATTRIBUTES
# A reader lets a save rename the file meanwhile; it never lets anyone write into it.
_READ_SHARE = winfileops.FILE_SHARE_READ | winfileops.FILE_SHARE_DELETE
_TEMP_ACCESS = _READ_ACCESS | winprims.FILE_WRITE_DATA | winfileops.DELETE

LOCK_WAIT = 3.0  # seconds a save waits for another one to finish
_LOCK_STEP = 0.05
_CHUNK = 64 * 1024
_FILE_DISPOSITION_INFORMATION = 13
_STATUS_FILE_IS_A_DIRECTORY = 0xC000_00BA  # reported as ERROR_ACCESS_DENIED


class Refused(Exception):
    """The folder or a file in it cannot be used safely; nothing was written."""

    LINK = "link"  # a link, a junction, a reparse point or not the expected kind
    UNVERIFIED = "unverified"  # the volume, the mapping or the location is not proven
    BUSY = "busy"  # another save holds the lock

    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


class Folder:
    """The handles of a folder and of each ancestor up to its volume root (``open``)."""

    def __init__(self) -> None:
        self._handles: list[winfileops.Handle] = []
        self.root: winfileops.VolumeRoot | None = None

    @classmethod
    def open(cls, folder: Path, *, create: bool) -> Folder:
        """Walk to ``folder`` by handles and hold it; ``create`` may make its last part only.

        ``FileNotFoundError`` if a component is missing (with ``create``: one but the
        last); ``Refused`` for a link, junction, non-directory or an unproven volume.
        """
        if not winfileops.AVAILABLE:
            raise Refused(Refused.UNVERIFIED, "settings need Windows file handles")
        self = cls()
        try:
            self._walk(folder, create=create)
        except BaseException as exc:
            winfileops.release_after(exc, self.close)
            raise
        return self

    def _walk(self, folder: Path, *, create: bool) -> None:
        try:
            root = winfileops.volume_root(folder)
            self._handles.append(winfileops.open_anchor(root.anchor))
            winfileops.prove_volume_root(self.last, root)
        except winfileops.UnverifiedRootError as exc:
            raise Refused(Refused.UNVERIFIED, str(exc)) from exc
        self.root = root
        parts = folder.parts[1:]
        for position, name in enumerate(parts, start=1):
            self._handles.append(self._component(name, create=create and position == len(parts)))
            info = winfileops.query(self.last)
            if info.reparse_point or not info.directory:
                raise Refused(Refused.LINK, f"{self.last.path} is a link, a junction or no folder")
        self.check()

    def _component(self, name: str, *, create: bool) -> winfileops.Handle:
        def existing() -> winfileops.Handle:
            return winfileops.open_relative(
                self.last, name, directory=True, access=_DIR_ACCESS, share=_HELD_SHARE
            )[0]

        try:
            return existing()
        except FileNotFoundError:
            if not create:
                raise
        except NotADirectoryError as exc:
            raise Refused(Refused.LINK, f"{self.last.path / name} is not a folder") from exc
        try:
            return winfileops.open_relative(
                self.last, name, directory=True, access=_DIR_ACCESS, share=_HELD_SHARE, create=True
            )[0]
        except FileExistsError:
            return existing()  # made concurrently: that object is checked instead

    @property
    def last(self) -> winfileops.Handle:
        return self._handles[-1]

    @property
    def path(self) -> Path:
        return self.last.path

    def check(self) -> None:
        """``Refused`` unless the folder still is on its proven volume under its drive letter."""
        assert self.root is not None
        try:
            final = winfileops.final_nt_path(self.last)
            winfileops.check_mapping(self.root)
        except (OSError, winfileops.UnverifiedRootError) as exc:
            raise Refused(Refused.UNVERIFIED, str(exc)) from exc
        if not (final.upper() + "\\").startswith(self.root.device.upper() + "\\"):
            raise Refused(
                Refused.UNVERIFIED, f"{self.path} is {final!r}, not on {self.root.device}"
            )

    def close(self) -> None:
        """Release every handle, innermost first; the first failure is raised after all."""
        handles, self._handles = self._handles, []
        failure: BaseException | None = None
        for handle in reversed(handles):
            try:
                handle.close()
            except BaseException as exc:  # close the rest first
                failure = failure or exc
        if failure is not None:
            raise failure

    def __enter__(self) -> Folder:
        return self

    def __exit__(self, kind: object, error: BaseException | None, tb: object) -> None:
        if error is None:
            self.close()
        else:
            winfileops.release_after(error, self.close)

    # --- files in the folder ----------------------------------------------------------

    def lock(self, name: str, wait: float = LOCK_WAIT) -> winfileops.Handle:
        """Take the lock file ``name`` (made if missing); ``Refused(BUSY)`` after ``wait``."""
        deadline = time.monotonic() + wait
        while True:
            try:
                handle = self._open_lock(name)
                break
            except PermissionError as exc:
                if getattr(exc, "winerror", None) != winfileops.ERROR_SHARING_VIOLATION:
                    raise
                if time.monotonic() >= deadline:
                    raise Refused(Refused.BUSY, f"{self.path / name} is held") from exc
                time.sleep(_LOCK_STEP)
        try:
            self._plain(handle)
        except BaseException as exc:
            winfileops.release_after(exc, handle.close)
            raise
        return handle

    def _open_lock(self, name: str) -> winfileops.Handle:
        try:
            return winfileops.create_file(self.last, name, access=_LOCK_ACCESS, share=0)
        except FileExistsError:
            pass
        try:
            return winfileops.open_relative(
                self.last, name, directory=False, access=_LOCK_ACCESS, share=0
            )[0]
        except PermissionError as exc:
            if getattr(exc, "ntstatus", None) == _STATUS_FILE_IS_A_DIRECTORY:
                raise Refused(Refused.LINK, f"{self.path / name} is a folder") from exc
            raise

    def open_file(self, name: str, *, for_rename: bool) -> winfileops.Handle | None:
        """Open the plain file ``name`` to read it; ``None`` if there is none.

        ``for_rename`` adds ``DELETE`` (to rename or delete it by this handle) and lets
        others only read it meanwhile; else others may also rename it, never write it.
        """
        access = _READ_ACCESS | (winfileops.DELETE if for_rename else 0)
        share = winfileops.FILE_SHARE_READ if for_rename else _READ_SHARE
        try:
            handle, _ = winfileops.open_relative(
                self.last, name, directory=False, access=access, share=share
            )
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            if getattr(exc, "ntstatus", None) == _STATUS_FILE_IS_A_DIRECTORY:
                raise Refused(Refused.LINK, f"{self.path / name} is a folder") from exc
            raise
        try:
            self._plain(handle)
        except BaseException as exc:
            winfileops.release_after(exc, handle.close)
            raise
        return handle

    def create(self, name: str) -> winfileops.Handle:
        """Create the new file ``name`` (never an existing one), not shared, deletable."""
        handle = winfileops.create_file(self.last, name, access=_TEMP_ACCESS, share=0)
        try:
            self._plain(handle)
        except BaseException as exc:
            winfileops.release_after(exc, handle.close)
            raise
        return handle

    def identity(self, name: str) -> int | None:
        """The file id ``name`` in the folder refers to now (``None``: no such file)."""
        try:
            handle, _ = winfileops.open_relative(
                self.last, name, directory=False, access=winfileops.FILE_READ_ATTRIBUTES,
                share=winfileops.SHARE_ALL,
            )  # fmt: skip
        except FileNotFoundError:
            return None
        with handle:
            return winfileops.query(handle).file_id

    def rename(self, handle: winfileops.Handle, name: str) -> None:
        """Give the file of ``handle`` the new name ``name`` in this folder; never replaces."""
        winfileops.rename_into(handle, self.last, name)

    def _plain(self, handle: winfileops.Handle) -> None:
        info = winfileops.query(handle)
        if info.reparse_point or info.directory or info.links != 1 or info.delete_pending:
            raise Refused(Refused.LINK, f"{handle.path} is not a plain file")
        final = winfileops.final_nt_path(handle).upper()
        assert self.root is not None
        if not final.startswith(self.root.device.upper() + "\\"):
            raise Refused(Refused.UNVERIFIED, f"{handle.path} is not on {self.root.device}")


def read_all(handle: winfileops.Handle, limit: int) -> bytes | None:
    """The whole file of ``handle`` from offset 0; ``None`` if it is over ``limit`` bytes."""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = winprims.read_at(handle, size, _CHUNK)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)


def write_all(handle: winfileops.Handle, data: bytes) -> None:
    """Write ``data`` at offset 0, flush it to the disk and read it back to compare."""
    winprims.write_at(handle, 0, data)
    winprims.flush(handle)
    if read_all(handle, len(data)) != data:
        raise OSError(f"{handle.path} does not read back as written")


class _IoStatus(ctypes.Structure):
    _fields_ = (("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t))


_NT_SET_INFORMATION = None


def delete(handle: winfileops.Handle) -> None:
    """Mark the file of ``handle`` (opened with ``DELETE``) for deletion when it is closed."""
    global _NT_SET_INFORMATION
    if _NT_SET_INFORMATION is None:
        function = ctypes.WinDLL("ntdll").NtSetInformationFile
        function.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(_IoStatus), ctypes.c_void_p, ctypes.c_ulong,
            ctypes.c_int,
        )  # fmt: skip
        function.restype = ctypes.c_long
        _NT_SET_INFORMATION = function
    flag = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFORMATION.DeleteFile = TRUE
    iosb = _IoStatus()
    status = _NT_SET_INFORMATION(
        handle.raw, ctypes.byref(iosb), ctypes.byref(flag), 1, _FILE_DISPOSITION_INFORMATION
    )
    if status < 0:
        raise OSError(f"cannot delete {handle.path}: NTSTATUS {status & 0xFFFF_FFFF:#010x}")
    if not winfileops.query(handle).delete_pending:
        raise OSError(f"cannot delete {handle.path}: not marked for deletion")
