"""ACL D3 prototype (R3B-6): read, judge and derive the security of a cross-volume copy.

Isolated: nothing of the runner, the CLI or the GUI imports this module, and the
production gate stays ``runner.UndecidedAcl`` (every copy refused). It holds the
policy under test and the pieces to prove it on NTFS, nothing that copies.

Policy under test (PROPOSED, OWNER_DECISION D3): access to the temporary file and the
final file must never be wider than access to the source.

* The copy's DACL is the source's effective DACL: exactly its ACEs that apply to the
  file (no inherit-only ones), in their order, inheritance flags cleared, marked
  protected so nothing of the destination is inherited (``derive``). It is given to
  the create call itself (``create_temp_with``: ``OBJECT_ATTRIBUTES.SecurityDescriptor``),
  never fixed after the file exists.
* The owner of the copy is the creator (the token's default owner); it must be the
  source's owner (the owner's implicit rights would differ otherwise).
* The target directory must not give any principal more directory rights than the
  source's directory gives it (``wider_principals``, by ``AuthzAccessCheck`` for each
  principal on its own: ``FILE_DELETE_CHILD`` there deletes or replaces any file in it,
  whatever the file's DACL). A category directory that does not exist yet is created
  with the source directory's DACL, protected (``derive(..., directory=True)``).
* Only ALLOW-only DACLs take part (``supported``, for the source file, the source
  directory and an existing target directory alike). With access-allowed ACEs only, a
  token's rights are the union of what each of its SIDs gets, so a principal-by-
  principal comparison covers every token made of them. A deny ACE breaks that: the
  deny for a group can take from a token what the same ACL grants its user SID alone,
  and no set of single-SID checks proves the contrary for an arbitrary token. Any deny
  ACE (explicit, inherited or inherit-only) is refused (``UNSUPPORTED_DENY``).
* An Authz answer counts only as a confirmed grant (``Error`` 0, a mask) or a confirmed
  denial (``ERROR_ACCESS_DENIED``, mask 0, as documented for ``MAXIMUM_ALLOWED``);
  anything else is ``UNVERIFIED``, never zero rights (``effective_access``).
* Refused: a descriptor that cannot be read or parsed, no owner, no DACL or a NULL
  DACL, control bits not known here, any ACE other than access allowed (deny, callback,
  object, conditional, ...), unknown ACE flags, generic or undefined access bits, a
  mandatory label or any other SACL entry read (scoped policy and resource attribute
  entries included).

What is compared is the parsed binary descriptor (owner, control, each ACE's type,
flags, mask and SID) and the effective access computed by Authz, never SDDL text.
Not covered here (see the R3B-6 report): the SACL (needs SeSecurityPrivilege), central
access policies, tokens with restricting or deny-only SIDs, a later re-propagation of
the destination's permissions, the directories above the target directory.
"""

from __future__ import annotations

import ctypes
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from downloads_organizer import winfileops, winprims
from downloads_organizer.winfileops import Handle, HandleState, OpenOutcome

