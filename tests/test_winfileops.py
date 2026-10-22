"""Real Windows behavior of the handle-based primitives (temporary directories only)."""

from __future__ import annotations

import io
import itertools
import os
import stat
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from downloads_organizer import winfileops as w

pytestmark = pytest.mark.windows_only

HELD_ACCESS = w.FILE_LIST_DIRECTORY | w.FILE_READ_ATTRIBUTES
HELD_SHARE = w.FILE_SHARE_READ | w.FILE_SHARE_WRITE
FILE_ACCESS = w.DELETE | w.FILE_READ_ATTRIBUTES
STATUS_FILE_IS_A_DIRECTORY = 0xC000_00BA


def walk(path: Path, *, access: int = HELD_ACCESS, share: int = HELD_SHARE) -> w.Handle:
    """Open directory ``path`` component by component; only the last handle is kept."""
    handle = w.open_anchor(Path(path.anchor))
    for index, part in enumerate(path.parts[1:], start=2):
        final = index == len(path.parts)
        child, _ = w.open_relative(
            handle,
            part,
            directory=True,
            access=access if final else w.FILE_READ_ATTRIBUTES,
            share=share if final else w.SHARE_ALL,
        )
        handle.close()
        handle = child
    return handle


def open_file(directory: w.Handle, name: str) -> w.Handle:
    handle, created = w.open_relative(
        directory, name, directory=False, access=FILE_ACCESS, share=w.FILE_SHARE_READ
    )
    assert not created
    return handle


@pytest.fixture
def dirs(tmp_path: Path) -> Iterator[tuple[Path, Path, w.Handle, w.Handle]]:
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    sh, dh = walk(src), walk(dst)
    yield src, dst, sh, dh
    sh.close()
    dh.close()


def write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_query_matches_stat(tmp_path: Path, dirs: tuple[Path, Path, w.Handle, w.Handle]) -> None:
    src, _, sh, _ = dirs
    st = write(src / "a.pdf", b"hello").stat(follow_symlinks=False)
    with open_file(sh, "a.pdf") as fh:
        info = w.query(fh)
    assert (info.file_id, info.volume_serial) == (st.st_ino, st.st_dev)
    assert (info.size, info.mtime_ns, info.attributes) == (
        st.st_size,
        st.st_mtime_ns,
        st.st_file_attributes,
    )
    assert info.links == 1
    assert not (info.directory or info.reparse_point or info.delete_pending)
    dir_info = w.query(sh)
    assert dir_info.directory
    assert dir_info.file_id == src.stat().st_ino


def test_query_volume_reports_the_local_ntfs_disk(tmp_path: Path) -> None:
    with w.open_anchor(Path(tmp_path.anchor)) as root, walk(tmp_path) as held:
        for handle in (root, held):
            volume = w.query_volume(handle)
            assert (volume.filesystem, volume.device_type) == ("NTFS", w.FILE_DEVICE_DISK)
            assert not (volume.remote or volume.removable)


def test_handle_close_is_idempotent(dirs: tuple[Path, Path, w.Handle, w.Handle]) -> None:
    src, _, sh, _ = dirs
    write(src / "a.pdf", b"x")
    fh = open_file(sh, "a.pdf")
    fh.close()
    assert fh.state is w.HandleState.CLOSED and fh.close_error is None
    fh.close()
    with pytest.raises(ValueError, match="closed"):
        _ = fh.raw


@pytest.mark.parametrize("name", ["", ".", "..", "a\\b", "a/b", "c:x", "a\0b"])
def test_open_relative_rejects_non_components(
    dirs: tuple[Path, Path, w.Handle, w.Handle], name: str
) -> None:
    with pytest.raises(ValueError, match="single path component"):
        w.open_relative(dirs[2], name, directory=False, access=FILE_ACCESS, share=0)


def test_open_anchor_rejects_non_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="volume root"):
        w.open_anchor(tmp_path)


def test_open_errors_map_to_python_exceptions(
    dirs: tuple[Path, Path, w.Handle, w.Handle],
) -> None:
    src, _, sh, _ = dirs
    write(src / "f.pdf", b"x")
    (src / "sub").mkdir()
    access, share = w.FILE_READ_ATTRIBUTES, w.SHARE_ALL
    with pytest.raises(FileNotFoundError):
        w.open_relative(sh, "missing.pdf", directory=False, access=access, share=share)
    with pytest.raises(NotADirectoryError):
        w.open_relative(sh, "f.pdf", directory=True, access=access, share=share)
    with pytest.raises(PermissionError) as info:
        w.open_relative(sh, "sub", directory=False, access=access, share=share)
    assert info.value.ntstatus == STATUS_FILE_IS_A_DIRECTORY  # type: ignore[attr-defined]


def test_lookup_is_case_insensitive(dirs: tuple[Path, Path, w.Handle, w.Handle]) -> None:
    src, _, sh, _ = dirs
    st = write(src / "Report.PDF", b"x").stat()
    with open_file(sh, "report.pdf") as fh:
        assert w.query(fh).file_id == st.st_ino


def test_create_directory_never_reuses_an_existing_name(
    tmp_path: Path, dirs: tuple[Path, Path, w.Handle, w.Handle]
) -> None:
    _, dst, _, dh = dirs
    handle, created = w.open_relative(
        dh, "PDF", directory=True, access=HELD_ACCESS, share=HELD_SHARE, create=True
    )
    with handle:
        assert created
        assert w.query(handle).directory
    assert (dst / "PDF").is_dir()
    write(dst / "PDF" / "keep.txt", b"keep")
    write(dst / "file", b"plain file")
    for name in ("PDF", "pdf", "file"):
        with pytest.raises(FileExistsError) as info:
            w.open_relative(
                dh, name, directory=True, access=HELD_ACCESS, share=HELD_SHARE, create=True
            )
        assert info.value.ntstatus == w.STATUS_OBJECT_NAME_COLLISION  # type: ignore[attr-defined]
    assert (dst / "PDF" / "keep.txt").read_bytes() == b"keep"
    assert (dst / "file").read_bytes() == b"plain file"


