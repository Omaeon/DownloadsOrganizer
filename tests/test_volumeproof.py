"""Read-only volume proof of the preview (R3B-1), on real temporary trees (``tmp_path`` only).

Nothing here moves, copies or deletes a file outside ``tmp_path``; the proof itself must
not change anything at all (``no_side_effects`` and a tree snapshot around it).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest

from downloads_organizer import volumeproof, winfileops
from downloads_organizer.executor import ExecReason, ItemStatus, execute_plan, validate_plan
from downloads_organizer.extensions import ExtensionTable
from downloads_organizer.models import (
    CollisionPolicy,
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
from downloads_organizer.planner import build_plan
from downloads_organizer.volumeproof import (
    FreshnessReason,
    LocationProbe,
    check_plan_fresh,
    describe_volume,
    display_text,
    preview_plan,
    probe_location,
    probe_volumes,
)

pytestmark = pytest.mark.windows_only

TABLE = ExtensionTable()
EXTS = frozenset({".pdf", ".jpg", ".mp3"})
V999 = "\\Device\\HarddiskVolume999"
NETWORK = "\\Device\\LanmanRedirector\\;Z:0000000000012345\\server\\share"
Snapshot = Callable[[Path], dict[str, tuple[object, ...]]]


# --- helpers (as in test_executor) -----------------------------------------------------


@pytest.fixture
def area(tmp_path: Path) -> Path:
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


def fake_mapping(
    monkeypatch: pytest.MonkeyPatch, drive: str, answers: dict[int, object]
) -> list[int]:
    """The n-th ``query_dos_device(drive)`` (counted from now) answers ``answers[n]``
    (raised if an exception), the real mapping otherwise; returns the calls so far.

    A ``probe_location`` reads the mapping three times: ``volume_root`` (1), after the
    root was opened (``prove_volume_root``, 2) and after the walk (3); ``probe_volumes``
    the source first (1-3), then the destination (4-6).
    """
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

    def wrapper(*args: object, **kwargs: object) -> object:
        nonlocal calls
        if not only(*args):
            return real(*args, **kwargs)
        calls += 1
        return replacement(*args, **kwargs) if calls == nth else real(*args, **kwargs)

    monkeypatch.setattr(winfileops, name, wrapper)


def is_anchor(handle: winfileops.Handle) -> bool:
    return str(handle.path) == handle.path.anchor


def raise_(exc: BaseException) -> Callable[..., object]:
    def fail(*args: object, **kwargs: object) -> object:
        raise exc

    return fail


def refused(probe: LocationProbe, reason: ProbeReason) -> str:
    assert probe.proof is None, probe
    assert probe.problem is not None and probe.problem.reason is reason, probe.problem
    assert probe.problem.path == probe.path
    return probe.problem.detail or ""


def preview(source: Path, destination: Path | None = None) -> Plan:
    result = preview_plan(source, destination, TABLE, EXTS)
    assert result.release_errors == ()
    return result.plan


def proof_of(path: Path) -> LocationProof:
    probe = probe_location(path)
    assert probe.problem is None and probe.proof is not None, probe.problem
    return probe.proof


# --- probe_location: what is proven -----------------------------------------------------


def test_existing_directory_is_proven_on_its_volume(src: Path, close_failures) -> None:
    proof = proof_of(src)
    stat = src.stat(follow_symlinks=False)
    assert proof.path == src and proof.exists and proof.deepest == src
    assert (proof.volume_serial, proof.deepest_id) == (stat.st_dev, stat.st_ino)
    assert proof.device == winfileops.drive_device(src.drive)
    assert proof.filesystem == "NTFS"
    assert close_failures.opened, "the proof is made through handles"
    close_failures.assert_each_closed_once()
    assert all(str(h.state) == "closed" for h in close_failures.opened)  # type: ignore[attr-defined]


def test_missing_directory_is_proven_by_its_nearest_existing_ancestor(src: Path) -> None:
    proof = proof_of(src / "DEL" / "Documents")
    assert not proof.exists and proof.deepest == src
    assert proof.deepest_id == src.stat(follow_symlinks=False).st_ino


def test_volume_root_itself(src: Path) -> None:
    root = Path(src.anchor)
    proof = proof_of(root)
    assert proof.exists and proof.deepest == root


def test_probe_opens_only_read_attributes(src: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, int, bool]] = []
    real = winfileops.open_relative

    def spy(parent, name, *, directory, access, share):  # type: ignore[no-untyped-def]
        seen.append((access, share, directory))
        return real(parent, name, directory=directory, access=access, share=share)

    monkeypatch.setattr(winfileops, "open_relative", spy)
    proof_of(src)
    assert seen and set(seen) == {(winfileops.FILE_READ_ATTRIBUTES, winfileops.SHARE_ALL, True)}


def test_probe_changes_nothing(src: Path, snapshot: Snapshot, no_side_effects) -> None:
    touch(src, "a.pdf")
    before = snapshot(src.parent)
    with no_side_effects():
        proof_of(src)
        proof_of(src / "DEL")
    assert snapshot(src.parent) == before


def test_relative_path_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="not a normalized absolute path"):
        probe_location(Path("relative"))
    with pytest.raises(ValueError):
        probe_location("C:\\Users")  # type: ignore[arg-type]


def test_unsupported_platform_is_refused(src: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winfileops, "AVAILABLE", False)
    refused(probe_location(src), ProbeReason.UNSUPPORTED_PLATFORM)
    plan = preview(src)
    assert [e.reason for e in plan.errors] == [PlanErrorReason.VOLUME_UNVERIFIED] * 2
    assert plan.items == () and plan.volume is not None
    assert plan.volume.mode is VolumeMode.UNVERIFIED


# --- reparse points and other objects in the path ----------------------------------------


def test_junction_in_the_path_is_refused(area: Path, make_junction, close_failures) -> None:
    (area / "real" / "inner").mkdir(parents=True)
    link = make_junction(area / "link", area / "real")
    for path in (link, link / "inner", link / "inner" / "missing"):
        detail = refused(probe_location(path), ProbeReason.REPARSE_IN_PATH)
        assert str(link) in detail
    close_failures.assert_each_closed_once()


def test_junction_as_the_destination_is_unverified(src: Path, area: Path, make_junction) -> None:
    (area / "elsewhere").mkdir()
    link = make_junction(area / "Sorted", area / "elsewhere")
    check = probe_volumes(src, link)
    assert check.proof.mode is VolumeMode.UNVERIFIED and check.proof.destination is None
    assert [p.reason for p in check.proof.problems] == [ProbeReason.REPARSE_IN_PATH]
    assert not preview(src, link).ok  # the planner refuses it already


def test_file_in_the_path_is_refused(src: Path) -> None:
    touch(src, "a.pdf")
    refused(probe_location(src / "a.pdf"), ProbeReason.NOT_A_DIRECTORY)
    refused(probe_location(src / "a.pdf" / "below"), ProbeReason.NOT_A_DIRECTORY)


def test_component_reported_as_a_file_is_refused(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = winfileops.query
    on_call(
        monkeypatch,
        "query",
        2,
        lambda h: dataclasses.replace(real(h), directory=False),
        only=lambda h: not is_anchor(h),
    )
    refused(probe_location(src), ProbeReason.NOT_A_DIRECTORY)


def test_reparse_attribute_without_a_link_is_refused(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = winfileops.query
    flag = winfileops.FILE_ATTRIBUTE_REPARSE_POINT
    on_call(
        monkeypatch,
        "query",
        1,
        lambda h: dataclasses.replace(real(h), attributes=real(h).attributes | flag),
        only=lambda h: not is_anchor(h),
    )
    refused(probe_location(src), ProbeReason.REPARSE_IN_PATH)


def test_component_on_another_volume_is_a_mounted_volume(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    real = winfileops.query

    def other_volume(handle: winfileops.Handle) -> winfileops.HandleInfo:
        info = real(handle)
        return dataclasses.replace(info, volume_serial=info.volume_serial ^ 0x5A5A)

    on_call(monkeypatch, "query", 2, other_volume, only=lambda h: not is_anchor(h))
    detail = refused(probe_location(src), ProbeReason.MOUNTED_VOLUME)
    assert "is on volume" in detail
    close_failures.assert_each_closed_once()


def test_non_normalized_path_is_a_programming_error(src: Path, close_failures) -> None:
    with pytest.raises(ValueError, match="not a normalized absolute path"):
        probe_location(Path(str(src) + "\\..\\Downloads"))
    assert close_failures.opened == []


# --- the drive letter: subst, network and changed mappings --------------------------------

MAPPINGS: dict[str, tuple[int, object, str]] = {
    # name: (nth query_dos_device call, answer, expected detail)
    "subst of a directory": (1, None, "not the drive letter of a disk volume"),
    "subst of the volume root": (1, None, "not the drive letter of a disk volume"),
    "network drive": (1, (NETWORK,), "not the drive letter of a disk volume"),
    "ambiguous": (1, None, "not the drive letter of a disk volume"),
    "undefined": (1, FileNotFoundError(2, "not found"), "cannot read what"),
    "unreadable": (1, PermissionError(13, "Access is denied"), "cannot read what"),
    "another volume": (1, (V999,), "not the root of"),
    "subst while the root was opened": (2, None, "not the drive letter of a disk volume"),
    "another volume while the root was opened": (2, (V999,), "was redefined"),
    "subst during the walk": (3, None, "not the drive letter of a disk volume"),
    "another volume during the walk": (3, (V999,), "was redefined"),
}


@pytest.mark.parametrize("case", MAPPINGS)
def test_unproven_drive_letter_is_refused(
    case: str, src: Path, area: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    nth, answer, expected = MAPPINGS[case]
    device = winfileops.drive_device(src.drive)
    if answer is None:
        answer = {
            "subst of the volume root": ("\\??\\" + src.drive,),
            "ambiguous": (device, V999),
        }.get(case, ("\\??\\" + str(area),))
    calls = fake_mapping(monkeypatch, src.drive, {nth: answer})
    detail = refused(probe_location(src), ProbeReason.ROOT_UNVERIFIED)
    assert expected in detail
    assert len(calls) == nth  # refused at that read, nothing proven after it
    close_failures.assert_each_closed_once()


def test_real_subst_drive_is_refused(src: Path, area: Path, subst_drives) -> None:
    """Opt-in (``DOWNLOADSORGANIZER_REAL_SUBST=1``): a real ``subst`` of ``tmp_path``."""
    touch(src, "a.pdf")
    (letter,) = subst_drives.free(1)
    subst_drives.map(letter, str(area))
    mapped = Path(letter + "\\") / "Downloads"
    detail = refused(probe_location(mapped), ProbeReason.ROOT_UNVERIFIED)
    assert "not the drive letter of a disk volume" in detail
    plan = preview(mapped)
    assert not plan.ok and plan.volume is not None
    assert plan.volume.mode is VolumeMode.UNVERIFIED
    assert (src / "a.pdf").is_file()


def test_unc_path_is_refused() -> None:
    probe = probe_location(Path("\\\\server\\share\\Downloads"))
    assert "only a directory on a local drive letter" in refused(probe, ProbeReason.ROOT_UNVERIFIED)


# --- the filesystem -----------------------------------------------------------------------

FILESYSTEMS = {
    "FAT32": winfileops.VolumeInfo("FAT32", winfileops.FILE_DEVICE_DISK, 0),
    "exFAT": winfileops.VolumeInfo("exFAT", winfileops.FILE_DEVICE_DISK, 0),
    "ReFS": winfileops.VolumeInfo("ReFS", winfileops.FILE_DEVICE_DISK, 0),
    "remote NTFS": winfileops.VolumeInfo("NTFS", winfileops.FILE_DEVICE_DISK, 0x10),
    "removable NTFS": winfileops.VolumeInfo("NTFS", winfileops.FILE_DEVICE_DISK, 0x01),
    "CD-ROM": winfileops.VolumeInfo("CDFS", 0x02, 0),
    "virtual device NTFS": winfileops.VolumeInfo("NTFS", 0x08, 0),
}


@pytest.mark.parametrize("name", FILESYSTEMS)
def test_unsupported_filesystem_is_refused(
    name: str, src: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    info = FILESYSTEMS[name]
    on_call(monkeypatch, "query_volume", 1, lambda handle: info, only=is_anchor)
    detail = refused(probe_location(src), ProbeReason.ROOT_UNVERIFIED)
    assert "only a local NTFS disk is supported" in detail
    close_failures.assert_each_closed_once()


def test_filesystem_recorded_is_the_proven_one(src: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fat = FILESYSTEMS["FAT32"]
    on_call(monkeypatch, "query_volume", 2, lambda handle: fat, only=is_anchor)
    assert "only NTFS is supported" in refused(probe_location(src), ProbeReason.ROOT_UNVERIFIED)


def test_root_that_is_not_the_volume_root_is_refused(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = winfileops.query
    on_call(
        monkeypatch,
        "query",
        1,
        lambda h: dataclasses.replace(real(h), file_id=real(h).file_id + 1),
        only=is_anchor,
    )
    detail = refused(probe_location(src), ProbeReason.ROOT_UNVERIFIED)
    assert "is not the root directory of its volume" in detail


# --- open and query failures: refused, every handle released once -------------------------


def test_root_cannot_be_opened(src: Path, monkeypatch: pytest.MonkeyPatch, close_failures) -> None:
    on_call(monkeypatch, "open_anchor", 1, raise_(PermissionError(13, "Access is denied")))
    assert "cannot open" in refused(probe_location(src), ProbeReason.ROOT_UNVERIFIED)
    assert close_failures.opened == []


def test_root_cannot_be_queried(src: Path, monkeypatch: pytest.MonkeyPatch, close_failures) -> None:
    on_call(monkeypatch, "query", 2, raise_(OSError(5, "query failed")), only=is_anchor)
    assert "cannot query" in refused(probe_location(src), ProbeReason.ROOT_UNVERIFIED)
    close_failures.assert_each_closed_once()


@pytest.mark.parametrize("nth", [1, 2, 3])
def test_component_cannot_be_opened(
    nth: int, src: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    on_call(monkeypatch, "open_relative", nth, raise_(PermissionError(5, "Access is denied")))
    assert "cannot open" in refused(probe_location(src), ProbeReason.OPEN_FAILED)
    assert len(close_failures.opened) == nth  # the anchor and nth - 1 components
    close_failures.assert_each_closed_once()


def test_component_cannot_be_queried(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    on_call(
        monkeypatch, "query", 2, raise_(OSError(5, "query failed")), only=lambda h: not is_anchor(h)
    )
    assert "cannot query" in refused(probe_location(src), ProbeReason.OPEN_FAILED)
    close_failures.assert_each_closed_once()


def test_sharing_violation_is_open_failed_not_missing(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = OSError(0, "used by another process", str(src), winfileops.ERROR_SHARING_VIOLATION)
    on_call(monkeypatch, "open_relative", 1, raise_(error))
    refused(probe_location(src / "DEL"), ProbeReason.OPEN_FAILED)


# --- release failures: refused, the error objects kept ------------------------------------


def test_failed_release_refuses_the_proof(src: Path, close_failures) -> None:
    close_failures.select = lambda h: not is_anchor(h)
    probe = probe_location(src)
    detail = refused(probe, ProbeReason.RELEASE_FAILED)
    assert "CloseHandle failed" in detail
    (error,) = probe.release_errors
    assert isinstance(error, winfileops.HandleCloseError)
    assert error.handle_value == close_failures.failed[0]
    close_failures.assert_each_closed_once()  # the others released, none retried


def test_all_releases_fail_and_all_are_kept(src: Path, close_failures) -> None:
    close_failures.select = lambda h: True
    close_failures.limit = 100
    probe = probe_location(src)
    refused(probe, ProbeReason.RELEASE_FAILED)
    assert len(probe.release_errors) == len(close_failures.opened) == len(close_failures.failed)
    assert [e.handle_value for e in probe.release_errors] == close_failures.failed  # type: ignore[attr-defined]
    close_failures.assert_each_closed_once()


def test_failed_release_after_a_refusal_keeps_the_reason(
    area: Path, make_junction, close_failures
) -> None:
    (area / "real").mkdir()
    link = make_junction(area / "link", area / "real")
    close_failures.select = is_anchor
    probe = probe_location(link)
    detail = refused(probe, ProbeReason.REPARSE_IN_PATH)
    assert "then releasing failed" in detail and "CloseHandle failed" in detail
    assert len(probe.release_errors) == 1


def test_failed_release_refuses_the_plan(src: Path, close_failures) -> None:
    touch(src, "a.pdf")
    close_failures.select = is_anchor
    result = preview_plan(src, None, TABLE, EXTS)
    assert not result.plan.ok and result.plan.items == ()
    assert len(result.release_errors) == 1
    assert [e.reason for e in result.plan.errors] == [PlanErrorReason.VOLUME_UNVERIFIED]
    assert "release_failed" in (result.plan.errors[0].detail or "")


# --- interrupts and programming errors: raised after every handle was released ------------


def test_interrupt_in_the_walk_is_raised_after_releasing(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    stop = KeyboardInterrupt()
    on_call(monkeypatch, "open_relative", 2, raise_(stop))
    with pytest.raises(KeyboardInterrupt) as raised:
        probe_location(src)
    assert raised.value is stop
    close_failures.assert_each_closed_once()


def test_interrupt_in_the_walk_carries_failed_releases(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    stop = KeyboardInterrupt()
    on_call(monkeypatch, "open_relative", 2, raise_(stop))
    close_failures.select = is_anchor
    with pytest.raises(KeyboardInterrupt) as raised:
        probe_location(src)
    assert raised.value is stop
    (error,) = winfileops.release_failures(stop)
    assert isinstance(error, winfileops.HandleCloseError)
    close_failures.assert_each_closed_once()


def test_interrupt_of_a_release_is_raised_with_the_other_failures(
    src: Path, close_failures
) -> None:
    stop = SystemExit(3)
    close_failures.interrupt = stop
    close_failures.select = lambda h: True  # the newest is interrupted, the next one fails
    with pytest.raises(SystemExit) as raised:
        probe_location(src)
    assert raised.value is stop
    failures = winfileops.release_failures(stop)
    assert [type(e) for e in failures] == [winfileops.HandleCloseError]
    close_failures.assert_each_closed_once()


def test_programming_error_is_raised_after_releasing(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    bug = RuntimeError("bug")
    on_call(monkeypatch, "query", 2, raise_(bug), only=lambda h: not is_anchor(h))
    with pytest.raises(RuntimeError) as raised:
        probe_location(src)
    assert raised.value is bug
    close_failures.assert_each_closed_once()


def test_programming_error_and_failed_release_raise_both(
    src: Path, monkeypatch: pytest.MonkeyPatch, close_failures
) -> None:
    bug = RuntimeError("bug")
    on_call(monkeypatch, "query", 2, raise_(bug), only=lambda h: not is_anchor(h))
    close_failures.select = is_anchor
    with pytest.raises(winfileops.ReleaseError) as raised:
        probe_location(src)
    assert raised.value.error is bug and raised.value.__cause__ is bug
    assert [type(e) for e in winfileops.release_failures(raised.value)] == [
        winfileops.HandleCloseError
    ]


# --- path races between the handles and lstat ------------------------------------------


def race_after_walk(monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]) -> None:
    """``change`` runs while every handle of the walk is still open (at its last
    mapping read), before the handles are released and lstat checks the path."""
    real = winfileops.check_mapping
    calls = 0

    def check(root: winfileops.VolumeRoot) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:  # 1: prove_volume_root, 2: the end of the walk
            change()
        real(root)

    monkeypatch.setattr(winfileops, "check_mapping", check)


def test_directory_replaced_during_the_preview(
    src: Path, area: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def replace_directory() -> None:
        src.rename(area / "old")  # allowed: the walk shares delete
        src.mkdir()

    race_after_walk(monkeypatch, replace_directory)
    detail = refused(probe_location(src), ProbeReason.PATH_CHANGED_DURING_PREVIEW)
    assert "not the directory the handles saw" in detail


def test_directory_removed_during_the_preview(
    src: Path, area: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    race_after_walk(monkeypatch, lambda: src.rename(area / "gone"))
    refused(probe_location(src), ProbeReason.PATH_CHANGED_DURING_PREVIEW)


def test_missing_directory_created_during_the_preview(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    race_after_walk(monkeypatch, lambda: (src / "DEL").mkdir())
    detail = refused(probe_location(src / "DEL"), ProbeReason.PATH_CHANGED_DURING_PREVIEW)
    assert "deepest existing directory" in detail


def test_junction_swapped_in_during_the_preview(
    src: Path, area: Path, monkeypatch: pytest.MonkeyPatch, make_junction
) -> None:
    (area / "elsewhere").mkdir()

    def swap() -> None:
        src.rename(area / "old")
        make_junction(src, area / "elsewhere")

    race_after_walk(monkeypatch, swap)
    refused(probe_location(src), ProbeReason.PATH_CHANGED_DURING_PREVIEW)


def test_source_missing_is_refused(src: Path) -> None:
    check = probe_volumes(src / "missing", src / "DEL")
    assert check.proof.mode is VolumeMode.UNVERIFIED
    assert [p.reason for p in check.proof.problems] == [ProbeReason.PATH_CHANGED_DURING_PREVIEW]


# --- probe_volumes and the plan -----------------------------------------------------------


def test_same_volume_plan_keeps_its_items_and_carries_the_proof(
    src: Path, snapshot: Snapshot, no_side_effects
) -> None:
    touch(src, "a.pdf", "b.jpg", "c.txt")
    before = snapshot(src.parent)
    with no_side_effects():
        result = preview_plan(src, None, TABLE, EXTS)
    assert snapshot(src.parent) == before  # nothing created: no DEL, no journal
    assert not (src / "DEL").exists()
    plain = build_plan(src, None, TABLE, EXTS)
    plan = result.plan
    assert plan.ok and plan.volume is not None and plan.volume.mode is VolumeMode.SAME
    assert dataclasses.replace(plan, volume=None) == plain
    assert plan.volume.source is not None and plan.volume.source.path == src
    assert plan.volume.destination is not None and not plan.volume.destination.exists
    assert validate_plan(plan, TABLE) == ()


def test_explicit_destination_on_the_same_volume(src: Path, area: Path) -> None:
    touch(src, "a.pdf")
    (area / "Sorted").mkdir()
    plan = preview(src, area / "Sorted")
    assert plan.ok and plan.volume is not None and plan.volume.mode is VolumeMode.SAME
    assert plan.volume.destination is not None and plan.volume.destination.exists


def test_plan_with_errors_is_not_probed(src: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(volumeproof, "probe_location", raise_(AssertionError("probed")))
    plan = preview(src / "missing")
    assert not plan.ok and plan.volume is None


def fake_destination(
    monkeypatch: pytest.MonkeyPatch, **changes: object
) -> Callable[[Path], LocationProbe]:
    """``probe_location`` of the destination (the second call) proves ``changes``."""
    real = volumeproof.probe_location
    calls = 0

    def probe(path: Path) -> LocationProbe:
        nonlocal calls
        calls += 1
        result = real(path)
        if calls % 2 == 1 or result.proof is None:
            return result
        return LocationProbe(path, dataclasses.replace(result.proof, **changes), None)

    monkeypatch.setattr(volumeproof, "probe_location", probe)
    return probe


@pytest.mark.parametrize(
    ("changes", "mode", "reason"),
    [
        ({"device": V999, "volume_serial": 0x1234}, VolumeMode.CROSS, PlanErrorReason.CROSS_VOLUME),
        ({"device": V999}, VolumeMode.AMBIGUOUS, PlanErrorReason.VOLUME_AMBIGUOUS),
        ({"volume_serial": 0x1234}, VolumeMode.AMBIGUOUS, PlanErrorReason.VOLUME_AMBIGUOUS),
    ],
)
def test_other_volume_refuses_the_plan(
    changes: dict[str, object],
    mode: VolumeMode,
    reason: PlanErrorReason,
    src: Path,
    area: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: Snapshot,
) -> None:
    touch(src, "a.pdf", "b.jpg")
    before = snapshot(area)
    fake_destination(monkeypatch, **changes)
    plan = preview(src)
    assert plan.volume is not None and plan.volume.mode is mode
    assert [e.reason for e in plan.errors] == [reason]
    assert plan.items == () and plan.moves == () and plan.directories_to_create == ()
    assert validate_plan(plan, TABLE) == ("the plan has errors",)
    result = execute_plan(plan, TABLE)
    assert result.stopped is ExecReason.PLAN_INVALID and result.items == ()
    assert not result.changed_anything
    assert snapshot(area) == before


def test_cross_volume_says_copy_is_not_enabled(src: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    touch(src, "a.pdf")
    fake_destination(monkeypatch, device=V999, volume_serial=1)
    (error,) = preview(src).errors
    assert error.path == src / "DEL" and "cross-volume copy is not enabled" in (error.detail or "")


def test_device_compared_case_insensitively(src: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    touch(src, "a.pdf")
    device = winfileops.drive_device(src.drive)
    fake_destination(monkeypatch, device=device.lower())
    plan = preview(src)
    assert plan.ok and plan.volume is not None and plan.volume.mode is VolumeMode.SAME


# --- check_plan_fresh: PLAN_STALE ---------------------------------------------------------


def test_fresh_plan(src: Path, snapshot: Snapshot, no_side_effects) -> None:
    touch(src, "a.pdf")
    plan = preview(src)
    before = snapshot(src.parent)
    with no_side_effects():
        check = check_plan_fresh(plan)
    assert snapshot(src.parent) == before
    assert check.fresh and check.reason is None and check.release_errors == ()
    assert check.current == plan.volume


def test_plan_without_a_proof_is_never_fresh(src: Path) -> None:
    touch(src, "a.pdf")
    check = check_plan_fresh(build_plan(src, None, TABLE, EXTS))
    assert not check.fresh and check.reason is FreshnessReason.NO_VOLUME_PROOF


def test_refused_plan_is_never_fresh(src: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    touch(src, "a.pdf")
    fake_destination(monkeypatch, device=V999, volume_serial=1)
    plan = preview(src)
    monkeypatch.undo()
    check = check_plan_fresh(plan)
    assert not check.fresh and check.reason is FreshnessReason.VOLUME_REFUSED


def stale(plan: Plan) -> str:
    check = check_plan_fresh(plan)
    assert not check.fresh and check.reason is FreshnessReason.PLAN_STALE, check
    return check.detail or ""


def test_destination_created_after_the_preview_is_stale(src: Path) -> None:
    touch(src, "a.pdf")
    plan = preview(src)
    (src / "DEL").mkdir()
    assert "destination is not what the preview proved" in stale(plan)


def test_source_replaced_after_the_preview_is_stale(src: Path, area: Path) -> None:
    touch(src, "a.pdf")
    plan = preview(src)
    src.rename(area / "old")
    touch(src, "a.pdf")
    assert "source is not what the preview proved" in stale(plan)


def test_destination_replaced_after_the_preview_is_stale(src: Path, area: Path) -> None:
    touch(src, "a.pdf")
    (area / "Sorted").mkdir()
    plan = preview(src, area / "Sorted")
    (area / "Sorted").rename(area / "old")
    (area / "Sorted").mkdir()
    assert "destination is not what the preview proved" in stale(plan)


def test_destination_became_a_junction_after_the_preview_is_stale(
    src: Path, area: Path, make_junction
) -> None:
    touch(src, "a.pdf")
    (area / "Sorted").mkdir()
    (area / "elsewhere").mkdir()
    plan = preview(src, area / "Sorted")
    (area / "Sorted").rename(area / "old")
    make_junction(area / "Sorted", area / "elsewhere")
    assert "reparse_in_path" in stale(plan)


def test_drive_letter_remapped_after_the_preview_is_stale(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(src, "a.pdf")
    plan = preview(src)
    fake_mapping(monkeypatch, src.drive, {1: (V999,)})
    assert "root_unverified" in stale(plan)


def test_other_volume_after_the_preview_is_stale(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(src, "a.pdf")
    plan = preview(src)
    fake_destination(monkeypatch, device=V999, volume_serial=1)
    assert "volumes are cross now" in stale(plan)


@pytest.mark.parametrize(
    "changes",
    [
        {"device": V999},
        {"volume_serial": 1},
        {"filesystem": "ReFS"},
        {"deepest_id": 1},
    ],
)
def test_any_difference_from_the_stored_proof_is_stale(
    changes: dict[str, object], src: Path
) -> None:
    touch(src, "a.pdf")
    plan = preview(src)
    assert plan.volume is not None and plan.volume.source and plan.volume.destination
    source = dataclasses.replace(plan.volume.source, **changes)
    destination = dataclasses.replace(plan.volume.destination, **changes)
    forged = dataclasses.replace(plan.volume, source=source, destination=destination)
    assert "source is not what the preview proved" in stale(
        dataclasses.replace(plan, volume=forged)
    )


def test_failed_release_while_checking_is_stale(src: Path, close_failures) -> None:
    touch(src, "a.pdf")
    plan = preview(src)
    close_failures.select = is_anchor
    check = check_plan_fresh(plan)
    assert check.reason is FreshnessReason.PLAN_STALE and "release_failed" in (check.detail or "")
    assert len(check.release_errors) == 1


# --- R3A is unchanged ---------------------------------------------------------------------


def test_previewed_plan_executes_as_before(src: Path) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = preview(src)
    result = execute_plan(plan, TABLE)
    assert result.complete, result
    assert [r.status for r in result.items] == [ItemStatus.MOVED] * 2
    assert sorted(p.name for p in (src / "DEL").rglob("*") if p.is_file()) == ["a.pdf", "b.jpg"]


def test_executor_ignores_the_proof_and_proves_the_volume_again(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(src, "a.pdf")
    plan = preview(src)
    fake_mapping(monkeypatch, src.drive, {1: (V999,)})
    result = execute_plan(plan, TABLE)
    assert not result.complete and not result.changed_anything, result
    assert (src / "a.pdf").is_file() and not (src / "DEL").exists()


# --- model invariants ---------------------------------------------------------------------


def location(path: Path, **changes: object) -> LocationProof:
    values: dict[str, object] = {
        "path": path,
        "device": "\\Device\\HarddiskVolume3",
        "volume_serial": 7,
        "filesystem": "NTFS",
        "deepest": path,
        "deepest_id": 11,
        "exists": True,
    }
    values.update(changes)
    return LocationProof(**values)  # type: ignore[arg-type]


ROOT = Path("C:\\x\\Downloads")


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"deepest": Path("C:\\elsewhere")}, ValueError),
        ({"exists": False}, ValueError),
        ({"deepest": ROOT.parent}, ValueError),  # exists, but the deepest is a parent
        ({"volume_serial": -1}, TypeError),
        ({"deepest_id": True}, TypeError),
        ({"device": None}, TypeError),
        ({"path": "C:\\x"}, TypeError),
    ],
)
def test_location_proof_refuses_contradictions(changes: dict[str, object], error: type) -> None:
    with pytest.raises(error):
        location(ROOT, **changes)


def test_missing_location_proof() -> None:
    proof = location(ROOT, deepest=ROOT.parent, exists=False)
    assert proof.volume == ("\\DEVICE\\HARDDISKVOLUME3", 7)


def test_volume_mode() -> None:
    a = location(ROOT)
    assert volume_mode(a, location(ROOT, device="\\device\\harddiskvolume3")) is VolumeMode.SAME
    assert volume_mode(a, location(ROOT, device=V999, volume_serial=8)) is VolumeMode.CROSS
    assert volume_mode(a, location(ROOT, device=V999)) is VolumeMode.AMBIGUOUS
    assert volume_mode(a, location(ROOT, volume_serial=8)) is VolumeMode.AMBIGUOUS


def test_volume_proof_refuses_contradictions() -> None:
    a, b = location(ROOT), location(ROOT / "DEL", deepest=ROOT, exists=False)
    problem = LocationProblem(ROOT, ProbeReason.OPEN_FAILED, "x")
    with pytest.raises(ValueError, match="needs a problem"):
        VolumeProof(VolumeMode.UNVERIFIED, a, b)
    with pytest.raises(ValueError, match="needs both proofs"):
        VolumeProof(VolumeMode.SAME, a, None)
    with pytest.raises(ValueError, match="needs both proofs"):
        VolumeProof(VolumeMode.SAME, a, b, (problem,))
    with pytest.raises(ValueError, match="does not match"):
        VolumeProof(VolumeMode.CROSS, a, b)
    with pytest.raises(ValueError):
        VolumeProof("guess", a, b)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        VolumeProof(VolumeMode.UNVERIFIED, None, None, ("problem",))  # type: ignore[arg-type]
    assert VolumeProof(VolumeMode.UNVERIFIED, a, None, [problem]).problems == (problem,)


def test_plan_refuses_a_proof_that_does_not_belong_to_it() -> None:
    destination = ROOT / "DEL"
    a, b = location(ROOT), location(destination, deepest=ROOT, exists=False)
    same = VolumeProof(VolumeMode.SAME, a, b)
    ok = Plan(ROOT, destination, False, CollisionPolicy.RENAME, volume=same)
    assert ok.volume is same
    error = (PlanError(PlanErrorReason.CROSS_VOLUME, destination, "x"),)
    cross = VolumeProof(VolumeMode.CROSS, a, location(destination, device=V999, volume_serial=1))
    assert Plan(ROOT, destination, False, CollisionPolicy.RENAME, errors=error, volume=cross).volume
    with pytest.raises(ValueError, match="errors expected"):
        Plan(ROOT, destination, False, CollisionPolicy.RENAME, volume=cross)
    with pytest.raises(ValueError, match="destination root"):
        Plan(ROOT, None, False, CollisionPolicy.RENAME, errors=error, volume=same)
    with pytest.raises(ValueError):
        Plan(ROOT / "other", destination, True, CollisionPolicy.RENAME, volume=same)
    with pytest.raises(ValueError):
        Plan(ROOT, ROOT / "elsewhere", True, CollisionPolicy.RENAME, volume=same)
    foreign = VolumeProof(
        VolumeMode.UNVERIFIED,
        None,
        None,
        (LocationProblem(Path("C:\\y"), ProbeReason.OPEN_FAILED),),
    )
    with pytest.raises(ValueError):
        Plan(ROOT, destination, False, CollisionPolicy.RENAME, errors=error, volume=foreign)
    with pytest.raises(TypeError):
        Plan(ROOT, destination, False, CollisionPolicy.RENAME, volume="SAME")  # type: ignore[arg-type]


# --- display ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("Downloads", "Downloads"),
        ("Загрузки", "Загрузки"),
        ("evil\u202etxt.exe", "evil\\u202etxt.exe"),
        ("a\nb\tc", "a\\u000ab\\u0009c"),
        ("zero\u200bwidth", "zero\\u200bwidth"),
        ("line\u2028sep", "line\\u2028sep"),
        ("lone\udc80", "lone\\udc80"),
    ],
)
def test_display_text_shows_hidden_characters(text: str, shown: str) -> None:
    assert display_text(text) == shown


def test_describe_without_a_proof() -> None:
    assert describe_volume(None) == ("Том не проверялся: выполнение запрещено.",)


def test_describe_same_volume(src: Path) -> None:
    plan = preview(src)
    lines = describe_volume(plan.volume)
    assert lines[0].startswith("Источник и назначение на одном томе")
    assert lines[1].startswith(f"Источник: {src} — NTFS")
    assert lines[2].startswith(f"Назначение: {src / 'DEL'} (нет; ближайший: {src})")
    assert len(lines) == 3


def test_describe_refusal_escapes_names() -> None:
    path = Path("C:\\x\\evil\u202e")
    problem = LocationProblem(path, ProbeReason.REPARSE_IN_PATH, "link\u202e\n")
    lines = describe_volume(VolumeProof(VolumeMode.UNVERIFIED, None, None, (problem,)))
    assert lines == (
        "Том не подтверждён: план отклонён.",
        "Отказ (reparse_in_path) C:\\x\\evil\\u202e: link\\u202e\\u000a",
    )
    assert all("\u202e" not in line and "\n" not in line for line in lines)


def test_describe_cross_volume_says_it_will_not_run() -> None:
    a = location(ROOT)
    b = location(ROOT / "DEL", deepest=ROOT, exists=False, device=V999, volume_serial=1)
    (first, *_) = describe_volume(VolumeProof(VolumeMode.CROSS, a, b))
    assert "Копирование между томами отключено" in first


def test_module_has_no_write_primitives() -> None:
    """The proof uses no call that creates, writes, renames or deletes."""
    text = Path(volumeproof.__file__).read_text(encoding="utf-8")
    for name in ("create_file", "rename_into", "open_stream", "mkdir", "unlink", "rmdir"):
        assert name not in text, name
    assert "os." not in text.replace("os.PathLike", "")
