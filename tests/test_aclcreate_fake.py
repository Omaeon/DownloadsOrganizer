"""R3B-7A: ``aclcreate`` over a scripted ``Ops``: no NT call, no file, no Authz.

The fake holds one parent directory handle and records every call. The descriptor the
object is created with is captured from the create call itself; what "NTFS" then stores
is the given DACL with the creator as owner (``stored``), unless a test changes it. The
fake Authz grants an ALLOW-only DACL's masks for the SID itself and Everyone (the owner
gets ``READ_CONTROL | WRITE_DAC`` without an OWNER RIGHTS ACE). Failures and interrupts
are injected at named events. After every test (``invariants``) each handle was closed
at most once, an ``UNKNOWN`` one never, and only a ``CREATED_OWNED`` one was disposed.
"""

from __future__ import annotations

import ast
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from test_aclproof import acl, packed, raw_ace

from downloads_organizer import aclcreate as c
from downloads_organizer import aclproof as a
from downloads_organizer import features, runner
from downloads_organizer.aclcreate import Created, CreateReason, Creation, Identity
from downloads_organizer.aclproof import Ace, AclReason, Derived, Descriptor, Sid
from downloads_organizer.winfileops import (
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_REPARSE_POINT,
    HandleCloseError,
    HandleInfo,
    HandleState,
    HandleUnknownError,
    OpenOutcome,
    release_failures,
)
from downloads_organizer.winprims import Disposal, Disposed, Lookup, LookupKind

FA, FR = a.FILE_ALL_ACCESS, 0x120089
ALLOW, DENY = a.ACCESS_ALLOWED_ACE_TYPE, a.ACCESS_DENIED_ACE_TYPE
OICI = a.OBJECT_INHERIT_ACE | a.CONTAINER_INHERIT_ACE
ME = Sid.parse("S-1-5-21-1-2-3-1001")
BOB = Sid.parse("S-1-5-21-1-2-3-1002")
EVERYONE, SYSTEM = Sid.parse("S-1-1-0"), Sid.parse("S-1-5-18")
OWNER_RIGHTS = Sid.parse("S-1-3-4")
ROOT = Path("C:/fake/dst")
VOLUME, PARENT_ID, NEW_ID = 0x1234_5678_9ABC_DEF0, 100, 200
PARENT = Identity(VOLUME, PARENT_ID)


def sd(*aces: Ace | bytes, owner: Sid | None = ME, **kw: object) -> Descriptor:
    return Descriptor.parse(packed(owner, acl(*aces), **kw))  # type: ignore[arg-type]


SOURCE_DIR = sd(Ace(ALLOW, OICI, FA, ME), Ace(ALLOW, OICI, FA, SYSTEM), control=a.SE_DACL_PROTECTED)
SOURCE = sd(Ace(ALLOW, 0, FA, ME), Ace(ALLOW, 0, FR, SYSTEM))
WIDE_DIR = sd(
    Ace(ALLOW, OICI, FA, ME), Ace(ALLOW, OICI, FA, SYSTEM), Ace(ALLOW, OICI, FR, EVERYONE)
)
CONDITIONAL = raw_ace(0x9, 0, struct.pack("<I", FR) + EVERYONE.data)  # ALLOW_CALLBACK
LOW_LABEL = acl(raw_ace(0x11, 0, struct.pack("<I", 1) + Sid.parse("S-1-16-4096").data))


def stored(given: bytes, owner: Sid = ME) -> bytes:
    """What NTFS keeps of a descriptor given on create: its DACL, the creator as owner."""
    parsed = Descriptor.parse(given)
    return packed(owner, acl(*parsed.dacl or ()), group=owner, control=parsed.control & 0x1000)


def info(
    *, directory: bool, file_id: int = NEW_ID, volume: int = VOLUME, attributes: int | None = None,
    reparse_tag: int = 0, size: int = 0, links: int = 1, delete_pending: bool = False,
) -> HandleInfo:  # fmt: skip
    if attributes is None:
        attributes = FILE_ATTRIBUTE_DIRECTORY if directory else 0x20
    return HandleInfo(
        volume, file_id, attributes, reparse_tag, size, 0, links, delete_pending, directory
    )


