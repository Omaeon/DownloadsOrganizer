"""R3B-6 B2: the ACL D3 prototype (``aclproof``), pure and on the NTFS of ``tmp_path``.

The pure part checks the parser and the policy on synthetic bytes. The NTFS part sets
descriptors with ``SetFileSecurityW`` on files and directories the test creates under
``tmp_path`` only, and judges by the parsed binary descriptor and by ``AuthzAccessCheck``,
never by SDDL text (SDDL is printed as ``OBSERVED`` for the report only). The current
user stays owner of everything, so ``tmp_path`` can always be removed.
"""

from __future__ import annotations

import ctypes
import inspect
import struct
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_winfileops import walk

from downloads_organizer import aclproof as a
from downloads_organizer import runner
from downloads_organizer import winfileops as w
from downloads_organizer import winprims as p
from downloads_organizer.aclproof import Ace, AclReason, AclRefusal, Descriptor, Sid
from downloads_organizer.winfileops import Handle, OpenOutcome

FA, FR, FW = 0x1F01FF, 0x120089, 0x120116
ALLOW, DENY = a.ACCESS_ALLOWED_ACE_TYPE, a.ACCESS_DENIED_ACE_TYPE
OICI = a.OBJECT_INHERIT_ACE | a.CONTAINER_INHERIT_ACE
ALICE = Sid.parse("S-1-5-21-1-2-3-1001")
BOB = Sid.parse("S-1-5-21-1-2-3-1002")
EVERYONE, SYSTEM = Sid.parse("S-1-1-0"), Sid.parse("S-1-5-18")
OWNER_RIGHTS = Sid.parse("S-1-3-4")
DIR_ACCESS = w.FILE_LIST_DIRECTORY | w.FILE_READ_ATTRIBUTES | w.FILE_ADD_FILE
DIR_SHARE = w.FILE_SHARE_READ | w.FILE_SHARE_WRITE


# --- synthetic descriptors -------------------------------------------------------------


def raw_ace(kind: int, flags: int, body: bytes) -> bytes:
    return struct.pack("<BBH", kind, flags, 4 + len(body)) + body


def acl(*aces: Ace | bytes) -> bytes:
    body = b"".join(x if isinstance(x, bytes) else x.pack() for x in aces)
    return struct.pack("<BBHHH", 2, 0, 8 + len(body), len(aces), 0) + body


def packed(
    owner: Sid | None,
    dacl: bytes | None,
    *,
    group: Sid | None = None,
    sacl: bytes | None = None,
    present: bool = True,
    control: int = 0,
) -> bytes:
    """A self-relative descriptor; ``dacl=None`` with ``present`` is a NULL DACL."""
    parts, offsets, at = [], [], 20
    for part in (owner.data if owner else None, group.data if group else None, sacl, dacl):
        offsets.append(at if part else 0)
        if part:
            parts.append(part)
            at += len(part)
    flags = a.SE_SELF_RELATIVE | control | (a.SE_DACL_PRESENT if present else 0)
    flags |= a.SE_SACL_PRESENT if sacl else 0
    head = struct.pack("<BBHIIII", 1, 0, flags, offsets[0], offsets[1], offsets[2], offsets[3])
    return head + b"".join(parts)


def parsed(owner: Sid | None, *aces: Ace | bytes, **kw: object) -> Descriptor:
    return Descriptor.parse(packed(owner, acl(*aces), **kw))  # type: ignore[arg-type]


# --- pure: the binary form --------------------------------------------------------------


@pytest.mark.parametrize("text", ["S-1-1-0", "S-1-5-18", "S-1-5-32-544", str(ALICE)])
def test_a_sid_round_trips(text):
    assert str(Sid.parse(text)) == text


@pytest.mark.parametrize("text", ["", "S-1", "X-1-5", "S-2-5-1", "S-1-5-x", "S-1-5-4294967296"])
def test_a_bad_sid_is_rejected(text):
    with pytest.raises(ValueError):
        Sid.parse(text)


def test_a_built_descriptor_parses_back_protected_and_ownerless():
    aces = (Ace(DENY, 0, FW, BOB), Ace(ALLOW, 0, FA, ALICE))
    d = Descriptor.parse(a.build_descriptor(aces))
    assert d.dacl == aces and d.protected and d.dacl_present
    assert d.owner is None and d.group is None and d.sacl is None


def test_a_descriptor_keeps_owner_group_aces_and_unknown_types():
    callback = raw_ace(9, 0, struct.pack("<I", FR) + EVERYONE.data + b"artx")
    d = parsed(ALICE, Ace(ALLOW, 0x10, FA, SYSTEM), callback, group=BOB)
    assert (d.owner, d.group) == (ALICE, BOB)
    assert d.dacl == (Ace(ALLOW, 0x10, FA, SYSTEM), Ace(9, 0))


def good() -> bytes:
    return packed(ALICE, acl(Ace(ALLOW, 0, FA, ALICE)), group=BOB)


def poke(data: bytes, offset: int, fmt: str, value: int) -> bytes:
    out = bytearray(data)
    struct.pack_into(fmt, out, offset, value)
    return bytes(out)


DACL_AT = 20 + len(ALICE.data) + len(BOB.data)
MALFORMED = {
    "short": good()[:19],
    "revision": poke(good(), 0, "<B", 2),
    "absolute": poke(good(), 2, "<H", a.SE_DACL_PRESENT),
    "owner beyond": poke(good(), 4, "<I", len(good())),
    "owner sid revision": poke(good(), 20, "<B", 3),
    "owner sid too many": poke(good(), 21, "<B", 16),
    "dacl beyond": poke(good(), 16, "<I", len(good()) - 4),
    "acl size beyond": poke(good(), DACL_AT + 2, "<H", 400),
    "acl revision": poke(good(), DACL_AT, "<B", 7),
    "ace count beyond": poke(good(), DACL_AT + 4, "<H", 2),
    "ace size odd": poke(good(), DACL_AT + 8 + 2, "<H", 18),
    "ace size small": poke(good(), DACL_AT + 8 + 2, "<H", 4),
    "ace sid count": poke(good(), DACL_AT + 8 + 8 + 1, "<B", 4),
}


@pytest.mark.parametrize("data", MALFORMED.values(), ids=MALFORMED.keys())
def test_a_malformed_descriptor_is_rejected(data):
    with pytest.raises(a.MalformedDescriptor):
        Descriptor.parse(data)


# --- pure: the policy -------------------------------------------------------------------


