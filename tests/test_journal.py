from __future__ import annotations

import dataclasses
import io
import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from downloads_organizer import journal as journal_module
from downloads_organizer import winfileops
from downloads_organizer.journal import (
    FIELDS,
    SCHEMA_VERSION,
    Journal,
    JournalCloseError,
    JournalEntry,
    JournalError,
    JournalProblem,
    JournalReport,
    Operation,
    OperationState,
    Status,
    analyze,
    default_log_dir,
    read_journal,
)


def lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class FailingStream(io.BytesIO):
    """Accepts ``ok`` writes, then fails every write."""

    def __init__(self, ok: int, error: Exception | None = None) -> None:
        super().__init__()
        self.ok = ok
        self.error = error or OSError(28, "No space left on device")

    def write(self, data: object) -> int:  # type: ignore[override]
        if self.ok <= 0:
            raise self.error
        self.ok -= 1
        return super().write(data)  # type: ignore[arg-type]

    def fileno(self) -> int:
        raise io.UnsupportedOperation("no fd")


def test_default_log_dir_is_under_localappdata(isolated_user_dirs: Path) -> None:
    expected = Path(os.environ["LOCALAPPDATA"]) / "DownloadsOrganizer" / "logs"
    assert default_log_dir() == expected
    assert str(expected).startswith(str(isolated_user_dirs))


@pytest.mark.parametrize("environ", [{}, {"LOCALAPPDATA": ""}, {"LOCALAPPDATA": "relative"}])
def test_default_log_dir_requires_absolute_localappdata(environ: dict[str, str]) -> None:
    with pytest.raises(JournalError, match="LOCALAPPDATA"):
        default_log_dir(environ)


def test_create_writes_json_lines_with_all_fields(isolated_user_dirs: Path) -> None:
    with Journal.create() as log:
        assert log.path is not None
        assert log.path.parent == default_log_dir()
        assert log.path.name.startswith("run-") and log.path.name.endswith(".jsonl")
        assert log.run_id in log.path.name
        assert log.write(Operation.RUN, Status.STARTED, source=Path("C:/src")) == 1
        seq = log.write(
            Operation.MOVE, Status.INTENT, source=Path("C:/src/ä.pdf"), target=Path("C:/t")
        )
        assert log.write(Operation.MOVE, Status.DONE, intent=seq, reason="ok") == 3
        path = log.path
    records = lines(path)
    assert [tuple(r) for r in records] == [FIELDS] * 3
    assert [r["sequence"] for r in records] == [1, 2, 3]
    assert {r["schema_version"] for r in records} == {SCHEMA_VERSION}
    assert {r["run_id"] for r in records} == {log.run_id}
    assert records[1]["source"] == str(Path("C:/src/ä.pdf"))
    assert records[2]["intent"] == 2
    stamp = datetime.fromisoformat(str(records[0]["timestamp_utc"]))
    assert stamp.utcoffset() is not None and stamp.utcoffset().total_seconds() == 0


def test_every_record_is_flushed_and_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[int] = []
    monkeypatch.setattr(journal_module.os, "fsync", synced.append)
    with Journal.create(tmp_path / "logs") as log:
        assert log.path is not None
        log.write(Operation.RUN, Status.STARTED)
        assert len(synced) == 1
        assert len(lines(log.path)) == 1  # readable before close
        log.write(Operation.RUN, Status.FINISHED)
        assert len(synced) == 2


def test_create_never_reuses_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(journal_module, "_utc_now", lambda: fixed)
    monkeypatch.setattr(journal_module.uuid, "uuid4", lambda: type("U", (), {"hex": "same"})())
    first = Journal.create(tmp_path)
    first.write(Operation.RUN, Status.STARTED)
    first.close()
    content = first.path.read_bytes() if first.path else b""
    with pytest.raises(JournalError, match="cannot create journal"):
        Journal.create(tmp_path)
    assert first.path is not None and first.path.read_bytes() == content


def test_create_failure_is_journal_error(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    base = outer / "base"
    base.mkdir(parents=True)
    blocker = base / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(JournalError):
        Journal.create(blocker / "logs")
    assert blocker.read_text(encoding="utf-8") == "not a directory"
    # NTFS refuses to rename a directory with an open handle anywhere below it, even
    # one shared for delete such as the walk's handle of ``base``.
    outer.rename(tmp_path / "moved")  # no handle below it was left open


def test_create_requires_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winfileops, "AVAILABLE", False)
    with pytest.raises(JournalError, match="only supported on Windows"):
        Journal.create(tmp_path / "logs")
    assert not (tmp_path / "logs").exists()


@pytest.mark.windows_only
def test_create_refuses_a_junction_in_the_log_path(tmp_path: Path, make_junction) -> None:
    real = tmp_path / "real"
    real.mkdir()
    make_junction(tmp_path / "link", real)
    with pytest.raises(JournalError, match="link or junction"):
        Journal.create(tmp_path / "link" / "logs")
    assert not (real / "logs").exists()  # refused before anything was created below it
    assert list(real.iterdir()) == []
    (tmp_path / "link").rename(tmp_path / "link2")  # handles released


@pytest.mark.windows_only
def test_create_refuses_a_junction_as_the_log_directory(tmp_path: Path, make_junction) -> None:
    real = tmp_path / "real"
    real.mkdir()
    make_junction(tmp_path / "logs", real)
    with pytest.raises(JournalError, match="link or junction"):
        Journal.create(tmp_path / "logs")
    assert list(real.iterdir()) == []


@pytest.mark.windows_only
def test_create_refuses_a_junction_in_the_default_path(
    isolated_user_dirs: Path, make_junction
) -> None:
    real = isolated_user_dirs / "real"
    real.mkdir()
    make_junction(Path(os.environ["LOCALAPPDATA"]) / "DownloadsOrganizer", real)
    with pytest.raises(JournalError, match="link or junction"):
        Journal.create()
    assert list(real.iterdir()) == []


@pytest.mark.windows_only
def test_create_does_not_create_a_missing_localappdata(isolated_user_dirs: Path) -> None:
    local = Path(os.environ["LOCALAPPDATA"])
    local.rmdir()
    with pytest.raises(JournalError):
        Journal.create()
    assert not local.exists()


