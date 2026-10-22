"""``runner.run_plan`` (R3B-5): the mode, the barriers, the freshness check and how a copy
run reacts to what ``copy_item`` reports. ``copy_item`` itself is scripted here (no file
is copied); the preview, the volume roots and the directory walk are real, on
``tmp_path`` only. The cross-volume plans are made with a faked destination proof
(``fake_destination``: another device and serial), so the run's own root checks are
pointed at the one real volume through ``runner._expected_volumes``.

The real copy is in ``test_runner_ntfs``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from test_executor import TABLE, forge, touch
from test_volumeproof import EXTS, V999, fake_destination

from downloads_organizer import executor, features, runner, volumeproof, winfileops
from downloads_organizer.copycommit import CopyResult, CopyStatus, Reason, Stop, TransferState
from downloads_organizer.executor import ExecReason, ExecutionResult
from downloads_organizer.journal import Journal, JournalError, read_journal
from downloads_organizer.journal_v2 import (
    Commit,
    JournalReportV2,
    JournalV2,
    StatusV2,
    TempState,
    VolumeRef,
)
from downloads_organizer.journal_v3 import read_any_journal
from downloads_organizer.models import LocationProblem, Plan, ProbeReason, VolumeMode
from downloads_organizer.runner import (
    ACL_REFUSED,
    ACL_UNDECIDED_DETAIL,
    CopyOptions,
    CopyRunResult,
    RunRefusal,
    RunRefusalReason,
    UndecidedAcl,
    run_plan,
)
from downloads_organizer.volumeproof import LocationProbe, preview_plan
from downloads_organizer.winfileops import HandleCloseError

pytestmark = pytest.mark.windows_only

Snapshot = Callable[[Path], dict[str, tuple[object, ...]]]


# --- stands ----------------------------------------------------------------------------


@pytest.fixture
def src(tmp_path: Path) -> Path:
    path = tmp_path / "area" / "Downloads"
    path.mkdir(parents=True)
    return path


class Permit:
    """A test-only ACL gate that allows every copy, or refuses chosen files."""

    def __init__(self, refuse: frozenset[str] = frozenset()) -> None:
        self.refuse = refuse
        self.items: list[str] = []

    def check_run(self, plan: Plan) -> str | None:
        return None

    def check_item(self, source: Path, target: Path) -> str | None:
        self.items.append(source.name)
        return "scripted: not allowed" if source.name in self.refuse else None


@dataclass
class Journals:
    """Counts every journal created, of either version."""

    v1: list[Journal] = field(default_factory=list)
    v2: list[JournalV2] = field(default_factory=list)


@pytest.fixture
def journals(monkeypatch: pytest.MonkeyPatch) -> Journals:
    seen = Journals()
    real_v1, real_v2 = Journal.create.__func__, JournalV2.create.__func__  # type: ignore[attr-defined]

    def v1(cls: type[Journal], directory: Path | None = None) -> Journal:
        seen.v1.append(journal := real_v1(cls, directory))
        return journal

    def v2(cls: type[JournalV2], directory: Path | None = None) -> JournalV2:
        seen.v2.append(journal := real_v2(cls, directory))
        file = journal._file  # created by ``Journal.create``: the v2 file, not a v1 journal
        seen.v1[:] = [j for j in seen.v1 if j is not file]
        return journal

    monkeypatch.setattr(Journal, "create", classmethod(v1))
    monkeypatch.setattr(JournalV2, "create", classmethod(v2))
    return seen


def cross_plan(monkeypatch: pytest.MonkeyPatch, src: Path) -> Plan:
    """A copy plan: the flag on, the destination proven on another volume (faked)."""
    monkeypatch.setattr(features, "CROSS_VOLUME_COPY", True)
    fake_destination(monkeypatch, device=V999, volume_serial=0x1234)
    preview = preview_plan(src, None, TABLE, EXTS, cross_copy=True)
    plan = preview.plan
    assert plan.ok and plan.cross_copy, plan.errors
    assert plan.volume is not None and plan.volume.mode is VolumeMode.CROSS
    assert plan.volume.source is not None
    real = VolumeRef.of(plan.volume.source)
    monkeypatch.setattr(runner, "_expected_volumes", lambda _plan: (real, real))
    return plan


def same_plan(src: Path) -> Plan:
    plan = preview_plan(src, None, TABLE, EXTS).plan
    assert plan.ok and plan.volume is not None and plan.volume.mode is VolumeMode.SAME
    return plan


def unverified_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    """The destination (every second ``probe_location``) cannot be proven."""
    real = volumeproof.probe_location
    calls = 0

    def probe(path: Path) -> LocationProbe:
        nonlocal calls
        calls += 1
        if calls % 2 == 1:
            return real(path)
        problem = LocationProblem(path, ProbeReason.ROOT_UNVERIFIED, "scripted")
        return LocationProbe(path, None, problem)

    monkeypatch.setattr(volumeproof, "probe_location", probe)


Make = Callable[[Path, Path], CopyResult]


class Script:
    """Stands in for ``copy_item``: closes ``src`` (C17) and returns scripted results."""

    def __init__(self, *results: Make | BaseException) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def __call__(self, journal: JournalV2, *, source: Path, target: Path, src, **_: object):  # type: ignore[no-untyped-def]
        self.calls.append(source.name)
        src.close()
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result(source, target)


def result(
    status: CopyStatus,
    reason: str | None = None,
    *,
    stop: Stop | None = None,
    commit: Commit = Commit.NOT_COMMITTED,
    temp: TempState = TempState.REMOVED,
    terminal: StatusV2 | None = StatusV2.FAILED,
    cleanup: tuple[BaseException, ...] = (),
    journal_error: JournalError | None = None,
) -> Make:
    def make(source: Path, target: Path) -> CopyResult:
        state = TransferState(
            source, target, intent=7, temp_state=temp, commit=commit, terminal=terminal
        )
        return CopyResult(status, reason, stop, state, cleanup, journal_error, terminal)

    return make


COPIED = result(
    CopyStatus.COPIED,
    commit=Commit.COMMITTED,
    temp=TempState.COMMITTED,
    terminal=StatusV2.DONE,
)


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Script]]:
    def install(*results: Make | BaseException) -> Script:
        fake = Script(*results)
        monkeypatch.setattr(runner, "copy_item", fake)
        return fake

    yield install


def copy_run(plan: Plan, gate: Permit | None = None) -> CopyRunResult:
    outcome = run_plan(plan, TABLE, copy=CopyOptions(acl=gate or Permit()))
    assert isinstance(outcome, CopyRunResult), outcome
    return outcome


def statuses(outcome: CopyRunResult) -> list[tuple[str, CopyStatus, str]]:
    return [(r.item.source.name, r.status, r.reason) for r in outcome.items]


def v2_report(outcome: CopyRunResult) -> JournalReportV2:
    assert outcome.journal_path is not None
    report = read_any_journal(outcome.journal_path)
    assert isinstance(report, JournalReportV2)
    return report


# --- barrier 1, the mode, the journal version ----------------------------------------


def test_the_cross_volume_copy_is_off_in_the_code() -> None:
    assert features.CROSS_VOLUME_COPY is False
    assert features.cross_volume_copy_enabled() is False


def test_same_volume_runs_the_r3a_move_with_journal_v1(
    src: Path, journals: Journals, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(src, "a.pdf")
    plan = same_plan(src)
    calls: list[Plan] = []
    real = executor.execute_plan

    def spy(plan: Plan, table: object, **kwargs: object) -> ExecutionResult:
        calls.append(plan)
        return real(plan, table, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "execute_plan", spy)
    outcome = run_plan(plan, TABLE, copy=CopyOptions(acl=Permit()))
    assert isinstance(outcome, ExecutionResult) and outcome.complete
    assert calls == [plan] and len(journals.v1) == 1 and journals.v2 == []
    (item,) = plan.moves
    assert not (src / "a.pdf").exists() and item.target.exists()
    assert outcome.journal_path is not None
    report = read_journal(outcome.journal_path)
    assert not report.problems and not isinstance(
        read_any_journal(outcome.journal_path), JournalReportV2
    )


def test_a_same_volume_plan_never_becomes_a_copy(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking for a copy of a ``SAME`` plan keeps the move plan (no switch of the mode)."""
    monkeypatch.setattr(features, "CROSS_VOLUME_COPY", True)
    touch(src, "a.pdf")
    plan = preview_plan(src, None, TABLE, EXTS, cross_copy=True).plan
    assert plan.ok and plan.cross_copy is False and plan.volume.mode is VolumeMode.SAME  # type: ignore[union-attr]