def test_derive_keeps_effective_aces_in_order_and_drops_inheritance():
    io = a.INHERIT_ONLY_ACE | OICI
    source = parsed(
        ALICE,
        Ace(ALLOW, 0x10, FW, BOB),
        Ace(ALLOW, io, FA, EVERYONE),  # applies to children only: not to the file
        Ace(ALLOW, 0x10 | OICI, FA, ALICE),
        Ace(ALLOW, 0, FR, SYSTEM),
    )
    derived = a.derive(source, ALICE)
    assert not isinstance(derived, AclRefusal)
    expected = (Ace(ALLOW, 0, FW, BOB), Ace(ALLOW, 0, FA, ALICE), Ace(ALLOW, 0, FR, SYSTEM))
    assert derived.aces == expected and derived.owner == ALICE
    made = Descriptor.parse(derived.descriptor)
    assert made.dacl == expected and made.protected and made.owner is None


def test_derive_for_a_directory_keeps_propagation_and_drops_inherited():
    source = parsed(
        ALICE, Ace(ALLOW, 0x10 | OICI, FA, ALICE), Ace(ALLOW, a.INHERIT_ONLY_ACE | 1, FR, BOB)
    )
    derived = a.derive(source, ALICE, directory=True)
    assert not isinstance(derived, AclRefusal)
    assert derived.aces == (
        Ace(ALLOW, OICI, FA, ALICE),
        Ace(ALLOW, a.INHERIT_ONLY_ACE | 1, FR, BOB),
    )


LABEL = Ace(a.SYSTEM_MANDATORY_LABEL_ACE_TYPE, 0, 1, Sid.parse("S-1-16-4096"))
REFUSED = {
    "no owner": (parsed(None, Ace(ALLOW, 0, FA, ALICE)), AclReason.NO_OWNER),
    "owner differs": (parsed(BOB, Ace(ALLOW, 0, FA, ALICE)), AclReason.OWNER_DIFFERS),
    "no dacl": (Descriptor.parse(packed(ALICE, None, present=False)), AclReason.NO_DACL),
    "null dacl": (Descriptor.parse(packed(ALICE, None)), AclReason.NULL_DACL),
    "callback ace": (
        parsed(ALICE, raw_ace(9, 0, struct.pack("<I", FR) + EVERYONE.data)),
        AclReason.UNKNOWN_ACE,
    ),
    "object ace": (parsed(ALICE, raw_ace(5, 0, b"\0" * 8 + EVERYONE.data)), AclReason.UNKNOWN_ACE),
    "label in dacl": (parsed(ALICE, LABEL), AclReason.UNKNOWN_ACE),
    "audit flag": (parsed(ALICE, Ace(ALLOW, 0x40, FR, ALICE)), AclReason.UNKNOWN_FLAGS),
    "generic all": (parsed(ALICE, Ace(ALLOW, 0, 0x1000_0000, ALICE)), AclReason.UNKNOWN_RIGHTS),
    "undefined bit": (parsed(ALICE, Ace(ALLOW, 0, FR | 0x200, ALICE)), AclReason.UNKNOWN_RIGHTS),
    "maximum allowed": (
        parsed(ALICE, Ace(ALLOW, 0, a.MAXIMUM_ALLOWED, ALICE)),
        AclReason.UNKNOWN_RIGHTS,
    ),
    "empty mask": (parsed(ALICE, Ace(ALLOW, 0, 0, ALICE)), AclReason.UNKNOWN_RIGHTS),
    "label": (parsed(ALICE, Ace(ALLOW, 0, FA, ALICE), sacl=acl(LABEL)), AclReason.LABEL),
    "audit ace": (
        parsed(ALICE, Ace(ALLOW, 0, FA, ALICE), sacl=acl(Ace(2, 0xC0, FA, EVERYONE))),
        AclReason.UNKNOWN_SACL,
    ),
    "deny": (
        parsed(ALICE, Ace(DENY, 0, FW, BOB), Ace(ALLOW, 0, FA, ALICE)),
        AclReason.UNSUPPORTED_DENY,
    ),
    "deny after allow": (
        parsed(ALICE, Ace(ALLOW, 0, FA, ALICE), Ace(DENY, 0, FW, BOB)),
        AclReason.UNSUPPORTED_DENY,
    ),
    "inherited deny": (
        parsed(ALICE, Ace(ALLOW, 0, FA, ALICE), Ace(DENY, a.INHERITED_ACE | OICI, FW, BOB)),
        AclReason.UNSUPPORTED_DENY,
    ),
    "inherit-only deny": (
        parsed(ALICE, Ace(ALLOW, 0, FA, ALICE), Ace(DENY, a.INHERIT_ONLY_ACE | OICI, FW, BOB)),
        AclReason.UNSUPPORTED_DENY,
    ),
    "server security": (
        parsed(ALICE, Ace(ALLOW, 0, FA, ALICE), control=0x0040),
        AclReason.UNKNOWN_CONTROL,
    ),
    "dacl untrusted": (
        parsed(ALICE, Ace(ALLOW, 0, FA, ALICE), control=0x0080),
        AclReason.UNKNOWN_CONTROL,
    ),
    "rm control": (
        parsed(ALICE, Ace(ALLOW, 0, FA, ALICE), control=a.SE_RM_CONTROL_VALID),
        AclReason.UNKNOWN_CONTROL,
    ),
}


@pytest.mark.parametrize(("source", "reason"), REFUSED.values(), ids=REFUSED.keys())
def test_derive_refuses(source, reason):
    for directory in (False, True):
        refusal = a.derive(source, ALICE, directory=directory)
        assert isinstance(refusal, AclRefusal) and refusal.reason is reason, refusal


FILE_OK = parsed(ALICE, Ace(ALLOW, 0, FA, ALICE), Ace(ALLOW, 0, FR, SYSTEM))
DIR_OK = parsed(ALICE, Ace(ALLOW, OICI, FA, ALICE), Ace(ALLOW, OICI, FA, SYSTEM))
NOT_ASKED = pytest.fail  # what effective_access must not reach: refused before Authz


