"""Shared test fixtures.

Every test runs with per-user application directories redirected into ``tmp_path``,
so no test can read or write real settings or logs of the current Windows user.
"""

from __future__ import annotations

import builtins
import contextlib
import ctypes
import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

TreeSnapshot = dict[str, tuple[object, ...]]


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("windows_only") and sys.platform != "win32":
        pytest.skip("requires Windows")


@pytest.fixture(autouse=True)
def isolated_user_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "user_home"
    for name, sub in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local")):
        path = home / "AppData" / sub
        path.mkdir(parents=True)
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def snapshot_tree(root: Path) -> TreeSnapshot:
    """Names, types, sizes, mtimes, attributes and content hashes below ``root``.

    Links and reparse points are recorded but neither followed nor read. Metadata is
    read with a fresh ``lstat``: the copy in the NTFS directory listing is updated
    lazily, so a directory's listed mtime can change merely because someone stat-ed it.
    """
    result: TreeSnapshot = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as it:
            for entry in it:
                st = Path(entry.path).stat(follow_symlinks=False)
                attrs = getattr(st, "st_file_attributes", 0)
                tag = getattr(st, "st_reparse_tag", 0)
                link = stat.S_ISLNK(st.st_mode) or bool(tag)
                rel = os.path.relpath(entry.path, root)
                digest = None
                if stat.S_ISREG(st.st_mode) and not link:
                    digest = hashlib.sha256(Path(entry.path).read_bytes()).hexdigest()
                is_dir = stat.S_ISDIR(st.st_mode)
                result[rel] = (is_dir, link, st.st_size, st.st_mtime_ns, attrs, tag, digest)
                if is_dir and not link:
                    pending.append(Path(entry.path))
    return result


@pytest.fixture
def snapshot() -> Callable[[Path], TreeSnapshot]:
    return snapshot_tree


def _forbidden(name: str) -> Callable[..., object]:
    def fail(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"side effect during planning: {name}{args!r}")

    return fail


_MUTATING_OS = (
    "mkdir", "makedirs", "rename", "renames", "replace", "remove", "unlink", "rmdir",
    "removedirs", "link", "symlink", "truncate", "utime", "chmod", "open",
)  # fmt: skip
_MUTATING_SHUTIL = (
    "move", "copy", "copy2", "copyfile", "copytree", "rmtree", "copystat", "copymode",
)  # fmt: skip


@pytest.fixture
def no_side_effects() -> Callable[[], contextlib.AbstractContextManager[None]]:
    """Context manager that makes any file creation, change or open fail loudly.

    Opening for reading is forbidden too: the planner must never touch file contents.
    """

    @contextlib.contextmanager
    def guard() -> Iterator[None]:
        with pytest.MonkeyPatch.context() as mp:
            for name in _MUTATING_OS:
                mp.setattr(os, name, _forbidden(f"os.{name}"))
            for name in _MUTATING_SHUTIL:
                mp.setattr(shutil, name, _forbidden(f"shutil.{name}"))
            mp.setattr(builtins, "open", _forbidden("open"))
            mp.setattr(io, "open", _forbidden("io.open"))
            yield

    return guard


# --- Windows helpers (only used by tests marked windows_only) -------------------------


@pytest.fixture
def make_junction() -> Callable[[Path, Path], Path]:
    def make(link: Path, target: Path) -> Path:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
        )
        if result.returncode != 0:
            pytest.fail(f"mklink /J failed: {result.stderr!r}")
        return link

    return make


