from __future__ import annotations

import json

import pytest

from downloads_organizer.catalog import DEFAULT_CATEGORIES, Category
from downloads_organizer.extensions import (
    MAX_EXTENSION_LENGTH,
    REASON_MESSAGES,
    ExtensionError,
    ExtensionErrorReason,
    ExtensionTable,
    RejectedEntry,
    extension_of,
    normalize_extension,
)
from downloads_organizer.pathsafety import UnsafeFolderNameError

R = ExtensionErrorReason


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("pdf", ".pdf"),
        (".PDF", ".pdf"),
        ("  .Heic ", ".heic"),
        ("7z", ".7z"),
        ("my_ext", ".my_ext"),
        ("a-b", ".a-b"),
        ("x" * (MAX_EXTENSION_LENGTH - 1), "." + "x" * (MAX_EXTENSION_LENGTH - 1)),
    ],
)
def test_normalize_valid(raw, expected):
    assert normalize_extension(raw) == expected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", R.EMPTY),
        ("   ", R.EMPTY),
        (".", R.EMPTY),
        (".tar.gz", R.COMPOUND),
        ("tar.gz", R.COMPOUND),
        ("..pdf", R.INVALID_CHARACTERS),
        ("pdf.", R.INVALID_CHARACTERS),
        ("p df", R.INVALID_CHARACTERS),
        ("a/b", R.INVALID_CHARACTERS),
        ("a\\b", R.INVALID_CHARACTERS),
        ("..\\..\\x", R.INVALID_CHARACTERS),
        ("c:", R.INVALID_CHARACTERS),
        ("*", R.INVALID_CHARACTERS),
        ("?x", R.INVALID_CHARACTERS),
        ("<x>", R.INVALID_CHARACTERS),
        ("a|b", R.INVALID_CHARACTERS),
        ('a"b', R.INVALID_CHARACTERS),
        ("a\tb", R.INVALID_CHARACTERS),
        ("a\x00b", R.INVALID_CHARACTERS),
        (chr(0x200B) + "pdf", R.INVALID_CHARACTERS),
        ("пдф", R.INVALID_CHARACTERS),
        ("".join(map(chr, (0xFF50, 0xFF44, 0xFF46))), R.INVALID_CHARACTERS),
        ("x" * MAX_EXTENSION_LENGTH, R.TOO_LONG),
        (".part", R.PARTIAL_DOWNLOAD),
        ("CRDOWNLOAD", R.PARTIAL_DOWNLOAD),
        (".!ut", R.PARTIAL_DOWNLOAD),
    ],
)
def test_normalize_rejects(raw, reason):
    with pytest.raises(ExtensionError) as info:
        normalize_extension(raw)
    assert info.value.reason is reason
    assert info.value.value == raw


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Report.PDF", ".pdf"),
        ("archive.tar.gz", ".gz"),
        ("Photo.JpG", ".jpg"),
        ("..pdf", ".pdf"),
        (".bashrc", None),
        ("noext", None),
        ("file.", None),
        ("", None),
    ],
)
def test_extension_of(name, expected):
    assert extension_of(name) == expected


def test_every_reason_has_a_message():
    assert set(REASON_MESSAGES) == set(ExtensionErrorReason)
    assert REASON_MESSAGES[R.EMPTY] in str(ExtensionError(R.EMPTY, ""))


def test_default_table_classifies_case_insensitively():
    table = ExtensionTable()
    assert table.category_for(".JPG").key == "Images"
    assert table.category_for(".unknown") is None
    assert table.category_for_filename("Report.PDF").key == "PDF"
    assert table.category_for_filename("video.mp4.part") is None
    assert table.category_for_filename("README") is None
    assert len(table.all_extensions()) == 40
    assert table.custom == {}


def test_with_custom_adds_normalized_and_keeps_original_unchanged():
    table = ExtensionTable()
    updated = table.with_custom(" .HEIC ", "Images")
    assert updated.custom == {".heic": "Images"}
    assert updated.category_for(".heic").key == "Images"
    assert updated.is_custom(".heic")
    assert not updated.is_builtin(".heic")
    assert updated.extensions("Images")[-1] == ".heic"
    assert table.custom == {}
    assert table.category_for(".heic") is None