def test_cross_volume_with_the_flag_off_is_refused_by_the_preview(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals, snapshot: Snapshot
) -> None:
    touch(src, "a.pdf")
    fake_destination(monkeypatch, device=V999, volume_serial=0x1234)
    plan = preview_plan(src, None, TABLE, EXTS, cross_copy=True).plan  # the parameter alone
    assert not plan.ok and plan.cross_copy is False and plan.items == ()
    before = snapshot(src)
    outcome = run_plan(plan, TABLE, copy=CopyOptions(acl=Permit()))
    assert isinstance(outcome, RunRefusal) and outcome.reason is RunRefusalReason.PLAN_INVALID
    assert snapshot(src) == before and journals.v1 == [] and journals.v2 == []


def test_a_copy_plan_is_refused_once_the_flag_is_off(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals, snapshot: Snapshot, script
) -> None:
    touch(src, "a.pdf")
    plan = cross_plan(monkeypatch, src)
    fake = script(COPIED)
    monkeypatch.setattr(features, "CROSS_VOLUME_COPY", False)
    before = snapshot(src)
    outcome = run_plan(plan, TABLE, copy=CopyOptions(acl=Permit()))
    assert isinstance(outcome, RunRefusal) and outcome.reason is RunRefusalReason.COPY_DISABLED
    assert not outcome.changed_anything and fake.calls == []
    assert snapshot(src) == before and journals.v1 == [] and journals.v2 == []