@pytest.mark.windows_only
def test_create_refuses_a_directory_swapped_for_a_junction_during_the_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_junction
) -> None:
    logs, elsewhere = tmp_path / "logs", tmp_path / "elsewhere"
    logs.mkdir()
    elsewhere.mkdir()
    real_open = winfileops.open_relative

    def swap(parent: winfileops.Handle, name: str, **kwargs: object):
        if name == "logs" and not logs.is_junction():  # after the parent was checked
            logs.rmdir()
            make_junction(logs, elsewhere)
        return real_open(parent, name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(winfileops, "open_relative", swap)
    with pytest.raises(JournalError, match="link or junction"):
        Journal.create(logs)
    assert list(elsewhere.iterdir()) == []


@pytest.mark.windows_only
@pytest.mark.parametrize("racer", ["directory", "junction"])
def test_create_checks_a_directory_created_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_junction, spy: HandleSpy, racer: str
) -> None:
    logs, elsewhere = tmp_path / "logs", tmp_path / "elsewhere"
    elsewhere.mkdir()
    real_open = winfileops.open_relative
    raced: list[str] = []

    def race(parent: winfileops.Handle, name: str, **kwargs: object):
        if kwargs.get("create") and not raced:  # someone else is quicker
            raced.append(name)
            if racer == "directory":
                logs.mkdir()
            else:
                make_junction(logs, elsewhere)
        return real_open(parent, name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(winfileops, "open_relative", race)
    if racer == "directory":  # the existing directory is checked and used
        with Journal.create(logs) as log:
            assert log.path is not None and log.path.parent == logs
        assert [p.name for p in logs.iterdir()] == [log.path.name]
    else:
        with pytest.raises(JournalError, match="link or junction"):
            Journal.create(logs)
        assert list(elsewhere.iterdir()) == []
    assert raced == ["logs"]
    spy.assert_released()


@pytest.mark.windows_only
def test_log_directory_is_held_from_creation_until_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swapping the directory between the check and the file creation is impossible."""
    logs = tmp_path / "logs"
    real_create = journal_module._create_log_file
    attempts: list[BaseException] = []

    def swap_then_create(chain: journal_module._DirectoryChain, path: Path):
        try:
            logs.rename(tmp_path / "swapped")
        except OSError as exc:
            attempts.append(exc)
        return real_create(chain, path)

    monkeypatch.setattr(journal_module, "_create_log_file", swap_then_create)
    with Journal.create(logs) as log:
        assert len(attempts) == 1 and isinstance(attempts[0], PermissionError)
        assert log.path is not None and log.path.parent == logs and log.path.is_file()
        with pytest.raises(PermissionError):
            logs.rename(tmp_path / "later")
    logs.rename(tmp_path / "later")  # released by close
    assert not (tmp_path / "swapped").exists()


class HandleSpy:
    """Every ``winfileops.Handle`` opened during a test and every ``CloseHandle`` call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.opened: list[tuple[winfileops.Handle, int]] = []
        self.detached: list[int] = []  # handed to the CRT, which closes them itself
        self.closed: list[int] = []
        spy = self
        real_init, real_detach = winfileops.Handle.__init__, winfileops.Handle.detach
        kernel32 = winfileops._api().kernel32
        real_close = kernel32.CloseHandle

        def init(handle: winfileops.Handle, raw: int, path: Path) -> None:
            real_init(handle, raw, path)
            spy.opened.append((handle, raw))

        def detach(handle: winfileops.Handle) -> int:
            raw = real_detach(handle)
            spy.detached.append(raw)
            return raw

        def close(raw: int) -> object:
            spy.closed.append(raw)
            return real_close(raw)

        monkeypatch.setattr(winfileops.Handle, "__init__", init)
        monkeypatch.setattr(winfileops.Handle, "detach", detach)
        monkeypatch.setattr(kernel32, "CloseHandle", close)

    def open_now(self) -> list[Path]:
        """Handles not confirmed released: open, or their ``CloseHandle`` failed."""
        released = (winfileops.HandleState.CLOSED, winfileops.HandleState.DETACHED)
        return [handle.path for handle, _ in self.opened if handle.state not in released]

    def assert_released(self) -> None:
        """No handle is left open, and each was closed exactly once (none twice)."""
        assert self.opened, "no handle was opened"
        assert self.open_now() == []
        owned = Counter(raw for _, raw in self.opened)
        owned.subtract(self.detached)
        assert Counter(self.closed) == +owned


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> HandleSpy:
    return HandleSpy(monkeypatch)


def tree(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def chain_of(directory: Path) -> list[Path]:
    """The volume root and every directory below it down to ``directory``."""
    return [Path(*directory.parts[:n]) for n in range(1, len(directory.parts) + 1)]


@pytest.mark.windows_only
@pytest.mark.parametrize("decoy", [False, True])
def test_an_ancestor_cannot_be_swapped_during_the_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_junction, spy: HandleSpy, decoy: bool
) -> None:
    """A checked ancestor stays held: it can be neither renamed nor replaced by a junction."""
    parent, elsewhere = tmp_path / "a", tmp_path / "elsewhere"
    parent.mkdir()
    elsewhere.mkdir()
    name = "run-20260101T000000000000Z-" + "d" * 32 + ".jsonl"
    if decoy:  # the substitute would even hold a file of that very name
        monkeypatch.setattr(journal_module, "_utc_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
        monkeypatch.setattr(journal_module.uuid, "uuid4", lambda: type("U", (), {"hex": "d" * 32}))
        (elsewhere / "logs").mkdir()
        (elsewhere / "logs" / name).write_bytes(b"decoy")
    before = tree(elsewhere)
    real_open = winfileops.open_relative
    attempts: list[OSError | None] = []

    def swap(handle: winfileops.Handle, name: str, **kwargs: object):
        if name == "logs" and not attempts:  # ``a`` was checked and is held
            try:
                parent.rename(tmp_path / "a_moved")
            except OSError as exc:
                attempts.append(exc)
            else:
                attempts.append(None)
                make_junction(parent, elsewhere)
        return real_open(handle, name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(winfileops, "open_relative", swap)
    with Journal.create(parent / "logs") as log:
        assert len(attempts) == 1 and isinstance(attempts[0], PermissionError), attempts
        assert log.path is not None and log.path.parent == parent / "logs"
        log.write(Operation.RUN, Status.STARTED)
    assert not (tmp_path / "a_moved").exists() and not parent.is_junction()
    assert tree(elsewhere) == before
    if decoy:
        assert (elsewhere / "logs" / name).read_bytes() == b"decoy"
    assert [p.name for p in (parent / "logs").iterdir()] == [log.path.name]
    assert [r["status"] for r in lines(log.path)] == ["started"]
    spy.assert_released()
    parent.rename(tmp_path / "a_back")


@pytest.mark.windows_only
@pytest.mark.parametrize("missing", [False, True], ids=["existing", "created"])
def test_the_whole_chain_is_held_from_the_walk_until_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy, missing: bool
) -> None:
    logs = tmp_path / "root" / "a" / "b" / "logs"
    (logs.parent if missing else logs).mkdir(parents=True)
    real_create = winfileops.create_file
    held_at_create: list[list[Path]] = []

    def create(parent: winfileops.Handle, name: str, **kwargs: object) -> winfileops.Handle:
        held_at_create.append(spy.open_now())
        return real_create(parent, name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(winfileops, "create_file", create)
    with Journal.create(logs) as log:
        log.write(Operation.RUN, Status.STARTED)
        assert spy.open_now() == chain_of(logs)  # held while writing
    assert held_at_create == [chain_of(logs)]
    spy.assert_released()
    (tmp_path / "root").rename(tmp_path / "moved")


_PHASES = ["walk", "create", "write", "before close"]
_ANCESTORS = ["root", "a", "b", "logs"]
_CHILD = {"root": "a", "a": "b", "b": "logs"}


@pytest.mark.windows_only
@pytest.mark.parametrize(
    ("ancestor", "phase"),
    [(a, p) for p in _PHASES for a in _ANCESTORS if not (p == "walk" and a == "logs")],
)
def test_no_directory_of_the_path_can_be_renamed_while_the_journal_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy, ancestor: str, phase: str
) -> None:
    root = tmp_path / "root"
    dirs = {"root": root, "a": root / "a", "b": root / "a" / "b", "logs": root / "a" / "b" / "logs"}
    dirs["b"].mkdir(parents=True)  # ``logs`` is created by the journal
    attempts: list[OSError | None] = []

    def attempt() -> None:
        if attempts:
            return
        victim = dirs[ancestor]
        try:
            victim.rename(victim.with_name(victim.name + "_moved"))
        except OSError as exc:
            attempts.append(exc)
        else:
            attempts.append(None)

    if phase == "walk":  # the ancestor is checked, its child is about to be opened
        real_open = winfileops.open_relative

        def open_relative(parent: winfileops.Handle, name: str, **kwargs: object):
            if name == _CHILD[ancestor]:
                attempt()
            return real_open(parent, name, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(winfileops, "open_relative", open_relative)
    elif phase == "create":
        real_create = winfileops.create_file

        def create(parent: winfileops.Handle, name: str, **kwargs: object) -> winfileops.Handle:
            attempt()
            return real_create(parent, name, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(winfileops, "create_file", create)
    elif phase == "write":
        real_fsync = journal_module.os.fsync

        def fsync(fd: int) -> None:
            attempt()
            real_fsync(fd)

        monkeypatch.setattr(journal_module.os, "fsync", fsync)
    with Journal.create(dirs["logs"]) as log:
        for data in move_journal():
            log.write(
                Operation(data["operation"]), Status(data["status"]),
                intent=data["intent"],  # type: ignore[arg-type]
                source=None if data["source"] is None else Path(str(data["source"])),
                target=None if data["target"] is None else Path(str(data["target"])),
            )  # fmt: skip
        if phase == "before close":
            attempt()
    assert len(attempts) == 1 and isinstance(attempts[0], PermissionError), attempts
    assert not [p for p in tmp_path.rglob("*_moved")]
    assert log.path is not None and [p.name for p in dirs["logs"].iterdir()] == [log.path.name]
    report = read_journal(log.path)  # written where it was checked, and intact
    assert [e.state for e in report.entries] == [OperationState.DONE]
    assert report.finished and not report.needs_review
    spy.assert_released()
    dirs[ancestor].rename(dirs[ancestor].with_name("released"))


@pytest.mark.windows_only
@pytest.mark.parametrize("where", ["start", "middle", "end"])
def test_create_refuses_a_junction_anywhere_in_the_path(
    tmp_path: Path, make_junction, spy: HandleSpy, where: str
) -> None:
    real = tmp_path / "real"
    (real / "a" / "b" / "logs").mkdir(parents=True)  # what the junction would lead into
    root = tmp_path / "root"
    if where == "start":
        make_junction(root, real)
    elif where == "middle":
        root.mkdir()
        make_junction(root / "a", real / "a")
    else:
        (root / "a" / "b").mkdir(parents=True)
        make_junction(root / "a" / "b" / "logs", real / "a" / "b" / "logs")
    before = tree(real)
    with pytest.raises(JournalError, match="link or junction"):
        Journal.create(root / "a" / "b" / "logs")
    assert tree(real) == before  # nothing was created in the substituted directory
    spy.assert_released()


VOLUMES = {
    "FAT32": winfileops.VolumeInfo("FAT32", winfileops.FILE_DEVICE_DISK, 0),
    "exFAT removable": winfileops.VolumeInfo(
        "exFAT", winfileops.FILE_DEVICE_DISK, winfileops.FILE_REMOVABLE_MEDIA
    ),
    "NTFS removable": winfileops.VolumeInfo(
        "NTFS", winfileops.FILE_DEVICE_DISK, winfileops.FILE_REMOVABLE_MEDIA
    ),
    "NTFS remote": winfileops.VolumeInfo(
        "NTFS", winfileops.FILE_DEVICE_DISK, winfileops.FILE_REMOTE_DEVICE
    ),
    "network filesystem": winfileops.VolumeInfo("NTFS", 0x14, 0),
    "ReFS": winfileops.VolumeInfo("ReFS", winfileops.FILE_DEVICE_DISK, 0),
    "no name": winfileops.VolumeInfo("", winfileops.FILE_DEVICE_DISK, 0),
}


@pytest.mark.windows_only
@pytest.mark.parametrize("volume", VOLUMES.values(), ids=VOLUMES.keys())
def test_create_refuses_a_volume_it_was_not_verified_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy, volume: object
) -> None:
    monkeypatch.setattr(winfileops, "query_volume", lambda handle: volume)
    with pytest.raises(JournalError, match="only a local NTFS disk is supported"):
        Journal.create(tmp_path / "logs")
    assert not (tmp_path / "logs").exists()
    spy.assert_released()


@pytest.mark.windows_only
def test_create_refuses_when_the_volume_cannot_be_queried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy
) -> None:
    def broken(handle: winfileops.Handle) -> winfileops.VolumeInfo:
        raise OSError(1, "Incorrect function")

    monkeypatch.setattr(winfileops, "query_volume", broken)
    with pytest.raises(JournalError, match="Incorrect function"):
        Journal.create(tmp_path / "logs")
    assert not (tmp_path / "logs").exists()
    spy.assert_released()


def _fail_on_call(
    monkeypatch: pytest.MonkeyPatch, owner: object, name: str, number: int
) -> list[int]:
    """Make call ``number`` (1-based) of ``owner.name`` fail; returns the call counter."""
    real, calls = getattr(owner, name), [0]

    def failing(*args: object, **kwargs: object) -> object:
        calls[0] += 1
        if calls[0] == number:
            raise OSError(5, "Access is denied")
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, failing)
    return calls


@pytest.mark.windows_only
@pytest.mark.parametrize("which", ["second", "middle", "last"])
@pytest.mark.parametrize("function", ["open_relative", "query"])
def test_a_failure_at_any_component_releases_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy, function: str, which: str
) -> None:
    logs = tmp_path / "root" / "a" / "logs"
    logs.mkdir(parents=True)
    components = len(logs.parts) - 1  # opened below the volume root, one call each
    number = {"second": 1, "middle": components // 2, "last": components}[which]
    number += function == "query"  # the volume root is queried first
    calls = _fail_on_call(monkeypatch, winfileops, function, number)
    with pytest.raises(JournalError, match="Access is denied"):
        Journal.create(logs)
    assert calls[0] == number  # it stopped at the failure
    assert list(logs.iterdir()) == []
    spy.assert_released()
    (tmp_path / "root").rename(tmp_path / "moved")


@pytest.mark.windows_only
def test_a_failing_check_of_the_new_file_releases_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    # The root and every directory below it are queried first, then the new file.
    _fail_on_call(monkeypatch, winfileops, "query", len(logs.parts) + 1)
    with pytest.raises(JournalError, match="Access is denied"):
        Journal.create(logs)
    spy.assert_released()
    # Residual: the new file stays, empty, where it was created through the held handle.
    (left,) = logs.iterdir()
    assert left.stat().st_size == 0
    left.unlink()  # its handle was closed
    logs.rename(tmp_path / "moved")


@pytest.mark.windows_only
def test_create_refuses_a_name_that_does_not_lead_to_the_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy
) -> None:
    """The held chain rules this out; the check behind it is enforced all the same."""
    logs = tmp_path / "logs"
    logs.mkdir()
    real = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda p: real(logs) if p.parent == logs else real(p))
    with pytest.raises(JournalError, match="does not lead to the new file"):
        Journal.create(logs)
    spy.assert_released()
    logs.rename(tmp_path / "moved")


@pytest.mark.windows_only
def test_create_closes_the_directory_when_the_file_cannot_be_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy
) -> None:
    logs = tmp_path / "logs"

    def broken(*args: object, **kwargs: object) -> winfileops.Handle:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(winfileops, "create_file", broken)
    with pytest.raises(JournalError, match="No space"):
        Journal.create(logs)
    spy.assert_released()
    logs.rename(tmp_path / "moved")  # not held any more