# security descriptor control
SE_OWNER_DEFAULTED = 0x0001
SE_DACL_PRESENT = 0x0004
SE_SACL_PRESENT = 0x0010
SE_DACL_AUTO_INHERITED = 0x0400
SE_DACL_PROTECTED = 0x1000
SE_RM_CONTROL_VALID = 0x4000
SE_SELF_RELATIVE = 0x8000
# owner/group/DACL/SACL defaulted, auto-inherit requested/inherited, protected, self-relative
_KNOWN_CONTROL = 0x0001 | 0x0002 | SE_DACL_PRESENT | 0x0008 | SE_SACL_PRESENT | 0x0020
_KNOWN_CONTROL |= 0x0100 | 0x0200 | SE_DACL_AUTO_INHERITED | 0x0800 | SE_DACL_PROTECTED
_KNOWN_CONTROL |= 0x2000 | SE_SELF_RELATIVE
# ACE types and flags
ACCESS_ALLOWED_ACE_TYPE = 0x0
ACCESS_DENIED_ACE_TYPE = 0x1
SYSTEM_MANDATORY_LABEL_ACE_TYPE = 0x11
OBJECT_INHERIT_ACE = 0x01
CONTAINER_INHERIT_ACE = 0x02
NO_PROPAGATE_INHERIT_ACE = 0x04
INHERIT_ONLY_ACE = 0x08
INHERITED_ACE = 0x10
_KNOWN_ACE_FLAGS = 0x1F
_PROPAGATION = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE | NO_PROPAGATE_INHERIT_ACE
# access rights
READ_CONTROL = 0x0002_0000
FILE_ALL_ACCESS = 0x001F_01FF
MAXIMUM_ALLOWED = 0x0200_0000
FILE_LIST_DIRECTORY = 0x0001
FILE_ADD_FILE = 0x0002
FILE_DELETE_CHILD = 0x0040
DIRECTORY_RIGHTS = FILE_ALL_ACCESS & ~winfileops.SYNCHRONIZE
# security information
OWNER_SECURITY_INFORMATION = 0x1
GROUP_SECURITY_INFORMATION = 0x2
DACL_SECURITY_INFORMATION = 0x4
LABEL_SECURITY_INFORMATION = 0x10
ATTRIBUTE_SECURITY_INFORMATION = 0x20  # resource attributes: READ_CONTROL suffices
SCOPE_SECURITY_INFORMATION = 0x40  # central access policy id: READ_CONTROL suffices
_READ_INFO = (
    OWNER_SECURITY_INFORMATION
    | GROUP_SECURITY_INFORMATION
    | DACL_SECURITY_INFORMATION
    | LABEL_SECURITY_INFORMATION
    | ATTRIBUTE_SECURITY_INFORMATION
    | SCOPE_SECURITY_INFORMATION
)
ERROR_ACCESS_DENIED = 5

# SIDs that never stand for a principal in a token, as ACE trustees on a file
_PLACEHOLDERS = frozenset({"S-1-3-0", "S-1-3-1", "S-1-3-4"})  # creator owner/group, owner rights
WELL_KNOWN = (
    "S-1-1-0",  # Everyone
    "S-1-5-7",  # Anonymous
    "S-1-5-4",  # Interactive
    "S-1-5-11",  # Authenticated Users
    "S-1-5-32-545",  # Users
    "S-1-5-32-546",  # Guests
    "S-1-5-32-544",  # Administrators
    "S-1-5-18",  # SYSTEM
)


class MalformedDescriptor(ValueError):
    """The bytes are not a well-formed self-relative security descriptor."""


class AclReason(StrEnum):
    UNREADABLE = "descriptor_unreadable"
    MALFORMED = "descriptor_malformed"
    NO_OWNER = "no_owner"
    OWNER_DIFFERS = "owner_differs"
    NO_DACL = "no_dacl"
    NULL_DACL = "null_dacl"  # present but NULL: no access control at all
    UNKNOWN_CONTROL = "unknown_control"
    UNSUPPORTED_DENY = "unsupported_deny_semantics"  # group semantics not proven here
    UNKNOWN_ACE = "unknown_ace"
    UNKNOWN_FLAGS = "unknown_ace_flags"
    UNKNOWN_RIGHTS = "unknown_rights"
    LABEL = "mandatory_label"
    UNKNOWN_SACL = "unknown_sacl"
    DIRECTORY_WIDER = "directory_wider"
    UNVERIFIED = "unverified"  # an effective access could not be computed
    MISMATCH = "descriptor_mismatch"  # the object does not have what it was given


@dataclass(frozen=True, slots=True)
class AclRefusal:
    reason: AclReason
    detail: str


# --- the binary form ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sid:
    data: bytes

    @classmethod
    def parse(cls, text: str) -> Sid:
        """``S-1-<authority>-<sub>...`` to its binary form."""
        parts = text.split("-")
        if len(parts) < 3 or parts[0] != "S" or parts[1] != "1" or len(parts) > 18:
            raise ValueError(f"not a SID: {text!r}")
        authority, subs = int(parts[2]), [int(p) for p in parts[3:]]
        if not 0 <= authority < 1 << 48 or any(not 0 <= s < 1 << 32 for s in subs):
            raise ValueError(f"not a SID: {text!r}")
        head = bytes((1, len(subs))) + authority.to_bytes(6, "big")
        return cls(head + b"".join(s.to_bytes(4, "little") for s in subs))

    def __str__(self) -> str:
        authority = int.from_bytes(self.data[2:8], "big")
        count = self.data[1]
        subs = struct.unpack_from(f"<{count}I", self.data, 8)
        return "-".join(["S", str(self.data[0]), str(authority), *map(str, subs)])


