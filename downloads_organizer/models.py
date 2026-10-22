"""Data model of a move plan: what the planner decided for every candidate file.

A plan is an immutable preview. It records the exact target path (including a
collision suffix) and a metadata snapshot of every source file, so the executor can
re-check before acting. The snapshot is evidence, not a guarantee that the file is
unchanged later.

The classes check their own invariants on construction (enum values, consistent
action/reason, no moves next to plan errors, targets directly in a category folder of
the destination root) and raise ``TypeError``/``ValueError`` otherwise. That keeps a
contradictory plan from being built by mistake; it does not replace the executor's
own checks before it touches the filesystem.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class CollisionPolicy(StrEnum):
    """What to do when the target name is already taken."""

    RENAME = "rename"  # pick ``name_1.ext``, ``name_2.ext``, ... as the legacy program did
    SKIP = "skip"


class PlanAction(StrEnum):
    MOVE = "move"  # move under the original name
    RENAME = "rename"  # move under a new name with a collision suffix
    SKIP = "skip"


class SkipReason(StrEnum):
    """Why a candidate file (selected extension, top level of the source) is not moved."""

    SYMLINK = "symlink"
    CLOUD_PLACEHOLDER = "cloud_placeholder"
    OFFLINE = "offline"
    REPARSE_POINT = "reparse_point"
    NOT_REGULAR_FILE = "not_regular_file"
    SYSTEM = "system"
    HIDDEN = "hidden"
    METADATA_UNKNOWN = "metadata_unknown"  # attributes, reparse tag, size or mtime unusable
    PARTIAL_DOWNLOAD = "partial_download"
    VANISHED = "vanished"
    STAT_FAILED = "stat_failed"
    TARGET_DIR_INVALID = "target_dir_invalid"
    TARGET_DIR_IS_SOURCE = "target_dir_is_source"
    TARGET_DIR_IDENTITY_UNKNOWN = "target_dir_identity_unknown"  # might be the source
    TARGET_CHECK_FAILED = "target_check_failed"
    TARGET_EXISTS = "target_exists"
    TARGET_RESERVED = "target_reserved"
    NO_FREE_NAME = "no_free_name"
    NAME_TOO_LONG = "name_too_long"


class PlanErrorReason(StrEnum):
    """Why no plan could be built at all."""

    NOTHING_SELECTED = "nothing_selected"
    SOURCE_INVALID_PATH = "source_invalid_path"
    SOURCE_MISSING = "source_missing"
    SOURCE_NOT_DIRECTORY = "source_not_directory"
    SOURCE_REPARSE_POINT = "source_reparse_point"
    SOURCE_ACCESS_DENIED = "source_access_denied"
    SOURCE_UNREADABLE = "source_unreadable"
    SOURCE_METADATA_UNKNOWN = "source_metadata_unknown"
    DESTINATION_INVALID_PATH = "destination_invalid_path"
    DESTINATION_UNAVAILABLE = "destination_unavailable"
    DESTINATION_NOT_DIRECTORY = "destination_not_directory"
    DESTINATION_REPARSE_POINT = "destination_reparse_point"
    DESTINATION_ACCESS_DENIED = "destination_access_denied"
    DESTINATION_METADATA_UNKNOWN = "destination_metadata_unknown"
    SAME_DIRECTORY = "same_directory"
    DIRECTORY_IDENTITY_UNKNOWN = "directory_identity_unknown"  # might be the same directory
    # Set by ``volumeproof.preview_plan`` only: the volume check refused the plan.
    VOLUME_UNVERIFIED = "volume_unverified"
    CROSS_VOLUME = "cross_volume"  # cross-volume copy is not enabled
    VOLUME_AMBIGUOUS = "volume_ambiguous"


class VolumeMode(StrEnum):
    """How the source and the destination root relate, as proven by ``volumeproof``."""

    SAME = "same"  # one proven volume: the same-volume executor applies
    CROSS = "cross"  # two proven volumes: refused, cross-volume copy is not enabled
    AMBIGUOUS = "ambiguous"  # device and volume serial disagree: refused
    UNVERIFIED = "unverified"  # a location could not be proven: refused


class ProbeReason(StrEnum):
    """Why ``volumeproof.probe_location`` could not prove a location."""

    UNSUPPORTED_PLATFORM = "unsupported_platform"
    ROOT_UNVERIFIED = "root_unverified"  # drive letter, volume root or filesystem
    OPEN_FAILED = "open_failed"  # opening or querying a component failed
    REPARSE_IN_PATH = "reparse_in_path"  # symlink, junction or other reparse point
    NOT_A_DIRECTORY = "not_a_directory"
    MOUNTED_VOLUME = "mounted_volume"  # a component is on another volume
    PATH_CHANGED_DURING_PREVIEW = "path_changed_during_preview"
    RELEASE_FAILED = "release_failed"  # a handle of the probe was not released


def _require(value: object, kind: type | tuple[type, ...], name: str) -> None:
    if not isinstance(value, kind):
        raise TypeError(f"{name}: unexpected {type(value).__name__} {value!r}")


def _optional_int(value: object, name: str, *, signed: bool = False) -> None:
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name}: expected int or None, got {type(value).__name__}")
    if not signed and value < 0:
        raise ValueError(f"{name}: negative value {value}")


def _tuple_of[T](values: Iterable[T], kind: type[T], name: str) -> tuple[T, ...]:
    """An own tuple copy of ``values``; a list passed in cannot change the plan later."""
    if isinstance(values, str | bytes):
        raise TypeError(f"{name}: expected a collection, got {type(values).__name__}")
    result = tuple(values)
    for value in result:
        _require(value, kind, f"{name} element")
    return result


def _plain_component(name: str) -> bool:
    return name not in ("", ".", "..")


@dataclass(frozen=True, slots=True)
class FileInfo:
    """Metadata snapshot of a source file taken while planning.

    ``attributes`` are Windows file attributes; ``file_id`` and ``device`` are the
    file index and volume serial number. A field is ``None`` when the platform did not
    provide a usable value (for the ids: a non-zero one); only skipped items may lack
    size, mtime or attributes.
    """

    size: int | None
    mtime_ns: int | None
    attributes: int | None
    file_id: int | None
    device: int | None

    def __post_init__(self) -> None:
        _optional_int(self.size, "size")
        _optional_int(self.mtime_ns, "mtime_ns", signed=True)
        _optional_int(self.attributes, "attributes")
        _optional_int(self.file_id, "file_id")
        _optional_int(self.device, "device")

    @property
    def complete(self) -> bool:
        """Size, mtime and attributes are known (required for a planned move)."""
        return None not in (self.size, self.mtime_ns, self.attributes)


@dataclass(frozen=True, slots=True)
class PlanItem:
    """The planner's decision for one candidate file.

    ``target`` is the intended final path: the chosen name for ``MOVE``/``RENAME``,
    and for ``SKIP`` the path the file would have been moved to. ``reason`` is set
    exactly for ``SKIP``; ``detail`` carries extra diagnostics (e.g. an OS error).
    ``file`` is the freshest metadata read; for some skipped items it comes from the
    directory listing only and then has no ``file_id``/``device``.

    ``action`` and ``reason`` may be given as their string values and are normalized
    to the enums. ``MOVE`` keeps the source name, ``RENAME`` changes it, and both need
    a complete snapshot.
    """

    source: Path
    target: Path
    category_key: str
    extension: str
    action: PlanAction
    file: FileInfo
    reason: SkipReason | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", PlanAction(self.action))
        if self.reason is not None:
            object.__setattr__(self, "reason", SkipReason(self.reason))
        _require(self.source, Path, "source")
        _require(self.target, Path, "target")
        _require(self.category_key, str, "category_key")
        _require(self.extension, str, "extension")
        _require(self.file, FileInfo, "file")
        _require(self.detail, (str, type(None)), "detail")
        if (self.action is PlanAction.SKIP) != (self.reason is not None):
            raise ValueError("reason must be set exactly for skipped items")
        if not (_plain_component(self.source.name) and _plain_component(self.target.name)):
            raise ValueError("source and target must name a file")
        if self.action is PlanAction.SKIP:
            return
        if not self.file.complete:
            raise ValueError(f"{self.action} needs a complete metadata snapshot")
        if (self.action is PlanAction.MOVE) != (self.target.name == self.source.name):
            raise ValueError("move keeps the file name, rename changes it")


@dataclass(frozen=True, slots=True)
class PlanError:
    """A problem that prevents planning; ``path`` is the offending path, if any."""

    reason: PlanErrorReason
    path: Path | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", PlanErrorReason(self.reason))
        _require(self.path, (Path, type(None)), "path")
        _require(self.detail, (str, type(None)), "detail")


@dataclass(frozen=True, slots=True)
class LocationProof:
    """A directory proven, read-only through handles, to be on one local NTFS volume.

    ``device`` is the ``\\Device\\HarddiskVolumeN`` the drive letter was defined as and
    ``volume_serial`` the serial read from the handles; together they identify the
    volume. ``deepest`` is ``path`` if it exists, else its nearest existing ancestor,
    and ``deepest_id`` that directory's file id. Evidence of the moment it was read.
    """

    path: Path
    device: str
    volume_serial: int
    filesystem: str
    deepest: Path
    deepest_id: int
    exists: bool

    def __post_init__(self) -> None:
        _require(self.path, Path, "path")
        _require(self.device, str, "device")
        _require(self.filesystem, str, "filesystem")
        _require(self.deepest, Path, "deepest")
        _require(self.exists, bool, "exists")
        for name in ("volume_serial", "deepest_id"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise TypeError(f"{name}: expected a non-negative int, got {value!r}")
        if self.deepest not in (self.path, *self.path.parents):
            raise ValueError(f"{self.deepest} is not {self.path} or one of its parents")
        if self.exists != (self.deepest == self.path):
            raise ValueError("exists means that the deepest existing directory is the path")

    @property
    def volume(self) -> tuple[str, int]:
        """The identity decisions are made by: device (case-insensitive) and serial."""
        return (self.device.upper(), self.volume_serial)


@dataclass(frozen=True, slots=True)
class LocationProblem:
    """Why ``path`` could not be proven; ``detail`` is for display only."""

    path: Path
    reason: ProbeReason
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", ProbeReason(self.reason))
        _require(self.path, Path, "path")
        _require(self.detail, (str, type(None)), "detail")


def volume_mode(a: LocationProof, b: LocationProof) -> VolumeMode:
    """SAME if device and serial agree, CROSS if both differ, AMBIGUOUS otherwise."""
    same_device = a.device.upper() == b.device.upper()
    same_serial = a.volume_serial == b.volume_serial
    if same_device and same_serial:
        return VolumeMode.SAME
    if not same_device and not same_serial:
        return VolumeMode.CROSS
    return VolumeMode.AMBIGUOUS


@dataclass(frozen=True, slots=True)
class VolumeProof:
    """Result of ``volumeproof.probe_volumes`` for a source and a destination root.

    ``UNVERIFIED`` has at least one problem (and the proofs that could be made, for
    display); every other mode has both proofs, no problem, and is what
    ``volume_mode`` says for them.
    """

    mode: VolumeMode
    source: LocationProof | None
    destination: LocationProof | None
    problems: tuple[LocationProblem, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", VolumeMode(self.mode))
        _require(self.source, (LocationProof, type(None)), "source")
        _require(self.destination, (LocationProof, type(None)), "destination")
        problems = _tuple_of(self.problems, LocationProblem, "problems")
        object.__setattr__(self, "problems", problems)
        if self.mode is VolumeMode.UNVERIFIED:
            if not problems:
                raise ValueError("an unverified volume proof needs a problem")
            return
        if problems or self.source is None or self.destination is None:
            raise ValueError(f"{self.mode} needs both proofs and no problem")
        if volume_mode(self.source, self.destination) is not self.mode:
            raise ValueError(f"{self.mode} does not match the proven volumes")


@dataclass(frozen=True, slots=True)
class Plan:
    """Result of ``build_plan``. With ``errors`` there are no items.

    ``destination_root`` is the effective root: ``<source>/DEL`` without an explicit
    destination, otherwise the destination itself; categories go below it.
    ``directories_to_create`` lists missing directories the executor would have to
    create for the planned moves, parents first.

    Invariants, checked on construction: ``errors`` exclude ``items`` and
    ``directories_to_create``; every item is a file directly in ``source`` with the
    target ``<destination_root>/<folder>/<name>``; directories to create are only the
    root, its ancestors and folders directly below it, and only for planned moves.
    The collections are stored as tuples copied from the arguments.

    ``volume`` is the read-only volume proof of ``volumeproof.preview_plan`` (``None``:
    not checked, as ``build_plan`` returns it). It belongs to this source and
    destination root, and a plan whose volumes are not proven ``SAME`` has errors,
    unless it is a ``cross_copy`` plan.

    ``cross_copy`` (R3B-5, off unless ``features.CROSS_VOLUME_COPY``): the items are
    copies to another volume, every source is kept. Only a preview that asked for it
    sets it, and only with a ``CROSS`` proof and no errors.
    """

    source: Path
    destination_root: Path | None
    explicit_destination: bool
    policy: CollisionPolicy
    items: tuple[PlanItem, ...] = ()
    errors: tuple[PlanError, ...] = ()
    directories_to_create: tuple[Path, ...] = ()
    volume: VolumeProof | None = None
    cross_copy: bool = False

    def __post_init__(self) -> None:
        self._check_lists()
        volume = self.volume
        _require(volume, (VolumeProof, type(None)), "volume")
        _require(self.cross_copy, bool, "cross_copy")
        if self.cross_copy and (volume is None or volume.mode is not VolumeMode.CROSS):
            raise ValueError("a cross-volume copy needs volumes proven cross")
        if self.cross_copy and self.errors:
            raise ValueError("a plan with errors is no cross-volume copy")
        if volume is None:
            return
        if self.destination_root is None:
            raise ValueError("a volume proof needs a destination root")
        if volume.mode is not VolumeMode.SAME and not self.errors and not self.cross_copy:
            raise ValueError(f"volumes proven {volume.mode} refuse the plan: errors expected")
        if volume.source is not None and volume.source.path != self.source:
            raise ValueError("the volume proof is not of this source")
        if volume.destination is not None and volume.destination.path != self.destination_root:
            raise ValueError("the volume proof is not of this destination root")
        for problem in volume.problems:
            if problem.path not in (self.source, self.destination_root):
                raise ValueError(f"volume problem of {problem.path}: not a path of this plan")

    def _check_lists(self) -> None:
        object.__setattr__(self, "policy", CollisionPolicy(self.policy))
        items = _tuple_of(self.items, PlanItem, "items")
        errors = _tuple_of(self.errors, PlanError, "errors")
        directories = _tuple_of(self.directories_to_create, Path, "directories_to_create")
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "errors", errors)
        object.__setattr__(self, "directories_to_create", directories)
        _require(self.source, Path, "source")
        _require(self.destination_root, (Path, type(None)), "destination_root")
        _require(self.explicit_destination, bool, "explicit_destination")
        if errors and (items or directories):
            raise ValueError("a plan with errors has no items and no directories to create")
        root = self.destination_root
        if root is None:
            if items or directories:
                raise ValueError("a plan with items needs a destination root")
            return
        for item in items:
            if item.source.parent != self.source:
                raise ValueError(f"{item.source} is not directly in the source {self.source}")
            folder = item.target.parent
            if folder.parent != root or not _plain_component(folder.name):
                raise ValueError(f"{item.target} is not in a category folder of {root}")
        if directories and not any(i.action is not PlanAction.SKIP for i in items):
            raise ValueError("directories to create without planned moves")
        allowed = (root, *root.parents)
        for directory in directories:
            if directory not in allowed and directory.parent != root:
                raise ValueError(f"{directory} is not the destination root or directly below it")

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def moves(self) -> tuple[PlanItem, ...]:
        """Planned moves and renames; always empty for a plan with errors."""
        if self.errors:
            return ()
        return tuple(i for i in self.items if i.action is not PlanAction.SKIP)

    @property
    def skipped(self) -> tuple[PlanItem, ...]:
        return tuple(i for i in self.items if i.action is PlanAction.SKIP)


@dataclass(frozen=True, slots=True)
class OutsideGroup:
    """Files outside the plan with one extension (``None``: no extension).

    ``known``: the extension is in the table but not selected; otherwise no category has it.
    """

    extension: str | None
    count: int
    known: bool = False

    def __post_init__(self) -> None:
        _require(self.extension, (str, type(None)), "extension")
        _require(self.known, bool, "known")
        if not isinstance(self.count, int) or isinstance(self.count, bool) or self.count < 1:
            raise ValueError(f"count of an outside group must be a positive int: {self.count!r}")
        if self.extension is None and self.known:
            raise ValueError("files without an extension cannot have a known extension")


@dataclass(frozen=True, slots=True)
class Inventory:
    """What one listing of the source held, counted while the plan was built (R4-D).

    Only the top level, as the plan sees it: nothing below a folder is read and no link
    or junction is followed. ``files`` are all non-directory entries (plain files and
    file links alike), ``links`` how many of them are links or other reparse points,
    ``folders`` the directory entries (junctions and directory links included), which
    are never inputs. Every file is either an item of the plan (skipped ones included:
    ``in_plan``) or in one of the ``outside`` groups: ``files == in_plan + outside``.
    The numbers are of the moment of the listing, not of later changes.
    """

    files: int
    links: int
    folders: int
    in_plan: int
    outside: tuple[OutsideGroup, ...] = ()

    def __post_init__(self) -> None:
        for name in ("files", "links", "folders", "in_plan"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative int: {value!r}")
        outside = _tuple_of(self.outside, OutsideGroup, "outside")
        object.__setattr__(self, "outside", outside)
        if len({(g.extension, g.known) for g in outside}) != len(outside):
            raise ValueError("an extension appears in more than one outside group")
        if self.links > self.files:
            raise ValueError("more links than files")
        if self.files != self.in_plan + self.outside_count:
            raise ValueError("files must be the plan's items plus the files outside it")

    @property
    def outside_count(self) -> int:
        return sum(g.count for g in self.outside)
