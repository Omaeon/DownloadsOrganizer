from __future__ import annotations

import ctypes
import dataclasses
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from downloads_organizer import pathsafety, planner
from downloads_organizer.catalog import Category
from downloads_organizer.extensions import ExtensionTable
from downloads_organizer.models import (
    CollisionPolicy,
    Inventory,
    OutsideGroup,
    Plan,
    PlanAction,
    PlanErrorReason,
    SkipReason,
)
from downloads_organizer.pathsafety import (
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    UnsafeFolderNameError,
    path_key,
)
from downloads_organizer.planner import build_plan, plan_with_inventory

TABLE = ExtensionTable()
MOVE, RENAME, SKIP = PlanAction.MOVE, PlanAction.RENAME, PlanAction.SKIP


def touch(directory: Path, *names: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text(f"content of {name}", encoding="utf-8")


def summary(plan: Plan) -> list[tuple[str, str, PlanAction, SkipReason | None]]:
    """(source name, target relative to the plan root, action, reason) per item."""
    assert plan.destination_root is not None
    return [
        (
            i.source.name,
            i.target.relative_to(plan.destination_root).as_posix(),
            i.action,
            i.reason,
        )
        for i in plan.items
    ]


@pytest.fixture
def plan_readonly(tmp_path: Path, snapshot, no_side_effects) -> Callable[..., Plan]:
    """``build_plan`` that asserts the temporary tree is unchanged by the call."""

    def run(*args: object, **kwargs: object) -> Plan:
        before = snapshot(tmp_path)
        with no_side_effects():
            plan = build_plan(*args, **kwargs)
        assert snapshot(tmp_path) == before
        return plan

    return run


@pytest.fixture
def src(tmp_path: Path) -> Path:
    path = tmp_path / "Downloads"
    path.mkdir()
    return path


# --- basic routing ------------------------------------------------------------------


def test_default_destination_is_del_inside_source(src: Path, plan_readonly):
    touch(src, "a.pdf", "b.JPG", "c.unknown", "d.txt", "noext", ".hidden_dotfile")
    plan = plan_readonly(src, None, TABLE, {".pdf", ".jpg"})
    assert plan.ok
    assert plan.destination_root == src / "DEL"
    assert not plan.explicit_destination
    assert summary(plan) == [
        ("a.pdf", "PDF/a.pdf", MOVE, None),
        ("b.JPG", "Images/b.JPG", MOVE, None),
    ]
    assert [i.extension for i in plan.items] == [".pdf", ".jpg"]
    assert [i.category_key for i in plan.items] == ["PDF", "Images"]
    assert plan.directories_to_create == (src / "DEL", src / "DEL" / "Images", src / "DEL" / "PDF")
    assert not (src / "DEL").exists()


def test_explicit_existing_destination_uses_category_folders(
    tmp_path: Path, src: Path, plan_readonly
):
    dest = tmp_path / "Sorted"
    (dest / "PDF").mkdir(parents=True)
    touch(src, "a.pdf", "song.mp3")
    plan = plan_readonly(src, dest, TABLE, {".pdf", ".mp3"})
    assert plan.ok
    assert plan.explicit_destination
    assert [i.target for i in plan.items] == [dest / "PDF" / "a.pdf", dest / "Audio" / "song.mp3"]
    assert plan.directories_to_create == (dest / "Audio",)


def test_missing_destination_is_planned_for_creation_but_not_created(
    tmp_path: Path, src: Path, plan_readonly
):
    dest = tmp_path / "new" / "sub"
    touch(src, "a.pdf")
    plan = plan_readonly(src, str(dest), TABLE, {".pdf"})
    assert plan.ok
    assert plan.items[0].target == dest / "PDF" / "a.pdf"
    assert plan.directories_to_create == (tmp_path / "new", dest, dest / "PDF")
    assert not (tmp_path / "new").exists()


def test_existing_del_folder_is_allowed_and_not_an_input(src: Path, plan_readonly):
    touch(src / "DEL" / "PDF", "old.pdf")
    touch(src, "new.pdf")
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    assert plan.ok
    assert summary(plan) == [("new.pdf", "PDF/new.pdf", MOVE, None)]
    assert plan.directories_to_create == ()


def test_destination_inside_source_is_allowed(src: Path, plan_readonly):
    touch(src, "a.pdf")
    plan = plan_readonly(src, src / "Sorted", TABLE, {".pdf"})
    assert plan.ok
    assert plan.items[0].target == src / "Sorted" / "PDF" / "a.pdf"


def test_empty_source_gives_empty_plan(src: Path, plan_readonly):
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    assert plan.ok
    assert plan.items == ()
    assert plan.directories_to_create == ()


def test_only_selected_extensions_are_planned(src: Path, plan_readonly):
    touch(src, "a.jpg", "b.png", "c.PNG", "d.bmp", "e.pdf")
    plan = plan_readonly(src, None, TABLE, {".png", ".bmp"})
    assert [i.source.name for i in plan.items] == ["b.png", "c.PNG", "d.bmp"]
    assert {i.extension for i in plan.items} == {".png", ".bmp"}


def test_custom_extension_goes_to_its_category(src: Path, plan_readonly):
    table = TABLE.with_custom(".heic", "Images")
    touch(src, "photo.HEIC", "photo.pdf")
    plan = plan_readonly(src, None, table, {".heic"})
    assert summary(plan) == [("photo.HEIC", "Images/photo.HEIC", MOVE, None)]


def test_no_recursion_and_folders_are_never_inputs(src: Path, plan_readonly):
    touch(src / "sub", "inner.pdf")
    (src / "folder.pdf").mkdir()
    touch(src, "top.pdf")
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    assert [i.source for i in plan.items] == [src / "top.pdf"]


def test_items_are_sorted_case_insensitively_and_deterministically(src: Path, plan_readonly):
    touch(src, "b.pdf", "C.pdf", "a.pdf", "A_2.pdf")
    first = plan_readonly(src, None, TABLE, {".pdf"})
    second = plan_readonly(src, None, TABLE, {".pdf"})
    assert [i.source.name for i in first.items] == ["a.pdf", "A_2.pdf", "b.pdf", "C.pdf"]
    assert first == second


# --- metadata snapshot --------------------------------------------------------------


def test_items_carry_metadata_snapshot(src: Path, plan_readonly):
    touch(src, "a.pdf")
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    st = (src / "a.pdf").stat(follow_symlinks=False)
    info = plan.items[0].file
    assert info.size == st.st_size == len("content of a.pdf")
    assert info.mtime_ns == st.st_mtime_ns
    assert info.attributes == getattr(st, "st_file_attributes", None)
    if st.st_ino and st.st_dev:
        assert (info.file_id, info.device) == (st.st_ino, st.st_dev)


# --- collisions ---------------------------------------------------------------------


def test_rename_uses_legacy_suffix_scheme(tmp_path: Path, src: Path, plan_readonly):
    dest = tmp_path / "dest"
    touch(dest / "PDF", "a.pdf", "a_1.pdf")
    touch(dest / "Images", "Photo.JPG")
    touch(dest / "Archive", "backup.tar.gz")
    touch(src, "a.pdf", "Photo.JPG", "backup.tar.gz")
    plan = plan_readonly(src, dest, TABLE, {".pdf", ".jpg", ".gz"})
    assert summary(plan) == [
        ("a.pdf", "PDF/a_2.pdf", RENAME, None),
        ("backup.tar.gz", "Archive/backup.tar_1.gz", RENAME, None),
        ("Photo.JPG", "Images/Photo_1.JPG", RENAME, None),
    ]


def test_collisions_ignore_case(tmp_path: Path, src: Path, plan_readonly):
    dest = tmp_path / "dest"
    touch(dest / "PDF", "REPORT.PDF", "Report_1.pdf")
    touch(src, "report.pdf")
    plan = plan_readonly(src, dest, TABLE, {".pdf"})
    assert summary(plan) == [("report.pdf", "PDF/report_2.pdf", RENAME, None)]


def test_names_are_reserved_within_one_plan(tmp_path: Path, src: Path, plan_readonly):
    dest = tmp_path / "dest"
    touch(dest / "PDF", "a.pdf")
    touch(src, "a.pdf", "a_1.pdf", "A_1_1.pdf")
    plan = plan_readonly(src, dest, TABLE, {".pdf"})
    assert summary(plan) == [
        ("a.pdf", "PDF/a_1.pdf", RENAME, None),
        ("a_1.pdf", "PDF/a_1_1.pdf", RENAME, None),
        ("A_1_1.pdf", "PDF/A_1_1_1.pdf", RENAME, None),
    ]
    targets = [str(i.target).casefold() for i in plan.items]
    assert len(targets) == len(set(targets))


def test_skip_policy_skips_taken_names(tmp_path: Path, src: Path, plan_readonly):
    dest = tmp_path / "dest"
    touch(dest / "PDF", "A.PDF")
    touch(src, "a.pdf", "b.pdf", "c.mp3")
    plan = plan_readonly(src, dest, TABLE, {".pdf", ".mp3"}, CollisionPolicy.SKIP)
    assert plan.policy is CollisionPolicy.SKIP
    assert summary(plan) == [
        ("a.pdf", "PDF/a.pdf", SKIP, SkipReason.TARGET_EXISTS),
        ("b.pdf", "PDF/b.pdf", MOVE, None),
        ("c.mp3", "Audio/c.mp3", MOVE, None),
    ]
    assert plan.directories_to_create == (dest / "Audio",)


def test_dangling_or_unusual_entries_in_destination_count_as_taken(
    tmp_path: Path, src: Path, plan_readonly
):
    dest = tmp_path / "dest"
    (dest / "PDF" / "a.pdf").mkdir(parents=True)  # a folder with the file's name
    touch(src, "a.pdf")
    plan = plan_readonly(src, dest, TABLE, {".pdf"})
    assert summary(plan) == [("a.pdf", "PDF/a_1.pdf", RENAME, None)]


@pytest.mark.windows_only
def test_case_only_duplicates_in_case_sensitive_source(
    tmp_path: Path, src: Path, make_case_sensitive, plan_readonly
):
    make_case_sensitive(src)
    touch(src, "A.pdf", "a.pdf")
    assert sorted(p.name for p in src.iterdir()) == ["A.pdf", "a.pdf"]
    plan = plan_readonly(src, tmp_path / "dest", TABLE, {".pdf"})
    assert summary(plan) == [
        ("A.pdf", "PDF/A.pdf", MOVE, None),
        ("a.pdf", "PDF/a_1.pdf", RENAME, None),
    ]
    skip = plan_readonly(src, tmp_path / "dest", TABLE, {".pdf"}, CollisionPolicy.SKIP)
    assert [(i.action, i.reason) for i in skip.items] == [
        (MOVE, None),
        (SKIP, SkipReason.TARGET_RESERVED),
    ]


def test_rename_gives_up_after_max_attempts(tmp_path: Path, src: Path, monkeypatch, plan_readonly):
    monkeypatch.setattr(planner, "MAX_RENAME_ATTEMPTS", 2)
    dest = tmp_path / "dest"
    touch(dest / "PDF", "a.pdf", "a_1.pdf", "a_2.pdf")
    touch(src, "a.pdf")
    plan = plan_readonly(src, dest, TABLE, {".pdf"})
    assert summary(plan) == [("a.pdf", "PDF/a.pdf", SKIP, SkipReason.NO_FREE_NAME)]


def test_rename_that_would_exceed_name_limit_is_skipped(
    tmp_path: Path, src: Path, monkeypatch, plan_readonly
):
    monkeypatch.setattr(pathsafety, "MAX_NAME_UTF16_UNITS", len("a.pdf"))
    dest = tmp_path / "dest"
    touch(dest / "PDF", "a.pdf")
    touch(src, "a.pdf")
    plan = plan_readonly(src, dest, TABLE, {".pdf"})
    assert [(i.action, i.reason) for i in plan.items] == [(SKIP, SkipReason.NAME_TOO_LONG)]


# --- files that must not be moved ---------------------------------------------------


def test_partial_downloads_and_their_final_names_are_skipped(src: Path, plan_readonly):
    touch(src, "file.pdf", "file.pdf.part", "x.zip", "x.zip.CRDOWNLOAD", "y.pdf", "z.part")
    plan = plan_readonly(src, None, TABLE, {".pdf", ".zip"})
    assert summary(plan) == [
        ("file.pdf", "PDF/file.pdf", SKIP, SkipReason.PARTIAL_DOWNLOAD),
        ("x.zip", "Archive/x.zip", SKIP, SkipReason.PARTIAL_DOWNLOAD),
        ("y.pdf", "PDF/y.pdf", MOVE, None),
    ]
    assert plan.directories_to_create == (src / "DEL", src / "DEL" / "PDF")


@pytest.mark.windows_only
def test_hidden_system_and_offline_files_are_skipped(src: Path, set_attributes, plan_readonly):
    touch(src, "hidden.pdf", "system.pdf", "offline.pdf", "plain.pdf", "readonly.pdf")
    set_attributes(src / "hidden.pdf", stat.FILE_ATTRIBUTE_HIDDEN)
    set_attributes(src / "system.pdf", stat.FILE_ATTRIBUTE_SYSTEM)
    set_attributes(src / "offline.pdf", stat.FILE_ATTRIBUTE_OFFLINE)
    set_attributes(src / "readonly.pdf", stat.FILE_ATTRIBUTE_READONLY)
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    assert [(i.source.name, i.action, i.reason) for i in plan.items] == [
        ("hidden.pdf", SKIP, SkipReason.HIDDEN),
        ("offline.pdf", SKIP, SkipReason.OFFLINE),
        ("plain.pdf", MOVE, None),
        ("readonly.pdf", MOVE, None),
        ("system.pdf", SKIP, SkipReason.SYSTEM),
    ]
    (src / "readonly.pdf").chmod(stat.S_IWRITE)  # let tmp_path cleanup remove it


@pytest.mark.windows_only
def test_junction_in_source_is_ignored_and_not_followed(
    tmp_path: Path, src: Path, make_junction, plan_readonly
):
    outside = tmp_path / "outside"
    touch(outside, "secret.pdf")
    make_junction(src / "link.pdf", outside)
    make_junction(src / "linkdir", outside)
    touch(src, "a.pdf")
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    assert [i.source for i in plan.items] == [src / "a.pdf"]


def test_file_symlink_in_source_is_skipped(tmp_path: Path, src: Path, plan_readonly):
    touch(tmp_path, "real.pdf")
    try:
        (src / "link.pdf").symlink_to(tmp_path / "real.pdf")
    except OSError as exc:  # needs Developer Mode or a privilege on Windows
        pytest.skip(f"cannot create symlinks: {exc}")
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    assert [(i.source.name, i.action, i.reason) for i in plan.items] == [
        ("link.pdf", SKIP, SkipReason.SYMLINK)
    ]


class _FakeEntry:
    def __init__(self, entry: os.DirEntry[str], st: os.stat_result) -> None:
        self.name, self.path, self._st = entry.name, entry.path, st

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        return self._st


def _with_listing_attributes(
    monkeypatch, directory: Path, names: dict[str, tuple[object, object]]
) -> list[str]:
    """Make listing ``directory`` report other attributes/tag for some names (``None``: unknown).

    Returns the names ``planner.lstat`` was called for.
    """
    real_scandir, real_lstat = os.scandir, planner.lstat
    lstat_calls: list[str] = []

    class Listing:
        def __init__(self, path: str) -> None:
            self._it = real_scandir(path)
            self._fake = Path(path) == directory

        def __enter__(self) -> Listing:
            return self

        def __exit__(self, *exc: object) -> None:
            self._it.close()

        def __iter__(self) -> Listing:
            return self

        def __next__(self) -> object:
            entry = next(self._it)
            if not self._fake or entry.name not in names:
                return entry
            attrs, tag = names[entry.name]
            st = entry.stat(follow_symlinks=False)
            fake = os.stat_result(
                (st.st_mode, 0, 0, 1, 0, 0, st.st_size, 0, 0, 0),
                {"st_file_attributes": attrs, "st_reparse_tag": tag, "st_mtime_ns": st.st_mtime_ns},
            )
            return _FakeEntry(entry, fake)

    def spy_lstat(path: os.PathLike[str]) -> os.stat_result:
        lstat_calls.append(Path(path).name)
        return real_lstat(path)

    monkeypatch.setattr(os, "scandir", Listing)
    monkeypatch.setattr(planner, "lstat", spy_lstat)
    return lstat_calls


def test_cloud_placeholder_attributes_are_skipped_without_opening(
    src: Path, monkeypatch, plan_readonly
):
    # Substituted listing data: this checks the classification, not a real OneDrive.
    touch(src, "cloud.pdf", "disguised.pdf", "plain.pdf")
    reparse = stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_ARCHIVE
    calls = _with_listing_attributes(
        monkeypatch,
        src,
        {
            "cloud.pdf": (reparse, 0x9000701A),
            "disguised.pdf": (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, 0),
        },
    )
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    assert [(i.source.name, i.action, i.reason) for i in plan.items] == [
        ("cloud.pdf", SKIP, SkipReason.CLOUD_PLACEHOLDER),
        ("disguised.pdf", SKIP, SkipReason.CLOUD_PLACEHOLDER),
        ("plain.pdf", MOVE, None),
    ]
    assert "cloud.pdf" not in calls
    assert "disguised.pdf" not in calls
    assert plan.items[0].file.file_id is None  # snapshot taken from the listing only


# --- invalid source and destination -------------------------------------------------


def _errors(plan: Plan) -> list[tuple[PlanErrorReason, Path | None]]:
    assert plan.items == ()
    assert plan.directories_to_create == ()
    return [(e.reason, e.path) for e in plan.errors]


def test_missing_source(tmp_path: Path, plan_readonly):
    plan = plan_readonly(tmp_path / "nope", None, TABLE, {".pdf"})
    assert not plan.ok
    assert _errors(plan) == [(PlanErrorReason.SOURCE_MISSING, tmp_path / "nope")]


def test_source_is_a_file(tmp_path: Path, plan_readonly):
    touch(tmp_path, "file.pdf")
    plan = plan_readonly(tmp_path / "file.pdf", None, TABLE, {".pdf"})
    assert _errors(plan) == [(PlanErrorReason.SOURCE_NOT_DIRECTORY, tmp_path / "file.pdf")]


def test_relative_source_and_destination_are_rejected(src: Path, plan_readonly):
    plan = plan_readonly("Downloads", "out", TABLE, {".pdf"})
    assert [e.reason for e in plan.errors] == [
        PlanErrorReason.SOURCE_INVALID_PATH,
        PlanErrorReason.DESTINATION_INVALID_PATH,
    ]
    assert plan.items == ()


def test_errors_are_collected_together(tmp_path: Path, src: Path, plan_readonly):
    touch(tmp_path, "file")
    plan = plan_readonly(src, tmp_path / "file" / "dest", TABLE, set())
    assert _errors(plan) == [
        (PlanErrorReason.DESTINATION_NOT_DIRECTORY, tmp_path / "file"),
        (PlanErrorReason.NOTHING_SELECTED, None),
    ]


def test_del_that_is_a_file_is_a_destination_error(src: Path, plan_readonly):
    touch(src, "DEL", "a.pdf")
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    assert _errors(plan) == [(PlanErrorReason.DESTINATION_NOT_DIRECTORY, src / "DEL")]


@pytest.mark.parametrize(
    "variant",
    [
        lambda p: p,
        lambda p: str(p) + os.sep,
        lambda p: p / "sub" / "..",
        pytest.param(lambda p: str(p).upper(), marks=pytest.mark.windows_only),
    ],
)
def test_destination_equal_to_source_is_rejected(src: Path, plan_readonly, variant):
    touch(src, "a.pdf")
    plan = plan_readonly(src, variant(src), TABLE, {".pdf"})
    assert [e.reason for e in plan.errors] == [PlanErrorReason.SAME_DIRECTORY]
    assert plan.items == ()


def test_category_folder_equal_to_source_is_skipped(tmp_path: Path, plan_readonly):
    source = tmp_path / "x" / "PDF"
    touch(source, "a.pdf", "b.mp3")
    plan = plan_readonly(source, tmp_path / "x", TABLE, {".pdf", ".mp3"})
    assert summary(plan) == [
        ("a.pdf", "PDF/a.pdf", SKIP, SkipReason.TARGET_DIR_IS_SOURCE),
        ("b.mp3", "Audio/b.mp3", MOVE, None),
    ]


def test_category_path_that_is_a_file_is_skipped(tmp_path: Path, src: Path, plan_readonly):
    dest = tmp_path / "dest"
    touch(dest, "PDF")
    touch(src, "a.pdf")
    plan = plan_readonly(src, dest, TABLE, {".pdf"})
    item = plan.items[0]
    assert (item.action, item.reason, item.detail) == (
        SKIP,
        SkipReason.TARGET_DIR_INVALID,
        "not a directory",
    )
    assert plan.directories_to_create == ()


@pytest.mark.windows_only
def test_source_junction_or_source_below_junction_is_rejected(
    tmp_path: Path, make_junction, plan_readonly
):
    real = tmp_path / "real"
    touch(real / "inner", "a.pdf")
    link = make_junction(tmp_path / "link", real)
    for source in (link, link / "inner"):
        plan = plan_readonly(source, None, TABLE, {".pdf"})
        assert _errors(plan) == [(PlanErrorReason.SOURCE_REPARSE_POINT, link)]


@pytest.mark.windows_only
def test_destination_junction_at_any_depth_is_rejected(
    tmp_path: Path, src: Path, make_junction, plan_readonly
):
    real = tmp_path / "real"
    (real / "inner").mkdir(parents=True)
    link = make_junction(tmp_path / "link", real)
    touch(src, "a.pdf")
    for dest in (link, link / "inner", link / "inner" / "missing"):
        plan = plan_readonly(src, dest, TABLE, {".pdf"})
        assert _errors(plan) == [(PlanErrorReason.DESTINATION_REPARSE_POINT, link)]


@pytest.mark.windows_only
def test_del_junction_is_rejected(tmp_path: Path, src: Path, make_junction, plan_readonly):
    (tmp_path / "elsewhere").mkdir()
    make_junction(src / "DEL", tmp_path / "elsewhere")
    touch(src, "a.pdf")
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    assert _errors(plan) == [(PlanErrorReason.DESTINATION_REPARSE_POINT, src / "DEL")]


@pytest.mark.windows_only
def test_category_folder_junction_is_skipped(
    tmp_path: Path, src: Path, make_junction, plan_readonly
):
    (tmp_path / "elsewhere").mkdir()
    (src / "DEL").mkdir()
    make_junction(src / "DEL" / "PDF", tmp_path / "elsewhere")
    touch(src, "a.pdf", "b.mp3")
    plan = plan_readonly(src, None, TABLE, {".pdf", ".mp3"})
    assert [(i.source.name, i.action, i.reason, i.detail) for i in plan.items] == [
        ("a.pdf", SKIP, SkipReason.TARGET_DIR_INVALID, "reparse point"),
        ("b.mp3", MOVE, None, None),
    ]
    assert plan.directories_to_create == (src / "DEL" / "Audio",)


# --- unreadable and failing objects -------------------------------------------------


@pytest.mark.windows_only
def test_source_without_list_permission(tmp_path: Path, src: Path, deny_listing, no_side_effects):
    touch(src, "a.pdf")
    deny_listing(src)
    with no_side_effects():
        plan = build_plan(src, None, TABLE, {".pdf"})
    assert [(e.reason, e.path) for e in plan.errors] == [
        (PlanErrorReason.SOURCE_ACCESS_DENIED, src)
    ]
    assert plan.items == ()
    assert not (src / "DEL").exists()


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (PermissionError(13, "denied"), PlanErrorReason.SOURCE_ACCESS_DENIED),
        (FileNotFoundError(2, "gone"), PlanErrorReason.SOURCE_MISSING),
        (OSError(5, "I/O error"), PlanErrorReason.SOURCE_UNREADABLE),
    ],
)
def test_listing_failure_midway_discards_partial_results(
    tmp_path: Path, src: Path, snapshot, no_side_effects, error: OSError, reason: PlanErrorReason
):
    touch(src, "a.pdf", "b.pdf", "c.pdf")
    real_scandir = os.scandir

    class Failing:
        def __init__(self, path: str) -> None:
            self._it = real_scandir(path)
            self._seen = 0

        def __enter__(self) -> Failing:
            return self

        def __exit__(self, *exc: object) -> None:
            self._it.close()

        def __iter__(self) -> Failing:
            return self

        def __next__(self) -> os.DirEntry[str]:
            if self._seen == 1:
                raise error
            self._seen += 1
            return next(self._it)

    before = snapshot(tmp_path)
    with pytest.MonkeyPatch.context() as mp, no_side_effects():
        mp.setattr(os, "scandir", Failing)
        plan = build_plan(src, None, TABLE, {".pdf"})
    assert snapshot(tmp_path) == before
    assert [(e.reason, e.path) for e in plan.errors] == [(reason, src)]
    assert plan.items == ()