@dataclass(eq=False)
class FakeHandle:
    path: Path
    role: str
    state: HandleState = HandleState.NEW
    outcome: OpenOutcome | None = None
    unreliable: bool = False
    status: int | None = None
    close_error: BaseException | None = None
    closes: int = 0
    disposes: int = 0
    given: bytes | None = None  # the descriptor of the create call
    info: HandleInfo | None = None
    descriptor: bytes | None = None


_UNKNOWN = {
    OpenOutcome.UNCERTAIN: None,
    OpenOutcome.ANOMALY: 0,
    OpenOutcome.UNPROVEN: 0x103,
}
_NOT_OPENED = {OpenOutcome.NAME_TAKEN: 0xC000_0035, OpenOutcome.NOT_CREATED_CLAIMED: 0xC000_0022}


@dataclass(eq=False)
class FakeOps:
    parent_sd: bytes = SOURCE_DIR.raw
    outcome: OpenOutcome = OpenOutcome.CREATED_OWNED
    unreliable: bool = False
    new_info: HandleInfo | None = None
    store: object = None  # given -> stored descriptor (bytes), or an exception to raise
    disposed: Disposed = Disposed.OK
    lookup: Lookup = field(default_factory=lambda: Lookup(LookupKind.ABSENT, reason="absent"))
    fail: dict[str, BaseException] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    handles: list[FakeHandle] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.parent = FakeHandle(ROOT, "H_DIR", HandleState.OPEN, OpenOutcome.CREATED_OWNED)
        self.parent.info = info(directory=True, file_id=PARENT_ID)
        self.parent.descriptor = self.parent_sd

    def hit(self, event: str) -> None:
        self.events.append(event)
        if event in self.fail:
            raise self.fail[event]

    def new(self, path: Path, role: str) -> FakeHandle:
        handle = FakeHandle(path, role)
        self.handles.append(handle)
        self.events.append(f"new:{role}")
        return handle

    def create_with(self, handle, parent, name, descriptor) -> OpenOutcome:
        assert handle.state is HandleState.NEW and parent is self.parent
        assert parent.path / name == handle.path
        handle.given = descriptor
        handle.state = HandleState.CALLING
        self.hit("create:before")
        outcome = self.outcome
        if outcome in _UNKNOWN:
            handle.state, handle.outcome, handle.status = HandleState.UNKNOWN, outcome, None
            if outcome is not OpenOutcome.UNCERTAIN:
                handle.status = _UNKNOWN[outcome]
            handle.close_error = HandleUnknownError(0, "unknown", str(handle.path), 0)
            return outcome
        if outcome in _NOT_OPENED:
            handle.state, handle.outcome = HandleState.NOT_OPENED, outcome
            handle.status = _NOT_OPENED[outcome]
            return outcome
        handle.state, handle.outcome, handle.unreliable = HandleState.OPEN, outcome, self.unreliable
        handle.status = 0
        handle.info = self.new_info or info(directory=handle.role == "H_DIR")
        if isinstance(self.store, BaseException):
            handle.descriptor = self.store  # type: ignore[assignment]
        else:
            handle.descriptor = (self.store or stored)(descriptor)  # type: ignore[operator]
        self.hit("create:after")
        return outcome

    def open_error(self, handle) -> OSError:
        assert handle.status is not None
        return PermissionError(13, f"status {handle.status:#x}", str(handle.path))

    def query(self, handle) -> HandleInfo:
        assert handle.state is HandleState.OPEN
        self.hit("query:parent" if handle is self.parent else "query:new")
        return handle.info

    def read_descriptor(self, handle) -> bytes:
        assert handle.state is HandleState.OPEN
        self.hit("sd:parent" if handle is self.parent else "sd:new")
        if isinstance(handle.descriptor, BaseException):
            raise handle.descriptor
        return handle.descriptor

    def effective_access(self, descriptor: bytes, sid: Sid, what: str) -> int:
        self.hit(f"authz:{what}")
        parsed = Descriptor.parse(descriptor)
        granted, owner_rights = 0, False
        for ace in parsed.dacl or ():
            owner_rights |= ace.sid == OWNER_RIGHTS
            if ace.flags & a.INHERIT_ONLY_ACE:
                continue
            if ace.sid in (sid, EVERYONE) or (ace.sid == OWNER_RIGHTS and sid == parsed.owner):
                granted |= ace.mask or 0
        if sid == parsed.owner and not owner_rights:
            granted |= a.READ_CONTROL | 0x4_0000
        return granted

    def lookup_entry(self, handle, directory, name) -> Lookup:
        assert handle.role == "H_LOOKUP" and handle.state is HandleState.NEW
        assert directory is self.parent
        handle.state = HandleState.CLOSED  # opened, queried and closed by the primitive
        self.hit("lookup")
        return self.lookup

    def dispose(self, handle, disposal: Disposal) -> Disposed:
        assert handle.outcome is OpenOutcome.CREATED_OWNED and handle.state is HandleState.OPEN
        assert not handle.unreliable and handle.disposes == 0
        handle.disposes += 1
        self.hit("dispose")
        return self.disposed

    def close(self, handle) -> None:
        handle.closes += 1
        assert handle.closes == 1, f"{handle.path} closed twice"
        assert handle.state is HandleState.OPEN, f"close of a {handle.state} handle"
        event = f"close:{handle.role}"
        self.events.append(event)
        if event in self.fail:
            handle.state = HandleState.CLOSE_FAILED
            handle.close_error = self.fail[event]
            raise self.fail[event]
        handle.state = HandleState.CLOSED

    def created(self) -> FakeHandle:
        (handle,) = [h for h in self.handles if h.role != "H_LOOKUP"]
        return handle


