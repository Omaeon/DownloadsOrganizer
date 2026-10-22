"""Side-effect-free move planning: which top-level files go where, under which name.

``build_plan`` only reads directory listings and metadata. It never creates the
``DEL`` or category directories, never opens files for reading and never moves,
copies, renames or deletes anything. Its result is a preview for the user to
review; the executor must follow it exactly or report what changed.

``plan_with_inventory`` is the same plan with an ``Inventory`` of the very listing it
was made from (R4-D): how many files the folder held and which of them stayed outside
the plan, by extension. It reads nothing more than ``build_plan``.
"""

from __future__ import annotations

import os
import stat
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from downloads_organizer.catalog import PARTIAL_DOWNLOAD_EXTENSIONS, Category
from downloads_organizer.extensions import ExtensionTable, extension_of
from downloads_organizer.models import (
    CollisionPolicy,
    FileInfo,
    Inventory,
    OutsideGroup,
    Plan,
    PlanAction,
    PlanError,
    PlanErrorReason,
    PlanItem,
    SkipReason,
)
from downloads_organizer.pathsafety import (
    DirectoryChain,
    DirectoryRelation,
    PathProblem,
    PathSafetyError,
    UnsafeFolderNameError,
    check_directory_chain,
    check_folder_name,
    directory_problem,
    directory_relation,
    file_identity,
    file_mode,
    file_skip_reason,
    int_field,
    is_directory,
    lstat,
    metadata_problem,
    name_key,
    name_too_long,
    normalize_dir_path,
    path_key,
    reparse_tag,
    windows_attributes,
)

DEFAULT_DESTINATION_DIR = "DEL"
"""Directory inside the source used when no destination is chosen (legacy behaviour)."""

MAX_RENAME_ATTEMPTS = 9999
"""Highest collision suffix tried before a file is skipped with ``NO_FREE_NAME``."""

_SOURCE_ERRORS = {
    PathProblem.INVALID_PATH: PlanErrorReason.SOURCE_INVALID_PATH,
    PathProblem.UNAVAILABLE: PlanErrorReason.SOURCE_MISSING,
    PathProblem.NOT_DIRECTORY: PlanErrorReason.SOURCE_NOT_DIRECTORY,
    PathProblem.REPARSE_POINT: PlanErrorReason.SOURCE_REPARSE_POINT,
    PathProblem.ACCESS_DENIED: PlanErrorReason.SOURCE_ACCESS_DENIED,
    PathProblem.UNREADABLE: PlanErrorReason.SOURCE_UNREADABLE,
    PathProblem.METADATA_UNKNOWN: PlanErrorReason.SOURCE_METADATA_UNKNOWN,
}
_DESTINATION_ERRORS = {
    PathProblem.INVALID_PATH: PlanErrorReason.DESTINATION_INVALID_PATH,
    PathProblem.UNAVAILABLE: PlanErrorReason.DESTINATION_UNAVAILABLE,
    PathProblem.NOT_DIRECTORY: PlanErrorReason.DESTINATION_NOT_DIRECTORY,
    PathProblem.REPARSE_POINT: PlanErrorReason.DESTINATION_REPARSE_POINT,
    PathProblem.ACCESS_DENIED: PlanErrorReason.DESTINATION_ACCESS_DENIED,
    PathProblem.UNREADABLE: PlanErrorReason.DESTINATION_UNAVAILABLE,
    PathProblem.METADATA_UNKNOWN: PlanErrorReason.DESTINATION_METADATA_UNKNOWN,
}


class _TargetCheckError(Exception):
    """A target name could not be checked; carries the OS error text."""


@dataclass(frozen=True, slots=True)
class _Entry:
    name: str
    path: Path
    st: os.stat_result


@dataclass(frozen=True, slots=True)
class _TargetDir:
    path: Path
    exists: bool
    problem: SkipReason | None = None
    detail: str | None = None


def _path_error(
    exc: PathSafetyError, mapping: dict[PathProblem, PlanErrorReason], fallback: Path
) -> PlanError:
    return PlanError(mapping[exc.problem], exc.path or fallback, exc.detail)