@pytest.mark.windows_only
def test_create_closes_the_file_when_the_stream_cannot_be_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy
) -> None:
    import msvcrt

    logs = tmp_path / "logs"

    def broken(*args: object) -> int:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(msvcrt, "open_osfhandle", broken)
    with pytest.raises(JournalError, match="Too many open files"):
        Journal.create(logs)
    spy.assert_released()
    for path in logs.iterdir():
        path.unlink()  # the file handle was closed
    logs.rename(tmp_path / "moved")


@pytest.mark.windows_only
def test_create_closes_the_descriptor_when_the_file_object_cannot_be_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy
) -> None:
    logs = tmp_path / "logs"

    def broken(*args: object, **kwargs: object) -> object:
        raise OSError(12, "Not enough memory")

    monkeypatch.setattr(winfileops, "FileIO", broken)
    with pytest.raises(JournalError, match="Not enough memory"):
        Journal.create(logs)
    spy.assert_released()
    for path in logs.iterdir():
        path.unlink()  # the descriptor, which owned the file handle, was closed
    logs.rename(tmp_path / "moved")


@pytest.mark.windows_only
def test_close_releases_the_chain_even_if_the_file_cannot_be_closed(
    tmp_path: Path, spy: HandleSpy
) -> None:
    log = Journal.create(tmp_path / "logs")
    log.write(Operation.RUN, Status.STARTED)
    real = log._stream
    assert real is not None

    class Unclosable:
        def close(self) -> None:
            real.close()
            raise OSError(5, "I/O error")

    log._stream = Unclosable()  # type: ignore[assignment]
    with pytest.raises(JournalCloseError, match="I/O error") as raised:
        log.close()
    assert [type(e) for e in raised.value.errors] == [OSError]
    spy.assert_released()
    with pytest.raises(JournalCloseError) as again:
        log.close()  # the same error again; nothing left to close, nothing closed twice
    assert again.value is raised.value
    spy.assert_released()
    (tmp_path / "logs").rename(tmp_path / "moved")


def test_closing_the_chain_closes_every_handle_even_if_one_fails() -> None:
    closed: list[str] = []

    class Fake:
        def __init__(self, name: str, error: Exception | None = None) -> None:
            self.name, self.error = name, error

        def close(self) -> None:
            closed.append(self.name)
            if self.error is not None:
                raise self.error

    chain = journal_module._DirectoryChain()
    for fake in (Fake("root"), Fake("a", OSError(6, "invalid handle")), Fake("b"), Fake("c")):
        chain.push(fake)  # type: ignore[arg-type]
    with pytest.raises(JournalCloseError, match="invalid handle") as raised:
        chain.close()
    assert closed == ["c", "b", "a", "root"]  # innermost first, none skipped
    assert [str(e) for e in raised.value.errors] == ["[Errno 6] invalid handle"]
    chain.close()
    assert closed == ["c", "b", "a", "root"]  # the chain gave them up: no second close


@pytest.mark.parametrize(
    "error", [OSError(28, "No space left on device"), ValueError("I/O on closed file")]
)
def test_write_failure_poisons_the_journal(error: Exception) -> None:
    log = Journal(FailingStream(ok=1, error=error), run_id="r")
    log._stream.fileno = lambda: 0  # type: ignore[union-attr, method-assign]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(journal_module.os, "fsync", lambda fd: None)
        assert log.write(Operation.RUN, Status.STARTED) == 1
        with pytest.raises(JournalError, match="record 2"):
            log.write(Operation.MOVE, Status.INTENT)
        assert log.failed
        with pytest.raises(JournalError, match="unusable"):
            log.write(Operation.MOVE, Status.INTENT)


def test_fsync_failure_poisons_the_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(fd: int) -> None:
        raise OSError(5, "I/O error")

    log = Journal.create(tmp_path)
    monkeypatch.setattr(journal_module.os, "fsync", broken)
    with pytest.raises(JournalError):
        log.write(Operation.RUN, Status.STARTED)
    assert log.failed
    log.close()


def test_short_write_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Short(io.BytesIO):
        def write(self, data: object) -> int:  # type: ignore[override]
            return 1

    monkeypatch.setattr(journal_module.os, "fsync", lambda fd: None)
    stream = Short()
    stream.fileno = lambda: 0  # type: ignore[method-assign]
    log = Journal(stream)
    with pytest.raises(JournalError, match="short write"):
        log.write(Operation.RUN, Status.STARTED)


def test_closed_journal_refuses_writes(tmp_path: Path) -> None:
    log = Journal.create(tmp_path)
    log.close()
    log.close()
    with pytest.raises(JournalError, match="closed"):
        log.write(Operation.RUN, Status.STARTED)


def test_unknown_operation_or_status_is_refused(tmp_path: Path) -> None:
    with Journal.create(tmp_path) as log:
        with pytest.raises(JournalError):
            log.write("format_disk", Status.DONE)  # type: ignore[arg-type]
        assert log.failed


def test_lone_surrogates_in_names_round_trip(tmp_path: Path) -> None:
    source = Path("C:/x/\udcff.pdf")
    with Journal.create(tmp_path) as log:
        log.write(Operation.RUN, Status.STARTED)
        log.write(Operation.MOVE, Status.INTENT, source=source, target=Path("C:/t/x.pdf"))
        path = log.path
    assert path is not None
    assert b"\\udcff" in path.read_bytes()  # stored as a JSON escape, file stays UTF-8
    report = read_journal(path)
    assert [str(p) for p in report.problems] == []
    assert report.records[1]["source"] == str(source)  # type: ignore[index]


# --- reading: the DONE trust model ------------------------------------------------------

STAMP = "2026-01-01T00:00:00.000000+00:00"
SRC, DST = "C:\\in\\a.pdf", "C:\\out\\PDF\\a.pdf"


def record(
    seq: object,
    op: object,
    status: object,
    intent: object = None,
    *,
    source: object = None,
    target: object = None,
    **over: object,
) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": "r",
        "sequence": seq,
        "timestamp_utc": STAMP,
        "operation": op,
        "status": status,
        "intent": intent,
        "source": source,
        "target": target,
        "reason": None,
        "error_code": None,
        "error_message": None,
    }
    data.update(over)
    assert tuple(data) == FIELDS
    return data


def move_journal() -> list[dict[str, object]]:
    """A complete, valid run with one confirmed move."""
    return [
        record(1, "run", "started", source="C:\\in", target="C:\\out"),
        record(2, "move", "intent", source=SRC, target=DST),
        record(3, "move", "done", 2, source=SRC, target=DST, reason="moved"),
        record(4, "run", "finished", reason="complete"),
    ]


def moves(report: JournalReport) -> list[JournalEntry]:
    return [e for e in report.entries if e.operation == "move"]


def assert_not_done(report: JournalReport) -> None:
    """Fail-closed: no DONE, the operation stays listed for manual review."""
    entries = moves(report)
    assert entries, "the operation disappeared from the report"
    assert all(e.state is not OperationState.DONE for e in entries)
    assert all(e in report.recovery_needed for e in entries)
    assert report.problems and report.needs_review


def test_a_valid_journal_confirms_the_move() -> None:
    report = analyze(move_journal())
    (entry,) = report.entries
    assert entry.state is OperationState.DONE
    assert (entry.source, entry.target, entry.attempted) == (SRC, DST, True)
    assert entry.outcome is not None and entry.outcome["sequence"] == 3
    assert report.finished and not report.problems and not report.review_whole_journal
    assert not report.needs_review


def test_analyze_distinguishes_every_state() -> None:
    def move(seq: int, status: str, name: str, intent: int | None = None) -> dict[str, object]:
        return record(seq, "move", status, intent, source=name, target=f"t\\{name}")

    report = analyze(
        [
            record(1, "run", "started"),
            move(2, "skipped", "planned-skip"),
            record(3, "mkdir", "intent", target="dir"),
            record(4, "mkdir", "done", 3, target="dir"),
            move(5, "intent", "moved"),
            move(6, "done", "moved", 5),
            move(7, "intent", "refused"),
            move(8, "skipped", "refused", 7),
            move(9, "intent", "failed"),
            move(10, "failed", "failed", 9),
            move(11, "intent", "unclear"),
            move(12, "recovery_required", "unclear", 11),
            move(13, "cancelled", "cancelled"),
            record(14, "move", "failed", source="no-target", target=None),
            move(15, "intent", "crashed"),
        ]
    )
    states = [(e.source or e.target, e.state, e.attempted) for e in report.entries]
    assert states == [
        ("planned-skip", OperationState.SKIPPED, False),
        ("dir", OperationState.DONE, True),
        ("moved", OperationState.DONE, True),
        ("refused", OperationState.SKIPPED, True),
        ("failed", OperationState.FAILED, True),
        ("unclear", OperationState.RECOVERY_REQUIRED, True),
        ("cancelled", OperationState.CANCELLED, False),
        ("no-target", OperationState.FAILED, False),
        ("crashed", OperationState.UNCERTAIN, True),
    ]
    assert not report.finished
    assert not report.problems and not report.review_whole_journal
    assert [e.source for e in report.uncertain] == ["crashed"]
    assert [e.source for e in report.recovery_needed] == ["unclear", "crashed"]


# Each case changes the DONE record (index 2) so that it must not confirm the move.
FALSE_DONE = {
    "foreign run_id": {"run_id": "other"},
    "mkdir answers a move": {"operation": "mkdir", "source": None},
    "operation {}": {"operation": {}},
    "operation []": {"operation": []},
    "unknown operation": {"operation": "copy"},
    "status {}": {"status": {}},
    "status []": {"status": []},
    "unknown status": {"status": "exploded"},
    "other source": {"source": "C:\\in\\b.pdf"},
    "other target": {"target": "C:\\out\\PDF\\b.pdf"},
    "missing target": {"target": None},
    "source differs in case": {"source": SRC.upper()},
    "schema 2": {"schema_version": 2},
    "schema '1'": {"schema_version": "1"},
    "schema True": {"schema_version": True},
    "no intent": {"intent": None},
    "intent True": {"intent": True},
    "intent []": {"intent": []},
    "intent {}": {"intent": {}},
    "intent 0": {"intent": 0},
    "intent -2": {"intent": -2},
    "intent '2'": {"intent": "2"},
    "intent of the run record": {"intent": 1},
    "intent of itself": {"intent": 3},
    "intent that does not exist": {"intent": 99},
    "sequence None": {"sequence": None},
    "sequence True": {"sequence": True},
    "sequence False": {"sequence": False},
    "sequence 0": {"sequence": 0},
    "sequence -3": {"sequence": -3},
    "sequence '3'": {"sequence": "3"},
    "sequence {}": {"sequence": {}},
    "sequence 3.0": {"sequence": 3.0},
    "sequence of the intent": {"sequence": 2},
    "sequence gap": {"sequence": 7},
    "timestamp not UTC": {"timestamp_utc": "2026-01-01T03:00:00+03:00"},
    "timestamp naive": {"timestamp_utc": "2026-01-01T00:00:00"},
    "timestamp garbage": {"timestamp_utc": "yesterday"},
    "error_code text": {"error_code": "5"},
    "reason not text": {"reason": ["moved"]},
}


@pytest.mark.parametrize("change", FALSE_DONE.values(), ids=FALSE_DONE.keys())
def test_a_suspicious_outcome_never_confirms_a_move(change: dict[str, object]) -> None:
    records = move_journal()
    records[2] = {**records[2], **change}
    assert_not_done(analyze(records))


