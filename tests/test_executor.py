"""Executor tests on real temporary trees (``tmp_path`` only).

Every test checks the filesystem afterwards (content hashes and file ids), not only the
reported statuses.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from downloads_organizer import executor, journal, winfileops
from downloads_organizer.executor import (
    ExecReason,
    ExecutionResult,
    ItemStatus,
    execute_plan,
    validate_plan,
)
from downloads_organizer.extensions import ExtensionTable
from downloads_organizer.journal import Journal, JournalError, OperationState, read_journal
from downloads_organizer.models import CollisionPolicy, FileInfo, Plan, PlanAction, PlanItem
from downloads_organizer.planner import build_plan

pytestmark = pytest.mark.windows_only

TABLE = ExtensionTable()
MOVED, SKIPPED, FAILED = ItemStatus.MOVED, ItemStatus.SKIPPED, ItemStatus.FAILED
CANCELLED, RECOVERY = ItemStatus.CANCELLED, ItemStatus.RECOVERY_REQUIRED
EXTS = frozenset({".pdf", ".jpg", ".mp3"})
Snapshot = Callable[[Path], dict[str, tuple[object, ...]]]


# --- helpers ---------------------------------------------------------------------------


@pytest.fixture
def area(tmp_path: Path) -> Path:
    """Everything the organizer may touch; logs live elsewhere (``user_home``)."""
    path = tmp_path / "area"
    path.mkdir()
    return path


@pytest.fixture
def src(area: Path) -> Path:
    path = area / "Downloads"
    path.mkdir()
    return path


def touch(directory: Path, *names: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(f"content of {name}".encode())


def fingerprint(path: Path) -> tuple[int, str]:
    """(file id, sha256) of a regular file."""
    return path.stat(follow_symlinks=False).st_ino, hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprints(directory: Path) -> dict[str, tuple[int, str]]:
    return {p.name: fingerprint(p) for p in directory.iterdir() if p.is_file()}


def plan_for(
    source: Path,
    destination: Path | None = None,
    exts: frozenset[str] = EXTS,
    policy: CollisionPolicy = CollisionPolicy.RENAME,
) -> Plan:
    plan = build_plan(source, destination, TABLE, exts, policy)
    assert plan.ok, plan.errors
    return plan


def run(plan: object, **kwargs: object) -> ExecutionResult:
    return execute_plan(plan, TABLE, **kwargs)  # type: ignore[arg-type]


def outcome(result: ExecutionResult) -> list[tuple[str, ItemStatus, ExecReason]]:
    return [(r.item.source.name, r.status, r.reason) for r in result.items]


def forge[T](obj: T, **changes: object) -> T:
    """A copy of a frozen dataclass with ``changes``, bypassing ``__post_init__``."""
    new = object.__new__(type(obj))
    for field in dataclasses.fields(obj):  # type: ignore[arg-type]
        object.__setattr__(new, field.name, changes.get(field.name, getattr(obj, field.name)))
    return new


def journal_of(result: ExecutionResult) -> journal.JournalReport:
    assert result.journal_path is not None
    report = read_journal(result.journal_path)
    assert not report.problems, report.problems
    return report


def assert_journal_consistent(result: ExecutionResult) -> journal.JournalReport:
    """The journal agrees with the result: every move attempt has intent and outcome."""
    report = journal_of(result)
    assert report.finished
    assert not report.uncertain
    moves = {e.source: e for e in report.entries if e.operation == "move"}
    for item in result.items:
        entry = moves[str(item.item.source)]
        assert entry.state.value == executor._JOURNAL_STATUS[item.status].value
        if item.status is MOVED:
            assert entry.attempted
            assert entry.intent is not None and entry.intent["target"] == str(item.item.target)
    return report


@pytest.fixture
def deny() -> Iterator[Callable[[Path, str], None]]:
    """Add a deny ACE for Everyone (``icacls`` rights such as ``AD``); removed on teardown."""
    denied: list[Path] = []

    def add(directory: Path, rights: str) -> None:
        subprocess.run(
            ["icacls", str(directory), "/deny", f"*S-1-1-0:({rights})"],
            check=True,
            capture_output=True,
        )
        denied.append(directory)

    yield add
    for directory in denied:
        subprocess.run(
            ["icacls", str(directory), "/remove:d", "*S-1-1-0"], check=True, capture_output=True
        )


# --- 1. normal moves -------------------------------------------------------------------


def test_moves_files_and_creates_only_planned_directories(src: Path) -> None:
    touch(src, "a.pdf", "b.JPG", "song.mp3", "notes.txt")
    before = fingerprints(src)
    plan = plan_for(src)
    root = src / "DEL"
    assert plan.directories_to_create == (root, root / "Audio", root / "Images", root / "PDF")

    result = run(plan)

    assert outcome(result) == [
        ("a.pdf", MOVED, ExecReason.COMPLETED),
        ("b.JPG", MOVED, ExecReason.COMPLETED),
        ("song.mp3", MOVED, ExecReason.COMPLETED),
    ]
    assert result.complete and not result.partial and result.stopped is None
    # Created on first use: DEL, then the category of the first file of each kind.
    assert result.directories_created == (root, root / "PDF", root / "Images", root / "Audio")
    assert fingerprints(root / "PDF") == {"a.pdf": before["a.pdf"]}
    assert fingerprints(root / "Images") == {"b.JPG": before["b.JPG"]}
    assert fingerprints(root / "Audio") == {"song.mp3": before["song.mp3"]}
    assert fingerprints(src) == {"notes.txt": before["notes.txt"]}
    assert sorted(p.name for p in root.iterdir()) == ["Audio", "Images", "PDF"]
    report = assert_journal_consistent(result)
    mkdirs = [(e.target, e.state) for e in report.entries if e.operation == "mkdir"]
    assert mkdirs == [(str(d), OperationState.DONE) for d in result.directories_created]


def test_journal_is_written_to_localappdata_and_never_into_the_tree(
    src: Path, isolated_user_dirs: Path
) -> None:
    touch(src, "a.pdf")
    result = run(plan_for(src))
    assert result.journal_path is not None
    assert result.journal_path.parent == journal.default_log_dir()
    assert result.journal_path.is_relative_to(isolated_user_dirs)
    records = journal_of(result).records
    assert [(r["operation"], r["status"]) for r in records] == [
        ("run", "started"),
        ("mkdir", "intent"),
        ("mkdir", "done"),
        ("mkdir", "intent"),
        ("mkdir", "done"),
        ("move", "intent"),
        ("move", "done"),
        ("run", "finished"),
    ]
    assert {r["run_id"] for r in records} == {result.run_id}


def test_explicit_destination_and_planned_rename(src: Path, area: Path) -> None:
    dest = area / "Sorted"
    touch(dest / "PDF", "a.pdf")
    touch(src, "a.pdf", "A.PDF.pdf")
    existing = fingerprint(dest / "PDF" / "a.pdf")
    before = fingerprints(src)
    plan = plan_for(src, dest)
    assert [(i.action, i.target.name) for i in plan.items] == [
        (PlanAction.RENAME, "a_1.pdf"),
        (PlanAction.MOVE, "A.PDF.pdf"),
    ]
    assert plan.directories_to_create == ()

    result = run(plan)

    assert [r.status for r in result.items] == [MOVED, MOVED]
    assert fingerprints(dest / "PDF") == {
        "a.pdf": existing,
        "a_1.pdf": before["a.pdf"],
        "A.PDF.pdf": before["A.PDF.pdf"],
    }
    assert result.directories_created == ()
    assert_journal_consistent(result)


def test_planned_skips_are_reported_and_journaled(src: Path, set_attributes) -> None:
    touch(src, "a.pdf", "hidden.pdf")
    set_attributes(src / "hidden.pdf", stat.FILE_ATTRIBUTE_HIDDEN)
    plan = plan_for(src)
    assert [i.action for i in plan.items] == [PlanAction.MOVE, PlanAction.SKIP]

    result = run(plan)

    assert outcome(result) == [
        ("a.pdf", MOVED, ExecReason.COMPLETED),
        ("hidden.pdf", SKIPPED, ExecReason.PLANNED_SKIP),
    ]
    assert result.complete
    assert (src / "hidden.pdf").exists()
    entries = assert_journal_consistent(result).entries
    assert [e.attempted for e in entries if e.operation == "move"] == [True, False]


def test_plan_without_moves_changes_nothing(src: Path, area: Path, snapshot: Snapshot) -> None:
    touch(src, "notes.txt")
    before = snapshot(area)
    result = run(plan_for(src))
    assert result.items == () and result.complete and not result.changed_anything
    assert snapshot(area) == before


# --- 2./20. existing targets are never overwritten -------------------------------------


@pytest.mark.parametrize("kind", ["same", "case", "directory", "junction"])
def test_target_created_before_execution_is_never_replaced(
    src: Path, area: Path, snapshot: Snapshot, make_junction, kind: str
) -> None:
    touch(src, "a.pdf", "b.pdf")
    before = fingerprints(src)
    plan = plan_for(src)
    category = src / "DEL" / "PDF"
    category.mkdir(parents=True)
    if kind == "same":
        (category / "a.pdf").write_bytes(b"not yours")
    elif kind == "case":
        (category / "A.PDF").write_bytes(b"not yours")
    elif kind == "directory":
        touch(category / "a.pdf", "inside.txt")
    else:
        (area / "elsewhere").mkdir()
        make_junction(category / "a.pdf", area / "elsewhere")
    taken = {k: v for k, v in snapshot(area).items() if k.startswith("Downloads\\DEL\\PDF\\a")}

    result = run(plan)

    assert outcome(result) == [
        ("a.pdf", SKIPPED, ExecReason.TARGET_EXISTS),
        ("b.pdf", MOVED, ExecReason.COMPLETED),
    ]
    assert result.partial and not result.complete
    after = snapshot(area)
    assert {k: v for k, v in after.items() if k in taken} == taken
    assert fingerprint(src / "a.pdf") == before["a.pdf"]
    assert fingerprint(category / "b.pdf") == before["b.pdf"]
    assert result.directories_created == ()  # both existed by then
    assert_journal_consistent(result)


# --- 3. target appears after the checks, right before the rename -----------------------


@pytest.mark.parametrize("kind", ["file", "case", "directory"])
def test_target_appearing_at_the_last_moment_is_never_replaced(
    src: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    touch(src, "a.pdf")
    before = fingerprint(src / "a.pdf")
    plan = plan_for(src)
    real = winfileops.rename_into

    def racing(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        intruder = src / "DEL" / "PDF" / (name.upper() if kind == "case" else name)
        if kind == "directory":
            intruder.mkdir()
        else:
            intruder.write_bytes(b"intruder")
        real(handle, directory, name)

    monkeypatch.setattr(winfileops, "rename_into", racing)
    result = run(plan)

    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.TARGET_EXISTS)]
    assert result.items[0].error_code in (winfileops.ERROR_ALREADY_EXISTS, 80)
    assert fingerprint(src / "a.pdf") == before
    [intruder] = (src / "DEL" / "PDF").iterdir()
    if kind == "directory":
        assert intruder.is_dir() and list(intruder.iterdir()) == []
    else:
        assert intruder.read_bytes() == b"intruder"
    report = assert_journal_consistent(result)
    [entry] = [e for e in report.entries if e.operation == "move"]
    assert entry.attempted and entry.state is OperationState.SKIPPED


# --- 4./5./6. the source no longer matches the preview ---------------------------------


@pytest.mark.parametrize("change", ["content", "mtime", "readonly", "hidden"])
def test_changed_source_is_skipped(src: Path, change: str) -> None:
    touch(src, "a.pdf", "b.pdf")
    path = src / "a.pdf"
    plan = plan_for(src)
    if change == "content":
        path.write_bytes(b"changed after the preview")
    elif change == "mtime":
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
    elif change == "readonly":
        path.chmod(stat.S_IREAD)
    else:
        subprocess.run(["attrib", "+h", str(path)], check=True, capture_output=True)
    changed = fingerprint(path)

    result = run(plan)

    expected = ExecReason.SOURCE_UNSAFE if change == "hidden" else ExecReason.SOURCE_CHANGED
    assert outcome(result)[0] == ("a.pdf", SKIPPED, expected)
    assert result.items[1].status is MOVED
    assert fingerprint(path) == changed
    assert not (src / "DEL" / "PDF" / "a.pdf").exists()
    path.chmod(stat.S_IWRITE)
    assert_journal_consistent(result)


def test_vanished_source_is_skipped(src: Path) -> None:
    touch(src, "a.pdf", "b.pdf")
    plan = plan_for(src)
    (src / "a.pdf").unlink()
    result = run(plan)
    assert outcome(result) == [
        ("a.pdf", SKIPPED, ExecReason.SOURCE_MISSING),
        ("b.pdf", MOVED, ExecReason.COMPLETED),
    ]
    assert sorted(p.name for p in (src / "DEL" / "PDF").iterdir()) == ["b.pdf"]


def test_source_replaced_by_identical_copy_is_skipped(src: Path) -> None:
    """Same name, content, size and mtime, but another file: the file id tells."""
    touch(src, "a.pdf")
    path = src / "a.pdf"
    old = path.stat()
    plan = plan_for(src)
    path.rename(src / "original.bin")
    path.write_bytes(b"content of a.pdf")
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    new = path.stat()
    assert (new.st_size, new.st_mtime_ns) == (old.st_size, old.st_mtime_ns)
    assert new.st_ino != old.st_ino

    result = run(plan)

    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.SOURCE_REPLACED)]
    assert fingerprint(path)[0] == new.st_ino
    assert fingerprint(src / "original.bin")[0] == old.st_ino
    assert list((src / "DEL" / "PDF").iterdir()) == []


def test_source_replaced_by_directory_is_skipped(src: Path) -> None:
    touch(src, "a.pdf")
    plan = plan_for(src)
    (src / "a.pdf").unlink()
    touch(src / "a.pdf", "inside.txt")
    result = run(plan)
    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.SOURCE_REPLACED)]
    assert (src / "a.pdf" / "inside.txt").is_file()


def test_source_in_use_by_a_writer_is_skipped(src: Path) -> None:
    touch(src, "a.pdf", "b.pdf")
    plan = plan_for(src)
    with (src / "a.pdf").open("ab"):
        result = run(plan)
    assert outcome(result)[0] == ("a.pdf", SKIPPED, ExecReason.SOURCE_IN_USE)
    assert result.items[0].error_code == winfileops.ERROR_SHARING_VIOLATION
    assert (src / "a.pdf").read_bytes() == b"content of a.pdf"
    assert result.items[1].status is MOVED


def test_source_cannot_change_while_it_is_being_moved(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the checks and the rename the opened file is locked against writers."""
    touch(src, "a.pdf")
    before = fingerprint(src / "a.pdf")
    plan = plan_for(src)
    real = winfileops.rename_into
    attempts: list[type[BaseException]] = []

    def meddle(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        for action in (
            lambda: (src / "a.pdf").open("ab"),
            lambda: (src / "a.pdf").unlink(),
            lambda: (src / "a.pdf").rename(src / "other.pdf"),
        ):
            with pytest.raises(PermissionError) as info:
                action()
            attempts.append(info.type)
        real(handle, directory, name)

    monkeypatch.setattr(winfileops, "rename_into", meddle)
    result = run(plan)
    assert len(attempts) == 3
    assert outcome(result) == [("a.pdf", MOVED, ExecReason.COMPLETED)]
    assert fingerprint(src / "DEL" / "PDF" / "a.pdf") == before


# --- 7./8. links and junctions ---------------------------------------------------------


def test_destination_replaced_by_junction_stops_before_any_change(
    src: Path, area: Path, snapshot: Snapshot, make_junction
) -> None:
    dest = area / "Sorted"
    dest.mkdir()
    touch(src, "a.pdf")
    plan = plan_for(src, dest)
    dest.rename(area / "Sorted_real")
    (area / "elsewhere").mkdir()
    make_junction(dest, area / "elsewhere")
    before = snapshot(area)

    result = run(plan)

    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.DESTINATION_CHANGED)]
    assert result.stopped is ExecReason.DESTINATION_CHANGED
    assert snapshot(area) == before
    assert list((area / "elsewhere").iterdir()) == []
    assert not any(e.operation == "mkdir" for e in journal_of(result).entries)


def test_destination_ancestor_replaced_by_junction(
    src: Path, area: Path, snapshot: Snapshot, make_junction
) -> None:
    touch(src, "a.pdf")
    plan = plan_for(src, area / "A" / "B")
    (area / "elsewhere").mkdir()
    make_junction(area / "A", area / "elsewhere")
    before = snapshot(area)
    result = run(plan)
    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.DESTINATION_CHANGED)]
    assert snapshot(area) == before


def test_source_directory_replaced_by_junction(
    src: Path, area: Path, snapshot: Snapshot, make_junction
) -> None:
    touch(src, "a.pdf")
    plan = plan_for(src, area / "Sorted")
    src.rename(area / "real")
    make_junction(src, area / "real")
    before = snapshot(area)
    result = run(plan)
    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.SOURCE_DIR_CHANGED)]
    assert snapshot(area) == before


