from __future__ import annotations

import pytest

from downloads_organizer.catalog import (
    DEFAULT_CATEGORIES,
    PARTIAL_DOWNLOAD_EXTENSIONS,
    Category,
    category_by_key,
)
from downloads_organizer.extensions import normalize_extension
from downloads_organizer.pathsafety import UnsafeFolderNameError, folder_name_problem

# Characterization of GROUPS in the legacy file_organizer.py (baseline commit b20da74).
LEGACY_GROUPS: dict[str, tuple[str, list[str]]] = {
    "Torrent": ("Torrent", [".torrent"]),
    "PDF": ("PDF", [".pdf"]),
    "Images": ("Images", [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"]),
    "Office": ("Office", [".docx", ".xlsx", ".pptx", ".doc", ".xls", ".csv"]),
    "EXE": ("EXE", [".exe", ".msi"]),
    "Video": ("Video", [".mp4", ".avi", ".mkv", ".mov", ".wmv"]),
    "TXT": ("TXT", [".txt", ".log", ".md"]),
    "Archive": ("Archive", [".zip", ".rar", ".7z", ".tar", ".gz"]),
    "Audio": ("Audio", [".mp3", ".wav", ".flac", ".aac", ".ogg"]),
    "Code": ("Code", [".py", ".js", ".ts", ".html", ".css", ".json"]),
}

# Approved display order (coordinator R1-FIX-01): key -> (common, rare), compared as-is.
APPROVED_DISPLAY: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Torrent": ((".torrent",), ()),
    "PDF": ((".pdf",), ()),
    "Images": ((".jpg", ".jpeg", ".png", ".gif", ".webp"), (".bmp",)),
    "Office": ((".docx", ".xlsx", ".pptx", ".csv"), (".doc", ".xls")),
    "EXE": ((".exe", ".msi"), ()),
    "Video": ((".mp4", ".avi", ".mkv", ".mov"), (".wmv",)),
    "TXT": ((".txt", ".md"), (".log",)),
    "Archive": ((".zip", ".rar", ".7z"), (".tar", ".gz")),
    "Audio": ((".mp3", ".wav", ".flac"), (".aac", ".ogg")),
    "Code": ((".py", ".js", ".ts", ".html", ".css", ".json"), ()),
}

WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{base}{n}" for base in ("COM", "LPT") for n in range(1, 10)
}


def test_categories_match_legacy_order_keys_and_folders():
    assert [c.key for c in DEFAULT_CATEGORIES] == list(LEGACY_GROUPS)
    for category in DEFAULT_CATEGORIES:
        assert category.folder == LEGACY_GROUPS[category.key][0]


def test_extension_membership_per_category_matches_legacy():
    # Order is intentionally not compared here: the display order is approved separately.
    for category in DEFAULT_CATEGORIES:
        assert len(category.extensions) == len(LEGACY_GROUPS[category.key][1]), category.key
        assert set(category.extensions) == set(LEGACY_GROUPS[category.key][1]), category.key
    assert sum(len(c.extensions) for c in DEFAULT_CATEGORIES) == 40


def test_common_and_rare_follow_approved_display_order():
    assert [c.key for c in DEFAULT_CATEGORIES] == list(APPROVED_DISPLAY)
    for category in DEFAULT_CATEGORIES:
        common, rare = APPROVED_DISPLAY[category.key]
        assert category.common == common, category.key
        assert category.rare == rare, category.key
        assert category.extensions == common + rare, category.key


def test_every_extension_belongs_to_exactly_one_category():
    all_exts = [ext for c in DEFAULT_CATEGORIES for ext in c.extensions]
    assert len(all_exts) == len(set(all_exts))


def test_common_and_rare_are_disjoint_and_common_is_never_empty():
    for category in DEFAULT_CATEGORIES:
        assert category.common, category.key
        assert not set(category.common) & set(category.rare), category.key
        assert category.extensions == category.common + category.rare


def test_builtin_extensions_are_normalized():
    for category in DEFAULT_CATEGORIES:
        for ext in category.extensions:
            assert normalize_extension(ext) == ext


def test_ts_stays_in_code():
    assert ".ts" in category_by_key("Code").extensions


def test_partial_download_extensions_never_overlap_builtins():
    all_exts = {ext for c in DEFAULT_CATEGORIES for ext in c.extensions}
    assert not all_exts & PARTIAL_DOWNLOAD_EXTENSIONS
    assert {".part", ".crdownload"} <= PARTIAL_DOWNLOAD_EXTENSIONS
    assert all(ext == ext.lower() and ext.startswith(".") for ext in PARTIAL_DOWNLOAD_EXTENSIONS)


def test_folder_names_are_valid_windows_names():
    for category in DEFAULT_CATEGORIES:
        name = category.folder
        assert name
        assert not set(name) & set('<>:"/\\|?*')
        assert not name.endswith((".", " "))
        assert name.upper() not in WINDOWS_RESERVED_NAMES
        assert folder_name_problem(name) is None
    folders = [c.folder.casefold() for c in DEFAULT_CATEGORIES]
    assert len(folders) == len(set(folders))


@pytest.mark.parametrize(
    "folder",
    ["../outside", "/outside", "C:\\outside", "C:relative", "nested/link/PDF", "CON", "PDF.", ""],
)
def test_category_with_unsafe_folder_cannot_be_created(folder: str):
    with pytest.raises(UnsafeFolderNameError):
        Category("PDF", folder, "", (".pdf",))


def test_category_by_key_unknown_raises():
    with pytest.raises(KeyError):
        category_by_key("Nope")