def test_junction_is_opened_as_itself_and_not_followed(
    tmp_path: Path,
    dirs: tuple[Path, Path, w.Handle, w.Handle],
    make_junction: Callable[[Path, Path], Path],
) -> None:
    _, dst, _, dh = dirs
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    make_junction(dst / "PDF", elsewhere)
    handle, _ = w.open_relative(
        dh, "PDF", directory=True, access=w.FILE_READ_ATTRIBUTES, share=w.SHARE_ALL
    )
    with handle:
        info = w.query(handle)
        assert info.reparse_point
        assert info.reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
        with pytest.raises(OSError, match=r"."):
            w.open_relative(
                handle, "new", directory=True, access=w.FILE_READ_ATTRIBUTES,
                share=w.SHARE_ALL, create=True,
            )  # fmt: skip
    assert list(elsewhere.iterdir()) == []
    with pytest.raises(FileExistsError):
        w.open_relative(
            dh, "PDF", directory=True, access=HELD_ACCESS, share=HELD_SHARE, create=True
        )


def test_rename_into_moves_the_opened_file(dirs: tuple[Path, Path, w.Handle, w.Handle]) -> None:
    src, dst, sh, dh = dirs
    before = write(src / "a.pdf", b"payload").stat()
    with open_file(sh, "a.pdf") as fh:
        w.rename_into(fh, dh, "a.pdf")
        assert w.query(fh).file_id == before.st_ino
    assert not (src / "a.pdf").exists()
    after = (dst / "a.pdf").stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert (dst / "a.pdf").read_bytes() == b"payload"


@pytest.mark.parametrize("name", ["emoji 😀 файл.pdf", "x" * 150 + ".pdf"])
def test_rename_into_handles_any_valid_name(
    dirs: tuple[Path, Path, w.Handle, w.Handle], name: str
) -> None:
    src, dst, sh, dh = dirs
    write(src / name, b"x")
    with open_file(sh, name) as fh:
        w.rename_into(fh, dh, name)
    assert [p.name for p in dst.iterdir()] == [name]
    assert (dst / name).read_bytes() == b"x"


@pytest.mark.parametrize("existing", ["a.pdf", "A.PDF", "dir", "junction"])
def test_rename_into_never_replaces(
    tmp_path: Path,
    dirs: tuple[Path, Path, w.Handle, w.Handle],
    make_junction: Callable[[Path, Path], Path],
    snapshot: Callable[[Path], dict[str, tuple[object, ...]]],
    existing: str,
) -> None:
    src, dst, sh, dh = dirs
    write(src / "a.pdf", b"mine")
    name = "a.pdf"
    if existing == "dir":
        (dst / "a.pdf").mkdir()
        write(dst / "a.pdf" / "inside.txt", b"inside")
    elif existing == "junction":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        make_junction(dst / "a.pdf", elsewhere)
    else:
        write(dst / existing, b"theirs")
        name = "a.pdf" if existing == "A.PDF" else existing
    before = snapshot(tmp_path)
    with open_file(sh, "a.pdf") as fh, pytest.raises(FileExistsError):
        w.rename_into(fh, dh, name)
    assert snapshot(tmp_path) == before
    assert (src / "a.pdf").read_bytes() == b"mine"


def test_held_directory_cannot_be_renamed_or_deleted_but_accepts_renames(
    tmp_path: Path, dirs: tuple[Path, Path, w.Handle, w.Handle]
) -> None:
    src, dst, sh, _ = dirs
    (dst / "PDF").mkdir()
    held = walk(dst / "PDF")
    try:
        with pytest.raises(PermissionError):
            (dst / "PDF").rename(dst / "Other")
        with pytest.raises(PermissionError):
            (dst / "PDF").rmdir()
        # The rename opens the target directory for adding an entry: still allowed.
        write(src / "a.pdf", b"x")
        with open_file(sh, "a.pdf") as fh:
            w.rename_into(fh, held, "a.pdf")
        assert (dst / "PDF" / "a.pdf").read_bytes() == b"x"
        write(dst / "PDF" / "other.txt", b"others may add files")
    finally:
        held.close()
    (dst / "PDF").rename(dst / "Other")  # released: possible again


def test_open_source_blocks_writers_and_writers_block_it(
    dirs: tuple[Path, Path, w.Handle, w.Handle],
) -> None:
    src, _, sh, _ = dirs
    path = write(src / "a.pdf", b"x")
    with open_file(sh, "a.pdf"):
        with pytest.raises(PermissionError):
            path.open("ab")
        with pytest.raises(PermissionError):
            path.rename(src / "b.pdf")
    write(src / "busy.pdf", b"y")
    with (src / "busy.pdf").open("ab"), pytest.raises(PermissionError) as info:
        open_file(sh, "busy.pdf")
    assert info.value.winerror == w.ERROR_SHARING_VIOLATION


def test_rename_follows_the_handle_not_the_path(
    tmp_path: Path, dirs: tuple[Path, Path, w.Handle, w.Handle]
) -> None:
    """A directory renamed while unheld: the file goes where the handle points."""
    src, dst, sh, _ = dirs
    (dst / "PDF").mkdir()
    lookup = walk(dst / "PDF", access=w.FILE_READ_ATTRIBUTES, share=w.SHARE_ALL)
    with lookup:
        (dst / "PDF").rename(dst / "Moved")
        write(src / "a.pdf", b"x")
        with open_file(sh, "a.pdf") as fh:
            w.rename_into(fh, lookup, "a.pdf")
    assert (dst / "Moved" / "a.pdf").read_bytes() == b"x"
    assert not (dst / "PDF").exists()


def test_stat_identity_of_directory_matches_handle(tmp_path: Path) -> None:
    handle = walk(tmp_path)
    with handle:
        info = w.query(handle)
    st = tmp_path.stat(follow_symlinks=False)
    assert (info.volume_serial, info.file_id) == (st.st_dev, st.st_ino)


WRITE_ACCESS = w.FILE_GENERIC_WRITE | w.FILE_READ_ATTRIBUTES