@pytest.mark.parametrize(
    "change",
    [{"run_id": "other"}, {"source": None}, {"target": None}, {"schema_version": 0},
     {"intent": 1}, {"operation": "mkdir"}, {"status": "Intent"}, {"sequence": True}],
    ids=["foreign run_id", "no source", "no target", "schema 0", "intent refers",
         "mkdir intent", "status case", "sequence True"],
)  # fmt: skip
def test_a_suspicious_intent_is_never_confirmed(change: dict[str, object]) -> None:
    records = move_journal()
    records[1] = {**records[1], **change}
    report = analyze(records)
    assert all(e.state is not OperationState.DONE for e in report.entries)
    assert report.recovery_needed and report.problems
    assert any(p.line == 3 for p in report.problems)  # the DONE is named as not answering


def test_missing_and_unknown_fields_never_confirm() -> None:
    for change in ("missing", "unknown"):
        records = move_journal()
        if change == "missing":
            del records[2]["target"]
        else:
            records[2]["extra"] = 1
        assert_not_done(analyze(records))


@pytest.mark.parametrize("bad", [[], "done", 3, None, True])
def test_an_outcome_that_is_no_object_leaves_the_move_open(bad: object) -> None:
    records: list[object] = list(move_journal())
    records[2] = bad
    report = analyze(records)
    assert_not_done(report)
    assert report.review_whole_journal  # it cannot be bound to an operation


def test_done_without_an_intent_is_recovery_required() -> None:
    records = move_journal()
    del records[1]
    for number, data in enumerate(records, start=1):
        data["sequence"] = number
    records[1]["intent"] = None
    report = analyze(records)
    assert_not_done(report)
    assert moves(report)[0].state is OperationState.RECOVERY_REQUIRED
    assert moves(report)[0].attempted is False


def test_done_before_its_intent_is_not_trusted() -> None:
    records = [
        record(1, "run", "started"),
        record(2, "move", "done", 3, source=SRC, target=DST),
        record(3, "move", "intent", source=SRC, target=DST),
        record(4, "run", "finished"),
    ]
    assert_not_done(analyze(records))


def test_duplicate_intent_sequence_confirms_neither() -> None:
    records = [
        record(1, "run", "started"),
        record(2, "move", "intent", source=SRC, target=DST),
        record(2, "move", "intent", source="C:\\in\\b.pdf", target="C:\\out\\b.pdf"),
        record(3, "move", "done", 2, source=SRC, target=DST),
        record(4, "run", "finished"),
    ]
    report = analyze(records)
    assert_not_done(report)
    entries = moves(report)
    # The second intent neither overwrites nor hides the first: both stay listed.
    assert [e.source for e in entries] == [SRC, "C:\\in\\b.pdf"]
    assert all(e.state is OperationState.RECOVERY_REQUIRED for e in entries)
    assert all(e.conflicts for e in entries)


@pytest.mark.parametrize("reused", [2, 3], ids=["intent", "done"])
def test_a_sequence_reused_later_spoils_the_first_record(reused: int) -> None:
    """Sequences must be unique: a later copy also discredits the earlier record."""
    records = move_journal()
    other = record(reused, "move", "skipped", source="C:\\in\\b.pdf", reason="excluded")
    records.insert(3, other)
    report = analyze(records)
    (entry,) = [e for e in moves(report) if e.source == SRC]
    assert entry.state is OperationState.RECOVERY_REQUIRED
    assert any(p.line == reused and "used by 2 records" in p.message for p in report.problems)
    assert report.needs_review and report.review_whole_journal


def test_two_answers_to_one_intent_confirm_nothing() -> None:
    records = move_journal()
    second = {**records[2], "sequence": 4}
    records[3]["sequence"] = 5
    records.insert(3, second)
    report = analyze(records)
    assert_not_done(report)
    assert len(moves(report)[0].conflicts) == 2


def test_a_conflicting_second_answer_spoils_the_first() -> None:
    records = move_journal()
    late = record(4, "move", "recovery_required", 2, source=SRC, target=DST)
    records[3]["sequence"] = 5
    records.insert(3, late)
    assert_not_done(analyze(records))


def test_a_broken_record_referring_to_the_intent_spoils_it() -> None:
    records = move_journal()
    broken = record(4, {}, [], 2, source=SRC, target=DST)
    records[3]["sequence"] = 5
    records.insert(3, broken)
    assert_not_done(analyze(records))


def test_unsupported_schema_confirms_nothing() -> None:
    records = [{**r, "schema_version": 2} for r in move_journal()]
    report = analyze(records)
    assert_not_done(report)
    assert not report.finished and report.review_whole_journal


def test_journal_of_another_run_confirms_nothing() -> None:
    records = move_journal()
    report = analyze(records, run_id="expected")
    assert_not_done(report)
    assert not report.finished


@pytest.mark.parametrize(
    "change",
    [{"run_id": "other"}, {"schema_version": 9}, {"status": "done"}, {"intent": 2},
     {"sequence": 9}],
    ids=["foreign run_id", "schema", "wrong status", "refers to intent", "sequence gap"],
)  # fmt: skip
def test_only_a_valid_final_record_counts_as_finished(change: dict[str, object]) -> None:
    records = move_journal()
    records[3] = {**records[3], **change}
    report = analyze(records)
    assert not report.finished
    assert report.review_whole_journal and report.needs_review


def test_records_after_the_final_record_are_not_trusted() -> None:
    records = [
        record(1, "run", "started"),
        record(2, "move", "intent", source=SRC, target=DST),
        record(3, "run", "finished"),
        record(4, "move", "done", 2, source=SRC, target=DST),
    ]
    report = analyze(records)
    assert_not_done(report)
    assert report.review_whole_journal


def test_a_missing_start_needs_whole_journal_review() -> None:
    report = analyze(move_journal()[1:])
    assert report.review_whole_journal
    assert any("run/started" in str(p) for p in report.problems)


def test_problem_text_names_the_line() -> None:
    records = move_journal()
    records[2]["run_id"] = "other"
    texts = [str(p) for p in analyze(records).problems]
    assert any(t.startswith("line 3: ") and "run_id 'other'" in t for t in texts), texts
    assert str(JournalProblem(None, "whole journal")) == "whole journal"


BAD_VALUES: list[object] = [
    None, True, False, 0, -1, 1.5, 2**70, "", "x", "done", {}, [], {"a": 1}, ["move"],
]  # fmt: skip


def harmless(index: int, name: str, value: object, original: object) -> bool:
    """A change that leaves the record valid and equal in everything that counts."""
    if type(value) is type(original) and value == original:
        return True
    if name in ("reason", "error_message"):
        return value is None or isinstance(value, str)
    if name == "error_code":
        return value is None or type(value) is int
    if index in (0, 3) and name in ("source", "target"):  # free text in run records
        return value is None or (isinstance(value, str) and value != "")
    return False


def test_any_corrupted_field_is_analyzed_without_crashing() -> None:
    """Every field of every record, every wrong type: no exception, no false DONE."""
    for index in range(4):
        for name in FIELDS:
            for value in BAD_VALUES:
                records = move_journal()
                original = records[index][name]
                records[index][name] = value
                report = analyze(records)  # must not raise
                label = f"record {index + 1}, {name}={value!r}"
                if harmless(index, name, value, original):
                    continue
                assert report.problems, label
                if index in (1, 2):
                    assert all(e.state is not OperationState.DONE for e in moves(report)), label
                    assert moves(report) and report.recovery_needed, label
                if index == 3:
                    assert not report.finished, label


def write_lines(path: Path, lines_: list[bytes]) -> Path:
    path.write_bytes(b"".join(line + b"\n" for line in lines_))
    return path


def encode(data: object) -> bytes:
    return json.dumps(data).encode("utf-8")


def test_read_journal_survives_a_truncated_last_line(tmp_path: Path) -> None:
    with Journal.create(tmp_path) as log:
        log.write(Operation.RUN, Status.STARTED)
        log.write(Operation.MOVE, Status.INTENT, source=Path("C:/a.pdf"), target=Path("C:/t"))
        path = log.path
    assert path is not None
    with path.open("ab") as stream:
        stream.write(b'{"schema_version": 1, "sequ')  # a crash in the middle of a write
    report = read_journal(path)
    assert any(str(p).startswith("line 3: unreadable") for p in report.problems)
    assert report.unreadable == ((3, b'{"schema_version": 1, "sequ'),)
    # The lost line may have been the outcome: the change needs recovery, not waiting.
    (entry,) = report.entries
    assert entry.state is OperationState.RECOVERY_REQUIRED and entry.intent is not None
    assert report.recovery_needed == (entry,)
    assert not report.finished and report.review_whole_journal and report.needs_review


def test_a_corrupted_line_between_valid_ones_keeps_the_rest(tmp_path: Path) -> None:
    good = [encode(r) for r in move_journal()]
    path = write_lines(tmp_path / "j.jsonl", [good[0], good[1], b"\xff{garbage", good[2], good[3]])
    report = read_journal(path)
    assert [p.line for p in report.problems] == [3]
    assert report.review_whole_journal  # the lost line may have said anything
    assert list(report.records) == move_journal()  # the valid records are kept
    assert_not_done(report)
    (entry,) = report.entries
    assert entry.state is OperationState.RECOVERY_REQUIRED and entry.outcome is None
    assert entry.conflicts == (move_journal()[2],)  # the DONE is kept, unconfirmed
    assert not report.finished


def test_a_corrupted_record_in_place_of_an_outcome_leaves_the_move_open(tmp_path: Path) -> None:
    good = [encode(r) for r in move_journal()]
    path = write_lines(tmp_path / "j.jsonl", [good[0], good[1], b'{"status": "do', good[3]])
    report = read_journal(path)
    assert_not_done(report)
    assert moves(report)[0].state is OperationState.RECOVERY_REQUIRED
    assert not report.finished  # the final record follows a gap: not trusted


def test_duplicate_keys_make_a_line_unreadable(tmp_path: Path) -> None:
    good = [encode(r) for r in move_journal()]
    twice = good[2][:-1] + b', "status": "recovery_required"}'
    path = write_lines(tmp_path / "j.jsonl", [good[0], good[1], twice, good[3]])
    report = read_journal(path)
    assert any("twice" in str(p) for p in report.problems)
    assert_not_done(report)


@pytest.mark.windows_only
def test_read_journal_binds_records_to_the_run_in_the_file_name(tmp_path: Path) -> None:
    with Journal.create(tmp_path) as log:
        log.run_id = "f" * 32  # consistent records, but of another run than the file
        log.write(Operation.RUN, Status.STARTED)
        seq = log.write(Operation.MOVE, Status.INTENT, source=Path(SRC), target=Path(DST))
        log.write(Operation.MOVE, Status.DONE, intent=seq, source=Path(SRC), target=Path(DST))
        log.write(Operation.RUN, Status.FINISHED)
        path = log.path
    assert path is not None
    report = read_journal(path)
    assert_not_done(report)
    assert not report.finished and report.review_whole_journal