def test_extensions_order_is_common_rare_custom():
    table = ExtensionTable().with_custom("webm", "Video").with_custom("m4v", "Video")
    assert table.extensions("Video") == (".mp4", ".avi", ".mkv", ".mov", ".wmv", ".m4v", ".webm")
    assert table.custom_extensions("Video") == (".m4v", ".webm")


def test_custom_view_is_read_only():
    table = ExtensionTable().with_custom("heic", "Images")
    with pytest.raises(TypeError):
        table.custom[".x"] = "Images"  # type: ignore[index]


@pytest.mark.parametrize(
    ("raw", "key", "reason"),
    [
        (".JPG", "Code", R.BUILTIN_DUPLICATE),
        ("pdf", "PDF", R.BUILTIN_DUPLICATE),
        ("heic", "Nope", R.UNKNOWN_CATEGORY),
        ("tar.gz", "Archive", R.COMPOUND),
        ("", "Images", R.EMPTY),
        ("part", "Archive", R.PARTIAL_DOWNLOAD),
    ],
)
def test_with_custom_rejects(raw, key, reason):
    with pytest.raises(ExtensionError) as info:
        ExtensionTable().with_custom(raw, key)
    assert info.value.reason is reason


@pytest.mark.parametrize(
    ("raw", "key"), [("heic", "Images"), (".HEIC", "Images"), ("Heic", "Code")]
)
def test_with_custom_rejects_duplicate_custom_in_any_case_or_category(raw, key):
    table = ExtensionTable().with_custom("heic", "Images")
    with pytest.raises(ExtensionError) as info:
        table.with_custom(raw, key)
    assert info.value.reason is R.CUSTOM_DUPLICATE


def test_without_custom_removes_case_insensitively():
    table = ExtensionTable().with_custom("heic", "Images").with_custom("webm", "Video")
    updated = table.without_custom(".HEIC")
    assert updated.custom == {".webm": "Video"}
    assert updated.category_for(".heic") is None
    assert table.custom == {".heic": "Images", ".webm": "Video"}


@pytest.mark.parametrize("raw", [".jpg", "heic"])
def test_without_custom_refuses_builtin_or_absent(raw):
    with pytest.raises(ExtensionError) as info:
        ExtensionTable().without_custom(raw)
    assert info.value.reason is R.NOT_CUSTOM


def test_removed_custom_can_be_added_again_to_another_category():
    table = ExtensionTable().with_custom("heic", "Images").without_custom("heic")
    assert table.with_custom("heic", "Code").category_for(".heic").key == "Code"


def test_constructor_validates_custom_mapping():
    with pytest.raises(ExtensionError):
        ExtensionTable(custom={".jpg": "Code"})
    with pytest.raises(ExtensionError):
        ExtensionTable(custom={".heic": "Nope"})


def test_constructor_rejects_inconsistent_builtin_categories():
    dup = (Category("A", "A", "", (".x",)), Category("B", "B", "", (".x",)))
    with pytest.raises(ValueError, match="duplicate"):
        ExtensionTable(dup)
    unnormalized = (Category("A", "A", "", (".X",)),)
    with pytest.raises(ValueError, match="invalid"):
        ExtensionTable(unnormalized)
    same_key = (Category("A", "A", "", (".x",)), Category("A", "B", "", (".y",)))
    with pytest.raises(ValueError, match="category key"):
        ExtensionTable(same_key)


@pytest.mark.parametrize("folder", ["../outside", "C:\\outside", "nested/PDF", "NUL.txt"])
def test_constructor_rejects_category_altered_to_unsafe_folder(folder: str):
    altered = Category("PDF", "PDF", "", (".pdf",))
    object.__setattr__(altered, "folder", folder)  # bypasses Category's own check
    with pytest.raises(UnsafeFolderNameError):
        ExtensionTable((altered, *DEFAULT_CATEGORIES[2:]))


def test_custom_extensions_unknown_category_raises():
    with pytest.raises(KeyError):
        ExtensionTable().custom_extensions("Nope")


def test_from_stored_loads_valid_entries():
    table, rejected = ExtensionTable.from_stored([(".HEIC", "Images"), ("webm", "Video")])
    assert table.custom == {".heic": "Images", ".webm": "Video"}
    assert rejected == []