@pytest.fixture
def ops() -> Iterator[FakeOps]:
    fake = FakeOps()
    yield fake
    invariants(fake)


def invariants(fake: FakeOps) -> None:
    for handle in [*fake.handles, fake.parent]:
        assert handle.closes <= 1
        if handle.outcome in _UNKNOWN:
            assert handle.closes == 0 and handle.disposes == 0
        if handle.disposes:
            assert handle.outcome is OpenOutcome.CREATED_OWNED
    assert fake.parent.closes == 0 and fake.parent.state is HandleState.OPEN


def category(fake: FakeOps, name: str = "Documents", source_dir: Descriptor = SOURCE_DIR):
    return c.create_category(fake, fake.parent, PARENT, name, source_dir, ME)  # type: ignore[arg-type]


def temp(
    fake: FakeOps, source: Descriptor = SOURCE, source_dir: Descriptor = SOURCE_DIR
) -> Creation:
    return c.create_temp(fake, fake.parent, PARENT, "a.partial", source, source_dir, ME)  # type: ignore[arg-type]


def make(fake: FakeOps, kind: str) -> Creation:
    return category(fake) if kind == "dir" else temp(fake)


KINDS = pytest.mark.parametrize("kind", ["dir", "file"])


# --- success ---------------------------------------------------------------------------


@KINDS
def test_the_object_is_created_with_the_derived_descriptor_in_the_create_call(ops, kind):
    creation = make(ops, kind)
    handle = ops.created()
    assert creation.state is Created.CREATED and creation.allowed and creation.refusal is None
    assert creation.handle is handle and handle.state is HandleState.OPEN and handle.closes == 0
    assert creation.identity == Identity(VOLUME, NEW_ID)
    assert handle.given == creation.derived.descriptor  # the very bytes of the create call
    given = Descriptor.parse(handle.given)
    assert given.protected and given.owner is None and given.sacl is None
    assert not any(ace.flags & a.INHERITED_ACE for ace in given.dacl)
    if kind == "dir":
        assert given.dacl == SOURCE_DIR.dacl  # inheritance flags kept for children
    else:
        assert given.dacl == tuple(Ace(ALLOW, 0, x.mask, x.sid) for x in SOURCE.dacl)
    create = ops.events.index("create:before")
    assert ops.events.index("query:parent") < create  # the parent proven first
    assert "sd:new" in ops.events[create:] and "authz:the new object" in ops.events[create:]
    assert "dispose" not in ops.events and "lookup" not in ops.events


def test_the_temp_is_created_only_after_the_category_is_judged(ops):
    temp(ops)
    create = ops.events.index("create:before")
    assert ops.events.index("sd:parent") < create
    assert ops.events.index("authz:the target") < create
    assert ops.events.index("authz:the source directory") < create