def _selected_categories(table: ExtensionTable, selected: Iterable[str]) -> dict[str, Category]:
    """Map each selected extension to its category; raises before any filesystem access.

    ``UnsafeFolderNameError`` if a category folder is not one safe directory name
    (possible only for a ``Category`` changed after construction).
    """
    if isinstance(selected, str):
        raise TypeError("selected must be a collection of extensions, not a str")
    result: dict[str, Category] = {}
    for ext in selected:
        category = table.category_for(ext) if isinstance(ext, str) else None
        if category is None:
            raise ValueError(f"selected extension {ext!r} is not in the extension table")
        check_folder_name(category.folder)
        result[ext.lower()] = category
    return result


def _snapshot(st: os.stat_result) -> FileInfo:
    identity = file_identity(st)
    return FileInfo(
        size=int_field(st, "st_size"),
        mtime_ns=int_field(st, "st_mtime_ns", minimum=None),
        attributes=windows_attributes(st),
        file_id=identity[1] if identity else None,
        device=identity[0] if identity else None,
    )


def _scan(source: Path) -> list[_Entry]:
    """Top-level entries of ``source`` with the metadata ``scandir`` returned.

    On Windows that metadata comes from the directory listing itself, so no entry is
    opened. Raises ``OSError`` if the directory cannot be listed completely.
    """
    entries: list[_Entry] = []
    with os.scandir(source) as it:
        for entry in it:
            entries.append(_Entry(entry.name, Path(entry.path), entry.stat(follow_symlinks=False)))
    entries.sort(key=lambda e: (name_key(e.name), e.name))
    return entries


def _category_dir(root: Path, folder: str) -> Path:
    """``root / folder``, checked to be a direct child of ``root`` (never outside it)."""
    check_folder_name(folder)
    path = root / folder
    if path.name != folder or Path(os.path.normpath(path)).parent != root:
        raise UnsafeFolderNameError(folder, f"does not stay directly below {root}")
    return path


def _target_dir(
    root: DirectoryChain, folder: str, source: Path, source_st: os.stat_result
) -> _TargetDir:
    path = _category_dir(root.path, folder)
    if not root.exists:
        return _TargetDir(path, exists=False)
    try:
        st = lstat(path)
    except FileNotFoundError:
        return _TargetDir(path, exists=False)
    except OSError as exc:
        return _TargetDir(path, True, SkipReason.TARGET_CHECK_FAILED, str(exc))
    problem = directory_problem(st)
    if problem is PathProblem.METADATA_UNKNOWN:
        return _TargetDir(path, True, SkipReason.TARGET_CHECK_FAILED, metadata_problem(st))
    if problem is PathProblem.REPARSE_POINT:
        return _TargetDir(path, True, SkipReason.TARGET_DIR_INVALID, "reparse point")
    if problem is not None:
        return _TargetDir(path, True, SkipReason.TARGET_DIR_INVALID, "not a directory")
    relation = directory_relation(path, st, source, source_st)
    if relation is DirectoryRelation.SAME:
        return _TargetDir(path, True, SkipReason.TARGET_DIR_IS_SOURCE)
    if relation is DirectoryRelation.UNKNOWN:
        return _TargetDir(path, True, SkipReason.TARGET_DIR_IDENTITY_UNKNOWN)
    return _TargetDir(path, exists=True)


def _name_taken(target_dir: _TargetDir, name: str, reserved: set[str]) -> SkipReason | None:
    """``TARGET_RESERVED``/``TARGET_EXISTS`` if ``name`` is taken, else ``None``.

    Existence is asked from the filesystem itself (``lstat`` of the exact path), so
    case-insensitive matches, 8.3 short names and dangling links all count as taken.
    """
    if name_key(name) in reserved:
        return SkipReason.TARGET_RESERVED
    if not target_dir.exists:
        return None
    try:
        lstat(target_dir.path / name)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _TargetCheckError(str(exc)) from None
    return SkipReason.TARGET_EXISTS


def _choose_name(
    name: str, target_dir: _TargetDir, reserved: set[str], policy: CollisionPolicy
) -> tuple[str, SkipReason | None]:
    """Pick the final name (``name``, then ``stem_1.ext``, ...) or the reason to skip."""
    taken = _name_taken(target_dir, name, reserved)
    if taken is None:
        return name, None
    if policy is CollisionPolicy.SKIP:
        return name, taken
    dot = name.rfind(".")
    stem, suffix = name[:dot], name[dot:]
    for n in range(1, MAX_RENAME_ATTEMPTS + 1):
        candidate = f"{stem}_{n}{suffix}"
        if name_too_long(candidate):
            return name, SkipReason.NAME_TOO_LONG
        if _name_taken(target_dir, candidate, reserved) is None:
            return candidate, None
    return name, SkipReason.NO_FREE_NAME


