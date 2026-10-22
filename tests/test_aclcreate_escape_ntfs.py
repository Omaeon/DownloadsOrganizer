"""R3B-7B1 on the NTFS of ``tmp_path`` (C:): exceptions after the real ``NtCreateFile``.

An exception is injected around one real call of ``NtOps``; what the ``Creation``
attached to it says is compared with what is really there: the object exists (or
not), its file id is the identity recorded (if one was), the creating handle is
closed (the file opens again without a sharing violation), nothing else in the target
changed, the source neither, and the ``sentinel`` outside ``tmp_path`` stays as it was.
The ``acquisitions`` stand checks that every handle is closed once and none leaks.
Only objects under ``tmp_path`` are created; the test removes nothing by itself.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable
from pathlib import Path

import pytest
import test_aclcreate_ntfs as ntfs
from test_aclcreate_ntfs import (
    CATEGORY,
    TEMP,
    category,
    close,
    held,
    identity,
    state_of,
    temp,
)
from test_aclproof import (
    PRIVATE,
    descriptor,
    observe,
    sentinel,  # noqa: F401  (a fixture: what stays as it is outside tmp_path)
    tree,
    unlocked,  # noqa: F401  (autouse: tmp_path stays removable)
)
from test_journal_v2 import RUN_ID, SV, TV, Stream, clock, fsyncs  # noqa: F401

from downloads_organizer import aclcreate as c
from downloads_organizer import journal_v2 as v2
from downloads_organizer import winfileops as w
from downloads_organizer.aclcreate import Created, CreateReason, Stage
from downloads_organizer.journal_v2 import Commit, OperationV2, StatusV2, TempState
from downloads_organizer.winfileops import HandleState, OpenOutcome, ReleaseError

pytestmark = [pytest.mark.windows_only, pytest.mark.real_ntfs]

# the fixtures of test_aclcreate_ntfs: the creator, the spy, the sentinel directory
me, creates, outside = ntfs.me, ntfs.creates, ntfs.outside

ERRORS: dict[str, Callable[[], BaseException]] = {
    "ValueError": lambda: ValueError("a bug"),
    "KeyboardInterrupt": lambda: KeyboardInterrupt("ctrl-c"),
    "SystemExit": lambda: SystemExit(3),
}
NEW_ROLES = ("H_DIR", "H_TMP")


class Throw(c.NtOps):
    """The real calls; ``error`` is raised once at ``step`` for the new object ``target``.

    ``unreadable``: reading the new object's descriptor fails with an ``OSError`` (a
    refusal, so the removal begins) before a ``dispose-*`` step."""

    def __init__(
        self, step: str, error: BaseException, target: Path, *, unreadable: bool = False
    ) -> None:
        self.step, self.error, self.target, self.unreadable = step, error, target, unreadable
        self.handles: list[w.Handle] = []
        self.steps: list[str] = []

    def fire(self, step: str) -> None:
        self.steps.append(step)
        if step == self.step:
            self.step = ""  # once
            raise self.error

    def new(self, path: Path, role: str) -> w.Handle:
        handle = super().new(path, role)
        self.handles.append(handle)
        return handle

    def created(self) -> w.Handle:
        (handle,) = (h for h in self.handles if h.role in NEW_ROLES)
        return handle

    def create_with(self, handle, parent, name, descriptor) -> OpenOutcome:
        outcome = super().create_with(handle, parent, name, descriptor)
        self.fire("create-after")
        return outcome

    def open_error(self, handle) -> OSError:
        self.fire("open-error")
        return super().open_error(handle)

    def query(self, handle):
        if handle.path == self.target:
            self.fire("query")
        elif any(h.path == self.target for h in self.handles):
            self.fire("query-parent")  # the parent's query after the create call
        return super().query(handle)

    def read_descriptor(self, handle) -> bytes:
        if handle.path == self.target:
            self.fire("descriptor")
            if self.unreadable:
                raise OSError(5, "injected: the new object's descriptor is not read")
        return super().read_descriptor(handle)

    def effective_access(self, descriptor, sid, what):
        if what == "the new object":
            self.fire("authz")
        return super().effective_access(descriptor, sid, what)

    def lookup_entry(self, handle, directory, name):
        self.fire("lookup")
        return super().lookup_entry(handle, directory, name)

    def dispose(self, handle, disposal):
        self.fire("dispose-before")
        result = super().dispose(handle, disposal)
        self.fire("dispose-after")
        return result


# step, Created, Stage, the identity was read, the object is still there
STEPS = [
    ("create-after", Created.UNCHECKED, Stage.CREATE, False, True),
    ("query", Created.UNCHECKED, Stage.QUERY, False, True),
    ("query-parent", Created.UNCHECKED, Stage.QUERY, True, True),
    ("descriptor", Created.UNCHECKED, Stage.DESCRIPTOR, True, True),
    ("authz", Created.UNCHECKED, Stage.AUTHZ, True, True),
    ("dispose-before", Created.LEFT, Stage.REMOVE, True, True),
    ("dispose-after", Created.REMOVAL_UNKNOWN, Stage.REMOVE, True, False),
]


def run(what: str, tmp_path: Path, me, ops_for: Callable[[Path], Throw]):
    """``create_category`` or ``create_temp`` (in a new category) with ``ops_for(target)``;
    returns the exception info, the ``Throw`` ops, the target path and the source."""
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    before = state_of(source)
    parent = held(dst)
    cat = None
    target = dst / CATEGORY if what == "category" else dst / CATEGORY / TEMP
    ops = ops_for(target)
    try:
        if what == "temp":
            cat = category(c.NtOps(), parent, descriptor(source.parent), me)
        with pytest.raises(BaseException) as raised:
            if what == "category":
                category(ops, parent, descriptor(source.parent), me)
            else:
                temp(ops, cat, source, me)
    finally:
        close(cat, parent)
    assert state_of(source) == before
    assert [x.name for x in dst.iterdir()] == ([CATEGORY] if target.exists() or cat else [])
    return raised, ops, target


# FILE_READ_DATA / FILE_LIST_DIRECTORY: an access the share check applies to (an open
# for attributes only is never a sharing violation, so it would prove nothing).
_DATA = 0x0001 | w.FILE_READ_ATTRIBUTES


def exclusive(target: Path, what: str) -> w.Handle:
    """``target`` opened for data, sharing nothing: ``ERROR_SHARING_VIOLATION`` (32)
    while any other handle to it is open."""
    if what == "category":
        return held(target, access=_DATA, share=0)
    directory = held(target.parent)
    try:
        handle, _ = w.open_relative(directory, target.name, directory=False, access=_DATA, share=0)
    finally:
        directory.close()
    return handle


def reopened(target: Path, what: str) -> c.Identity:
    """The object opened again exclusively: so the creating handle is closed."""
    handle = exclusive(target, what)
    try:
        return identity(handle)
    finally:
        handle.close()


@pytest.mark.parametrize("name", list(ERRORS))
@pytest.mark.parametrize(
    ("step", "state", "stage", "known", "there"), STEPS, ids=[s[0] for s in STEPS]
)
@pytest.mark.parametrize("what", ["category", "temp"])
def test_an_exception_after_the_create_call_is_what_is_really_there(
    tmp_path, me, record_property, acquisitions, outside, what, step, state, stage, known,
    there, name,
):  # fmt: skip
    error = ERRORS[name]()
    removal = step.startswith("dispose")
    raised, ops, target = run(
        what, tmp_path, me, lambda target: Throw(step, error, target, unreadable=removal)
    )
    assert raised.value is error  # the very object, with its traceback
    (creation,) = raised.value.creations  # type: ignore[attr-defined]
    observe(record_property, f"{what} {step} {name}", (creation.state, creation.describe()))
    assert creation.raised is error and creation.handle is None
    assert creation.state is state and creation.stage is stage
    assert creation.owned and creation.status == 0 and creation.release_errors == ()
    assert ops.steps.count("dispose-before") == int(removal) and "lookup" not in ops.steps
    created = ops.created()
    assert created.state is HandleState.CLOSED and creation.handle_state is HandleState.CLOSED
    if removal:
        assert creation.refusal.reason is not CreateReason.EXCEPTION  # the check failed first
        assert creation.disposal is not None
        spent = creation.disposal.posix_spent or creation.disposal.classic_spent
        assert spent is (step == "dispose-after")
    else:
        assert creation.refusal.reason is CreateReason.EXCEPTION
    assert target.exists() is there
    if there:
        found = reopened(target, what)
        if known:
            assert creation.identity == found  # the very object, by its file id
        else:
            assert creation.identity is None


def test_a_failed_close_after_an_exception_keeps_both_and_the_object(
    tmp_path, me, acquisitions, outside
):
    """``CloseHandle`` fails after an exception: ``ReleaseError`` of both, the handle
    ``CLOSE_FAILED`` and still open (the file cannot be opened: sharing violation); the
    stand closes it on teardown, never ``Handle.close``."""
    error = ValueError("a bug")
    acquisitions.select = lambda owner: getattr(owner, "role", None) == "H_TMP"
    raised, _, target = run("temp", tmp_path, me, lambda t: Throw("descriptor", error, t))
    assert isinstance(raised.value, ReleaseError) and raised.value.error is error
    (creation,) = error.creations  # type: ignore[attr-defined]
    assert raised.value.creations == (creation,)  # type: ignore[attr-defined]
    assert creation.state is Created.UNCHECKED and creation.raised is error
    (failure,) = creation.release_errors
    assert isinstance(failure, w.HandleCloseError) and failure.winerror == 31
    assert creation.handle_state is HandleState.CLOSE_FAILED
    with pytest.raises(OSError) as still:
        close(exclusive(target, "temp"))
    assert still.value.winerror == 32  # ERROR_SHARING_VIOLATION: the handle is still open


def test_a_failed_close_after_an_error_raises_release_error(tmp_path, me, acquisitions):
    error = RuntimeError("a bug")
    acquisitions.select = lambda owner: getattr(owner, "role", None) == "H_DIR"
    raised, ops, _ = run("category", tmp_path, me, lambda t: Throw("authz", error, t))
    assert isinstance(raised.value, ReleaseError) and raised.value.error is error
    (creation,) = raised.value.creations  # type: ignore[attr-defined]
    assert error.creations == (creation,)  # type: ignore[attr-defined]
    assert ops.created().state is HandleState.CLOSE_FAILED


@pytest.mark.parametrize("name", ["KeyboardInterrupt", "SystemExit"])
def test_an_interrupt_of_the_close_after_an_error_is_raised(
    tmp_path, me, acquisitions, outside, name
):
    """The stand really closes the handle, then raises: the interrupt wins and carries
    the creation; the object is there, closed (opens again)."""
    error, interrupt = ValueError("a bug"), ERRORS[name]()
    acquisitions.select = lambda owner: getattr(owner, "role", None) == "H_TMP"
    acquisitions.interrupt = interrupt
    raised, _, target = run("temp", tmp_path, me, lambda t: Throw("query-parent", error, t))
    assert raised.value is interrupt
    (creation,) = interrupt.creations  # type: ignore[attr-defined]
    assert creation.raised is error and creation.release_errors == (interrupt,)
    assert creation.state is Created.UNCHECKED and creation.identity == reopened(target, "temp")


@pytest.mark.parametrize("name", ["KeyboardInterrupt", "SystemExit"])
@pytest.mark.parametrize("what", ["category", "temp"])
def test_a_lost_create_status_carries_an_unknown_creation(
    tmp_path, me, record_property, creates, outside, what, name
):
    """The status of the real ``NtCreateFile`` never reaches ``create_with``: the
    creation is ``UNKNOWN`` with no identity, nothing looked up, closed or removed —
    though the object is there. Only the test closes the raw value, for its cleanup."""
    interrupt = ERRORS[name]()
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    parent = held(dst)
    cat = None
    target = dst / CATEGORY if what == "category" else dst / CATEGORY / TEMP
    ops = Throw("", interrupt, target)
    try:
        if what == "temp":
            cat = category(c.NtOps(), parent, descriptor(source.parent), me)
        creates.interrupt = interrupt
        with pytest.raises(type(interrupt)) as raised:
            if what == "category":
                category(ops, parent, descriptor(source.parent), me)
            else:
                temp(ops, cat, source, me)
        assert raised.value is interrupt
        (creation,) = interrupt.creations  # type: ignore[attr-defined]
        observe(record_property, f"lost {what}", creation.describe())
        assert creation.state is Created.UNKNOWN and creation.stage is Stage.CREATE
        assert creation.identity is None and not creation.owned and creation.status is None
        assert creation.handle_state is HandleState.UNKNOWN
        assert creation.refusal.reason is CreateReason.EXCEPTION
        assert ops.steps == [] and [h.role for h in ops.handles] == [ops.created().role]
        assert target.exists()  # it is there, and nobody claims it was removed
    finally:
        if ops.handles and ops.handles[-1].state is HandleState.UNKNOWN and creates.given:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            assert kernel32.CloseHandle(creates.given[-1].out)  # the test's own cleanup
        close(cat, parent)


@pytest.mark.parametrize("what", ["category", "temp"])
def test_an_exception_after_a_taken_name_leaves_their_object_as_it_was(
    tmp_path, me, acquisitions, outside, what
):
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    parent = held(dst)
    cat = None
    error = ValueError("a bug")
    try:
        if what == "category":
            theirs = dst / CATEGORY
            theirs.mkdir()
            (theirs / "keep.txt").write_bytes(b"theirs")
        else:
            cat = category(c.NtOps(), parent, descriptor(source.parent), me)
            theirs = dst / CATEGORY / TEMP
            theirs.write_bytes(b"theirs")
        before = (theirs.stat().st_ino, descriptor(theirs).raw, sorted(dst.rglob("*")))
        ops = Throw("open-error", error, theirs)
        with pytest.raises(ValueError) as raised:
            if what == "category":
                category(ops, parent, descriptor(source.parent), me)
            else:
                temp(ops, cat, source, me)
        assert raised.value is error
        (creation,) = error.creations  # type: ignore[attr-defined]
        assert creation.state is Created.REFUSED and creation.stage is Stage.NOT_CREATED
        assert creation.refusal.reason is CreateReason.NAME_TAKEN and creation.identity is None
        assert ops.created().state is HandleState.NOT_OPENED
        assert ops.steps == ["create-after", "open-error"]  # no lookup, no disposition
    finally:
        close(cat, parent)
    assert (theirs.stat().st_ino, descriptor(theirs).raw, sorted(dst.rglob("*"))) == before
    kept = theirs / "keep.txt" if what == "category" else theirs
    assert kept.read_bytes() == b"theirs"


@pytest.mark.usefixtures("fsyncs")
def test_a_real_object_left_by_an_exception_is_journaled_as_left(
    tmp_path, me, acquisitions, outside
):
    """The JSONL v2 record of a real ``UNCHECKED`` temp: ``failed`` / ``left``, its
    observed file id in ``diagnostics``, ``temp_id`` null; the reader takes it."""
    name = v2.temp_name(RUN_ID, 2)
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    parent = held(dst)
    cat = None
    error = KeyboardInterrupt()
    try:
        cat = category(c.NtOps(), parent, descriptor(source.parent), me)
        ops = Throw("authz", error, dst / CATEGORY / name)
        with pytest.raises(KeyboardInterrupt):
            temp(ops, cat, source, me, name=name)
    finally:
        close(cat, parent)
    (creation,) = error.creations  # type: ignore[attr-defined]
    found = reopened(dst / CATEGORY / name, "temp")
    assert creation.identity == found
    writer = v2.JournalV2(Stream(), run_id=RUN_ID, clock=clock)
    src, target = Path("D:/Inbox/a.pdf"), Path("E:/Sorted/PDF/a.pdf")
    writer.write(
        OperationV2.RUN, StatusV2.STARTED, source=src.parent, target=target.parent,
        reason="items=1 copies=1", source_volume=SV, target_volume=TV,
    )  # fmt: skip
    writer.write(
        OperationV2.COPY, StatusV2.INTENT, source=src, target=target, source_volume=SV,
        target_volume=TV, temp=name, size=7, source_id=1, commit=Commit.NOT_ATTEMPTED,
        temp_state=TempState.NONE,
    )  # fmt: skip
    result = c.journal_temp(
        writer, creation, intent=2, source=src, target=target, source_volume=SV,
        target_volume=TV, temp=name, size=7, source_id=1,
    )  # fmt: skip
    assert result.journal_error is None and not result.may_continue
    written = writer.history[-1]
    assert (written["status"], written["temp_state"], written["temp_id"]) == (
        StatusV2.FAILED, TempState.LEFT, None,
    )  # fmt: skip
    assert written["diagnostics"]["temp_id_observed"] == f"{found.file_id:032x}"
    assert "disposition not attempted" in written["diagnostics"]["cleanup"]  # no removal tried
    report = v2.analyze_v2(writer.history, run_id=RUN_ID)
    assert report.problems == () and v2.ReviewFlag.TEMP_LEFT in report.entries[0].flags