def _sid_at(data: bytes, offset: int, end: int) -> Sid:
    if offset < 0 or offset + 8 > end:
        raise MalformedDescriptor(f"SID at {offset} beyond {end}")
    revision, count = data[offset], data[offset + 1]
    length = 8 + 4 * count
    if revision != 1 or count > 15 or offset + length > end:
        raise MalformedDescriptor(f"SID at {offset}: revision {revision}, {count} sub-authorities")
    return Sid(bytes(data[offset : offset + length]))


@dataclass(frozen=True, slots=True)
class Ace:
    """One ACE; ``mask`` and ``sid`` only for the types whose layout is known here."""

    type: int
    flags: int
    mask: int | None = None
    sid: Sid | None = None

    def pack(self) -> bytes:
        assert self.mask is not None and self.sid is not None
        size = 8 + len(self.sid.data)
        return struct.pack("<BBHI", self.type, self.flags, size, self.mask) + self.sid.data

    def __str__(self) -> str:
        kind = {ACCESS_ALLOWED_ACE_TYPE: "allow", ACCESS_DENIED_ACE_TYPE: "deny"}
        name = kind.get(self.type, f"type {self.type:#x}")
        mask = "?" if self.mask is None else f"{self.mask:#x}"
        return f"{name} {self.sid} {mask} flags {self.flags:#x}"


_SIMPLE = frozenset(
    {ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE, SYSTEM_MANDATORY_LABEL_ACE_TYPE}
)


def _acl_at(data: bytes, offset: int) -> tuple[Ace, ...]:
    if offset + 8 > len(data):
        raise MalformedDescriptor(f"ACL at {offset} beyond {len(data)}")
    revision, _, size, count, _ = struct.unpack_from("<BBHHH", data, offset)
    end = offset + size
    if revision not in (2, 4) or size < 8 or end > len(data):
        raise MalformedDescriptor(f"ACL at {offset}: revision {revision}, size {size}")
    aces: list[Ace] = []
    at = offset + 8
    for _ in range(count):
        if at + 4 > end:
            raise MalformedDescriptor(f"ACE at {at} beyond the ACL")
        kind, flags, ace_size = struct.unpack_from("<BBH", data, at)
        if ace_size < 8 or ace_size % 4 or at + ace_size > end:
            raise MalformedDescriptor(f"ACE at {at}: size {ace_size}")
        if kind in _SIMPLE:
            (mask,) = struct.unpack_from("<I", data, at + 4)
            sid = _sid_at(data, at + 8, at + ace_size)
            if 8 + len(sid.data) != ace_size:
                raise MalformedDescriptor(f"ACE at {at}: {ace_size} bytes for its SID")
            aces.append(Ace(kind, flags, mask, sid))
        else:
            aces.append(Ace(kind, flags))
        at += ace_size
    return tuple(aces)


@dataclass(frozen=True, slots=True)
class Descriptor:
    """A parsed self-relative security descriptor (owner, group, DACL, label SACL)."""

    control: int
    owner: Sid | None
    group: Sid | None
    dacl: tuple[Ace, ...] | None  # None: not present, or present and NULL
    sacl: tuple[Ace, ...] | None
    raw: bytes = field(repr=False, default=b"")

    @property
    def dacl_present(self) -> bool:
        return bool(self.control & SE_DACL_PRESENT)

    @property
    def null_dacl(self) -> bool:
        return self.dacl_present and self.dacl is None

    @property
    def protected(self) -> bool:
        return bool(self.control & SE_DACL_PROTECTED)

    @classmethod
    def parse(cls, data: bytes) -> Descriptor:
        if len(data) < 20:
            raise MalformedDescriptor(f"{len(data)} bytes")
        revision, _, control, owner, group, sacl, dacl = struct.unpack_from("<BBHIIII", data)
        if revision != 1 or not control & SE_SELF_RELATIVE:
            raise MalformedDescriptor(f"revision {revision}, control {control:#x}")
        return cls(
            control,
            _sid_at(data, owner, len(data)) if owner else None,
            _sid_at(data, group, len(data)) if group else None,
            _acl_at(data, dacl) if control & SE_DACL_PRESENT and dacl else None,
            _acl_at(data, sacl) if control & SE_SACL_PRESENT and sacl else None,
            bytes(data),
        )