@pytest.mark.windows_only
def test_read_journal_accepts_what_the_writer_writes(tmp_path: Path) -> None:
    with Journal.create(tmp_path) as log:
        log.write(Operation.RUN, Status.STARTED, source=Path("C:/in"), target=Path("C:/out"))
        mk = log.write(Operation.MKDIR, Status.INTENT, target=Path("C:/out/PDF"))
        log.write(Operation.MKDIR, Status.DONE, intent=mk, target=Path("C:/out/PDF"))
        seq = log.write(Operation.MOVE, Status.INTENT, source=Path(SRC), target=Path(DST))
        log.write(
            Operation.MOVE, Status.FAILED, intent=seq, source=Path(SRC), target=Path(DST),
            reason="locked", error_code=32, error_message="in use",
        )  # fmt: skip
        log.write(Operation.MOVE, Status.CANCELLED, source=Path("C:/in/b.pdf"))
        log.write(Operation.RUN, Status.FINISHED, reason="cancelled")
        path = log.path
    assert path is not None
    report = read_journal(path)
    assert not report.problems and report.finished and not report.needs_review
    assert [e.state for e in report.entries] == [
        OperationState.DONE,
        OperationState.FAILED,
        OperationState.CANCELLED,
    ]


# --- reading: a journal that cannot be trusted as a whole confirms nothing ---------------

B_SRC, B_DST = "C:\\in\\b.pdf", "C:\\out\\PDF\\b.pdf"


def two_moves() -> list[dict[str, object]]:
    """A complete, valid run with two confirmed moves."""
    return [
        record(1, "run", "started"),
        record(2, "move", "intent", source=SRC, target=DST),
        record(3, "move", "done", 2, source=SRC, target=DST),
        record(4, "move", "intent", source=B_SRC, target=B_DST),
        record(5, "move", "done", 4, source=B_SRC, target=B_DST),
        record(6, "run", "finished"),
    ]


def insert(records: list[dict[str, object]], index: int, extra: object) -> list[object]:
    """``extra`` at ``index``; the sequence numbers and references after it move up."""
    result: list[object] = [*records[:index], extra]
    for data in records[index:]:
        moved = {**data, "sequence": data["sequence"] + 1}  # type: ignore[operator]
        if isinstance(data["intent"], int) and data["intent"] >= index + 1:
            moved["intent"] = data["intent"] + 1
        result.append(moved)
    return result


def assert_distrusted(report: JournalReport) -> None:
    """Global distrust: every operation is listed for recovery, nothing is confirmed."""
    intents = [e.intent["source"] for e in report.entries if e.intent is not None]
    assert SRC in intents and B_SRC in intents, "an operation disappeared from the report"
    for entry in report.entries:
        assert entry.state is OperationState.RECOVERY_REQUIRED, entry
        assert entry.outcome is None, entry
        assert any("cannot be trusted" in p for p in entry.problems), entry
    assert report.recovery_needed == report.entries
    assert report.review_whole_journal and report.needs_review and not report.finished
    assert report.problems


def test_an_intact_journal_confirms_every_move() -> None:
    report = analyze(two_moves())
    assert [e.state for e in report.entries] == [OperationState.DONE] * 2
    assert report.finished and not report.problems and not report.needs_review
    assert report.unreadable == ()


def test_a_journal_that_just_stops_keeps_what_it_proved() -> None:
    """No run/finished alone is no sign of lost records: confirmed moves stay confirmed."""
    report = analyze(two_moves()[:4])  # the second move's outcome was never written
    assert [e.state for e in report.entries] == [OperationState.DONE, OperationState.UNCERTAIN]
    assert not report.finished and not report.review_whole_journal and not report.problems
    assert [e.source for e in report.recovery_needed] == [B_SRC] and report.needs_review


DAMAGE = {
    "invalid UTF-8": b'\xff\xfe{"status": "done"}',
    "cut off JSON": b'{"schema_version": 1, "sequ',
    "duplicate key": b'{"status": "done", "status": "failed"}',
    "not JSON": b"garbage",
    "empty line": b"",
}
POSITIONS = {
    "before the first intent": 1,
    "between intent and done": 2,
    "after a done": 3,
    "between the second intent and done": 4,
    "before the final record": 5,
    "after the final record": 6,
}


@pytest.mark.parametrize("at", POSITIONS.values(), ids=POSITIONS.keys())
@pytest.mark.parametrize("damage", DAMAGE.values(), ids=DAMAGE.keys())
def test_an_unreadable_line_anywhere_distrusts_every_operation(
    tmp_path: Path, damage: bytes, at: int
) -> None:
    good = [encode(r) for r in two_moves()]
    report = read_journal(write_lines(tmp_path / "j.jsonl", [*good[:at], damage, *good[at:]]))
    assert_distrusted(report)
    assert report.unreadable == ((at + 1, damage + b"\n"),)
    assert [p.line for p in report.problems if "unreadable" in p.message] == [at + 1]
    assert list(report.records) == two_moves()  # every valid record is still there
    first, second = moves(report)
    assert first.intent == two_moves()[1] and first.conflicts == (two_moves()[2],)
    assert second.intent == two_moves()[3] and second.conflicts == (two_moves()[4],)


def test_two_unreadable_lines_are_both_kept(tmp_path: Path) -> None:
    good = [encode(r) for r in two_moves()]
    lines_ = [good[0], good[1], b"\xff", good[2], good[3], good[4], b"{", good[5]]
    report = read_journal(write_lines(tmp_path / "j.jsonl", lines_))
    assert_distrusted(report)
    assert report.unreadable == ((3, b"\xff\n"), (7, b"{\n"))
    assert [p.line for p in report.problems if "unreadable" in p.message] == [3, 7]


def test_a_truncated_last_line_after_valid_moves_distrusts_them(tmp_path: Path) -> None:
    good = [encode(r) for r in two_moves()[:5]]
    path = tmp_path / "j.jsonl"
    path.write_bytes(b"".join(line + b"\n" for line in good) + b'{"schema_version": 1, "seq')
    report = read_journal(path)
    assert_distrusted(report)
    assert report.unreadable == ((6, b'{"schema_version": 1, "seq'),)


UNBOUND: dict[str, object] = {
    "a list": [],
    "a number": 3,
    "an object of no record": {"status": "done"},
    "an invalid move without a reference": record(
        4, "move", "done", source=SRC, target=DST, timestamp_utc="yesterday"
    ),
    "an unknown operation without a reference": record(4, "copy", "done", source=SRC),
    "a reference of the wrong type": record(4, "move", "done", "2", source=SRC, target=DST),
    "a reference to no intent": record(4, "move", "done", 99, source=SRC, target=DST),
    "a reference to the run record": record(4, "move", "done", 1, source=SRC, target=DST),
    "an invalid run record": record(4, "run", "started", timestamp_utc="yesterday"),
}


@pytest.mark.parametrize("extra", UNBOUND.values(), ids=UNBOUND.keys())
def test_a_record_bound_to_no_operation_distrusts_every_operation(extra: object) -> None:
    report = analyze(insert(two_moves(), 3, extra))  # between the two moves
    assert_distrusted(report)
    assert any(p.line == 4 for p in report.problems)


# FIX-02: no operation is confirmed without a valid run/started in the first place.

BAD_START = {
    "schema 2": {"schema_version": 2},
    "schema '1'": {"schema_version": "1"},
    "sequence 2": {"sequence": 2},
    "sequence 0": {"sequence": 0},
    "sequence True": {"sequence": True},
    "timestamp garbage": {"timestamp_utc": "yesterday"},
    "timestamp not UTC": {"timestamp_utc": "2026-01-01T03:00:00+03:00"},
    "status finished": {"status": "finished"},
    "status intent": {"status": "intent"},
    "refers to an intent": {"intent": 2},
    "unknown field": {"extra": 1},
}


@pytest.mark.parametrize("bound", [False, True], ids=["run id from records", "run id given"])
@pytest.mark.parametrize("change", BAD_START.values(), ids=BAD_START.keys())
def test_an_invalid_start_confirms_nothing(change: dict[str, object], bound: bool) -> None:
    records = two_moves()
    records[0] = {**records[0], **change}
    report = analyze(records, run_id="r" if bound else None)
    assert_distrusted(report)
    assert any(p.line == 1 and "valid run/started" in p.message for p in report.problems)


@pytest.mark.parametrize("bound", [False, True], ids=["run id from records", "run id given"])
def test_a_start_of_another_run_confirms_nothing(bound: bool) -> None:
    records = two_moves()
    records[0] = {**records[0], "run_id": "other"}
    assert_distrusted(analyze(records, run_id="r" if bound else None))


def test_a_missing_start_with_renumbered_records_confirms_nothing() -> None:
    records = two_moves()[1:]
    for number, data in enumerate(records, start=1):
        data["sequence"] = number
    records[1]["intent"], records[3]["intent"] = 1, 3  # consistent among themselves
    report = analyze(records)
    assert_distrusted(report)
    assert any(p.line == 1 and "valid run/started" in p.message for p in report.problems)
    # The run id is never taken from a first record that is no valid run/started.
    assert any("cannot be checked" in p.message for p in report.problems)


def test_a_missing_start_in_a_named_journal_confirms_nothing(tmp_path: Path) -> None:
    run_id = "e" * 32
    records = [{**r, "run_id": run_id} for r in two_moves()[1:]]
    for number, data in enumerate(records, start=1):
        data["sequence"] = number
    records[1]["intent"], records[3]["intent"] = 1, 3
    name = f"run-20260101T000000000000Z-{run_id}.jsonl"
    report = read_journal(write_lines(tmp_path / name, [encode(r) for r in records]))
    assert_distrusted(report)
    assert any(p.line == 1 and "valid run/started" in p.message for p in report.problems)


def test_a_start_of_another_run_than_the_file_name_confirms_nothing(tmp_path: Path) -> None:
    run_id = "e" * 32
    records = [{**r, "run_id": run_id} for r in two_moves()]
    records[0]["run_id"] = "f" * 32
    name = f"run-20260101T000000000000Z-{run_id}.jsonl"
    report = read_journal(write_lines(tmp_path / name, [encode(r) for r in records]))
    assert_distrusted(report)
    assert any(p.line == 1 and "is not this run's" in p.message for p in report.problems)


def test_a_start_that_is_not_the_first_record_confirms_nothing() -> None:
    records = two_moves()
    records[0], records[1] = records[1], records[0]
    records[0]["sequence"], records[1]["sequence"] = 1, 2
    records[2]["intent"] = 1
    report = analyze(records)
    assert_distrusted(report)
    assert any(p.line == 2 and "not the first record" in p.message for p in report.problems)


def test_a_second_start_confirms_nothing() -> None:
    report = analyze(insert(two_moves(), 3, record(4, "run", "started")))
    assert_distrusted(report)
    assert any(p.line == 4 and "not the first record" in p.message for p in report.problems)


def test_a_valid_start_in_a_named_journal_confirms_the_moves(tmp_path: Path) -> None:
    run_id = "e" * 32
    records = [{**r, "run_id": run_id} for r in two_moves()]
    name = f"run-20260101T000000000000Z-{run_id}.jsonl"
    report = read_journal(write_lines(tmp_path / name, [encode(r) for r in records]))
    assert [e.state for e in report.entries] == [OperationState.DONE] * 2
    assert report.finished and not report.needs_review