def test_source_directory_replaced_by_a_copy(src: Path, area: Path) -> None:
    touch(src, "a.pdf")
    plan = plan_for(src, area / "Sorted")
    src.rename(area / "original")
    touch(src, "a.pdf")
    result = run(plan)
    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.SOURCE_REPLACED)]
    assert (src / "a.pdf").is_file() and (area / "original" / "a.pdf").is_file()


def test_category_directory_replaced_by_junction_before_execution(
    src: Path, area: Path, make_junction
) -> None:
    touch(src / "DEL" / "PDF")
    touch(src, "a.pdf", "song.mp3")
    plan = plan_for(src)
    (src / "DEL" / "PDF").rmdir()
    (area / "elsewhere").mkdir()
    make_junction(src / "DEL" / "PDF", area / "elsewhere")

    result = run(plan)

    assert outcome(result) == [
        ("a.pdf", SKIPPED, ExecReason.TARGET_DIR_INVALID),
        ("song.mp3", MOVED, ExecReason.COMPLETED),
    ]
    assert list((area / "elsewhere").iterdir()) == []
    assert (src / "a.pdf").is_file()
    assert_journal_consistent(result)


def test_category_directory_swapped_after_the_preflight_checks(
    src: Path, area: Path, make_junction, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handle walk sees the junction even if the path checks passed just before."""
    touch(src / "DEL" / "PDF")
    touch(src, "a.pdf")
    plan = plan_for(src)
    (area / "elsewhere").mkdir()
    real = executor.check_directory_chain
    swapped: list[Path] = []

    def check_then_swap(path: Path):
        chain = real(path)
        if path == src / "DEL" and not swapped:
            (path / "PDF").rename(path / "PDF_old")
            make_junction(path / "PDF", area / "elsewhere")
            swapped.append(path)
        return chain

    monkeypatch.setattr(executor, "check_directory_chain", check_then_swap)
    result = run(plan)

    assert swapped
    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.TARGET_DIR_INVALID)]
    assert list((area / "elsewhere").iterdir()) == []
    assert list((src / "DEL" / "PDF_old").iterdir()) == []
    assert (src / "a.pdf").is_file()


def test_held_directories_cannot_be_swapped_during_the_run(
    src: Path, area: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(src, "a.pdf", "b.pdf")
    plan = plan_for(src)
    real = winfileops.rename_into
    blocked: list[str] = []

    def try_swap(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        for path in (src / "DEL" / "PDF", src / "DEL", src):
            with pytest.raises(PermissionError):
                path.rename(path.with_name(path.name + "_moved"))
            blocked.append(path.name)
        real(handle, directory, name)

    monkeypatch.setattr(winfileops, "rename_into", try_swap)
    result = run(plan)
    assert blocked == ["PDF", "DEL", "Downloads"] * 2
    assert [r.status for r in result.items] == [MOVED, MOVED]
    assert sorted(p.name for p in (src / "DEL" / "PDF").iterdir()) == ["a.pdf", "b.pdf"]


def test_directory_created_concurrently_is_verified_and_used(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(src, "a.pdf")
    plan = plan_for(src)
    real = winfileops.open_relative

    def racing(parent, name, **kwargs):
        if kwargs.get("create") and name == "PDF":
            (src / "DEL" / "PDF").mkdir()
            (src / "DEL" / "PDF" / "theirs.txt").write_bytes(b"theirs")
        return real(parent, name, **kwargs)

    monkeypatch.setattr(winfileops, "open_relative", racing)
    result = run(plan)

    assert outcome(result) == [("a.pdf", MOVED, ExecReason.COMPLETED)]
    assert result.directories_created == (src / "DEL",)
    assert (src / "DEL" / "PDF" / "theirs.txt").read_bytes() == b"theirs"
    mkdirs = [e for e in journal_of(result).entries if e.operation == "mkdir"]
    assert [e.outcome["reason"] for e in mkdirs if e.outcome] == ["created", "already_existed"]


def test_junction_created_concurrently_instead_of_a_directory_is_refused(
    src: Path, area: Path, make_junction, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(src, "a.pdf")
    plan = plan_for(src)
    (area / "elsewhere").mkdir()
    real = winfileops.open_relative

    def racing(parent, name, **kwargs):
        if kwargs.get("create") and name == "PDF":
            make_junction(src / "DEL" / "PDF", area / "elsewhere")
        return real(parent, name, **kwargs)

    monkeypatch.setattr(winfileops, "open_relative", racing)
    result = run(plan)

    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.TARGET_DIR_INVALID)]
    assert list((area / "elsewhere").iterdir()) == []
    assert (src / "a.pdf").is_file()
    mkdir = [e for e in journal_of(result).entries if e.target == str(src / "DEL" / "PDF")]
    assert [e.state for e in mkdir] == [OperationState.FAILED]


def test_category_directory_removed_after_preview_is_not_recreated(src: Path) -> None:
    touch(src / "DEL" / "PDF")
    touch(src, "a.pdf")
    plan = plan_for(src)
    assert plan.directories_to_create == ()
    (src / "DEL" / "PDF").rmdir()
    result = run(plan)
    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.TARGET_DIR_MISSING)]
    assert not (src / "DEL" / "PDF").exists()


def test_destination_root_removed_after_preview_stops_the_run(src: Path, area: Path) -> None:
    touch(area / "Sorted")
    touch(src, "a.pdf")
    plan = plan_for(src, area / "Sorted")
    (area / "Sorted").rmdir()
    result = run(plan)
    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.TARGET_DIR_MISSING)]
    assert result.stopped is ExecReason.TARGET_DIR_MISSING
    assert not (area / "Sorted").exists()


def test_existing_file_where_a_directory_is_planned(src: Path) -> None:
    touch(src, "a.pdf")
    plan = plan_for(src)
    (src / "DEL").mkdir()
    (src / "DEL" / "PDF").write_bytes(b"a file named like the category")
    result = run(plan)
    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.TARGET_DIR_INVALID)]
    assert (src / "DEL" / "PDF").read_bytes() == b"a file named like the category"
    assert (src / "a.pdf").is_file()


@pytest.mark.parametrize("which", ["source", "root"])
def test_volume_checks(src: Path, monkeypatch: pytest.MonkeyPatch, which: str) -> None:
    touch(src, "a.pdf")
    plan = plan_for(src)
    real = executor.file_identity
    calls: list[object] = []

    def fake(st: object) -> tuple[int, int] | None:
        calls.append(st)
        identity = real(st)
        if which == "source" and len(calls) == 1:
            return None
        if which == "root" and len(calls) == 2 and identity:
            return (identity[0] + 1, identity[1])
        return identity

    monkeypatch.setattr(executor, "file_identity", fake)
    result = run(plan)
    reason = ExecReason.VOLUME_UNKNOWN if which == "source" else ExecReason.CROSS_VOLUME
    assert outcome(result) == [("a.pdf", SKIPPED, reason)]
    assert not (src / "DEL").exists()


# --- 9.-12., 18., 19. forged and invalid plans -----------------------------------------


@pytest.fixture
def valid(src: Path, area: Path) -> Plan:
    touch(src, "a.pdf", "b.pdf", "song.mp3")
    return plan_for(src, area / "Sorted")


def _invalid_plans(plan: Plan, src: Path, area: Path) -> dict[str, object]:
    a, b, song = plan.items
    root = plan.destination_root
    assert root is not None
    pdf = root / "PDF"
    return {
        "not a plan": "plan",
        "none": None,
        "errors": forge(plan, errors=("boom",)),
        "items not a tuple": forge(plan, items=list(plan.items)),
        "item not an item": forge(plan, items=(a, "b")),
        "relative source": forge(plan, source=Path("Downloads")),
        "unnormalized source": forge(plan, source=src / "x" / ".."),
        "root is source": forge(plan, destination_root=src),
        "default root not DEL": forge(plan, explicit_destination=False),
        "explicit not bool": forge(plan, explicit_destination=1),
        "bad policy": forge(plan, policy="overwrite"),
        "duplicate source": forge(plan, items=(a, forge(b, source=a.source))),
        "duplicate source case": forge(
            plan, items=(a, forge(b, source=src / "A.PDF", target=pdf / "A.PDF"))
        ),
        "duplicate target": forge(plan, items=(a, forge(b, target=a.target, action="rename"))),
        "duplicate target case": forge(
            plan, items=(a, forge(b, target=pdf / "A.PDF", action=PlanAction.RENAME))
        ),
        "self move": forge(plan, items=(forge(a, target=a.source),)),
        "target outside root": forge(plan, items=(forge(a, target=area / "elsewhere" / "a.pdf"),)),
        "target with dotdot": forge(plan, items=(forge(a, target=pdf / ".." / "a.pdf"),)),
        "target in root": forge(plan, items=(forge(a, target=root / "a.pdf"),)),
        "wrong category folder": forge(plan, items=(forge(a, target=root / "Audio" / "a.pdf"),)),
        "unknown category": forge(plan, items=(forge(a, category_key="Secret"),)),
        "category of other extension": forge(
            plan, items=(forge(a, category_key="Audio", target=root / "Audio" / "a.pdf"),)
        ),
        "extension mismatch": forge(plan, items=(forge(a, extension=".mp3"),)),
        "target extension changed": forge(
            plan, items=(forge(a, target=pdf / "a.exe", action=PlanAction.RENAME),)
        ),
        "move that renames": forge(plan, items=(forge(a, target=pdf / "x.pdf"),)),
        "rename that keeps": forge(plan, items=(forge(a, action=PlanAction.RENAME),)),
        "source not in source dir": forge(plan, items=(forge(a, source=area / "a.pdf"),)),
        "unknown action": forge(plan, items=(forge(a, action="delete"),)),
        "skip without reason": forge(plan, items=(forge(a, action=PlanAction.SKIP),)),
        "move with reason": forge(plan, items=(forge(a, reason="hidden"),)),
        "incomplete snapshot": forge(plan, items=(forge(a, file=forge(a.file, size=None)),)),
        "bool mtime": forge(plan, items=(forge(a, file=forge(a.file, mtime_ns=True)),)),
        "negative size": forge(plan, items=(forge(a, file=forge(a.file, size=-1)),)),
        "zero file id": forge(plan, items=(forge(a, file=forge(a.file, file_id=0)),)),
        "no snapshot": forge(plan, items=(forge(a, file=None),)),
        "unused directory": forge(
            plan, directories_to_create=(*plan.directories_to_create, root / "Video")
        ),
        "directory outside root": forge(plan, directories_to_create=(area / "other",)),
        "nested directory": forge(plan, directories_to_create=(pdf / "sub",)),
        "duplicate directory": forge(plan, directories_to_create=(root, root, pdf, root / "Audio")),
        "children first": forge(plan, directories_to_create=(pdf, root / "Audio", root)),
        "skipped level": forge(plan, directories_to_create=(area, pdf, root / "Audio")),
        "root without categories": forge(plan, directories_to_create=(root, pdf)),
        "source ancestor": forge(plan, directories_to_create=(area, root, pdf, root / "Audio")),
        "directories without moves": forge(
            plan, items=(forge(a, action=PlanAction.SKIP, reason="hidden"),)
        ),
        "directory not a path": forge(plan, directories_to_create=(str(root),)),
        "unsafe category folder": forge(
            plan, items=(forge(a, target=root / "con" / "a.pdf"),), directories_to_create=()
        ),
        "song in pdf": forge(plan, items=(forge(song, target=pdf / "song.mp3"),)),
    }


def test_planner_output_is_valid(valid: Plan, area: Path) -> None:
    assert validate_plan(valid, TABLE) == ()
    assert valid.directories_to_create == (
        area / "Sorted",
        area / "Sorted" / "Audio",
        area / "Sorted" / "PDF",
    )


def test_every_forged_plan_is_detected(valid: Plan, src: Path, area: Path) -> None:
    for name, plan in _invalid_plans(valid, src, area).items():
        assert validate_plan(plan, TABLE), name


def test_invalid_plans_change_nothing(
    valid: Plan, src: Path, area: Path, snapshot: Snapshot, isolated_user_dirs: Path
) -> None:
    before = snapshot(area)
    logs_before = snapshot(isolated_user_dirs)
    for name, plan in _invalid_plans(valid, src, area).items():
        result = run(plan)
        assert result.stopped is ExecReason.PLAN_INVALID, name
        assert result.problems, name
        assert not result.changed_anything, name
        assert result.journal_path is None, name
        assert all(r.status is SKIPPED for r in result.items), name
        assert all(r.reason is ExecReason.PLAN_INVALID for r in result.items), name
    assert snapshot(area) == before
    assert snapshot(isolated_user_dirs) == logs_before  # not even a journal


def test_validate_plan_never_raises_on_garbage(valid: Plan) -> None:
    class Weird(Path):
        def __str__(self) -> str:
            raise RuntimeError("weird path")

    plan = forge(valid, source=object.__new__(Weird))
    assert validate_plan(plan, TABLE)
    assert validate_plan(valid, "table")  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["file_id", "device"])
def test_missing_ids_are_skipped_without_touching_the_file(
    valid: Plan, src: Path, field: str
) -> None:
    a, b, song = valid.items
    plan = forge(valid, items=(forge(a, file=forge(a.file, **{field: None})), b, song))
    assert validate_plan(plan, TABLE) == ()
    before = fingerprint(src / "a.pdf")
    result = run(plan)
    assert outcome(result)[0] == ("a.pdf", SKIPPED, ExecReason.METADATA_UNKNOWN)
    assert [r.status for r in result.items[1:]] == [MOVED, MOVED]
    assert fingerprint(src / "a.pdf") == before


def test_snapshot_from_another_volume_is_refused(valid: Plan, src: Path) -> None:
    a, b, song = valid.items
    plan = forge(valid, items=(forge(a, file=forge(a.file, device=a.file.device + 1)), b, song))
    result = run(plan)
    assert outcome(result)[0] == ("a.pdf", SKIPPED, ExecReason.SOURCE_REPLACED)
    assert (src / "a.pdf").is_file()


def test_reserved_names_are_skipped(valid: Plan, src: Path) -> None:
    a, b, song = valid.items
    root = valid.destination_root
    assert root is not None
    plan = forge(valid, items=(forge(a, target=root / "PDF" / "con.pdf", action="rename"), b, song))
    plan = forge(plan, items=tuple(forge(i, action=PlanAction(i.action)) for i in plan.items))
    assert validate_plan(plan, TABLE) == ()
    result = run(plan)
    assert outcome(result)[0] == ("a.pdf", SKIPPED, ExecReason.UNSAFE_NAME)
    assert (src / "a.pdf").is_file()


def test_unsupported_platform_changes_nothing(
    valid: Plan, area: Path, snapshot: Snapshot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(winfileops, "AVAILABLE", False)
    before = snapshot(area)
    result = run(valid)
    assert {r.reason for r in result.items} == {ExecReason.UNSUPPORTED_PLATFORM}
    assert result.journal_path is None
    assert snapshot(area) == before


# --- 13./14. filesystem errors ---------------------------------------------------------


def test_permission_error_creating_a_directory(
    src: Path, area: Path, snapshot: Snapshot, deny: Callable[[Path, str], None]
) -> None:
    touch(src, "a.pdf", "song.mp3")
    plan = plan_for(src)
    deny(src, "AD")  # no new subdirectories in the source directory
    before = snapshot(area)

    result = run(plan)

    assert [(r.status, r.reason) for r in result.items] == [
        (FAILED, ExecReason.TARGET_DIR_FAILED),
        (FAILED, ExecReason.TARGET_DIR_FAILED),
    ]
    assert result.items[0].error_code == 5
    assert snapshot(area) == before
    assert result.directories_created == ()
    mkdir = [e for e in journal_of(result).entries if e.operation == "mkdir"]
    assert [(e.target, e.state) for e in mkdir] == [(str(src / "DEL"), OperationState.FAILED)]


def test_permission_error_creating_a_category_directory(
    src: Path, deny: Callable[[Path, str], None]
) -> None:
    touch(src / "DEL" / "Audio")
    touch(src, "a.pdf", "song.mp3")
    plan = plan_for(src)
    deny(src / "DEL", "AD")
    result = run(plan)
    assert outcome(result) == [
        ("a.pdf", FAILED, ExecReason.TARGET_DIR_FAILED),
        ("song.mp3", MOVED, ExecReason.COMPLETED),
    ]
    assert (src / "a.pdf").is_file() and not (src / "DEL" / "PDF").exists()


@pytest.mark.parametrize(
    ("winerror", "reason"),
    [(5, ExecReason.ACCESS_DENIED), (17, ExecReason.CROSS_VOLUME), (1117, ExecReason.MOVE_FAILED)],
)
def test_rename_error_leaves_the_file_and_continues(
    src: Path, monkeypatch: pytest.MonkeyPatch, winerror: int, reason: ExecReason
) -> None:
    touch(src, "a.pdf", "b.pdf")
    before = fingerprints(src)
    plan = plan_for(src)
    real = winfileops.rename_into

    def failing(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        if name == "a.pdf":
            raise OSError(0, "injected", str(src / name), winerror)
        real(handle, directory, name)

    monkeypatch.setattr(winfileops, "rename_into", failing)
    result = run(plan)

    assert outcome(result) == [("a.pdf", FAILED, reason), ("b.pdf", MOVED, ExecReason.COMPLETED)]
    assert result.items[0].error_code == winerror
    assert fingerprint(src / "a.pdf") == before["a.pdf"]
    assert fingerprint(src / "DEL" / "PDF" / "b.pdf") == before["b.pdf"]
    assert result.partial
    assert_journal_consistent(result)


def test_error_after_the_rename_happened_requires_recovery(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(src, "a.pdf", "b.pdf")
    plan = plan_for(src)
    real = winfileops.rename_into

    def lying(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        real(handle, directory, name)
        raise OSError(0, "reported failure after success", None, 1117)

    monkeypatch.setattr(winfileops, "rename_into", lying)
    result = run(plan)

    assert outcome(result) == [
        ("a.pdf", RECOVERY, ExecReason.MOVE_FAILED),
        ("b.pdf", CANCELLED, ExecReason.RUN_ABORTED),
    ]
    assert "target" in (result.items[0].detail or "")
    assert (src / "DEL" / "PDF" / "a.pdf").is_file() and (src / "b.pdf").is_file()
    assert not result.complete and result.partial
    assert assert_journal_consistent(result).recovery_needed


class _Unprintable(RuntimeError):
    def __str__(self) -> str:
        raise ValueError("cannot describe the error")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RuntimeError("unexpected after the rename"), ExecReason.UNEXPECTED_ERROR),
        # The per-file handler fails itself: the run's abort sees the unanswered intent.
        (_Unprintable(), ExecReason.RUN_ABORTED),
    ],
    ids=["per-file handler", "run abort"],
)
def test_unexpected_error_after_a_real_rename_is_never_reported_as_not_moved(
    src: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, reason: ExecReason
) -> None:
    """The intent of the move is unanswered: the outcome must stay unknown, never
    cancelled, failed or moved."""
    touch(src, "a.pdf", "b.pdf")
    plan = plan_for(src)
    real = winfileops.rename_into

    def broken(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        real(handle, directory, name)
        raise error

    monkeypatch.setattr(winfileops, "rename_into", broken)
    result = run(plan)

    assert outcome(result) == [
        ("a.pdf", RECOVERY, reason),
        ("b.pdf", CANCELLED, ExecReason.RUN_ABORTED),
    ]
    assert result.stopped is ExecReason.RUN_ABORTED
    assert (src / "DEL" / "PDF" / "a.pdf").is_file() and (src / "b.pdf").is_file()
    assert not result.complete and result.partial
    report = journal_of(result)
    moves = [(Path(e.source).name, e.state, e.attempted) for e in report.entries if e.source]
    if reason is ExecReason.RUN_ABORTED:  # nothing more is written once the run aborts
        assert not report.finished
        assert moves == [("a.pdf", OperationState.UNCERTAIN, True)]  # never a false DONE
    else:
        assert_journal_consistent(result)
        assert moves[0] == ("a.pdf", OperationState.RECOVERY_REQUIRED, True)
    assert report.recovery_needed


def test_file_not_found_at_target_after_rename_requires_recovery(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(src, "a.pdf", "b.pdf")
    plan = plan_for(src)
    real = winfileops.rename_into

    def elsewhere(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        real(handle, directory, "unexpected.pdf")

    monkeypatch.setattr(winfileops, "rename_into", elsewhere)
    result = run(plan)
    assert outcome(result) == [
        ("a.pdf", RECOVERY, ExecReason.VERIFY_FAILED),
        ("b.pdf", CANCELLED, ExecReason.RUN_ABORTED),
    ]
    assert (src / "DEL" / "PDF" / "unexpected.pdf").is_file()


# --- 15. journal failures --------------------------------------------------------------


def test_journal_cannot_be_created(valid: Plan, area: Path, snapshot: Snapshot) -> None:
    def broken() -> Journal:
        raise JournalError("disk full")

    before = snapshot(area)
    result = run(valid, journal=broken)
    assert {(r.status, r.reason) for r in result.items} == {(CANCELLED, ExecReason.JOURNAL_FAILED)}
    assert result.stopped is ExecReason.JOURNAL_FAILED and result.problems
    assert snapshot(area) == before


def test_journal_directory_blocked_by_a_file(
    valid: Plan, area: Path, snapshot: Snapshot, isolated_user_dirs: Path
) -> None:
    app = journal.default_log_dir().parent
    app.mkdir(parents=True)
    (app / "logs").write_bytes(b"not a directory")
    before = snapshot(area)
    result = run(valid)
    assert {r.reason for r in result.items} == {ExecReason.JOURNAL_FAILED}
    assert snapshot(area) == before


# Records of a run that creates DEL and DEL/PDF and moves a.pdf and b.pdf:
#   1 run started | 2-3 mkdir DEL | 4-5 mkdir PDF | 6-7 move a | 8-9 move b | 10 run finished
@pytest.mark.parametrize("failing", range(1, 11))
def test_journal_write_failure_stops_all_further_changes(
    src: Path, monkeypatch: pytest.MonkeyPatch, failing: int
) -> None:
    touch(src, "a.pdf", "b.pdf")
    before = fingerprints(src)
    plan = plan_for(src)
    calls = 0
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == failing:
            raise OSError(5, "injected fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(journal.os, "fsync", fsync)
    result = run(plan)
    monkeypatch.undo()

    assert calls == failing  # nothing more was attempted after the failure
    assert result.stopped is ExecReason.JOURNAL_FAILED
    assert result.problems and not result.complete
    assert result.journal_path is not None
    records = read_journal(result.journal_path).records
    assert len(records) == failing  # the failing record reached the file, nothing after
    intents = {(r["operation"], r["target"]) for r in records if r["status"] == "intent"}
    # Every change on disk was preceded by a written intent.
    for directory in result.directories_created:
        assert ("mkdir", str(directory)) in intents
    for name, fp in before.items():
        target = src / "DEL" / "PDF" / name
        item = next(r for r in result.items if r.item.source.name == name)
        if target.exists():
            assert ("move", str(target)) in intents
            assert fingerprint(target) == fp
            assert item.status is MOVED  # a confirmed move stays reported as moved
        else:
            assert fingerprint(src / name) == fp
            assert item.status is CANCELLED
    moved = sum(r.status is MOVED for r in result.items)
    assert moved == {7: 1, 8: 1, 9: 2, 10: 2}.get(failing, 0)
    assert (src / "DEL").exists() == (failing >= 3)


# --- 16./17. partial runs and cancellation ---------------------------------------------


def test_unexpected_error_mid_run_keeps_earlier_results(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(src, "a.pdf", "b.pdf", "c.pdf", "d.pdf")
    before = fingerprints(src)
    plan = plan_for(src)
    real = winfileops.rename_into
    calls = 0

    def flaky(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("bug")
        real(handle, directory, name)

    monkeypatch.setattr(winfileops, "rename_into", flaky)
    result = run(plan)

    assert outcome(result) == [
        ("a.pdf", MOVED, ExecReason.COMPLETED),
        ("b.pdf", MOVED, ExecReason.COMPLETED),
        ("c.pdf", RECOVERY, ExecReason.UNEXPECTED_ERROR),
        ("d.pdf", CANCELLED, ExecReason.RUN_ABORTED),
    ]
    assert result.counts[MOVED] == 2 and result.counts[CANCELLED] == 1
    assert result.partial and not result.complete
    assert result.stopped is ExecReason.RUN_ABORTED
    assert fingerprints(src / "DEL" / "PDF") == {k: before[k] for k in ("a.pdf", "b.pdf")}
    assert fingerprints(src) == {k: before[k] for k in ("c.pdf", "d.pdf")}
    assert_journal_consistent(result)


def test_errors_on_some_files_do_not_stop_the_others(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(src, "a.pdf", "b.pdf", "c.pdf")
    plan = plan_for(src)
    (src / "b.pdf").unlink()
    result = run(plan)
    assert [r.status for r in result.items] == [MOVED, SKIPPED, MOVED]
    assert result.counts == {
        MOVED: 2,
        SKIPPED: 1,
        FAILED: 0,
        CANCELLED: 0,
        RECOVERY: 0,
    }
    assert result.partial and not result.complete


def test_cancel_between_files(src: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    touch(src, "a.pdf", "b.pdf", "c.pdf", "d.pdf")
    plan = plan_for(src)
    events: list[str] = []
    real = winfileops.rename_into

    def rename(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        events.append(f"rename {name}")
        real(handle, directory, name)
        events.append("renamed")

    def cancel() -> bool:
        events.append("cancel?")
        return events.count("cancel?") == 3

    monkeypatch.setattr(winfileops, "rename_into", rename)
    result = run(plan, cancel=cancel)

    assert events == [
        "cancel?", "rename a.pdf", "renamed",
        "cancel?", "rename b.pdf", "renamed",
        "cancel?",
    ]  # fmt: skip
    assert outcome(result) == [
        ("a.pdf", MOVED, ExecReason.COMPLETED),
        ("b.pdf", MOVED, ExecReason.COMPLETED),
        ("c.pdf", CANCELLED, ExecReason.CANCELLED),
        ("d.pdf", CANCELLED, ExecReason.CANCELLED),
    ]
    assert result.stopped is ExecReason.CANCELLED and result.partial
    assert sorted(p.name for p in src.iterdir() if p.is_file()) == ["c.pdf", "d.pdf"]
    report = assert_journal_consistent(result)
    assert [e.attempted for e in report.entries if e.operation == "move"] == [
        True,
        True,
        False,
        False,
    ]


def test_cancel_before_the_first_file_changes_nothing(src: Path) -> None:
    touch(src, "a.pdf")
    result = run(plan_for(src), cancel=lambda: True)
    assert outcome(result) == [("a.pdf", CANCELLED, ExecReason.CANCELLED)]
    assert not (src / "DEL").exists()
    assert not result.changed_anything and not result.complete


def test_broken_cancel_callback_cancels(src: Path) -> None:
    def cancel() -> bool:
        raise RuntimeError("ui gone")

    touch(src, "a.pdf")
    result = run(plan_for(src), cancel=cancel)
    assert outcome(result) == [("a.pdf", CANCELLED, ExecReason.CANCELLED)]
    assert any("cancel callback failed" in p for p in result.problems)


def test_execute_plan_never_raises(src: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    touch(src, "a.pdf", "b.pdf")
    plan = plan_for(src)
    calls = 0
    real = executor._Run.cancel_requested

    def explode(self: executor._Run) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("internal bug")
        return real(self)

    monkeypatch.setattr(executor._Run, "cancel_requested", explode)
    result = run(plan)
    assert outcome(result) == [
        ("a.pdf", MOVED, ExecReason.COMPLETED),
        ("b.pdf", CANCELLED, ExecReason.RUN_ABORTED),
    ]
    assert result.stopped is ExecReason.RUN_ABORTED and result.partial


def test_handles_are_released_after_the_run(src: Path) -> None:
    touch(src, "a.pdf")
    result = run(plan_for(src))
    assert result.complete
    (src / "DEL" / "PDF").rename(src / "DEL" / "Renamed")  # not held any more
    (src / "DEL").rename(src / "DEL2")
    src.rename(src.with_name("Downloads2"))


def test_result_is_complete_only_for_full_success(src: Path) -> None:
    touch(src, "a.pdf")
    plan = plan_for(src)
    result = run(plan)
    assert result.complete and result.changed_anything and not result.partial
    partial = dataclasses.replace(result, problems=("x",))
    assert not partial.complete and partial.partial
    item = dataclasses.replace(result.items[0], status=SKIPPED, reason=ExecReason.TARGET_EXISTS)
    unchanged = dataclasses.replace(result, items=(item,), directories_created=())
    assert not unchanged.complete and not unchanged.partial and not unchanged.changed_anything


def test_file_info_snapshot_is_compared_through_the_handle(src: Path) -> None:
    """A plan built from a stale snapshot (other mtime) is refused, not trusted."""
    touch(src, "a.pdf")
    plan = plan_for(src)
    [a] = plan.items
    stale = FileInfo(
        a.file.size, a.file.mtime_ns - 1, a.file.attributes, a.file.file_id, a.file.device
    )
    result = run(dataclasses.replace(plan, items=(dataclasses.replace(a, file=stale),)))
    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.SOURCE_CHANGED)]
    assert isinstance(a, PlanItem)


# --- handle ownership (R3A-FIX-01 / EXEC-01) -----------------------------------------


def executor_query(
    monkeypatch: pytest.MonkeyPatch,
    fail: Callable[[winfileops.Handle, int], bool],
    error: Exception,
) -> list[winfileops.Handle]:
    """Make ``winfileops.query`` raise ``error`` for the executor's calls ``fail`` selects."""
    real = winfileops.query
    calls: list[winfileops.Handle] = []

    def query(handle: winfileops.Handle) -> winfileops.HandleInfo:
        if sys._getframe(1).f_globals.get("__name__") == executor.__name__:
            calls.append(handle)
            if fail(handle, len(calls)):
                raise error
        return real(handle)

    monkeypatch.setattr(winfileops, "query", query)
    return calls


def tree(root: Path) -> list[tuple[int, str]]:
    """(file id, sha256) of every file below ``root``, wherever it is."""
    return sorted(fingerprint(p) for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize(
    "error", [OSError(5, "injected"), RuntimeError("injected")], ids=["OSError", "RuntimeError"]
)
def test_source_handle_is_released_when_its_query_fails(
    src: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    plan = plan_for(src)
    seen: list[int] = []

    def second_query_of_the_source(handle: winfileops.Handle, number: int) -> bool:
        if handle.path != src:
            return False
        seen.append(number)
        return len(seen) == 2  # the first is the walk's check, the second check_run's

    with monkeypatch.context() as mp:
        executor_query(mp, second_query_of_the_source, error)
        result = run(plan)
    assert len(seen) == 2
    assert outcome(result) == [
        ("a.pdf", SKIPPED, ExecReason.RUN_ABORTED),
        ("b.jpg", SKIPPED, ExecReason.RUN_ABORTED),
    ]
    assert result.stopped is ExecReason.RUN_ABORTED and "injected" in (result.stop_detail or "")
    assert not result.changed_anything and result.directories_created == ()
    assert fingerprints(src) == before and not (src / "DEL").exists()
    report = journal_of(result)
    assert report.finished and not any(e.attempted for e in report.entries)
    moved = src.rename(src.with_name("Downloads2"))  # the source handle was closed
    for path in moved.iterdir():
        path.unlink()
    moved.rmdir()


@pytest.mark.parametrize(
    "error", [OSError(5, "injected"), RuntimeError("injected")], ids=["OSError", "RuntimeError"]
)
def test_every_handle_is_released_whichever_query_fails(
    area: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def case(name: str) -> tuple[Path, Plan]:
        source = area / name / "Downloads"
        touch(source, "a.pdf", "b.jpg", "c.pdf")
        return source, plan_for(source)

    with monkeypatch.context() as mp:
        clean = executor_query(mp, lambda handle, number: False, error)
        source, plan = case("clean")
        assert run(plan).complete
    total = len(clean)
    assert total >= 8, total  # walk, check_run, category directories, files, probes
    for number in range(1, total + 1):
        source, plan = case(f"fail{number}")
        before = tree(source)
        with monkeypatch.context() as mp:
            executor_query(mp, lambda handle, n, k=number: n == k, error)
            result = run(plan)
        label = f"query {number} of {total}"
        assert not result.complete, label
        assert tree(source) == before, label  # nothing lost or duplicated
        assert result.journal_path is not None
        report = read_journal(result.journal_path)
        done = {e.source for e in report.entries if e.state is OperationState.DONE}
        moved = {str(r.item.source) for r in result.items if r.status is MOVED}
        assert {s for s in done if s and s.endswith((".pdf", ".jpg"))} == moved, label
        renamed = source.parent.rename(area / f"released{number}")  # no handle below it
        for path in sorted(renamed.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        renamed.rmdir()


def test_journal_is_closed_when_the_run_cannot_be_set_up(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(src, "a.pdf")
    plan = plan_for(src)
    created: list[Journal] = []

    def create() -> Journal:
        created.append(Journal.create())
        return created[0]

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected")

    monkeypatch.setattr(executor._Run, "__init__", broken)
    with pytest.raises(RuntimeError, match="injected"):
        run(plan, journal=create)
    (log,) = created
    assert log.path is not None
    log.path.parent.rename(log.path.parent.with_name("moved"))  # directory released
    assert (src / "a.pdf").exists()


@pytest.mark.parametrize(
    ("setup_error", "close_error", "raised"),
    [
        (RuntimeError("injected"), KeyboardInterrupt(), "close"),
        (KeyboardInterrupt(), OSError(5, "close failed"), "setup"),
        (SystemExit(3), KeyboardInterrupt(), "setup"),
    ],
    ids=["error-then-interrupt", "interrupt-then-error", "interrupt-then-interrupt"],
)
def test_failed_setup_and_failed_journal_close_never_drop_an_interrupt(
    src: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup_error: BaseException,
    close_error: BaseException,
    raised: str,
) -> None:
    touch(src, "a.pdf")
    plan = plan_for(src)
    created: list[Journal] = []

    def create() -> Journal:
        created.append(Journal.create())
        return created[0]

    def broken(*args: object, **kwargs: object) -> None:
        raise setup_error

    real_close = Journal.close

    def close(self: Journal) -> None:
        real_close(self)  # really released, then the close "fails"
        raise close_error

    monkeypatch.setattr(executor._Run, "__init__", broken)
    monkeypatch.setattr(Journal, "close", close)
    with pytest.raises(BaseException) as info:
        run(plan, journal=create)
    if raised == "close":  # the interrupt wins; the original error is its cause
        assert info.value is close_error
        assert info.value.__cause__ is setup_error
        assert winfileops.release_failures(close_error) == [setup_error]  # as an object too
    else:  # the first interrupt stays the one raised
        assert info.value is setup_error
        assert winfileops.release_failures(setup_error) == [close_error]
    assert any("then closing the journal failed" in n for n in setup_error.__notes__)
    (log,) = created
    assert log.path is not None
    log.path.parent.rename(log.path.parent.with_name("moved"))  # directory released
    assert (src / "a.pdf").exists()


# --- verified journal volume and checked handle release (R3A-FIX-03) ------------------


@pytest.mark.parametrize("mapping", ["subst", "undefined", "another volume during the check"])
def test_journal_on_an_unproven_drive_letter_stops_before_any_move(
    src: Path, monkeypatch: pytest.MonkeyPatch, snapshot: Snapshot, mapping: str
) -> None:
    """The default journal (%LOCALAPPDATA%) is on a drive letter that is not proven."""
    touch(src, "a.pdf", "b.jpg")
    plan = plan_for(src)
    before = snapshot(src)
    local = Path(os.environ["LOCALAPPDATA"])
    (device,) = winfileops.query_dos_device(local.drive)
    real = winfileops.query_dos_device
    answers: list[object] = {
        "subst": [("\\??\\" + str(src),)],
        "undefined": [FileNotFoundError(2, "undefined")],
        "another volume during the check": [(device,), ("\\Device\\HarddiskVolume999",)],
    }[mapping]

    def query(name: str) -> tuple[str, ...]:
        if name.upper() != local.drive.upper():
            return real(name)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer  # type: ignore[return-value]

    monkeypatch.setattr(winfileops, "query_dos_device", query)
    result = run(plan)
    assert outcome(result) == [
        ("a.pdf", CANCELLED, ExecReason.JOURNAL_FAILED),
        ("b.jpg", CANCELLED, ExecReason.JOURNAL_FAILED),
    ]
    assert result.stopped is ExecReason.JOURNAL_FAILED and result.journal_path is None
    assert not result.changed_anything and not result.complete
    assert snapshot(src) == before
    assert not (local / "DownloadsOrganizer").exists()


def test_failed_release_of_a_file_handle_stops_the_run_and_keeps_the_move(
    src: Path, close_failures
) -> None:
    touch(src, "a.pdf", "b.jpg", "c.pdf")
    plan = plan_for(src)
    close_failures.select = lambda handle: handle.path == src / "a.pdf"
    result = run(plan)
    assert outcome(result) == [
        ("a.pdf", MOVED, ExecReason.COMPLETED),  # moved and verified: stays MOVED
        ("b.jpg", CANCELLED, ExecReason.CLEANUP_FAILED),
        ("c.pdf", CANCELLED, ExecReason.CLEANUP_FAILED),
    ]
    assert result.stopped is ExecReason.CLEANUP_FAILED
    (problem,) = result.cleanup_problems
    assert "a.pdf" in problem and "CloseHandle failed" in problem
    assert result.changed_anything and result.partial and not result.complete
    assert (src / "DEL" / "PDF" / "a.pdf").is_file() and not (src / "a.pdf").exists()
    assert (src / "b.jpg").exists() and (src / "c.pdf").exists()
    report = assert_journal_consistent(result)
    cancelled = [e for e in report.entries if e.state is OperationState.CANCELLED]
    assert len(cancelled) == 2
    with pytest.raises(PermissionError):
        (src / "DEL" / "PDF" / "a.pdf").read_bytes()  # the handle really is still open
    close_failures.really_close()
    assert (src / "DEL" / "PDF" / "a.pdf").read_bytes() == b"content of a.pdf"
    (src / "DEL" / "PDF" / "a.pdf").unlink()


def test_failed_release_of_a_directory_handle_is_reported_after_the_run(
    src: Path, close_failures
) -> None:
    touch(src, "a.pdf", "b.jpg")
    close_failures.select = lambda handle: handle.path == src / "DEL" / "PDF"
    result = run(plan_for(src))
    assert [s for _, s, _ in outcome(result)] == [MOVED, MOVED]
    (problem,) = result.cleanup_problems
    assert str(src / "DEL" / "PDF") in problem
    assert not result.complete and result.partial and not result.problems
    assert_journal_consistent(result)
    with pytest.raises(PermissionError):
        (src / "DEL" / "PDF").rename(src / "DEL" / "Renamed")  # still held
    close_failures.really_close()
    (src / "DEL" / "PDF").rename(src / "DEL" / "Renamed")


def test_failed_release_of_the_journal_is_reported_with_the_results(
    src: Path, close_failures
) -> None:
    touch(src, "a.pdf", "b.jpg")
    logs = Path(os.environ["LOCALAPPDATA"]) / "DownloadsOrganizer" / "logs"
    close_failures.select = lambda handle: handle.path == logs
    result = run(plan_for(src))
    assert [s for _, s, _ in outcome(result)] == [MOVED, MOVED]  # the results are kept
    (problem,) = result.cleanup_problems
    assert problem.startswith("cannot close journal") and "CloseHandle failed" in problem
    assert not result.complete and result.stopped is None
    assert_journal_consistent(result)  # the journal itself was written completely
    close_failures.really_close()


def test_failed_release_while_probing_for_the_file_is_reported(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    """The probe handles of ``locate`` (after the rename) are released and checked too."""
    touch(src, "a.pdf", "b.jpg")
    plan = plan_for(src)
    probes = 0

    def probe(handle: winfileops.Handle) -> bool:
        nonlocal probes
        if handle.path == src / "DEL" / "PDF" / "a.pdf":
            probes += 1
            return probes == 1  # the probe handle, not the moved one (opened at the source)
        return False

    close_failures.select = probe
    result = run(plan)
    assert outcome(result) == [
        ("a.pdf", MOVED, ExecReason.COMPLETED),
        ("b.jpg", CANCELLED, ExecReason.CLEANUP_FAILED),
    ]
    assert len(result.cleanup_problems) == 1 and not result.complete
    close_failures.really_close()


# --- proven volume roots of the source and the destination (R3A-FIX-04 / EXEC-02) -----

V999 = "\\Device\\HarddiskVolume999"
NETWORK = "\\Device\\LanmanRedirector\\;Z:0000000000012345\\server\\share"


def fake_mapping(
    monkeypatch: pytest.MonkeyPatch, drive: str, answers: dict[int, object]
) -> list[int]:
    """The n-th ``query_dos_device(drive)`` (counted from now) answers ``answers[n]``
    (raised if an exception), the real mapping otherwise; returns the calls so far."""
    real = winfileops.query_dos_device
    calls: list[int] = []

    def query(name: str) -> tuple[str, ...]:
        if name.upper() != drive.upper():
            return real(name)
        calls.append(len(calls) + 1)
        answer = answers.get(len(calls))
        if answer is None:
            return real(name)
        if isinstance(answer, BaseException):
            raise answer
        return answer  # type: ignore[return-value]

    monkeypatch.setattr(winfileops, "query_dos_device", query)
    return calls


def on_call(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    nth: int,
    replacement: Callable[..., object],
    only: Callable[..., bool] = lambda *args: True,
) -> None:
    """The ``nth`` call of ``winfileops.<name>`` (among those ``only`` accepts) is
    answered by ``replacement``; every other call by the real function."""
    real = getattr(winfileops, name)
    calls = 0

    def wrapper(*args: object) -> object:
        nonlocal calls
        if not only(*args):
            return real(*args)
        calls += 1
        return replacement(*args) if calls == nth else real(*args)

    monkeypatch.setattr(winfileops, name, wrapper)


def is_anchor(handle: winfileops.Handle) -> bool:
    return str(handle.path) == handle.path.anchor


def disguised_anchor(target: Path, anchor: Path) -> winfileops.Handle:
    """A handle of ``target`` that claims to be ``anchor``, opened like ``open_anchor``
    does: what a drive letter remapped (and restored) around the open would give."""
    api = winfileops._api()
    raw = api.kernel32.CreateFileW(
        str(target),
        winfileops.FILE_READ_ATTRIBUTES | winfileops.SYNCHRONIZE,
        winfileops.SHARE_ALL,
        None,
        winfileops._OPEN_EXISTING,
        winfileops._FILE_FLAG_BACKUP_SEMANTICS | winfileops._FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    assert raw is not None and raw != api.invalid_handle
    return winfileops.Handle(raw, anchor)


# Every case happens after the preview: the plan was built with the real mapping.
MAPPING_CASES = {
    "subst of a directory": "not the drive letter of a disk volume",
    "subst of a directory behind a junction": "not the drive letter of a disk volume",
    "nested subst": "not the drive letter of a disk volume",
    "subst of the volume root": "not the drive letter of a disk volume",
    "network drive": "not the drive letter of a disk volume",
    "undefined": "cannot read what",
    "unreadable": "cannot read what",
    "ambiguous": "not the drive letter of a disk volume",
    "another volume since the preview": "not the root of",
    "subst while the root was opened": "not the drive letter of a disk volume",
    "another volume while the root was opened": "was redefined while it was checked",
    "another volume during the run checks": "was redefined while it was checked",
}
OPENED_CASES = {  # the drive letter says volume, the opened object is something else
    "opened a nested directory": "is not the root directory of its volume",
    "opened a directory behind a junction": "is not the root directory of its volume",
    "opened a junction": "is not the root directory of its volume",
}
FILESYSTEM_CASES = {
    "FAT32": winfileops.VolumeInfo("FAT32", winfileops.FILE_DEVICE_DISK, 0),
    "exFAT": winfileops.VolumeInfo("exFAT", winfileops.FILE_DEVICE_DISK, 0),
    "ReFS": winfileops.VolumeInfo("ReFS", winfileops.FILE_DEVICE_DISK, 0),
    "remote NTFS": winfileops.VolumeInfo("NTFS", winfileops.FILE_DEVICE_DISK, 0x10),
    "removable NTFS": winfileops.VolumeInfo("NTFS", winfileops.FILE_DEVICE_DISK, 0x01),
    "CD-ROM": winfileops.VolumeInfo("CDFS", 0x02, 0),
    "virtual device NTFS": winfileops.VolumeInfo("NTFS", 0x08, 0),
}
UNPROVEN_ROOTS = {
    **MAPPING_CASES,
    **OPENED_CASES,
    **{f"filesystem {name}": "only a local NTFS disk is supported" for name in FILESYSTEM_CASES},
    "filesystem cannot be queried": "cannot check that",
    "final path inside the volume": "not the root of",
}


def unprove(case: str, which: str, monkeypatch: pytest.MonkeyPatch, area: Path, link: Path) -> None:
    """Make the volume root of ``which`` ("source" or "destination") unprovable."""
    drive, device = area.drive, winfileops.drive_device(area.drive)
    # query_dos_device(C:) of a run: 1 volume_root, 2 after the open (source); 3, 4
    # (destination); 5, 6 the re-checks at the end of the run checks; 7 journal close.
    first, after_open, last = (1, 2, 5) if which == "source" else (3, 4, 6)
    nth = 1 if which == "source" else 2  # its open_anchor and the anchor queries
    mappings: dict[str, dict[int, object]] = {
        "subst of a directory": {first: ("\\??\\" + str(area),)},
        "subst of a directory behind a junction": {first: ("\\??\\" + str(link / "inner"),)},
        "nested subst": {first: ("\\??\\Y:\\inner",)},
        "subst of the volume root": {first: ("\\??\\" + drive,)},
        "network drive": {first: (NETWORK,)},
        "undefined": {first: FileNotFoundError(2, "The system cannot find the file")},
        "unreadable": {first: PermissionError(13, "Access is denied")},
        "ambiguous": {first: (device, V999)},
        # The opened root is not V999's: refused before the mapping is read again.
        "another volume since the preview": {first: (V999,)},
        "subst while the root was opened": {after_open: ("\\??\\" + str(area),)},
        "another volume while the root was opened": {after_open: (V999,)},
        "another volume during the run checks": {last: (V999,)},
    }
    opened = {
        "opened a nested directory": area / "inner",
        "opened a directory behind a junction": link / "inner",
        "opened a junction": link,
    }
    if case in mappings:
        fake_mapping(monkeypatch, drive, mappings[case])
    elif case in opened:
        target = opened[case]
        on_call(monkeypatch, "open_anchor", nth, lambda anchor: disguised_anchor(target, anchor))
    elif case.startswith("filesystem ") and case[11:] in FILESYSTEM_CASES:
        info = FILESYSTEM_CASES[case[11:]]
        on_call(monkeypatch, "query_volume", nth, lambda handle: info, only=is_anchor)
    elif case == "filesystem cannot be queried":

        def broken(handle: winfileops.Handle) -> object:
            raise OSError(1, "Incorrect function")

        on_call(monkeypatch, "query_volume", nth, broken, only=is_anchor)
    else:
        assert case == "final path inside the volume"
        inside = device + "\\Users\\"
        on_call(monkeypatch, "final_nt_path", nth, lambda handle: inside, only=is_anchor)


def assert_root_refused(result: ExecutionResult, keyword: str) -> journal.JournalReport:
    """Refused before anything: no move, no directory, a refusal (not a move error)."""
    assert [(s, r) for _, s, r in outcome(result)] == [
        (SKIPPED, ExecReason.ROOT_UNVERIFIED) for _ in result.items
    ]
    assert result.items
    assert result.stopped is ExecReason.ROOT_UNVERIFIED
    assert result.stop_detail is not None and keyword in result.stop_detail, result.stop_detail
    assert not result.complete and not result.changed_anything
    assert result.directories_created == () and result.cleanup_problems == ()
    assert result.problems == ()  # a refusal, not an error
    report = assert_journal_consistent(result)
    changes = [e for e in report.entries if e.operation != "run"]
    assert [e.operation for e in changes] == ["move"] * len(result.items)  # no mkdir
    for entry in changes:
        assert entry.state is OperationState.SKIPPED and not entry.attempted
        assert entry.outcome is not None and entry.outcome["reason"] == "root_unverified"
    return report


@pytest.mark.parametrize("case", list(UNPROVEN_ROOTS))
@pytest.mark.parametrize("which", ["source", "destination"])
def test_unproven_volume_root_refuses_the_run_before_anything_changes(
    src: Path,
    area: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: Snapshot,
    make_junction,
    close_failures,
    which: str,
    case: str,
) -> None:
    touch(src, "a.pdf", "b.jpg")
    (area / "inner").mkdir()
    link = make_junction(tmp_path / "link", area)
    plan = plan_for(src) if which == "source" else plan_for(src, area / "Sorted")
    assert validate_plan(plan, TABLE) == ()  # the R2 checks alone would accept it
    before = snapshot(area)
    log = Journal.create()  # on the real C:, before the drive letter is faked
    unprove(case, which, monkeypatch, area, link)

    result = run(plan, journal=lambda: log)

    assert_root_refused(result, UNPROVEN_ROOTS[case])
    assert snapshot(area) == before
    close_failures.assert_each_closed_once()  # also the refused anchor
    area.rename(area.with_name("renamed"))  # nothing below it is held any more


@pytest.mark.parametrize("destination", [None, "Sorted"])
def test_both_roots_are_proven_before_anything_is_created_or_moved(
    src: Path, area: Path, monkeypatch: pytest.MonkeyPatch, destination: str | None
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = plan_for(src, None if destination is None else area / destination)
    anchor, device = src.anchor, winfileops.drive_device(src.drive)
    log = Journal.create()
    calls = fake_mapping(monkeypatch, src.drive, {})  # only counts
    events: list[tuple[str, ...]] = []
    real_open, real_prove = winfileops.open_anchor, winfileops.prove_volume_root
    real_relative, real_rename = winfileops.open_relative, winfileops.rename_into

    def open_anchor(path: Path) -> winfileops.Handle:
        events.append(("open", str(path)))
        return real_open(path)

    def prove(handle: winfileops.Handle, root: winfileops.VolumeRoot) -> None:
        events.append(("prove", str(handle.path), root.device))
        real_prove(handle, root)

    def open_relative(
        parent: winfileops.Handle, name: str, **kwargs: object
    ) -> tuple[winfileops.Handle, bool]:
        if kwargs.get("create"):
            events.append(("create", name))
        return real_relative(parent, name, **kwargs)  # type: ignore[arg-type]

    def rename(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        events.append(("rename", name))
        real_rename(handle, directory, name)

    monkeypatch.setattr(winfileops, "open_anchor", open_anchor)
    monkeypatch.setattr(winfileops, "prove_volume_root", prove)
    monkeypatch.setattr(winfileops, "open_relative", open_relative)
    monkeypatch.setattr(winfileops, "rename_into", rename)

    result = run(plan, journal=lambda: log)

    assert result.complete
    assert events[:4] == [("open", anchor), ("prove", anchor, device)] * 2
    assert {e[0] for e in events[4:]} == {"create", "rename"}  # no other anchor opened
    assert calls == list(range(1, 8))  # 2 per proof, 2 re-checks, the journal's close


def test_roots_on_two_volumes_are_refused(
    src: Path, area: Path, monkeypatch: pytest.MonkeyPatch, snapshot: Snapshot
) -> None:
    touch(src, "a.pdf")
    plan = plan_for(src)
    before = snapshot(area)
    log = Journal.create()
    real = winfileops.volume_root
    roots: list[winfileops.VolumeRoot] = []

    def volume_root(path: Path) -> winfileops.VolumeRoot:
        roots.append(real(path))
        return roots[0] if len(roots) == 1 else winfileops.VolumeRoot(roots[-1].drive, V999)

    monkeypatch.setattr(winfileops, "volume_root", volume_root)
    monkeypatch.setattr(winfileops, "prove_volume_root", lambda handle, root: None)
    result = run(plan, journal=lambda: log)
    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.CROSS_VOLUME)]
    assert result.stop_detail is not None and V999 in result.stop_detail
    assert not result.changed_anything and snapshot(area) == before


def test_real_subst_roots_are_refused_before_anything_changes(
    src: Path, area: Path, tmp_path: Path, subst_drives, make_junction, snapshot: Snapshot
) -> None:
    """Opt-in: real ``subst`` letters (free letters, temporary directories only) for the
    source or the destination; the journal stays on the real C: and records the refusal."""
    touch(src, "a.pdf", "b.jpg")
    (area / "Sorted").mkdir()
    junction = make_junction(tmp_path / "junction", tmp_path)
    plain, nested, linked, whole = subst_drives.free(4)
    subst_drives.map(plain, str(area))
    subst_drives.map(nested, plain + "\\")  # a subst of a subst drive
    # To a directory behind a junction (one to the junction itself is refused by the
    # planner already: the drive's root is then the junction).
    subst_drives.map(linked, str(junction / area.name))
    subst_drives.map(whole, tmp_path.anchor)  # the volume root itself
    inside = Path(whole + "\\") / area.relative_to(area.anchor)
    plans = {
        "source on a subst of a directory": plan_for(Path(plain + "\\Downloads")),
        "source on a nested subst": plan_for(Path(nested + "\\Downloads")),
        "source on a subst behind a junction": plan_for(Path(linked + "\\Downloads")),
        "source on a subst of the volume root": plan_for(inside / "Downloads"),
        # Only the source on a subst, the destination on the real drive letter.
        "only the source on a subst of a directory": plan_for(
            Path(plain + "\\Downloads"), area / "Sorted"
        ),
        "only the source on a nested subst": plan_for(
            Path(nested + "\\Downloads"), area / "Sorted"
        ),
        "only the source on a subst of the volume root": plan_for(
            inside / "Downloads", area / "Sorted"
        ),
        "destination on a subst of a directory": plan_for(src, Path(plain + "\\Sorted")),
        "destination on a subst behind a junction": plan_for(src, Path(linked + "\\Sorted")),
        "destination on a subst of the volume root": plan_for(src, inside / "Sorted"),
    }
    before = snapshot(area)
    for name, plan in plans.items():
        assert validate_plan(plan, TABLE) == (), name  # the R2 checks alone accept it
        result = run(plan)  # the default journal: on the real C: (LOCALAPPDATA)
        assert result.journal_path is not None
        assert result.journal_path.drive.upper() == tmp_path.drive.upper()
        assert_root_refused(result, "not the drive letter of a disk volume")
        assert snapshot(area) == before, name


# --- structured release errors of the journal reach the result (HANDLE-02) ------------


def test_journal_stream_failure_and_its_failed_release_both_reach_the_result(
    src: Path, monkeypatch: pytest.MonkeyPatch, snapshot: Snapshot, close_failures
) -> None:
    """``open_osfhandle`` fails, then ``CloseHandle`` of the journal file is FALSE."""
    import msvcrt

    touch(src, "a.pdf", "b.jpg")
    plan = plan_for(src)
    before = snapshot(src)
    logs = journal.default_log_dir()

    def broken(*args: object) -> int:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(msvcrt, "open_osfhandle", broken)
    close_failures.select = lambda handle: handle.path.parent == logs
    result = run(plan)
    assert outcome(result) == [
        ("a.pdf", CANCELLED, ExecReason.JOURNAL_FAILED),
        ("b.jpg", CANCELLED, ExecReason.JOURNAL_FAILED),
    ]
    # The main reason stays the journal failure; the release failure is reported apart.
    assert result.problems == ("cannot create journal: [Errno 24] Too many open files",)
    assert result.stopped is ExecReason.JOURNAL_FAILED and result.journal_path is None
    (raw,) = close_failures.failed
    (problem,) = result.cleanup_problems
    assert problem.startswith("cannot release journal: HandleCloseError: ")
    assert "[WinError 31]" in problem and f"{raw:#x}" in problem
    assert repr(str(logs))[1:-1] in problem  # OSError shows the file name as a repr
    (error,) = result.cleanup_errors  # and as the object itself
    assert isinstance(error, winfileops.HandleCloseError)
    assert (error.winerror, error.handle_value) == (31, raw)
    assert Path(error.filename).parent == logs
    assert not result.complete and not result.changed_anything
    assert snapshot(src) == before
    close_failures.assert_each_closed_once()  # the failed one: never retried
    close_failures.really_close()


def _protect(raw: int, on: bool) -> None:
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    k.SetHandleInformation.restype = wintypes.BOOL
    assert k.SetHandleInformation(raw, 2, 2 if on else 0)  # HANDLE_FLAG_PROTECT_FROM_CLOSE


def _really_close(raw: int) -> None:
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    assert k.CloseHandle(raw)  # it really had stayed open


def test_journal_released_by_the_c_runtime_after_a_stream_failure_reaches_the_result(
    src: Path, monkeypatch: pytest.MonkeyPatch, snapshot: Snapshot
) -> None:
    """After the hand-over the C runtime owns the handle; its close is a real FALSE."""
    import msvcrt

    touch(src, "a.pdf")
    plan = plan_for(src)
    before = snapshot(src)
    protected: list[int] = []

    def broken(fd: int, mode: str) -> object:
        protected.append(msvcrt.get_osfhandle(fd))
        _protect(protected[-1], True)
        raise MemoryError("no memory for the file object")

    monkeypatch.setattr(winfileops, "FileIO", broken)
    try:
        result = run(plan)
        assert outcome(result) == [("a.pdf", CANCELLED, ExecReason.JOURNAL_FAILED)]
        assert result.problems == (
            "cannot create journal: MemoryError: no memory for the file object",
        )
        (problem,) = result.cleanup_problems
        assert problem.startswith("cannot release journal: OSError: ")
        assert not result.complete and snapshot(src) == before
    finally:
        for raw in protected:
            _protect(raw, False)
            _really_close(raw)


def test_failed_close_of_the_journal_stream_is_reported_with_the_results(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream owns the handle; closing it at the end of the run is a real FALSE."""
    import msvcrt

    touch(src, "a.pdf", "b.jpg")
    plan = plan_for(src)
    real = winfileops.open_stream
    protected: list[int] = []

    def open_stream(handle: winfileops.Handle) -> object:
        stream = real(handle)
        protected.append(msvcrt.get_osfhandle(stream.fileno()))
        _protect(protected[-1], True)
        return stream

    monkeypatch.setattr(winfileops, "open_stream", open_stream)
    try:
        result = run(plan)
        assert [s for _, s, _ in outcome(result)] == [MOVED, MOVED]  # the results stay
        (problem,) = result.cleanup_problems
        assert problem.startswith("cannot close journal") and "OSError" in problem
        assert not result.complete and result.stopped is None
    finally:
        for raw in protected:
            _protect(raw, False)
            _really_close(raw)
    report = journal_of(result)
    assert report.finished and not report.uncertain  # every record was written


# --- cleanup and interrupts (HANDLE-03) -----------------------------------------------


class FakeHandle:
    def __init__(self, name: str, calls: list[str], error: BaseException | None = None) -> None:
        self.path, self.calls, self.error = Path(name), calls, error

    def close(self) -> None:
        self.calls.append(str(self.path))
        if self.error is not None:
            raise self.error


@pytest.fixture
def bare_run(src: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[executor._Run, list[str]]:
    """A run that owns nothing yet; ``calls`` records each close, ``journal`` included."""
    touch(src, "a.pdf")
    log = Journal.create()
    calls: list[str] = []
    real = log.close

    def close() -> None:
        calls.append("journal")
        real()

    monkeypatch.setattr(log, "close", close)
    return executor._Run(plan_for(src), TABLE, log, None), calls


CLEANUP_ERRORS: dict[str, Callable[[], list[BaseException | None]]] = {
    "KeyboardInterrupt": lambda: [KeyboardInterrupt(), None, None],
    "SystemExit": lambda: [SystemExit(2), None, None],
    "OSError": lambda: [OSError(6, "The handle is invalid"), None, None],
    "several": lambda: [KeyboardInterrupt(), OSError(6, "The handle is invalid"), SystemExit(3)],
}


@pytest.mark.parametrize("kind", list(CLEANUP_ERRORS))
def test_close_tries_every_handle_and_the_journal_whatever_is_raised(
    bare_run: tuple[executor._Run, list[str]], kind: str
) -> None:
    run_, calls = bare_run
    errors = CLEANUP_ERRORS[kind]()
    handles = [FakeHandle(f"h{i}", calls, e) for i, e in enumerate(errors, 1)]
    for handle in reversed(handles):  # h1 is the newest: closed first
        run_.own(handle)  # type: ignore[arg-type]
    interrupts = [e for e in errors if e is not None and not isinstance(e, Exception)]
    if interrupts:
        with pytest.raises(BaseException) as raised:
            run_.close()
        assert raised.value is interrupts[0]  # the first interrupt, raised after all
        notes = raised.value.__notes__
        assert notes[0].startswith(
            f"DownloadsOrganizer: run {run_.journal.run_id} interrupted after 0 verified"
        )
        assert [n for n in notes if n.startswith("cleanup: ")] == [
            f"cleanup: {p}" for p in run_.cleanup_problems
        ]
    else:
        run_.close()
    assert calls == ["h1", "h2", "h3", "journal"]  # each exactly once, the journal last
    assert run_.owned == {}
    assert run_.cleanup_problems == [
        f"cannot close h{i}: {type(e).__name__}: {e}" for i, e in enumerate(errors, 1) if e
    ]
    assert run_.journal.path is not None
    run_.journal.path.parent.rename(run_.journal.path.parent.with_name("moved"))  # closed


def test_close_after_an_interrupt_only_adds_notes_to_it(
    bare_run: tuple[executor._Run, list[str]],
) -> None:
    run_, calls = bare_run
    run_.own(FakeHandle("h1", calls, SystemExit(3)))  # type: ignore[arg-type]
    interrupt = KeyboardInterrupt()
    run_.close(interrupted=interrupt)  # the caller raises it
    assert calls == ["h1", "journal"]
    assert "cleanup: cannot close h1: SystemExit: 3" in interrupt.__notes__


def test_interrupt_while_closing_the_journal_is_raised_after_the_handles(
    bare_run: tuple[executor._Run, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    run_, calls = bare_run
    real = run_.journal.close

    def close() -> None:
        real()
        raise KeyboardInterrupt

    monkeypatch.setattr(run_.journal, "close", close)
    run_.own(FakeHandle("h1", calls))  # type: ignore[arg-type]
    with pytest.raises(KeyboardInterrupt):
        run_.close()
    assert calls == ["h1", "journal"]
    assert run_.cleanup_problems == ["cannot close journal: KeyboardInterrupt: "]


def test_interrupt_in_release_is_raised_and_the_handle_is_never_closed_again(
    bare_run: tuple[executor._Run, list[str]],
) -> None:
    run_, calls = bare_run
    handle = run_.own(FakeHandle("h1", calls, KeyboardInterrupt()))  # type: ignore[arg-type]
    other = run_.own(FakeHandle("h2", calls))  # type: ignore[arg-type]
    with pytest.raises(KeyboardInterrupt):
        run_.release(handle)
    assert calls == ["h1"] and list(run_.owned.values()) == [other]
    assert run_.stopped is ExecReason.CLEANUP_FAILED
    run_.close()
    assert calls == ["h1", "h2", "journal"]


def only_journal() -> Path:
    (path,) = journal.default_log_dir().iterdir()
    return path


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interrupt_right_after_a_real_move(
    src: Path,
    monkeypatch: pytest.MonkeyPatch,
    close_failures,
    interrupt: type[BaseException],
) -> None:
    touch(src, "a.pdf", "b.jpg", "c.pdf")
    before = fingerprints(src)
    plan = plan_for(src)
    events: list[str] = []
    real_rename, real_open = winfileops.rename_into, winfileops.open_relative
    real_create = winfileops.create_file

    def rename(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        events.append(f"rename {name}")
        real_rename(handle, directory, name)
        events.append("interrupt")
        raise interrupt()

    def open_relative(
        parent: winfileops.Handle, name: str, **kwargs: object
    ) -> tuple[winfileops.Handle, bool]:
        events.append(f"open {name}")
        return real_open(parent, name, **kwargs)  # type: ignore[arg-type]

    def create_file(parent: winfileops.Handle, name: str, **kwargs: object) -> object:
        events.append(f"create {name}")
        return real_create(parent, name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(winfileops, "rename_into", rename)
    monkeypatch.setattr(winfileops, "open_relative", open_relative)
    monkeypatch.setattr(winfileops, "create_file", create_file)
    with pytest.raises(interrupt) as raised:
        run(plan)

    assert events[-2:] == ["rename a.pdf", "interrupt"]  # no file operation after it
    assert [e for e in events if e.startswith("rename")] == ["rename a.pdf"]
    assert fingerprints(src / "DEL" / "PDF") == {"a.pdf": before["a.pdf"]}
    assert fingerprints(src) == {"b.jpg": before["b.jpg"], "c.pdf": before["c.pdf"]}
    assert not (src / "DEL" / "Images").exists()
    path = only_journal()
    report = read_journal(path)
    assert not report.finished
    moves = [(Path(e.source).name, e.state, e.attempted) for e in report.entries if e.source]
    assert moves == [("a.pdf", OperationState.UNCERTAIN, True)]  # never a false DONE
    notes = raised.value.__notes__
    assert "interrupted after 0 verified move(s)" in notes[0] and str(path) in notes[0]
    assert notes[1].startswith(
        f"outcome unknown, check by hand: move of {src / 'a.pdf'}"
        f" to {src / 'DEL' / 'PDF' / 'a.pdf'} (journal intent "
    )
    assert len(notes) == 2  # no cleanup problem
    close_failures.assert_each_closed_once()
    (src / "DEL" / "PDF").rename(src / "DEL" / "Renamed")  # released
    src.rename(src.with_name("Downloads2"))


def test_interrupt_from_the_cancel_callback_keeps_the_verified_moves(
    src: Path, close_failures
) -> None:
    touch(src, "a.pdf", "b.jpg")
    asked = 0

    def cancel() -> bool:
        nonlocal asked
        asked += 1
        if asked == 2:
            raise KeyboardInterrupt
        return False

    with pytest.raises(KeyboardInterrupt) as raised:
        run(plan_for(src), cancel=cancel)
    assert (src / "DEL" / "PDF" / "a.pdf").is_file() and (src / "b.jpg").is_file()
    assert not (src / "DEL" / "Images").exists()
    report = read_journal(only_journal())
    assert not report.finished
    moves = [(Path(e.source).name, e.state) for e in report.entries if e.operation == "move"]
    assert moves == [("a.pdf", OperationState.DONE)]
    (note,) = raised.value.__notes__
    assert "interrupted after 1 verified move(s)" in note
    close_failures.assert_each_closed_once()


def test_interrupt_and_a_failed_release_during_its_cleanup(src: Path, close_failures) -> None:
    touch(src, "a.pdf", "b.jpg")
    close_failures.select = lambda handle: handle.path == src / "DEL" / "PDF"
    interrupt = KeyboardInterrupt()

    def cancel() -> bool:
        if (src / "DEL" / "PDF" / "a.pdf").exists():
            raise interrupt
        return False

    with pytest.raises(KeyboardInterrupt) as raised:
        run(plan_for(src), cancel=cancel)
    assert raised.value is interrupt  # not hidden by the cleanup failure
    (problem,) = [n for n in interrupt.__notes__ if n.startswith("cleanup: ")]
    assert str(src / "DEL" / "PDF") in problem and "CloseHandle failed" in problem
    assert len(close_failures.failed) == 1
    close_failures.assert_each_closed_once()  # the failed one: never retried
    close_failures.really_close()
    (src / "DEL" / "PDF").rename(src / "DEL" / "Renamed")


def test_interrupt_during_the_final_cleanup_is_raised_after_everything_was_released(
    src: Path, close_failures
) -> None:
    touch(src, "a.pdf", "b.jpg")
    close_failures.select = lambda handle: handle.path == src / "DEL" / "PDF"
    close_failures.interrupt = interrupt = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as raised:
        run(plan_for(src))
    assert raised.value is interrupt
    assert (src / "DEL" / "PDF" / "a.pdf").is_file() and (
        src / "DEL" / "Images" / "b.jpg"
    ).is_file()
    report = read_journal(only_journal())
    assert report.finished and not report.uncertain and not report.problems  # closed after
    assert [e.state for e in report.entries if e.operation == "move"] == [OperationState.DONE] * 2
    notes = interrupt.__notes__
    assert "interrupted after 2 verified move(s)" in notes[0]
    assert notes[1:] == [f"cleanup: cannot close {src / 'DEL' / 'PDF'}: KeyboardInterrupt: "]
    close_failures.assert_each_closed_once()
    (src / "DEL").rename(src / "DEL2")  # everything released


# --- continuous protection of the directory chains (EXEC-03) --------------------------

SHARING_VIOLATION = winfileops.ERROR_SHARING_VIOLATION


def _kernel32():  # type: ignore[no-untyped-def]
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    k.MoveFileExW.restype = wintypes.BOOL
    k.RemoveDirectoryW.argtypes = [wintypes.LPCWSTR]
    k.RemoveDirectoryW.restype = wintypes.BOOL
    k.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]  # fmt: skip
    k.CreateFileW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k


def win_rename(old: Path, new: Path) -> int:
    """Rename by ``MoveFileExW`` (no flags: never replaces): 0, or ``GetLastError``."""
    import ctypes

    return 0 if _kernel32().MoveFileExW(str(old), str(new), 0) else ctypes.get_last_error()


def win_rmdir(path: Path) -> int:
    """``RemoveDirectoryW``: 0, or ``GetLastError``."""
    import ctypes

    return 0 if _kernel32().RemoveDirectoryW(str(path)) else ctypes.get_last_error()


@contextlib.contextmanager
def opened_for_deletion(path: Path) -> Iterator[None]:
    """``path`` held by someone else with ``DELETE`` access (sharing everything)."""
    import ctypes

    k = _kernel32()
    raw = k.CreateFileW(
        str(path), winfileops.DELETE | winfileops.FILE_READ_ATTRIBUTES, winfileops.SHARE_ALL,
        None, winfileops._OPEN_EXISTING,
        winfileops._FILE_FLAG_BACKUP_SEMANTICS | winfileops._FILE_FLAG_OPEN_REPARSE_POINT, None,
    )  # fmt: skip
    assert raw is not None and raw != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    try:
        yield
    finally:
        assert k.CloseHandle(raw)


def test_destination_ancestor_cannot_be_renamed_while_its_child_is_not_open_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    """The EXEC-03 regression: ``A`` is open, ``B`` is not yet; renaming ``A`` to
    ``A_old`` by the real API is refused, the file ends up physically below ``A``."""
    source, a = tmp_path / "S", tmp_path / "A"
    touch(source, "a.pdf")
    a.mkdir()
    destination = a / "B" / "Sorted"
    before, a_id = fingerprint(source / "a.pdf"), a.stat().st_ino
    plan = plan_for(source, destination)
    assert plan.directories_to_create == (a / "B", destination, destination / "PDF")
    log = Journal.create(tmp_path / "journal")  # its own verified directory
    real = winfileops.open_relative
    attempts: list[tuple[Path, int]] = []

    def open_relative(parent: winfileops.Handle, name: str, **kwargs: object) -> object:
        if name == "B" and not attempts:
            assert parent.path == a  # A is open and checked, B not opened yet
            attempts.append((parent.path, win_rename(a, tmp_path / "A_old")))
        return real(parent, name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(winfileops, "open_relative", open_relative)
    result = run(plan, journal=lambda: log)
    monkeypatch.undo()

    assert attempts == [(a, SHARING_VIOLATION)]  # refused while the handle was held
    assert not (tmp_path / "A_old").exists()
    assert result.complete and outcome(result) == [("a.pdf", MOVED, ExecReason.COMPLETED)]
    moved = destination / "PDF" / "a.pdf"
    assert fingerprint(moved) == before and a.stat().st_ino == a_id  # below the same A
    assert not (source / "a.pdf").exists()
    assert result.directories_created == plan.directories_to_create
    report = assert_journal_consistent(result)
    assert [Path(e.target or "") for e in report.entries if e.operation == "mkdir"] == list(
        plan.directories_to_create
    )
    (move,) = [e for e in report.entries if e.operation == "move"]
    assert move.target == str(moved) and move.state is OperationState.DONE
    close_failures.assert_each_closed_once()
    assert win_rename(a, tmp_path / "A_old") == 0  # released: now it can be renamed
    assert fingerprint(tmp_path / "A_old" / "B" / "Sorted" / "PDF" / "a.pdf") == before


def test_held_destination_ancestor_cannot_be_removed_or_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swapping ``A`` for a junction needs it removed or renamed first: both refused."""
    source, a = tmp_path / "S", tmp_path / "A"
    touch(source, "a.pdf")
    a.mkdir()
    (tmp_path / "elsewhere").mkdir()
    plan = plan_for(source, a / "B" / "Sorted")
    a_id = a.stat().st_ino
    real = winfileops.open_relative
    codes: list[int] = []

    def open_relative(parent: winfileops.Handle, name: str, **kwargs: object) -> object:
        if name == "B" and not codes:
            codes.extend([win_rmdir(a), win_rename(a, tmp_path / "A_old")])  # A is empty
        return real(parent, name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(winfileops, "open_relative", open_relative)
    result = run(plan)
    assert codes == [SHARING_VIOLATION, SHARING_VIOLATION]
    assert outcome(result) == [("a.pdf", MOVED, ExecReason.COMPLETED)]
    assert a.stat().st_ino == a_id and not a.is_junction()
    assert (a / "B" / "Sorted" / "PDF" / "a.pdf").is_file()
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_destination_ancestor_swapped_for_a_junction_before_the_walk_reaches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_junction
) -> None:
    """Not held yet when it was swapped: the walk sees the junction through its own
    handle and stops there; nothing is created or moved, and the chain is released."""
    source, a = tmp_path / "S", tmp_path / "A"
    touch(source, "a.pdf")
    (a / "B").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    plan = plan_for(source, a / "B" / "Sorted")
    real = executor.check_directory_chain
    swapped: list[Path] = []

    def check_then_swap(path: Path):  # type: ignore[no-untyped-def]
        chain = real(path)
        if path == plan.destination_root and not swapped:
            assert win_rename(a, tmp_path / "A_real") == 0  # nobody holds A yet
            make_junction(a, tmp_path / "elsewhere")
            swapped.append(path)
        return chain

    monkeypatch.setattr(executor, "check_directory_chain", check_then_swap)
    result = run(plan)
    assert swapped
    assert outcome(result) == [("a.pdf", SKIPPED, ExecReason.TARGET_DIR_INVALID)]
    assert str(a) in (result.items[0].detail or "")
    assert list((tmp_path / "elsewhere").iterdir()) == []
    assert sorted(p.name for p in (tmp_path / "A_real").iterdir()) == ["B"]
    assert list((tmp_path / "A_real" / "B").iterdir()) == []
    assert (source / "a.pdf").is_file()
    assert not any(e.operation == "mkdir" for e in assert_journal_consistent(result).entries)
    assert win_rename(tmp_path / "A_real", tmp_path / "A_again") == 0  # released
    assert win_rmdir(a) == 0  # the junction itself, not its target


def test_every_held_directory_of_both_chains_stays_unrenamable_until_the_run_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    """Source ``X\\S``, destination ``A\\B\\C\\Sorted`` (``C`` and below created): before
    and after every open and around the move, renaming any directory the run opened so
    far is refused with a sharing violation. A released parent would give "access
    denied" (it has an open child) or be renamed: neither happens."""
    source, a = tmp_path / "X" / "S", tmp_path / "A"
    touch(source, "a.pdf")
    (a / "B").mkdir(parents=True)
    destination = a / "B" / "C" / "Sorted"
    before = fingerprint(source / "a.pdf")
    plan = plan_for(source, destination)
    log = Journal.create(tmp_path / "journal")
    real_open, real_rename = winfileops.open_relative, winfileops.rename_into
    held: list[Path] = []  # opened by the run, inside tmp_path only
    tried: list[tuple[str, dict[str, int]]] = []

    def try_renames(moment: str) -> None:
        tried.append((moment, {p.name: win_rename(p, p.with_name(p.name + "_old")) for p in held}))

    def open_relative(parent: winfileops.Handle, name: str, **kwargs: object) -> object:
        try_renames(f"before {name}")
        handle, created = real_open(parent, name, **kwargs)  # type: ignore[arg-type]
        if kwargs["directory"] and tmp_path in handle.path.parents:
            held.append(handle.path)
        try_renames(f"after {name}")
        return handle, created

    def rename_into(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        try_renames("before the move")
        real_rename(handle, directory, name)
        try_renames("after the move")

    monkeypatch.setattr(winfileops, "open_relative", open_relative)
    monkeypatch.setattr(winfileops, "rename_into", rename_into)
    result = run(plan, journal=lambda: log)
    monkeypatch.undo()

    assert [p.name for p in held] == ["X", "S", "A", "B", "C", "Sorted", "PDF"]
    moments = dict(tried)
    assert list(moments["after A"]) == ["X", "S", "A"]  # right after A was opened
    assert list(moments["before B"]) == ["X", "S", "A"]  # A open, B not yet
    assert list(moments["before the move"]) == [p.name for p in held]
    refused = [(m, n, c) for m, codes in tried for n, c in codes.items() if c != SHARING_VIOLATION]
    assert refused == []
    assert sum(len(codes) for _, codes in tried) > 40
    assert not list(tmp_path.rglob("*_old"))
    assert outcome(result) == [("a.pdf", MOVED, ExecReason.COMPLETED)]
    assert fingerprint(destination / "PDF" / "a.pdf") == before
    assert_journal_consistent(result)
    close_failures.assert_each_closed_once()
    assert win_rename(a, tmp_path / "A_old") == 0 and win_rename(source, source.parent / "S2") == 0
    assert win_rename(tmp_path / "X", tmp_path / "X_old") == 0  # all released
    assert fingerprint(tmp_path / "A_old" / "B" / "C" / "Sorted" / "PDF" / "a.pdf") == before


def test_source_ancestor_cannot_be_renamed_from_the_moment_it_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "X" / "S"
    touch(source, "a.pdf")
    plan = plan_for(source, tmp_path / "Sorted")
    real = winfileops.open_relative
    codes: dict[str, int] = {}

    def open_relative(parent: winfileops.Handle, name: str, **kwargs: object) -> object:
        if name == "S" and "X" not in codes:  # X just opened, S not yet
            codes["X"] = win_rename(tmp_path / "X", tmp_path / "X_old")
        return real(parent, name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(winfileops, "open_relative", open_relative)
    result = run(plan)
    assert codes == {"X": SHARING_VIOLATION}
    assert outcome(result) == [("a.pdf", MOVED, ExecReason.COMPLETED)]
    assert (tmp_path / "Sorted" / "PDF" / "a.pdf").is_file() and not (tmp_path / "X_old").exists()


def test_directory_held_for_deletion_by_someone_else_is_refused_without_a_weaker_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snapshot: Snapshot
) -> None:
    source, a = tmp_path / "S", tmp_path / "A"
    touch(source, "a.pdf")
    a.mkdir()
    plan = plan_for(source, a / "B")
    real = winfileops.open_relative
    shares: list[int] = []

    def open_relative(parent: winfileops.Handle, name: str, **kwargs: object) -> object:
        if name == "A":
            shares.append(kwargs["share"])  # type: ignore[arg-type]
        return real(parent, name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(winfileops, "open_relative", open_relative)
    before = snapshot(tmp_path / "A")
    with opened_for_deletion(a):
        result = run(plan)
    assert shares == [executor._HELD_SHARE]  # exactly one attempt, never a weaker one
    assert outcome(result) == [("a.pdf", FAILED, ExecReason.TARGET_DIR_FAILED)]
    assert result.items[0].error_code == SHARING_VIOLATION
    assert snapshot(tmp_path / "A") == before and (source / "a.pdf").is_file()
    assert not any(e.operation == "mkdir" for e in assert_journal_consistent(result).entries)
    assert win_rename(a, tmp_path / "A_old") == 0  # the run released its chain


def released_paths(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The path of every handle the run releases before ``close``, in order."""
    real = executor._Run.release
    released: list[Path] = []

    def release(self: executor._Run, handle: winfileops.Handle, **kwargs: object) -> None:
        released.append(handle.path)
        real(self, handle, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(executor._Run, "release", release)
    return released


def test_failure_opening_a_child_releases_the_chain_and_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    source, a = tmp_path / "S", tmp_path / "A"
    touch(source, "a.pdf")
    (a / "B").mkdir(parents=True)
    before = fingerprints(source)
    plan = plan_for(source, a / "B" / "Sorted")
    real = winfileops.open_relative

    def open_relative(parent: winfileops.Handle, name: str, **kwargs: object) -> object:
        if name == "B":
            raise PermissionError(13, "injected", str(parent.path / name), 5)
        return real(parent, name, **kwargs)  # type: ignore[arg-type]

    released = released_paths(monkeypatch)
    monkeypatch.setattr(winfileops, "open_relative", open_relative)
    result = run(plan)
    assert outcome(result) == [("a.pdf", FAILED, ExecReason.TARGET_DIR_FAILED)]
    assert result.items[0].error_code == 5
    chain = [a, *list(a.parents)[:-1]]  # this walk's handles, newest first; not the root
    assert released[: len(chain)] == chain
    assert fingerprints(source) == before
    assert sorted(p.name for p in a.rglob("*")) == ["B"]
    assert_journal_consistent(result)
    close_failures.assert_each_closed_once()
    assert win_rename(a, tmp_path / "A_old") == 0


def test_failure_after_creating_an_intermediate_directory_stops_and_releases_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    source, a = tmp_path / "S", tmp_path / "A"
    touch(source, "a.pdf", "b.pdf")
    a.mkdir()
    before = fingerprints(source)
    plan = plan_for(source, a / "B" / "Sorted")
    released = released_paths(monkeypatch)
    executor_query(monkeypatch, lambda handle, _: handle.path == a / "B", OSError(5, "injected"))
    result = run(plan)
    assert outcome(result) == [
        ("a.pdf", CANCELLED, ExecReason.RUN_ABORTED),
        ("b.pdf", CANCELLED, ExecReason.RUN_ABORTED),
    ]
    assert result.stopped is ExecReason.RUN_ABORTED and result.directories_created == (a / "B",)
    chain = [a / "B", a, *list(a.parents)[:-1]]  # B by mkdir, then the walk's, newest first
    assert released[: len(chain)] == chain
    assert fingerprints(source) == before and list((a / "B").iterdir()) == []
    report = journal_of(result)
    (mkdir,) = [e for e in report.entries if e.operation == "mkdir"]
    assert mkdir.target == str(a / "B") and mkdir.state is OperationState.RECOVERY_REQUIRED
    assert not any(e.attempted for e in report.entries if e.operation == "move")
    close_failures.assert_each_closed_once()
    assert win_rename(a, tmp_path / "A_old") == 0


DIR_INFO = winfileops.HandleInfo(7, 1, stat.FILE_ATTRIBUTE_DIRECTORY, 0, 0, 0, 1, False, True)
LINK_INFO = dataclasses.replace(
    DIR_INFO, attributes=DIR_INFO.attributes | stat.FILE_ATTRIBUTE_REPARSE_POINT
)


@pytest.mark.parametrize("failure", ["none", "open", "link", "query", "mkdir", "interrupt"])
def test_walk_holds_every_parent_and_releases_only_its_own_chain_on_a_refusal(
    bare_run: tuple[executor._Run, list[str]],
    src: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Fake API: each component is opened relative to the handle just checked, without
    ``FILE_SHARE_DELETE``; nothing of the chain is closed while the walk goes on; a
    refusal releases exactly the handles this walk opened, newest first, each once."""
    run_, calls = bare_run
    path = src / "DEL" / "PDF"  # planned for creation
    failing = path.parent if failure == "mkdir" else src
    anchor = run_.own(FakeHandle(path.anchor, calls))  # type: ignore[arg-type]
    opened: list[FakeHandle] = [anchor]  # type: ignore[list-item]
    shares: list[int] = []

    def open_relative(
        parent: FakeHandle, name: str, *, directory: bool, access: int, share: int,
        create: bool = False,
    ) -> tuple[FakeHandle, bool]:  # fmt: skip
        assert parent is opened[-1] and directory and calls == []
        shares.append(share)
        child = parent.path / name
        if child == failing and failure == "open":
            raise PermissionError(13, "injected", str(child), 5)
        if child == failing and failure == "mkdir":
            if create:
                raise PermissionError(13, "injected", str(child), 5)
            raise FileNotFoundError(2, "missing", str(child))
        handle = FakeHandle(str(child), calls)
        opened.append(handle)
        return handle, create

    def query(handle: FakeHandle) -> winfileops.HandleInfo:
        assert calls == []
        if handle.path == failing and failure == "link":
            return LINK_INFO
        if handle.path == failing and failure == "query":
            raise PermissionError(13, "injected", str(handle.path), 5)
        if handle.path == failing and failure == "interrupt":
            raise KeyboardInterrupt
        return DIR_INFO

    monkeypatch.setattr(winfileops, "open_relative", open_relative)
    monkeypatch.setattr(winfileops, "query", query)
    components = [*list(reversed(path.parents))[1:], path]  # below the volume root
    earlier = [str(p) for p in reversed(components[: components.index(failing)])]
    if failure == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            run_.walk(anchor, path, create=True)  # type: ignore[arg-type]
        assert calls == [str(src)]  # only the handle not handed to anyone yet
        assert list(run_.owned.values()) == opened[:-1]  # the rest: released by close
    else:
        result = run_.walk(anchor, path, create=True)  # type: ignore[arg-type]
        if failure == "none":
            assert result is opened[-1] and calls == []
            assert [h.path for h in opened[1:]] == components
            assert list(run_.owned.values()) == opened  # every handle of the chain held
        else:
            assert isinstance(result, executor._Refusal)
            expected = {
                "open": (FAILED, ExecReason.TARGET_DIR_FAILED, earlier),
                "link": (SKIPPED, ExecReason.TARGET_DIR_INVALID, [str(src), *earlier]),
                "query": (FAILED, ExecReason.TARGET_DIR_FAILED, [str(src), *earlier]),
                "mkdir": (FAILED, ExecReason.TARGET_DIR_FAILED, earlier),
            }
            assert (result.status, result.reason, calls) == expected[failure]
            assert list(run_.owned.values()) == [anchor]  # the root: owned until close
    assert shares and set(shares) == {executor._HELD_SHARE}
    assert not executor._HELD_SHARE & winfileops.FILE_SHARE_DELETE
    run_.close()
    assert calls[-2:] == [str(anchor.path), "journal"] and len(calls) == len(set(calls))
    assert len(calls) == len(opened) + 1  # every handle closed exactly once


# --- complete diagnostics of an interrupted cleanup (HANDLE-04) ------------------------

HCE = winfileops.HandleCloseError


class ClosingFails:
    """The journal's stream: ``close`` really closes it, then raises ``error`` if set."""

    def __init__(self, stream: object, error: BaseException | None) -> None:
        self.stream, self.error = stream, error

    def write(self, data: bytes) -> int:
        return self.stream.write(data)  # type: ignore[attr-defined]

    def flush(self) -> None:
        self.stream.flush()  # type: ignore[attr-defined]

    def fileno(self) -> int:
        return self.stream.fileno()  # type: ignore[attr-defined]

    def close(self) -> None:
        self.stream.close()  # type: ignore[attr-defined]
        if self.error is not None:
            raise self.error


def journal_closing_with(error: BaseException | None) -> Journal:
    log = Journal.create()
    log._stream = ClosingFails(log._stream, error)  # type: ignore[assignment]
    return log


def fail_closing(close_failures, *paths: Path) -> None:  # type: ignore[no-untyped-def]
    close_failures.select = lambda handle: handle.path in paths
    close_failures.limit = len(paths)


def interrupt_at_second_file(interrupt: BaseException) -> Callable[[], bool]:
    asked = 0

    def cancel() -> bool:
        nonlocal asked
        asked += 1
        if asked == 2:  # after the first move was verified
            raise interrupt
        return False

    return cancel


def assert_diagnostics_complete(
    raised: BaseException, failures: list[BaseException], close_failures
) -> None:
    """Every failure is kept as itself (with its values) and named in a note."""
    closing = [e for e in failures if isinstance(e, HCE)]
    assert sorted(e.handle_value for e in closing) == sorted(close_failures.failed)
    assert {e.winerror for e in closing} <= {31} and all(e.filename for e in closing)
    notes = "\n".join(raised.__notes__)
    for error in failures:  # a JournalError's message already names what it holds
        text = str(error) if isinstance(error, JournalError) else f"{type(error).__name__}: {error}"
        assert text in notes, error
    for error in closing:
        assert f"{error.handle_value:#x}" in notes and repr(error.filename)[1:-1] in notes


# What the run ends with; what closing the journal's stream raises (after closing it);
# whose CloseHandle fails; the release failures that must reach the caller, in order.
CleanupCase = tuple[BaseException, BaseException | None, tuple[str, ...], list[type]]
CLEANUP_CASES: dict[str, Callable[[], CleanupCase]] = {
    "SystemExit in the stream, a journal directory fails": lambda: (
        KeyboardInterrupt(),
        SystemExit(4),
        ("logs",),
        [SystemExit, HCE],
    ),
    "KeyboardInterrupt twice": lambda: (
        KeyboardInterrupt(),
        KeyboardInterrupt(),
        ("logs",),
        [KeyboardInterrupt, HCE],
    ),
    "SystemExit and a journal directory fails": lambda: (
        SystemExit(5),
        None,
        ("logs",),
        [journal.JournalCloseError, HCE],
    ),
    "several journal directories fail": lambda: (
        KeyboardInterrupt(),
        SystemExit(4),
        ("logs", "app"),
        [SystemExit, HCE, HCE],
    ),
    "stream error and a run directory fails": lambda: (
        KeyboardInterrupt(),
        OSError(5, "stream"),
        ("category",),
        [HCE, journal.JournalCloseError, OSError],
    ),
}


@pytest.mark.parametrize("case", list(CLEANUP_CASES))
def test_interrupt_keeps_every_failure_of_its_cleanup(src: Path, close_failures, case: str) -> None:
    ending, stream_error, failing, expected = CLEANUP_CASES[case]()
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    log = journal_closing_with(stream_error)
    logs = journal.default_log_dir()
    paths = {"logs": logs, "app": logs.parent, "category": src / "DEL" / "PDF"}
    fail_closing(close_failures, *(paths[name] for name in failing))

    with pytest.raises(type(ending)) as raised:
        run(plan_for(src), cancel=interrupt_at_second_file(ending), journal=lambda: log)

    assert raised.value is ending  # the original interrupt reaches the caller
    failures = winfileops.release_failures(ending)
    assert [type(e) for e in failures] == expected
    if stream_error is not None:
        assert any(e is stream_error for e in failures)
    assert {Path(e.filename) for e in failures if isinstance(e, HCE)} == {
        paths[name] for name in failing
    }
    assert len(close_failures.failed) == len(failing)
    assert_diagnostics_complete(ending, failures, close_failures)
    close_failures.assert_each_closed_once()  # every other handle tried, none twice
    assert fingerprints(src / "DEL" / "PDF") == {"a.pdf": before["a.pdf"]}  # no new move
    assert fingerprints(src) == {"b.jpg": before["b.jpg"]}
    assert log.path is not None
    report = read_journal(log.path)
    assert not report.finished
    moves = [(Path(e.source or "").name, e.state) for e in report.entries if e.operation == "move"]
    assert moves == [("a.pdf", OperationState.DONE)]  # the verified move: no false DONE


def test_systemexit_in_the_journal_stream_during_an_interrupted_cleanup(
    src: Path, close_failures
) -> None:
    """The HANDLE-04 scenario in detail: the run ends with ``KeyboardInterrupt``; its
    handles close; the journal stream's close raises ``SystemExit``; a journal directory
    fails ``CloseHandle``; the remaining directories are still released."""
    touch(src, "a.pdf", "b.jpg")
    exit_ = SystemExit(4)
    log = journal_closing_with(exit_)
    logs = journal.default_log_dir()
    fail_closing(close_failures, logs)
    interrupt = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt) as raised:
        run(plan_for(src), cancel=interrupt_at_second_file(interrupt), journal=lambda: log)

    assert raised.value is interrupt
    assert interrupt.release_errors == (exit_,)  # type: ignore[attr-defined]
    (closing,) = exit_.release_errors  # type: ignore[attr-defined]
    assert isinstance(closing, HCE)
    assert (closing.winerror, closing.handle_value) == (31, close_failures.failed[0])
    assert Path(closing.filename) == logs
    assert winfileops.release_failures(interrupt) == [exit_, closing]
    (note,) = [n for n in interrupt.__notes__ if n.startswith("cleanup: ")]
    assert note.startswith("cleanup: cannot close journal: SystemExit: 4 [notes: ")
    assert str(closing) in note  # the nested note is kept, not only the type and message
    (inner,) = exit_.__notes__
    assert inner == f"cannot close journal {log.path}: SystemExit: 4; HCE: {closing}".replace(
        "HCE", "HandleCloseError"
    )
    paths = [h.path for h in close_failures.closed if h is not None]  # type: ignore[attr-defined]
    # The journal is released last: its directory failed, every ancestor was still tried.
    assert paths[paths.index(logs) :] == [logs, *logs.parents]
    close_failures.assert_each_closed_once()
    (src / "DEL").rename(src / "DEL2")  # the run's own handles were released


def test_interrupt_with_an_unanswered_intent_keeps_its_cleanup_failures(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    exit_ = SystemExit(4)
    log = journal_closing_with(exit_)
    fail_closing(close_failures, journal.default_log_dir())
    interrupt = KeyboardInterrupt()
    real = winfileops.rename_into

    def rename(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        real(handle, directory, name)
        raise interrupt  # the move happened, its outcome was never written

    monkeypatch.setattr(winfileops, "rename_into", rename)
    with pytest.raises(KeyboardInterrupt) as raised:
        run(plan_for(src), journal=lambda: log)

    assert raised.value is interrupt
    failures = winfileops.release_failures(interrupt)
    assert [type(e) for e in failures] == [SystemExit, HCE] and failures[0] is exit_
    assert_diagnostics_complete(interrupt, failures, close_failures)
    assert any(
        n.startswith("outcome unknown, check by hand: move of ") for n in interrupt.__notes__
    )
    assert log.path is not None
    moves = [
        (Path(e.source or "").name, e.state, e.attempted)
        for e in read_journal(log.path).entries
        if e.operation == "move"
    ]
    assert moves == [("a.pdf", OperationState.UNCERTAIN, True)]  # never a false DONE
    assert fingerprints(src / "DEL" / "PDF") == {"a.pdf": before["a.pdf"]}
    assert fingerprints(src) == {"b.jpg": before["b.jpg"]}  # nothing started after it
    close_failures.assert_each_closed_once()


def test_journal_close_error_without_an_interrupt_reaches_the_result_as_objects(
    src: Path, close_failures
) -> None:
    touch(src, "a.pdf", "b.jpg")
    stream_error = OSError(5, "stream")
    log = journal_closing_with(stream_error)
    logs = journal.default_log_dir()
    fail_closing(close_failures, logs, src / "DEL" / "PDF")

    result = run(plan_for(src), journal=lambda: log)

    assert [r.status for r in result.items] == [MOVED, MOVED]  # truthful: both moved
    assert not result.complete and result.stopped is None
    hce, closing = result.cleanup_errors
    assert isinstance(hce, HCE) and Path(hce.filename) == src / "DEL" / "PDF"
    assert isinstance(closing, journal.JournalCloseError)
    assert closing.errors[0] is stream_error and isinstance(closing.errors[1], HCE)
    assert Path(closing.errors[1].filename) == logs
    assert winfileops.release_failures(closing) == list(closing.errors)
    handles = sorted(e.handle_value for e in (hce, closing.errors[1]))  # type: ignore[union-attr]
    assert handles == sorted(close_failures.failed)
    assert len(result.cleanup_problems) == 2
    assert f"{hce.handle_value:#x}" in result.cleanup_problems[0]
    assert f"{closing.errors[1].handle_value:#x}" in result.cleanup_problems[1]  # type: ignore[union-attr]
    close_failures.assert_each_closed_once()


# --- the primary error survives an interrupted release (HANDLE-05) ---------------------

Pair = tuple[BaseException | None, BaseException]
UNCERTAIN, DONE = OperationState.UNCERTAIN, OperationState.DONE


def query_raising(monkeypatch: pytest.MonkeyPatch, path: Path, error: BaseException) -> None:
    """``winfileops.query`` raises ``error`` once, for the handle opened as ``path``."""
    real, pending = winfileops.query, [error]

    def query(handle: winfileops.Handle) -> object:
        if handle.path == path and pending:
            raise pending.pop()
        return real(handle)

    monkeypatch.setattr(winfileops, "query", query)


def release_raising(close_failures, path: Path, error: BaseException) -> None:  # type: ignore[no-untyped-def]
    """``CloseHandle`` of the handle opened as ``path`` raises ``error`` (after really
    closing it); for a ``HandleCloseError`` it returns ``FALSE`` (the handle stays open)."""
    close_failures.select = lambda handle: handle.path == path
    if isinstance(error, HCE):
        close_failures.limit = 1
    else:
        close_failures.limit, close_failures.interrupt = 0, error


def secondary_of(close_failures, error: BaseException) -> BaseException:  # type: ignore[no-untyped-def]
    """The release failure as raised: for a ``FALSE`` from ``CloseHandle``, the real one."""
    if not isinstance(error, HCE):
        return error
    failed = winfileops.HandleState.CLOSE_FAILED
    (handle,) = [h for h in close_failures.opened if h.state is failed]
    assert handle.close_error is not None and handle.close_error.handle_value == handle_raw(
        close_failures, handle
    )
    return handle.close_error


def handle_raw(close_failures, handle: object) -> int:  # type: ignore[no-untyped-def]
    (raw,) = [raw for raw, h in close_failures.handles.items() if h is handle]
    return raw


def creating(logs: list[Journal]) -> Callable[[], Journal]:
    def create() -> Journal:
        logs.append(Journal.create())
        return logs[-1]

    return create


def logged(logs: list[Journal], operation: str) -> list[tuple[str, OperationState, bool]]:
    (log,) = logs
    assert log.path is not None
    return [
        (Path(e.source or e.target or "").name, e.state, e.attempted)
        for e in read_journal(log.path).entries
        if e.operation == operation
    ]


def assert_released(close_failures, src: Path) -> None:  # type: ignore[no-untyped-def]
    """Every handle tried exactly once (none twice); the run holds nothing any more."""
    close_failures.assert_each_closed_once()
    close_failures.really_close()  # a FALSE from CloseHandle left that one open
    src.rename(src.with_name("released"))  # each was held without FILE_SHARE_DELETE


def assert_kept(raised: BaseException, primary: BaseException, secondary: BaseException) -> None:
    """``raised`` is the interrupt to reach the caller: the very object, with the other
    one kept as an object (``release_failures``) and in a note."""
    if not isinstance(primary, Exception):
        assert raised is primary  # not a new object of the same class either
        assert winfileops.release_failures(primary) == [secondary]
        assert f"then releasing failed: {winfileops.describe_error(secondary)}" in primary.__notes__
        assert sum(n.startswith("cleanup: cannot close ") for n in primary.__notes__) == 1
    else:  # an interrupt of the release is never dropped, nor the error before it
        assert raised is secondary
        assert winfileops.release_failures(secondary) == [primary]
        assert f"while releasing after {winfileops.describe_error(primary)}" in secondary.__notes__


# What ``query`` of the opened source file raises (or None); what closing it raises.
MOVE_MATRIX: dict[str, Callable[[], Pair]] = {
    "KeyboardInterrupt, then SystemExit": lambda: (KeyboardInterrupt("A"), SystemExit("B")),
    "KeyboardInterrupt twice": lambda: (KeyboardInterrupt("A"), KeyboardInterrupt("B")),
    "SystemExit, then KeyboardInterrupt": lambda: (SystemExit("A"), KeyboardInterrupt("B")),
    "SystemExit, then HandleCloseError": lambda: (
        SystemExit("A"),
        winfileops._close_error(0, Path("placeholder"), 31),  # CloseHandle returns FALSE
    ),
    "KeyboardInterrupt, then OSError": lambda: (KeyboardInterrupt("A"), OSError(6, "B")),
    "OSError, then SystemExit": lambda: (OSError(5, "A"), SystemExit("B")),
    "OSError twice": lambda: (OSError(5, "A"), OSError(6, "B")),
    "nothing, then KeyboardInterrupt": lambda: (None, KeyboardInterrupt("B")),
}


@pytest.mark.parametrize("case", list(MOVE_MATRIX))
def test_move_keeps_the_primary_when_releasing_the_source_fails(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures, case: str
) -> None:
    """``move`` after the source file was opened: its ``query`` fails or is interrupted
    (the primary), then closing the file fails or is interrupted as well."""
    primary, secondary = MOVE_MATRIX[case]()
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    if primary is not None:
        query_raising(monkeypatch, src / "a.pdf", primary)
    release_raising(close_failures, src / "a.pdf", secondary)
    logs: list[Journal] = []

    if isinstance(primary, Exception) and isinstance(secondary, Exception):
        result = run(plan_for(src), journal=creating(logs))  # both kept in the result
        assert outcome(result) == [
            ("a.pdf", FAILED, ExecReason.UNEXPECTED_ERROR),
            ("b.jpg", CANCELLED, ExecReason.CLEANUP_FAILED),
        ]
        assert result.items[0].detail == f"OSError: {primary}"
        assert result.cleanup_errors == (secondary,)
        assert logged(logs, "move") == [
            ("a.pdf", OperationState.FAILED, False),
            ("b.jpg", OperationState.CANCELLED, False),
        ]
        assert fingerprints(src) == before
        return assert_released(close_failures, src)

    with pytest.raises(BaseException) as raised:
        run(plan_for(src), journal=creating(logs))
    secondary = secondary_of(close_failures, secondary)
    if primary is None:  # nothing to keep: the interrupt of the release is raised
        assert raised.value is secondary
        assert winfileops.release_failures(secondary) == []
        notes = secondary.__notes__
        assert any(n.startswith("outcome unknown, check by hand: move of ") for n in notes)
        assert logged(logs, "move") == [("a.pdf", UNCERTAIN, True)]  # renamed, unverified
        assert fingerprints(src / "DEL" / "PDF") == {"a.pdf": before["a.pdf"]}
        assert fingerprints(src) == {"b.jpg": before["b.jpg"]}  # nothing after it
    else:
        assert_kept(raised.value, primary, secondary)
        if isinstance(secondary, HCE):  # its values, not only its text
            assert secondary.winerror == 31 and Path(secondary.filename) == src / "a.pdf"
            assert f"{secondary.handle_value:#x}" in "\n".join(primary.__notes__)
            close_failures.really_close()  # really still open: the file is locked
        assert logged(logs, "move") == []  # no intent: nothing was started
        assert fingerprints(src) == before
    assert_released(close_failures, src)


def test_execute_plan_keeps_an_interrupted_rename_over_an_interrupted_release(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    """The whole run with a fake ``rename_into``: it is interrupted before renaming
    (KeyboardInterrupt A), then closing the source file raises SystemExit B."""
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    interrupt, exit_ = KeyboardInterrupt("A"), SystemExit("B")

    def rename(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        raise interrupt

    monkeypatch.setattr(winfileops, "rename_into", rename)
    release_raising(close_failures, src / "a.pdf", exit_)
    logs: list[Journal] = []
    with pytest.raises(KeyboardInterrupt) as raised:
        run(plan_for(src), journal=creating(logs))

    assert_kept(raised.value, interrupt, exit_)
    assert any(
        n.startswith("outcome unknown, check by hand: move of ") for n in interrupt.__notes__
    )
    assert logged(logs, "move") == [("a.pdf", UNCERTAIN, True)]  # left for a manual check
    assert fingerprints(src) == before  # not renamed, nothing started after it
    assert_released(close_failures, src)


def test_move_keeps_the_interrupt_after_a_real_rename_over_an_interrupted_release(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    interrupt, exit_ = KeyboardInterrupt("A"), SystemExit("B")
    real = winfileops.rename_into

    def rename(handle: winfileops.Handle, directory: winfileops.Handle, name: str) -> None:
        real(handle, directory, name)
        raise interrupt  # the move happened, its outcome was never written

    monkeypatch.setattr(winfileops, "rename_into", rename)
    release_raising(close_failures, src / "a.pdf", exit_)
    logs: list[Journal] = []
    with pytest.raises(KeyboardInterrupt) as raised:
        run(plan_for(src), journal=creating(logs))

    assert_kept(raised.value, interrupt, exit_)
    assert any(
        n.startswith("outcome unknown, check by hand: move of ") for n in interrupt.__notes__
    )
    assert logged(logs, "move") == [("a.pdf", UNCERTAIN, True)]  # never a false DONE
    assert fingerprints(src / "DEL" / "PDF") == {"a.pdf": before["a.pdf"]}
    assert fingerprints(src) == {"b.jpg": before["b.jpg"]}  # no later move
    assert_released(close_failures, src)


# What checking the directory raises (``query``); what closing its handle raises.
DIRECTORY_CASES: dict[str, Callable[[], Pair]] = {
    "KeyboardInterrupt, then SystemExit": lambda: (KeyboardInterrupt("A"), SystemExit("B")),
    "SystemExit, then HandleCloseError": lambda: (
        SystemExit("A"),
        winfileops._close_error(0, Path("placeholder"), 31),
    ),
    "OSError, then KeyboardInterrupt": lambda: (OSError(5, "A"), KeyboardInterrupt("B")),
}


@pytest.mark.parametrize("case", list(DIRECTORY_CASES))
def test_open_dir_keeps_the_primary_when_releasing_the_directory_fails(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures, case: str
) -> None:
    """An existing category directory: its check fails or is interrupted, then its
    release. An ``OSError`` there becomes a refusal; it is kept on the interrupt."""
    primary, secondary = DIRECTORY_CASES[case]()
    category = src / "DEL" / "PDF"
    category.mkdir(parents=True)
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    assert primary is not None
    query_raising(monkeypatch, category, primary)
    release_raising(close_failures, category, secondary)
    logs: list[Journal] = []
    with pytest.raises(BaseException) as raised:
        run(plan_for(src), journal=creating(logs))

    assert_kept(raised.value, primary, secondary_of(close_failures, secondary))
    assert logged(logs, "move") == []  # no intent, no move
    assert fingerprints(src) == before
    assert_released(close_failures, src)


@pytest.mark.parametrize("case", list(DIRECTORY_CASES))
def test_mkdir_keeps_the_primary_when_releasing_the_new_directory_fails(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures, case: str
) -> None:
    """A category directory the run creates: its check fails or is interrupted, then its
    release; the handle is released once (the cleanup on the way out does not repeat
    it) and the directory's intent stays unanswered, for a manual check."""
    primary, secondary = DIRECTORY_CASES[case]()
    category = src / "DEL" / "PDF"
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    assert primary is not None
    query_raising(monkeypatch, category, primary)
    release_raising(close_failures, category, secondary)
    logs: list[Journal] = []
    with pytest.raises(BaseException) as raised:
        run(plan_for(src), journal=creating(logs))

    secondary = secondary_of(close_failures, secondary)
    assert_kept(raised.value, primary, secondary)
    cleanup = [n for n in raised.value.__notes__ if n.startswith("cleanup: ")]
    assert len(cleanup) == 1 and str(category) in cleanup[0]  # recorded once
    assert logged(logs, "mkdir") == [("DEL", DONE, True), ("PDF", UNCERTAIN, True)]
    assert logged(logs, "move") == []
    assert category.is_dir() and not any(category.iterdir())  # left as it is
    assert fingerprints(src) == before
    assert_released(close_failures, src)


def test_has_identity_keeps_the_primary_when_releasing_the_probe_fails(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    """Verifying a move: the probe of the target is interrupted, then its release."""
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    interrupt, exit_ = KeyboardInterrupt("A"), SystemExit("B")
    probe = src / "DEL" / "PDF" / "a.pdf"  # only the probe opens the file by this path
    query_raising(monkeypatch, probe, interrupt)
    release_raising(close_failures, probe, exit_)
    logs: list[Journal] = []
    with pytest.raises(KeyboardInterrupt) as raised:
        run(plan_for(src), journal=creating(logs))

    assert_kept(raised.value, interrupt, exit_)
    assert any(
        n.startswith("outcome unknown, check by hand: move of ") for n in interrupt.__notes__
    )
    assert logged(logs, "move") == [("a.pdf", UNCERTAIN, True)]  # not verified: no DONE
    assert fingerprints(src / "DEL" / "PDF") == {"a.pdf": before["a.pdf"]}
    assert fingerprints(src) == {"b.jpg": before["b.jpg"]}
    assert_released(close_failures, src)


# What proving the volume root raises; what releasing the root's handle raises.
ROOT_CASES: dict[str, Callable[[], Pair]] = {
    "KeyboardInterrupt, then SystemExit": lambda: (KeyboardInterrupt("A"), SystemExit("B")),
    "unverified root, then KeyboardInterrupt": lambda: (
        winfileops.UnverifiedRootError("A"),
        KeyboardInterrupt("B"),
    ),
}


@pytest.mark.parametrize("case", list(ROOT_CASES))
def test_open_root_keeps_the_primary_when_releasing_the_root_fails(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures, case: str
) -> None:
    primary, secondary = ROOT_CASES[case]()
    assert primary is not None
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    logs = [Journal.create()]  # proven before the failure is injected
    pending = [primary]

    def prove(handle: winfileops.Handle, root: object) -> None:
        raise pending.pop()

    monkeypatch.setattr(winfileops, "prove_volume_root", prove)
    release_raising(close_failures, Path(src.anchor), secondary)
    with pytest.raises(BaseException) as raised:
        run(plan_for(src), journal=lambda: logs[0])

    assert not pending  # proving the first root failed
    assert_kept(raised.value, primary, secondary)
    assert logged(logs, "move") == []
    assert fingerprints(src) == before
    assert_released(close_failures, src)


# --- a release failure before another one stays found (HANDLE-06) ---------------------


def held_files(directory: Path, *names: str) -> list[winfileops.Handle]:
    """Real handles of new files in ``directory`` (not in any plan), opened relative to
    a directory handle like the run opens its files; the directories are released."""
    touch(directory, *names)
    parent = winfileops.open_anchor(Path(directory.anchor))
    for part in directory.parts[1:]:
        child, _ = winfileops.open_relative(
            parent, part, directory=True, access=winfileops.FILE_READ_ATTRIBUTES,
            share=winfileops.SHARE_ALL,
        )  # fmt: skip
        parent.close()
        parent = child
    try:
        return [
            winfileops.open_relative(
                parent, name, directory=False, access=winfileops.FILE_READ_ATTRIBUTES,
                share=winfileops.SHARE_ALL,
            )[0]
            for name in names
        ]  # fmt: skip
    finally:
        parent.close()


class ReleasingOnClose(ClosingFails):
    """The journal's stream: ``close`` really closes it, then fails while it holds
    ``handles``, entered in order: each ``Handle.__exit__`` releases one, the last
    entered first, with the error the one after it raised."""

    def __init__(self, stream: object, handles: list[winfileops.Handle]) -> None:
        super().__init__(stream, None)
        self.handles, self.body = handles, OSError(5, "stream body")

    def close(self) -> None:
        super().close()
        with contextlib.ExitStack() as stack:
            for handle in self.handles:
                stack.enter_context(handle)
            raise self.body


def journal_releasing(handles: list[winfileops.Handle]) -> tuple[Journal, ReleasingOnClose]:
    log = Journal.create()
    stream = ReleasingOnClose(log._stream, handles)
    log._stream = stream  # type: ignore[assignment]
    return log, stream


def assert_closing(error: BaseException, handle: winfileops.Handle, close_failures) -> None:  # type: ignore[no-untyped-def]
    """``error`` is the very ``HandleCloseError`` of ``handle``, with its values."""
    assert error is handle.close_error and isinstance(error, HCE)
    assert error.winerror == 31 and error.handle_value in close_failures.failed
    assert error.handle_value == handle_raw(close_failures, handle)
    assert Path(error.filename) == handle.path
    assert handle.state is winfileops.HandleState.CLOSE_FAILED


def assert_moves(
    log: Journal, src: Path, before: dict[str, tuple[int, str]], names: list[str]
) -> None:
    """Only ``names`` were moved, each verified (DONE); nothing else was started."""
    assert log.path is not None
    moves = [
        (Path(e.source or "").name, e.state) for e in read_journal(log.path).entries
        if e.operation == "move"
    ]  # fmt: skip
    assert moves == [(name, DONE) for name in names]
    moved = {"a.pdf": src / "DEL" / "PDF", "b.jpg": src / "DEL" / "Images"}
    for name in ("a.pdf", "b.jpg"):
        place = moved[name] if name in names else src
        assert fingerprints(place)[name] == before[name]
    assert {p.name for p in src.iterdir() if p.is_file()} == {"a.pdf", "b.jpg"} - set(names)


@pytest.mark.parametrize("ending", ["KeyboardInterrupt", "nothing"])
def test_nested_failed_releases_in_the_cleanup_are_all_found(
    src: Path, area: Path, close_failures, ending: str
) -> None:
    """Two failed ``__exit__`` in a row while closing the journal: ``ReleaseError`` in
    ``ReleaseError``; both failed closes reach the caller, in the order they failed."""
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    first, second = held = held_files(area / "held", "first.bin", "second.bin")
    log, stream = journal_releasing(held)
    fail_closing(close_failures, first.path, second.path)
    interrupt = KeyboardInterrupt()

    if ending == "KeyboardInterrupt":
        with pytest.raises(KeyboardInterrupt) as raised:
            run(plan_for(src), cancel=interrupt_at_second_file(interrupt), journal=lambda: log)
        assert raised.value is interrupt
        closing, *failures = winfileops.release_failures(interrupt)
        assert interrupt.release_errors == (closing,)  # type: ignore[attr-defined]
        assert_moves(log, src, before, ["a.pdf"])  # no next move, no false DONE
    else:
        result = run(plan_for(src), journal=lambda: log)
        assert [r.status for r in result.items] == [MOVED, MOVED]
        assert not result.complete and result.stopped is None
        (closing,) = result.cleanup_errors
        failures = winfileops.release_failures(closing)
        assert len(result.cleanup_problems) == 1
        assert_moves(log, src, before, ["a.pdf", "b.jpg"])

    assert isinstance(closing, journal.JournalCloseError)
    (outer,) = closing.errors  # what the stream's close raised
    assert isinstance(outer, winfileops.ReleaseError)
    inner = outer.error
    assert isinstance(inner, winfileops.ReleaseError) and inner.error is stream.body
    assert len(failures) == 3 and failures[0] is outer  # not ``inner``, not the body
    assert_closing(failures[1], second, close_failures)  # second's __exit__ ran first
    assert_closing(failures[2], first, close_failures)
    assert sorted(e.handle_value for e in failures[1:]) == sorted(close_failures.failed)  # type: ignore[attr-defined]
    assert all(e is not stream.body and e is not inner for e in failures)
    assert_released(close_failures, src)


@pytest.mark.parametrize("ending", ["KeyboardInterrupt", "nothing"])
def test_failed_releases_then_an_interrupted_one_in_the_cleanup(
    src: Path, area: Path, close_failures, ending: str
) -> None:
    """Closing the journal: two ``__exit__`` fail ``CloseHandle``, then the third one's
    raises ``SystemExit``. The run's ``KeyboardInterrupt`` stays the one raised; without
    one, the ``SystemExit`` is; either way with both failed closes before it."""
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    held = held_files(area / "held", "first.bin", "second.bin", "third.bin")
    first, second, third = held
    log, stream = journal_releasing(held)
    exit_ = SystemExit(4)

    def select(handle: object) -> bool:
        if handle is first:  # the last one released: its CloseHandle raises SystemExit
            close_failures.interrupt = exit_
        return any(handle is h for h in held)

    close_failures.select, close_failures.limit = select, 2
    interrupt = KeyboardInterrupt()

    if ending == "KeyboardInterrupt":
        with pytest.raises(KeyboardInterrupt) as raised:
            run(plan_for(src), cancel=interrupt_at_second_file(interrupt), journal=lambda: log)
        assert raised.value is interrupt  # not replaced by SystemExit, nor by a new object
        assert interrupt.release_errors == (exit_,)  # type: ignore[attr-defined]
        kept, *failures = winfileops.release_failures(interrupt)
        assert kept is exit_
        notes = interrupt.__notes__
        assert any(n.startswith("cleanup: cannot close journal: SystemExit: 4") for n in notes)
        assert_moves(log, src, before, ["a.pdf"])
    else:
        with pytest.raises(SystemExit) as raised:
            run(plan_for(src), journal=lambda: log)
        assert raised.value is exit_  # an interrupt of the cleanup is never dropped
        failures = winfileops.release_failures(exit_)
        assert_moves(log, src, before, ["a.pdf", "b.jpg"])

    (released,) = exit_.release_errors  # type: ignore[attr-defined]
    assert isinstance(released, winfileops.ReleaseError)
    assert isinstance(released.error, winfileops.ReleaseError)
    assert released.error.error is stream.body
    assert len(failures) == 3 and failures[0] is released
    assert_closing(failures[1], third, close_failures)  # third's __exit__ ran first
    assert_closing(failures[2], second, close_failures)
    assert sorted(close_failures.failed) == sorted(e.handle_value for e in failures[1:])  # type: ignore[attr-defined]
    assert f"while releasing after {winfileops.describe_error(released)}" in exit_.__notes__
    assert [h for h in close_failures.closed if h is first] == [first]  # tried once too
    assert_released(close_failures, src)