def build_descriptor(aces: tuple[Ace, ...], *, protected: bool = True) -> bytes:
    """A self-relative descriptor with only a DACL: the owner and the group are the
    creator's defaults."""
    body = b"".join(ace.pack() for ace in aces)
    acl = struct.pack("<BBHHH", 2, 0, 8 + len(body), len(aces), 0) + body
    control = SE_SELF_RELATIVE | SE_DACL_PRESENT | (SE_DACL_PROTECTED if protected else 0)
    return struct.pack("<BBHIIII", 1, 0, control, 0, 0, 0, 20) + acl


# --- the policy --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Derived:
    """The descriptor to create the copy (or its directory) with, and what it holds."""

    aces: tuple[Ace, ...]
    owner: Sid
    descriptor: bytes = field(repr=False)


def supported(descriptor: Descriptor, what: str) -> AclRefusal | None:
    """Why ``descriptor`` is outside what the comparison can prove (module docstring),
    or None: every component is checked, none is left out as if it were not there."""
    if descriptor.owner is None:
        return AclRefusal(AclReason.NO_OWNER, f"{what} has no owner")
    if not descriptor.dacl_present:
        return AclRefusal(AclReason.NO_DACL, f"{what} has no DACL")
    if descriptor.dacl is None:
        return AclRefusal(AclReason.NULL_DACL, f"{what} has a NULL DACL")
    if descriptor.control & ~_KNOWN_CONTROL:
        detail = f"{what} has control {descriptor.control:#06x}"
        return AclRefusal(AclReason.UNKNOWN_CONTROL, detail)
    for ace in descriptor.sacl or ():
        if ace.type == SYSTEM_MANDATORY_LABEL_ACE_TYPE:
            return AclRefusal(AclReason.LABEL, f"{what} has a mandatory label: {ace}")
        return AclRefusal(AclReason.UNKNOWN_SACL, f"the SACL of {what} has {ace}")
    for ace in descriptor.dacl:
        if ace.type == ACCESS_DENIED_ACE_TYPE:
            return AclRefusal(AclReason.UNSUPPORTED_DENY, f"the DACL of {what} has {ace}")
        if ace.type != ACCESS_ALLOWED_ACE_TYPE:
            return AclRefusal(AclReason.UNKNOWN_ACE, f"the DACL of {what} has {ace}")
        assert ace.mask is not None and ace.sid is not None
        if ace.flags & ~_KNOWN_ACE_FLAGS:
            return AclRefusal(AclReason.UNKNOWN_FLAGS, f"the DACL of {what} has {ace}")
        if ace.mask & ~FILE_ALL_ACCESS or ace.mask == 0:
            return AclRefusal(AclReason.UNKNOWN_RIGHTS, f"the DACL of {what} has {ace}")
    return None


def derive(source: Descriptor, creator: Sid, *, directory: bool = False) -> Derived | AclRefusal:
    """The protected DACL for the copy of ``source`` (module docstring), or why not."""
    refusal = supported(source, "the source directory" if directory else "the source")
    if refusal is not None:
        return refusal
    if source.owner != creator:
        detail = f"the source is owned by {source.owner}, the copy would be by {creator}"
        return AclRefusal(AclReason.OWNER_DIFFERS, detail)
    aces: list[Ace] = []
    for ace in source.dacl or ():
        if directory:
            aces.append(
                Ace(ace.type, ace.flags & (_PROPAGATION | INHERIT_ONLY_ACE), ace.mask, ace.sid)
            )
        elif not ace.flags & INHERIT_ONLY_ACE:
            aces.append(Ace(ace.type, 0, ace.mask, ace.sid))
    kept = tuple(aces)
    return Derived(kept, creator, build_descriptor(kept))