# FIX-03: a record that refers to an intent is bound to it, whatever its operation.


def answered_twice(answer: object) -> list[object]:
    return [
        record(1, "run", "started"),
        record(2, "move", "intent", source=SRC, target=DST),
        record(3, "move", "done", 2, source=SRC, target=DST),
        answer,
        record(5, "run", "finished"),
    ]


CONTRADICTIONS: dict[str, object] = {
    "run/recovery_required": record(4, "run", "recovery_required", 2, source=SRC, target=DST),
    "run/failed": record(4, "run", "failed", 2, source=SRC, target=DST),
    "run/done": record(4, "run", "done", 2, source=SRC, target=DST),
    "run/finished with a reference": record(4, "run", "finished", 2),
    "run of another run": record(
        4, "run", "recovery_required", 2, source=SRC, target=DST, run_id="other"
    ),
    "move of another run": record(
        4, "move", "recovery_required", 2, source=SRC, target=DST, run_id="other"
    ),
    "unknown operation": record(4, "copy", "recovery_required", 2, source=SRC, target=DST),
    "operation {}": record(4, {}, "recovery_required", 2, source=SRC, target=DST),
    "unknown status": record(4, "run", "exploded", 2, source=SRC, target=DST),
    "status []": record(4, "move", [], 2, source=SRC, target=DST),
    "an intent with a reference": record(4, "move", "intent", 2, source=SRC, target=DST),
    "mkdir/failed": record(4, "mkdir", "failed", 2, target=DST),
}


@pytest.mark.parametrize("answer", CONTRADICTIONS.values(), ids=CONTRADICTIONS.keys())
def test_a_contradicting_answer_is_kept_as_a_conflict(answer: object) -> None:
    records = answered_twice(answer)
    report = analyze(records)  # must not raise
    (entry,) = [e for e in moves(report) if e.intent == records[1]]
    assert entry.state is OperationState.RECOVERY_REQUIRED and entry.outcome is None
    assert entry in report.recovery_needed and report.needs_review
    assert answer in entry.conflicts and records[2] in entry.conflicts
    assert any(p.line == 4 for p in report.problems)
    assert all(e.state is not OperationState.DONE for e in report.entries)


@pytest.mark.parametrize("ref", ["2", True, 2.0, [2], {}, 0, -2, 99, 1, 5])
@pytest.mark.parametrize("operation", ["run", "move", "copy"])
def test_a_contradiction_with_a_damaged_reference_distrusts_the_journal(
    ref: object, operation: str
) -> None:
    records = answered_twice(record(4, operation, "recovery_required", ref, source=SRC, target=DST))
    report = analyze(records)
    (entry,) = [e for e in moves(report) if e.intent == records[1]]
    assert entry.state is OperationState.RECOVERY_REQUIRED and entry.outcome is None
    assert entry in report.recovery_needed
    assert report.review_whole_journal and not report.finished
    assert any(p.line == 4 and "cannot be resolved" in p.message for p in report.problems)


def test_a_run_record_with_a_bad_status_and_no_reference_distrusts_the_journal() -> None:
    report = analyze(answered_twice(record(4, "run", "recovery_required", source=SRC)))
    (entry,) = moves(report)
    assert entry.state is OperationState.RECOVERY_REQUIRED and entry.outcome is None
    assert report.review_whole_journal and not report.finished


# --- verified volume root (R3A-FIX-03 / JOURNAL-08) -----------------------------------


def drive_device(path: Path) -> str:
    """What the drive letter of ``path`` really is, e.g. ``\\Device\\HarddiskVolume3``."""
    (device,) = winfileops.query_dos_device(path.drive)
    return device


OTHER_VOLUME = "\\Device\\HarddiskVolume999"  # a disk volume the opened root is not on


class FakeMapping:
    """``query_dos_device(drive)`` gives ``answers`` in turn (the last one repeats);
    an exception in ``answers`` is raised. Other names get the real answer."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, drive: str, *answers: object) -> None:
        real = winfileops.query_dos_device
        self.calls = 0

        def query(name: str) -> tuple[str, ...]:
            if name.upper() != drive.upper():
                return real(name)
            answer = answers[min(self.calls, len(answers) - 1)]
            self.calls += 1
            if isinstance(answer, BaseException):
                raise answer
            return answer  # type: ignore[return-value]

        monkeypatch.setattr(winfileops, "query_dos_device", query)


_open_anchor = winfileops.open_anchor


def open_directory(path: Path) -> winfileops.Handle:
    """A handle of directory ``path`` (what a redefined drive letter would open)."""
    handle = _open_anchor(Path(path.anchor))
    for part in path.parts[1:]:
        child, _ = winfileops.open_relative(
            handle,
            part,
            directory=True,
            access=winfileops.FILE_READ_ATTRIBUTES,
            share=winfileops.SHARE_ALL,
        )
        handle.close()
        handle = child
    return handle


@pytest.fixture
def substituted(tmp_path: Path) -> Path:
    """The directory a fake ``subst`` drive maps to; must stay exactly as it is."""
    target = tmp_path / "substituted"
    (target / "inner").mkdir(parents=True)
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    return target


def mapping_answers(
    case: str, device: str, substituted: Path, make_junction
) -> tuple[tuple[object, ...], str, bool]:
    """(answers of QueryDosDeviceW, expected message, whether the root gets opened)."""
    subst = "\\??\\" + str(substituted)
    refused = "not the drive letter of a disk volume"
    unreadable = "cannot read what"
    if case == "subst":
        return ((subst,),), refused, False
    if case == "nested subst":
        return (("\\??\\X:\\inner",),), refused, False
    if case == "subst to a junction":
        link = make_junction(substituted.parent / "junction", substituted)
        return (("\\??\\" + str(link),),), refused, False
    if case == "subdirectory of the volume":
        return ((device + "\\Users",),), refused, False
    if case == "undefined":
        return (
            (FileNotFoundError(2, "The system cannot find the file specified"),),
            unreadable,
            False,
        )
    if case == "unreadable":
        return (PermissionError(5, "Access is denied"),), unreadable, False
    if case == "ambiguous":
        return ((device, subst),), refused, False
    if case == "nothing":
        return ((),), "defined as nothing", False
    if case == "network drive":
        return (
            (("\\Device\\LanmanRedirector\\;Z:0000000000012345\\server\\share",),),
            refused,
            False,
        )
    if case == "cd-rom":
        return (("\\Device\\CdRom0",),), refused, False
    if case == "shadow copy":
        return (("\\Device\\HarddiskVolumeShadowCopy1",),), refused, False
    if case == "another volume":
        return ((OTHER_VOLUME,),), "not the root of", True
    if case == "subst during the check":
        return ((device,), (subst,)), refused, True
    if case == "another volume during the check":
        return ((device,), (OTHER_VOLUME,)), "was redefined while it was checked", True
    if case == "undefined during the check":
        return ((device,), FileNotFoundError(2, "gone")), unreadable, True
    raise AssertionError(case)


MAPPING_CASES = [
    "subst",
    "nested subst",
    "subst to a junction",
    "subdirectory of the volume",
    "undefined",
    "unreadable",
    "ambiguous",
    "nothing",
    "network drive",
    "cd-rom",
    "shadow copy",
    "another volume",
    "subst during the check",
    "another volume during the check",
    "undefined during the check",
]


@pytest.mark.windows_only
@pytest.mark.parametrize("default", [False, True], ids=["explicit", "default"])
@pytest.mark.parametrize("case", MAPPING_CASES)
def test_create_refuses_a_drive_letter_that_is_not_a_proven_volume_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spy: HandleSpy,
    make_junction,
    snapshot,
    substituted: Path,
    case: str,
    default: bool,
) -> None:
    device = drive_device(tmp_path)
    answers, message, opens = mapping_answers(case, device, substituted, make_junction)
    local = Path(os.environ["LOCALAPPDATA"])
    logs = local / "DownloadsOrganizer" / "logs" if default else tmp_path / "logs"
    before, before_local = snapshot(substituted), snapshot(local)
    FakeMapping(monkeypatch, tmp_path.drive, *answers)
    with pytest.raises(JournalError, match=message):
        Journal.create(None if default else logs)
    assert not logs.exists() and not (local / "DownloadsOrganizer").exists()
    assert snapshot(substituted) == before  # nothing created in the substituted directory
    assert snapshot(local) == before_local
    if opens:
        spy.assert_released()  # only the root was opened, and it was released
        assert [h.path for h, _ in spy.opened] == [Path(tmp_path.anchor)]
    else:
        assert spy.opened == []  # refused before anything was opened


@pytest.mark.windows_only
def test_create_refuses_a_root_that_was_swapped_after_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy, snapshot, substituted: Path
) -> None:
    """The letter is redefined between QueryDosDeviceW and the open: the opened object
    (here the substituted directory) is checked, not the name it was opened by."""
    before = snapshot(substituted)
    monkeypatch.setattr(winfileops, "open_anchor", lambda path: open_directory(substituted))
    with pytest.raises(JournalError, match="is not the root directory of its volume"):
        Journal.create(tmp_path / "logs")
    assert not (tmp_path / "logs").exists() and snapshot(substituted) == before
    spy.assert_released()


ROOT_INFO = {
    "not MFT record 5": {"file_id": 0x0005_0000_0000_0123},
    "high file id bits": {"file_id": (1 << 64) | 5},
    "reparse point": {"reparse_tag": 0xA000_0003},
    "not a directory": {"directory": False},
}


@pytest.mark.windows_only
@pytest.mark.parametrize("changes", ROOT_INFO.values(), ids=ROOT_INFO.keys())
def test_create_refuses_a_root_that_is_not_the_volume_root_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy, changes: dict[str, object]
) -> None:
    real = winfileops.query
    anchor = Path(tmp_path.anchor)

    def query(handle: winfileops.Handle) -> winfileops.HandleInfo:
        info = real(handle)
        return dataclasses.replace(info, **changes) if handle.path == anchor else info

    monkeypatch.setattr(winfileops, "query", query)
    with pytest.raises(JournalError, match="is not the root directory of its volume"):
        Journal.create(tmp_path / "logs")
    assert not (tmp_path / "logs").exists()
    spy.assert_released()


@pytest.mark.windows_only
@pytest.mark.parametrize("answer", ["subdirectory", "another volume", "error"])
def test_create_refuses_a_root_whose_final_path_is_not_the_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy, answer: str
) -> None:
    """MFT record 5 alone is not enough: the opened object must be the device's root."""
    device = drive_device(tmp_path)
    real = winfileops.final_nt_path
    anchor = Path(tmp_path.anchor)

    def final(handle: winfileops.Handle) -> str:
        if handle.path != anchor:
            return real(handle)
        if answer == "error":
            raise OSError(1, "Incorrect function")
        return device + "\\Users\\" if answer == "subdirectory" else "\\Device\\HarddiskVolume999\\"

    monkeypatch.setattr(winfileops, "final_nt_path", final)
    match = "Incorrect function" if answer == "error" else "not the root of"
    with pytest.raises(JournalError, match=match):
        Journal.create(tmp_path / "logs")
    assert not (tmp_path / "logs").exists()
    spy.assert_released()