def test_from_stored_skips_invalid_entries_without_raising():
    entries: list[tuple[object, object]] = [
        (".heic", "Images"),
        (None, "Images"),
        (".x", 5),
        (["a"], "Code"),
        ("tar.gz", "Archive"),
        (".jpg", "Code"),
        (".abc", "Nope"),
        (".HEIC", "Images"),
    ]
    table, rejected = ExtensionTable.from_stored(entries)
    assert table.custom == {".heic": "Images"}
    assert [r.reason for r in rejected] == [
        R.MALFORMED,
        R.MALFORMED,
        R.MALFORMED,
        R.COMPOUND,
        R.BUILTIN_DUPLICATE,
        R.UNKNOWN_CATEGORY,
        R.CUSTOM_DUPLICATE,
    ]


@pytest.mark.parametrize(
    "item",
    [
        None,
        [".heic"],
        [".heic", "Images", "extra"],
        42,
        {"extension": ".heic"},
        "ab",  # a two-character string would unpack into two strings
        (),
    ],
)
def test_from_stored_rejects_malformed_item_without_raising(item):
    table, rejected = ExtensionTable.from_stored([item])
    assert table.custom == {}
    assert rejected == [RejectedEntry(item, None, R.MALFORMED)]


@pytest.mark.parametrize(
    "entries",
    [
        None,
        "invalid",
        {"heic": "Images"},
        42,
        {(".heic", "Images")},
        ((".heic", "Images") for _ in range(1)),
    ],
)
def test_from_stored_rejects_malformed_top_level_without_raising(entries):
    table, rejected = ExtensionTable.from_stored(entries)
    assert table.custom == {}
    assert len(table.all_extensions()) == 40
    assert table.category_for(".jpg").key == "Images"
    assert rejected == [RejectedEntry(entries, None, R.MALFORMED)]


def test_from_stored_accepts_json_array_pairs():
    entries = json.loads('[[".heic", "Images"], ["WEBM", "Video"]]')
    table, rejected = ExtensionTable.from_stored(entries)
    assert table.custom == {".heic": "Images", ".webm": "Video"}
    assert table.category_for_filename("photo.HEIC").key == "Images"
    assert rejected == []


def test_from_stored_keeps_valid_entries_between_damaged_ones():
    entries = [
        None,
        [".heic", "Images"],
        42,
        ("webm", "Video"),
        [".x"],
        {"a": 1},
        [".avi", "Video", "extra"],
        ["m4v", "Video"],
    ]
    table, rejected = ExtensionTable.from_stored(entries)
    assert table.custom == {".heic": "Images", ".webm": "Video", ".m4v": "Video"}
    assert rejected == [
        RejectedEntry(None, None, R.MALFORMED),
        RejectedEntry(42, None, R.MALFORMED),
        RejectedEntry([".x"], None, R.MALFORMED),
        RejectedEntry({"a": 1}, None, R.MALFORMED),
        RejectedEntry([".avi", "Video", "extra"], None, R.MALFORMED),
    ]


def test_from_stored_repeats_and_conflicts_among_damaged_entries():
    entries = [
        [".foo", "Images"],
        None,
        ["FOO", "Images"],
        [".foo", "Code"],
        [".bar", "Code"],
        (".BAR", "Code"),
        [".baz", "Audio"],
    ]
    table, rejected = ExtensionTable.from_stored(entries)
    assert table.custom == {".bar": "Code", ".baz": "Audio"}
    assert table.category_for(".foo") is None
    assert rejected == [
        RejectedEntry(None, None, R.MALFORMED),
        RejectedEntry("FOO", "Images", R.CUSTOM_DUPLICATE),
        RejectedEntry(".foo", "Images", R.AMBIGUOUS),
        RejectedEntry(".foo", "Code", R.AMBIGUOUS),
        RejectedEntry(".BAR", "Code", R.CUSTOM_DUPLICATE),
    ]


def test_from_stored_rejects_ambiguous_mapping_entirely():
    entries = [(".foo", "Images"), ("bar", "Code"), (".FOO", "Code"), ("foo", "Images")]
    table, rejected = ExtensionTable.from_stored(entries)
    assert table.custom == {".bar": "Code"}
    assert table.category_for(".foo") is None
    assert rejected == [
        RejectedEntry(".foo", "Images", R.AMBIGUOUS),
        RejectedEntry(".FOO", "Code", R.AMBIGUOUS),
        RejectedEntry("foo", "Images", R.AMBIGUOUS),
    ]