def check_created(actual: Descriptor, derived: Derived) -> AclRefusal | None:
    """Has the new object exactly the security it was given (after create or rename)?"""
    problems = []
    if actual.owner != derived.owner:
        problems.append(f"owner {actual.owner}, expected {derived.owner}")
    if not actual.protected:
        problems.append("DACL not protected")
    if actual.dacl != derived.aces:
        shown = "NULL" if actual.dacl is None else "; ".join(map(str, actual.dacl))
        problems.append(f"DACL {shown}")
    if actual.sacl:
        problems.append(f"SACL {'; '.join(map(str, actual.sacl))}")
    if problems:
        return AclRefusal(AclReason.MISMATCH, ", ".join(problems))
    return None


def principals(*descriptors: Descriptor, creator: Sid) -> tuple[Sid, ...]:
    """Every principal the comparison asks about: the well-known ones, the creator, the
    owners and every trustee of the DACLs (placeholders left out), each once."""
    found: dict[bytes, Sid] = {}
    candidates = [Sid.parse(s) for s in WELL_KNOWN] + [creator]
    for descriptor in descriptors:
        candidates += [s for s in (descriptor.owner,) if s is not None]
        candidates += [a.sid for a in descriptor.dacl or () if a.sid is not None]
    for sid in candidates:
        if str(sid) not in _PLACEHOLDERS:
            found.setdefault(sid.data, sid)
    return tuple(found.values())


# --- Windows ------------------------------------------------------------------------