@pytest.mark.parametrize(
    ("bad", "reason"),
    [v for k, v in REFUSED.items() if k != "owner differs"],  # an owner is no defect of a dir
    ids=[k for k in REFUSED if k != "owner differs"],
)
@pytest.mark.parametrize("where", ["the source", "the source directory", "the target", "new"])
def test_judge_checks_every_descriptor_before_authz(monkeypatch, bad, reason, where):
    """Every descriptor that takes part is checked, whichever it is: the source file,
    the source directory (with and without a target directory), the target directory."""
    monkeypatch.setattr(a, "effective_access", lambda *args, **kw: NOT_ASKED("Authz asked"))
    file, source_dir, target = FILE_OK, DIR_OK, DIR_OK
    if where == "the source":
        file = bad
    elif where == "the target":
        target = bad
    else:
        source_dir = bad
        target = None if where == "new" else target
    refusal = a.judge(file, source_dir, target, ALICE).refusal
    assert refusal is not None and refusal.reason is reason, refusal
    if reason not in (AclReason.NO_DACL, AclReason.NULL_DACL, AclReason.NO_OWNER):
        assert f"{'the source directory' if where == 'new' else where} " in refusal.detail


DC = 0x40  # FILE_DELETE_CHILD
USERS = Sid.parse("S-1-5-21-1-2-3-2001")  # a group BOB is a member of
COMMON = (Ace(ALLOW, OICI, FA, ALICE), Ace(ALLOW, OICI, FA, SYSTEM))
DENY_GROUP_SOURCE = parsed(ALICE, Ace(DENY, 0, DC, USERS), Ace(ALLOW, 0, DC, BOB), *COMMON)
DENY_GROUP_TARGET = parsed(ALICE, Ace(ALLOW, 0, DC, BOB), *COMMON)


def test_the_group_deny_counterexample_is_refused_before_authz(monkeypatch):
    """HIGH-01: BOB in USERS; the source directory denies USERS ``FILE_DELETE_CHILD``
    first, then allows it BOB; the target allows it BOB; owner and other ACEs equal.
    BOB's token loses it at the source and keeps it at the target: refused, by the deny."""
    monkeypatch.setattr(a, "effective_access", lambda *args, **kw: NOT_ASKED("Authz asked"))
    refusal = a.judge(FILE_OK, DENY_GROUP_SOURCE, DENY_GROUP_TARGET, ALICE).refusal
    assert refusal is not None and refusal.reason is AclReason.UNSUPPORTED_DENY
    assert "the source directory" in refusal.detail and str(USERS) in refusal.detail


@pytest.mark.parametrize(
    ("error", "granted", "confirmed"),
    [
        (0, FA, True),
        (0, DC, True),
        (a.ERROR_ACCESS_DENIED, 0, True),  # MAXIMUM_ALLOWED and nothing granted
        (0, 0, False),
        (a.ERROR_ACCESS_DENIED, DC, False),
        (87, 0, False),
        (87, FA, False),
        (1314, 0, False),
        (1336, DC, False),
        (8, 0, False),
    ],
)
def test_only_a_confirmed_reply_counts(error, granted, confirmed):
    if confirmed:
        assert a._confirmed(granted, error, "x") == granted
    else:
        with pytest.raises(a.UnverifiedAccess, match=rf"x: reply error {error} .*{granted:#x}"):
            a._confirmed(granted, error, "x")


@pytest.mark.parametrize("which", ["source", "source.parent", "target"])
@pytest.mark.parametrize(
    ("failure", "reason"), [("oserror", AclReason.UNREADABLE), ("junk", AclReason.MALFORMED)]
)
def test_a_descriptor_that_cannot_be_read_is_refused_never_empty(
    monkeypatch, tmp_path, which, failure, reason
):
    """A failure to read or parse any of the three descriptors refuses, by that path;
    it is not taken for an empty or missing DACL."""
    source, target = tmp_path / "src" / "a.txt", tmp_path / "dst"
    source.parent.mkdir()
    source.write_bytes(b"x")
    target.mkdir()
    bad = {"source": source, "source.parent": source.parent, "target": target}[which]

    def read(path: Path) -> bytes:
        if path != bad:
            return packed(ALICE, acl(Ace(ALLOW, OICI, FA, ALICE)), group=ALICE)
        if failure == "junk":
            return b"\x01\x00junk"
        raise PermissionError(13, "denied", str(path))

    monkeypatch.setattr(a, "read_descriptor_at", read)
    monkeypatch.setattr(a, "effective_access", lambda *args, **kw: NOT_ASKED("Authz asked"))
    refusal = a.assess(source, target, creator=ALICE).refusal
    assert refusal is not None and refusal.reason is reason
    assert str(bad) in refusal.detail


def test_check_created_names_each_difference():
    aces = (Ace(ALLOW, 0, FA, ALICE),)
    derived = a.Derived(aces, ALICE, a.build_descriptor(aces))
    exact = parsed(ALICE, *aces, control=a.SE_DACL_PROTECTED)
    assert a.check_created(exact, derived) is None
    for actual, words in (
        (parsed(BOB, *aces, control=a.SE_DACL_PROTECTED), "owner"),
        (parsed(ALICE, *aces), "not protected"),
        (parsed(ALICE, Ace(ALLOW, 0x10, FA, ALICE), control=a.SE_DACL_PROTECTED), "DACL"),
        (parsed(ALICE, *aces, Ace(ALLOW, 0, FR, EVERYONE), control=a.SE_DACL_PROTECTED), "DACL"),
        (Descriptor.parse(packed(ALICE, None, control=a.SE_DACL_PROTECTED)), "NULL"),
        (parsed(ALICE, *aces, sacl=acl(LABEL), control=a.SE_DACL_PROTECTED), "SACL"),
    ):
        refusal = a.check_created(actual, derived)
        assert refusal is not None and refusal.reason is AclReason.MISMATCH
        assert words in refusal.detail


def test_principals_cover_well_known_owners_and_trustees_once():
    one = parsed(ALICE, Ace(ALLOW, 0, FA, OWNER_RIGHTS), Ace(ALLOW, 0, FR, BOB))
    two = parsed(BOB, Ace(ALLOW, 0, FA, EVERYONE), Ace(ALLOW, 0, FA, Sid.parse("S-1-3-0")))
    found = [str(s) for s in a.principals(one, two, creator=ALICE)]
    assert len(found) == len(set(found))
    assert {str(ALICE), str(BOB), *a.WELL_KNOWN} == set(found)
    assert not set(found) & {"S-1-3-0", "S-1-3-4"}


def test_the_production_gate_is_still_undecided_and_its_gate_does_not_use_aclproof():
    gate = runner.UndecidedAcl()
    assert (
        gate.check_run(None) == gate.check_item(Path("a"), Path("b")) == runner.ACL_UNDECIDED_DETAIL
    )  # type: ignore[arg-type]
    # R3B integration: the runner uses aclproof for the copy's security, the gate does not
    source = inspect.getsource(runner.UndecidedAcl)
    assert "aclproof" not in source and "aclcreate" not in source
    assert isinstance(runner.CopyOptions().acl, runner.UndecidedAcl)