def _fail_lstat_for(monkeypatch, target: Path, error: OSError) -> None:
    real_lstat = planner.lstat

    def faulty(path: os.PathLike[str]) -> os.stat_result:
        if Path(path) == target:
            raise error
        return real_lstat(path)

    monkeypatch.setattr(planner, "lstat", faulty)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (FileNotFoundError(2, "gone"), SkipReason.VANISHED),
        (PermissionError(13, "denied"), SkipReason.STAT_FAILED),
        (OSError(1392, "corrupt"), SkipReason.STAT_FAILED),
    ],
)
def test_unreadable_source_file_is_skipped_others_continue(
    src: Path, monkeypatch, plan_readonly, error: OSError, reason: SkipReason
):
    touch(src, "a.pdf", "b.pdf", "c.pdf")
    _fail_lstat_for(monkeypatch, src / "b.pdf", error)
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    assert [(i.source.name, i.action, i.reason) for i in plan.items] == [
        ("a.pdf", MOVE, None),
        ("b.pdf", SKIP, reason),
        ("c.pdf", MOVE, None),
    ]
    assert plan.items[1].detail is not None


def test_failed_target_name_check_is_skipped(tmp_path: Path, src: Path, monkeypatch, plan_readonly):
    dest = tmp_path / "dest"
    (dest / "PDF").mkdir(parents=True)
    touch(src, "a.pdf", "b.pdf")
    _fail_lstat_for(monkeypatch, dest / "PDF" / "a.pdf", PermissionError(13, "denied"))
    plan = plan_readonly(src, dest, TABLE, {".pdf"})
    assert [(i.source.name, i.action, i.reason) for i in plan.items] == [
        ("a.pdf", SKIP, SkipReason.TARGET_CHECK_FAILED),
        ("b.pdf", MOVE, None),
    ]