# --- the parent ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parent", "reason"),
    [
        (info(directory=True, file_id=PARENT_ID + 1), CreateReason.PARENT_IDENTITY),
        (info(directory=True, file_id=PARENT_ID, volume=VOLUME + 1), CreateReason.PARENT_VOLUME),
        (
            info(directory=True, file_id=PARENT_ID, attributes=0x410, reparse_tag=0xA000_0003),
            CreateReason.PARENT_REPARSE,
        ),
        (
            info(directory=True, file_id=PARENT_ID, reparse_tag=0xA000_000C),
            CreateReason.PARENT_REPARSE,
        ),
        (info(directory=False, file_id=PARENT_ID), CreateReason.PARENT_NOT_DIRECTORY),
        (
            info(directory=True, file_id=PARENT_ID, delete_pending=True),
            CreateReason.PARENT_DELETE_PENDING,
        ),
    ],
    ids=["other-id", "other-volume", "junction", "symlink-tag", "file", "delete-pending"],
)
@KINDS
def test_a_parent_that_is_not_the_proven_directory_is_refused_before_create(
    ops, kind, parent, reason
):
    ops.parent.info = parent
    creation = make(ops, kind)
    assert creation.state is Created.REFUSED and creation.refusal.reason is reason
    assert ops.handles == [] and "create:before" not in ops.events


def test_a_parent_that_cannot_be_queried_is_refused(ops):
    error = PermissionError(5, "denied")
    ops.fail["query:parent"] = error
    creation = category(ops)
    assert creation.refusal.reason is CreateReason.PARENT_UNREADABLE and creation.error is error
    assert ops.handles == []


def test_the_reparse_attribute_alone_is_a_reparse_point():
    assert info(directory=True, attributes=FILE_ATTRIBUTE_REPARSE_POINT).reparse_point


# --- the descriptors -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (sd(Ace(DENY, 0, 0x2, EVERYONE), Ace(ALLOW, 0, FA, ME)), AclReason.UNSUPPORTED_DENY),
        (sd(CONDITIONAL, Ace(ALLOW, 0, FA, ME)), AclReason.UNKNOWN_ACE),
        (sd(Ace(ALLOW, 0, FA, ME), sacl=LOW_LABEL), AclReason.LABEL),
        (Descriptor.parse(packed(ME, None)), AclReason.NULL_DACL),
        (Descriptor.parse(packed(ME, None, present=False)), AclReason.NO_DACL),
        (sd(Ace(ALLOW, 0, FA, ME), owner=None), AclReason.NO_OWNER),
        (sd(Ace(ALLOW, 0, FA, BOB), owner=BOB), AclReason.OWNER_DIFFERS),
    ],
    ids=["deny", "conditional", "label", "null-dacl", "no-dacl", "no-owner", "other-owner"],
)
def test_a_source_file_outside_the_policy_is_refused_before_anything(ops, source, reason):
    creation = temp(ops, source=source)
    assert creation.state is Created.REFUSED and creation.refusal.reason is reason
    assert ops.events == []


@pytest.mark.parametrize(
    ("source_dir", "reason"),
    [
        (sd(Ace(ALLOW, OICI, FA, ME), Ace(DENY, OICI, 0x40, EVERYONE)), AclReason.UNSUPPORTED_DENY),
        (sd(Ace(ALLOW, OICI, FA, ME), CONDITIONAL), AclReason.UNKNOWN_ACE),
        (sd(Ace(ALLOW, OICI, FA, ME), sacl=LOW_LABEL), AclReason.LABEL),
    ],
    ids=["deny", "conditional", "label"],
)
@KINDS
def test_a_source_directory_outside_the_policy_is_refused_before_create(
    ops, kind, source_dir, reason
):
    if kind == "dir":
        creation = category(ops, source_dir=source_dir)
    else:
        creation = temp(ops, source_dir=source_dir)
    assert creation.state is Created.REFUSED and creation.refusal.reason is reason
    assert ops.handles == [] and "create:before" not in ops.events


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        (WIDE_DIR, AclReason.DIRECTORY_WIDER),
        (
            sd(Ace(ALLOW, OICI, FA, ME), Ace(ALLOW, OICI, FA, SYSTEM), Ace(ALLOW, 0, 0x40, BOB)),
            AclReason.DIRECTORY_WIDER,
        ),
        (
            sd(Ace(DENY, OICI | a.INHERITED_ACE, 0x40, EVERYONE), Ace(ALLOW, OICI, FA, ME)),
            AclReason.UNSUPPORTED_DENY,
        ),
        (sd(Ace(ALLOW, OICI, FA, ME), CONDITIONAL), AclReason.UNKNOWN_ACE),
        (Descriptor.parse(packed(ME, None)), AclReason.NULL_DACL),
    ],
    ids=["everyone-read", "delete-child", "inherited-deny", "conditional", "null-dacl"],
)
def test_a_category_outside_the_policy_or_wider_gets_no_temp(ops, target, reason):
    ops.parent.descriptor = target.raw
    creation = temp(ops)
    assert creation.state is Created.REFUSED and creation.refusal.reason is reason
    assert ops.handles == [] and "create:before" not in ops.events