def test_create_file_makes_a_new_plain_file(dirs: tuple[Path, Path, w.Handle, w.Handle]) -> None:
    _, dst, _, dh = dirs
    with w.create_file(dh, "run.jsonl", access=WRITE_ACCESS, share=w.FILE_SHARE_READ) as fh:
        info = w.query(fh)
        assert not (info.directory or info.reparse_point)
        assert (info.links, info.size) == (1, 0)
        st = (dst / "run.jsonl").stat(follow_symlinks=False)
        assert (info.volume_serial, info.file_id) == (st.st_dev, st.st_ino)
    assert (dst / "run.jsonl").read_bytes() == b""


@pytest.mark.parametrize("existing", ["file", "case", "dir", "junction"])
def test_create_file_never_reuses_an_existing_name(
    tmp_path: Path,
    dirs: tuple[Path, Path, w.Handle, w.Handle],
    make_junction: Callable[[Path, Path], Path],
    snapshot: Callable[[Path], dict[str, tuple[object, ...]]],
    existing: str,
) -> None:
    _, dst, _, dh = dirs
    if existing == "dir":
        (dst / "run.jsonl").mkdir()
    elif existing == "junction":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        make_junction(dst / "run.jsonl", elsewhere)
    else:
        write(dst / ("RUN.JSONL" if existing == "case" else "run.jsonl"), b"theirs")
    before = snapshot(tmp_path)
    with pytest.raises(OSError) as info:
        w.create_file(dh, "run.jsonl", access=WRITE_ACCESS, share=w.FILE_SHARE_READ)
    expected = {w.STATUS_OBJECT_NAME_COLLISION}
    if existing in ("dir", "junction"):  # a directory entry: refused as one
        expected.add(STATUS_FILE_IS_A_DIRECTORY)
    assert info.value.ntstatus in expected  # type: ignore[attr-defined]
    assert snapshot(tmp_path) == before


def test_open_stream_takes_over_the_handle(dirs: tuple[Path, Path, w.Handle, w.Handle]) -> None:
    _, dst, _, dh = dirs
    fh = w.create_file(dh, "out.bin", access=WRITE_ACCESS, share=w.FILE_SHARE_READ)
    stream = w.open_stream(fh)
    assert fh.state is w.HandleState.DETACHED
    with pytest.raises(ValueError, match="detached"):
        _ = fh.raw  # the stream owns the OS handle now
    fh.close()  # a no-op; must not close the stream's handle
    stream.write(b"data")
    stream.flush()
    os.fsync(stream.fileno())
    stream.close()
    assert (dst / "out.bin").read_bytes() == b"data"
    (dst / "out.bin").unlink()  # nothing holds it any more


def test_detach_gives_up_ownership(dirs: tuple[Path, Path, w.Handle, w.Handle]) -> None:
    _, dst, _, dh = dirs
    fh = w.create_file(dh, "out.bin", access=WRITE_ACCESS, share=w.FILE_SHARE_READ)
    raw = fh.detach()
    fh.close()
    with pytest.raises(PermissionError):
        (dst / "out.bin").unlink()  # still open: the raw handle was not closed
    w.Handle(raw, dst / "out.bin").close()
    (dst / "out.bin").unlink()