def test_failed_category_folder_check_skips_its_files(
    tmp_path: Path, src: Path, monkeypatch, plan_readonly
):
    dest = tmp_path / "dest"
    (dest / "PDF").mkdir(parents=True)
    touch(src, "a.pdf", "b.mp3")
    _fail_lstat_for(monkeypatch, dest / "PDF", PermissionError(13, "denied"))
    plan = plan_readonly(src, dest, TABLE, {".pdf", ".mp3"})
    assert [(i.source.name, i.action, i.reason) for i in plan.items] == [
        ("a.pdf", SKIP, SkipReason.TARGET_CHECK_FAILED),
        ("b.mp3", MOVE, None),
    ]


def test_file_that_turned_into_a_link_while_planning_is_skipped(
    src: Path, monkeypatch, plan_readonly
):
    touch(src, "a.pdf")
    real_lstat = planner.lstat

    def changed(path: os.PathLike[str]) -> os.stat_result:
        st = real_lstat(path)
        if Path(path) != src / "a.pdf":
            return st
        return os.stat_result(
            (stat.S_IFLNK | 0o777, *tuple(st)[1:10]),
            {"st_file_attributes": 0, "st_reparse_tag": 0, "st_mtime_ns": st.st_mtime_ns},
        )

    monkeypatch.setattr(planner, "lstat", changed)
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    item = plan.items[0]
    assert (item.action, item.reason, item.detail) == (
        SKIP,
        SkipReason.SYMLINK,
        "changed while planning",
    )