@pytest.fixture
def set_attributes() -> Callable[[Path, int], None]:
    def set_attrs(path: Path, attributes: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.SetFileAttributesW(str(path), attributes):
            raise ctypes.WinError(ctypes.get_last_error())

    return set_attrs


@pytest.fixture
def make_case_sensitive() -> Callable[[Path], None]:
    def enable(directory: Path) -> None:
        result = subprocess.run(
            ["fsutil.exe", "file", "setCaseSensitiveInfo", str(directory), "enable"],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("per-directory case sensitivity is not available")

    return enable


@pytest.fixture
def deny_listing() -> Iterator[Callable[[Path], None]]:
    """Deny listing a directory to Everyone via its ACL; restored on teardown."""
    denied: list[Path] = []

    def deny(directory: Path) -> None:
        subprocess.run(
            ["icacls", str(directory), "/deny", "*S-1-1-0:(RD)"], check=True, capture_output=True
        )
        denied.append(directory)

    yield deny
    for directory in denied:
        subprocess.run(
            ["icacls", str(directory), "/remove:d", "*S-1-1-0"], check=True, capture_output=True
        )


class CloseFailures:
    """``CloseHandle`` returns ``FALSE`` (``GetLastError`` = ``error``) for chosen handles.

    The handle is really left open, as after a failed ``CloseHandle``; ``failed`` are
    the raw values, closed for real on teardown (``CloseHandle`` must succeed then).
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, error: int = 31) -> None:
        from downloads_organizer import winfileops

        self.error = error
        self.select: Callable[[object], bool] = lambda handle: False
        self.limit = 1  # how many closes fail at most
        # Raised once by the first chosen close instead, after really closing the handle.
        self.interrupt: BaseException | None = None
        self.failed: list[int] = []
        self.handles: dict[int, object] = {}
        self.opened: list[object] = []  # every Handle object, in the order of creation
        self.closed: list[object] = []  # the Handle of every CloseHandle call
        self.kernel32 = winfileops._api().kernel32
        real_init, self.real_close = winfileops.Handle.__init__, self.kernel32.CloseHandle
        failures = self

        def init(handle: object, raw: int, path: Path) -> None:
            real_init(handle, raw, path)  # type: ignore[arg-type]
            failures.handles[raw] = handle
            failures.opened.append(handle)

        def close(raw: int) -> object:
            handle = failures.handles.get(raw)
            failures.closed.append(handle)
            chosen = handle is not None and failures.select(handle)
            if chosen and failures.interrupt is not None:
                interrupt, failures.interrupt = failures.interrupt, None
                failures.real_close(raw)
                raise interrupt
            if chosen and len(failures.failed) < failures.limit:
                failures.failed.append(raw)
                ctypes.set_last_error(failures.error)
                return 0
            return failures.real_close(raw)

        monkeypatch.setattr(winfileops.Handle, "__init__", init)
        monkeypatch.setattr(self.kernel32, "CloseHandle", close)

    def really_close(self) -> None:
        raws, self.failed = self.failed, []
        for raw in raws:
            assert self.real_close(raw), f"handle {raw:#x} was not open any more"

    def assert_each_closed_once(self) -> None:
        """Every ``Handle`` was given to ``CloseHandle`` exactly once (none retried);
        one handed over (the journal file, to the C runtime) is not closed as a Handle."""
        counts = {id(h): sum(1 for c in self.closed if c is h) for h in self.opened}
        twice = [h for h in self.opened if counts[id(h)] > 1]
        never = [h for h in self.opened if counts[id(h)] == 0 and str(h.state) != "detached"]  # type: ignore[attr-defined]
        assert not twice and not never, (twice, never)


@pytest.fixture
def close_failures(monkeypatch: pytest.MonkeyPatch) -> Iterator[CloseFailures]:
    failures = CloseFailures(monkeypatch)
    try:
        yield failures
    finally:
        failures.really_close()


class SubstDrives:
    """Real ``subst`` drive letters, only on free letters and only to test directories.

    ``map`` records the mapping it made (as ``QueryDosDevice`` reports it); cleanup
    removes exactly those, and only while they still are what was made here: a
    mapping of the user (or of anyone else) is never touched.
    """

    def __init__(self) -> None:
        self.made: list[tuple[str, str]] = []

    def free(self, count: int) -> list[str]:
        """Unused drive letters (no volume, no DOS device), from Z: down."""
        from downloads_organizer import winfileops

        used = ctypes.WinDLL("kernel32").GetLogicalDrives()
        taken = {letter for letter, _ in self.made}
        free: list[str] = []
        for letter in "ZYXWVUTSRQPONMLKJIHGFED":
            name = f"{letter}:"
            if used & (1 << (ord(letter) - ord("A"))) or name in taken:
                continue
            try:
                winfileops.query_dos_device(name)
            except FileNotFoundError:
                free.append(name)
                if len(free) == count:
                    return free
        pytest.skip("NOT_TESTED: not enough free drive letters")

    def map(self, letter: str, path: str) -> None:
        from downloads_organizer import winfileops

        done = subprocess.run(["subst", letter, path], capture_output=True, text=True)
        if done.returncode != 0:
            pytest.skip(f"NOT_TESTED: subst {letter} failed: {done.stdout}{done.stderr}")
        (current,) = winfileops.query_dos_device(letter)
        self.made.append((letter, current))  # what cleanup removes, and only that
        # A drive root is kept without its backslash: \??\C: for C:\.
        assert current in ("\\??\\" + path, "\\??\\" + path.rstrip("\\")), current

    def remove(self) -> list[str]:
        """Remove what ``map`` made, newest first; what could not be removed."""
        from downloads_organizer import winfileops

        leftovers: list[str] = []
        made, self.made = self.made, []
        for letter, expected in reversed(made):
            try:
                current = winfileops.query_dos_device(letter)
            except OSError as exc:
                leftovers.append(f"{letter}: cannot check ({exc})")
                continue
            if current != (expected,):
                leftovers.append(f"{letter}: is {current!r} now, not ours: left alone")
                continue
            subprocess.run(["subst", letter, "/d"], capture_output=True, check=False)
            try:
                left = winfileops.query_dos_device(letter)
            except FileNotFoundError:
                continue
            leftovers.append(f"{letter}: still defined as {left!r}")
        return leftovers


@pytest.fixture
def subst_drives() -> Iterator[SubstDrives]:
    """Opt-in (``DOWNLOADSORGANIZER_REAL_SUBST=1``): real subst drives, removed in the end."""
    if sys.platform != "win32" or os.environ.get("DOWNLOADSORGANIZER_REAL_SUBST") != "1":
        pytest.skip("NOT_TESTED: real subst drives, opt in with DOWNLOADSORGANIZER_REAL_SUBST=1")
    drives = SubstDrives()
    try:
        yield drives
    finally:
        leftovers = drives.remove()
        if leftovers:  # also when the test failed: the original error is the context
            pytest.fail(f"subst drives not removed: {leftovers}")


# --- R3B-3: the ownership stand (FIX-03 §9.1, ERRATA B-01) ------------------------------


@dataclass(eq=False)
class Acq:
    """One acquisition: a raw handle value bound to one owner (``__init__`` or ``_bind``).

    Counted per acquisition, never per raw value: the same value may be handed out again
    once it was closed (the fake pool does that at once, Windows often does).
    """

    serial: int
    owner: object
    raw: int
    kind: str  # "init" or "bind"
    closes: int = 0  # production CloseHandle calls
    torn_down: bool = False  # closed directly by the stand's teardown


class FakePool:
    """Phantom HANDLE values (never real handles); a released value is handed out again
    at once, the lowest first."""

    BASE = 0x7FFF_0000_0000

    def __init__(self) -> None:
        self.free: list[int] = []
        self.issued = 0
        self.out: set[int] = set()

    def take(self) -> int:
        if self.free:
            raw = min(self.free)
            self.free.remove(raw)
        else:
            raw = self.BASE + 4 * self.issued
            self.issued += 1
        self.out.add(raw)
        return raw

    def release(self, raw: int) -> bool:
        if raw not in self.out:
            return False
        self.out.remove(raw)
        self.free.append(raw)
        return True


class Acquisitions:
    """The stand of FIX-03 §9.1: every bind of a raw handle and every ``CloseHandle``.

    * ``Handle.__init__`` and ``Handle._bind`` make an ``Acq``; binding a raw value that
      is live (bound and not closed) is a violation.
    * The ``CloseHandle`` spy: a raw value without a live ``Acq`` is a violation (and is
      not passed on). For an owner ``select`` chooses, up to ``limit`` closes fail
      (``FALSE``, ``GetLastError`` = ``error``): the value stays live, in ``failed``; or
      ``interrupt`` is raised once after really closing it.
    * ``finish`` (teardown) closes ``failed`` directly, never through ``Handle.close``,
      then reports: violations, an ``Acq`` closed twice, an ``Acq`` never closed (unless
      its owner is ``DETACHED``), a live value left (closed then, so nothing leaks).

    ``pool`` (fake runs): closes go to the pool instead of ``kernel32``. ``accounting``
    and ``teardown`` select the deliberate stand mutants of T67.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        pool: FakePool | None = None,
        accounting: str = "acq",
        teardown: str = "direct",
        error: int = 31,
    ) -> None:
        from downloads_organizer import winfileops

        self.pool = pool
        self.accounting = accounting
        self.teardown = teardown
        self.error = error
        self.acqs: list[Acq] = []
        self.live: dict[int, Acq] = {}
        self.violations: list[str] = []
        self.failed: list[Acq] = []
        self.select: Callable[[object], bool] = lambda owner: False
        self.limit = 1
        self.interrupt: BaseException | None = None
        self.news = 0  # Handle.new calls
        self.inits = 0  # Handle.__init__ calls
        self.calls: list[int] = []  # the raw value of every production CloseHandle
        self._by_raw: dict[int, int] = {}  # the "raw" accounting mutant only
        kernel32 = winfileops._api().kernel32
        self.real_close = kernel32.CloseHandle
        handle_class = winfileops.Handle
        real_init, real_bind = handle_class.__init__, handle_class._bind
        real_new = handle_class.__dict__["new"].__func__
        stand = self

        def init(handle: object, raw: int, path: Path) -> None:
            real_init(handle, raw, path)  # type: ignore[arg-type]
            stand.inits += 1
            stand._acquire(handle, raw, "init")

        def bind(handle: object, raw: int) -> None:
            real_bind(handle, raw)  # type: ignore[arg-type]
            stand._acquire(handle, raw, "bind")

        def new(cls: type, path: Path, role: str) -> object:
            stand.news += 1
            return real_new(cls, path, role)

        monkeypatch.setattr(handle_class, "__init__", init)
        monkeypatch.setattr(handle_class, "_bind", bind)
        monkeypatch.setattr(handle_class, "new", classmethod(new))
        monkeypatch.setattr(kernel32, "CloseHandle", self._close)

    def of(self, owner: object) -> list[Acq]:
        return [acq for acq in self.acqs if acq.owner is owner]

    def _acquire(self, owner: object, raw: int, kind: str) -> None:
        if raw in self.live:
            self.violations.append(f"{raw:#x} bound to {owner!r} while live for another owner")
        acq = Acq(len(self.acqs), owner, raw, kind)
        self.acqs.append(acq)
        self.live[raw] = acq

    def _really_close(self, raw: int) -> bool:
        if self.pool is not None:
            return self.pool.release(raw)
        return bool(self.real_close(raw))

    def _close(self, raw: int) -> int:
        self.calls.append(raw)
        if self.accounting == "raw":  # mutant: counts per raw value
            self._by_raw[raw] = self._by_raw.get(raw, 0) + 1
            if self._by_raw[raw] > 1:
                self.violations.append(f"{raw:#x} closed twice")
        acq = self.live.get(raw)
        if acq is None:
            self.violations.append(f"CloseHandle({raw:#x}) without a live acquisition")
            ctypes.set_last_error(6)  # ERROR_INVALID_HANDLE; never passed on
            return 0
        acq.closes += 1
        if acq.closes > 1:
            self.violations.append(f"acquisition {acq.serial} closed {acq.closes} times")
        if self.select(acq.owner):
            if self.interrupt is not None:
                interrupt, self.interrupt = self.interrupt, None
                assert self._really_close(raw)
                del self.live[raw]
                raise interrupt
            if len(self.failed) < self.limit:
                self.failed.append(acq)
                ctypes.set_last_error(self.error)
                return 0
        if not self._really_close(raw):
            self.violations.append(f"{raw:#x} was not open when closed")
            return 0
        del self.live[raw]
        return 1

    def finish(self) -> list[str]:
        """Teardown: close the failed ones directly, then every problem found."""
        from downloads_organizer import winfileops

        failed, self.failed = self.failed, []
        for acq in failed:
            if self.teardown == "direct":
                assert self._really_close(acq.raw), f"{acq.raw:#x} was not open any more"
                del self.live[acq.raw]
                acq.torn_down = True
            else:  # mutant: through Handle.close, which never calls CloseHandle again
                with contextlib.suppress(winfileops.HandleCloseError):
                    acq.owner.close()  # type: ignore[attr-defined]
        problems = list(self.violations)
        for acq in self.acqs:
            detached = str(acq.owner.state) == "detached"  # type: ignore[attr-defined]
            if acq.closes == 0 and not detached:
                problems.append(f"acquisition {acq.serial} never closed: {acq.owner!r}")
        for raw, acq in list(self.live.items()):
            if str(acq.owner.state) == "detached":  # type: ignore[attr-defined]
                continue  # closed by whom it was handed to
            problems.append(f"{raw:#x} left open ({acq.owner!r})")
            self._really_close(raw)  # nothing leaks out of the test
            del self.live[raw]
        return problems


@pytest.fixture
def acquisitions(monkeypatch: pytest.MonkeyPatch) -> Iterator[Acquisitions]:
    """The §9.1 stand over the real ``kernel32.CloseHandle`` (real NTFS tests)."""
    stand = Acquisitions(monkeypatch)
    yield stand
    problems = stand.finish()
    if problems:
        pytest.fail(f"ownership stand: {problems}")


def _signed(status: int) -> int:
    status &= 0xFFFF_FFFF
    return status - (1 << 32) if status & 0x8000_0000 else status


class FakeNtdll:
    """Scripted ntdll for ``winprims`` (no real call but ``RtlNtStatusToDosError``).

    ``script[name]`` is a list of steps, taken in order; without one a call succeeds.
    Every call is logged in ``log`` as ``(name, details)``; ``hooks[name]`` sees the
    details first. A step is an int (the status), a ``BaseException`` (raised, nothing
    written) or a dict: ``status``, ``out`` (``"pool"`` or a value), ``information``,
    ``iosb_status``, ``data`` (written to the buffer), ``raise_after`` (raised after
    writing out, IOSB and data).
    """

    def __init__(self, pool: FakePool) -> None:
        from ctypes import wintypes

        self.pool = pool
        self.script: dict[str, list[object]] = {}
        self.log: list[tuple[str, dict[str, object]]] = []
        self.hooks: dict[str, Callable[[dict[str, object]], None]] = {}
        real = ctypes.WinDLL("ntdll").RtlNtStatusToDosError
        real.argtypes = [ctypes.c_long]
        real.restype = wintypes.ULONG
        self.RtlNtStatusToDosError = real

    def calls(self, name: str) -> list[dict[str, object]]:
        return [details for logged, details in self.log if logged == name]

    def _step(self, name: str, details: dict[str, object]) -> dict[str, object]:
        self.log.append((name, details))
        if name in self.hooks:
            self.hooks[name](details)
        steps = self.script.get(name)
        step = steps.pop(0) if steps else {}
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, int):
            step = {"status": step}
        return step  # type: ignore[return-value]

    @staticmethod
    def _finish(step: dict[str, object], iosb: Any, default_information: int) -> int:
        status = int(step.get("status", 0))  # type: ignore[call-overload]
        iosb.Status = int(step.get("iosb_status", status)) & 0xFFFF_FFFF  # type: ignore[call-overload]
        iosb.Information = int(step.get("information", default_information))  # type: ignore[call-overload]
        raise_after = step.get("raise_after")
        if isinstance(raise_after, BaseException):
            raise raise_after
        return _signed(status)

    def _with_buffer(
        self, name: str, raw: int, iosb: Any, buffer: Any, length: int, **more: object
    ) -> int:
        step = self._step(name, dict(raw=raw, length=length, iosb=iosb._obj, **more))
        data = bytes(step.get("data", b""))  # type: ignore[call-overload]
        target = buffer._obj if hasattr(buffer, "_obj") else buffer
        ctypes.memmove(ctypes.addressof(target), data, min(len(data), length))
        return self._finish(step, iosb._obj, len(data))

    def NtCreateFile(
        self, out: Any, access: int, attributes: Any, iosb: Any, alloc: object, attrs: int,
        share: int, disposition: int, options: int, ea: object, ea_length: int,
    ) -> int:  # fmt: skip
        oa = attributes._obj
        uname = oa.ObjectName.contents
        name = ctypes.wstring_at(uname.Buffer, uname.Length // 2) if uname.Buffer else ""
        details = dict(
            access=access, share=share, disposition=disposition, options=options,
            attributes=attrs, name=name, root=oa.RootDirectory, iosb=iosb._obj, out=out._obj,
        )  # fmt: skip
        step = self._step("NtCreateFile", details)
        status = int(step.get("status", 0))  # type: ignore[call-overload]
        value = step.get("out", "pool" if status == 0 else 0)
        out._obj.value = self.pool.take() if value == "pool" else value
        return self._finish(step, iosb._obj, 2 if disposition == 2 else 1)

    def NtSetInformationFile(
        self, raw: int, iosb: Any, info: Any, length: int, info_class: int
    ) -> int:
        data = bytes(info._obj)[:length] if hasattr(info, "_obj") else bytes(info)[:length]
        details = dict(raw=raw, info_class=info_class, data=data, length=length, iosb=iosb._obj)
        return self._finish(self._step("NtSetInformationFile", details), iosb._obj, 0)

    def NtQueryDirectoryFile(
        self, raw: int, event: object, apc: object, context: object, iosb: Any, buffer: Any,
        length: int, info_class: int, single: bool, name: Any, restart: bool,
    ) -> int:  # fmt: skip
        uname = name._obj
        text = ctypes.wstring_at(uname.Buffer, uname.Length // 2) if uname.Buffer else ""
        return self._with_buffer(
            "NtQueryDirectoryFile", raw, iosb, buffer, length,
            info_class=info_class, single=single, pattern=text, restart=restart, event=event,
        )  # fmt: skip

    def NtReadFile(
        self, raw: int, event: object, apc: object, context: object, iosb: Any, buffer: Any,
        length: int, offset: Any, key: object,
    ) -> int:  # fmt: skip
        return self._with_buffer("NtReadFile", raw, iosb, buffer, length, offset=offset._obj.value)

    def NtWriteFile(
        self, raw: int, event: object, apc: object, context: object, iosb: Any, buffer: Any,
        length: int, offset: Any, key: object,
    ) -> int:  # fmt: skip
        details = dict(raw=raw, data=bytes(buffer)[:length], offset=offset._obj.value)
        return self._finish(self._step("NtWriteFile", details), iosb._obj, length)

    def NtFlushBuffersFile(self, raw: int, iosb: Any) -> int:
        return self._finish(self._step("NtFlushBuffersFile", dict(raw=raw)), iosb._obj, 0)

    def NtQueryInformationFile(
        self, raw: int, iosb: Any, buffer: Any, length: int, info_class: int
    ) -> int:
        return self._with_buffer(
            "NtQueryInformationFile", raw, iosb, buffer, length, info_class=info_class
        )

    def NtQueryVolumeInformationFile(
        self, raw: int, iosb: Any, buffer: Any, length: int, info_class: int
    ) -> int:
        return self._with_buffer(
            "NtQueryVolumeInformationFile", raw, iosb, buffer, length, info_class=info_class
        )


class FakeNt:
    """``winprims`` with a scripted ntdll over a ``FakePool`` (the F tests)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, pool: FakePool) -> None:
        import types

        from downloads_organizer import winprims

        self.pool = pool
        self.ntdll = FakeNtdll(pool)
        self.prims = types.SimpleNamespace(**vars(winprims._prims()))
        self.prims.ntdll = self.ntdll
        monkeypatch.setattr(winprims, "_prims", lambda: self.prims)


@pytest.fixture
def fake_nt(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FakeNt, Acquisitions]]:
    """A scripted ntdll and the §9.1 stand over a fake pool that reuses values at once."""
    pool = FakePool()
    stand = Acquisitions(monkeypatch, pool=pool)
    nt = FakeNt(monkeypatch, pool)
    yield nt, stand
    problems = stand.finish()
    if problems:
        pytest.fail(f"ownership stand: {problems}")