@pytest.mark.windows_only
@pytest.mark.parametrize("where", ["final path", "redefined", "subst", "undefined"])
def test_create_refuses_a_new_file_off_the_verified_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy, where: str
) -> None:
    """Checked again through the new file's handle; the file stays where the held chain
    put it (on the verified volume, empty), never in a substituted directory."""
    logs = tmp_path / "logs"
    logs.mkdir()
    device = drive_device(tmp_path)
    if where == "final path":
        real = winfileops.final_nt_path

        def final(handle: winfileops.Handle) -> str:
            if handle.path.parent == logs:
                return "\\Device\\HarddiskVolume999\\" + handle.path.name
            return real(handle)

        monkeypatch.setattr(winfileops, "final_nt_path", final)
        match = "is not on"
    elif where == "redefined":
        FakeMapping(monkeypatch, tmp_path.drive, (device,), (device,), (OTHER_VOLUME,))
        match = "was redefined"
    elif where == "subst":
        FakeMapping(monkeypatch, tmp_path.drive, (device,), (device,), ("\\??\\C:\\other",))
        match = "not the drive letter of a disk volume"
    else:
        FakeMapping(monkeypatch, tmp_path.drive, (device,), (device,), FileNotFoundError(2, "x"))
        match = "cannot read what"
    with pytest.raises(JournalError, match=match):
        Journal.create(logs)
    spy.assert_released()
    (left,) = logs.iterdir()  # residual: the new, empty file on the verified volume
    assert left.stat().st_size == 0
    left.unlink()
    logs.rename(tmp_path / "moved")


@pytest.mark.windows_only
@pytest.mark.parametrize(
    "path",
    [
        "\\\\server\\share\\logs",
        "\\\\localhost\\C$\\logs",
        "\\\\?\\C:\\logs",
        "\\\\?\\UNC\\server\\share\\logs",
        "\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\logs",
        "\\\\.\\C:\\logs",
    ],
)
def test_create_refuses_unc_and_device_paths(path: str, spy: HandleSpy) -> None:
    with pytest.raises(JournalError):
        Journal.create(Path(path))
    assert spy.opened == []


@pytest.mark.windows_only
def test_create_refuses_a_non_drive_anchor_even_if_normalized(
    monkeypatch: pytest.MonkeyPatch, spy: HandleSpy
) -> None:
    """The drive-letter check does not depend on normalize_dir_path refusing UNC."""
    monkeypatch.setattr(journal_module, "normalize_dir_path", lambda path: path)
    with pytest.raises(JournalError, match="only a directory on a local drive letter"):
        Journal.create(Path("\\\\server\\share\\logs"))
    assert spy.opened == []


@pytest.mark.windows_only
@pytest.mark.parametrize("default", [False, True], ids=["explicit", "default"])
def test_ordinary_ntfs_drive_is_proven_and_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy, default: bool
) -> None:
    """No false refusal: every check runs on the real API and passes."""
    device = drive_device(tmp_path)
    assert re.fullmatch(r"\\Device\\HarddiskVolume[0-9]+", device)
    calls: Counter[str] = Counter()
    for name in ("query_dos_device", "final_nt_path"):
        real = getattr(winfileops, name)

        def counted(*args: object, _real=real, _name=name) -> object:
            calls[_name] += 1
            return _real(*args)

        monkeypatch.setattr(winfileops, name, counted)
    log = Journal.create(None if default else tmp_path / "logs")
    assert log._chain is not None
    assert (log._chain.drive, log._chain.device) == (tmp_path.drive, device)
    assert calls == {"query_dos_device": 3, "final_nt_path": 2}  # root twice, root, file
    log.write(Operation.RUN, Status.STARTED)
    log.close()
    assert calls["query_dos_device"] == 4  # checked again on close
    assert log.path is not None and read_journal(log.path).entries == ()
    spy.assert_released()


@pytest.mark.windows_only
@pytest.mark.parametrize("change", ["redefined", "undefined", "renamed"])
def test_close_reports_a_journal_path_that_no_longer_leads_to_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy, change: str
) -> None:
    log = Journal.create(tmp_path / "logs")
    log.write(Operation.RUN, Status.STARTED)
    assert log.path is not None
    if change == "redefined":
        FakeMapping(monkeypatch, tmp_path.drive, ("\\??\\C:\\other",))
        match = "is no longer"
    elif change == "undefined":
        FakeMapping(monkeypatch, tmp_path.drive, FileNotFoundError(2, "gone"))
        match = "cannot check that"
    else:
        other = tmp_path / "other.jsonl"
        other.write_bytes(b"")
        real = Path.lstat
        path = log.path
        monkeypatch.setattr(Path, "lstat", lambda p: real(other) if p == path else real(p))
        match = "no longer leads to the journal file"
    with pytest.raises(JournalCloseError, match=match):
        log.close()
    spy.assert_released()  # the file and the chain are released all the same
    monkeypatch.undo()
    assert len(lines(log.path)) == 1  # what was written is kept
    (tmp_path / "logs").rename(tmp_path / "moved")


# --- handle release failures in the journal (R3A-FIX-03 / HANDLE-01) ------------------


@pytest.mark.windows_only
def test_close_reports_a_directory_handle_that_cannot_be_released(
    tmp_path: Path, spy: HandleSpy, close_failures
) -> None:
    logs = tmp_path / "a" / "logs"
    logs.parent.mkdir()
    log = Journal.create(logs)
    log.write(Operation.RUN, Status.STARTED)
    close_failures.select = lambda handle: handle.path == tmp_path / "a"
    with pytest.raises(JournalCloseError, match="CloseHandle failed") as raised:
        log.close()
    (error,) = raised.value.errors
    assert isinstance(error, winfileops.HandleCloseError) and error.winerror == 31
    assert spy.open_now() == [tmp_path / "a"]  # only that one: the others were released
    with pytest.raises(JournalCloseError) as again:
        log.close()  # reported again, never retried
    assert again.value is raised.value and len(close_failures.failed) == 1
    assert log.path is not None
    log.path.unlink()  # the file was closed
    with pytest.raises(PermissionError):
        (tmp_path / "a").rename(tmp_path / "b")  # still held: not treated as released
    close_failures.really_close()
    (tmp_path / "a").rename(tmp_path / "b")


@pytest.mark.windows_only
def test_close_reports_both_the_file_and_a_directory_failure(
    tmp_path: Path, spy: HandleSpy, close_failures
) -> None:
    log = Journal.create(tmp_path / "logs")
    real = log._stream
    assert real is not None

    class Unclosable:
        def close(self) -> None:
            real.close()
            raise OSError(5, "I/O error")

    log._stream = Unclosable()  # type: ignore[assignment]
    close_failures.select = lambda handle: handle.path == tmp_path
    with pytest.raises(JournalCloseError) as raised:
        log.close()
    first, second = raised.value.errors
    assert str(first) == "[Errno 5] I/O error"
    assert isinstance(second, winfileops.HandleCloseError)
    assert spy.open_now() == [tmp_path]
    close_failures.really_close()


@pytest.mark.windows_only
def test_close_releases_everything_even_if_the_location_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy
) -> None:
    log = Journal.create(tmp_path / "logs")
    log.write(Operation.RUN, Status.STARTED)
    broken = RuntimeError("location check broke")

    def location_problem(*args: object) -> str | None:
        raise broken

    monkeypatch.setattr(journal_module, "_location_problem", location_problem)
    with pytest.raises(JournalCloseError) as raised:
        log.close()
    assert list(raised.value.errors) == [broken]
    assert spy.open_now() == []  # the file and every directory were released
    assert log.path is not None
    log.path.unlink()
    (tmp_path / "logs").rmdir()


@pytest.mark.windows_only
def test_create_reports_a_release_failure_with_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy, close_failures
) -> None:
    def broken(*args: object, **kwargs: object) -> winfileops.Handle:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(winfileops, "create_file", broken)
    close_failures.select = lambda handle: handle.path == tmp_path
    with pytest.raises(JournalError) as raised:
        Journal.create(tmp_path / "logs")
    assert str(raised.value) == "cannot create journal: [Errno 28] No space left on device"
    assert isinstance(raised.value.__cause__, OSError)  # the original error
    (error,) = raised.value.release_errors  # the release failure, as its own object
    assert isinstance(error, winfileops.HandleCloseError) and error.winerror == 31
    assert error.handle_value == close_failures.failed[0] and error.filename == str(tmp_path)
    assert spy.open_now() == [tmp_path]
    close_failures.really_close()


@pytest.mark.windows_only
def test_create_keeps_both_errors_when_the_stream_fails_and_the_file_cannot_be_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy, close_failures
) -> None:
    """HANDLE-02: ``open_osfhandle`` fails, then ``CloseHandle`` of the file is FALSE."""
    import msvcrt

    logs = tmp_path / "logs"

    def broken(*args: object) -> int:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(msvcrt, "open_osfhandle", broken)
    close_failures.select = lambda handle: handle.path.parent == logs
    with pytest.raises(JournalError) as raised:
        Journal.create(logs)
    error = raised.value
    assert str(error) == "cannot create journal: [Errno 24] Too many open files"
    assert isinstance(error.__cause__, OSError) and error.__cause__.errno == 24
    (release,) = error.release_errors
    assert isinstance(release, winfileops.HandleCloseError) and release.winerror == 31
    (raw,) = close_failures.failed
    assert release.handle_value == raw and Path(release.filename).parent == logs
    assert f"{raw:#x}" in str(release)
    (file_handle,) = spy.open_now()  # only the file: never treated as closed
    assert file_handle.parent == logs
    close_failures.really_close()
    for path in logs.iterdir():
        path.unlink()
    logs.rename(tmp_path / "moved")