# --- API errors ---------------------------------------------------------------------


def test_nothing_selected_is_a_plan_error(src: Path, plan_readonly):
    touch(src, "a.pdf")
    plan = plan_readonly(src, None, TABLE, set())
    assert [e.reason for e in plan.errors] == [PlanErrorReason.NOTHING_SELECTED]
    assert plan.items == ()


def test_unknown_selected_extension_raises(src: Path):
    with pytest.raises(ValueError, match="heic"):
        build_plan(src, None, TABLE, {".heic"})
    with pytest.raises(ValueError):
        build_plan(src, None, TABLE, {".part"})


def test_selected_must_not_be_a_plain_string(src: Path):
    with pytest.raises(TypeError):
        build_plan(src, None, TABLE, ".pdf")


def test_unknown_policy_raises(src: Path):
    with pytest.raises(ValueError):
        build_plan(src, None, TABLE, {".pdf"}, "overwrite")  # type: ignore[arg-type]


# --- plans are snapshots ------------------------------------------------------------


def test_filesystem_changes_between_calls_are_not_folded_into_old_plan(
    tmp_path: Path, src: Path, plan_readonly
):
    dest = tmp_path / "dest"
    touch(src, "a.pdf", "b.pdf")
    first = plan_readonly(src, dest, TABLE, {".pdf"})
    frozen = dataclasses.replace(first)
    touch(dest / "PDF", "a.pdf")  # target becomes occupied after the preview
    (src / "b.pdf").unlink()
    touch(src, "c.pdf")
    second = plan_readonly(src, dest, TABLE, {".pdf"})

    assert first == frozen
    assert [(i.source.name, i.target.name, i.action) for i in first.items] == [
        ("a.pdf", "a.pdf", MOVE),
        ("b.pdf", "b.pdf", MOVE),
    ]
    assert [(i.source.name, i.target.name, i.action) for i in second.items] == [
        ("a.pdf", "a_1.pdf", RENAME),
        ("c.pdf", "c.pdf", MOVE),
    ]
    assert second.directories_to_create == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.items[0].target = dest / "other.pdf"  # type: ignore[misc]


