from __future__ import annotations

import os
import stat
import string
from pathlib import Path

import pytest

from downloads_organizer import pathsafety
from downloads_organizer.catalog import DEFAULT_CATEGORIES
from downloads_organizer.models import SkipReason
from downloads_organizer.pathsafety import (
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    DirectoryRelation,
    PathProblem,
    PathSafetyError,
    UnsafeFolderNameError,
    check_directory_chain,
    check_folder_name,
    directory_problem,
    directory_relation,
    file_skip_reason,
    folder_name_problem,
    is_directory,
    metadata_problem,
    name_key,
    name_too_long,
    normalize_dir_path,
)

REG = stat.S_IFREG | 0o666
DIR = stat.S_IFDIR | 0o777
LNK = stat.S_IFLNK | 0o777
MISSING = object()  # leave the field out of the stat result
REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT


def fake_stat(
    mode: object = REG,
    attrs: object = 0,
    tag: object = 0,
    *,
    ino: int = 0,
    dev: int = 0,
    size: object = 7,
    mtime_ns: object = 0,
) -> os.stat_result:
    """A stat result as ``os.scandir`` would report it; no real file involved.

    Any field may be given a wrong value; ``MISSING`` omits ``st_file_attributes``,
    ``st_reparse_tag`` or ``st_mtime_ns`` (``os.stat_result`` then reports ``None``).
    """
    extra = {"st_file_attributes": attrs, "st_reparse_tag": tag, "st_mtime_ns": mtime_ns}
    return os.stat_result(
        (mode, ino, dev, 1, 0, 0, size, 0, 0, 0),
        {k: v for k, v in extra.items() if v is not MISSING},
    )


# --- classification of attributes (substituted data, not a real OneDrive check) -----


@pytest.mark.parametrize(
    ("st", "expected"),
    [
        (fake_stat(), None),
        (fake_stat(attrs=stat.FILE_ATTRIBUTE_ARCHIVE | stat.FILE_ATTRIBUTE_READONLY), None),
        (fake_stat(LNK), SkipReason.SYMLINK),
        (
            fake_stat(attrs=stat.FILE_ATTRIBUTE_REPARSE_POINT, tag=stat.IO_REPARSE_TAG_SYMLINK),
            SkipReason.SYMLINK,
        ),
        (
            fake_stat(attrs=stat.FILE_ATTRIBUTE_REPARSE_POINT, tag=0x9000001A),
            SkipReason.CLOUD_PLACEHOLDER,
        ),
        (
            fake_stat(attrs=stat.FILE_ATTRIBUTE_REPARSE_POINT, tag=0x9000101A),
            SkipReason.CLOUD_PLACEHOLDER,
        ),
        (
            fake_stat(attrs=stat.FILE_ATTRIBUTE_REPARSE_POINT, tag=0x9000F01A),
            SkipReason.CLOUD_PLACEHOLDER,
        ),
        # A disguised placeholder: no reparse attribute, only the recall attributes.
        (fake_stat(attrs=FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS), SkipReason.CLOUD_PLACEHOLDER),
        (fake_stat(attrs=FILE_ATTRIBUTE_RECALL_ON_OPEN), SkipReason.CLOUD_PLACEHOLDER),
        (fake_stat(attrs=stat.FILE_ATTRIBUTE_OFFLINE), SkipReason.OFFLINE),
        (
            fake_stat(attrs=stat.FILE_ATTRIBUTE_REPARSE_POINT, tag=stat.IO_REPARSE_TAG_APPEXECLINK),
            SkipReason.REPARSE_POINT,
        ),
        (
            fake_stat(attrs=stat.FILE_ATTRIBUTE_REPARSE_POINT, tag=stat.IO_REPARSE_TAG_MOUNT_POINT),
            SkipReason.REPARSE_POINT,
        ),
        (fake_stat(tag=0x80000013), SkipReason.REPARSE_POINT),  # dedup tag, attribute missing
        (fake_stat(attrs=stat.FILE_ATTRIBUTE_DEVICE), SkipReason.NOT_REGULAR_FILE),
        (fake_stat(stat.S_IFIFO | 0o666), SkipReason.NOT_REGULAR_FILE),
        (fake_stat(attrs=stat.FILE_ATTRIBUTE_SYSTEM), SkipReason.SYSTEM),
        (fake_stat(attrs=stat.FILE_ATTRIBUTE_HIDDEN), SkipReason.HIDDEN),
        (
            fake_stat(attrs=stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM),
            SkipReason.SYSTEM,
        ),
        (
            fake_stat(attrs=stat.FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS),
            SkipReason.CLOUD_PLACEHOLDER,
        ),
    ],
)
def test_file_skip_reason_classifies_attributes(st: os.stat_result, expected: SkipReason | None):
    assert file_skip_reason(st) is expected


