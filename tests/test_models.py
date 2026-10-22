from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from downloads_organizer.models import (
    CollisionPolicy,
    FileInfo,
    Plan,
    PlanAction,
    PlanError,
    PlanErrorReason,
    PlanItem,
    SkipReason,
)

INFO = FileInfo(size=1, mtime_ns=2, attributes=0x20, file_id=None, device=None)
SOURCE = Path("C:/s")
ROOT = Path("C:/s/DEL")


def item(
    action: PlanAction | str,
    reason: SkipReason | str | None = None,
    *,
    target: Path = ROOT / "PDF" / "a.pdf",
    source: Path = SOURCE / "a.pdf",
    info: FileInfo = INFO,
) -> PlanItem:
    return PlanItem(source, target, "PDF", ".pdf", action, info, reason)  # type: ignore[arg-type]


def renamed() -> PlanItem:
    return item(PlanAction.RENAME, target=ROOT / "PDF" / "a_1.pdf")


def plan(**kwargs: object) -> Plan:
    args: dict[str, object] = {
        "source": SOURCE,
        "destination_root": ROOT,
        "explicit_destination": False,
        "policy": CollisionPolicy.RENAME,
    }
    args.update(kwargs)
    return Plan(**args)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        (PlanAction.SKIP, None),
        (PlanAction.MOVE, SkipReason.HIDDEN),
        (PlanAction.RENAME, SkipReason.TARGET_EXISTS),
    ],
)
def test_reason_is_required_exactly_for_skipped_items(
    action: PlanAction, reason: SkipReason | None
):
    with pytest.raises(ValueError, match="reason"):
        item(action, reason)


def test_plan_partitions_items_into_moves_and_skipped():
    move, rename = item(PlanAction.MOVE), renamed()
    skip = item(PlanAction.SKIP, SkipReason.HIDDEN)
    result = plan(items=(move, skip, rename))
    assert result.ok
    assert result.moves == (move, rename)
    assert result.skipped == (skip,)


def test_plan_with_errors_is_not_ok_and_immutable():
    result = plan(
        destination_root=None,
        explicit_destination=True,
        policy=CollisionPolicy.SKIP,
        errors=(PlanError(PlanErrorReason.NOTHING_SELECTED),),
    )
    assert not result.ok
    assert result.moves == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.items = (item(PlanAction.MOVE),)  # type: ignore[misc]


def test_enum_values_are_stable_strings():
    # Values may later end up in move logs and settings; renaming them is a format change.
    assert CollisionPolicy("rename") is CollisionPolicy.RENAME
    assert SkipReason.CLOUD_PLACEHOLDER == "cloud_placeholder"
    assert PlanErrorReason.SAME_DIRECTORY == "same_directory"
    assert SkipReason.METADATA_UNKNOWN == "metadata_unknown"
    assert SkipReason.TARGET_DIR_IDENTITY_UNKNOWN == "target_dir_identity_unknown"
    assert PlanErrorReason.DIRECTORY_IDENTITY_UNKNOWN == "directory_identity_unknown"


# --- invariants (R2 defect: contradictory items and plans could be constructed) -----


def test_string_enum_values_are_normalized():
    skip = item("skip", "hidden")
    assert skip.action is PlanAction.SKIP
    assert skip.reason is SkipReason.HIDDEN
    assert plan(policy="skip").policy is CollisionPolicy.SKIP
    assert PlanError("same_directory").reason is PlanErrorReason.SAME_DIRECTORY


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: item("delete"), id="action-unknown"),
        pytest.param(lambda: item(None), id="action-none"),  # type: ignore[arg-type]
        pytest.param(lambda: item(PlanAction.SKIP, "bad"), id="reason-unknown"),
        pytest.param(lambda: plan(policy="overwrite"), id="policy-unknown"),
        pytest.param(lambda: plan(policy=None), id="policy-none"),
        pytest.param(lambda: PlanError("bad"), id="error-reason-unknown"),
    ],
)
def test_invalid_enum_values_are_rejected(build):
    with pytest.raises(ValueError):
        build()


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: item(PlanAction.MOVE, source="C:/s/a.pdf"), id="source-str"),
        pytest.param(lambda: item(PlanAction.MOVE, info=None), id="info-none"),
        pytest.param(
            lambda: PlanItem(SOURCE / "a.pdf", ROOT / "PDF" / "a.pdf", 1, ".pdf", "move", INFO),
            id="category-int",
        ),
        pytest.param(
            lambda: FileInfo(size="1", mtime_ns=0, attributes=0, file_id=None, device=None),
            id="size-str",
        ),
        pytest.param(
            lambda: FileInfo(size=True, mtime_ns=0, attributes=0, file_id=None, device=None),
            id="size-bool",
        ),
        pytest.param(lambda: PlanError(PlanErrorReason.SAME_DIRECTORY, "C:/s"), id="path-str"),
        pytest.param(lambda: plan(source="C:/s"), id="plan-source-str"),
        pytest.param(lambda: plan(explicit_destination=1), id="explicit-int"),
        pytest.param(lambda: plan(items=[item(PlanAction.MOVE), "x"]), id="items-element"),
        pytest.param(lambda: plan(items="abc"), id="items-str"),
        pytest.param(lambda: plan(errors=[None]), id="errors-element"),
        pytest.param(lambda: plan(directories_to_create=["C:/s/DEL"]), id="dirs-element-str"),
        pytest.param(lambda: plan(items=5), id="items-not-iterable"),
    ],
)
def test_wrong_types_raise_type_error(build):
    with pytest.raises(TypeError):
        build()