def test_plan_snapshot_reveals_file_change_after_preview(src: Path, plan_readonly):
    touch(src, "a.pdf")
    before = plan_readonly(src, None, TABLE, {".pdf"}).items[0].file
    (src / "a.pdf").write_text("a longer content than before", encoding="utf-8")
    after = plan_readonly(src, None, TABLE, {".pdf"}).items[0].file
    assert before.size != after.size
    if before.file_id is not None:
        assert before.file_id == after.file_id  # same file, changed content


# --- unsafe category folders (R2 defect: ``root / folder`` was not validated) -------

ESCAPING_FOLDERS = [
    "../outside",
    "../../outside",
    "/outside",
    "\\outside",
    "C:\\outside",
    "C:relative",
    "D:relative",
    "nested/link/PDF",
    "nested\\PDF",
]
UNSAFE_FOLDERS = [*ESCAPING_FOLDERS, "CON", "NUL.txt", "PDF.", "PDF ", "PDF:stream", "..", ""]


def _table_with_folder(folder: str) -> ExtensionTable:
    """A table whose PDF category was altered after both were validated."""
    category = Category("PDF", "PDF", "", (".pdf",))
    table = ExtensionTable((category, Category("Audio", "Audio", "", (".mp3",))))
    object.__setattr__(category, "folder", folder)
    return table