# --- Windows: Authz on synthetic descriptors ------------------------------------------

windows = pytest.mark.windows_only


@windows
def test_equal_sddl_text_with_another_owner_is_other_access():
    """The same DACL bytes (OWNER RIGHTS: full) give a different principal full access
    when the owner differs: comparing the DACL (or its SDDL) is no proof."""
    dacl = acl(Ace(ALLOW, 0, FA, OWNER_RIGHTS))
    alice_owns = Descriptor.parse(packed(ALICE, dacl, group=ALICE))
    bob_owns = Descriptor.parse(packed(BOB, dacl, group=ALICE))
    assert alice_owns.dacl == bob_owns.dacl
    assert a.effective_access(alice_owns.raw, ALICE) == FA
    assert a.effective_access(bob_owns.raw, ALICE) == 0
    assert a.wider_principals(bob_owns, alice_owns, (ALICE, BOB), FA) == ((BOB, FA),)


@windows
def test_other_aces_with_equal_access_are_not_wider():
    split = Descriptor.parse(
        packed(ALICE, acl(Ace(ALLOW, 0, FR, BOB), Ace(ALLOW, 0, FW, BOB)), group=ALICE)
    )
    joined = Descriptor.parse(packed(ALICE, acl(Ace(ALLOW, 0, FR | FW, BOB)), group=ALICE))
    assert split.dacl != joined.dacl
    assert a.effective_access(split.raw, BOB) == a.effective_access(joined.raw, BOB) == FR | FW
    assert a.wider_principals(split, joined, (ALICE, BOB, EVERYONE), FA) == ()


@windows
def test_a_deny_ace_first_takes_effect():
    denied = Descriptor.parse(
        packed(ALICE, acl(Ace(DENY, 0, FW, BOB), Ace(ALLOW, 0, FA, BOB)), group=ALICE)
    )
    assert a.effective_access(denied.raw, BOB) == FA & ~FW  # FW holds READ_CONTROL, SYNCHRONIZE