@pytest.mark.parametrize("bad", [b"\x01\x00", b""], ids=["truncated", "empty"])
def test_a_malformed_or_unreadable_category_descriptor_is_refused(ops, bad):
    ops.parent.descriptor = bad
    assert temp(ops).refusal.reason is AclReason.MALFORMED
    error = PermissionError(5, "denied")
    ops.parent.descriptor = error  # type: ignore[assignment]
    creation = temp(ops)
    assert creation.refusal.reason is AclReason.UNREADABLE and creation.error is error
    assert ops.handles == []


@pytest.mark.parametrize(
    "error",
    [OSError(87, "AuthzInitializeContextFromSid"), a.UnverifiedAccess(0, "reply 1307, 0x0")],
    ids=["failed-call", "unconfirmed-reply"],
)
@pytest.mark.parametrize("event", ["authz:the target", "authz:the source directory"])
def test_an_authz_failure_before_create_is_a_refusal_never_a_pass(ops, error, event):
    ops.fail[event] = error
    creation = temp(ops)
    assert creation.state is Created.REFUSED and creation.refusal.reason is AclReason.UNVERIFIED
    assert creation.error is error and ops.handles == []


def test_a_descriptor_to_create_with_that_is_not_the_derived_one_is_refused(ops):
    derived = a.derive(SOURCE, ME)
    assert isinstance(derived, Derived)
    tampered = [
        replace(derived, descriptor=a.build_descriptor(derived.aces, protected=False)),
        replace(derived, descriptor=a.build_descriptor((*derived.aces, Ace(ALLOW, 0, FR, BOB)))),
        replace(derived, descriptor=packed(ME, acl(*derived.aces), control=a.SE_DACL_PROTECTED)),
        replace(derived, descriptor=b"\x01"),
    ]
    for bad in tampered:
        creation = c._create(
            ops, ops.parent, PARENT, "a.partial", "H_TMP", lambda bad=bad: bad, SOURCE, ME, FA, None
        )  # type: ignore[arg-type]
        assert creation.state is Created.REFUSED
        assert creation.refusal.reason in (AclReason.MISMATCH, AclReason.MALFORMED)
    assert ops.events == []


# --- the create call -------------------------------------------------------------------


@KINDS
def test_a_taken_name_opens_nothing_and_is_refused(ops, kind):
    ops.outcome = OpenOutcome.NAME_TAKEN
    creation = make(ops, kind)
    assert creation.state is Created.REFUSED and creation.refusal.reason is CreateReason.NAME_TAKEN
    assert isinstance(creation.error, OSError)
    assert ops.created().closes == 0 and "lookup" not in ops.events


@pytest.mark.parametrize(
    ("lookup", "state", "reason"),
    [
        (Lookup(LookupKind.ABSENT, reason="absent"), Created.REFUSED, CreateReason.CREATE_FAILED),
        (Lookup(LookupKind.FOUND, NEW_ID), Created.UNKNOWN, CreateReason.OUTCOME_UNKNOWN),
        (Lookup(LookupKind.UNKNOWN, reason="x"), Created.UNKNOWN, CreateReason.OUTCOME_UNKNOWN),
    ],
    ids=["absent", "found", "unknown"],
)
def test_an_error_status_counts_as_not_created_only_if_the_name_is_absent(
    ops, lookup, state, reason
):
    ops.outcome, ops.lookup = OpenOutcome.NOT_CREATED_CLAIMED, lookup
    creation = temp(ops)
    assert creation.state is state and creation.refusal.reason is reason
    assert creation.lookup is lookup and "dispose" not in ops.events


def test_a_failed_close_of_the_lookup_is_kept(ops):
    ops.outcome = OpenOutcome.NOT_CREATED_CLAIMED
    error = HandleCloseError(0, "close", "x", 6)
    ops.fail["lookup"] = error
    creation = temp(ops)
    assert creation.state is Created.UNKNOWN and creation.release_errors == (error,)