def _forbid_filesystem_access(monkeypatch) -> None:
    def accessed(*args: object, **kwargs: object) -> None:
        raise AssertionError("the filesystem was accessed")

    for name in ("normalize_dir_path", "check_directory_chain", "_scan", "lstat"):
        monkeypatch.setattr(planner, name, accessed)


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("folder", UNSAFE_FOLDERS)
def test_unsafe_category_folder_is_an_api_error_before_any_access(
    tmp_path: Path, src: Path, monkeypatch, snapshot, no_side_effects, folder: str, explicit: bool
):
    touch(src, "a.pdf", "b.mp3")
    table = _table_with_folder(folder)
    _forbid_filesystem_access(monkeypatch)
    before = snapshot(tmp_path)
    with no_side_effects(), pytest.raises(UnsafeFolderNameError) as info:
        build_plan(src, tmp_path / "dest" if explicit else None, table, {".pdf", ".mp3"})
    assert info.value.folder == folder
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("folder", ESCAPING_FOLDERS)
def test_target_never_leaves_destination_root_even_without_name_check(
    tmp_path: Path, src: Path, monkeypatch, snapshot, no_side_effects, folder: str
):
    # Second layer: with the name check disabled, containment still refuses the folder.
    touch(src, "a.pdf")
    table = _table_with_folder(folder)
    monkeypatch.setattr(planner, "check_folder_name", lambda name: name)
    before = snapshot(tmp_path)
    with no_side_effects(), pytest.raises(UnsafeFolderNameError, match="directly below"):
        build_plan(src, None, table, {".pdf"})
    assert snapshot(tmp_path) == before


def test_unsafe_folder_of_unselected_category_does_not_matter(src: Path, plan_readonly):
    touch(src, "a.pdf", "b.mp3")
    plan = plan_readonly(src, None, _table_with_folder("../outside"), {".mp3"})
    assert summary(plan) == [("b.mp3", "Audio/b.mp3", MOVE, None)]


# --- directory identity (R2 defect: unknown identity was taken for "different") -----

_STAT_SEQUENCE = ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size")


def _altered(st: os.stat_result, **changes: object) -> os.stat_result:
    """Copy of a real stat result with some fields replaced (``None``: unknown)."""
    sequence = list(tuple(st)[:10])
    for index, name in enumerate(_STAT_SEQUENCE):
        if name in changes:
            sequence[index] = changes.pop(name)
    extra = {
        "st_file_attributes": getattr(st, "st_file_attributes", None),
        "st_reparse_tag": getattr(st, "st_reparse_tag", None),
        "st_mtime_ns": st.st_mtime_ns,
        **changes,
    }
    return os.stat_result(sequence, extra)


def _alter_lstat(
    monkeypatch, match: Callable[[Path], bool], *, chain: bool = True, **changes: object
) -> None:
    """Report altered metadata for matching paths: in the planner and, with ``chain``,
    in directory chain checks."""
    real_lstat = pathsafety.lstat

    def altered(path: os.PathLike[str]) -> os.stat_result:
        st = real_lstat(path)
        return _altered(st, **changes) if match(Path(path)) else st

    if chain:
        monkeypatch.setattr(pathsafety, "lstat", altered)
    monkeypatch.setattr(planner, "lstat", altered)


NO_IDS = [
    pytest.param({"st_ino": 0, "st_dev": 0}, id="zero-ids"),
    pytest.param({"st_ino": None, "st_dev": None}, id="missing-ids"),
    pytest.param({"st_ino": 0}, id="zero-file-id"),
]


@pytest.mark.parametrize("ids", NO_IDS)
def test_default_mode_without_ids_works_while_del_is_missing(
    src: Path, monkeypatch, plan_readonly, ids: dict[str, object]
):
    touch(src, "a.pdf")
    _alter_lstat(monkeypatch, lambda p: True, **ids)
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    assert plan.ok
    assert summary(plan) == [("a.pdf", "PDF/a.pdf", MOVE, None)]
    assert plan.directories_to_create == (src / "DEL", src / "DEL" / "PDF")


@pytest.mark.parametrize("ids", NO_IDS)
def test_existing_destination_without_ids_is_refused(
    tmp_path: Path, src: Path, monkeypatch, plan_readonly, ids: dict[str, object]
):
    (src / "DEL").mkdir()
    (tmp_path / "dest").mkdir()
    touch(src, "a.pdf")
    _alter_lstat(monkeypatch, lambda p: True, **ids)
    for destination in (None, tmp_path / "dest"):
        plan = plan_readonly(src, destination, TABLE, {".pdf"})
        expected_root = src / "DEL" if destination is None else destination
        assert _errors(plan) == [(PlanErrorReason.DIRECTORY_IDENTITY_UNKNOWN, expected_root)]
        assert plan.moves == ()


def test_equal_paths_are_the_same_directory_without_ids(src: Path, monkeypatch, plan_readonly):
    touch(src, "a.pdf")
    _alter_lstat(monkeypatch, lambda p: True, st_ino=0, st_dev=0)
    plan = plan_readonly(src, src / "sub" / "..", TABLE, {".pdf"})
    assert _errors(plan) == [(PlanErrorReason.SAME_DIRECTORY, src)]


def test_existing_destination_with_reliable_ids_is_different(
    tmp_path: Path, src: Path, plan_readonly
):
    (src / "DEL").mkdir()
    (tmp_path / "dest").mkdir()
    touch(src, "a.pdf")
    for destination in (None, tmp_path / "dest"):
        plan = plan_readonly(src, destination, TABLE, {".pdf"})
        assert plan.ok
        assert [i.action for i in plan.items] == [MOVE]