class _Api:
    def __init__(self) -> None:
        from ctypes import wintypes as wt

        self.wt = wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        az = ctypes.WinDLL("authz", use_last_error=True)
        ptr, pptr = ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        k32.CreateFileW.argtypes = [
            wt.LPCWSTR,
            wt.DWORD,
            wt.DWORD,
            ptr,
            wt.DWORD,
            wt.DWORD,
            wt.HANDLE,
        ]
        k32.CreateFileW.restype = wt.HANDLE
        k32.CloseHandle.argtypes = [wt.HANDLE]
        k32.CloseHandle.restype = wt.BOOL
        k32.GetCurrentProcess.restype = wt.HANDLE
        k32.LocalFree.argtypes = [ptr]
        k32.LocalFree.restype = ptr

        class SecurityAttributes(ctypes.Structure):
            _fields_ = (
                ("nLength", wt.DWORD),
                ("lpSecurityDescriptor", ptr),
                ("bInheritHandle", wt.BOOL),
            )

        k32.CreateDirectoryW.argtypes = [wt.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
        k32.CreateDirectoryW.restype = wt.BOOL
        adv.GetSecurityInfo.argtypes = [wt.HANDLE, ctypes.c_int, wt.DWORD, ptr, ptr, ptr, ptr, pptr]
        adv.GetSecurityInfo.restype = wt.DWORD
        adv.GetSecurityDescriptorLength.argtypes = [ptr]
        adv.GetSecurityDescriptorLength.restype = wt.DWORD
        adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        adv.OpenProcessToken.restype = wt.BOOL
        adv.GetTokenInformation.argtypes = [
            wt.HANDLE,
            ctypes.c_int,
            ptr,
            wt.DWORD,
            ctypes.POINTER(wt.DWORD),
        ]
        adv.GetTokenInformation.restype = wt.BOOL
        adv.GetLengthSid.argtypes = [ptr]
        adv.GetLengthSid.restype = wt.DWORD

        class Luid(ctypes.Structure):
            _fields_ = (("LowPart", wt.DWORD), ("HighPart", wt.LONG))

        class Request(ctypes.Structure):
            _fields_ = (
                ("DesiredAccess", wt.DWORD),
                ("PrincipalSelfSid", ptr),
                ("ObjectTypeList", ptr),
                ("ObjectTypeListLength", wt.DWORD),
                ("OptionalArguments", ptr),
            )

        class Reply(ctypes.Structure):
            _fields_ = (
                ("ResultListLength", wt.DWORD),
                ("GrantedAccessMask", ctypes.POINTER(wt.DWORD)),
                ("SaclEvaluationResults", ctypes.POINTER(wt.DWORD)),
                ("Error", ctypes.POINTER(wt.DWORD)),
            )

        az.AuthzInitializeResourceManager.argtypes = [wt.DWORD, ptr, ptr, ptr, wt.LPCWSTR, pptr]
        az.AuthzInitializeResourceManager.restype = wt.BOOL
        az.AuthzInitializeContextFromSid.argtypes = [wt.DWORD, ptr, ptr, ptr, Luid, ptr, pptr]
        az.AuthzInitializeContextFromSid.restype = wt.BOOL
        az.AuthzAccessCheck.argtypes = [
            wt.DWORD, ptr, ctypes.POINTER(Request), ptr, ptr, ptr, wt.DWORD,
            ctypes.POINTER(Reply), ptr,
        ]  # fmt: skip
        az.AuthzAccessCheck.restype = wt.BOOL
        az.AuthzFreeContext.argtypes = [ptr]
        az.AuthzFreeResourceManager.argtypes = [ptr]
        self.k32, self.adv, self.az = k32, adv, az
        self.SecurityAttributes, self.Luid, self.Request, self.Reply = (
            SecurityAttributes,
            Luid,
            Request,
            Reply,
        )


_API: _Api | None = None


def _api() -> _Api:
    global _API
    if _API is None:
        _API = _Api()
    return _API


def _error(code: int, what: str) -> OSError:
    return OSError(0, f"{what}: {ctypes.FormatError(code).strip()}", None, code)


_INVALID = ctypes.c_void_p(-1).value
_OPEN_EXISTING = 3
_FLAG_BACKUP_SEMANTICS = 0x0200_0000
_FLAG_OPEN_REPARSE_POINT = 0x0020_0000


def _descriptor_of_raw(raw: int, what: str) -> bytes:
    api = _api()
    pointer = ctypes.c_void_p()
    code = api.adv.GetSecurityInfo(
        raw, 1, _READ_INFO, None, None, None, None, ctypes.byref(pointer)
    )
    if code:
        raise _error(code, f"reading the security of {what}")
    try:
        return ctypes.string_at(pointer, api.adv.GetSecurityDescriptorLength(pointer))
    finally:
        api.k32.LocalFree(pointer)


def read_descriptor(handle: Handle) -> bytes:
    """The owner, group, DACL and label of an open handle (it needs ``READ_CONTROL``)."""
    return _descriptor_of_raw(handle.raw, str(handle.path))


def read_descriptor_at(path: Path) -> bytes:
    """The same for ``path`` itself (a reparse point is not followed); for the tests and
    the prototype, the production gate would read through the run's handles."""
    api = _api()
    raw = api.k32.CreateFileW(
        str(path),
        READ_CONTROL,
        winfileops.SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FLAG_BACKUP_SEMANTICS | _FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if raw in (None, 0, _INVALID):
        raise _error(ctypes.get_last_error(), f"opening {path} for its security")
    try:
        return _descriptor_of_raw(raw, str(path))
    finally:
        api.k32.CloseHandle(raw)


_TOKEN_QUERY = 0x0008
_TOKEN_OWNER = 4


def creator_sid() -> Sid:
    """The default owner of what this process creates (``TokenOwner``)."""
    api = _api()
    token = api.wt.HANDLE()
    if not api.adv.OpenProcessToken(api.k32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _error(ctypes.get_last_error(), "OpenProcessToken")
    try:
        size = api.wt.DWORD()
        api.adv.GetTokenInformation(token, _TOKEN_OWNER, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not api.adv.GetTokenInformation(token, _TOKEN_OWNER, buffer, size, ctypes.byref(size)):
            raise _error(ctypes.get_last_error(), "GetTokenInformation(TokenOwner)")
        sid = ctypes.c_void_p.from_buffer(buffer).value
        return Sid(ctypes.string_at(sid, api.adv.GetLengthSid(sid)))
    finally:
        api.k32.CloseHandle(token)


_AUTHZ_RM_FLAG_NO_AUDIT = 0x1
_AUTHZ_SKIP_TOKEN_GROUPS = 0x2


class UnverifiedAccess(OSError):
    """``AuthzAccessCheck`` answered with something that is neither a confirmed grant
    nor a confirmed denial: the rights are unknown, never taken as zero."""


def _confirmed(granted: int, error: int, what: str) -> int:
    """The rights of one ``AUTHZ_ACCESS_REPLY`` entry for ``MAXIMUM_ALLOWED``: ``Error``
    0 with a mask (granted), ``ERROR_ACCESS_DENIED`` with mask 0 (nothing granted);
    any other combination raises ``UnverifiedAccess`` with both values."""
    if (error == 0 and granted != 0) or (error == ERROR_ACCESS_DENIED and granted == 0):
        return granted
    text = ctypes.FormatError(error).strip() if error else "no error"
    raise UnverifiedAccess(
        0, f"{what}: reply error {error} ({text}), granted {granted:#x}", None, error
    )


def effective_access(descriptor: bytes, sid: Sid, *, what: str = "the descriptor") -> int:
    """The rights ``descriptor`` grants a context of ``sid`` alone (no groups):
    ``AuthzAccessCheck`` for ``MAXIMUM_ALLOWED``, owner rights included. A failed call
    raises ``OSError``, an answer that is not confirmed ``UnverifiedAccess``."""
    api = _api()
    manager, context = ctypes.c_void_p(), ctypes.c_void_p()
    if not api.az.AuthzInitializeResourceManager(
        _AUTHZ_RM_FLAG_NO_AUDIT, None, None, None, None, ctypes.byref(manager)
    ):
        raise _error(ctypes.get_last_error(), "AuthzInitializeResourceManager")
    try:
        sid_buffer = ctypes.create_string_buffer(sid.data, len(sid.data))
        if not api.az.AuthzInitializeContextFromSid(
            _AUTHZ_SKIP_TOKEN_GROUPS, sid_buffer, manager, None, api.Luid(0, 0), None,
            ctypes.byref(context),
        ):  # fmt: skip
            raise _error(ctypes.get_last_error(), f"AuthzInitializeContextFromSid({sid})")
        try:
            sd = ctypes.create_string_buffer(descriptor, len(descriptor))
            request = api.Request(MAXIMUM_ALLOWED, None, None, 0, None)
            granted, error, sacl = api.wt.DWORD(), api.wt.DWORD(), api.wt.DWORD()
            reply = api.Reply(
                1, ctypes.pointer(granted), ctypes.pointer(sacl), ctypes.pointer(error)
            )
            if not api.az.AuthzAccessCheck(
                0, context, ctypes.byref(request), None, sd, None, 0, ctypes.byref(reply), None
            ):
                raise _error(ctypes.get_last_error(), f"AuthzAccessCheck({sid}) on {what}")
            return _confirmed(granted.value, error.value, f"AuthzAccessCheck({sid}) on {what}")
        finally:
            api.az.AuthzFreeContext(context)
    finally:
        api.az.AuthzFreeResourceManager(manager)


def wider_principals(
    target: Descriptor, source: Descriptor, sids: tuple[Sid, ...], rights: int
) -> tuple[tuple[Sid, int], ...]:
    """Each principal that ``target`` grants any of ``rights`` that ``source`` does not,
    with those extra rights. Sound only for ALLOW-only DACLs (``supported``); raises
    ``OSError`` (``UnverifiedAccess`` among them) if any one answer is not confirmed."""
    wider = []
    for sid in sids:
        theirs = effective_access(target.raw, sid, what="the target")
        ours = effective_access(source.raw, sid, what="the source")
        extra = theirs & ~ours & rights
        if extra:
            wider.append((sid, extra))
    return tuple(wider)


def create_temp_with(handle: Handle, parent: Handle, name: str, descriptor: bytes) -> OpenOutcome:
    """``winprims.create_into`` with ``descriptor`` given to ``NtCreateFile`` itself (the
    file never exists without it), and ``READ_CONTROL`` to check it afterwards."""
    winfileops._require(handle.role == "H_TMP", f"create_temp_with for {handle.role}")
    winfileops._require(handle.state is HandleState.NEW, f"create into a {handle.state} handle")
    winfileops._single_component(name)
    profile = winprims.PROFILES["H_TMP"]
    api = winprims._prims()
    base = api.base
    buffer = ctypes.create_unicode_buffer(name)
    length = len(name.encode("utf-16-le", "surrogatepass"))
    uname = base.UnicodeString(length, length + 2, ctypes.addressof(buffer))
    sd = ctypes.create_string_buffer(descriptor, len(descriptor))
    attributes = base.ObjectAttributes(
        ctypes.sizeof(base.ObjectAttributes), parent.raw, ctypes.pointer(uname),
        winfileops._OBJ_CASE_INSENSITIVE, ctypes.addressof(sd), None,
    )  # fmt: skip
    handle._keep = (buffer, uname, sd, attributes)
    handle._out = base.wintypes.HANDLE(0)
    handle._iosb = base.IoStatusBlock(0, 0xFFFF_FFFF)
    handle._status = None
    handle.state = HandleState.CALLING
    try:
        handle._status = api.ntdll.NtCreateFile(
            ctypes.byref(handle._out),
            profile.access | READ_CONTROL,
            ctypes.byref(attributes),
            ctypes.byref(handle._iosb),
            None,
            profile.attributes,
            profile.share,
            profile.disposition,
            profile.options,
            None,
            0,
        )
    except BaseException:
        if handle._status is None:
            handle.classify(profile.disposition)
        raise
    return handle.classify(profile.disposition)


def create_directory_with(path: Path, descriptor: bytes) -> None:
    """Create the directory ``path`` (never an existing one) with ``descriptor``."""
    api = _api()
    sd = ctypes.create_string_buffer(descriptor, len(descriptor))
    attributes = api.SecurityAttributes(
        ctypes.sizeof(api.SecurityAttributes), ctypes.addressof(sd), False
    )
    if not api.k32.CreateDirectoryW(str(path), ctypes.byref(attributes)):
        raise _error(ctypes.get_last_error(), f"creating {path}")


# --- the assessment of one copy -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Assessment:
    """``assess``: a refusal, or the descriptors to create the copy and its directory."""

    refusal: AclRefusal | None
    file: Derived | None = None
    directory: Derived | None = None  # only if the target directory does not exist yet
    wider: tuple[tuple[Sid, int], ...] = ()

    @property
    def allowed(self) -> bool:
        return self.refusal is None


def _read(path: Path) -> Descriptor | AclRefusal:
    try:
        return Descriptor.parse(read_descriptor_at(path))
    except MalformedDescriptor as exc:
        return AclRefusal(AclReason.MALFORMED, f"{path}: {exc}")
    except OSError as exc:
        return AclRefusal(AclReason.UNREADABLE, f"{path}: {winfileops.describe_error(exc)}")


def assess(source: Path, target_dir: Path, *, creator: Sid | None = None) -> Assessment:
    """Whether the copy of ``source`` into ``target_dir`` can keep the policy (module
    docstring), read only: nothing is created or changed."""
    try:
        owner = creator_sid() if creator is None else creator
    except OSError as exc:
        return Assessment(AclRefusal(AclReason.UNVERIFIED, winfileops.describe_error(exc)))
    file_sd = _read(source)
    if isinstance(file_sd, AclRefusal):
        return Assessment(file_sd)
    source_dir = _read(source.parent)
    if isinstance(source_dir, AclRefusal):
        return Assessment(source_dir)
    target = _read(target_dir) if target_dir.exists() else None
    if isinstance(target, AclRefusal):
        return Assessment(target)
    return judge(file_sd, source_dir, target, owner)


def judge(
    file_sd: Descriptor, source_dir: Descriptor, target: Descriptor | None, creator: Sid
) -> Assessment:
    """``assess`` on descriptors already read; ``target`` None: no target directory yet.
    Every descriptor that takes part is checked by ``supported`` before Authz is asked."""
    derived = derive(file_sd, creator)
    if isinstance(derived, AclRefusal):
        return Assessment(derived)
    if target is None:
        made = derive(source_dir, creator, directory=True)
        if isinstance(made, AclRefusal):
            return Assessment(made)
        return Assessment(None, derived, made)
    for descriptor, what in ((source_dir, "the source directory"), (target, "the target")):
        refusal = supported(descriptor, what)
        if refusal is not None:
            return Assessment(refusal)
    try:
        wider = wider_principals(
            target, source_dir, principals(target, source_dir, creator=creator), DIRECTORY_RIGHTS
        )
    except OSError as exc:
        return Assessment(AclRefusal(AclReason.UNVERIFIED, winfileops.describe_error(exc)))
    if wider:
        detail = "; ".join(f"{sid} gets {extra:#x} more" for sid, extra in wider)
        return Assessment(AclRefusal(AclReason.DIRECTORY_WIDER, detail), wider=wider)
    return Assessment(None, derived)