def group_access(descriptor: bytes, user: Sid, group: Sid) -> int:
    """``MAXIMUM_ALLOWED`` for a context of ``user`` with ``group`` added and enabled
    (``AuthzAddSidsToContext``): a token the per-principal comparison never asks about."""
    from ctypes import wintypes as wt

    class SidAndAttributes(ctypes.Structure):
        _fields_ = (("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD))

    api, ptr = a._api(), ctypes.c_void_p
    az = ctypes.WinDLL("authz", use_last_error=True)
    az.AuthzAddSidsToContext.argtypes = [
        ptr, ctypes.POINTER(SidAndAttributes), wt.DWORD, ptr, wt.DWORD, ctypes.POINTER(ptr),
    ]  # fmt: skip
    az.AuthzAddSidsToContext.restype = wt.BOOL
    manager, alone, joined = ptr(), ptr(), ptr()
    assert api.az.AuthzInitializeResourceManager(1, None, None, None, None, ctypes.byref(manager))
    user_sid = ctypes.create_string_buffer(user.data, len(user.data))
    group_sid = ctypes.create_string_buffer(group.data, len(group.data))
    try:
        assert api.az.AuthzInitializeContextFromSid(
            2, user_sid, manager, None, api.Luid(0, 0), None, ctypes.byref(alone)
        )
        member = SidAndAttributes(ctypes.addressof(group_sid), 4)  # SE_GROUP_ENABLED
        assert az.AuthzAddSidsToContext(
            alone, ctypes.byref(member), 1, None, 0, ctypes.byref(joined)
        )
        sd = ctypes.create_string_buffer(descriptor, len(descriptor))
        request = api.Request(a.MAXIMUM_ALLOWED, None, None, 0, None)
        granted, error, sacl = wt.DWORD(), wt.DWORD(), wt.DWORD()
        reply = api.Reply(1, ctypes.pointer(granted), ctypes.pointer(sacl), ctypes.pointer(error))
        assert api.az.AuthzAccessCheck(
            0, joined, ctypes.byref(request), None, sd, None, 0, ctypes.byref(reply), None
        )
        return a._confirmed(granted.value, error.value, f"{user} in {group}")
    finally:
        for context in (joined, alone):
            if context.value:
                api.az.AuthzFreeContext(context)
        api.az.AuthzFreeResourceManager(manager)


@windows
def test_the_group_deny_counterexample_is_what_single_principals_cannot_see():
    """HIGH-01 on Authz: per principal (BOB alone, USERS alone) the target is not wider,
    so the comparison of R3B-6 allowed it; BOB's token with USERS gets
    ``FILE_DELETE_CHILD`` at the target and not at the source. ``judge`` now refuses."""
    source, target = DENY_GROUP_SOURCE, DENY_GROUP_TARGET
    assert a.wider_principals(target, source, (BOB, USERS), DC) == ()
    assert group_access(source.raw, BOB, USERS) & DC == 0
    assert group_access(target.raw, BOB, USERS) & DC == DC
    assessment = a.judge(FILE_OK, source, target, ALICE)
    assert not assessment.allowed and assessment.refusal is not None
    assert assessment.refusal.reason is AclReason.UNSUPPORTED_DENY


SOURCE_DIR = parsed(ALICE, Ace(ALLOW, OICI, FA, ALICE), Ace(ALLOW, OICI, FA, SYSTEM), group=ALICE)
TARGET_DIR = parsed(ALICE, Ace(ALLOW, OICI, FA, SYSTEM), Ace(ALLOW, OICI, FA, ALICE), group=ALICE)


class ReplyOverride:
    """``az`` of ``aclproof._API`` with ``AuthzAccessCheck`` the real call, after which
    the reply for one descriptor is replaced: answers this machine gives on no demand."""

    def __init__(
        self, real: object, descriptor: bytes, error: int | None, granted: int, ok: bool
    ) -> None:
        self.real, self.descriptor = real, descriptor
        self.error, self.granted, self.ok, self.seen = error, granted, ok, 0

    def __getattr__(self, name: str) -> object:
        return getattr(self.real, name)

    def AuthzAccessCheck(self, *args: object) -> int:  # the Win32 name
        ok = self.real.AuthzAccessCheck(*args)  # type: ignore[attr-defined]
        assert ok
        if args[4].raw != self.descriptor:  # type: ignore[attr-defined]
            return ok
        self.seen += 1
        if self.error is None:  # the honest reply, only counted
            return ok
        reply = args[7]._obj  # type: ignore[attr-defined]
        reply.Error[0], reply.GrantedAccessMask[0] = self.error, self.granted
        if not self.ok:
            ctypes.set_last_error(self.error)
            return 0
        return ok


@windows
def test_the_reply_override_is_a_no_op_for_an_honest_reply(monkeypatch):
    assert SOURCE_DIR.raw != TARGET_DIR.raw
    assert a.judge(FILE_OK, SOURCE_DIR, TARGET_DIR, ALICE).allowed
    api = a._api()
    override = ReplyOverride(api.az, TARGET_DIR.raw, None, 0, True)
    monkeypatch.setattr(api, "az", override)
    assert a.judge(FILE_OK, SOURCE_DIR, TARGET_DIR, ALICE).allowed and override.seen


@windows
@pytest.mark.parametrize("side", ["target", "source"])
@pytest.mark.parametrize(
    ("error", "granted"),
    [(87, 0), (87, DC), (87, FA), (1314, 0), (1336, FA), (8, 0), (5, DC), (5, FA), (0, 0)],
)
def test_a_reply_error_is_unverified_never_zero_rights(monkeypatch, side, error, granted):
    """HIGH-02: ``AuthzAccessCheck`` returns TRUE and the reply has a nonzero ``Error``
    (or ``ERROR_ACCESS_DENIED`` with a mask, or no error and no mask) for the target or
    the source directory: refused UNVERIFIED, whatever the mask, with the side and the
    code in the detail. Taking it as zero rights allows (target) or refuses for the wrong
    reason (source)."""
    api = a._api()
    chosen = TARGET_DIR if side == "target" else SOURCE_DIR
    override = ReplyOverride(api.az, chosen.raw, error, granted, True)
    monkeypatch.setattr(api, "az", override)
    assessment = a.judge(FILE_OK, SOURCE_DIR, TARGET_DIR, ALICE)
    assert override.seen
    refusal = assessment.refusal
    assert refusal is not None and refusal.reason is AclReason.UNVERIFIED, refusal
    assert f"on the {side}: reply error {error} " in refusal.detail
    assert f"granted {granted:#x}" in refusal.detail


@windows
@pytest.mark.parametrize("side", ["target", "source"])
def test_a_failed_access_check_is_unverified(monkeypatch, side):
    api = a._api()
    chosen = TARGET_DIR if side == "target" else SOURCE_DIR
    monkeypatch.setattr(api, "az", ReplyOverride(api.az, chosen.raw, 87, FA, False))
    refusal = a.judge(FILE_OK, SOURCE_DIR, TARGET_DIR, ALICE).refusal
    assert refusal is not None and refusal.reason is AclReason.UNVERIFIED
    assert "[WinError 87]" in refusal.detail and f"on the {side}: " in refusal.detail


ALLOW_ONLY = {
    # equal DACL bytes, the target owned by BOB: BOB's implicit owner rights are more
    "equal dacl, other owner gains": (
        SOURCE_DIR,
        parsed(BOB, *SOURCE_DIR.dacl, group=ALICE),  # type: ignore[misc]
        ((BOB, 0x60000),),  # READ_CONTROL | WRITE_DAC
    ),
    # the owners differ but the DACL already gives each owner full control
    "other owners, nothing more": (
        parsed(BOB, Ace(ALLOW, OICI, FA, ALICE), Ace(ALLOW, OICI, FA, BOB), group=ALICE),
        parsed(ALICE, Ace(ALLOW, OICI, FA, BOB), Ace(ALLOW, OICI, FA, ALICE), group=ALICE),
        (),
    ),
    # the same rights through inherited ACEs: not wider
    "inherited, equal": (
        SOURCE_DIR,
        parsed(
            ALICE,
            Ace(ALLOW, a.INHERITED_ACE | OICI, FA, ALICE),
            Ace(ALLOW, a.INHERITED_ACE | OICI, FA, SYSTEM),
            group=ALICE,
        ),
        (),
    ),
    # an inherited ACE that gives Everyone reading: wider
    "inherited, wider": (
        SOURCE_DIR,
        parsed(ALICE, *COMMON, Ace(ALLOW, a.INHERITED_ACE | OICI, FR, EVERYONE), group=ALICE),
        ((EVERYONE, FR & a.DIRECTORY_RIGHTS),),  # and each well-known SID (Authz adds WD)
    ),
    # an inherit-only ACE does not apply to the directory itself
    "inherit-only, not wider": (
        SOURCE_DIR,
        parsed(ALICE, *COMMON, Ace(ALLOW, a.INHERIT_ONLY_ACE | OICI, FA, EVERYONE), group=ALICE),
        (),
    ),
}


@windows
@pytest.mark.parametrize(("source_dir", "target", "wider"), ALLOW_ONLY.values(), ids=ALLOW_ONLY)
def test_allow_only_directories_are_compared_by_effective_access(source_dir, target, wider):
    assessment = a.judge(FILE_OK, source_dir, target, ALICE)
    assert set(wider) <= set(assessment.wider) and bool(wider) == bool(assessment.wider)
    if wider:
        assert assessment.refusal is not None
        assert assessment.refusal.reason is AclReason.DIRECTORY_WIDER
    else:
        assert assessment.allowed and assessment.file is not None


# --- NTFS --------------------------------------------------------------------------------

_DACL, _LABEL = a.DACL_SECURITY_INFORMATION, a.LABEL_SECURITY_INFORMATION


def _advapi() -> ctypes.WinDLL:
    from ctypes import wintypes as wt

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    ptr = ctypes.c_void_p
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ptr), ptr,
    ]  # fmt: skip
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ptr, wt.DWORD, wt.DWORD, ctypes.POINTER(wt.LPWSTR), ptr,
    ]  # fmt: skip
    adv.SetFileSecurityW.argtypes = [wt.LPCWSTR, wt.DWORD, ptr]
    adv.SetKernelObjectSecurity.argtypes = [wt.HANDLE, wt.DWORD, ptr]
    return adv


def set_sddl(path: Path, sddl: str, info: int = _DACL) -> None:
    """Set part of the descriptor of a file the test created (``D:P(...)``: protected)."""
    adv, sd = _advapi(), ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(sd), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not adv.SetFileSecurityW(str(path), info, sd):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        ctypes.windll.kernel32.LocalFree(sd)