@dataclass(frozen=True, slots=True)
class _Decision:
    action: PlanAction
    name: str
    st: os.stat_result
    reason: SkipReason | None = None
    detail: str | None = None


def _skip_detail(reason: SkipReason, st: os.stat_result) -> str | None:
    return metadata_problem(st, file=True) if reason is SkipReason.METADATA_UNKNOWN else None


def _decide(
    entry: _Entry,
    tdir: _TargetDir,
    present: set[str],
    reserved: set[str],
    policy: CollisionPolicy,
) -> _Decision:
    """Decide for one candidate file; ``present`` are name keys of the source listing."""
    reason = file_skip_reason(entry.st)
    if reason is None and any(
        name_key(entry.name + p) in present for p in PARTIAL_DOWNLOAD_EXTENSIONS
    ):
        reason = SkipReason.PARTIAL_DOWNLOAD  # e.g. "file.pdf" next to "file.pdf.part"
    if reason is not None:
        return _Decision(
            PlanAction.SKIP, entry.name, entry.st, reason, _skip_detail(reason, entry.st)
        )
    # Fresh metadata for the snapshot (file and volume ids are not in the listing).
    try:
        st = lstat(entry.path)
    except FileNotFoundError as exc:
        return _Decision(PlanAction.SKIP, entry.name, entry.st, SkipReason.VANISHED, str(exc))
    except OSError as exc:
        return _Decision(PlanAction.SKIP, entry.name, entry.st, SkipReason.STAT_FAILED, str(exc))
    reason = file_skip_reason(st)
    if reason is not None:
        detail = _skip_detail(reason, st) or "changed while planning"
        return _Decision(PlanAction.SKIP, entry.name, st, reason, detail)
    if tdir.problem is not None:
        return _Decision(PlanAction.SKIP, entry.name, st, tdir.problem, tdir.detail)
    try:
        final, reason = _choose_name(entry.name, tdir, reserved, policy)
    except _TargetCheckError as exc:
        return _Decision(PlanAction.SKIP, entry.name, st, SkipReason.TARGET_CHECK_FAILED, str(exc))
    if reason is not None:
        return _Decision(PlanAction.SKIP, final, st, reason)
    return _Decision(PlanAction.MOVE if final == entry.name else PlanAction.RENAME, final, st)


def _is_link(st: os.stat_result) -> bool:
    """A symlink or another reparse point (counted, never followed)."""
    mode, attrs = file_mode(st), windows_attributes(st)
    return (
        (mode is not None and stat.S_ISLNK(mode))
        or (attrs is not None and bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT))
        or bool(reparse_tag(st))
    )


def _inventory(
    entries: list[_Entry], in_plan: int, outside: Counter[str | None], table: ExtensionTable
) -> Inventory:
    files = [e for e in entries if not is_directory(e.st)]
    groups = sorted(
        (
            OutsideGroup(ext, n, ext is not None and table.category_for(ext) is not None)
            for ext, n in outside.items()
        ),
        key=lambda g: (-g.count, g.extension is None, g.extension or ""),
    )
    return Inventory(
        files=len(files),
        links=sum(_is_link(e.st) for e in files),
        folders=len(entries) - len(files),
        in_plan=in_plan,
        outside=tuple(groups),
    )


def build_plan(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str] | None,
    table: ExtensionTable,
    selected: Iterable[str],
    policy: CollisionPolicy = CollisionPolicy.RENAME,
) -> Plan:
    """The plan of ``plan_with_inventory`` without the inventory."""
    return plan_with_inventory(source, destination, table, selected, policy)[0]