def test_directory_detection_includes_directory_links():
    assert is_directory(fake_stat(DIR))
    assert is_directory(fake_stat(LNK, attrs=stat.FILE_ATTRIBUTE_DIRECTORY))  # dir symlink
    junction = fake_stat(
        DIR,
        attrs=stat.FILE_ATTRIBUTE_DIRECTORY | stat.FILE_ATTRIBUTE_REPARSE_POINT,
        tag=stat.IO_REPARSE_TAG_MOUNT_POINT,
    )
    assert is_directory(junction)
    assert not is_directory(fake_stat())


# --- unknown metadata (R2 defect: missing values were read as 0, i.e. "safe") -------

UNKNOWN_FILE_METADATA = [
    pytest.param(fake_stat(attrs=MISSING), id="attributes-missing"),
    pytest.param(fake_stat(attrs=None), id="attributes-none"),
    pytest.param(fake_stat(attrs="32"), id="attributes-str"),
    pytest.param(fake_stat(attrs=True), id="attributes-bool"),
    pytest.param(fake_stat(attrs=-1), id="attributes-negative"),
    pytest.param(fake_stat(attrs=2**32), id="attributes-too-large"),
    pytest.param(fake_stat(attrs=object()), id="attributes-object"),
    pytest.param(fake_stat(mode=None), id="mode-none"),
    pytest.param(fake_stat(mode="33206"), id="mode-str"),
    pytest.param(fake_stat(mode=-1), id="mode-negative"),
    pytest.param(fake_stat(attrs=REPARSE, tag=MISSING), id="reparse-attribute-tag-missing"),
    pytest.param(fake_stat(attrs=REPARSE, tag=None), id="reparse-attribute-tag-none"),
    pytest.param(fake_stat(attrs=REPARSE, tag=0), id="reparse-attribute-tag-zero"),
    pytest.param(fake_stat(tag="0x9000001a"), id="tag-str"),
    pytest.param(fake_stat(tag=-1), id="tag-negative"),
    pytest.param(fake_stat(tag=2**32), id="tag-too-large"),
    pytest.param(fake_stat(tag=False), id="tag-bool"),
    pytest.param(fake_stat(size=None), id="size-none"),
    pytest.param(fake_stat(size=-1), id="size-negative"),
    pytest.param(fake_stat(size=7.0), id="size-float"),
    pytest.param(fake_stat(mtime_ns=MISSING), id="mtime-missing"),
    pytest.param(fake_stat(mtime_ns="0"), id="mtime-str"),
]


@pytest.mark.parametrize("st", UNKNOWN_FILE_METADATA)
def test_file_with_unknown_metadata_is_refused(st: os.stat_result):
    assert file_skip_reason(st) is SkipReason.METADATA_UNKNOWN
    assert metadata_problem(st, file=True) is not None


def test_missing_tag_without_reparse_attribute_is_a_plain_file():
    # Windows reports the tag only for reparse points: zero or absent is fine without it.
    assert file_skip_reason(fake_stat(tag=MISSING)) is None
    assert file_skip_reason(fake_stat(tag=0)) is None
    assert metadata_problem(fake_stat(tag=MISSING)) is None