def sddl(data: bytes) -> str:
    """For ``OBSERVED`` lines only: what the report shows, never what a test judges by."""
    from ctypes import wintypes as wt

    text = wt.LPWSTR()
    buffer = ctypes.create_string_buffer(data, len(data))
    _advapi().ConvertSecurityDescriptorToStringSecurityDescriptorW(
        buffer, 1, a._READ_INFO, ctypes.byref(text), None
    )
    try:
        return text.value or ""
    finally:
        ctypes.windll.kernel32.LocalFree(text)


def observe(record_property, step: str, value: object) -> None:
    print(f"OBSERVED {step}: {value}")
    record_property(step, str(value))


def descriptor(path: Path) -> Descriptor:
    return Descriptor.parse(a.read_descriptor_at(path))


class CleanupRefused(Exception):
    """The cleanup met something that is not plainly the test's own: it stops there."""


WRITE_DAC = 0x0004_0000
_PIN = w.FILE_SHARE_READ | w.FILE_SHARE_WRITE  # no FILE_SHARE_DELETE: no rename meanwhile


def set_dacl_of(handle: Handle, sddl_text: str) -> None:
    """Set the DACL through ``handle`` (``SetKernelObjectSecurity``), never by a path."""
    adv, sd = _advapi(), ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl_text, 1, ctypes.byref(sd), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not adv.SetKernelObjectSecurity(handle.raw, _DACL, sd):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        ctypes.windll.kernel32.LocalFree(sd)


def unlock_tree(root: Path) -> None:
    """Give the owner full control again of everything under ``root`` (every DACL a test
    sets leaves the owner ``WRITE_DAC`` and ``SYNCHRONIZE``), so it can be removed.

    Handle by handle: the walk starts at a handle of ``root`` and opens each listed
    entry relative to its directory's handle, as itself (a reparse point is never
    followed). Every handle stays open without ``FILE_SHARE_DELETE`` until the end, so
    nothing walked is renamed or replaced meanwhile. Before the DACL of anything is
    touched, it stops (``CleanupRefused``) at what is not plainly the test's own: a
    reparse point (a junction is no symlink to ``Path.is_symlink``), an entry that is
    not the object its directory listed, another volume, a file with another hard link
    (its descriptor is shared), a listing that may be incomplete."""
    top = walk(root, access=w.FILE_LIST_DIRECTORY | w.FILE_READ_ATTRIBUTES, share=_PIN)
    held = [top]
    try:
        base = w.query(top)
        if base.reparse_point or not base.directory:
            raise CleanupRefused(f"{root} is not a plain directory")
        pending = [top]
        while pending:
            directory = pending.pop()
            listing = p.query_directory(directory, "*")
            if listing.entries is None or (listing.information or 0) > p._LOOKUP_BUFFER // 2:
                raise CleanupRefused(f"{directory.path}: listing {listing.reason or 'too long'}")
            for entry in listing.entries:
                name = entry.name.decode("utf-16-le", "surrogatepass")
                if name in (".", ".."):
                    continue
                where = directory.path / name
                if entry.attributes & w.FILE_ATTRIBUTE_REPARSE_POINT or entry.reparse_tag:
                    raise CleanupRefused(f"{where} is a reparse point ({entry.reparse_tag:#x})")
                is_dir = bool(entry.attributes & w.FILE_ATTRIBUTE_DIRECTORY)
                access = w.FILE_READ_ATTRIBUTES | WRITE_DAC
                child, _ = w.open_relative(
                    directory,
                    name,
                    directory=is_dir,
                    access=access | (w.FILE_LIST_DIRECTORY if is_dir else 0),
                    share=_PIN,
                )
                held.append(child)
                info = w.query(child)
                if info.reparse_point:
                    raise CleanupRefused(f"{where} became a reparse point")
                if (info.file_id, info.volume_serial) != (entry.file_id, base.volume_serial):
                    raise CleanupRefused(f"{where} is not the object listed in {directory.path}")
                if not is_dir and info.links != 1:
                    raise CleanupRefused(f"{where} has {info.links} links")
                set_dacl_of(child, "D:P(A;OICI;FA;;;OW)" if is_dir else "D:P(A;;FA;;;OW)")
                if is_dir:
                    pending.append(child)
    finally:
        for handle in reversed(held):
            handle.close()


@pytest.fixture(autouse=True)
def unlocked(request: pytest.FixtureRequest) -> Iterator[None]:
    """After an NTFS test, ``unlock_tree`` on its ``tmp_path``, so that it is removable."""
    yield
    if w.AVAILABLE and "tmp_path" in request.fixturenames:
        unlock_tree(request.getfixturevalue("tmp_path"))


@pytest.fixture
def me() -> Sid:
    return a.creator_sid()


def tree(base: Path, source_dir: str, target_dir: str | None) -> tuple[Path, Path]:
    """``base/src/a.txt`` under a source directory DACL, and ``base/dst`` (if given)."""
    src = base / "src"
    src.mkdir()
    set_sddl(src, source_dir)
    (src / "a.txt").write_bytes(b"payload")
    dst = base / "dst"
    if target_dir is not None:
        dst.mkdir()
        set_sddl(dst, target_dir)
    return src / "a.txt", dst


PRIVATE = "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)"  # the user (owner) and SYSTEM only
WIDE = PRIVATE[:-1] + ")(A;OICI;FR;;;WD)"  # and Everyone may read and list


@pytest.mark.windows_only
@pytest.mark.real_ntfs
def test_the_descriptor_of_a_plain_file_is_read_and_parsed(tmp_path, me, record_property):
    f = tmp_path / "plain.txt"
    f.write_bytes(b"x")
    d = descriptor(f)
    observe(record_property, "plain file", sddl(d.raw))
    assert d.owner == me and d.dacl_present and d.dacl
    assert all(ace.flags & a.INHERITED_ACE for ace in d.dacl)  # all from tmp_path
    assert not d.protected
    with pytest.raises(OSError):
        a.read_descriptor_at(tmp_path / "missing.txt")


@pytest.mark.windows_only
@pytest.mark.real_ntfs
def test_a_plain_copy_inherits_the_wider_destination(tmp_path, me, record_property, acquisitions):
    """The counterexample: a file created the ordinary way in a wider directory gets the
    destination's inheritable ACEs; Everyone may read it although not the source."""
    source, dst = tree(tmp_path, PRIVATE, WIDE)
    directory = walk(dst, access=DIR_ACCESS, share=DIR_SHARE)
    plain = Handle.new(dst / "plain.partial", "H_TMP")
    try:
        assert p.create_into(plain, directory, "plain.partial") is OpenOutcome.CREATED_OWNED
    finally:
        plain.close()
        directory.close()
    copy, original = descriptor(dst / "plain.partial"), descriptor(source)
    observe(record_property, "source", sddl(original.raw))
    observe(record_property, "plain copy", sddl(copy.raw))
    assert a.effective_access(original.raw, EVERYONE) == 0
    assert a.effective_access(copy.raw, EVERYONE) == FR
    assert any(ace.flags & a.INHERITED_ACE for ace in copy.dacl or ())


