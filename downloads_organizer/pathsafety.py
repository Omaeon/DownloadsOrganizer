"""Read-only path checks: path syntax, directory chains, file attributes, name keys.

Nothing here creates, opens for reading or changes anything. Metadata is read with
``os.stat(..., follow_symlinks=False)``; file classification can use the data that
``os.scandir`` already returned, so cloud placeholders are recognised without being
opened. Metadata that is missing or malformed is reported as unknown, never taken
for "safe". A check only describes the moment it ran: it does not protect against
the path being replaced afterwards (the executor has to re-check and use operations
that refuse to overwrite).
"""

from __future__ import annotations

import os
import stat
import sys
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from downloads_organizer.models import SkipReason

# Windows file attributes that ``stat`` does not define.
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x0004_0000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x0040_0000
# IO_REPARSE_TAG_CLOUD and IO_REPARSE_TAG_CLOUD_1..F differ only in bits 12-15.
IO_REPARSE_TAG_CLOUD = 0x9000_001A
_CLOUD_TAG_MASK = 0xFFFF_0FFF
_UINT32_MAX = 0xFFFF_FFFF

MAX_NAME_UTF16_UNITS = 255
"""Longest file name component on NTFS, in UTF-16 code units."""

_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "//?/", "//./", "\\\\?/", "\\\\./")

_INVALID_NAME_CHARS = frozenset('<>:"/\\|?*')
# Control, format (e.g. bidi overrides) and lone surrogate characters.
_INVALID_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
# Device names Windows reserves in every directory, with or without an extension.
# A superset of ``os.path.isreserved`` (which leaves out COM0 and LPT0).
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{port}{n}" for port in ("COM", "LPT") for n in "0123456789¹²³"}
)


class PathProblem(StrEnum):
    INVALID_PATH = "invalid_path"
    UNAVAILABLE = "unavailable"
    NOT_DIRECTORY = "not_directory"
    REPARSE_POINT = "reparse_point"
    ACCESS_DENIED = "access_denied"
    UNREADABLE = "unreadable"
    METADATA_UNKNOWN = "metadata_unknown"


class DirectoryRelation(StrEnum):
    """Whether two directory paths name the same directory."""

    SAME = "same"
    DIFFERENT = "different"
    UNKNOWN = "unknown"  # not provable either way: callers must refuse, not assume DIFFERENT


class PathSafetyError(Exception):
    """A path failed a safety check; ``path`` is the offending component."""

    def __init__(self, problem: PathProblem, path: Path | None, detail: str | None = None) -> None:
        self.problem = problem
        self.path = path
        self.detail = detail
        super().__init__(f"{problem}: {path}" + (f" ({detail})" if detail else ""))


class UnsafeFolderNameError(ValueError):
    """A category folder is not one safe Windows directory name (an API error)."""

    def __init__(self, folder: object, problem: str) -> None:
        self.folder = folder
        self.problem = problem
        super().__init__(f"unsafe category folder {folder!r}: {problem}")


@dataclass(frozen=True, slots=True)
class DirectoryChain:
    """Result of ``check_directory_chain``.

    Every component of ``path`` up to ``deepest`` exists and is a plain directory;
    ``missing`` are the components below it that do not exist yet, parents first.
    """

    path: Path
    deepest: Path
    deepest_stat: os.stat_result
    missing: tuple[Path, ...]

    @property
    def exists(self) -> bool:
        return not self.missing


def lstat(path: str | os.PathLike[str]) -> os.stat_result:
    """``os.stat`` without following a final symlink or junction (the single stat call)."""
    return Path(path).stat(follow_symlinks=False)


# --- metadata fields ----------------------------------------------------------------


def int_field(
    st: object, name: str, *, minimum: int | None = 0, maximum: int | None = None
) -> int | None:
    """``st.<name>`` if it is an ``int`` (not ``bool``) within range, else ``None``.

    ``None`` means unknown: the field is missing, ``None``, of another type or out of
    range. Callers must treat that as a reason to refuse, not as zero.
    """
    value = getattr(st, name, None)
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        return None
    return value


def file_mode(st: object) -> int | None:
    return int_field(st, "st_mode", maximum=_UINT32_MAX)


def windows_attributes(st: object) -> int | None:
    """``FILE_ATTRIBUTE_*`` bits, or ``None`` if unknown."""
    return int_field(st, "st_file_attributes", maximum=_UINT32_MAX)


def reparse_tag(st: object) -> int | None:
    """The reparse tag, or ``None`` if unknown.

    Only meaningful when ``FILE_ATTRIBUTE_REPARSE_POINT`` is set; Windows reports 0
    otherwise. A missing tag is therefore harmless without that attribute, but with
    it the kind of reparse point (symlink, cloud file, ...) is unknown.
    """
    return int_field(st, "st_reparse_tag", maximum=_UINT32_MAX)


def is_cloud_tag(tag: int) -> bool:
    return tag & _CLOUD_TAG_MASK == IO_REPARSE_TAG_CLOUD