def test_links_and_cloud_files_are_named_even_with_incomplete_metadata():
    assert file_skip_reason(fake_stat(LNK, attrs=MISSING)) is SkipReason.SYMLINK
    recall = fake_stat(mode=None, attrs=FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    assert file_skip_reason(recall) is SkipReason.CLOUD_PLACEHOLDER
    cloud = fake_stat(attrs=REPARSE, tag=0x9000001A, size=None)
    assert file_skip_reason(cloud) is SkipReason.CLOUD_PLACEHOLDER


@pytest.mark.parametrize(
    "st",
    [
        pytest.param(fake_stat(DIR, attrs=MISSING), id="attributes-missing"),
        pytest.param(fake_stat(DIR, attrs="16"), id="attributes-str"),
        pytest.param(fake_stat(mode=None, attrs=stat.FILE_ATTRIBUTE_DIRECTORY), id="mode-none"),
        pytest.param(fake_stat(DIR, attrs=REPARSE, tag=MISSING), id="reparse-tag-missing"),
        pytest.param(fake_stat(DIR, attrs=REPARSE, tag=0), id="reparse-tag-zero"),
        pytest.param(fake_stat(DIR, tag=-5), id="tag-invalid"),
    ],
)
def test_directory_with_unknown_metadata_is_not_a_plain_directory(st: os.stat_result):
    assert directory_problem(st) is PathProblem.METADATA_UNKNOWN


def test_directory_problem_classifies_known_metadata():
    assert directory_problem(fake_stat(DIR, attrs=stat.FILE_ATTRIBUTE_DIRECTORY)) is None
    assert directory_problem(fake_stat(DIR, size=None)) is None  # size is irrelevant here
    junction = fake_stat(DIR, attrs=REPARSE, tag=stat.IO_REPARSE_TAG_MOUNT_POINT)
    assert directory_problem(junction) is PathProblem.REPARSE_POINT
    assert directory_problem(fake_stat(LNK)) is PathProblem.REPARSE_POINT
    assert directory_problem(fake_stat()) is PathProblem.NOT_DIRECTORY


def test_unknown_metadata_never_raises_type_error():
    for value in (None, "x", 1.5, object(), b"\0", [], True):
        st = fake_stat(mode=value, attrs=value, tag=value, size=value, mtime_ns=value)
        assert file_skip_reason(st) is SkipReason.METADATA_UNKNOWN
        assert directory_problem(st) is PathProblem.METADATA_UNKNOWN
        assert not is_directory(st)


# --- category folder names ----------------------------------------------------------

UNSAFE_FOLDERS = [
    "",
    ".",
    "..",
    "../outside",
    "../../outside",
    "/outside",
    "\\outside",
    "C:\\outside",
    "C:/outside",
    "C:relative",
    "C:",
    "\\\\server\\share",
    "//server/share",
    "\\\\?\\C:\\x",
    "nested/link/PDF",
    "nested\\PDF",
    "PDF:stream",
    "PDF::$DATA",
    "CON",
    "con",
    "NUL.txt",
    "COM1",
    "COM0",
    "LPT0.log",
    "COM\u00b9",
    "CONIN$",
    "AUX .txt",
    "PDF.",
    "PDF ",
    " . ",
    "...",
    "a\x00b",
    "a\x1fb",
    "a\tb",
    "a\u202eb",  # right-to-left override
    "a\ud800b",  # lone surrogate
    "a*b",
    "a?b",
    'a"b',
    "a<b",
    "a>b",
    "a|b",
    "x" * 256,
    "\U0001f600" * 128,
]


@pytest.mark.parametrize("folder", UNSAFE_FOLDERS)
def test_unsafe_folder_names_are_rejected(folder: str):
    assert folder_name_problem(folder) is not None
    with pytest.raises(UnsafeFolderNameError) as info:
        check_folder_name(folder)
    assert info.value.folder == folder
    assert info.value.problem


@pytest.mark.parametrize("folder", [None, 1, b"PDF", Path("PDF"), ["PDF"]])
def test_non_string_folder_names_are_rejected(folder: object):
    with pytest.raises(UnsafeFolderNameError):
        check_folder_name(folder)


@pytest.mark.parametrize(
    "folder",
    [c.folder for c in DEFAULT_CATEGORIES] + ["My Files", "Документы", "a.b", "CONSOLE", "x" * 255],
)
def test_safe_folder_names_are_accepted(folder: str):
    assert check_folder_name(folder) == folder


@pytest.mark.windows_only
def test_isreserved_is_one_of_the_checks(monkeypatch):
    # A name only Windows itself knows to be reserved is refused too.
    monkeypatch.setattr(pathsafety.os.path, "isreserved", lambda name: name == "Special")
    assert folder_name_problem("Special") == "reserved by Windows"
    assert folder_name_problem("Other") is None


# --- names ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Report.PDF", "report.pdf"),
        ("\u0131.txt", "I.TXT"),  # dotless i upper-cases to I, as on NTFS
        ("stra\u00dfe.txt", "STRASSE.txt"),  # conservative, documented
    ],
)
def test_name_key_treats_case_variants_as_equal(a: str, b: str):
    assert name_key(a) == name_key(b)