def test_negative_size_is_rejected():
    with pytest.raises(ValueError, match="negative"):
        FileInfo(size=-1, mtime_ns=0, attributes=0, file_id=None, device=None)
    assert FileInfo(size=0, mtime_ns=-5, attributes=0, file_id=None, device=None).complete


@pytest.mark.parametrize("action", [PlanAction.MOVE, PlanAction.RENAME])
@pytest.mark.parametrize(
    "info",
    [
        FileInfo(size=None, mtime_ns=0, attributes=0, file_id=None, device=None),
        FileInfo(size=1, mtime_ns=None, attributes=0, file_id=None, device=None),
        FileInfo(size=1, mtime_ns=0, attributes=None, file_id=None, device=None),
    ],
)
def test_moves_need_a_complete_snapshot(action: PlanAction, info: FileInfo):
    target = ROOT / "PDF" / ("a.pdf" if action is PlanAction.MOVE else "a_1.pdf")
    with pytest.raises(ValueError, match="snapshot"):
        item(action, target=target, info=info)
    assert item(PlanAction.SKIP, SkipReason.METADATA_UNKNOWN, info=info).file is info


def test_move_keeps_the_name_and_rename_changes_it():
    with pytest.raises(ValueError, match="name"):
        item(PlanAction.MOVE, target=ROOT / "PDF" / "a_1.pdf")
    with pytest.raises(ValueError, match="name"):
        item(PlanAction.RENAME, target=ROOT / "PDF" / "a.pdf")


@pytest.mark.parametrize("target", [Path("C:/s/DEL/PDF/.."), Path("C:/")])
def test_target_must_name_a_file(target: Path):
    with pytest.raises(ValueError, match="name a file"):
        item(PlanAction.SKIP, SkipReason.HIDDEN, target=target)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"items": (item(PlanAction.MOVE),)}, id="move"),
        pytest.param({"items": (item(PlanAction.SKIP, SkipReason.HIDDEN),)}, id="skip"),
        pytest.param({"directories_to_create": (ROOT,)}, id="directories"),
    ],
)
def test_plan_with_errors_has_no_items_or_directories(kwargs: dict[str, object]):
    with pytest.raises(ValueError, match="errors"):
        plan(errors=(PlanError(PlanErrorReason.SAME_DIRECTORY, ROOT),), **kwargs)


def test_plan_without_root_has_no_items():
    with pytest.raises(ValueError, match="root"):
        plan(destination_root=None, items=(item(PlanAction.SKIP, SkipReason.HIDDEN),))


@pytest.mark.parametrize(
    ("source", "target"),
    [
        pytest.param(Path("C:/other/a.pdf"), ROOT / "PDF" / "a.pdf", id="source-elsewhere"),
        pytest.param(SOURCE / "sub" / "a.pdf", ROOT / "PDF" / "a.pdf", id="source-nested"),
        pytest.param(SOURCE / "a.pdf", Path("C:/outside/a.pdf"), id="target-outside"),
        pytest.param(SOURCE / "a.pdf", ROOT / "a.pdf", id="target-in-root"),
        pytest.param(SOURCE / "a.pdf", ROOT / "x" / "PDF" / "a.pdf", id="target-nested"),
        pytest.param(SOURCE / "a.pdf", ROOT / ".." / "a.pdf", id="target-dotdot"),
    ],
)
def test_plan_items_stay_between_source_and_category_folders(source: Path, target: Path):
    with pytest.raises(ValueError):
        plan(items=(item(PlanAction.MOVE, source=source, target=target),))


def test_directories_to_create_need_moves_and_stay_near_the_root():
    move = item(PlanAction.MOVE)
    assert plan(items=(move,), directories_to_create=(ROOT, ROOT / "PDF")).moves == (move,)
    with pytest.raises(ValueError, match="without planned moves"):
        plan(items=(item(PlanAction.SKIP, SkipReason.HIDDEN),), directories_to_create=(ROOT,))
    for outside in (Path("C:/elsewhere"), ROOT / "PDF" / "deeper"):
        with pytest.raises(ValueError, match="directly below"):
            plan(items=(move,), directories_to_create=(outside,))


def test_plan_copies_collections_passed_in():
    items = [item(PlanAction.MOVE)]
    directories = [ROOT / "PDF"]
    result = plan(items=items, directories_to_create=directories)
    items.append(renamed())
    directories.append(Path("C:/elsewhere"))
    assert isinstance(result.items, tuple)
    assert len(result.items) == 1
    assert result.directories_to_create == (ROOT / "PDF",)


def test_moves_of_an_inconsistent_plan_are_never_returned():
    # Bypassing the constructor checks still yields no moves next to errors.
    result = plan(items=(item(PlanAction.MOVE),))
    object.__setattr__(result, "errors", (PlanError(PlanErrorReason.SAME_DIRECTORY),))
    assert result.moves == ()