@pytest.mark.windows_only
@pytest.mark.real_ntfs
def test_the_partial_gets_its_descriptor_at_creation_and_keeps_it(
    tmp_path, me, record_property, acquisitions
):
    """``create_temp_with`` in the wider directory: protected, nothing inherited, the
    source's ACEs exactly, its owner; the same access for every principal; unchanged by
    a rename in the directory and by one into a directory that would give more."""
    source, dst = tree(tmp_path, PRIVATE, WIDE)
    set_sddl(source, f"D:P(A;;FR;;;BG)(A;;FA;;;{me})(A;;FR;;;SY)")
    other = tmp_path / "other"
    other.mkdir()
    set_sddl(other, PRIVATE[:-1] + ")(A;OICI;FA;;;AU)")  # Authenticated Users: full control
    before = a.read_descriptor_at(source)
    derived = a.derive(Descriptor.parse(before), me)
    assert not isinstance(derived, AclRefusal)
    directory = walk(dst, access=DIR_ACCESS, share=DIR_SHARE)
    second = walk(other, access=DIR_ACCESS, share=DIR_SHARE)
    handle = Handle.new(dst / "a.partial", "H_TMP")
    try:
        outcome = a.create_temp_with(handle, directory, "a.partial", derived.descriptor)
        assert outcome is OpenOutcome.CREATED_OWNED
        created = Descriptor.parse(a.read_descriptor(handle))  # READ_CONTROL on H_TMP
        observe(record_property, "created", sddl(created.raw))
        assert a.check_created(created, derived) is None
        assert not any(ace.flags & a.INHERITED_ACE for ace in created.dacl or ())
        sids = a.principals(created, Descriptor.parse(before), creator=me)
        assert a.wider_principals(created, Descriptor.parse(before), sids, FA) == ()
        assert a.wider_principals(Descriptor.parse(before), created, sids, FA) == ()
        p.write_at(handle, 0, b"payload")
        w.rename_into(handle, directory, "a.txt")
        assert a.check_created(descriptor(dst / "a.txt"), derived) is None
        w.rename_into(handle, second, "a.txt")
        moved = descriptor(other / "a.txt")
        observe(record_property, "after rename into a wider directory", sddl(moved.raw))
        assert a.check_created(moved, derived) is None
        assert a.effective_access(moved.raw, Sid.parse("S-1-5-11")) == 0
    finally:
        handle.close()
        directory.close()
        second.close()
    assert a.read_descriptor_at(source) == before  # the source's security is unchanged


@pytest.mark.windows_only
@pytest.mark.real_ntfs
def test_the_creating_handle_writes_where_the_dacl_allows_only_reading(
    tmp_path, me, record_property, acquisitions
):
    """OBSERVED platform behaviour the integration must know: the new file's DACL does
    not limit the handle that created it (NTFS grants the requested access on create);
    any later open is limited. The copy is written through the creating handle only."""
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    set_sddl(source, f"D:P(A;;FR;;;{me})(A;;FA;;;SY)")
    derived = a.derive(descriptor(source), me)
    assert not isinstance(derived, AclRefusal)
    directory = walk(dst, access=DIR_ACCESS, share=DIR_SHARE)
    handle = Handle.new(dst / "r.partial", "H_TMP")
    try:
        assert a.create_temp_with(handle, directory, "r.partial", derived.descriptor) is (
            OpenOutcome.CREATED_OWNED
        )
        p.write_at(handle, 0, b"written")
        assert p.read_at(handle, 0, 7) == b"written"
        with pytest.raises(PermissionError):
            (dst / "r.partial").open("r+b")  # an open that must fail
        observe(record_property, "read-only DACL", "creating handle writes, a reopen is denied")
    finally:
        handle.close()
        directory.close()


@pytest.mark.windows_only
@pytest.mark.real_ntfs
def test_a_category_directory_is_created_with_the_source_directory_dacl(
    tmp_path, me, record_property
):
    source, dst = tree(tmp_path, PRIVATE, WIDE)
    assessment = a.assess(source, dst / "Documents", creator=me)
    assert assessment.allowed and assessment.directory is not None
    a.create_directory_with(dst / "Documents", assessment.directory.descriptor)
    made = descriptor(dst / "Documents")
    observe(record_property, "category directory", sddl(made.raw))
    assert a.check_created(made, assessment.directory) is None
    assert {ace.flags for ace in made.dacl or ()} == {OICI}
    (dst / "Documents" / "child.txt").write_bytes(b"c")  # an ordinary file inside it
    child = descriptor(dst / "Documents" / "child.txt")
    assert a.effective_access(child.raw, EVERYONE) == 0
    with pytest.raises(FileExistsError):
        a.create_directory_with(dst / "Documents", assessment.directory.descriptor)


@pytest.mark.windows_only
@pytest.mark.real_ntfs
@pytest.mark.parametrize(
    ("target", "allowed"),
    [
        (PRIVATE, True),
        ("D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;OW)", True),  # other order, other SDDL text
        ("D:P(A;OICI;FA;;;OW)", True),  # narrower
        (WIDE, False),
        (PRIVATE[:-1] + ")(A;;0x40;;;AU)", False),  # FILE_DELETE_CHILD: replace any file
        (PRIVATE[:-1] + ")(A;OICI;FA;;;BU)", False),
    ],
    ids=["equal", "reordered", "narrower", "everyone-read", "delete-child", "users-full"],
)
def test_a_target_directory_wider_than_the_source_directory_is_refused(
    tmp_path, me, record_property, target, allowed
):
    source, dst = tree(tmp_path, PRIVATE, target)
    before = a.read_descriptor_at(source)
    assessment = a.assess(source, dst, creator=me)
    observe(record_property, f"target {target}", assessment.refusal)
    assert assessment.allowed is allowed
    if not allowed:
        assert assessment.refusal is not None
        assert assessment.refusal.reason is AclReason.DIRECTORY_WIDER and assessment.wider
    assert a.read_descriptor_at(source) == before
    assert list(dst.iterdir()) == []  # assess creates nothing