@pytest.mark.parametrize("outcome", list(_UNKNOWN), ids=str)
@KINDS
def test_an_unknown_create_outcome_is_never_closed_nor_cleaned(ops, kind, outcome):
    ops.outcome = outcome
    creation = make(ops, kind)
    handle = ops.created()
    assert creation.state is Created.UNKNOWN
    assert creation.refusal.reason is CreateReason.OUTCOME_UNKNOWN
    assert handle.state is HandleState.UNKNOWN and handle.closes == 0
    assert isinstance(creation.error, HandleUnknownError)
    assert "dispose" not in ops.events and "lookup" not in ops.events


@pytest.mark.parametrize(
    ("outcome", "unreliable"),
    [
        (OpenOutcome.OPENED_OWNED, False),
        (OpenOutcome.OPENED_OWNED, True),
        (OpenOutcome.CREATED_OWNED, True),
    ],
    ids=["opened", "opened-unreliable", "created-unreliable"],
)
def test_a_handle_not_proven_created_is_only_closed(ops, outcome, unreliable):
    ops.outcome, ops.unreliable = outcome, unreliable
    creation = temp(ops)
    assert creation.state is Created.UNKNOWN
    assert creation.refusal.reason is CreateReason.NOT_PROVEN_OWN
    assert ops.created().closes == 1 and creation.handle is None
    assert "dispose" not in ops.events and "sd:new" not in ops.events


def test_a_failed_close_of_a_handle_not_proven_created_is_kept(ops):
    ops.outcome = OpenOutcome.OPENED_OWNED
    error = HandleCloseError(0, "close", "x", 6)
    ops.fail["close:H_TMP"] = error
    creation = temp(ops)
    assert creation.state is Created.UNKNOWN and creation.release_errors == (error,)


# --- checks after create, and the removal ---------------------------------------------


def widened(given: bytes) -> bytes:
    parsed = Descriptor.parse(given)
    return packed(ME, acl(*parsed.dacl, Ace(ALLOW, 0, FR, EVERYONE)), control=a.SE_DACL_PROTECTED)


def inherited(given: bytes) -> bytes:
    parsed = Descriptor.parse(given)
    aces = (*parsed.dacl, Ace(ALLOW, a.INHERITED_ACE, FR, EVERYONE))
    return packed(ME, acl(*aces), control=a.SE_DACL_AUTO_INHERITED)


@pytest.mark.parametrize(
    ("store", "reason"),
    [
        (widened, AclReason.MISMATCH),
        (inherited, AclReason.MISMATCH),
        (lambda given: stored(given, owner=BOB), AclReason.MISMATCH),
        (lambda given: packed(ME, acl(*Descriptor.parse(given).dacl)), AclReason.MISMATCH),
        (lambda given: b"\x01\x00\x00", AclReason.MALFORMED),
        (PermissionError(5, "GetSecurityInfo"), AclReason.UNREADABLE),
    ],
    ids=["wider-dacl", "inherited-ace", "other-owner", "not-protected", "malformed", "unreadable"],
)
@KINDS
def test_a_new_object_without_exactly_its_descriptor_is_removed(ops, kind, store, reason):
    ops.store = store
    creation = make(ops, kind)
    handle = ops.created()
    assert creation.state is Created.REMOVED and creation.refusal.reason is reason
    assert handle.disposes == 1 and handle.closes == 1 and creation.handle is None
    assert ops.events[-1] == "lookup" and creation.lookup.kind is LookupKind.ABSENT


@pytest.mark.parametrize(
    "error",
    [OSError(87, "AuthzAccessCheck"), a.UnverifiedAccess(0, "reply 0, granted 0")],
    ids=["failed-call", "unconfirmed-reply"],
)
@KINDS
def test_an_authz_failure_after_create_removes_the_object(ops, kind, error):
    ops.fail["authz:the new object"] = error
    creation = make(ops, kind)
    assert creation.state is Created.REMOVED and creation.refusal.reason is AclReason.UNVERIFIED
    assert creation.error is error and ops.created().disposes == 1