def metadata_problem(st: object, *, file: bool = False) -> str | None:
    """What makes ``st`` unusable for the safety checks, or ``None`` if it is complete.

    Required: ``st_mode`` and the Windows attributes; the reparse tag when the
    reparse attribute is set (it must then be non-zero) and, if present, a valid tag
    otherwise. For ``file`` also the size and mtime the plan snapshot records.
    """
    if file_mode(st) is None:
        return "file mode unavailable or invalid"
    attrs = windows_attributes(st)
    if attrs is None:
        return "file attributes unavailable or invalid"
    tag = reparse_tag(st)
    if getattr(st, "st_reparse_tag", None) is not None and tag is None:
        return "reparse tag invalid"
    if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        if tag is None:
            return "reparse point without a reparse tag"
        if tag == 0:
            return "reparse point with a zero reparse tag"
    if file:
        if int_field(st, "st_size") is None:
            return "file size unavailable or invalid"
        if int_field(st, "st_mtime_ns", minimum=None) is None:
            return "modification time unavailable or invalid"
    return None


def is_directory(st: object) -> bool:
    """Directory entry, including directory symlinks and junctions (never followed).

    ``False`` when neither the mode nor the attributes are known; such an entry is
    then treated as a file candidate, whose unknown metadata makes it skipped.
    """
    mode, attrs = file_mode(st), windows_attributes(st)
    return (mode is not None and stat.S_ISDIR(mode)) or (
        attrs is not None and bool(attrs & stat.FILE_ATTRIBUTE_DIRECTORY)
    )


def directory_problem(st: object) -> PathProblem | None:
    """Why ``st`` is not a plain directory (no link, junction or reparse point)."""
    mode, attrs, tag = file_mode(st), windows_attributes(st), reparse_tag(st)
    if mode is None or attrs is None or metadata_problem(st) is not None:
        return PathProblem.METADATA_UNKNOWN
    if stat.S_ISLNK(mode) or attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT or tag:
        return PathProblem.REPARSE_POINT
    if not stat.S_ISDIR(mode):
        return PathProblem.NOT_DIRECTORY
    return None