def test_name_key_keeps_distinct_names_and_does_not_normalize_unicode():
    assert name_key("a.pdf") != name_key("a_1.pdf")
    assert name_key("caf\u00e9.txt") != name_key("cafe\u0301.txt")  # NTFS does not normalize


def test_name_too_long_counts_utf16_units():
    assert not name_too_long("a" * 255)
    assert name_too_long("a" * 256)
    assert not name_too_long("\U0001f600" * 127 + "a")  # 255 units
    assert name_too_long("\U0001f600" * 128)  # 256 units


# --- path syntax --------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "relative\\dir",
        "relative/dir",
        ".",
        "a\0b",
        "\\\\?\\C:\\x",
        "\\\\.\\C:",
        "//?/C:/x",
    ],
)
def test_normalize_rejects_relative_device_and_garbage_paths(raw: str):
    with pytest.raises(PathSafetyError) as info:
        normalize_dir_path(raw)
    assert info.value.problem is PathProblem.INVALID_PATH


@pytest.mark.windows_only
@pytest.mark.parametrize("raw", ["C:relative", "\\rooted\\no\\drive"])
def test_normalize_rejects_drive_relative_paths(raw: str):
    with pytest.raises(PathSafetyError) as info:
        normalize_dir_path(raw)
    assert info.value.problem is PathProblem.INVALID_PATH


def test_normalize_collapses_dot_segments_lexically(tmp_path: Path):
    raw = str(tmp_path / "a" / ".." / "b" / ".")
    assert normalize_dir_path(raw) == tmp_path / "b"
    assert normalize_dir_path(tmp_path) == tmp_path


def test_normalize_rejects_bytes():
    with pytest.raises(TypeError):
        normalize_dir_path(b"C:\\x")  # type: ignore[arg-type]


# --- directory chains ---------------------------------------------------------------


def test_chain_of_existing_directory(tmp_path: Path):
    chain = check_directory_chain(tmp_path)
    assert chain.exists
    assert chain.deepest == tmp_path
    assert chain.missing == ()


def test_chain_lists_missing_tail_without_creating_it(tmp_path: Path, snapshot):
    before = snapshot(tmp_path)
    target = tmp_path / "new" / "deeper" / "PDF"
    chain = check_directory_chain(target)
    assert not chain.exists
    assert chain.deepest == tmp_path
    assert chain.missing == (tmp_path / "new", tmp_path / "new" / "deeper", target)
    assert snapshot(tmp_path) == before


def test_chain_rejects_file_component(tmp_path: Path):
    (tmp_path / "file").write_text("x")
    with pytest.raises(PathSafetyError) as info:
        check_directory_chain(tmp_path / "file" / "below")
    assert info.value.problem is PathProblem.NOT_DIRECTORY
    assert info.value.path == tmp_path / "file"


@pytest.mark.windows_only
@pytest.mark.parametrize("tail", [(), ("inner",), ("inner", "missing")])
def test_chain_rejects_junction_at_any_depth(tmp_path: Path, make_junction, tail: tuple[str, ...]):
    real = tmp_path / "real"
    (real / "inner").mkdir(parents=True)
    link = make_junction(tmp_path / "link", real)
    with pytest.raises(PathSafetyError) as info:
        check_directory_chain(link.joinpath(*tail))
    assert info.value.problem is PathProblem.REPARSE_POINT
    assert info.value.path == link


@pytest.mark.windows_only
def test_chain_reports_missing_drive_as_unavailable():
    used = {d for d in string.ascii_uppercase if Path(f"{d}:\\").exists()}
    free = next((d for d in reversed(string.ascii_uppercase) if d not in used), None)
    if free is None:
        pytest.skip("no unused drive letter")
    with pytest.raises(PathSafetyError) as info:
        check_directory_chain(Path(f"{free}:\\dest"))
    assert info.value.problem is PathProblem.UNAVAILABLE