@pytest.mark.parametrize("ids", NO_IDS)
def test_category_folder_without_ids_is_skipped(
    tmp_path: Path, src: Path, monkeypatch, plan_readonly, ids: dict[str, object]
):
    dest = tmp_path / "dest"
    (dest / "PDF").mkdir(parents=True)
    (dest / "Audio").mkdir()
    touch(src, "a.pdf", "b.mp3")
    _alter_lstat(monkeypatch, lambda p: p == dest / "PDF", chain=False, **ids)
    plan = plan_readonly(src, dest, TABLE, {".pdf", ".mp3"})
    assert summary(plan) == [
        ("a.pdf", "PDF/a.pdf", SKIP, SkipReason.TARGET_DIR_IDENTITY_UNKNOWN),
        ("b.mp3", "Audio/b.mp3", MOVE, None),
    ]
    assert plan.directories_to_create == ()


def _short_path(path: Path) -> Path | None:
    """The 8.3 short form of existing ``path`` if the volume has one, else ``None``."""
    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
        return None
    short = Path(buffer.value)
    return short if path_key(short) != path_key(path) else None


@pytest.mark.windows_only
def test_short_name_alias_of_source_is_detected_by_identity(
    tmp_path: Path, monkeypatch, plan_readonly
):
    source = tmp_path / "LongDestinationName" / "PDF"
    touch(source, "a.pdf", "b.mp3")
    alias = _short_path(source.parent)
    if alias is None:
        pytest.skip("no 8.3 short names on this volume")
    # Destination alias of the source itself, and of its parent (category folder = source).
    assert _errors(plan_readonly(source, alias / "PDF", TABLE, {".pdf"})) == [
        (PlanErrorReason.SAME_DIRECTORY, alias / "PDF")
    ]
    plan = plan_readonly(source, alias, TABLE, {".pdf", ".mp3"})
    assert summary(plan) == [
        ("a.pdf", "PDF/a.pdf", SKIP, SkipReason.TARGET_DIR_IS_SOURCE),
        ("b.mp3", "Audio/b.mp3", MOVE, None),
    ]
    # Without ids the same aliases cannot be told apart and are refused, not moved into.
    _alter_lstat(monkeypatch, lambda p: True, st_ino=0, st_dev=0)
    assert _errors(plan_readonly(source, alias / "PDF", TABLE, {".pdf"})) == [
        (PlanErrorReason.DIRECTORY_IDENTITY_UNKNOWN, alias / "PDF")
    ]
    assert _errors(plan_readonly(source, alias, TABLE, {".pdf"})) == [
        (PlanErrorReason.DIRECTORY_IDENTITY_UNKNOWN, alias)
    ]


# --- unknown metadata (R2 defect: missing values were read as 0, i.e. "safe") -------

REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT


def test_listing_with_unknown_metadata_is_skipped_without_opening(
    src: Path, monkeypatch, plan_readonly
):
    touch(src, "a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf")
    calls = _with_listing_attributes(
        monkeypatch,
        src,
        {
            "a.pdf": (None, 0),
            "b.pdf": (REPARSE, None),
            "c.pdf": (REPARSE, 0),
            "d.pdf": ("32", 0),
        },
    )
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    assert [(i.source.name, i.action, i.reason) for i in plan.items] == [
        ("a.pdf", SKIP, SkipReason.METADATA_UNKNOWN),
        ("b.pdf", SKIP, SkipReason.METADATA_UNKNOWN),
        ("c.pdf", SKIP, SkipReason.METADATA_UNKNOWN),
        ("d.pdf", SKIP, SkipReason.METADATA_UNKNOWN),
        ("e.pdf", MOVE, None),
    ]
    assert all(i.detail for i in plan.skipped)
    assert not {"a.pdf", "b.pdf", "c.pdf", "d.pdf"} & set(calls)
    assert plan.items[0].file.attributes is None
    assert plan.directories_to_create == (src / "DEL", src / "DEL" / "PDF")


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"st_file_attributes": None}, "file attributes unavailable or invalid"),
        ({"st_file_attributes": REPARSE, "st_reparse_tag": None}, "without a reparse tag"),
        ({"st_size": None}, "file size unavailable or invalid"),
        ({"st_mtime_ns": None}, "modification time unavailable or invalid"),
        ({"st_mode": None}, "file mode unavailable or invalid"),
    ],
)
def test_fresh_stat_with_unknown_metadata_is_skipped(
    src: Path, monkeypatch, plan_readonly, changes: dict[str, object], detail: str
):
    touch(src, "a.pdf", "b.pdf")
    _alter_lstat(monkeypatch, lambda p: p == src / "a.pdf", chain=False, **changes)
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    assert [(i.source.name, i.action, i.reason) for i in plan.items] == [
        ("a.pdf", SKIP, SkipReason.METADATA_UNKNOWN),
        ("b.pdf", MOVE, None),
    ]
    assert detail in (plan.items[0].detail or "")


def test_category_folder_with_unknown_metadata_is_skipped(
    tmp_path: Path, src: Path, monkeypatch, plan_readonly
):
    dest = tmp_path / "dest"
    (dest / "PDF").mkdir(parents=True)
    touch(src, "a.pdf", "b.mp3")
    _alter_lstat(monkeypatch, lambda p: p == dest / "PDF", chain=False, st_file_attributes=None)
    plan = plan_readonly(src, dest, TABLE, {".pdf", ".mp3"})
    assert summary(plan) == [
        ("a.pdf", "PDF/a.pdf", SKIP, SkipReason.TARGET_CHECK_FAILED),
        ("b.mp3", "Audio/b.mp3", MOVE, None),
    ]
    assert "attributes" in (plan.items[0].detail or "")
    assert plan.directories_to_create == (dest / "Audio",)


def test_source_with_unknown_metadata_is_a_plan_error(src: Path, monkeypatch, plan_readonly):
    touch(src, "a.pdf")
    _alter_lstat(monkeypatch, lambda p: p == src, st_file_attributes=None)
    plan = plan_readonly(src, None, TABLE, {".pdf"})
    assert _errors(plan) == [(PlanErrorReason.SOURCE_METADATA_UNKNOWN, src)]


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"st_file_attributes": None}, id="attributes-missing"),
        pytest.param({"st_file_attributes": REPARSE | 0x10, "st_reparse_tag": None}, id="tag"),
        pytest.param({"st_mode": "16895"}, id="mode-str"),
    ],
)
def test_destination_chain_with_unknown_metadata_is_a_plan_error(
    tmp_path: Path, src: Path, monkeypatch, plan_readonly, changes: dict[str, object]
):
    dest = tmp_path / "dest"
    (dest / "inner").mkdir(parents=True)
    (src / "DEL").mkdir()
    touch(src, "a.pdf")
    _alter_lstat(monkeypatch, lambda p: p in (dest, src / "DEL"), **changes)
    explicit = plan_readonly(src, dest / "inner" / "new", TABLE, {".pdf"})
    assert _errors(explicit) == [(PlanErrorReason.DESTINATION_METADATA_UNKNOWN, dest)]
    default = plan_readonly(src, None, TABLE, {".pdf"})
    assert _errors(default) == [(PlanErrorReason.DESTINATION_METADATA_UNKNOWN, src / "DEL")]