@pytest.mark.parametrize(
    ("new", "kind"),
    [
        (info(directory=False), "dir"),
        (info(directory=True), "file"),
        (info(directory=False, attributes=0x420, reparse_tag=0xA000_000C), "file"),
        (info(directory=True, volume=VOLUME + 1), "dir"),
        (info(directory=True, file_id=PARENT_ID), "dir"),
        (info(directory=False, links=2), "file"),
        (info(directory=False, size=1), "file"),
        (info(directory=False, delete_pending=True), "file"),
    ],
    ids=["dir-is-file", "file-is-dir", "reparse", "volume", "parent-itself", "links", "size",
         "delete-pending"],
)  # fmt: skip
def test_a_new_object_that_is_not_what_was_asked_for_is_removed(ops, new, kind):
    ops.new_info = new
    creation = make(ops, kind)
    assert creation.state is Created.REMOVED
    assert creation.refusal.reason is CreateReason.OBJECT_PROBLEM
    assert "sd:new" not in ops.events  # judged before its descriptor


@pytest.mark.parametrize("event", ["query:new", "query:parent"])
def test_a_failed_query_after_create_removes_the_object(ops, event):
    ops.fail[event] = PermissionError(5, "query")
    if event == "query:parent":  # the parent's first query is before create: let it pass
        ops.fail.pop(event)
        original = ops.query

        def query(handle):
            if handle is ops.parent and "create:after" in ops.events:
                raise PermissionError(5, "query")
            return original(handle)

        ops.query = query  # type: ignore[method-assign]
    creation = temp(ops)
    assert creation.state is Created.REMOVED
    assert creation.refusal.reason is CreateReason.OBJECT_PROBLEM


@pytest.mark.parametrize(
    ("disposed", "lookup", "state"),
    [
        (Disposed.REFUSED, None, Created.LEFT),
        (Disposed.UNSURE, None, Created.REMOVAL_UNKNOWN),
        (Disposed.NONE, None, Created.REMOVAL_UNKNOWN),
        (Disposed.OK, Lookup(LookupKind.FOUND, NEW_ID), Created.LEFT),
        (Disposed.OK, Lookup(LookupKind.FOUND, NEW_ID + 1), Created.REMOVAL_UNKNOWN),
        (
            Disposed.OK,
            Lookup(LookupKind.UNKNOWN, reason="attributes 0x10"),
            Created.REMOVAL_UNKNOWN,
        ),
    ],
    ids=["refused", "unsure", "none", "found-same", "found-other", "lookup-unknown"],
)
def test_a_removal_is_removed_only_when_proven(ops, disposed, lookup, state):
    ops.store, ops.disposed = widened, disposed
    if lookup is not None:
        ops.lookup = lookup
    creation = temp(ops)
    assert creation.state is state and creation.refusal.reason is AclReason.MISMATCH
    assert ops.created().closes == 1
    assert ("lookup" in ops.events) is (lookup is not None)
    assert isinstance(creation.disposal, Disposal)


def test_a_failed_close_after_the_disposition_is_unknown_and_kept(ops):
    ops.store = widened
    error = HandleCloseError(0, "close", "x", 6)
    ops.fail["close:H_TMP"] = error
    creation = temp(ops)
    assert creation.state is Created.REMOVAL_UNKNOWN and creation.release_errors == (error,)
    assert "lookup" not in ops.events  # the handle may still be open


def test_a_failed_close_of_the_removal_lookup_is_unknown_and_kept(ops):
    ops.store = widened
    error = HandleCloseError(0, "close", "x", 6)
    ops.fail["lookup"] = error
    creation = temp(ops)
    assert creation.state is Created.REMOVAL_UNKNOWN and creation.release_errors == (error,)


def test_verify_refuses_a_descriptor_wider_than_the_original(ops):
    creation = temp(ops)
    narrower = sd(Ace(ALLOW, 0, FA, ME))  # SYSTEM may not read the "original"
    refusal, error = c.verify(ops, creation.handle, creation.derived, narrower, ME, FA)
    assert refusal.reason is CreateReason.WIDER and str(SYSTEM) in refusal.detail
    assert error is None
    assert c.verify(ops, creation.handle, creation.derived, SOURCE, ME, FA) == (None, None)
    ops.close(creation.handle)


# --- interrupts ------------------------------------------------------------------------


@KINDS
def test_an_interrupt_before_the_status_leaves_the_handle_unclosed(ops, kind):
    ops.fail["create:before"] = KeyboardInterrupt("create")
    with pytest.raises(KeyboardInterrupt):
        make(ops, kind)
    assert ops.created().closes == 0  # CALLING: its owner makes it UNKNOWN, never closed