def plan_with_inventory(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str] | None,
    table: ExtensionTable,
    selected: Iterable[str],
    policy: CollisionPolicy = CollisionPolicy.RENAME,
) -> tuple[Plan, Inventory | None]:
    """Plan moving the selected top-level files of ``source``; never changes anything.

    ``destination`` ``None`` means ``<source>/DEL/<category>``, otherwise files go to
    ``<destination>/<category>``. ``selected`` are extensions of ``table`` (an
    unknown one raises ``ValueError``, a category folder that is not one safe
    directory name ``UnsafeFolderNameError``, both before anything is read). Path and
    scan problems are returned as ``Plan.errors`` (then there are no items); per-file
    problems as skipped items. Directories that cannot be proven different from the
    source are refused, never assumed different.
    Items are sorted case-insensitively by file name, and collision names are
    reserved in that order.

    The inventory counts the same listing; it is ``None`` when the plan has errors
    found before the listing or the listing failed (nothing was counted then).
    """
    policy = CollisionPolicy(policy)
    selected_map = _selected_categories(table, selected)
    explicit = destination is not None
    errors: list[PlanError] = []
    raw_source = Path(os.fspath(source))

    src: Path | None = None
    src_chain: DirectoryChain | None = None
    try:
        src = normalize_dir_path(source)
        src_chain = check_directory_chain(src)
        if not src_chain.exists:
            errors.append(PlanError(PlanErrorReason.SOURCE_MISSING, src_chain.missing[0]))
    except PathSafetyError as exc:
        errors.append(_path_error(exc, _SOURCE_ERRORS, src or raw_source))

    root: Path | None = None
    root_chain: DirectoryChain | None = None
    if destination is not None:
        try:
            root = normalize_dir_path(destination)
        except PathSafetyError as exc:
            errors.append(_path_error(exc, _DESTINATION_ERRORS, Path(os.fspath(destination))))
    elif src is not None:
        root = src / DEFAULT_DESTINATION_DIR
    if root is not None and not errors:
        try:
            root_chain = check_directory_chain(root)
        except PathSafetyError as exc:
            errors.append(_path_error(exc, _DESTINATION_ERRORS, root))
    if src is not None and src_chain is not None and root_chain is not None and root_chain.exists:
        relation = directory_relation(src, src_chain.deepest_stat, root, root_chain.deepest_stat)
        if relation is DirectoryRelation.SAME:
            errors.append(PlanError(PlanErrorReason.SAME_DIRECTORY, root))
        elif relation is DirectoryRelation.UNKNOWN:
            errors.append(PlanError(PlanErrorReason.DIRECTORY_IDENTITY_UNKNOWN, root))
    if not selected_map:
        errors.append(PlanError(PlanErrorReason.NOTHING_SELECTED))

    def failed() -> tuple[Plan, None]:
        return Plan(src or raw_source, root, explicit, policy, errors=tuple(errors)), None

    if errors or src is None or src_chain is None or root_chain is None:
        return failed()

    try:
        entries = _scan(src)
    except FileNotFoundError as exc:
        errors.append(PlanError(PlanErrorReason.SOURCE_MISSING, src, str(exc)))
    except PermissionError as exc:
        errors.append(PlanError(PlanErrorReason.SOURCE_ACCESS_DENIED, src, str(exc)))
    except OSError as exc:
        errors.append(PlanError(PlanErrorReason.SOURCE_UNREADABLE, src, str(exc)))
    if errors:
        return failed()

    present = {name_key(e.name) for e in entries}
    target_dirs: dict[str, _TargetDir] = {}
    reserved: dict[str, set[str]] = {}
    to_create: dict[str, Path] = {}
    items: list[PlanItem] = []
    outside: Counter[str | None] = Counter()
    for entry in entries:
        if is_directory(entry.st):
            continue  # folders, directory links and junctions are never inputs
        ext = extension_of(entry.name)
        category = selected_map.get(ext) if ext is not None else None
        if ext is None or category is None:
            outside[ext] += 1
            continue
        if category.key not in target_dirs:
            target_dirs[category.key] = _target_dir(
                root_chain, category.folder, src, src_chain.deepest_stat
            )
        tdir = target_dirs[category.key]
        names = reserved.setdefault(path_key(tdir.path), set())
        decision = _decide(entry, tdir, present, names, policy)
        items.append(
            PlanItem(
                entry.path,
                tdir.path / decision.name,
                category.key,
                ext,
                decision.action,
                _snapshot(decision.st),
                decision.reason,
                decision.detail,
            )
        )
        if decision.action is PlanAction.SKIP:
            continue
        names.add(name_key(decision.name))
        if not tdir.exists:
            for missing in (*root_chain.missing, tdir.path):
                to_create.setdefault(path_key(missing), missing)

    directories = tuple(to_create[k] for k in sorted(to_create))
    plan = Plan(src, root, explicit, policy, tuple(items), (), directories)
    return plan, _inventory(entries, len(items), outside, table)
