"""R3B end to end on the NTFS of ``tmp_path`` (C:): ``run_plan`` with the real
``aclcreate`` calls, the v3 mkdir blocks and the copy with its security.

As in ``test_runner_ntfs``: one volume, proven ``CROSS`` by a faked destination proof,
the test-only gate ``Permit`` (the production gate stays ``UndecidedAcl``). Only what
the test creates under ``tmp_path`` is created, changed or removed. A failure is
injected through ``CopyOptions.acl_ops`` (a subclass of the real ``NtOps``), through the
journal stream, or by replacing ``aclcreate.create_category`` in the runner; no safety
check of the runner is replaced.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
import test_runner_fake
from test_aclproof import (
    EVERYONE,
    PRIVATE,
    SYSTEM,
    descriptor,
    me,  # noqa: F401  (a fixture: the creator SID)
    set_sddl,
    unlocked,  # noqa: F401  (autouse: tmp_path stays removable)
)
from test_executor import TABLE, fingerprints, touch
from test_runner_fake import Journals, Permit, cross_plan
from test_runner_ntfs import FailOn, by_name, partials, target_of
from test_winfileops import walk

from downloads_organizer import aclcreate as c
from downloads_organizer import aclproof as a
from downloads_organizer import runner
from downloads_organizer import winfileops as w
from downloads_organizer.aclproof import Descriptor, Sid
from downloads_organizer.copycommit import CopyStatus
from downloads_organizer.executor import ExecReason
from downloads_organizer.journal import OperationState
from downloads_organizer.journal_v2 import TempState
from downloads_organizer.journal_v3 import MAY_EXIST, JournalReportV3, JournalV3, read_any_journal
from downloads_organizer.models import Plan
from downloads_organizer.runner import CopyOptions, CopyRunResult, run_plan

pytestmark = [pytest.mark.windows_only, pytest.mark.real_ntfs]
journals = test_runner_fake.journals  # the fixture


@pytest.fixture
def src(tmp_path: Path) -> Path:
    """The source (and destination root): only the user and SYSTEM, protected."""
    path = tmp_path / "area" / "Downloads"
    path.mkdir(parents=True)
    set_sddl(path, PRIVATE)
    return path


class Ops(c.NtOps):
    """The real calls. ``before_create(path)`` runs once, right before the directory
    ``at`` is created (the first one created, without ``at``); reading the descriptor
    of ``fail_read`` fails (after ``race``), of ``raise_read`` raises a non-``OSError``."""

    def __init__(
        self,
        *,
        before_create: Callable[[Path], None] | None = None,
        at: Path | None = None,
        fail_read: Path | None = None,
        race: Callable[[], None] | None = None,
        raise_read: Path | None = None,
    ) -> None:
        self.before_create, self.fail_read, self.race = before_create, fail_read, race
        self.raise_read, self.at = raise_read, at
        self.reads: list[Path] = []

    def create_with(self, handle: w.Handle, parent: w.Handle, name: str, descriptor: bytes):  # type: ignore[no-untyped-def]
        path, hook = Path(handle.path), self.before_create
        if handle.role == "H_DIR" and hook is not None and self.at in (None, path):
            self.before_create = None
            hook(path)
        return super().create_with(handle, parent, name, descriptor)

    def read_descriptor(self, handle: w.Handle) -> bytes:
        path = Path(handle.path)
        self.reads.append(path)
        if path == self.fail_read:
            if self.race is not None:
                self.race()
            raise OSError(5, "injected: the descriptor is not read")
        if path == self.raise_read:
            raise RuntimeError("injected: reading the descriptor raised")
        return super().read_descriptor(handle)


def run(plan: Plan, ops: c.Ops | None = None, **kw: object) -> CopyRunResult:
    options = CopyOptions(acl=Permit(), acl_ops=ops)
    outcome = run_plan(plan, TABLE, copy=options, **kw)  # type: ignore[arg-type]
    assert isinstance(outcome, CopyRunResult), outcome
    return outcome


def file_id(path: Path) -> str:
    """The 128-bit file id of a directory or file, as the journal writes it."""
    if path.is_dir():
        handle = walk(path, access=w.FILE_READ_ATTRIBUTES, share=w.SHARE_ALL)
    else:
        parent = walk(path.parent, access=w.FILE_READ_ATTRIBUTES, share=w.SHARE_ALL)
        try:
            handle, _ = w.open_relative(
                parent, path.name, directory=False, access=w.FILE_READ_ATTRIBUTES,
                share=w.SHARE_ALL,
            )  # fmt: skip
        finally:
            parent.close()
    try:
        return f"{w.query(handle).file_id:032x}"
    finally:
        handle.close()


def mkdirs(journal: JournalV3) -> list[dict[str, object]]:
    return [dict(r) for r in journal.history if r["operation"] == "mkdir"]


def report_of(outcome: CopyRunResult) -> JournalReportV3:
    assert outcome.journal_path is not None
    report = read_any_journal(outcome.journal_path)
    assert isinstance(report, JournalReportV3)
    return report


def inherited(d: Descriptor) -> bool:
    return any(ace.flags & a.INHERITED_ACE for ace in d.dacl or ())


def private_as(created: Path, original: Path, me: Sid, rights: int) -> None:  # noqa: F811
    """Protected, nothing inherited, owned by the creator, and by Authz the same access
    as ``original`` for every principal (both ways); Everyone gets nothing."""
    made, model = descriptor(created), descriptor(original)
    assert made.protected and not inherited(made) and made.owner == me
    sids = a.principals(made, model, creator=me)
    assert a.wider_principals(made, model, sids, rights) == ()
    assert a.wider_principals(model, made, sids, rights) == ()
    assert a.effective_access(made.raw, EVERYONE) == 0


# --- the whole path -------------------------------------------------------------------


def test_copy_creates_each_category_with_its_security_journals_it_and_keeps_the_sources(
    src: Path,
    monkeypatch: pytest.MonkeyPatch,
    journals: Journals,
    me: Sid,  # noqa: F811
) -> None:
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    plan = cross_plan(monkeypatch, src)
    root = plan.destination_root
    assert root is not None and root.parent == src and not root.exists()
    categories = list(dict.fromkeys(i.target.parent for i in plan.moves))  # in plan order
    assert len(categories) == 2 and all(d.parent == root for d in categories)
    outcome = run(plan)
    assert outcome.complete and outcome.stopped is None, outcome.problems
    assert outcome.directories_created == (root, *categories)  # the root first
    assert fingerprints(src) == before  # every source: the same file, the same content
    for name in ("a.pdf", "b.jpg"):
        target = target_of(plan, name)
        assert target.read_bytes() == (src / name).read_bytes()
        private_as(target, src / name, me, a.FILE_ALL_ACCESS)  # the final file
    for directory in (root, *categories):  # each from the source directory's DACL
        private_as(directory, src, me, a.DIRECTORY_RIGHTS)
    assert partials(plan) == []
    (journal,) = journals.v2
    blocks = mkdirs(journal)
    assert [(r["status"], r["dir_state"]) for r in blocks] == [
        ("intent", None),
        ("done", "created"),
    ] * 3
    assert [Path(str(r["target"])) for r in blocks[::2]] == [root, *categories]
    for intent, done in zip(blocks[::2], blocks[1::2], strict=True):
        parent = file_id(Path(str(intent["target"])).parent)
        assert intent["parent_id"] == done["parent_id"] == parent
        assert intent["dir_id"] is None and done["intent"] == intent["sequence"]
        assert done["dir_id"] == file_id(Path(str(done["target"])))
    report = report_of(outcome)
    assert report.problems == () and report.finished and not report.needs_review
    states = {(e.operation, e.state) for e in report.entries}
    assert states == {("mkdir", OperationState.DONE), ("copy", OperationState.DONE)}


def test_an_existing_category_keeps_its_files_and_needs_no_mkdir(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals
) -> None:
    touch(src, "a.pdf")
    plan = cross_plan(monkeypatch, src)
    category = target_of(plan, "a.pdf").parent
    touch(category, "theirs.txt")
    theirs = fingerprints(category)
    sd = a.read_descriptor_at(category)
    plan = cross_plan(monkeypatch, src)  # the folder exists before this preview
    outcome = run(plan)
    assert outcome.complete and outcome.directories_created == (), outcome.problems
    assert fingerprints(category)["theirs.txt"] == theirs["theirs.txt"]
    assert a.read_descriptor_at(category) == sd  # its security is not changed
    assert target_of(plan, "a.pdf").read_bytes() == (src / "a.pdf").read_bytes()
    (journal,) = journals.v2
    assert mkdirs(journal) == []
    assert report_of(outcome).problems == ()


def test_a_category_raced_in_before_the_create_is_existing_and_used(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals
) -> None:
    touch(src, "a.pdf")
    plan = cross_plan(monkeypatch, src)
    category = target_of(plan, "a.pdf").parent

    def race(path: Path) -> None:
        path.mkdir()
        (path / "theirs.txt").write_bytes(b"theirs")

    outcome = run(plan, Ops(before_create=race, at=category))
    assert outcome.complete, outcome.problems
    assert by_name(outcome)["a.pdf"].status is CopyStatus.COPIED  # type: ignore[attr-defined]
    assert (category / "theirs.txt").read_bytes() == b"theirs"
    (journal,) = journals.v2
    created_root, existing = mkdirs(journal)[1], mkdirs(journal)[3]
    assert (created_root["dir_state"], existing["dir_state"]) == ("created", "existing")
    assert existing["status"] == "done" and existing["dir_id"] == file_id(category)
    assert existing["parent_id"] == file_id(category.parent)
    assert outcome.directories_created == (category.parent,)  # not the raced one
    assert report_of(outcome).problems == ()


def test_a_file_raced_in_under_the_category_name_is_refused_and_kept(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    raced: list[Path] = []

    def race(path: Path) -> None:
        path.write_bytes(b"theirs")
        raced.append(path)

    outcome = run(plan, Ops(before_create=race, at=target_of(plan, "a.pdf").parent))
    (taken,) = raced
    assert taken.read_bytes() == b"theirs"
    (lost,) = [r for r in outcome.items if r.item.target.parent == taken]
    other = next(r for r in outcome.items if r is not lost)
    assert (lost.status, lost.reason) == (CopyStatus.SKIPPED, ExecReason.TARGET_DIR_INVALID)
    assert other.status is CopyStatus.COPIED and outcome.stopped is None
    (journal,) = journals.v2
    first = mkdirs(journal)[2:4]  # after the root's block
    assert [(r["status"], r["dir_state"], r["dir_id"]) for r in first] == [
        ("intent", None, None),
        ("failed", "refused", None),
    ]
    assert report_of(outcome).problems == ()


def test_a_failed_check_of_a_new_category_removes_only_it_and_the_run_goes_on(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals
) -> None:
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    plan = cross_plan(monkeypatch, src)
    category = target_of(plan, "a.pdf").parent
    outcome = run(plan, Ops(fail_read=category))
    assert not category.exists()  # removed: it was this run's own and empty
    a_pdf, b_jpg = by_name(outcome)["a.pdf"], by_name(outcome)["b.jpg"]
    assert (a_pdf.status, a_pdf.reason) == (CopyStatus.FAILED, ExecReason.TARGET_DIR_FAILED)  # type: ignore[attr-defined]
    assert b_jpg.status is CopyStatus.COPIED and outcome.stopped is None  # type: ignore[attr-defined]
    assert fingerprints(src) == before
    (journal,) = journals.v2
    removed = next(r for r in mkdirs(journal) if r["target"] == str(category) and r["dir_state"])
    assert (removed["status"], removed["dir_state"]) == ("failed", "removed")
    assert removed["reason"] == "descriptor_unreadable" and removed["dir_id"] is not None
    report = report_of(outcome)
    assert report.problems == () and report.finished


# --- stops ----------------------------------------------------------------------------


def test_a_new_category_left_behind_requires_recovery_and_stops_the_run(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals
) -> None:
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    plan = cross_plan(monkeypatch, src)
    first = plan.moves[0].target.parent
    theirs = first / "theirs.txt"
    ops = Ops(fail_read=first, race=lambda: theirs.write_bytes(b"theirs"))
    outcome = run(plan, ops)
    assert outcome.stopped is ExecReason.RUN_ABORTED
    assert theirs.read_bytes() == b"theirs" and first.is_dir()  # never removed with it
    assert first in outcome.directories_created
    assert not any(i.target.exists() for i in plan.moves) and fingerprints(src) == before
    assert all(r.status is not CopyStatus.COPIED and r.transfer is None for r in outcome.items)
    (journal,) = journals.v2
    blocks = mkdirs(journal)
    assert len(blocks) == 4  # the root, then this one; nothing more was created
    left = blocks[3]
    assert (left["status"], left["dir_state"]) == ("recovery_required", "left")
    assert left["dir_id"] == file_id(first)
    assert not any(r["operation"] == "copy" and r["status"] == "intent" for r in journal.history)
    report = report_of(outcome)
    (entry,) = [e for e in report.entries if e.target == str(first)]
    assert entry.state is OperationState.RECOVERY_REQUIRED
    assert any("still there" in note for note in entry.notes)


def test_an_exception_while_checking_a_new_category_is_unchecked_and_stops(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)
    first = plan.moves[0].target.parent
    outcome = run(plan, Ops(raise_read=first))
    assert outcome.stopped is ExecReason.RUN_ABORTED and first.is_dir()
    assert not any(i.target.exists() for i in plan.moves)
    (journal,) = journals.v2
    blocks = mkdirs(journal)
    assert len(blocks) == 4
    assert (blocks[3]["status"], blocks[3]["dir_state"]) == ("recovery_required", "unchecked")
    assert blocks[3]["reason"] == "exception" and blocks[3]["dir_id"] == file_id(first)
    assert journal.history[-1]["operation"] == "run"  # the block has its outcome
    report = report_of(outcome)
    assert report.problems == ()
    assert any(e.state is OperationState.RECOVERY_REQUIRED for e in report.entries)


def test_an_unknown_create_outcome_requires_recovery_and_stops(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals
) -> None:
    touch(src, "a.pdf", "b.jpg")
    plan = cross_plan(monkeypatch, src)

    def lost(*_args: object) -> c.Creation:
        raise RuntimeError("scripted: the create call's outcome is lost")

    monkeypatch.setattr(runner.aclcreate, "create_category", lost)
    outcome = run(plan)
    assert outcome.stopped is ExecReason.RUN_ABORTED
    assert not any(i.target.exists() for i in plan.moves)
    (journal,) = journals.v2
    intent, unknown = mkdirs(journal)
    assert (unknown["status"], unknown["dir_state"], unknown["dir_id"]) == (
        "recovery_required",
        "unknown",
        None,
    )
    assert unknown["parent_id"] == intent["parent_id"] == file_id(src)
    report = report_of(outcome)
    (entry,) = [e for e in report.entries if e.operation == "mkdir"]
    assert entry.state is OperationState.RECOVERY_REQUIRED
    assert any("may exist" in note for note in entry.notes)


def test_a_create_cut_off_after_it_succeeded_is_never_vouched_for(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals
) -> None:
    touch(src, "a.pdf")
    plan = cross_plan(monkeypatch, src)
    root = target_of(plan, "a.pdf").parent.parent
    real = runner.aclcreate.create_category

    def cut_off(ops: c.Ops, *args: object) -> c.Creation:
        creation = real(ops, *args)  # type: ignore[arg-type]
        assert creation.state is c.Created.CREATED and creation.handle is not None
        ops.close(creation.handle)  # the test's own handle: nothing is left open
        raise c.attach_creation(RuntimeError("scripted: cut off after the create"), creation)

    monkeypatch.setattr(runner.aclcreate, "create_category", cut_off)
    outcome = run(plan)
    assert outcome.stopped is ExecReason.RUN_ABORTED
    assert outcome.directories_created == (root,)  # it may be there: reported, never removed
    assert root.is_dir() and not any(i.target.exists() for i in plan.moves)
    (journal,) = journals.v2
    intent, unchecked = mkdirs(journal)
    assert (unchecked["status"], unchecked["dir_state"], unchecked["reason"]) == (
        "recovery_required",
        "unchecked",
        "exception",
    )
    assert unchecked["dir_id"] == file_id(root) and unchecked["intent"] == intent["sequence"]


def test_an_existing_category_whose_identity_is_not_proven_is_refused(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals
) -> None:
    """The identity of a held directory comes from its check; when it is missing (the
    bookkeeping lost it) the raced-in directory is refused, never used or journaled."""
    touch(src, "a.pdf")
    plan = cross_plan(monkeypatch, src)
    category = target_of(plan, "a.pdf").parent
    held = runner._CopyRun.identity

    def lost(self: runner._CopyRun, handle: w.Handle) -> object:
        return None if Path(handle.path) == category else held(self, handle)

    monkeypatch.setattr(runner._CopyRun, "identity", lost)

    def race(path: Path) -> None:
        path.mkdir()
        (path / "theirs.txt").write_bytes(b"theirs")

    outcome = run(plan, Ops(before_create=race, at=category))
    assert by_name(outcome)["a.pdf"].reason == str(ExecReason.TARGET_DIR_FAILED)  # type: ignore[attr-defined]
    assert not target_of(plan, "a.pdf").exists()
    assert (category / "theirs.txt").read_bytes() == b"theirs"
    (journal,) = journals.v2
    refused = mkdirs(journal)[3]
    assert (refused["status"], refused["dir_state"], refused["dir_id"]) == (
        "failed",
        "refused",
        None,
    )


def test_a_failed_mkdir_intent_creates_nothing_and_stops(
    src: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    touch(src, "a.pdf", "b.jpg")
    before = sorted(p.name for p in src.iterdir())
    plan = cross_plan(monkeypatch, src)
    path = tmp_path / "journal.jsonl"
    stream = FailOn(path.open("w+b"), b'"operation":"mkdir","status":"intent"')
    made: list[JournalV3] = []

    def journal() -> JournalV3:
        made.append(log := JournalV3(stream, path))  # type: ignore[arg-type]
        return log

    ops = Ops()
    outcome = run(plan, ops, journal_v3=journal)
    assert outcome.stopped is ExecReason.JOURNAL_FAILED
    assert sorted(p.name for p in src.iterdir()) == before  # no category, no file
    assert outcome.directories_created == ()
    (log,) = made
    assert [r["operation"] for r in log.history] == ["run"]


def test_an_interrupt_in_the_create_leaves_the_intent_open_and_may_exist(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals
) -> None:
    touch(src, "a.pdf")
    plan = cross_plan(monkeypatch, src)

    def interrupt(_path: Path) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(plan, Ops(before_create=interrupt))
    (journal,) = journals.v2
    assert journal.history[-1]["status"] == "intent"
    assert journal.history[-1]["operation"] == "mkdir"
    assert journal.path is not None
    report = read_any_journal(journal.path)
    assert isinstance(report, JournalReportV3) and not report.finished
    (entry,) = [e for e in report.entries if e.operation == "mkdir"]
    assert entry.state is OperationState.UNCERTAIN and MAY_EXIST in entry.notes
    assert not target_of(plan, "a.pdf").exists()


def test_an_unreadable_source_directory_descriptor_refuses_the_run(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals
) -> None:
    touch(src, "a.pdf", "b.jpg")
    before = sorted(p.name for p in src.iterdir())
    plan = cross_plan(monkeypatch, src)
    outcome = run(plan, Ops(fail_read=src))
    assert outcome.stopped is ExecReason.RUN_ABORTED
    assert any("run check failed" in p for p in outcome.problems)
    assert all(r.status is CopyStatus.SKIPPED for r in outcome.items)
    assert sorted(p.name for p in src.iterdir()) == before
    (journal,) = journals.v2
    assert mkdirs(journal) == []


def test_an_unreadable_file_descriptor_fails_only_that_file(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals
) -> None:
    touch(src, "a.pdf", "b.jpg")
    before = fingerprints(src)
    plan = cross_plan(monkeypatch, src)
    outcome = run(plan, Ops(fail_read=src / "a.pdf"))
    a_pdf, b_jpg = by_name(outcome)["a.pdf"], by_name(outcome)["b.jpg"]
    assert (a_pdf.status, a_pdf.reason) == (CopyStatus.FAILED, ExecReason.SOURCE_CHECK_FAILED)  # type: ignore[attr-defined]
    assert a_pdf.transfer is None and not target_of(plan, "a.pdf").exists()  # type: ignore[attr-defined]
    assert b_jpg.status is CopyStatus.COPIED and outcome.stopped is None  # type: ignore[attr-defined]
    assert fingerprints(src) == before and partials(plan) == []
    assert report_of(outcome).problems == ()


# --- the security is checked again before the commit ----------------------------------


class TempOps(Ops):
    """The real calls; at the ``nth`` read of the temporary file's descriptor, ``before``
    runs on its path first (or, without it, the read fails)."""

    def __init__(self, nth: int, before: Callable[[Path], None] | None = None) -> None:
        super().__init__()
        self.nth, self.before, self.temp_reads = nth, before, 0

    def read_descriptor(self, handle: w.Handle) -> bytes:
        path = Path(handle.path)
        if path.suffix == ".partial":
            self.temp_reads += 1
            if self.temp_reads == self.nth:
                if self.before is None:
                    raise OSError(5, "injected: the temporary file's descriptor is not read")
                self.before(path)
        return super().read_descriptor(handle)


WIDER_FILE = "D:P(A;;FA;;;OW)(A;;FA;;;SY)(A;;FR;;;WD)"  # and Everyone may read


@pytest.mark.parametrize("change", ["unreadable", "widened"])
def test_a_temporary_file_whose_security_changed_is_never_committed(
    src: Path, monkeypatch: pytest.MonkeyPatch, journals: Journals, change: str
) -> None:
    """After the create (read 1) the descriptor is read again before the commit: a
    failed read or a DACL that now lets Everyone read stops the commit; nothing is
    renamed to the final name, the source is kept."""
    touch(src, "a.pdf")
    before = fingerprints(src)
    plan = cross_plan(monkeypatch, src)

    def widen(path: Path) -> None:
        set_sddl(path, WIDER_FILE)

    ops = TempOps(2, None if change == "unreadable" else widen)
    outcome = run(plan, ops)
    assert ops.temp_reads >= 2
    result = by_name(outcome)["a.pdf"]
    assert (result.status, result.reason) == (CopyStatus.FAILED, "verify_failed")  # type: ignore[attr-defined]
    assert result.transfer.temp_state is TempState.REMOVED  # type: ignore[attr-defined]
    assert not target_of(plan, "a.pdf").exists() and fingerprints(src) == before
    assert partials(plan) == [] and outcome.stopped is None
    (journal,) = journals.v2
    copies = [(r["status"], r["temp_state"]) for r in journal.history if r["operation"] == "copy"]
    assert copies == [
        ("intent", "none"),
        ("staged", "present"),
        ("written", "present"),
        ("failed", "removed"),
    ]
    assert report_of(outcome).problems == ()


# --- the source's descriptor is read again before the commit --------------------------

FILE_READ_DATA = 0x1
ONLY_OWNER = "D:P(A;;FA;;;OW)"  # SYSTEM, allowed before, loses every right, READ too
WITH_DENY = "D:P(D;;FR;;;AN)(A;;FA;;;OW)(A;;FA;;;SY)"  # a deny ACE: not supported
TOUCHED = ""  # not a DACL: the source's mtime is changed instead (the DACL is kept)


class SourceOps(Ops):
    """The real calls. At the ``at``-th read of the temporary file's descriptor ``change``
    runs first (on that path); the reads of the source's descriptor are counted and the
    ``fail``-th of them fails. Nothing else is replaced: each source read that is not
    failed is the real one through ``H_SRC``."""

    def __init__(
        self,
        source: Path,
        *,
        at: int = 0,
        change: Callable[[Path], None] | None = None,
        fail: int = 0,
    ) -> None:
        super().__init__()
        self.source, self.at_read, self.change, self.fail = source, at, change, fail
        self.temp_reads = self.source_reads = 0

    def read_descriptor(self, handle: w.Handle) -> bytes:
        path = Path(handle.path)
        if path.suffix == ".partial":
            self.temp_reads += 1
            if self.temp_reads == self.at_read and self.change is not None:
                self.change(path)
        elif path == self.source:
            self.source_reads += 1
            if self.source_reads == self.fail:
                raise OSError(5, "injected: the source's descriptor is not read again")
        return super().read_descriptor(handle)


BEFORE_C12 = [("intent", "none"), ("staged", "present"), ("written", "present")]
BEFORE_COMMIT = [*BEFORE_C12, ("verified", "present")]


@pytest.mark.parametrize(
    ("at", "sddl", "fail", "rows", "reads", "why"),
    [
        (2, ONLY_OWNER, 0, BEFORE_COMMIT, 3, "changed after the copy began"),  # right before
        (1, ONLY_OWNER, 0, BEFORE_C12, 2, "changed after the copy began"),  # before C12
        (2, WITH_DENY, 0, BEFORE_COMMIT, 3, "unsupported_deny"),
        (0, None, 3, BEFORE_COMMIT, 3, "is not read again"),  # the last read fails
        (0, None, 2, BEFORE_C12, 2, "is not read again"),  # the read at C12 fails
        (2, TOUCHED, 0, BEFORE_COMMIT, 2, "the source now"),  # H_SRC no longer as planned
    ],
    ids=[
        "revoked-before-commit",
        "revoked-before-c12",
        "unsupported",
        "unread-last",
        "unread",
        "touched-before-commit",
    ],
)
def test_a_source_whose_security_changed_during_the_copy_is_never_committed(
    src: Path,
    monkeypatch: pytest.MonkeyPatch,
    journals: Journals,
    at: int,
    sddl: str | None,
    fail: int,
    rows: list[tuple[str, str]],
    reads: int,
    why: str,
) -> None:
    """The temporary file got its DACL from the source's descriptor read before the copy.
    When the source's DACL changes after that (SYSTEM loses READ, or a deny ACE comes in),
    it is not read again, or the source held is no longer as planned, nothing is
    committed: the temporary file (still with the old DACL) is removed, no final file, no
    false ``done``; the source and the files of others in the category are kept."""
    touch(src, "a.pdf")
    plan = cross_plan(monkeypatch, src)
    category = target_of(plan, "a.pdf").parent
    touch(category, "theirs.txt")
    theirs, theirs_sd = fingerprints(category), a.read_descriptor_at(category)
    plan = cross_plan(monkeypatch, src)  # the category exists before this preview
    source = src / "a.pdf"
    before, identity = fingerprints(src), file_id(source)
    assert a.effective_access(descriptor(source).raw, SYSTEM) & FILE_READ_DATA
    seen: list[tuple[int, int]] = []

    def revoke(temp: Path) -> None:
        assert sddl is not None
        if sddl == TOUCHED:  # only the test's own source file, also below
            os.utime(source, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        else:
            set_sddl(source, sddl)
        system = (descriptor(temp).raw, descriptor(source).raw)
        seen.append(tuple(a.effective_access(d, SYSTEM) & FILE_READ_DATA for d in system))  # type: ignore[arg-type]

    ops = SourceOps(source, at=at, change=revoke if sddl is not None else None, fail=fail)
    outcome = run(plan, ops)
    result = by_name(outcome)["a.pdf"]
    assert not target_of(plan, "a.pdf").exists() and partials(plan) == []
    assert (result.status, result.reason) == (CopyStatus.FAILED, "verify_failed")  # type: ignore[attr-defined]
    assert result.transfer.temp_state is TempState.REMOVED  # type: ignore[attr-defined]
    assert result.transfer.rename_spent is False  # type: ignore[attr-defined]
    if sddl is not None:  # the temporary file still with the old DACL, the source without
        assert seen == [(FILE_READ_DATA, 0 if sddl == ONLY_OWNER else FILE_READ_DATA)]
    assert ops.source_reads == reads  # before the copy, at C12, before the commit
    assert fingerprints(src) == before and file_id(source) == identity
    assert fingerprints(category) == theirs and a.read_descriptor_at(category) == theirs_sd
    (journal,) = journals.v2
    copies = [r for r in journal.history if r["operation"] == "copy"]
    assert [(r["status"], r["temp_state"]) for r in copies] == [*rows, ("failed", "removed")]
    assert all(r["commit"] == "not_attempted" for r in copies)  # no C14, no rename
    assert why in str(copies[-1])
    report = report_of(outcome)
    assert report.problems == () and outcome.stopped is None
    assert {(e.operation, e.state) for e in report.entries} == {("copy", OperationState.FAILED)}