@pytest.mark.parametrize(
    "event", ["create:after", "query:new", "sd:new", "authz:the new object", "dispose"]
)
@KINDS
def test_an_interrupt_after_create_closes_once_and_is_raised(ops, kind, event):
    if event == "dispose":
        ops.store = widened
    interrupt = KeyboardInterrupt(event)
    ops.fail[event] = interrupt
    with pytest.raises(KeyboardInterrupt) as raised:
        make(ops, kind)
    assert raised.value is interrupt
    handle = ops.created()
    assert handle.closes == 1 and handle.disposes == (1 if event == "dispose" else 0)


def test_an_interrupt_with_a_failed_close_keeps_both(ops):
    interrupt, error = KeyboardInterrupt("sd"), HandleCloseError(0, "close", "x", 6)
    ops.fail["sd:new"], ops.fail["close:H_TMP"] = interrupt, error
    with pytest.raises(KeyboardInterrupt) as raised:
        temp(ops)
    assert raised.value is interrupt and error in release_failures(interrupt)


def test_an_unexpected_error_in_a_check_closes_once_and_is_raised(ops):
    ops.fail["authz:the new object"] = ValueError("a bug")
    with pytest.raises(ValueError):
        temp(ops)
    assert ops.created().closes == 1 and "dispose" not in ops.events


def test_an_interrupt_in_the_close_of_the_removal_is_raised_after_one_close(ops):
    ops.store = widened
    ops.fail["close:H_TMP"] = KeyboardInterrupt("close")
    with pytest.raises(KeyboardInterrupt):
        temp(ops)
    assert ops.created().closes == 1 and "lookup" not in ops.events


# --- the primitives' guards and the isolation ------------------------------------------


@pytest.mark.parametrize("role", ["H_SRC", "H_LOOKUP", "H_PROBE"])
def test_create_with_refuses_other_roles(role):
    from downloads_organizer.winfileops import Handle

    handle = Handle.new(ROOT / "x", role)
    with pytest.raises(AssertionError):
        c.create_with(handle, handle, "x", b"")
    handle.close()


def test_dispose_directory_only_for_a_created_category():
    from downloads_organizer.winfileops import Handle

    for role in ("H_TMP", "H_DIR"):
        handle = Handle.new(ROOT / "x", role)  # NEW: never created
        with pytest.raises(AssertionError):
            c.dispose_directory(handle, Disposal())
        handle.close()


def test_the_profiles_never_share_delete_and_never_follow_a_reparse_point():
    from downloads_organizer import winfileops as w

    for profile in c.PROFILES.values():
        assert not profile.share & w.FILE_SHARE_DELETE
        assert profile.disposition == w._FILE_CREATE  # never opens an existing name
        assert profile.options & w._FILE_OPEN_REPARSE_POINT
        assert profile.access & a.READ_CONTROL
    assert c.TMP_PROFILE.share == 0
    assert c.DIR_PROFILE.options & w._FILE_DIRECTORY_FILE
    assert c.TMP_PROFILE.options & w._FILE_NON_DIRECTORY_FILE


SOURCE_OF = Path(c.__file__).read_text(encoding="utf-8")


def test_nothing_in_aclcreate_sets_a_descriptor_after_create():
    names = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(ast.parse(SOURCE_OF))
        if isinstance(node, ast.Attribute | ast.Name)
    }
    setters = {
        "SetSecurityInfo", "SetNamedSecurityInfoW", "SetKernelObjectSecurity",
        "NtSetSecurityObject", "SetFileSecurityW", "SetFileSecurity", "create_directory_with",
        "CreateDirectoryW", "CreateFileW",
    }  # fmt: skip
    assert not names & setters


def test_aclcreate_is_wired_only_into_the_copy_never_the_cli_or_gui():
    """R3B integration: only the copy path (``copycommit``, ``runner``) imports it; the
    move, the CLI and the GUI do not; the production gate still refuses every copy."""
    package = Path(c.__file__).parent
    users = set()
    for module in [*package.glob("*.py"), package.parent / "file_organizer.py"]:
        imported = set()
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported |= {node.module or ""} | {alias.name for alias in node.names}
        if any("aclcreate" in name for name in imported):
            users.add(module.name)
    assert users == {"copycommit.py", "runner.py"}
    gate = runner.UndecidedAcl()
    assert gate.check_run(None) == runner.ACL_UNDECIDED_DETAIL  # type: ignore[arg-type]
    assert isinstance(runner.CopyOptions().acl, runner.UndecidedAcl)
    assert features.CROSS_VOLUME_COPY is False