@pytest.mark.parametrize(
    ("error", "problem"),
    [
        (PermissionError(13, "denied"), PathProblem.ACCESS_DENIED),
        (OSError(5, "device error"), PathProblem.UNREADABLE),
    ],
)
def test_chain_maps_stat_errors(tmp_path: Path, monkeypatch, error: OSError, problem: PathProblem):
    broken = tmp_path / "broken"
    broken.mkdir()
    real_lstat = pathsafety.lstat

    def faulty(path: os.PathLike[str]) -> os.stat_result:
        if Path(path) == broken:
            raise error
        return real_lstat(path)

    monkeypatch.setattr(pathsafety, "lstat", faulty)
    with pytest.raises(PathSafetyError) as info:
        check_directory_chain(broken / "dest")
    assert info.value.problem is problem
    assert info.value.path == broken


def test_chain_refuses_component_with_unknown_metadata(tmp_path: Path, monkeypatch):
    odd = tmp_path / "odd"
    odd.mkdir()
    real_lstat = pathsafety.lstat

    def without_attributes(path: os.PathLike[str]) -> os.stat_result:
        if Path(path) == odd:
            return fake_stat(DIR, attrs=MISSING, ino=5, dev=6)
        return real_lstat(path)

    monkeypatch.setattr(pathsafety, "lstat", without_attributes)
    with pytest.raises(PathSafetyError) as info:
        check_directory_chain(odd / "DEL" / "PDF")
    assert info.value.problem is PathProblem.METADATA_UNKNOWN
    assert info.value.path == odd
    assert "attributes" in (info.value.detail or "")


# --- directory identity -------------------------------------------------------------

A, B = Path("C:/data/a"), Path("C:/data/b")


@pytest.mark.parametrize(
    ("a_st", "b_st", "expected"),
    [
        pytest.param(
            fake_stat(DIR, ino=7, dev=9), fake_stat(DIR, ino=7, dev=9), "same", id="ids-equal"
        ),
        pytest.param(
            fake_stat(DIR, ino=7, dev=9), fake_stat(DIR, ino=8, dev=9), "different", id="ids-differ"
        ),
        pytest.param(
            fake_stat(DIR, ino=7, dev=9),
            fake_stat(DIR, ino=7, dev=10),
            "different",
            id="dev-differ",
        ),
        pytest.param(fake_stat(DIR), fake_stat(DIR), "unknown", id="zero-ids"),
        pytest.param(fake_stat(DIR, ino=7, dev=9), fake_stat(DIR), "unknown", id="one-side-zero"),
        pytest.param(fake_stat(DIR, ino=7), fake_stat(DIR, ino=8), "unknown", id="dev-zero"),
        pytest.param(None, fake_stat(DIR, ino=7, dev=9), "unknown", id="stat-missing"),
        pytest.param(None, None, "unknown", id="both-missing"),
    ],
)
def test_directory_relation_is_tri_state(
    a_st: os.stat_result | None, b_st: os.stat_result | None, expected: str
):
    # R2 defect: zero or missing ids gave "not the same", i.e. DIFFERENT.
    assert directory_relation(A, a_st, B, b_st) is DirectoryRelation(expected)


def test_equal_paths_are_the_same_without_ids():
    assert directory_relation(A, None, A, None) is DirectoryRelation.SAME
    assert directory_relation(A, fake_stat(DIR), Path("C:/data/x/../a"), None) is not (
        DirectoryRelation.DIFFERENT
    )


def test_real_distinct_directories_are_different(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    a_st, b_st = pathsafety.lstat(a), pathsafety.lstat(b)
    assert directory_relation(a, a_st, a, a_st) is DirectoryRelation.SAME
    assert directory_relation(a, a_st, b, b_st) is DirectoryRelation.DIFFERENT


@pytest.mark.windows_only
def test_directory_relation_ignores_case_on_windows(tmp_path: Path):
    a = tmp_path / "Folder"
    a.mkdir()
    upper = Path(str(a).upper())
    assert directory_relation(a, None, upper, None) is DirectoryRelation.SAME
    assert directory_relation(a, a.stat(), upper, upper.stat()) is DirectoryRelation.SAME
