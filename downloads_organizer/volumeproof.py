"""Read-only volume proof of a plan's source and destination root (R3B-1).

The preview proves, through Windows handles and without changing anything, on which
volume each of the two directories lies, and whether that is one volume (``SAME``),
two (``CROSS``) or contradictory (``AMBIGUOUS``). Anything that cannot be proven is
``UNVERIFIED``: a refusal, never an assumption. Only ``SAME`` leaves a plan
executable; a cross-volume copy is not enabled, so ``CROSS`` refuses it as well.

``probe_location`` for one directory, in this order:

1. the drive letter must be defined as exactly one ``\\Device\\HarddiskVolumeN``
   (``winfileops.volume_root``: no UNC path, ``subst``, network or other mapped drive);
2. its root is opened (``open_anchor``) and proven (``prove_volume_root``: local NTFS
   disk, root directory, MFT record 5, final NT path ``<device>\\``);
3. every existing component is opened relative to its parent's handle, read
   attributes only, sharing everything, never following a reparse point; each must be
   a directory, no reparse point, on the root's volume (else ``MOUNTED_VOLUME``); a
   missing component ends the walk at its nearest existing ancestor;
4. the drive letter mapping is read again;
5. every handle is released, newest first; a failed release is a refusal and the
   error objects are kept (``release_errors``);
6. the path is checked once more by ``lstat`` (``check_directory_chain``): the same
   deepest directory with the same identity, or ``PATH_CHANGED_DURING_PREVIEW``.

Nothing is created, written, renamed or deleted: the only handles opened are
``FILE_READ_ATTRIBUTES`` (+ ``SYNCHRONIZE``) opens of existing directories.
``check_plan_fresh`` repeats the proof before a run and refuses a plan whose volumes
are no longer what the preview proved (``PLAN_STALE``). The executor is not changed:
it proves the roots again on its own and still refuses another volume.

A cross-volume copy (R3B-5) is planned only on request (``cross_copy=True``) and only
while ``features.CROSS_VOLUME_COPY`` is on; otherwise ``CROSS`` is refused as before.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from downloads_organizer import features, winfileops
from downloads_organizer.extensions import ExtensionTable
from downloads_organizer.models import (
    CollisionPolicy,
    Inventory,
    LocationProblem,
    LocationProof,
    Plan,
    PlanError,
    PlanErrorReason,
    ProbeReason,
    VolumeMode,
    VolumeProof,
    volume_mode,
)
from downloads_organizer.pathsafety import PathSafetyError, check_directory_chain, file_identity
from downloads_organizer.planner import plan_with_inventory

_PROBE_ACCESS = winfileops.FILE_READ_ATTRIBUTES  # _nt_create adds SYNCHRONIZE


@dataclass(frozen=True, slots=True)
class LocationProbe:
    """``probe_location``: a proof or the problem, and every failed release as raised."""

    path: Path
    proof: LocationProof | None
    problem: LocationProblem | None
    release_errors: tuple[BaseException, ...] = field(default=(), compare=False)


@dataclass(frozen=True, slots=True)
class VolumeCheck:
    """``probe_volumes``: the proof to show and keep, and the failed releases."""

    proof: VolumeProof
    release_errors: tuple[BaseException, ...] = field(default=(), compare=False)


@dataclass(frozen=True, slots=True)
class Preview:
    """``preview_plan``: a plan with its volume proof, and the failed releases.

    ``inventory`` counts the listing the plan was built from (``None`` if nothing was
    listed). It stays when the volume proof refuses the plan: the folder was listed."""

    plan: Plan
    release_errors: tuple[BaseException, ...] = field(default=(), compare=False)
    inventory: Inventory | None = None


class FreshnessReason(StrEnum):
    NO_VOLUME_PROOF = "no_volume_proof"  # built without the preview: never executable
    VOLUME_REFUSED = "volume_refused"  # the preview already refused it
    PLAN_STALE = "plan_stale"  # the volumes are not what the preview proved any more


@dataclass(frozen=True, slots=True)
class FreshnessCheck:
    """``check_plan_fresh``: ``fresh`` only without a reason."""

    reason: FreshnessReason | None
    detail: str | None = None
    current: VolumeProof | None = None
    release_errors: tuple[BaseException, ...] = field(default=(), compare=False)

    @property
    def fresh(self) -> bool:
        return self.reason is None


class _Refused(Exception):
    """Internal: a step of the walk refused the location."""

    def __init__(self, reason: ProbeReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _close_all(handles: list[winfileops.Handle]) -> list[BaseException]:
    """Close every handle once, newest first, whatever fails; the failures in order."""
    failures: list[BaseException] = []
    while handles:
        handle = handles.pop()
        try:
            handle.close()
        except BaseException as exc:  # an interrupt too: the others are still closed
            failures.append(exc)
    return failures


def _walk(path: Path, handles: list[winfileops.Handle]) -> LocationProof:
    """Steps 1-4; every handle opened is appended to ``handles`` (the caller closes)."""
    try:
        root = winfileops.volume_root(path)
    except winfileops.UnverifiedRootError as exc:
        raise _Refused(ProbeReason.ROOT_UNVERIFIED, str(exc)) from exc
    try:
        anchor = winfileops.open_anchor(root.anchor)
    except OSError as exc:
        detail = f"cannot open {root.anchor}: {winfileops.describe_error(exc)}"
        raise _Refused(ProbeReason.ROOT_UNVERIFIED, detail) from exc
    handles.append(anchor)
    try:
        winfileops.prove_volume_root(anchor, root)
    except winfileops.UnverifiedRootError as exc:
        raise _Refused(ProbeReason.ROOT_UNVERIFIED, str(exc)) from exc
    try:
        filesystem = winfileops.query_volume(anchor).filesystem
        info = winfileops.query(anchor)
    except OSError as exc:
        detail = f"cannot query {root.anchor}: {winfileops.describe_error(exc)}"
        raise _Refused(ProbeReason.ROOT_UNVERIFIED, detail) from exc
    if filesystem not in winfileops.VERIFIED_FILESYSTEMS:  # what is recorded is what was proven
        detail = f"{root.anchor}: {filesystem or '?'} volume; only NTFS is supported"
        raise _Refused(ProbeReason.ROOT_UNVERIFIED, detail)
    serial = info.volume_serial
    current, deepest, deepest_id, exists = anchor, Path(path.anchor), info.file_id, True
    for part in path.parts[1:]:
        child = deepest / part
        try:
            handle, _ = winfileops.open_relative(
                current, part, directory=True, access=_PROBE_ACCESS, share=winfileops.SHARE_ALL
            )
        except FileNotFoundError:
            exists = False
            break
        except NotADirectoryError as exc:
            raise _Refused(ProbeReason.NOT_A_DIRECTORY, f"{child} is not a directory") from exc
        except OSError as exc:
            detail = f"cannot open {child}: {winfileops.describe_error(exc)}"
            raise _Refused(ProbeReason.OPEN_FAILED, detail) from exc
        handles.append(handle)
        try:
            info = winfileops.query(handle)
        except OSError as exc:
            detail = f"cannot query {child}: {winfileops.describe_error(exc)}"
            raise _Refused(ProbeReason.OPEN_FAILED, detail) from exc
        if info.reparse_point:
            detail = f"{child} is a link, junction or mount point (tag {info.reparse_tag:#x})"
            raise _Refused(ProbeReason.REPARSE_IN_PATH, detail)
        if not info.directory:
            raise _Refused(ProbeReason.NOT_A_DIRECTORY, f"{child} is not a directory")
        if info.volume_serial != serial:
            detail = f"{child} is on volume {info.volume_serial:#x}, not on {serial:#x}"
            raise _Refused(ProbeReason.MOUNTED_VOLUME, detail)
        current, deepest, deepest_id = handle, child, info.file_id
    try:
        winfileops.check_mapping(root)
    except winfileops.UnverifiedRootError as exc:
        raise _Refused(ProbeReason.ROOT_UNVERIFIED, str(exc)) from exc
    return LocationProof(path, root.device, serial, filesystem, deepest, deepest_id, exists)


def _lstat_problem(proof: LocationProof) -> str | None:
    """Step 6: what ``lstat`` sees now, compared with what the handles proved."""
    try:
        chain = check_directory_chain(proof.path)
    except PathSafetyError as exc:
        return str(exc)
    if chain.deepest != proof.deepest or chain.exists != proof.exists:
        return f"deepest existing directory is {chain.deepest}, the handles saw {proof.deepest}"
    if file_identity(chain.deepest_stat) != (proof.volume_serial, proof.deepest_id):
        return f"{proof.deepest} is not the directory the handles saw"
    return None


def probe_location(path: Path) -> LocationProbe:
    """Prove read-only on which volume the absolute directory ``path`` lies.

    A refusal is returned (``problem``), never raised. A programming error or an
    interrupt is raised after every handle was released: the interrupt itself, with
    failed releases attached (``winfileops.prevailing``).
    """
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"not a normalized absolute path: {path!r}")
    if not winfileops.AVAILABLE:
        problem = LocationProblem(path, ProbeReason.UNSUPPORTED_PLATFORM, "Windows only")
        return LocationProbe(path, None, problem)
    handles: list[winfileops.Handle] = []
    proof: LocationProof | None = None
    problem: LocationProblem | None = None
    try:
        proof = _walk(path, handles)
    except _Refused as exc:
        problem = LocationProblem(path, exc.reason, exc.detail)
    except BaseException as exc:
        raised: BaseException = exc
        for failure in _close_all(handles):
            raised = winfileops.prevailing(raised, failure)
        if raised is exc:
            raise
        # A ReleaseError or an interrupt of a release; ``prevailing`` already chained it
        # (its __cause__ / release_errors), ``from`` would replace that.
        raise raised  # noqa: B904
    failures = tuple(_close_all(handles))
    interrupts = [f for f in failures if not isinstance(f, Exception)]
    if interrupts:  # never dropped: the first one, carrying every other failure
        winfileops.attach_release_errors(interrupts[0], failures)
        raise interrupts[0]
    if failures:
        released = "; ".join(winfileops.describe_error(f) for f in failures)
        if problem is None:
            problem = LocationProblem(path, ProbeReason.RELEASE_FAILED, released)
        else:  # the first refusal stays the reason; the release failure is shown too
            detail = f"{problem.detail}; then releasing failed: {released}"
            problem = LocationProblem(path, problem.reason, detail)
        return LocationProbe(path, None, problem, failures)
    if proof is not None:
        changed = _lstat_problem(proof)
        if changed is not None:
            problem = LocationProblem(path, ProbeReason.PATH_CHANGED_DURING_PREVIEW, changed)
            proof = None
    return LocationProbe(path, proof, problem)


def probe_volumes(source: Path, destination_root: Path) -> VolumeCheck:
    """Prove both directories (source first) and how their volumes relate."""
    src = probe_location(source)
    dst = probe_location(destination_root)
    problems = [p for p in (src.problem, dst.problem) if p is not None]
    if src.proof is not None and not src.proof.exists:
        problems.append(
            LocationProblem(
                source, ProbeReason.PATH_CHANGED_DURING_PREVIEW, f"{source} does not exist"
            )
        )
    released = src.release_errors + dst.release_errors
    if problems or src.proof is None or dst.proof is None:
        proof = VolumeProof(VolumeMode.UNVERIFIED, src.proof, dst.proof, tuple(problems))
    else:
        proof = VolumeProof(volume_mode(src.proof, dst.proof), src.proof, dst.proof)
    return VolumeCheck(proof, released)


def _refusals(plan: Plan, proof: VolumeProof) -> tuple[PlanError, ...]:
    root = plan.destination_root
    if proof.mode is VolumeMode.CROSS:
        detail = "cross-volume copy is not enabled; files are moved within one volume only"
        return (PlanError(PlanErrorReason.CROSS_VOLUME, root, detail),)
    if proof.mode is VolumeMode.AMBIGUOUS:
        detail = "device and volume serial of source and destination disagree"
        return (PlanError(PlanErrorReason.VOLUME_AMBIGUOUS, root, detail),)
    return tuple(
        PlanError(PlanErrorReason.VOLUME_UNVERIFIED, p.path, f"{p.reason}: {p.detail}")
        for p in proof.problems
    )


def attach_volume_proof(plan: Plan, *, cross_copy: bool = False) -> Preview:
    """``plan`` with its volume proof. A plan with errors is returned unchanged (nothing
    to prove); unless the proof is ``SAME``, the plan is replaced by one with only
    errors (no items, no directories to create) and the proof, for display.

    ``cross_copy=True`` asks for a copy plan if the volumes are ``CROSS``: it is kept,
    marked ``cross_copy``, only while ``features.cross_volume_copy_enabled()``. A
    ``SAME`` plan stays a move plan either way."""
    if not plan.ok or plan.destination_root is None:
        return Preview(plan)
    check = probe_volumes(plan.source, plan.destination_root)
    proof = check.proof
    if proof.mode is VolumeMode.SAME:
        return Preview(replace(plan, volume=proof), check.release_errors)
    copy = cross_copy is True and features.cross_volume_copy_enabled()
    if proof.mode is VolumeMode.CROSS and copy and not check.release_errors:
        return Preview(replace(plan, volume=proof, cross_copy=True), check.release_errors)
    refused = Plan(
        source=plan.source,
        destination_root=plan.destination_root,
        explicit_destination=plan.explicit_destination,
        policy=plan.policy,
        errors=_refusals(plan, proof),
        volume=proof,
    )
    return Preview(refused, check.release_errors)


def preview_plan(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str] | None,
    table: ExtensionTable,
    selected: Iterable[str],
    policy: CollisionPolicy = CollisionPolicy.RENAME,
    *,
    cross_copy: bool = False,
) -> Preview:
    """``build_plan`` (unchanged), then ``attach_volume_proof``; changes nothing."""
    plan, inventory = plan_with_inventory(source, destination, table, selected, policy)
    return replace(attach_volume_proof(plan, cross_copy=cross_copy), inventory=inventory)


def _same_location(a: LocationProof | None, b: LocationProof | None) -> bool:
    if a is None or b is None:
        return False
    fields = ("path", "filesystem", "deepest", "deepest_id", "exists")
    return a.volume == b.volume and all(getattr(a, f) == getattr(b, f) for f in fields)


def check_plan_fresh(plan: Plan) -> FreshnessCheck:
    """Prove the volumes again; ``fresh`` only if they are exactly what the preview proved.

    Refused (never assumed fresh): no proof, a proof other than ``SAME`` (``CROSS`` for
    a ``cross_copy`` plan, and only while the copy is enabled), a location that cannot
    be proven now, or any difference in device, serial, filesystem, deepest existing
    directory, its id or whether the path exists.
    """
    stored = plan.volume
    if stored is None:
        detail = "the plan was not previewed with a volume proof"
        return FreshnessCheck(FreshnessReason.NO_VOLUME_PROOF, detail)
    expected = VolumeMode.CROSS if plan.cross_copy else VolumeMode.SAME
    if plan.cross_copy and not features.cross_volume_copy_enabled():
        detail = "cross-volume copy is not enabled"
        return FreshnessCheck(FreshnessReason.VOLUME_REFUSED, detail)
    if stored.mode is not expected or not plan.ok or plan.destination_root is None:
        return FreshnessCheck(FreshnessReason.VOLUME_REFUSED, f"volumes are {stored.mode}")
    check = probe_volumes(plan.source, plan.destination_root)
    current = check.proof
    if current.mode is not expected or check.release_errors:
        details = [f"{p.path}: {p.reason}: {p.detail}" for p in current.problems]
        detail = "; ".join(details) or f"volumes are {current.mode} now"
        return FreshnessCheck(FreshnessReason.PLAN_STALE, detail, current, check.release_errors)
    for name in ("source", "destination"):
        if not _same_location(getattr(stored, name), getattr(current, name)):
            detail = f"the {name} is not what the preview proved"
            return FreshnessCheck(FreshnessReason.PLAN_STALE, detail, current, check.release_errors)
    return FreshnessCheck(None, None, current, check.release_errors)


# --- display -------------------------------------------------------------------------

_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})


def display_text(text: str) -> str:
    """``text`` with control, format (bidi), separator, surrogate and unassigned
    characters shown as ``\\uXXXX``: a name cannot hide or reorder what is displayed."""
    return "".join(
        f"\\u{ord(c):04x}" if unicodedata.category(c) in _HIDDEN_CATEGORIES else c for c in text
    )


_MODE_TEXT = {
    VolumeMode.SAME: "Источник и назначение на одном томе: перемещение внутри тома.",
    VolumeMode.CROSS: (
        "Источник и назначение на разных томах. Копирование между томами отключено:"
        " план не будет выполнен."
    ),
    VolumeMode.AMBIGUOUS: "Тома не удалось однозначно сопоставить: план отклонён.",
    VolumeMode.UNVERIFIED: "Том не подтверждён: план отклонён.",
}


def _location_line(label: str, proof: LocationProof) -> str:
    where = display_text(str(proof.path))
    state = "" if proof.exists else f" (нет; ближайший: {display_text(str(proof.deepest))})"
    return (
        f"{label}: {where}{state} — {display_text(proof.filesystem)},"
        f" {display_text(proof.device)}, серийный номер {proof.volume_serial:#x}"
    )


def describe_volume(proof: VolumeProof | None) -> tuple[str, ...]:
    """Lines for the preview; every path and detail goes through ``display_text``."""
    if proof is None:
        return ("Том не проверялся: выполнение запрещено.",)
    lines = [_MODE_TEXT[proof.mode]]
    for label, location in (("Источник", proof.source), ("Назначение", proof.destination)):
        if location is not None:
            lines.append(_location_line(label, location))
    for problem in proof.problems:
        detail = f": {display_text(problem.detail)}" if problem.detail else ""
        lines.append(f"Отказ ({problem.reason}) {display_text(str(problem.path))}{detail}")
    return tuple(lines)