@pytest.mark.parametrize("value", [1, "True", object()])
def test_only_exactly_true_enables_the_copy(
    src: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    touch(src, "a.pdf")
    plan = cross_plan(monkeypatch, src)
    monkeypatch.setattr(features, "CROSS_VOLUME_COPY", value)
    outcome = run_plan(plan, TABLE, copy=CopyOptions(acl=Permit()))
    assert isinstance(outcome, RunRefusal) and outcome.reason is RunRefusalReason.COPY_DISABLED


@pytest.mark.parametrize(
    ("mode", "cross_copy"),
    [
        (VolumeMode.AMBIGUOUS, False),
        (VolumeMode.AMBIGUOUS, True),
        (VolumeMode.UNVERIFIED, False),
        (VolumeMode.UNVERIFIED, True),
        (VolumeMode.CROSS, False),  # a move between volumes: never
        (VolumeMode.SAME, True),  # a copy on one volume: never
    ],
)
def test_forged_mode_is_refused_before_any_journal(
    src: Path,
    monkeypatch: pytest.MonkeyPatch,
    journals: Journals,
    snapshot: Snapshot,
    mode: VolumeMode,
    cross_copy: bool,
) -> None:
    monkeypatch.setattr(features, "CROSS_VOLUME_COPY", True)
    touch(src, "a.pdf")
    plan = same_plan(src)
    forged = forge(plan, volume=forge(plan.volume, mode=mode), cross_copy=cross_copy)
    before = snapshot(src)
    outcome = run_plan(forged, TABLE, copy=CopyOptions(acl=Permit()))
    assert isinstance(outcome, RunRefusal) and outcome.reason is RunRefusalReason.VOLUME_REFUSED
    assert outcome.freshness is None  # by the mode check itself, not only by the freshness one
    assert snapshot(src) == before and journals.v1 == [] and journals.v2 == []


@pytest.mark.parametrize("mode", [VolumeMode.AMBIGUOUS, VolumeMode.UNVERIFIED])
def test_ambiguous_and_unverified_previews_are_refused(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals, mode: VolumeMode
) -> None:
    monkeypatch.setattr(features, "CROSS_VOLUME_COPY", True)
    touch(src, "a.pdf")
    if mode is VolumeMode.AMBIGUOUS:
        fake_destination(monkeypatch, device=V999)  # same serial, another device
    else:
        unverified_destination(monkeypatch)
    plan = preview_plan(src, None, TABLE, EXTS, cross_copy=True).plan
    assert not plan.ok and plan.volume is not None and plan.volume.mode is mode
    outcome = run_plan(plan, TABLE, copy=CopyOptions(acl=Permit()))
    assert isinstance(outcome, RunRefusal) and outcome.reason is RunRefusalReason.PLAN_INVALID
    assert journals.v1 == [] and journals.v2 == []


def test_a_plan_without_volume_proof_is_refused(src: Path, journals: Journals) -> None:
    touch(src, "a.pdf")
    plan = forge(same_plan(src), volume=None)
    outcome = run_plan(plan, TABLE)
    assert isinstance(outcome, RunRefusal) and outcome.reason is RunRefusalReason.NO_VOLUME_PROOF
    assert journals.v1 == [] and journals.v2 == []


# --- barrier 2: the ACL policy (D3) is not decided ----------------------------------


def test_production_acl_gate_refuses_every_copy_before_anything_exists(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals, snapshot: Snapshot, script
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    fake = script(COPIED, COPIED)
    before = snapshot(src)
    for options in (None, CopyOptions(), CopyOptions(acl=UndecidedAcl())):
        outcome = run_plan(plan, TABLE, copy=options)
        assert isinstance(outcome, RunRefusal), outcome
        assert (outcome.reason, outcome.detail) == (
            RunRefusalReason.ACL_UNDECIDED,
            ACL_UNDECIDED_DETAIL,
        )
    assert fake.calls == [] and snapshot(src) == before
    assert not plan.destination_root.exists()  # type: ignore[union-attr]
    assert journals.v1 == [] and journals.v2 == []


def test_the_undecided_gate_also_refuses_each_file() -> None:
    gate = UndecidedAcl()
    assert gate.check_item(Path("C:/a.pdf"), Path("D:/x/a.pdf")) == ACL_UNDECIDED_DETAIL


def test_a_file_refused_by_the_gate_gets_no_directory_and_no_copy(
    src: Path, monkeypatch: pytest.MonkeyPatch, script
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    fake = script(COPIED)
    gate = Permit(refuse=frozenset({"a.pdf"}))
    outcome = copy_run(plan, gate)
    assert sorted(fake.calls) == ["b.jpg"] and sorted(gate.items) == ["a.pdf", "b.jpg"]
    by_name = {r.item.source.name: r for r in outcome.items}
    assert (by_name["a.pdf"].status, by_name["a.pdf"].reason) == (CopyStatus.SKIPPED, ACL_REFUSED)
    (refused,) = (i for i in plan.moves if i.source.name == "a.pdf")
    assert not refused.target.parent.exists()  # its category directory was not made
    assert (src / "a.pdf").exists() and (src / "b.jpg").exists()
    assert v2_report(outcome).problems == ()


# --- freshness ---------------------------------------------------------------------


def test_a_stale_copy_plan_is_refused_before_any_change(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals, snapshot: Snapshot, script
) -> None:
    touch(src, "a.pdf")
    plan = cross_plan(monkeypatch, src)
    fake = script(COPIED)
    fake_destination(monkeypatch, device=V999, volume_serial=0x5678)  # another volume now
    before = snapshot(src)
    outcome = run_plan(plan, TABLE, copy=CopyOptions(acl=Permit()))
    assert isinstance(outcome, RunRefusal) and outcome.reason is RunRefusalReason.PLAN_STALE
    assert outcome.freshness is not None and not outcome.freshness.fresh
    assert fake.calls == [] and snapshot(src) == before and journals.v2 == []


def test_a_stale_move_plan_is_refused_before_any_change(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals, snapshot: Snapshot
) -> None:
    touch(src, "a.pdf")
    plan = same_plan(src)
    fake_destination(monkeypatch, deepest_id=12345)  # the destination is another directory
    before = snapshot(src)
    outcome = run_plan(plan, TABLE)
    assert isinstance(outcome, RunRefusal) and outcome.reason is RunRefusalReason.PLAN_STALE
    assert snapshot(src) == before and journals.v1 == [] and journals.v2 == []


def test_a_copy_plan_whose_destination_became_the_same_volume_is_stale(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals
) -> None:
    touch(src, "a.pdf")
    real = volumeproof.probe_location
    plan = cross_plan(monkeypatch, src)
    monkeypatch.setattr(volumeproof, "probe_location", real)  # the real volume again: SAME
    outcome = run_plan(plan, TABLE, copy=CopyOptions(acl=Permit()))
    assert isinstance(outcome, RunRefusal) and outcome.reason is RunRefusalReason.PLAN_STALE
    assert journals.v2 == []


def test_the_copy_preview_changes_nothing(
    src: Path, monkeypatch: pytest.MonkeyPatch, snapshot: Snapshot, no_side_effects
) -> None:
    touch(src, "a.pdf", "b.jpg")
    monkeypatch.setattr(features, "CROSS_VOLUME_COPY", True)
    fake_destination(monkeypatch, device=V999, volume_serial=0x1234)
    before = snapshot(src.parent)
    with no_side_effects():
        preview = preview_plan(src, None, TABLE, EXTS, cross_copy=True)
    assert preview.plan.cross_copy and snapshot(src.parent) == before


# --- a copy run: what copy_item reports ---------------------------------------------


def test_copies_journal_v2_with_every_source_kept(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals, script
) -> None:
    touch(src, "a.pdf", "b.jpg", "c.txt")  # c.txt: not selected
    plan = cross_plan(monkeypatch, src)
    fake = script(COPIED, COPIED)
    outcome = copy_run(plan)
    assert sorted(fake.calls) == ["a.pdf", "b.jpg"] and outcome.complete
    assert journals.v1 == [] and len(journals.v2) == 1
    assert outcome.counts[CopyStatus.COPIED] == 2 and outcome.stopped is None
    assert all((src / name).exists() for name in ("a.pdf", "b.jpg", "c.txt"))
    report = v2_report(outcome)
    assert report.problems == ()
    records = list(journals.v2[0].history)
    assert records[0]["operation"] == "run" and records[0]["status"] == "started"
    assert records[-1]["operation"] == "run" and records[-1]["status"] == "finished"
    assert all(r["source_volume"] is not None for r in records if r["operation"] == "run")
    assert all(r["operation"] != "move" for r in records)  # never a move record
    assert [r for r in records if r["operation"] == "copy"] == []  # the fake journals none


def test_a_definite_failure_lets_the_next_file_go_on(
    src: Path, monkeypatch: pytest.MonkeyPatch, script
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    error = result(CopyStatus.FAILED, Reason.COMMIT_FAILED)  # rename refused, temp removed
    fake = script(error, COPIED)
    outcome = copy_run(plan)
    assert len(fake.calls) == 2 and outcome.stopped is None and not outcome.complete
    assert sorted(r.status for r in outcome.items) == [CopyStatus.COPIED, CopyStatus.FAILED]


UNCERTAIN = {
    "recovery_required": result(
        CopyStatus.RECOVERY_REQUIRED,
        Reason.COMMIT_UNKNOWN,
        stop=Stop.RUN_ABORTED,
        commit=Commit.UNKNOWN,
        temp=TempState.UNKNOWN,
        terminal=StatusV2.RECOVERY_REQUIRED,
    ),
    "recovery_without_stop": result(
        CopyStatus.RECOVERY_REQUIRED,
        Reason.RENAME_NOT_ESTABLISHED,
        commit=Commit.COMMITTED,
        temp=TempState.COMMITTED,
        terminal=StatusV2.RECOVERY_REQUIRED,
    ),
    "temp_left": result(CopyStatus.FAILED, Reason.WRITE_FAILED, temp=TempState.LEFT),
    "temp_unknown": result(CopyStatus.FAILED, Reason.WRITE_FAILED, temp=TempState.UNKNOWN),
    "commit_unknown": result(CopyStatus.FAILED, Reason.COMMIT_FAILED, commit=Commit.UNKNOWN),
    "no_terminal": result(CopyStatus.FAILED, Reason.WRITE_FAILED, terminal=None),
}


@pytest.mark.parametrize("case", sorted(UNCERTAIN))
def test_after_an_uncertain_outcome_no_next_file_is_started(
    src: Path, monkeypatch: pytest.MonkeyPatch, script, case: str
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    fake = script(UNCERTAIN[case], COPIED)
    outcome = copy_run(plan)
    assert len(fake.calls) == 1 and outcome.stopped is ExecReason.RUN_ABORTED
    first, second = (
        (r for r in outcome.items if r.item.source.name == fake.calls[0]),
        (r for r in outcome.items if r.item.source.name != fake.calls[0]),
    )
    (first,), (second,) = tuple(first), tuple(second)
    assert first.transfer is not None and second.status is CopyStatus.CANCELLED
    assert second.reason == ExecReason.RUN_ABORTED
    assert any("check by hand" in p for p in outcome.problems)
    assert not outcome.complete and v2_report(outcome).problems == ()


def test_a_journal_failure_stops_the_run(
    src: Path, monkeypatch: pytest.MonkeyPatch, script
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    error = JournalError("scripted: journal write failed")
    failed = result(
        CopyStatus.FAILED,
        Reason.JOURNAL_FAILED,
        stop=Stop.JOURNAL_FAILED,
        commit=Commit.NOT_ATTEMPTED,
        terminal=None,
        journal_error=error,
    )
    fake = script(failed, COPIED)
    outcome = copy_run(plan)
    assert len(fake.calls) == 1 and outcome.stopped is ExecReason.JOURNAL_FAILED
    assert str(error) in outcome.problems
    assert [r.status for r in outcome.items].count(CopyStatus.CANCELLED) == 1


def test_a_failed_release_stops_the_run_and_is_kept(
    src: Path, monkeypatch: pytest.MonkeyPatch, script
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    error = OSError(6, "scripted: CloseHandle failed")
    copied = result(
        CopyStatus.COPIED,
        commit=Commit.COMMITTED,
        temp=TempState.COMMITTED,
        terminal=StatusV2.DONE,
        cleanup=(error,),
    )
    fake = script(copied, COPIED)
    outcome = copy_run(plan)
    assert len(fake.calls) == 1 and outcome.stopped is ExecReason.CLEANUP_FAILED
    assert error in outcome.cleanup_errors and outcome.cleanup_problems


def test_an_unexpected_error_inside_a_started_copy_is_recovery_required(
    src: Path, monkeypatch: pytest.MonkeyPatch, script
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    bug = RuntimeError("scripted: a bug after the intent")
    bug.transfer_states = (TransferState(src / "a.pdf", src / "x", intent=3),)  # type: ignore[attr-defined]
    fake = script(bug, COPIED)
    outcome = copy_run(plan)
    assert len(fake.calls) == 1 and outcome.stopped is ExecReason.RUN_ABORTED
    statuses_ = sorted(r.status for r in outcome.items)
    assert statuses_ == [CopyStatus.CANCELLED, CopyStatus.RECOVERY_REQUIRED]


def test_an_unexpected_error_with_a_failed_close_keeps_both_and_stops(
    src: Path, monkeypatch: pytest.MonkeyPatch, script
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    close = HandleCloseError(0, "scripted: CloseHandle failed (6)", str(src / "x"), 6)
    bug = RuntimeError("scripted: a bug after the intent")
    bug.transfer_states = (TransferState(src / "a.pdf", src / "x", intent=3),)  # type: ignore[attr-defined]
    winfileops.attach_release_errors(bug, [close])
    fake = script(bug, COPIED)
    outcome = copy_run(plan)
    assert len(fake.calls) == 1  # the next file is not started
    assert outcome.stopped is ExecReason.RUN_ABORTED  # the cause stays the error, not the close
    assert outcome.stop_detail is not None and "a bug after the intent" in outcome.stop_detail
    by_name = {r.item.source.name: r for r in outcome.items}
    first, second = by_name["a.pdf"], by_name["b.jpg"]
    assert first.status is CopyStatus.RECOVERY_REQUIRED
    assert first.reason == ExecReason.UNEXPECTED_ERROR and first.transfer is not None
    assert first.cleanup_errors == (close,) and first.cleanup_errors[0] is close
    assert second.status is CopyStatus.CANCELLED and second.cleanup_errors == ()
    assert len(outcome.cleanup_errors) == 1 and outcome.cleanup_errors[0] is close
    assert len(outcome.cleanup_problems) == 1
    assert "CloseHandle failed" in outcome.cleanup_problems[0]
    assert any("a bug after the intent" in p for p in outcome.problems)
    assert not outcome.complete and outcome.partial


def test_an_unexpected_error_before_the_copy_started_is_a_failure(
    src: Path, monkeypatch: pytest.MonkeyPatch, script
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    fake = script(RuntimeError("scripted: a bug before the intent"), COPIED)
    outcome = copy_run(plan)
    assert len(fake.calls) == 1 and outcome.stopped is ExecReason.RUN_ABORTED
    assert sorted(r.status for r in outcome.items) == [CopyStatus.CANCELLED, CopyStatus.FAILED]
    assert v2_report(outcome).problems == ()


def test_an_interrupt_is_raised_and_nothing_is_started_after_it(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals, script
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    fake = script(KeyboardInterrupt(), COPIED)
    with pytest.raises(KeyboardInterrupt) as raised:
        run_plan(plan, TABLE, copy=CopyOptions(acl=Permit()))
    assert len(fake.calls) == 1
    assert any("every source was kept" in n for n in raised.value.__notes__)
    (journal,) = journals.v2
    assert journal.state == "closed"


def test_cancel_before_a_file_cancels_the_rest(
    src: Path, monkeypatch: pytest.MonkeyPatch, script
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    fake = script(COPIED, COPIED)
    asked = iter([False, True, True, True, True, True, True, True])
    outcome = run_plan(plan, TABLE, cancel=lambda: next(asked), copy=CopyOptions(acl=Permit()))
    assert isinstance(outcome, CopyRunResult)
    assert len(fake.calls) <= 1 and outcome.stopped is ExecReason.CANCELLED
    assert v2_report(outcome).problems == ()


def test_the_gui_does_not_reach_the_copy() -> None:
    gui = (Path(__file__).parents[1] / "file_organizer.py").read_text(encoding="utf-8")
    assert "runner" not in gui and "cross_copy" not in gui and "CROSS_VOLUME_COPY" not in gui