def file_skip_reason(st: object) -> SkipReason | None:
    """Why a non-directory entry must not be moved, or ``None`` if it is a plain file.

    The checks only use attributes and the reparse tag, never the file contents.
    Links and cloud files are named as such even if other metadata is unknown;
    otherwise incomplete metadata gives ``METADATA_UNKNOWN``.
    """
    mode, attrs, tag = file_mode(st), windows_attributes(st), reparse_tag(st)
    if (mode is not None and stat.S_ISLNK(mode)) or tag == stat.IO_REPARSE_TAG_SYMLINK:
        return SkipReason.SYMLINK
    if (tag is not None and is_cloud_tag(tag)) or (
        attrs is not None
        and attrs & (FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    ):
        return SkipReason.CLOUD_PLACEHOLDER
    if mode is None or attrs is None or metadata_problem(st, file=True) is not None:
        return SkipReason.METADATA_UNKNOWN
    if attrs & stat.FILE_ATTRIBUTE_OFFLINE:
        return SkipReason.OFFLINE
    if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT or tag:
        return SkipReason.REPARSE_POINT
    if not stat.S_ISREG(mode) or attrs & stat.FILE_ATTRIBUTE_DEVICE:
        return SkipReason.NOT_REGULAR_FILE
    if attrs & stat.FILE_ATTRIBUTE_SYSTEM:
        return SkipReason.SYSTEM
    if attrs & stat.FILE_ATTRIBUTE_HIDDEN:
        return SkipReason.HIDDEN
    return None


def file_identity(st: object) -> tuple[int, int] | None:
    """``(device, file_id)`` when the platform provided both as positive integers."""
    dev, ino = int_field(st, "st_dev", minimum=1), int_field(st, "st_ino", minimum=1)
    if dev is None or ino is None:
        return None
    return (dev, ino)


# --- names --------------------------------------------------------------------------


def name_key(name: str) -> str:
    """Case-insensitive comparison key for a file name.

    Approximates the Windows rule (per-character uppercase, no Unicode
    normalization); ``upper()`` first so that e.g. dotless i (U+0131) matches ``I``. It may
    treat a few names as equal that NTFS distinguishes (``ß``/``SS``), which only
    leads to a needless rename or skip.
    """
    return name.upper().casefold()


def path_key(path: Path) -> str:
    return os.path.normcase(str(path))


def name_too_long(name: str) -> bool:
    return len(name.encode("utf-16-le")) // 2 > MAX_NAME_UTF16_UNITS


def folder_name_problem(name: object) -> str | None:
    """Why ``name`` is not one safe Windows directory name, or ``None`` if it is.

    Rejects everything that is not exactly one plain component: empty, ``.``/``..``,
    separators, drive and stream colons (so also absolute, UNC and drive-relative
    paths), characters Windows forbids, control and format characters, trailing dots
    and spaces (Windows would silently strip them), reserved device names and names
    longer than a component may be. ``os.path.isreserved`` is one of the checks.
    """
    if not isinstance(name, str):
        return f"expected a str, got {type(name).__name__}"
    if not name:
        return "empty"
    if name in (".", ".."):
        return "relative path component"
    bad = sorted(
        {c for c in name if c in _INVALID_NAME_CHARS}
        | {c for c in name if unicodedata.category(c) in _INVALID_NAME_CATEGORIES}
    )
    if bad:
        return f"contains path separators or forbidden characters {''.join(bad)!r}"
    if name.endswith((".", " ")):
        return "ends with a dot or space"
    if name.partition(".")[0].rstrip(" ").upper() in _RESERVED_DEVICE_NAMES:
        return "reserved device name"
    if sys.platform == "win32" and os.path.isreserved(name):
        return "reserved by Windows"
    if name_too_long(name):
        return f"longer than {MAX_NAME_UTF16_UNITS} UTF-16 code units"
    return None


def check_folder_name(name: object) -> str:
    """Return ``name`` if it is a safe directory name, else raise ``UnsafeFolderNameError``."""
    problem = folder_name_problem(name)
    if problem is not None or not isinstance(name, str):
        raise UnsafeFolderNameError(name, problem or "not a str")
    return name


# --- paths --------------------------------------------------------------------------


def normalize_dir_path(raw: str | os.PathLike[str]) -> Path:
    """Return an absolute, normalized path or raise ``PathSafetyError(INVALID_PATH)``.

    Relative, drive-relative and device (``\\\\?\\``, ``\\\\.\\``) paths are rejected,
    so the result never depends on the current directory. ``..`` and, on Windows,
    trailing dots and spaces are resolved the way Windows resolves them.
    """
    text = os.fspath(raw)
    if not isinstance(text, str):
        raise TypeError(f"expected a str path, got {type(text).__name__}")
    if not text.strip() or "\0" in text or text.startswith(_DEVICE_PREFIXES):
        raise PathSafetyError(PathProblem.INVALID_PATH, None, repr(text))
    path = Path(text)
    if not path.is_absolute():
        raise PathSafetyError(PathProblem.INVALID_PATH, None, repr(text))
    # Lexical only: Path.resolve() would follow links, which is exactly what must not happen.
    return Path(os.path.abspath(path))  # noqa: PTH100


def check_directory_chain(path: Path) -> DirectoryChain:
    """Check every existing component of absolute ``path``, from the root down.

    Each existing component must be a directory that is not a symlink, junction or
    other reparse point, with complete metadata. The first missing component ends the
    walk; it and all below it are reported as ``missing``. Nothing is created.
    Raises ``PathSafetyError``.
    """
    if not path.is_absolute():
        raise PathSafetyError(PathProblem.INVALID_PATH, path)
    current = Path(path.anchor)
    components = [current]
    for part in path.parts[1:]:
        current = current / part
        components.append(current)
    deepest: tuple[Path, os.stat_result] | None = None
    for index, component in enumerate(components):
        try:
            st = lstat(component)
        except FileNotFoundError:
            if deepest is None:
                raise PathSafetyError(PathProblem.UNAVAILABLE, component) from None
            return DirectoryChain(path, deepest[0], deepest[1], tuple(components[index:]))
        except NotADirectoryError as exc:
            raise PathSafetyError(PathProblem.NOT_DIRECTORY, component, str(exc)) from None
        except PermissionError as exc:
            raise PathSafetyError(PathProblem.ACCESS_DENIED, component, str(exc)) from None
        except OSError as exc:
            raise PathSafetyError(PathProblem.UNREADABLE, component, str(exc)) from None
        problem = directory_problem(st)
        if problem is PathProblem.METADATA_UNKNOWN:
            raise PathSafetyError(problem, component, metadata_problem(st))
        if problem is PathProblem.REPARSE_POINT:
            tag = reparse_tag(st)
            raise PathSafetyError(problem, component, f"{tag:#x}" if tag is not None else None)
        if problem is not None:
            raise PathSafetyError(problem, component)
        deepest = (component, st)
    if deepest is None:  # unreachable: there is always at least the anchor
        raise PathSafetyError(PathProblem.INVALID_PATH, path)
    return DirectoryChain(path, deepest[0], deepest[1], ())


def directory_relation(
    a: Path, a_st: os.stat_result | None, b: Path, b_st: os.stat_result | None
) -> DirectoryRelation:
    """Whether existing directories ``a`` and ``b`` are the same one.

    Equal normalized paths are ``SAME``. Otherwise only reliable file identities
    (positive volume serial and file id for both) decide; without them aliases such
    as 8.3 short names, ``subst`` drives or network mappings cannot be ruled out and
    the result is ``UNKNOWN``. ``os.path.samefile`` is no better: it compares the very
    same ``st_dev``/``st_ino`` values.
    """
    if path_key(a) == path_key(b):
        return DirectoryRelation.SAME
    a_id = file_identity(a_st) if a_st is not None else None
    b_id = file_identity(b_st) if b_st is not None else None
    if a_id is None or b_id is None:
        return DirectoryRelation.UNKNOWN
    return DirectoryRelation.SAME if a_id == b_id else DirectoryRelation.DIFFERENT