@pytest.mark.windows_only
def test_create_keeps_both_errors_when_the_crt_cannot_release_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: HandleSpy
) -> None:
    """HANDLE-02 after the hand-over: the C runtime owns the handle, building the
    stream fails, and the CRT's close of it is a real ``FALSE``
    (HANDLE_FLAG_PROTECT_FROM_CLOSE)."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    k.SetHandleInformation.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    logs = tmp_path / "logs"
    protected: list[int] = []

    def broken(fd: int, mode: str) -> object:
        raw = msvcrt.get_osfhandle(fd)
        assert k.SetHandleInformation(raw, 2, 2)
        protected.append(raw)
        raise MemoryError("no memory for the file object")

    monkeypatch.setattr(winfileops, "FileIO", broken)
    try:
        with pytest.raises(JournalError) as raised:
            Journal.create(logs)
        error = raised.value
        assert str(error) == "cannot create journal: MemoryError: no memory for the file object"
        assert isinstance(error.__cause__, MemoryError)
        (release,) = error.release_errors
        assert isinstance(release, OSError) and release is not error.__cause__
        assert spy.open_now() == []  # every Handle object: released or handed over
    finally:
        for raw in protected:
            assert k.SetHandleInformation(raw, 2, 0)
            assert k.CloseHandle(raw)  # it really had stayed open
    for path in logs.iterdir():
        path.unlink()
    logs.rename(tmp_path / "moved")


def test_releasing_the_chain_reports_every_failure_and_interrupts_last() -> None:
    closed: list[str] = []

    class Fake:
        def __init__(self, name: str, error: BaseException | None = None) -> None:
            self.name, self.error, self.path = name, error, Path(name)

        def close(self) -> None:
            closed.append(self.name)
            if self.error is not None:
                raise self.error

    chain = journal_module._DirectoryChain()
    first, interrupt = OSError(6, "invalid handle"), KeyboardInterrupt()
    for fake in (Fake("root"), Fake("a", first), Fake("b", interrupt), Fake("c")):
        chain.push(fake)  # type: ignore[arg-type]
    with pytest.raises(KeyboardInterrupt) as raised:
        chain.close()
    assert closed == ["c", "b", "a", "root"]  # the interrupt did not stop the release
    assert raised.value is interrupt and "invalid handle" in raised.value.__notes__[0]


@pytest.mark.windows_only
def test_interrupt_while_releasing_a_failed_create_is_raised_not_kept_as_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HANDLE-03: ``create`` fails, and an interrupt comes while its handles are released:
    the interrupt is raised (with what failed), never stored in ``release_errors``."""
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a directory", encoding="utf-8")
    real = journal_module._DirectoryChain.release
    interrupt = KeyboardInterrupt()
    released: list[int] = []

    def release(self: journal_module._DirectoryChain) -> list[BaseException]:
        errors = real(self)  # every handle is still released for real
        released.append(len(errors))
        return [*errors, interrupt]

    monkeypatch.setattr(journal_module._DirectoryChain, "release", release)
    with pytest.raises(KeyboardInterrupt) as raised:
        Journal.create(blocked / "logs")
    assert raised.value is interrupt and released == [0]
    assert isinstance(raised.value.__cause__, JournalError)
    (note,) = raised.value.__notes__
    assert note.startswith("while creating the journal (JournalError: cannot create journal")
    blocked.rename(tmp_path / "moved")  # nothing held


def test_interrupt_among_close_failures_carries_the_others_as_objects() -> None:
    """HANDLE-04: the interrupt is raised with every other failure as an object."""
    first = OSError(6, "invalid handle")
    closing = winfileops._close_error(0x1F4, Path("C:/x/logs"), 31)
    interrupt = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as raised:
        journal_module._raise_failures("cannot close", [first, interrupt, closing])
    assert raised.value is interrupt
    assert interrupt.release_errors == (first, closing)  # type: ignore[attr-defined]
    assert winfileops.release_failures(interrupt) == [first, closing]
    (note,) = interrupt.__notes__
    assert note.startswith("cannot close: OSError: ")
    assert f"HandleCloseError: {closing}" in note and "0x1f4" in note


def test_journal_close_error_keeps_every_failure_for_release_failures() -> None:
    first, closing = OSError(6, "a"), winfileops._close_error(7, Path("C:/x"), 31)
    with pytest.raises(JournalCloseError) as raised:
        journal_module._raise_failures("cannot close", [first, closing])
    assert raised.value.errors == raised.value.release_errors == (first, closing)
    assert winfileops.release_failures(raised.value) == [first, closing]


@pytest.mark.windows_only
def test_interrupt_while_releasing_a_failed_create_keeps_every_earlier_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HANDLE-04: the stream's own release failure and the chain's failures all stay
    attached to the interrupt, as objects."""
    earlier = winfileops._close_error(0x11, tmp_path / "logs" / "run.jsonl", 31)
    chain_error = winfileops._close_error(0x22, tmp_path / "logs", 31)
    interrupt = SystemExit(3)
    real = journal_module._DirectoryChain.release

    def create_log_file(chain: object, path: Path) -> object:
        raise winfileops.ReleaseError(OSError(5, "cannot wrap"), [earlier])

    def release(self: journal_module._DirectoryChain) -> list[BaseException]:
        return [*real(self), chain_error, interrupt]

    monkeypatch.setattr(journal_module, "_create_log_file", create_log_file)
    monkeypatch.setattr(journal_module._DirectoryChain, "release", release)
    with pytest.raises(SystemExit) as raised:
        Journal.create(tmp_path / "logs")
    assert raised.value is interrupt
    cause = interrupt.__cause__
    assert isinstance(cause, JournalError)
    assert cause.release_errors == (earlier,)
    # HANDLE-05: the error the interrupt was raised in place of is kept as an object too
    assert interrupt.release_errors == (cause, earlier, chain_error)  # type: ignore[attr-defined]
    assert winfileops.release_failures(interrupt) == [cause, earlier, chain_error]
    (note,) = interrupt.__notes__
    assert f"HandleCloseError: {chain_error}" in note
    (tmp_path / "logs").rename(tmp_path / "moved")  # nothing held


# What leaves the ``with`` block (or None); what closing the journal's stream raises.
EXIT_CASES = {
    "KeyboardInterrupt, then SystemExit": lambda: (KeyboardInterrupt("A"), SystemExit("B")),
    "SystemExit, then KeyboardInterrupt": lambda: (SystemExit("A"), KeyboardInterrupt("B")),
    "KeyboardInterrupt, then OSError": lambda: (KeyboardInterrupt("A"), OSError(5, "B")),
    "OSError, then SystemExit": lambda: (OSError(28, "A"), SystemExit("B")),
    "nothing, then KeyboardInterrupt": lambda: (None, KeyboardInterrupt("B")),
}


@pytest.mark.windows_only
@pytest.mark.parametrize("case", list(EXIT_CASES))
def test_journal_exit_keeps_the_error_leaving_the_block(
    tmp_path: Path, spy: HandleSpy, case: str
) -> None:
    """HANDLE-05: the ``with`` block's own error stays the one raised (the very object)
    when closing the journal fails or is interrupted; an interrupt is never dropped."""
    primary, secondary = EXIT_CASES[case]()
    log = Journal.create(tmp_path / "logs")
    real = log._stream
    assert real is not None

    class ClosingFails:
        def close(self) -> None:
            real.close()
            raise secondary

    with pytest.raises(BaseException) as raised, log:
        log.write(Operation.RUN, Status.STARTED)
        log._stream = ClosingFails()  # type: ignore[assignment]
        if primary is not None:
            raise primary
    failures = winfileops.release_failures(raised.value)
    if primary is not None and not isinstance(primary, Exception):
        assert raised.value is primary
        if isinstance(secondary, Exception):  # wrapped with the chain's failures, if any
            (closing,) = failures[:1]
            assert isinstance(closing, JournalCloseError) and closing.errors == (secondary,)
            assert secondary in failures
        else:
            assert failures == [secondary]
    else:
        assert raised.value is secondary
        assert failures == ([] if primary is None else [primary])
    spy.assert_released()  # the directory chain was released, each handle once
    assert lines(log.path or Path())[0]["status"] == "started"
    (tmp_path / "logs").rename(tmp_path / "moved")  # nothing held


# --- real subst drives (opt-in: DOWNLOADSORGANIZER_REAL_SUBST=1) ----------------------


def free_drive_letters(count: int) -> list[str]:
    """Unused drive letters (no volume, no DOS device), from Z: down."""
    import ctypes

    used = ctypes.WinDLL("kernel32").GetLogicalDrives()
    free: list[str] = []
    for letter in "ZYXWVUTSRQPONMLKJIHGFED":
        if used & (1 << (ord(letter) - ord("A"))):
            continue
        try:
            winfileops.query_dos_device(f"{letter}:")
        except FileNotFoundError:
            free.append(f"{letter}:")
            if len(free) == count:
                return free
    pytest.skip("NOT_TESTED: not enough free drive letters")


@pytest.mark.windows_only
@pytest.mark.skipif(
    os.environ.get("DOWNLOADSORGANIZER_REAL_SUBST") != "1",
    reason="NOT_TESTED: real subst drives, opt in with DOWNLOADSORGANIZER_REAL_SUBST=1",
)
def test_real_subst_drives_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snapshot, make_junction, spy: HandleSpy
) -> None:
    """Real ``subst`` letters on free drive letters, mapped to temporary directories only;
    removed in ``finally`` (and only if they still are the mappings made here)."""
    import subprocess

    from downloads_organizer.executor import ExecReason, ItemStatus, execute_plan
    from downloads_organizer.extensions import ExtensionTable
    from downloads_organizer.models import CollisionPolicy
    from downloads_organizer.planner import build_plan

    target = tmp_path / "substituted"
    for sub in ("inner", "Local"):
        (target / sub).mkdir(parents=True)
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    junction = make_junction(tmp_path / "junction", target)
    plain, nested, linked, whole = free_drive_letters(4)
    mappings = [
        (plain, str(target)),
        (nested, plain + "\\inner"),  # nested: a subst of a subst drive
        (linked, str(junction)),  # to a directory behind a junction
        (whole, tmp_path.anchor),  # even the volume root itself: no virtual resolution
    ]
    made: list[tuple[str, str]] = []
    leftovers: list[str] = []
    try:
        for letter, path in mappings:
            done = subprocess.run(["subst", letter, path], capture_output=True, text=True)
            if done.returncode != 0:
                pytest.skip(f"NOT_TESTED: subst {letter} failed: {done.stdout}{done.stderr}")
            (current,) = winfileops.query_dos_device(letter)
            made.append((letter, current))  # what cleanup removes, and only that
            # A drive root is kept without its backslash: \??\C: for C:\.
            assert current in ("\\??\\" + path, "\\??\\" + path.rstrip("\\"))
        before = snapshot(target)
        candidates = [
            Path(plain + "\\logs"),
            Path(plain + "\\"),
            Path(plain + "\\inner\\logs"),
            Path(nested + "\\logs"),
            Path(linked + "\\logs"),
            Path(whole + "\\") / tmp_path.relative_to(tmp_path.anchor) / "logs",
        ]
        for path in candidates:
            with pytest.raises(JournalError, match="not the drive letter of a disk volume"):
                Journal.create(path)
        monkeypatch.setenv("LOCALAPPDATA", plain + "\\Local")
        with pytest.raises(JournalError, match="not the drive letter of a disk volume"):
            Journal.create()
        assert spy.opened == []  # refused before anything was opened
        assert not (tmp_path / "logs").exists()

        source = tmp_path / "Downloads"
        source.mkdir()
        (source / "a.pdf").write_bytes(b"a")
        table = ExtensionTable()
        plan = build_plan(source, None, table, frozenset({".pdf"}), CollisionPolicy.RENAME)
        result = execute_plan(plan, table)  # the default journal is on the subst drive
        assert [(r.status, r.reason) for r in result.items] == [
            (ItemStatus.CANCELLED, ExecReason.JOURNAL_FAILED)
        ]
        assert sorted(p.name for p in source.iterdir()) == ["a.pdf"]
        assert snapshot(target) == before  # nothing created in the substituted directory

        # The hole that was closed: the old checks alone would have accepted plain.
        with winfileops.open_anchor(Path(plain + "\\")) as root:
            volume = winfileops.query_volume(root)
            assert (volume.filesystem, volume.remote, volume.removable) == ("NTFS", False, False)
            assert winfileops.query(root).file_id & ((1 << 48) - 1) != 5
            final = winfileops.final_nt_path(root).upper()
            assert final.endswith(str(target)[2:].upper())
    finally:
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
        if leftovers:  # also when the test failed: the original error is the context
            pytest.fail(f"subst drives not removed: {leftovers}")
