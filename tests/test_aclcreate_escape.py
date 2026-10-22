"""R3B-7B1: exceptions leaving ``create_category`` / ``create_temp`` (scripted ``Ops``)
and the ``Creation`` of a temporary file in the JSONL v2 journal.

Every failure is injected at a named event of ``test_aclcreate_fake.FakeOps``; after
every test (``invariants``) each handle was closed at most once, an ``UNKNOWN`` one
never, and only a ``CREATED_OWNED`` one was disposed. Checked each time: the very
exception is raised (or what ``release_after`` puts in its place) with the ``Creation``
attached, its state says what was established and nothing more, there was one create
call, and nothing was looked up or removed after the exception.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from test_aclcreate_fake import (
    ME,
    NEW_ID,
    PARENT,
    SOURCE,
    SOURCE_DIR,
    VOLUME,
    WIDE_DIR,
    FakeHandle,
    FakeOps,
    category,
    invariants,
    temp,
    widened,
)
from test_journal_v2 import RUN_ID, SV, TV, Stream, clock, fsyncs  # noqa: F401

from downloads_organizer import aclcreate as c
from downloads_organizer import journal_v2 as v2
from downloads_organizer.aclcreate import Created, CreateReason, Creation, Identity, Stage
from downloads_organizer.aclproof import AclReason
from downloads_organizer.journal_v2 import (
    Commit,
    JournalError,
    JournalV2,
    OperationV2,
    ReviewFlag,
    StatusV2,
    TempState,
)
from downloads_organizer.winfileops import (
    HandleCloseError,
    HandleState,
    OpenOutcome,
    ReleaseError,
    release_failures,
)
from downloads_organizer.winprims import Disposal, Disposed, Lookup, LookupKind, _Call

ERRORS: dict[str, Callable[[], BaseException]] = {
    "ValueError": lambda: ValueError("a bug"),
    "RuntimeError": lambda: RuntimeError("a bug"),
    "OSError": lambda: OSError(5, "injected"),
    "KeyboardInterrupt": lambda: KeyboardInterrupt("ctrl-c"),
    "SystemExit": lambda: SystemExit(3),
}
NOT_OS = [name for name in ERRORS if name != "OSError"]
KINDS = pytest.mark.parametrize("kind", ["dir", "file"])


@dataclass(eq=False)
class Ops(FakeOps):
    """``later``: fails an event only after the create call (the parent's query is also
    made before it); ``spend``: what ``dispose`` marks spent before it fails."""

    later: dict[str, BaseException] = field(default_factory=dict)
    spend: _Call | None = None
    open_fails: BaseException | None = None

    def hit(self, event: str) -> None:
        if event in self.later and "create:after" in self.events:
            self.events.append(event)
            raise self.later[event]
        super().hit(event)

    def dispose(self, handle, disposal: Disposal) -> Disposed:
        if self.spend is not None:
            disposal.posix, disposal.posix_spent = self.spend, True
        return super().dispose(handle, disposal)

    def open_error(self, handle) -> OSError:
        if self.open_fails is not None:
            raise self.open_fails
        return super().open_error(handle)


@pytest.fixture
def ops() -> Iterator[Ops]:
    fake = Ops()
    yield fake
    invariants(fake)


def make(fake: Ops, kind: str) -> Creation:
    return category(fake) if kind == "dir" else temp(fake)


def escape(fake: Ops, kind: str, error: BaseException) -> tuple[Creation, BaseException]:
    """Run, expect an exception, return the one ``Creation`` attached and what was raised."""
    with pytest.raises(BaseException) as raised:
        make(fake, kind)
    (creation,) = raised.value.creations  # type: ignore[attr-defined]
    assert creation.raised is error and creation.handle is None
    assert any(n.startswith("create H_") for n in raised.value.__notes__)
    return creation, raised.value


def no_second_try(fake: Ops) -> None:
    assert fake.events.count("create:before") <= 1
    assert fake.events.count("dispose") <= 1


# --- after the create call, before or during the checks --------------------------------


@dataclass(frozen=True)
class Case:
    event: str
    state: Created
    stage: Stage
    identity: bool  # the new object's identity was read before the exception
    closes: int
    later: bool = False  # injected only after the create call
    removal: bool = False  # a check failed first: the removal began


CASES = [
    Case("create:before", Created.UNKNOWN, Stage.CREATE, False, 0),
    Case("create:after", Created.UNCHECKED, Stage.CREATE, False, 1),
    Case("query:new", Created.UNCHECKED, Stage.QUERY, False, 1),
    Case("query:parent", Created.UNCHECKED, Stage.QUERY, True, 1, later=True),
    Case("sd:new", Created.UNCHECKED, Stage.DESCRIPTOR, True, 1),
    Case("authz:the new object", Created.UNCHECKED, Stage.AUTHZ, True, 1),
    Case("dispose", Created.LEFT, Stage.REMOVE, True, 1, removal=True),
]
# An OSError of a query, a descriptor read or Authz is a refusal, not an exception.
_REFUSED_BY_OSERROR = {"query:new", "query:parent", "sd:new", "authz:the new object"}


ESCAPES = [
    pytest.param(case, name, id=f"{case.event}-{name}")
    for case in CASES
    for name in ERRORS
    if not (name == "OSError" and case.event in _REFUSED_BY_OSERROR)
]


@pytest.mark.parametrize(("case", "name"), ESCAPES)
@KINDS
def test_an_exception_keeps_what_was_established_and_nothing_more(ops, kind, case, name):
    error = ERRORS[name]()
    (ops.later if case.later else ops.fail)[case.event] = error
    if case.removal:
        ops.store = widened
    creation, raised = escape(ops, kind, error)
    handle = ops.created()
    assert raised is error  # the very object, its traceback on it
    assert "hit" in [frame.name for frame in traceback.extract_tb(error.__traceback__)]
    assert creation.state is case.state and creation.stage is case.stage
    assert creation.parent == PARENT and creation.role == ("H_DIR" if kind == "dir" else "H_TMP")
    if case.identity:
        assert creation.identity == Identity(VOLUME, NEW_ID)
    else:
        assert creation.identity is None
    before = case.event == "create:before"
    assert creation.owned is not before
    assert creation.status == (None if before else 0)
    assert creation.outcome is (None if before else OpenOutcome.CREATED_OWNED)
    assert handle.closes == case.closes and handle.disposes == int(case.removal)
    assert creation.handle_state is handle.state
    assert handle.state is (HandleState.CALLING if before else HandleState.CLOSED)
    assert creation.release_errors == ()
    want = AclReason.MISMATCH if case.removal else CreateReason.EXCEPTION
    assert creation.refusal is not None and creation.refusal.reason is want
    assert "lookup" not in ops.events  # never looked up after the exception
    no_second_try(ops)


@pytest.mark.parametrize(
    ("spent", "state"),
    [
        (None, Created.LEFT),
        (_Call(()), Created.REMOVAL_UNKNOWN),  # spent, its status never captured
        (_Call((), status=0), Created.REMOVAL_UNKNOWN),
        (_Call((), status=-0x3FFF_FFDE), Created.LEFT),  # STATUS_ACCESS_DENIED: refused
    ],
    ids=["nothing-spent", "status-lost", "status-ok", "refused"],
)
@pytest.mark.parametrize("name", ["KeyboardInterrupt", "RuntimeError"])
def test_an_exception_in_the_disposition_is_left_only_if_nothing_may_have_removed_it(
    ops, spent, state, name
):
    error = ERRORS[name]()
    ops.store, ops.spend, ops.fail["dispose"] = widened, spent, error
    creation, _ = escape(ops, "file", error)
    assert creation.state is state and creation.stage is Stage.REMOVE
    assert ops.created().closes == 1 and "lookup" not in ops.events


# --- the other steps -------------------------------------------------------------------


@pytest.mark.parametrize("name", NOT_OS)
def test_an_exception_before_the_create_call_is_refused_without_a_handle(ops, name):
    error = ERRORS[name]()
    ops.fail["query:parent"] = error  # the parent's first query
    creation, _ = escape(ops, "file", error)
    assert creation.state is Created.REFUSED and creation.stage is Stage.PREPARE
    assert creation.parent is None and creation.handle_state is None
    assert ops.handles == [] and creation.refusal.reason is CreateReason.EXCEPTION


@pytest.mark.parametrize("name", NOT_OS)
def test_an_exception_judging_the_category_creates_no_temp(ops, name):
    error = ERRORS[name]()
    ops.fail["authz:the target"] = error
    creation, _ = escape(ops, "file", error)
    assert creation.state is Created.REFUSED and creation.stage is Stage.PREPARE
    assert creation.parent == PARENT and ops.handles == []


@pytest.mark.parametrize("name", NOT_OS)
def test_an_exception_closing_a_handle_not_proven_created_stays_unknown(ops, name):
    error = ERRORS[name]()
    ops.outcome, ops.fail["close:H_TMP"] = OpenOutcome.OPENED_OWNED, error
    creation, _ = escape(ops, "file", error)
    handle = ops.created()
    assert creation.state is Created.UNKNOWN and creation.stage is Stage.RELEASE
    assert creation.refusal.reason is CreateReason.NOT_PROVEN_OWN
    assert handle.closes == 1 and handle.state is HandleState.CLOSE_FAILED
    assert creation.handle_state is HandleState.CLOSE_FAILED and "dispose" not in ops.events


@pytest.mark.parametrize("name", NOT_OS)
def test_an_exception_in_the_lookup_after_an_error_status_stays_unknown(ops, name):
    error = ERRORS[name]()
    ops.outcome, ops.fail["lookup"] = OpenOutcome.NOT_CREATED_CLAIMED, error
    creation, _ = escape(ops, "file", error)
    assert creation.state is Created.UNKNOWN and creation.stage is Stage.NOT_CREATED
    assert creation.status == 0xC000_0022 and creation.lookup is None
    assert creation.refusal.reason is CreateReason.OUTCOME_UNKNOWN
    assert ops.created().closes == 0


def test_an_exception_after_a_taken_name_never_touches_the_other_object(ops):
    error = RuntimeError("open_error")
    ops.outcome, ops.open_fails = OpenOutcome.NAME_TAKEN, error
    creation, _ = escape(ops, "file", error)
    assert creation.state is Created.REFUSED and creation.stage is Stage.NOT_CREATED
    assert creation.refusal.reason is CreateReason.NAME_TAKEN and creation.identity is None
    assert ops.created().closes == 0
    assert "lookup" not in ops.events and "dispose" not in ops.events


@pytest.mark.parametrize(
    ("disposed", "event", "state"),
    [
        (Disposed.OK, "close:H_TMP", Created.REMOVAL_UNKNOWN),
        (Disposed.OK, "lookup", Created.REMOVAL_UNKNOWN),
        (Disposed.REFUSED, "close:H_TMP", Created.LEFT),
        (Disposed.UNSURE, "close:H_TMP", Created.REMOVAL_UNKNOWN),
    ],
    ids=["ok-close", "ok-lookup", "refused-close", "unsure-close"],
)
@pytest.mark.parametrize("name", NOT_OS)
def test_an_exception_after_the_disposition_is_never_called_removed(
    ops, disposed, event, state, name
):
    error = ERRORS[name]()
    ops.store, ops.disposed, ops.fail[event] = widened, disposed, error
    creation, _ = escape(ops, "file", error)
    assert creation.state is state and creation.stage is Stage.REMOVE
    assert ops.created().closes == 1  # never a second close
    assert creation.identity == Identity(VOLUME, NEW_ID)


# --- the close after the exception -----------------------------------------------------


def test_a_failed_close_after_an_error_raises_both_and_keeps_the_creation(ops):
    error, failure = ValueError("a bug"), HandleCloseError(0, "close", "x", 6)
    ops.fail["sd:new"], ops.fail["close:H_TMP"] = error, failure
    with pytest.raises(ReleaseError) as raised:
        temp(ops)
    assert raised.value.error is error and raised.value.__cause__ is error
    (creation,) = raised.value.creations  # type: ignore[attr-defined]
    assert error.creations == (creation,)  # type: ignore[attr-defined]
    assert creation.raised is error and creation.release_errors == (failure,)
    assert creation.state is Created.UNCHECKED and creation.handle_state is HandleState.CLOSE_FAILED
    assert ops.created().closes == 1


@pytest.mark.parametrize("name", ["KeyboardInterrupt", "SystemExit"])
def test_an_interrupt_with_a_failed_close_is_raised_itself(ops, name):
    interrupt, failure = ERRORS[name](), HandleCloseError(0, "close", "x", 6)
    ops.fail["authz:the new object"], ops.fail["close:H_DIR"] = interrupt, failure
    creation, raised = escape(ops, "dir", interrupt)
    assert raised is interrupt and failure in release_failures(interrupt)
    assert creation.release_errors == (failure,) and creation.state is Created.UNCHECKED


@pytest.mark.parametrize("name", ["KeyboardInterrupt", "SystemExit"])
def test_an_interrupt_of_the_close_after_an_error_is_never_lost(ops, name):
    error, interrupt = ValueError("a bug"), ERRORS[name]()
    ops.fail["sd:new"], ops.fail["close:H_TMP"] = error, interrupt
    with pytest.raises(type(interrupt)) as raised:
        temp(ops)
    assert raised.value is interrupt
    (creation,) = interrupt.creations  # type: ignore[attr-defined]
    assert error.creations == (creation,)  # type: ignore[attr-defined]
    assert creation.raised is error and creation.release_errors == (interrupt,)
    assert ops.created().closes == 1


def test_an_interrupt_right_after_every_check_passed_closes_and_hands_nothing_over(ops):
    """The window between the last check and the return: no caller ever receives the
    open handle, so it is closed once and the object is ``UNCHECKED`` (it is there)."""
    creation = temp(ops)
    handle = creation.handle
    interrupt = KeyboardInterrupt()
    c._escaped(ops, creation, handle, interrupt)  # type: ignore[arg-type]
    assert creation.state is Created.UNCHECKED and creation.handle is None
    assert handle.closes == 1 and interrupt.creations == (creation,)  # type: ignore[union-attr,attr-defined]


def test_a_calling_handle_with_a_status_is_unknown_never_unchecked():
    creation = Creation(Created.UNKNOWN)
    handle = FakeHandle(Path("x"), "H_TMP", HandleState.CALLING, status=0)
    c._classified(creation, handle)  # type: ignore[arg-type]
    assert creation.state is Created.UNKNOWN and not creation.owned


def test_a_creation_is_attached_once():
    exc, creation = ValueError(), Creation(Created.UNKNOWN, role="H_TMP", name="t")
    assert c.attach_creation(c.attach_creation(exc, creation), creation) is exc
    assert exc.creations == (creation,) and len(exc.__notes__) == 1  # type: ignore[attr-defined]


# --- the JSONL v2 record ---------------------------------------------------------------

TEMP = v2.temp_name(RUN_ID, 2)  # the intent is record 2, right after run/started
SRC, DST = Path("D:/Inbox/a.pdf"), Path("E:/Sorted/PDF/a.pdf")
SOURCE_ID, SIZE = 0x0123456789ABCDEF0123456789ABCDEF, 10


def journal(stream: Stream | None = None) -> JournalV2:
    writer = JournalV2(stream or Stream(), run_id=RUN_ID, clock=clock)
    writer.write(
        OperationV2.RUN, StatusV2.STARTED, source=Path("D:/Inbox"), target=Path("E:/Sorted"),
        reason="items=1 copies=1", source_volume=SV, target_volume=TV,
    )  # fmt: skip
    writer.write(
        OperationV2.COPY, StatusV2.INTENT, source=SRC, target=DST, source_volume=SV,
        target_volume=TV, temp=TEMP, size=SIZE, source_id=SOURCE_ID,
        commit=Commit.NOT_ATTEMPTED, temp_state=TempState.NONE,
    )  # fmt: skip
    return writer


def record(writer: JournalV2, creation: Creation) -> c.TempJournaled:
    return c.journal_temp(
        writer, creation, intent=2, source=SRC, target=DST, source_volume=SV,
        target_volume=TV, temp=TEMP, size=SIZE, source_id=SOURCE_ID,
    )  # fmt: skip


def created_temp(fake: Ops) -> Creation:
    """``create_temp`` under the journal's name; an exception's ``Creation`` if one."""
    try:
        return c.create_temp(fake, fake.parent, PARENT, TEMP, SOURCE, SOURCE_DIR, ME)  # type: ignore[arg-type]
    except BaseException as exc:  # the test looks at what it carries
        (creation,) = exc.creations  # type: ignore[attr-defined]
        return creation


def _taken(f: Ops) -> None:
    f.outcome = OpenOutcome.NAME_TAKEN


def _absent(f: Ops) -> None:
    f.outcome = OpenOutcome.NOT_CREATED_CLAIMED


def _wide(f: Ops) -> None:
    f.parent.descriptor = WIDE_DIR.raw


def _removed(f: Ops) -> None:
    f.store = widened


def _left(f: Ops) -> None:
    f.store, f.disposed = widened, Disposed.REFUSED


def _unchecked(f: Ops) -> None:
    f.fail["sd:new"] = KeyboardInterrupt()


def _lost(f: Ops) -> None:
    f.outcome = OpenOutcome.UNCERTAIN


def _cut(f: Ops) -> None:
    f.fail["create:before"] = SystemExit(1)


def _found(f: Ops) -> None:
    f.outcome, f.lookup = OpenOutcome.NOT_CREATED_CLAIMED, Lookup(LookupKind.FOUND, NEW_ID + 5)


def _unsure(f: Ops) -> None:
    f.store, f.disposed = widened, Disposed.UNSURE


def _created(f: Ops) -> None:
    pass


MISMATCH = str(AclReason.MISMATCH)
# setup, Created, status, temp_state, reason, the observed identity in diagnostics
JOURNALED = [
    (_created, Created.CREATED, StatusV2.STAGED, TempState.PRESENT, None, False),
    (_taken, Created.REFUSED, StatusV2.SKIPPED, TempState.NONE, v2.TEMP_NAME_TAKEN, False),
    (_absent, Created.REFUSED, StatusV2.FAILED, TempState.NONE, "create_failed", False),
    (_wide, Created.REFUSED, StatusV2.FAILED, TempState.NONE, "directory_wider", False),
    (_removed, Created.REMOVED, StatusV2.FAILED, TempState.REMOVED, MISMATCH, False),
    (_left, Created.LEFT, StatusV2.FAILED, TempState.LEFT, MISMATCH, True),
    (_unchecked, Created.UNCHECKED, StatusV2.FAILED, TempState.LEFT, "exception", True),
    (_lost, Created.UNKNOWN, StatusV2.FAILED, TempState.UNKNOWN, v2.TEMP_CREATE_UNCERTAIN, False),
    (_cut, Created.UNKNOWN, StatusV2.FAILED, TempState.UNKNOWN, v2.TEMP_CREATE_UNCERTAIN, False),
    (_found, Created.UNKNOWN, StatusV2.FAILED, TempState.UNKNOWN, v2.TEMP_CREATE_UNCERTAIN, False),
    (_unsure, Created.REMOVAL_UNKNOWN, StatusV2.FAILED, TempState.UNKNOWN, MISMATCH, True),
]


@pytest.mark.usefixtures("fsyncs")
@pytest.mark.parametrize(
    ("setup", "state", "status", "temp_state", "reason", "observed"),
    JOURNALED,
    ids=[s[0].__name__.strip("_") for s in JOURNALED],
)
def test_each_state_is_one_record_the_v2_reader_accepts(
    ops, setup, state, status, temp_state, reason, observed
):
    setup(ops)
    creation = created_temp(ops)
    assert creation.state is state
    writer = journal()
    result = record(writer, creation)
    assert result.sequence == 3 and result.journal_error is None
    assert result.may_continue is (status is StatusV2.STAGED)
    written = writer.history[-1]
    assert written["status"] == status and written["temp_state"] == temp_state
    assert written["reason"] == reason and written["commit"] == "not_attempted"
    assert v2.record_errors(dict(written)) == []
    report = v2.analyze_v2(writer.history, run_id=RUN_ID)
    assert report.problems == () and not report.review_whole_journal
    (entry,) = report.entries
    assert entry.state is not v2.OperationState.DONE
    if status is StatusV2.STAGED:
        assert written["temp_id"] == f"{NEW_ID:032x}" and written["diagnostics"] is None
        if creation.handle is not None:
            ops.close(creation.handle)
        return
    assert written["temp_id"] is None  # the grammar: null until staged
    diagnostics = written["diagnostics"]
    if temp_state in (TempState.LEFT, TempState.UNKNOWN):
        assert ReviewFlag.TEMP_LEFT in entry.flags
        assert diagnostics["delete_pending"] is None  # never claimed either way
        want = f"{NEW_ID:032x}" if observed else None
        assert diagnostics["temp_id_observed"] == want
        assert str(creation.stage) in diagnostics["temp_handle"]
    else:
        assert diagnostics is None and ReviewFlag.TEMP_LEFT not in entry.flags
    if reason == v2.TEMP_CREATE_UNCERTAIN:
        assert any("never owned" in note for note in entry.notes)


@pytest.mark.usefixtures("fsyncs")
def test_removed_is_written_only_for_a_proven_removal(ops):
    for setup, state, *_ in JOURNALED:
        fake = Ops()
        setup(fake)
        creation = created_temp(fake)
        written = c.temp_record(creation)
        assert (written.temp_state is TempState.REMOVED) is (state is Created.REMOVED)
        assert written.status not in (StatusV2.DONE, StatusV2.COMMIT_ATTEMPTED)
        if creation.handle is not None:
            fake.close(creation.handle)
        invariants(fake)


class Broken(Stream):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error
        self.armed = False

    def write(self, data: object) -> int:  # type: ignore[override]
        if self.armed:
            raise self.error
        return super().write(data)


@pytest.mark.usefixtures("fsyncs")
def test_a_failed_journal_write_forbids_going_on(ops):
    creation = created_temp(ops)
    assert creation.state is Created.CREATED
    stream = Broken(OSError(28, "disk full"))
    writer = journal(stream)
    stream.armed = True
    result = record(writer, creation)
    assert isinstance(result.journal_error, JournalError) and result.sequence is None
    assert not result.may_continue and writer.failed
    assert creation.handle is not None and creation.handle.state is HandleState.OPEN
    ops.close(creation.handle)  # journal_temp itself closes and removes nothing


@pytest.mark.usefixtures("fsyncs")
def test_an_interrupt_of_the_journal_write_is_raised_with_the_creation(ops):
    ops.store = widened
    creation = created_temp(ops)
    interrupt = KeyboardInterrupt()
    stream = Broken(interrupt)
    writer = journal(stream)
    stream.armed = True
    with pytest.raises(KeyboardInterrupt) as raised:
        record(writer, creation)
    assert raised.value is interrupt and interrupt.creations == (creation,)  # type: ignore[attr-defined]
    assert writer.state is v2.WriterState.UNTRUSTED


@pytest.mark.usefixtures("fsyncs")
def test_a_category_has_no_v2_record_and_a_wrong_name_is_refused(ops):
    directory = category(ops)
    with pytest.raises(AssertionError):
        c.temp_record(directory)  # mkdir records cannot carry its identity or state
    ops.close(directory.handle)
    other = temp(Ops())  # "a.partial": not the intent's temporary name
    with pytest.raises(AssertionError):
        record(journal(), other)


# --- R3B-7B2A: preparing the record is guarded like writing it -------------------------


@dataclass
class Journaled:
    """A journal after run/started and the intent, and what it holds right then."""

    stream: Stream
    writer: JournalV2
    data: bytes
    writes: int

    def unchanged(self) -> None:
        """Nothing was written for the temporary file and the writer takes records."""
        assert self.stream.getvalue() == self.data and self.stream.writes == self.writes
        assert len(self.writer.history) == 2 and self.writer.state is v2.WriterState.READY


def journaled() -> Journaled:
    stream = Stream()
    writer = journal(stream)
    return Journaled(stream, writer, stream.getvalue(), stream.writes)


def frames(exc: BaseException) -> list[str]:
    return [frame.name for frame in traceback.extract_tb(exc.__traceback__)]


def raises_in_temp_record(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    def broken_temp_record(creation: Creation) -> c.TempRecord:
        raise error

    monkeypatch.setattr(c, "temp_record", broken_temp_record)


PREPARATION_ERRORS: dict[str, Callable[[], BaseException]] = {
    **ERRORS,
    "AssertionError": lambda: AssertionError("a broken precondition"),
    "KeyError": lambda: KeyError("no state"),
    "JournalError": lambda: JournalError("not of the write"),
}


@pytest.mark.usefixtures("fsyncs")
@pytest.mark.parametrize("error", list(PREPARATION_ERRORS), ids=list(PREPARATION_ERRORS))
def test_an_error_preparing_the_record_is_raised_as_itself_and_nothing_written(
    ops, monkeypatch, error
):
    creation = created_temp(ops)
    assert creation.state is Created.CREATED
    journal_before = journaled()
    raised = PREPARATION_ERRORS[error]()
    raises_in_temp_record(monkeypatch, raised)
    with pytest.raises(type(raised)) as caught:
        record(journal_before.writer, creation)
    assert caught.value is raised and type(caught.value) is type(raised)
    assert "broken_temp_record" in frames(raised)  # the original traceback is kept
    assert "journal_temp" in frames(raised)
    assert raised.creations == (creation,)  # type: ignore[attr-defined]
    assert len(raised.__notes__) == 1
    journal_before.unchanged()
    assert creation.state is Created.CREATED and creation.raised is None
    assert creation.handle is not None and creation.handle.state is HandleState.OPEN
    assert creation.disposal is None and creation.lookup is None
    ops.close(creation.handle)  # journal_temp itself closes and removes nothing
    invariants(ops)


@pytest.mark.usefixtures("fsyncs")
def test_a_creation_already_on_the_preparation_error_is_not_attached_twice(ops, monkeypatch):
    creation = created_temp(ops)
    other = Creation(Created.UNKNOWN, role="H_TMP", name="another")
    raised = c.attach_creation(c.attach_creation(ValueError("again"), other), creation)
    raises_in_temp_record(monkeypatch, raised)
    journal_before = journaled()
    with pytest.raises(ValueError) as caught:
        record(journal_before.writer, creation)
    assert caught.value is raised
    assert raised.creations == (other, creation) and len(raised.__notes__) == 2  # type: ignore[attr-defined]
    journal_before.unchanged()
    ops.close(creation.handle)


@pytest.mark.usefixtures("fsyncs")
@pytest.mark.parametrize("kind", ["category", "wrong name"])
def test_a_record_v2_cannot_carry_is_refused_before_any_write(ops, kind):
    if kind == "category":
        made = creation = category(ops)
        creation.name = TEMP  # the intent's name: refused as an H_DIR all the same
    else:
        made = creation = temp(ops)  # "a.partial", not the intent's temporary name
    journal_before = journaled()
    with pytest.raises(AssertionError) as caught:
        record(journal_before.writer, creation)
    assert caught.value.creations == (creation,)  # type: ignore[attr-defined]
    assert ("temp_record" in frames(caught.value)) is (kind == "category")
    journal_before.unchanged()
    ops.close(made.handle)


@pytest.mark.usefixtures("fsyncs")
@pytest.mark.parametrize("error", ["ValueError", "KeyboardInterrupt", "SystemExit"])
def test_a_journal_kept_from_a_bad_preparation_still_takes_the_good_record(ops, monkeypatch, error):
    """The failed preparation wrote nothing: the very next record is sequence 3."""
    creation = created_temp(ops)
    journal_before = journaled()
    with monkeypatch.context() as patch:
        raises_in_temp_record(patch, ERRORS[error]())
        with pytest.raises((ValueError, KeyboardInterrupt, SystemExit)):
            record(journal_before.writer, creation)
    journal_before.unchanged()
    result = record(journal_before.writer, creation)
    assert result.sequence == 3 and result.may_continue
    assert [r["sequence"] for r in journal_before.writer.history] == [1, 2, 3]
    ops.close(creation.handle)


@pytest.mark.usefixtures("fsyncs")
def test_after_a_failed_write_nothing_more_is_written_or_allowed(ops):
    creation = created_temp(ops)
    stream = Broken(OSError(28, "disk full"))
    writer = journal(stream)
    stream.armed = True
    first = record(writer, creation)
    assert first.journal_error is not None and not first.may_continue
    stream.armed = False  # the disk would take it again: the writer does not
    writes = stream.writes
    second = record(writer, creation)
    assert second.journal_error is not None and second.sequence is None
    assert not second.may_continue and stream.writes == writes
    assert len(writer.history) == 2 and writer.state is v2.WriterState.FAILED
    ops.close(creation.handle)


@pytest.mark.usefixtures("fsyncs")
def test_the_staged_record_reads_back_strictly_from_a_file(ops, tmp_path):
    """The successful path, through the strict file readers (v2 by its first record)."""
    creation = created_temp(ops)
    stream = Stream()
    writer = journal(stream)
    result = record(writer, creation)
    assert result.sequence == 3 and result.may_continue and result.journal_error is None
    path = tmp_path / f"run-20260101T000000000000Z-{RUN_ID}.jsonl"
    path.write_bytes(stream.getvalue())
    for report in (v2.read_journal_v2(path), v2.read_any_journal(path)):
        assert isinstance(report, v2.JournalReportV2)
        assert report.problems == () and report.unreadable == ()
        assert not report.review_whole_journal and len(report.records) == 3
        (entry,) = report.entries
        # cut off after staged: the temporary file is present and the copy unfinished
        assert entry.state is v2.OperationState.UNCERTAIN
        assert ReviewFlag.TEMP_LEFT in entry.flags
    staged = v2.read_journal_v2(path).records[-1]
    assert staged == writer.history[-1] and v2.record_errors(dict(staged)) == []  # type: ignore[arg-type]
    assert staged["status"] == "staged" and staged["temp_id"] == f"{NEW_ID:032x}"  # type: ignore[index]
    ops.close(creation.handle)