@pytest.mark.windows_only
@pytest.mark.real_ntfs
@pytest.mark.parametrize(
    ("sddl_text", "info", "reason"),
    [
        ("D:P(XA;;FR;;;WD;(Member_of {SID(BA)}))(A;;FA;;;OW)", _DACL, AclReason.UNKNOWN_ACE),
        ("D:NO_ACCESS_CONTROL", _DACL, AclReason.NULL_DACL),
        ("S:(ML;;NW;;;LW)", _LABEL, AclReason.LABEL),
        ("D:P(A;;0x140001;;;OW)", _DACL, AclReason.UNREADABLE),  # no READ_CONTROL for the owner
        ("D:P(A;;FA;;;OW)(A;;GA;;;WD)", _DACL, None),  # GA is mapped to FA on a file
    ],
    ids=["conditional-ace", "null-dacl", "low-label", "unreadable", "generic-mapped"],
)
def test_a_source_descriptor_outside_the_policy_is_refused(
    tmp_path, me, record_property, sddl_text, info, reason
):
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    set_sddl(source, sddl_text, info)
    assessment = a.assess(source, dst, creator=me)
    observe(record_property, f"source {sddl_text}", assessment.refusal)
    if reason is None:  # NTFS stores the mapped mask; the copy's Everyone gets FA as at the source
        assert assessment.allowed and assessment.file is not None
        assert Ace(ALLOW, 0, FA, EVERYONE) in assessment.file.aces
    else:
        assert assessment.refusal is not None and assessment.refusal.reason is reason
    assert list(dst.iterdir()) == []


@pytest.mark.windows_only
@pytest.mark.real_ntfs
def test_an_unreadable_directory_is_refused(tmp_path, me):
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    set_sddl(dst, "D:P(A;;0x140001;;;OW)")  # WRITE_DAC, SYNCHRONIZE kept: the cleanup resets it
    refusal = a.assess(source, dst, creator=me).refusal
    assert refusal is not None and refusal.reason is AclReason.UNREADABLE
    set_sddl(source.parent, "D:P(A;;0x140021;;;OW)")  # list and traverse, no READ_CONTROL
    refusal = a.assess(source, tmp_path / "new", creator=me).refusal
    assert refusal is not None and refusal.reason is AclReason.UNREADABLE


@pytest.mark.windows_only
@pytest.mark.real_ntfs
def test_a_source_owned_by_another_principal_is_refused(tmp_path, me, record_property):
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    try:
        set_sddl(source, "O:BA", a.OWNER_SECURITY_INFORMATION)
    except OSError as exc:  # 1307 without an elevated token: the pure test covers it
        observe(record_property, "owner change", exc)
        pytest.skip(f"NOT_TESTED: cannot make another owner here ({exc})")
    refusal = a.assess(source, dst, creator=me).refusal
    assert refusal is not None and refusal.reason is AclReason.OWNER_DIFFERS


@pytest.mark.windows_only
@pytest.mark.real_ntfs
@pytest.mark.parametrize("where", ["the source", "the source directory", "the target"])
def test_a_deny_ace_on_ntfs_is_refused(tmp_path, me, record_property, where):
    """HIGH-01 on NTFS: a deny ACE for Everyone (a group of every token) in the source
    file, the source directory, or inherited (``ID``) in the target directory: refused."""
    source_dir = PRIVATE + "(D;CI;0x40;;;WD)" if where == "the source directory" else PRIVATE
    target_dir = "D:AI(D;OICIID;0x40;;;WD)(A;OICIID;FA;;;OW)(A;OICIID;FA;;;SY)"
    source, dst = tree(tmp_path, source_dir, target_dir if where == "the target" else PRIVATE)
    if where == "the source":
        set_sddl(source, f"D:P(D;;0x2;;;WD)(A;;FA;;;{me})")  # no write of data for anyone
    if where == "the target":
        assert descriptor(dst).dacl[0] == Ace(DENY, a.INHERITED_ACE | OICI, 0x40, EVERYONE)
    assessment = a.assess(source, dst, creator=me)
    observe(record_property, f"deny in {where}", assessment.refusal)
    assert assessment.refusal is not None
    assert assessment.refusal.reason is AclReason.UNSUPPORTED_DENY
    assert f"{where} " in assessment.refusal.detail
    assert list(dst.iterdir()) == []


# --- the cleanup itself ------------------------------------------------------------------


@pytest.fixture
def sentinel(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, object]]:
    """A directory outside the test's ``tmp_path`` and what must stay as it is there."""
    outside = tmp_path_factory.mktemp("sentinel")
    (outside / "keep.txt").write_bytes(b"sentinel")

    def snapshot() -> object:
        return (
            a.read_descriptor_at(outside),
            a.read_descriptor_at(outside / "keep.txt"),
            (outside / "keep.txt").read_bytes(),
            sorted(x.name for x in outside.iterdir()),
        )

    before = snapshot()
    yield outside, snapshot
    assert snapshot() == before


@pytest.mark.windows_only
@pytest.mark.real_ntfs
@pytest.mark.parametrize("depth", ["top", "nested"])
def test_the_cleanup_stops_at_a_junction_and_leaves_its_target_alone(tmp_path, sentinel, depth):
    """A junction is no symlink to ``Path.is_symlink``: the cleanup must stop at it,
    whether at the top or under a directory it has unlocked, and not touch the target."""
    import _winapi

    outside, snapshot = sentinel
    before = snapshot()
    parent = tmp_path if depth == "top" else tmp_path / "sub"
    parent.mkdir(exist_ok=True)
    (tmp_path / "mine.txt").write_bytes(b"m")
    link = parent / "link"
    _winapi.CreateJunction(str(outside), str(link))
    try:
        assert link.is_junction() and not link.is_symlink()
        with pytest.raises(CleanupRefused, match="reparse point"):
            unlock_tree(tmp_path)
        assert snapshot() == before
    finally:
        link.rmdir()  # the junction itself, not its target
    unlock_tree(tmp_path)  # without the junction, the same tree is unlocked


@pytest.mark.windows_only
@pytest.mark.real_ntfs
def test_the_cleanup_stops_at_a_hard_link_to_an_outside_file(tmp_path, sentinel):
    """A second hard link shares the outside file's descriptor: the cleanup stops."""
    outside, snapshot = sentinel
    before = snapshot()
    hard = tmp_path / "hard.txt"
    hard.hardlink_to(outside / "keep.txt")
    try:
        with pytest.raises(CleanupRefused, match="2 links"):
            unlock_tree(tmp_path)
        assert snapshot() == before
    finally:
        hard.unlink()  # this name only