# --- inventory: every file of the listing, in the plan or outside it (R4-D) -----------


def inventory_of(src: Path, *args: object, **kwargs: object) -> Inventory | None:
    return plan_with_inventory(src, *args, **kwargs)[1]


def test_inventory_counts_every_top_level_file_in_or_outside_the_plan(src: Path, snapshot):
    touch(src, "a.pdf", "B.PDF", "s1.ps1", "s2.PS1", "s3.ps1", "README", "notes.txt", "x.gs")
    touch(src / "subfolder", "inner.pdf", "inner.ps1")
    before = snapshot(src)
    plan, inventory = plan_with_inventory(src, None, TABLE, {".pdf"})
    assert snapshot(src) == before
    assert plan == build_plan(src, None, TABLE, {".pdf"})
    assert [i.source.name for i in plan.items] == ["a.pdf", "B.PDF"]
    assert inventory == Inventory(
        files=8,
        links=0,
        folders=1,
        in_plan=2,
        outside=(
            OutsideGroup(".ps1", 3),  # extensions compared case-insensitively
            OutsideGroup(".gs", 1),
            OutsideGroup(".txt", 1, known=True),  # in the table, not chosen
            OutsideGroup(None, 1),  # no extension: after the named ones
        ),
    )
    assert inventory.files == inventory.in_plan + inventory.outside_count


def test_inventory_does_not_open_subfolders_or_follow_junctions(
    tmp_path: Path, src: Path, make_junction, plan_readonly
):
    outside = tmp_path / "outside"
    touch(outside, "secret.pdf", "other.ps1")
    make_junction(src / "link.pdf", outside)
    make_junction(src / "linkdir", outside)
    touch(src / "nested" / "deeper", "deep.pdf")
    touch(src, "a.pdf")
    plan_readonly(src, None, TABLE, {".pdf"})  # nothing changed, nothing opened
    assert inventory_of(src, None, TABLE, {".pdf"}) == Inventory(
        files=1, links=0, folders=3, in_plan=1
    )


def test_inventory_counts_the_existing_default_destination_as_a_folder(src: Path):
    touch(src / "DEL" / "Documents", "old.pdf")
    touch(src, "a.pdf", "b.jar")
    inventory = inventory_of(src, None, TABLE, {".pdf"})
    assert inventory == Inventory(2, 0, 1, 1, (OutsideGroup(".jar", 1),))


def test_skipped_items_are_in_the_plan_not_outside_it(src: Path, monkeypatch):
    touch(src, "a.pdf", "b.pdf", "c.zip")
    _fail_lstat_for(monkeypatch, src / "b.pdf", PermissionError(13, "denied"))
    plan, inventory = plan_with_inventory(src, None, TABLE, {".pdf"})
    assert [(i.source.name, i.action) for i in plan.items] == [("a.pdf", MOVE), ("b.pdf", SKIP)]
    assert inventory == Inventory(3, 0, 0, 2, (OutsideGroup(".zip", 1, known=True),))


def test_inventory_of_an_empty_folder_is_zero(src: Path):
    assert inventory_of(src, None, TABLE, {".pdf"}) == Inventory(0, 0, 0, 0)


@pytest.mark.parametrize("case", ["missing", "nothing-selected", "same-directory"])
def test_no_inventory_when_the_folder_was_not_listed(tmp_path: Path, src: Path, case: str):
    touch(src, "a.pdf")
    if case == "missing":
        plan, inventory = plan_with_inventory(tmp_path / "absent", None, TABLE, {".pdf"})
    elif case == "nothing-selected":
        plan, inventory = plan_with_inventory(src, None, TABLE, set())
    else:
        plan, inventory = plan_with_inventory(src, src, TABLE, {".pdf"})
    assert plan.errors and inventory is None


def test_no_inventory_when_the_listing_fails(src: Path, monkeypatch):
    touch(src, "a.pdf")

    def denied(path: object) -> object:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(os, "scandir", denied)
    plan, inventory = plan_with_inventory(src, None, TABLE, {".pdf"})
    assert [e.reason for e in plan.errors] == [PlanErrorReason.SOURCE_ACCESS_DENIED]
    assert inventory is None


def test_file_links_are_counted_as_files_and_not_followed(tmp_path: Path, src: Path):
    touch(tmp_path, "real.pdf")
    try:
        (src / "link.pdf").symlink_to(tmp_path / "real.pdf")
    except OSError as exc:  # needs Developer Mode or a privilege on Windows
        pytest.skip(f"cannot create symlinks: {exc}")
    assert inventory_of(src, None, TABLE, {".pdf"}) == Inventory(1, 1, 0, 1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"files": 2, "links": 0, "folders": 0, "in_plan": 1},  # one file unaccounted for
        {"files": 1, "links": 2, "folders": 0, "in_plan": 1},  # more links than files
        {"files": -1, "links": 0, "folders": 0, "in_plan": 0},
        {"files": True, "links": 0, "folders": 0, "in_plan": 1},
        {
            "files": 2,
            "links": 0,
            "folders": 0,
            "in_plan": 0,
            "outside": (OutsideGroup(".a", 1), OutsideGroup(".a", 1)),
        },
    ],
)
def test_inventory_rejects_numbers_that_do_not_add_up(kwargs: dict[str, object]):
    with pytest.raises(ValueError):
        Inventory(**kwargs)  # type: ignore[arg-type]


def test_outside_group_rejects_bad_values():
    for bad in ({"extension": ".a", "count": 0}, {"extension": None, "count": 1, "known": True}):
        with pytest.raises(ValueError):
            OutsideGroup(**bad)  # type: ignore[arg-type]