@pytest.mark.parametrize("stage", ["open_osfhandle", "FileIO", "BufferedWriter"])
def test_open_stream_failure_closes_the_handle_exactly_once(
    dirs: tuple[Path, Path, w.Handle, w.Handle], monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Before ``open_osfhandle`` took it, the handle stays the caller's (``OPEN``); after
    that the descriptor or the raw file owns it and is closed here, once."""
    import msvcrt

    _, dst, _, dh = dirs
    fh = w.create_file(dh, "out.bin", access=WRITE_ACCESS, share=w.FILE_SHARE_READ)
    raw = fh.raw
    kernel32 = w._api().kernel32
    real_close_handle, real_os_close = kernel32.CloseHandle, os.close
    closed: list[int] = []
    fds_closed: list[int] = []

    def close_handle(handle: int) -> int:
        closed.append(handle)
        return real_close_handle(handle)

    def os_close(fd: int) -> None:
        fds_closed.append(fd)
        real_os_close(fd)

    def injected(*args: object, **kwargs: object) -> object:
        raise OSError("injected")

    with monkeypatch.context() as mp:
        mp.setattr(kernel32, "CloseHandle", close_handle)
        mp.setattr(os, "close", os_close)
        target = msvcrt if stage == "open_osfhandle" else w
        mp.setattr(target, stage, injected)
        with pytest.raises(OSError, match="injected"):
            w.open_stream(fh)
        if stage == "open_osfhandle":
            assert fh.state is w.HandleState.OPEN and closed == []  # still the caller's
        else:
            assert fh.state is w.HandleState.DETACHED
        fh.close()  # closes it only if it still is the caller's
    if stage == "open_osfhandle":
        assert (closed, fds_closed) == ([raw], [])  # the Handle closed it
    elif stage == "FileIO":
        assert closed == [] and len(fds_closed) == 1  # the descriptor closed it
    else:
        assert closed == [] and fds_closed == []  # the raw FileIO closed its descriptor
    assert not _is_open(raw)
    (dst / "out.bin").unlink()  # released


# --- checked CloseHandle (R3A-FIX-03 / HANDLE-01) -------------------------------------

HANDLE_FLAG_PROTECT_FROM_CLOSE = 0x2


def _kernel32() -> object:
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    k.SetHandleInformation.restype = wintypes.BOOL
    k.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetHandleInformation.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k


def _is_open(raw: int) -> bool:
    """Whether ``raw`` is a valid handle of this process (asked the OS, not the object)."""
    import ctypes
    from ctypes import wintypes

    flags = wintypes.DWORD()
    return bool(_kernel32().GetHandleInformation(raw, ctypes.byref(flags)))  # type: ignore[attr-defined]


def test_close_true_releases_the_handle(dirs: tuple[Path, Path, w.Handle, w.Handle]) -> None:
    src, _, sh, _ = dirs
    write(src / "a.pdf", b"x")
    fh = open_file(sh, "a.pdf")
    raw = fh.raw
    with pytest.raises(PermissionError):
        (src / "a.pdf").unlink()  # held without FILE_SHARE_DELETE
    fh.close()
    assert fh.state is w.HandleState.CLOSED and fh.close_error is None
    assert not _is_open(raw)
    (src / "a.pdf").unlink()  # really released


def test_close_false_raises_with_getlasterror_and_never_retries(
    dirs: tuple[Path, Path, w.Handle, w.Handle], close_failures
) -> None:
    src, _, sh, _ = dirs
    write(src / "a.pdf", b"x")
    close_failures.error = 6  # ERROR_INVALID_HANDLE
    close_failures.select = lambda handle: handle.path == src / "a.pdf"
    calls: list[int] = []
    fake = close_failures.kernel32.CloseHandle

    def counting(raw: int) -> object:
        calls.append(raw)
        return fake(raw)

    close_failures.kernel32.CloseHandle = counting
    try:
        fh = open_file(sh, "a.pdf")
        raw = fh.raw
        with pytest.raises(w.HandleCloseError) as raised:
            fh.close()
        error = raised.value
        assert error.winerror == 6 and error.handle_value == raw
        assert f"{raw:#x}" in str(error) and error.filename == str(src / "a.pdf")
        assert fh.state is w.HandleState.CLOSE_FAILED and fh.close_error is error
        with pytest.raises(ValueError, match="close failed"):
            _ = fh.raw  # not usable, and not reported as released either
        with pytest.raises(w.HandleCloseError) as again:
            fh.close()
        assert again.value is error
        with pytest.raises(w.HandleCloseError), fh:
            pass  # the context manager reports it as well
        assert calls == [raw]  # CloseHandle was called once, never retried
        assert repr(fh).startswith("<Handle close failed")
    finally:
        close_failures.kernel32.CloseHandle = fake
    assert _is_open(raw)  # the OS handle really is still open
    with pytest.raises(PermissionError):
        (src / "a.pdf").unlink()
    close_failures.really_close()
    (src / "a.pdf").unlink()


def test_close_that_never_returns_counts_as_failed(
    dirs: tuple[Path, Path, w.Handle, w.Handle], monkeypatch: pytest.MonkeyPatch
) -> None:
    src, _, sh, _ = dirs
    write(src / "a.pdf", b"x")
    fh = open_file(sh, "a.pdf")
    raw = fh.raw
    kernel32 = w._api().kernel32
    real = kernel32.CloseHandle

    def interrupted(handle: int) -> int:
        raise KeyboardInterrupt

    with monkeypatch.context() as mp:
        mp.setattr(kernel32, "CloseHandle", interrupted)
        with pytest.raises(KeyboardInterrupt):
            fh.close()
    assert fh.state is w.HandleState.CLOSE_FAILED
    with pytest.raises(w.HandleCloseError, match="did not complete") as raised:
        fh.close()  # unknown outcome: never reported as released, never retried
    assert raised.value.winerror == 0 and raised.value.handle_value == raw
    assert real(raw)  # it was still open
    (src / "a.pdf").unlink()


def test_protected_handle_is_reported_and_stays_open(
    dirs: tuple[Path, Path, w.Handle, w.Handle],
) -> None:
    """A real ``FALSE`` from ``CloseHandle``: HANDLE_FLAG_PROTECT_FROM_CLOSE."""
    src, _, sh, _ = dirs
    write(src / "a.pdf", b"x")
    fh = open_file(sh, "a.pdf")
    raw = fh.raw
    k = _kernel32()
    assert k.SetHandleInformation(
        raw, HANDLE_FLAG_PROTECT_FROM_CLOSE, HANDLE_FLAG_PROTECT_FROM_CLOSE
    )  # type: ignore[attr-defined]
    try:
        with pytest.raises(w.HandleCloseError) as raised:
            fh.close()
        assert raised.value.winerror == w.ERROR_INVALID_HANDLE
        assert fh.state is w.HandleState.CLOSE_FAILED
        assert _is_open(raw)
        with pytest.raises(PermissionError):
            (src / "a.pdf").unlink()
    finally:
        assert k.SetHandleInformation(raw, HANDLE_FLAG_PROTECT_FROM_CLOSE, 0)  # type: ignore[attr-defined]
        assert k.CloseHandle(raw)  # type: ignore[attr-defined]
    (src / "a.pdf").unlink()


def test_detached_handle_is_not_closed(
    dirs: tuple[Path, Path, w.Handle, w.Handle], close_failures
) -> None:
    _, dst, _, dh = dirs
    fh = w.create_file(dh, "out.bin", access=WRITE_ACCESS, share=w.FILE_SHARE_READ)
    close_failures.select = lambda handle: True  # any close would fail and be seen
    raw = fh.detach()
    fh.close()
    assert fh.state is w.HandleState.DETACHED and close_failures.failed == []
    assert _is_open(raw)
    close_failures.select = lambda handle: False
    w.Handle(raw, dst / "out.bin").close()
    (dst / "out.bin").unlink()


def test_open_stream_leaves_the_handle_to_the_caller_when_open_osfhandle_fails(
    dirs: tuple[Path, Path, w.Handle, w.Handle], monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    """HANDLE-02: nothing is closed (and no close error lost) inside ``open_stream``."""
    import msvcrt

    _, dst, _, dh = dirs
    fh = w.create_file(dh, "out.bin", access=WRITE_ACCESS, share=w.FILE_SHARE_READ)
    close_failures.select = lambda handle: handle is fh

    def injected(*args: object) -> int:
        raise OSError("injected")

    monkeypatch.setattr(msvcrt, "open_osfhandle", injected)
    with pytest.raises(OSError, match="injected"):
        w.open_stream(fh)
    assert fh.state is w.HandleState.OPEN and close_failures.failed == []
    with pytest.raises(w.HandleCloseError) as raised:
        fh.close()  # the caller's close: its failure is the caller's to report
    assert raised.value.handle_value == close_failures.failed[0]
    assert fh.state is w.HandleState.CLOSE_FAILED
    close_failures.really_close()
    (dst / "out.bin").unlink()


def _protect(raw: int, on: bool) -> None:
    flag = HANDLE_FLAG_PROTECT_FROM_CLOSE
    assert _kernel32().SetHandleInformation(raw, flag, flag if on else 0)  # type: ignore[attr-defined]


@pytest.mark.parametrize("stage", ["FileIO", "BufferedWriter"])
def test_open_stream_keeps_both_errors_when_the_release_fails_too(
    dirs: tuple[Path, Path, w.Handle, w.Handle], monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """After ``open_osfhandle`` the CRT owns the handle; a real ``CloseHandle`` FALSE
    (HANDLE_FLAG_PROTECT_FROM_CLOSE) while releasing it is kept next to the error."""
    _, dst, _, dh = dirs
    fh = w.create_file(dh, "out.bin", access=WRITE_ACCESS, share=w.FILE_SHARE_READ)
    raw = fh.raw
    _protect(raw, True)
    real = getattr(w, stage)

    def injected(*args: object, **kwargs: object) -> object:
        if stage == "BufferedWriter":
            assert isinstance(args[0], io.FileIO)
        raise OSError("injected")

    monkeypatch.setattr(w, stage, injected)
    try:
        with pytest.raises(w.ReleaseError) as raised:
            w.open_stream(fh)
        error = raised.value
        assert isinstance(error.error, OSError) and str(error.error) == "injected"
        assert error.__cause__ is error.error and str(error) == "OSError: injected"
        (release,) = error.releases
        assert isinstance(release, OSError) and release is not error.error  # separate object
        assert fh.state is w.HandleState.DETACHED  # the CRT had it: never closed twice
        assert _is_open(raw)  # the failed release really left it open
    finally:
        monkeypatch.setattr(w, stage, real)
        _protect(raw, False)
        assert _kernel32().CloseHandle(raw)  # type: ignore[attr-defined]
    (dst / "out.bin").unlink()


def test_failed_close_after_the_crt_took_the_handle_is_raised(
    dirs: tuple[Path, Path, w.Handle, w.Handle],
) -> None:
    """Closing the stream closes the OS handle; a failure of that is not swallowed."""
    _, dst, _, dh = dirs
    fh = w.create_file(dh, "out.bin", access=WRITE_ACCESS, share=w.FILE_SHARE_READ)
    raw = fh.raw
    stream = w.open_stream(fh)
    stream.write(b"x")
    _protect(raw, True)
    try:
        with pytest.raises(OSError):
            stream.close()
        assert stream.closed and _is_open(raw)
    finally:
        _protect(raw, False)
        assert _kernel32().CloseHandle(raw)  # type: ignore[attr-defined]
    assert (dst / "out.bin").read_bytes() == b"x"
    (dst / "out.bin").unlink()


# --- release failures kept as objects (HANDLE-04) -------------------------------------


def test_attach_release_errors_keeps_order_and_identity_and_never_the_exception_itself() -> None:
    interrupt = KeyboardInterrupt()
    first, second = OSError(6, "first"), OSError(6, "second")
    assert w.attach_release_errors(interrupt, [first, interrupt, first]) is interrupt
    w.attach_release_errors(interrupt, [second, first])
    assert interrupt.release_errors == (first, second)  # type: ignore[attr-defined]
    w.attach_release_errors(interrupt, [])
    assert interrupt.release_errors == (first, second)  # type: ignore[attr-defined]


def test_release_failures_follows_nested_errors_depth_first_once_each() -> None:
    outer, inner = KeyboardInterrupt(), SystemExit(3)
    a, b, c = OSError(6, "a"), OSError(6, "b"), OSError(6, "c")
    w.attach_release_errors(inner, [a, b])
    w.attach_release_errors(b, [c, outer])  # a cycle back to the outer one: not repeated
    w.attach_release_errors(outer, [inner, c])
    assert w.release_failures(outer) == [inner, a, b, c]
    assert w.release_failures(OSError()) == []
    released = w.ReleaseError(ValueError("op"), [a, inner])
    assert w.release_failures(released) == [a, inner, b, c, outer]


def test_describe_error_keeps_the_notes() -> None:
    exc = SystemExit(4)
    assert w.describe_error(exc) == "SystemExit: 4"
    exc.add_note("first")
    exc.add_note("second")
    assert w.describe_error(exc) == "SystemExit: 4 [notes: first | second]"


def test_release_after_an_interrupt_keeps_the_release_failure_as_an_object() -> None:
    interrupt, failure = KeyboardInterrupt(), OSError(6, "The handle is invalid")

    def release() -> None:
        raise failure

    try:
        w.release_after(interrupt, release)  # the caller raises the interrupt itself
    except BaseException as exc:  # an escaping interrupt would end the session, not fail
        pytest.fail(f"release_after raised {exc!r} instead of returning")
    assert interrupt.release_errors == (failure,)  # type: ignore[attr-defined]
    assert interrupt.__notes__ == [f"then releasing failed: OSError: {failure}"]


def test_interrupt_while_releasing_after_an_error_is_raised_with_the_error_noted() -> None:
    error, interrupt = OSError(5, "op failed"), SystemExit(2)
    error.add_note("detail")

    def release() -> None:
        raise interrupt

    with pytest.raises(SystemExit) as raised:
        w.release_after(error, release)
    assert raised.value is interrupt
    assert interrupt.__notes__ == [f"while releasing after OSError: {error} [notes: detail]"]


def test_release_after_an_error_wraps_both_and_keeps_the_release_failure() -> None:
    error, failure = OSError(5, "op failed"), OSError(6, "The handle is invalid")

    def release() -> None:
        raise failure

    with pytest.raises(w.ReleaseError) as raised:
        w.release_after(error, release)
    assert raised.value.error is error and raised.value.__cause__ is error
    assert raised.value.releases == raised.value.release_errors == (failure,)


# --- the primary error survives a failed or interrupted release (HANDLE-05) ------------


def _hce(raw: int = 0x1F4) -> w.HandleCloseError:
    return w._close_error(raw, Path("C:/held/dir"), 31)


# primary (what the operation ends with, or None), what releasing raises
RELEASE_MATRIX: dict[str, Callable[[], tuple[BaseException | None, BaseException]]] = {
    "KeyboardInterrupt, then SystemExit": lambda: (KeyboardInterrupt("A"), SystemExit("B")),
    "KeyboardInterrupt twice": lambda: (KeyboardInterrupt("A"), KeyboardInterrupt("B")),
    "SystemExit, then KeyboardInterrupt": lambda: (SystemExit("A"), KeyboardInterrupt("B")),
    "SystemExit, then HandleCloseError": lambda: (SystemExit("A"), _hce()),
    "KeyboardInterrupt, then OSError": lambda: (KeyboardInterrupt("A"), OSError(6, "B")),
    "OSError, then SystemExit": lambda: (OSError(5, "A"), SystemExit("B")),
    "OSError twice": lambda: (OSError(5, "A"), OSError(6, "B")),
    "nothing, then KeyboardInterrupt": lambda: (None, KeyboardInterrupt("B")),
}


def _held_operation(primary: BaseException | None, release: Callable[[], object]) -> None:
    """An operation that holds something, written like every caller of ``release_after``."""
    try:
        if primary is not None:
            raise primary
    except BaseException as exc:
        w.release_after(exc, release)
        raise
    release()


def _check_release_matrix(
    raised: BaseException, primary: BaseException | None, secondary: BaseException
) -> None:
    """What reaches the caller for one row of ``RELEASE_MATRIX``."""
    if primary is not None and not isinstance(primary, Exception):
        assert raised is primary  # the very object, not a new one of its class
        assert w.release_failures(primary) == [secondary]
        note = primary.__notes__[-1]
        assert note == f"then releasing failed: {w.describe_error(secondary)}"
    elif primary is None:
        assert raised is secondary and w.release_failures(secondary) == []
    elif not isinstance(secondary, Exception):
        assert raised is secondary  # an interrupt is never dropped
        assert w.release_failures(secondary) == [primary]  # nor the error before it
        assert secondary.__notes__[-1] == f"while releasing after {w.describe_error(primary)}"
    else:
        assert isinstance(raised, w.ReleaseError)
        assert raised.error is primary and raised.__cause__ is primary
        assert w.release_failures(raised) == [secondary]


@pytest.mark.parametrize("case", list(RELEASE_MATRIX))
def test_release_after_keeps_the_primary_in_every_combination(case: str) -> None:
    primary, secondary = RELEASE_MATRIX[case]()
    released: list[int] = []

    def release() -> None:
        released.append(1)
        raise secondary

    with pytest.raises(BaseException) as raised:
        _held_operation(primary, release)
    _check_release_matrix(raised.value, primary, secondary)
    assert released == [1]  # released once, never again for the second error


def test_release_after_keeps_what_the_secondary_interrupt_carries() -> None:
    primary, secondary, inner = KeyboardInterrupt("A"), SystemExit("B"), _hce(0x2A)
    w.attach_release_errors(secondary, [inner])

    def release() -> None:
        raise secondary

    with pytest.raises(KeyboardInterrupt) as raised:
        _held_operation(primary, release)
    assert raised.value is primary
    assert w.release_failures(primary) == [secondary, inner]  # nested ones as well


def test_release_after_keeps_the_values_of_a_handle_close_error() -> None:
    primary, closing = SystemExit("A"), _hce(0x1F4)

    def release() -> None:
        raise closing

    with pytest.raises(SystemExit) as raised:
        _held_operation(primary, release)
    assert raised.value is primary
    (kept,) = w.release_failures(primary)
    assert kept is closing
    assert (kept.winerror, kept.handle_value) == (31, 0x1F4)
    assert kept.filename == str(Path("C:/held/dir"))
    assert "0x1f4" in primary.__notes__[-1] and "HandleCloseError" in primary.__notes__[-1]


@pytest.mark.parametrize("case", list(RELEASE_MATRIX))
def test_handle_exit_keeps_the_primary_in_every_combination(
    dirs: tuple[Path, Path, w.Handle, w.Handle], close_failures, case: str
) -> None:
    """``with handle:`` on a real handle; ``CloseHandle`` itself raises the secondary
    (after really closing it), or returns ``FALSE`` for a ``HandleCloseError``."""
    primary, secondary = RELEASE_MATRIX[case]()
    src, _, sh, _ = dirs
    write(src / "a.pdf", b"x")
    close_failures.select = lambda handle: handle.path == src / "a.pdf"
    fake_failure = isinstance(secondary, w.HandleCloseError)
    if fake_failure:
        close_failures.limit = 1
    else:
        close_failures.limit, close_failures.interrupt = 0, secondary
    fh = open_file(sh, "a.pdf")
    raw = fh.raw

    with pytest.raises(BaseException) as raised, fh:
        if primary is not None:
            raise primary
    if fake_failure:
        secondary = fh.close_error  # the real one, raised by close()
        assert secondary is not None and secondary.handle_value == raw
    _check_release_matrix(raised.value, primary, secondary)
    assert fh.state is w.HandleState.CLOSE_FAILED  # never reported as released
    assert [h for h in close_failures.closed if h is fh] == [fh]  # CloseHandle once
    with pytest.raises(w.HandleCloseError):
        fh.close()
    assert [h for h in close_failures.closed if h is fh] == [fh]  # still once
    close_failures.really_close()
    (src / "a.pdf").unlink()  # released


# --- a release before another one stays found: through ReleaseError.error (HANDLE-06) --


def _closing(raw: int, name: str) -> w.HandleCloseError:
    return w._close_error(raw, Path(f"C:/held/{name}"), 31)


def _assert_same(found: list[BaseException], expected: list[BaseException | None]) -> None:
    """The very objects, in this order, each once (not equal ones, not copies)."""
    assert len(found) == len(expected), (found, expected)
    for got, want in zip(found, expected, strict=True):
        assert got is want, (found, expected)


def _assert_closing(error: BaseException | None, raw: int, path: Path) -> None:
    assert isinstance(error, w.HandleCloseError)
    assert (error.winerror, error.handle_value, error.filename) == (31, raw, str(path))


def test_release_failures_finds_a_release_before_a_second_one() -> None:  # A
    body = OSError(5, "body")
    first, second = _closing(0x10, "first"), _closing(0x20, "second")
    first.add_note("first detail")
    inner = w.prevailing(body, first)
    outer = w.prevailing(inner, second)
    assert isinstance(outer, w.ReleaseError) and isinstance(inner, w.ReleaseError)
    assert outer.error is inner and inner.error is body  # the structure is unchanged
    assert outer.releases == outer.release_errors == (second,)
    assert inner.releases == (first,)

    failures = w.release_failures(outer)

    _assert_same(failures, [first, second])  # first close before second close
    _assert_closing(failures[0], 0x10, Path("C:/held/first"))
    _assert_closing(failures[1], 0x20, Path("C:/held/second"))
    assert first.__notes__ == ["first detail"]  # kept as raised, notes included
    _assert_same(w.release_failures(inner), [first])


def test_release_failures_finds_three_releases_in_order_once_each() -> None:  # B
    closing = [_closing(0x10 * (n + 1), f"h{n}") for n in range(3)]
    raised: BaseException = OSError(5, "body")
    for error in closing:
        raised = w.prevailing(raised, error)

    failures = w.release_failures(raised)

    _assert_same(failures, list(closing))
    for n, error in enumerate(failures):
        _assert_closing(error, 0x10 * (n + 1), Path(f"C:/held/h{n}"))


def test_release_failures_has_no_depth_limit() -> None:
    """Deeper than the recursion limit: a walk on a stack, not by recursion."""
    depth = sys.getrecursionlimit() + 500
    chain = [OSError(6, f"release {n}") for n in range(depth)]
    for outer, inner in itertools.pairwise(chain):
        w.attach_release_errors(outer, [inner])
    interrupt = KeyboardInterrupt()
    w.attach_release_errors(interrupt, [chain[0]])
    _assert_same(w.release_failures(interrupt), list(chain))
    raised: BaseException = OSError(5, "body")
    for error in chain[:60]:  # ReleaseError in ReleaseError, 60 deep
        raised = w.prevailing(raised, error)
    _assert_same(w.release_failures(raised), list(chain))  # with every one nested in those


def _file_handles(sh: w.Handle, src: Path, names: list[str]) -> list[w.Handle]:
    for name in names:
        write(src / name, b"x")
    return [open_file(sh, name) for name in names]


def _closed_once(close_failures, handles: list[w.Handle]) -> None:  # type: ignore[no-untyped-def]
    for handle in handles:
        assert [h for h in close_failures.closed if h is handle] == [handle]
        assert handle.state is w.HandleState.CLOSE_FAILED
        with pytest.raises(w.HandleCloseError) as again:
            handle.close()  # the same error again, CloseHandle not called again
        assert again.value is handle.close_error
        assert [h for h in close_failures.closed if h is handle] == [handle]


def test_nested_handle_exits_keep_every_failed_close(
    dirs: tuple[Path, Path, w.Handle, w.Handle], close_failures
) -> None:  # C
    src, _, sh, _ = dirs
    names = ["a.pdf", "b.pdf", "c.pdf"]
    handles = _file_handles(sh, src, names)
    fa, fb, fc = handles
    raws = [h.raw for h in handles]
    close_failures.select = lambda handle: any(handle is h for h in handles)
    close_failures.limit = 3
    body = OSError(5, "body")

    with pytest.raises(w.ReleaseError) as raised, fa, fb, fc:
        raise body

    # fc's __exit__ runs first, with ``body``; fb's with fc's ReleaseError; then fa's.
    outer = raised.value
    assert isinstance(outer.error, w.ReleaseError)
    assert isinstance(outer.error.error, w.ReleaseError)
    assert outer.error.error.error is body
    failures = w.release_failures(outer)
    _assert_same(failures, [fc.close_error, fb.close_error, fa.close_error])
    for error, raw, name in zip(failures, raws[::-1], names[::-1], strict=True):
        _assert_closing(error, raw, src / name)
    assert sorted(raws) == sorted(close_failures.failed)
    assert all(e is not body for e in failures)
    _closed_once(close_failures, handles)
    close_failures.really_close()
    for name in names:
        (src / name).unlink()  # released


def test_two_failed_releases_then_an_interrupt_keep_all_three() -> None:  # D
    body, first, second = OSError(5, "body"), _closing(0x10, "first"), _closing(0x20, "second")
    released = w.prevailing(w.prevailing(body, first), second)
    exit_ = SystemExit(3)
    assert w.prevailing(released, exit_) is exit_  # an interrupted release prevails
    _assert_same(w.release_failures(exit_), [released, first, second])
    assert exit_.__notes__ == [f"while releasing after {w.describe_error(released)}"]
    # an interrupted operation: two failed releases, then an interrupted one
    interrupt, third = KeyboardInterrupt(), SystemExit(4)
    for error in (first, second, third):
        assert w.prevailing(interrupt, error) is interrupt  # never a new object
    _assert_same(w.release_failures(interrupt), [first, second, third])


@pytest.mark.parametrize("primary", ["OSError", "KeyboardInterrupt"])
def test_nested_handle_exits_then_an_interrupted_close(
    dirs: tuple[Path, Path, w.Handle, w.Handle], close_failures, primary: str
) -> None:  # D with real handles: fc and fb fail CloseHandle, then fa's raises SystemExit
    src, _, sh, _ = dirs
    names = ["a.pdf", "b.pdf", "c.pdf"]
    handles = _file_handles(sh, src, names)
    fa, fb, fc = handles
    raws = [h.raw for h in handles]
    exit_ = SystemExit(4)

    def select(handle: object) -> bool:
        if handle is fa:
            close_failures.interrupt = exit_  # raised by fa's CloseHandle, after closing it
        return any(handle is h for h in handles)

    close_failures.select, close_failures.limit = select, 2
    body: BaseException = OSError(5, "body") if primary == "OSError" else KeyboardInterrupt()

    with pytest.raises(BaseException) as raised, fa, fb, fc:
        raise body

    hc, hb = fc.close_error, fb.close_error
    _assert_closing(hc, raws[2], src / "c.pdf")
    _assert_closing(hb, raws[1], src / "b.pdf")
    if primary == "KeyboardInterrupt":
        assert raised.value is body  # not replaced by SystemExit, nor by a new object
        _assert_same(w.release_failures(body), [hc, hb, exit_])
    else:
        assert raised.value is exit_  # an interrupt is never dropped
        (released,) = exit_.release_errors  # type: ignore[attr-defined]
        assert isinstance(released, w.ReleaseError)
        assert isinstance(released.error, w.ReleaseError) and released.error.error is body
        _assert_same(w.release_failures(exit_), [released, hc, hb])
    assert sorted(close_failures.failed) == sorted(raws[1:])
    _closed_once(close_failures, [fb, fc])
    assert [h for h in close_failures.closed if h is fa] == [fa]  # tried once as well
    close_failures.really_close()
    for name in names:
        (src / name).unlink()  # released


def test_prevailing_again_keeps_what_the_error_already_carried() -> None:  # E
    earlier, first, second = _closing(0x10, "earlier"), _closing(0x20, "first"), _closing(0x30, "s")
    body = OSError(5, "body")
    w.attach_release_errors(body, [earlier])  # e.g. a JournalError with its releases
    outer = w.prevailing(body, first)
    _assert_same(w.release_failures(outer), [earlier, first])
    exit_ = SystemExit(2)
    w.attach_release_errors(exit_, [second])  # what the interrupt already carried
    assert w.prevailing(outer, exit_) is exit_
    _assert_same(w.release_failures(exit_), [second, outer, earlier, first])
    interrupt = KeyboardInterrupt()
    w.attach_release_errors(interrupt, [earlier])
    assert w.prevailing(interrupt, first) is interrupt
    assert w.prevailing(interrupt, outer) is interrupt
    _assert_same(w.release_failures(interrupt), [earlier, first, outer])


def test_release_failures_lists_an_object_found_twice_once() -> None:  # F
    body, shared, other = OSError(5, "body"), _closing(0x10, "shared"), _closing(0x20, "other")
    inner = w.prevailing(body, shared)
    w.attach_release_errors(other, [shared])  # the same object under the second one too
    outer = w.prevailing(inner, other)
    _assert_same(w.release_failures(outer), [shared, other])
    both = w.ReleaseError(OSError(5, "op"), [inner, shared, other])  # and next to it
    _assert_same(w.release_failures(both), [inner, shared, other])
    interrupt = KeyboardInterrupt()
    w.attach_release_errors(interrupt, [inner, outer, shared])
    _assert_same(w.release_failures(interrupt), [inner, shared, outer, other])


class _WalkedOnce:
    """``release_errors`` that fails the test when walked again (a cycle not noticed),
    instead of letting the walk run forever."""

    def __init__(self, *errors: BaseException) -> None:
        self.errors, self.walks = errors, 0

    def __iter__(self) -> Iterator[BaseException]:
        self.walks += 1
        assert self.walks <= 1, "walked again: a cycle was not noticed"
        return iter(self.errors)


def test_release_failures_ends_on_a_cycle() -> None:  # G
    body, a, b = OSError(5, "body"), _closing(0x10, "a"), _closing(0x20, "b")
    inner = w.ReleaseError(body, [a])
    outer = w.ReleaseError(inner, [b])
    inner.error = outer  # a cycle through the operation errors
    inner.release_errors = _WalkedOnce(a)  # type: ignore[assignment]
    outer.release_errors = _WalkedOnce(b)  # type: ignore[assignment]
    w.attach_release_errors(b, [outer])  # and back to the start through a release failure
    a.release_errors = (a,)  # type: ignore[attr-defined]  # to itself (attach never does)
    _assert_same(w.release_failures(outer), [a, b])

    c = _closing(0x30, "c")
    loop = w.ReleaseError(OSError(5, "body"), [])
    loop.error = loop
    loop.release_errors = _WalkedOnce(c, loop)  # type: ignore[assignment]
    _assert_same(w.release_failures(loop), [c])

    x, y = OSError(6, "x"), OSError(6, "y")
    w.attach_release_errors(x, [y])
    w.attach_release_errors(y, [x])
    _assert_same(w.release_failures(x), [y])  # the start itself is never listed
    interrupt = KeyboardInterrupt()
    w.attach_release_errors(interrupt, [x])
    _assert_same(w.release_failures(interrupt), [x, y])


def test_the_operation_error_is_not_a_release_failure() -> None:  # H
    body, first = OSError(5, "body"), _closing(0x10, "first")
    body.add_note("what failed")
    outer = w.prevailing(body, first)
    _assert_same(w.release_failures(outer), [first])
    assert isinstance(outer, w.ReleaseError)
    assert outer.error is body and outer.__cause__ is body
    assert body.__notes__ == ["what failed"]
    second = _closing(0x20, "second")
    twice = w.prevailing(outer, second)
    _assert_same(w.release_failures(twice), [first, second])  # not body, not ``outer``
    assert isinstance(twice, w.ReleaseError) and twice.error is outer
    assert w.release_failures(w.ReleaseError(body, [])) == []
    # what merely caused the operation error is not followed, nor its context
    cause, context = KeyboardInterrupt(), SystemExit(1)
    w.attach_release_errors(cause, [_closing(0x30, "cause")])
    w.attach_release_errors(context, [_closing(0x40, "context")])
    other = OSError(5, "other")
    other.__cause__, other.__context__ = cause, context
    _assert_same(w.release_failures(w.prevailing(other, first)), [first])
