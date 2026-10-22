"""R3B-7A: create a category directory and a temporary file relative to proven handles,
each with its security descriptor given to ``NtCreateFile`` itself (``aclproof``).

Used by the copy path only (R3B integration): ``runner`` creates the missing
directories of a copy run with ``create_category``, ``copycommit`` its temporary files
with ``create_temp``; the move, the CLI and the GUI never import it. The production gate
stays ``runner.UndecidedAcl`` and ``features.CROSS_VOLUME_COPY`` stays False. Nothing
here copies data, renames into a final name, recovers or removes anything but what the
same call has just created.

``create_category`` creates the directory ``name`` in the directory of a held ``parent``
handle; ``create_temp`` creates the temporary file ``name`` in the directory of a held
category handle. Both, in this order:

1. check the descriptor to create with: the one ``aclproof.derive`` made, ALLOW only
   (``_given``); for ``create_temp`` also the source directory and the category
   directory (read through its handle), and that the category gives no principal more
   directory rights than the source directory (Authz, principal by principal);
2. prove the parent from its handle (``prove_parent``): a directory, no reparse point,
   not being deleted, the expected volume serial and file id (``Identity``);
3. one ``NtCreateFile`` (``create_with``): one name component relative to the parent
   handle (``RootDirectory``), never a path; ``FILE_CREATE``, so an existing name (a
   junction, a symlink or a mount point among them) is never opened (``NAME_TAKEN``);
   ``FILE_OPEN_REPARSE_POINT``; the descriptor in ``OBJECT_ATTRIBUTES``: the object never
   exists with an inherited DACL, and nothing here ever sets a descriptor afterwards;
4. check the new object through the creating handle (``_check_new``): what it is
   (a directory or a file, no reparse point, one link, empty, on the parent's volume),
   its descriptor (``verify``: owner, protected, exactly the derived ACEs, no SACL,
   ``supported``) and, by Authz, no right of any principal wider than at the source;
5. if a check fails: remove only what this call created and holds open
   (``CREATED_OWNED``, the creating handle), close it once, look the name up, and say
   whether the removal is proven (``REMOVED``) or not (``LEFT``, ``REMOVAL_UNKNOWN``).

After an unknown outcome of the create call nothing is closed (``UNKNOWN`` handles never
are) and nothing is removed. A failure of any close is kept, as the object, in
``Creation.release_errors`` (or on the interrupt raised, ``winfileops.release_after``).

R3B-7B1, exceptions: ``Creation.state`` always says what is established so far (it is
set before each step, the most cautious state first), so an exception leaving a step
finds it true. Then (``_escaped``) the very exception is kept (``Creation.raised``, its
traceback on it), the handle is closed once if it is still ``OPEN`` (an ``UNKNOWN`` one
never), nothing is created again, looked up or removed, and the ``Creation`` is
attached to what is raised (``attach_creation``: ``exc.creations`` and a note; also to
a ``ReleaseError`` or an interrupt of the close raised in its place). A closed handle
never means that the object is gone: an object created and not removed is ``UNCHECKED``
(the checks did not end), ``LEFT`` or ``REMOVAL_UNKNOWN``. ``temp_record`` and
``journal_temp`` turn the ``Creation`` of a temporary file into the JSONL v2 record
after its copy intent (``copycommit``).

R3B-7B2A, for the integration to come: an unresolved ``UNKNOWN`` (a create call whose
outcome is not known, its raw handle, if any, left open: never closed here) stops the
run; no further file operation may start after it, and nothing recovers it on its own.
A category (``H_DIR``) has no JSONL v2 record that can carry its state or identity
(``temp_record`` refuses it): the copy run journals it in v3 (``journal_v3``: the
parent's id in a durable intent, then ``DirState`` and the directory's id).

The creating handle is returned open (``CREATED``). Its access was granted on create and
is not limited by the new DACL (an ``aclproof`` test shows a creating handle writing where
the DACL allows reading only): it is not shared, so nobody else holds that access. A file
handle shares nothing; a directory handle shares no delete (not renamed or removed while
held). Judged are the parsed binary descriptor and Authz answers, never SDDL text.
"""

from __future__ import annotations

import ctypes
import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from downloads_organizer import aclproof, winfileops, winprims
from downloads_organizer.aclproof import (
    AclReason,
    AclRefusal,
    Derived,
    Descriptor,
    MalformedDescriptor,
    Sid,
)
from downloads_organizer.journal_v2 import (
    TEMP_CREATE_UNCERTAIN,
    TEMP_NAME_TAKEN,
    Commit,
    Diagnostics,
    JournalError,
    JournalV2,
    OperationV2,
    StatusV2,
    TempState,
    VolumeRef,
)
from downloads_organizer.winfileops import (
    Handle,
    HandleInfo,
    HandleState,
    OpenOutcome,
    _require,
    release_after,
)
from downloads_organizer.winprims import Disposal, Disposed, Lookup, LookupKind, Profile

# The category directory: listed, files added to it, its security read, removed after a
# failed check; without FILE_SHARE_DELETE nobody renames or removes it while it is held.
DIR_PROFILE = Profile(
    winfileops.FILE_LIST_DIRECTORY | winfileops.FILE_ADD_FILE | winfileops.FILE_READ_ATTRIBUTES
    | aclproof.READ_CONTROL | winfileops.DELETE | winfileops.SYNCHRONIZE,
    winfileops.FILE_SHARE_READ | winfileops.FILE_SHARE_WRITE,
    winfileops._FILE_CREATE,
    winfileops._FILE_DIRECTORY_FILE | winfileops._FILE_OPEN_REPARSE_POINT
    | winfileops._FILE_SYNCHRONOUS_IO_NONALERT,
)  # fmt: skip
# The temporary file: the H_TMP profile (share 0) and READ_CONTROL to check it.
_TMP = winprims.PROFILES["H_TMP"]
TMP_PROFILE = Profile(
    _TMP.access | aclproof.READ_CONTROL, _TMP.share, _TMP.disposition, _TMP.options,
    _TMP.attributes,
)  # fmt: skip
PROFILES = {"H_DIR": DIR_PROFILE, "H_TMP": TMP_PROFILE}
_GIVEN_CONTROL = aclproof.SE_SELF_RELATIVE | aclproof.SE_DACL_PRESENT | aclproof.SE_DACL_PROTECTED


@dataclass(frozen=True, slots=True)
class Identity:
    """Which directory a handle must be: its volume serial and 128-bit file id."""

    volume_serial: int
    file_id: int

    @classmethod
    def of(cls, info: HandleInfo) -> Identity:
        return cls(info.volume_serial, info.file_id)


class CreateReason(StrEnum):
    PARENT_UNREADABLE = "parent_unreadable"  # the parent handle could not be queried
    PARENT_NOT_DIRECTORY = "parent_not_directory"
    PARENT_REPARSE = "parent_reparse_point"
    PARENT_DELETE_PENDING = "parent_delete_pending"
    PARENT_VOLUME = "parent_other_volume"
    PARENT_IDENTITY = "parent_other_object"
    NAME_TAKEN = "name_taken"  # STATUS_OBJECT_NAME_COLLISION: nothing was opened
    CREATE_FAILED = "create_failed"  # an error status, and the name is not there
    NOT_PROVEN_OWN = "not_proven_own"  # a handle, but not proven created by this call
    OUTCOME_UNKNOWN = "outcome_unknown"  # what the create call did is not known
    OBJECT_PROBLEM = "object_problem"  # the new object is not what was asked for
    WIDER = "wider_than_source"  # Authz: a principal gets more than at the source
    EXCEPTION = "exception"  # an exception (``Creation.raised``) cut the call off


@dataclass(frozen=True, slots=True)
class CreateRefusal:
    reason: CreateReason
    detail: str


Refusal = AclRefusal | CreateRefusal


class Created(StrEnum):
    CREATED = "created"  # created by this call, held open, every check passed
    REFUSED = "refused"  # proven: this call created nothing
    UNCHECKED = "unchecked"  # created by this call, its checks cut off: it is there
    UNKNOWN = "unknown"  # not known what the create call did: maybe an object, not ours
    REMOVED = "removed"  # created, a check failed, its removal proven by a lookup
    LEFT = "left"  # created, a check failed, still there (removal refused or not proven)
    REMOVAL_UNKNOWN = "removal_unknown"  # created, its removal begun: gone or not, unknown


class Stage(StrEnum):
    """The last step ``_create`` began (with ``Creation.raised``: the one that raised)."""

    PREPARE = "prepare"  # the descriptor to create with, the parent, the category
    CREATE = "create"  # the one create call and its classification
    NOT_CREATED = "not_created"  # an error status: the name looked up
    RELEASE = "release"  # a handle not proven created by this call is closed
    QUERY = "query"  # the new object and the parent queried (the identity)
    DESCRIPTOR = "descriptor"  # the new object's descriptor read and compared
    AUTHZ = "authz"  # Authz, principal by principal
    REMOVE = "remove"  # the disposition, the close, the lookup


@dataclass(eq=False)
class Creation:
    """The result of ``create_category`` or ``create_temp``; also attached to an exception
    leaving them (``attach_creation``). Only what was established is set: ``identity``
    once the new object was queried through the creating handle, ``parent`` once proven,
    ``status`` once captured. ``error`` is the error behind ``refusal``, ``raised`` the
    exception that cut the call off, ``release_errors`` the failures of the closes: never
    one in place of another."""

    state: Created
    refusal: Refusal | None = None
    handle: Handle | None = None  # CREATED only: open, the caller's to close
    identity: Identity | None = None  # of the new object, once it was queried
    derived: Derived | None = None
    outcome: OpenOutcome | None = None
    error: BaseException | None = None  # the error behind ``refusal``, as raised
    release_errors: tuple[BaseException, ...] = ()
    disposal: Disposal | None = None
    lookup: Lookup | None = None
    role: str | None = None  # H_DIR or H_TMP
    name: str | None = None  # the one name component created in the parent
    parent: Identity | None = None  # the parent, once proven from its handle
    status: int | None = None  # the NTSTATUS of the create call (unsigned), if captured
    owned: bool = False  # the create call proven to have created it (CREATED_OWNED)
    stage: Stage = Stage.PREPARE
    handle_state: HandleState | None = None  # of the creating handle when the call ended
    raised: BaseException | None = None  # the very exception that left the call

    @property
    def allowed(self) -> bool:
        return self.state is Created.CREATED

    def describe(self) -> str:
        status = None if self.status is None else f"{self.status:#010x}"
        identity = None if self.identity is None else f"{self.identity.file_id:032x}"
        reason = None if self.refusal is None else self.refusal.reason
        disposal = None if self.disposal is None else self.disposal.result
        return (
            f"{self.state}, stage {self.stage}, outcome {self.outcome}, status {status},"
            f" owned {self.owned}, identity {identity}, handle {self.handle_state},"
            f" reason {reason}, disposal {disposal}, release errors {len(self.release_errors)}"
        )


def attach_creation(exc: BaseException, creation: Creation) -> BaseException:
    """Add ``creation`` to ``exc.creations`` (each object once) and a note; returns ``exc``
    (the ``copycommit.attach_transfer_state`` way). Works on interrupts as well."""
    current = tuple(getattr(exc, "creations", ()))
    if all(c is not creation for c in current):
        exc.creations = (*current, creation)  # type: ignore[attr-defined]
        exc.add_note(f"create {creation.role} {creation.name}: {creation.describe()}")
    return exc


class Ops(Protocol):
    def new(self, path: Path, role: str) -> Handle: ...
    def create_with(
        self, handle: Handle, parent: Handle, name: str, descriptor: bytes
    ) -> OpenOutcome: ...
    def open_error(self, handle: Handle) -> OSError: ...
    def query(self, handle: Handle) -> HandleInfo: ...
    def read_descriptor(self, handle: Handle) -> bytes: ...
    def effective_access(self, descriptor: bytes, sid: Sid, what: str) -> int: ...
    def lookup_entry(self, handle: Handle, directory: Handle, name: str) -> Lookup: ...
    def dispose(self, handle: Handle, disposal: Disposal) -> Disposed: ...
    def close(self, handle: Handle) -> None: ...


# --- the Windows primitives -------------------------------------------------------------


def create_with(handle: Handle, parent: Handle, name: str, descriptor: bytes) -> OpenOutcome:
    """Create ``name`` in the directory of ``parent`` into ``handle`` (a ``Handle.new`` of
    role ``H_DIR``: a directory, ``H_TMP``: a file) as ``PROFILES[role]``, with
    ``descriptor`` in ``OBJECT_ATTRIBUTES.SecurityDescriptor`` of that very call.

    Classified like ``winprims.create_into`` (``Handle.classify``); an error status is
    not raised, an interrupt is (the handle is ``UNKNOWN`` if no status was captured).
    """
    _require(handle.role in PROFILES, f"create_with for {handle.role}")
    _require(handle.state is HandleState.NEW, f"create into a {handle.state} handle")
    winfileops._single_component(name)
    profile = PROFILES[handle.role]  # type: ignore[index]
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
    handle._iosb = base.IoStatusBlock(0, 0xFFFF_FFFF)  # a sentinel: proves nothing
    handle._status = None
    handle.state = HandleState.CALLING
    try:
        handle._status = api.ntdll.NtCreateFile(
            ctypes.byref(handle._out),
            profile.access,
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
    except BaseException:  # classified even with a status: an OPEN one is then closed
        handle.classify(profile.disposition)
        raise
    return handle.classify(profile.disposition)


def dispose_directory(handle: Handle, disposal: Disposal) -> Disposed:
    """The classic delete disposition of a directory this program created (``H_DIR``,
    ``CREATED_OWNED``, ``OPEN``), once. NTFS refuses it for a directory that is not
    empty (``STATUS_DIRECTORY_NOT_EMPTY``): nothing in it is ever deleted."""
    _require(
        handle.role == "H_DIR"
        and handle.outcome is OpenOutcome.CREATED_OWNED
        and handle.state is HandleState.OPEN,
        f"dispose_directory of {handle.role} {handle.outcome} {handle.state}",
    )
    _require(not (disposal.posix_spent or disposal.classic_spent), "a disposition was spent")
    api = winprims._prims()
    info = api.DispositionInfo(True)
    call, args = winprims._set_disposition(handle, info, winprims._FILE_DISPOSITION_INFORMATION)
    disposal.classic = call
    disposal.classic_spent = True  # right before the call
    winprims._invoke(call, api.ntdll.NtSetInformationFile, *args)
    return disposal.result


class NtOps:
    """The real calls: ``create_with``, the R3B-3 primitives, ``aclproof`` for security."""

    def new(self, path: Path, role: str) -> Handle:
        return Handle.new(path, role)

    def create_with(
        self, handle: Handle, parent: Handle, name: str, descriptor: bytes
    ) -> OpenOutcome:
        return create_with(handle, parent, name, descriptor)

    def open_error(self, handle: Handle) -> OSError:
        return winprims.open_error(handle)

    def query(self, handle: Handle) -> HandleInfo:
        return winfileops.query(handle)

    def read_descriptor(self, handle: Handle) -> bytes:
        return aclproof.read_descriptor(handle)

    def effective_access(self, descriptor: bytes, sid: Sid, what: str) -> int:
        return aclproof.effective_access(descriptor, sid, what=what)

    def lookup_entry(self, handle: Handle, directory: Handle, name: str) -> Lookup:
        return winprims.lookup_entry(handle, directory, name)

    def dispose(self, handle: Handle, disposal: Disposal) -> Disposed:
        if handle.role == "H_DIR":
            return dispose_directory(handle, disposal)
        return winprims.dispose(handle, disposal)

    def close(self, handle: Handle) -> None:
        handle.close()


# --- the checks -------------------------------------------------------------------------


def prove_parent(
    ops: Ops, parent: Handle, expected: Identity
) -> tuple[Refusal | None, OSError | None]:
    """Whether the held ``parent`` is the directory ``expected``, from the handle only."""
    try:
        info = ops.query(parent)
    except OSError as exc:
        detail = f"{parent.path}: {winfileops.describe_error(exc)}"
        return CreateRefusal(CreateReason.PARENT_UNREADABLE, detail), exc
    where = str(parent.path)
    if info.reparse_point:
        detail = f"{where}: reparse tag {info.reparse_tag:#x}, attributes {info.attributes:#x}"
        return CreateRefusal(CreateReason.PARENT_REPARSE, detail), None
    if not info.directory:
        return CreateRefusal(CreateReason.PARENT_NOT_DIRECTORY, where), None
    if info.delete_pending:
        return CreateRefusal(CreateReason.PARENT_DELETE_PENDING, where), None
    if info.volume_serial != expected.volume_serial:
        detail = f"{where}: volume {info.volume_serial:#x}, expected {expected.volume_serial:#x}"
        return CreateRefusal(CreateReason.PARENT_VOLUME, detail), None
    if info.file_id != expected.file_id:
        detail = f"{where}: file id {info.file_id:#x}, expected {expected.file_id:#x}"
        return CreateRefusal(CreateReason.PARENT_IDENTITY, detail), None
    return None, None


def _given(derived: Derived) -> AclRefusal | None:
    """The descriptor to create with is what ``derive`` makes: self-relative, a protected
    DACL of exactly ``derived.aces``, no owner, group or SACL, and ``supported``."""
    try:
        given = Descriptor.parse(derived.descriptor)
    except MalformedDescriptor as exc:
        return AclRefusal(AclReason.MALFORMED, f"the descriptor to create with: {exc}")
    if (
        given.owner is not None
        or given.group is not None
        or given.sacl is not None
        or given.control != _GIVEN_CONTROL
        or given.dacl != derived.aces
    ):
        detail = (
            f"the descriptor to create with is not the derived one (control {given.control:#x})"
        )
        return AclRefusal(AclReason.MISMATCH, detail)
    return aclproof.supported(dataclasses.replace(given, owner=derived.owner), "the new object")


def _read(ops: Ops, handle: Handle, what: str) -> tuple[Descriptor | AclRefusal, OSError | None]:
    try:
        return Descriptor.parse(ops.read_descriptor(handle)), None
    except MalformedDescriptor as exc:
        return AclRefusal(AclReason.MALFORMED, f"{what}: {exc}"), None
    except OSError as exc:
        return AclRefusal(AclReason.UNREADABLE, f"{what}: {winfileops.describe_error(exc)}"), exc


def _wider(
    ops: Ops, target: Descriptor, source: Descriptor, sids: tuple[Sid, ...], rights: int,
    what: tuple[str, str],
) -> tuple[tuple[Sid, int], ...]:  # fmt: skip
    """``aclproof.wider_principals`` through ``ops``: raises ``OSError`` if any one answer
    is not confirmed (``UnverifiedAccess`` among them), never takes it as zero."""
    wider = []
    for sid in sids:
        theirs = ops.effective_access(target.raw, sid, what[0])
        ours = ops.effective_access(source.raw, sid, what[1])
        extra = theirs & ~ours & rights
        if extra:
            wider.append((sid, extra))
    return tuple(wider)


def _not_wider(
    ops: Ops, target: Descriptor, source: Descriptor, creator: Sid, rights: int,
    what: tuple[str, str], reason: AclReason | CreateReason,
) -> tuple[Refusal | None, OSError | None]:  # fmt: skip
    sids = aclproof.principals(target, source, creator=creator)
    try:
        wider = _wider(ops, target, source, sids, rights, what)
    except OSError as exc:
        return AclRefusal(AclReason.UNVERIFIED, winfileops.describe_error(exc)), exc
    if not wider:
        return None, None
    detail = f"{what[0]}: " + "; ".join(f"{sid} gets {extra:#x} more" for sid, extra in wider)
    if isinstance(reason, AclReason):
        return AclRefusal(reason, detail), None
    return CreateRefusal(reason, detail), None


def category_not_wider(
    ops: Ops, directory: Handle, source_dir: Descriptor, creator: Sid
) -> tuple[Refusal | None, OSError | None]:
    """The category directory, read through ``directory``, gives no principal more
    directory rights than the source directory (``aclproof.judge``, by handle)."""
    target, error = _read(ops, directory, "the target")
    if isinstance(target, AclRefusal):
        return target, error
    for descriptor, what in ((source_dir, "the source directory"), (target, "the target")):
        refusal = aclproof.supported(descriptor, what)
        if refusal is not None:
            return refusal, None
    return _not_wider(
        ops, target, source_dir, creator, aclproof.DIRECTORY_RIGHTS,
        ("the target", "the source directory"), AclReason.DIRECTORY_WIDER,
    )  # fmt: skip


def verify(
    ops: Ops, handle: Handle, derived: Derived, original: Descriptor, creator: Sid, rights: int
) -> tuple[Refusal | None, OSError | None]:
    """The object of ``handle`` has exactly the security it was created with and gives no
    principal any of ``rights`` that ``original`` (the source file or the source
    directory) does not give it; also after a rename. Read through the handle (it needs
    ``READ_CONTROL``); a descriptor that cannot be read is a refusal, never a pass."""
    actual, error = _as_created(ops, handle, derived)
    if not isinstance(actual, Descriptor):
        return actual, error
    return _not_wider_than(ops, actual, original, creator, rights)


def _as_created(
    ops: Ops, handle: Handle, derived: Derived
) -> tuple[Descriptor | Refusal, OSError | None]:
    """The descriptor of ``handle``'s object, if it is exactly the one created with."""
    actual, error = _read(ops, handle, "the new object")
    if isinstance(actual, AclRefusal):
        return actual, error
    refusal = aclproof.check_created(actual, derived) or aclproof.supported(
        actual, "the new object"
    )
    return (actual, None) if refusal is None else (refusal, None)


def _not_wider_than(
    ops: Ops, actual: Descriptor, original: Descriptor, creator: Sid, rights: int
) -> tuple[Refusal | None, OSError | None]:
    return _not_wider(
        ops, actual, original, creator, rights, ("the new object", "the source"), CreateReason.WIDER
    )


def _object_problem(info: HandleInfo, parent: HandleInfo, *, directory: bool) -> str | None:
    if info.reparse_point:
        return f"reparse tag {info.reparse_tag:#x}, attributes {info.attributes:#x}"
    if info.directory is not directory:
        return "a directory" if info.directory else "not a directory"
    if info.delete_pending:
        return "delete pending"
    if info.volume_serial != parent.volume_serial:
        return f"volume {info.volume_serial:#x}, the parent's {parent.volume_serial:#x}"
    if info.file_id == parent.file_id:
        return "the parent itself"
    if not directory and (info.links != 1 or info.size != 0):
        return f"{info.links} links, {info.size} bytes"
    return None


# --- creation ---------------------------------------------------------------------------


def create_category(
    ops: Ops, parent: Handle, expected: Identity, name: str, source_dir: Descriptor, creator: Sid
) -> Creation:
    """Create the category directory ``name`` in the held ``parent`` (proven to be
    ``expected``) with the protected DACL of ``source_dir`` (``derive(directory=True)``)."""
    return _create(
        ops, parent, expected, name, "H_DIR",
        lambda: aclproof.derive(source_dir, creator, directory=True), source_dir, creator,
        aclproof.DIRECTORY_RIGHTS, None,
    )  # fmt: skip


def create_temp(
    ops: Ops,
    directory: Handle,
    expected: Identity,
    name: str,
    file_sd: Descriptor,
    source_dir: Descriptor,
    creator: Sid,
) -> Creation:
    """Create the temporary file ``name`` in the held category ``directory`` (proven to be
    ``expected``, ``READ_CONTROL`` on it) with the source's effective DACL, protected; only
    if the category gives no principal more than the source directory."""
    return _create(
        ops, directory, expected, name, "H_TMP", lambda: aclproof.derive(file_sd, creator),
        file_sd, creator, aclproof.FILE_ALL_ACCESS,
        lambda: category_not_wider(ops, directory, source_dir, creator),
    )  # fmt: skip


def _create(
    ops: Ops,
    parent: Handle,
    expected: Identity,
    name: str,
    role: str,
    derive: Callable[[], Derived | AclRefusal],
    original: Descriptor,
    creator: Sid,
    rights: int,
    check: Callable[[], tuple[Refusal | None, OSError | None]] | None,
) -> Creation:
    creation = Creation(Created.REFUSED, role=role, name=name)
    handle: Handle | None = None
    try:
        derived = derive()
        if isinstance(derived, AclRefusal):
            creation.refusal = derived
            return creation
        creation.derived = derived
        refusal: Refusal | None = _given(derived)
        error: OSError | None = None
        if refusal is None:
            refusal, error = prove_parent(ops, parent, expected)
        if refusal is None:
            creation.parent = expected
            if check is not None:
                refusal, error = check()
        if refusal is not None:
            creation.refusal, creation.error = refusal, error
            return creation
        handle = ops.new(parent.path / name, role)
        creation.stage = Stage.CREATE
        creation.state = Created.UNKNOWN  # from the call on, until it is classified
        ops.create_with(handle, parent, name, derived.descriptor)
        _classified(creation, handle)
        _require(
            handle.state in (HandleState.OPEN, HandleState.NOT_OPENED, HandleState.UNKNOWN),
            f"create_with left a {handle.state} handle",
        )
        if handle.state is HandleState.UNKNOWN:  # never closed, nothing removed
            detail = f"{handle.path}: {handle.outcome}"
            creation.refusal = CreateRefusal(CreateReason.OUTCOME_UNKNOWN, detail)
            creation.error = handle.close_error
        elif handle.state is not HandleState.OPEN:
            _not_created(ops, handle, parent, name, creation)
        elif not creation.owned:  # still UNKNOWN: it is only closed
            unreliable = " (unreliable)" if handle.unreliable else ""
            detail = f"{handle.path}: {handle.outcome}{unreliable}"
            creation.refusal = CreateRefusal(CreateReason.NOT_PROVEN_OWN, detail)
            creation.stage = Stage.RELEASE
            creation.release_errors += _close(ops, handle)
        else:  # UNCHECKED until every check passed
            refusal, error = _check_new(
                ops, handle, parent, role, derived, original, creator, rights, creation
            )
            if refusal is None:
                creation.state, creation.handle = Created.CREATED, handle
            else:
                creation.refusal, creation.error = refusal, error
                _remove(ops, handle, parent, name, creation)
    except BaseException as exc:
        _escaped(ops, creation, handle, exc)
        raise
    creation.handle_state = None if handle is None else handle.state
    return creation


def _classified(creation: Creation, handle: Handle) -> None:
    """What the create call left, from its handle alone (idempotent): ``REFUSED`` only if
    nothing was opened and the call was never made or the name exists, ``UNCHECKED``
    only for an open ``CREATED_OWNED`` handle, ``UNKNOWN`` for anything else."""
    creation.outcome, creation.status = handle.outcome, handle.status
    creation.owned = (
        handle.state is HandleState.OPEN
        and handle.outcome is OpenOutcome.CREATED_OWNED
        and not handle.unreliable
    )
    if handle.state is HandleState.NEW or (
        handle.state is HandleState.NOT_OPENED
        and handle.outcome in (OpenOutcome.NOT_CALLED, OpenOutcome.NAME_TAKEN)
    ):
        creation.state = Created.REFUSED
    elif creation.owned:
        creation.state = Created.UNCHECKED
    else:  # CALLING, UNKNOWN, another error status, or opened and not proven created
        creation.state = Created.UNKNOWN


def _escaped(ops: Ops, creation: Creation, handle: Handle | None, exc: BaseException) -> None:
    """``exc`` leaves ``_create`` in ``creation.stage``; the caller re-raises it.

    ``creation.state`` stays what the steps established (re-classified in the create
    step): nothing is created again, looked up or removed. A handle still ``OPEN`` is
    closed once (a ``CREATED`` one too: nobody receives it, so it becomes ``UNCHECKED``);
    any other, ``UNKNOWN`` and ``CALLING`` among them, is not touched. A failure of that
    close goes to ``release_errors``, ``release_after`` decides what is raised, and the
    ``Creation`` is attached to ``exc`` and to whatever is raised in its place."""
    creation.raised = exc
    if creation.stage is Stage.CREATE and handle is not None:
        _classified(creation, handle)
    if creation.state is Created.CREATED:
        creation.state, creation.handle = Created.UNCHECKED, None
    if creation.refusal is None:
        where = creation.name if handle is None else handle.path
        detail = f"{where}: {creation.stage}: {winfileops.describe_error(exc)}"
        creation.refusal = CreateRefusal(CreateReason.EXCEPTION, detail)
    instead: BaseException | None = None
    try:
        if handle is not None and handle.state is HandleState.OPEN:
            release_after(exc, lambda: _close_kept(ops, handle, creation))
    except BaseException as other:
        instead = other
        raise
    finally:
        creation.handle_state = None if handle is None else handle.state
        attach_creation(exc, creation)
        if instead is not None:
            attach_creation(instead, creation)


def _close_kept(ops: Ops, handle: Handle, creation: Creation) -> None:
    try:
        ops.close(handle)
    except BaseException as exc:
        creation.release_errors += (exc,)
        raise


def _not_created(ops: Ops, handle: Handle, parent: Handle, name: str, creation: Creation) -> None:
    """No handle: ``NAME_TAKEN`` (or no call) opened nothing; any other error status
    counts as not created only if the name is then proven absent (``UNKNOWN`` until)."""
    creation.stage = Stage.NOT_CREATED
    if creation.state is Created.REFUSED:
        if handle.outcome is OpenOutcome.NAME_TAKEN:
            detail = f"{handle.path}: the name exists, nothing was opened"
            creation.refusal = CreateRefusal(CreateReason.NAME_TAKEN, detail)
        else:
            detail = f"{handle.path}: {handle.outcome}"
            creation.refusal = CreateRefusal(CreateReason.CREATE_FAILED, detail)
        creation.error = ops.open_error(handle) if handle.status is not None else None
        return
    detail = f"{handle.path}: {handle.outcome}, not looked up"
    creation.refusal = CreateRefusal(CreateReason.OUTCOME_UNKNOWN, detail)
    creation.error = ops.open_error(handle) if handle.status is not None else None
    lookup, releases = _lookup(ops, parent, name)
    creation.lookup = lookup
    creation.release_errors += releases
    if lookup.kind is LookupKind.ABSENT:
        state, reason = Created.REFUSED, CreateReason.CREATE_FAILED
    else:
        state, reason = Created.UNKNOWN, CreateReason.OUTCOME_UNKNOWN
    detail = f"{handle.path}: {handle.outcome}, lookup {lookup.kind} {lookup.reason}"
    creation.state, creation.refusal = state, CreateRefusal(reason, detail)


def _check_new(
    ops: Ops,
    handle: Handle,
    parent: Handle,
    role: str,
    derived: Derived,
    original: Descriptor,
    creator: Sid,
    rights: int,
    creation: Creation,
) -> tuple[Refusal | None, OSError | None]:
    """The checks of the new object; ``creation.identity`` as soon as it is queried."""
    creation.stage = Stage.QUERY
    try:
        info = ops.query(handle)
        creation.identity = Identity.of(info)
        parent_info = ops.query(parent)
    except OSError as exc:
        detail = f"{handle.path}: {winfileops.describe_error(exc)}"
        return CreateRefusal(CreateReason.OBJECT_PROBLEM, detail), exc
    problem = _object_problem(info, parent_info, directory=role == "H_DIR")
    if problem is not None:
        return CreateRefusal(CreateReason.OBJECT_PROBLEM, f"{handle.path}: {problem}"), None
    creation.stage = Stage.DESCRIPTOR
    actual, error = _as_created(ops, handle, derived)
    if not isinstance(actual, Descriptor):
        return actual, error
    creation.stage = Stage.AUTHZ
    return _not_wider_than(ops, actual, original, creator, rights)


def _close(ops: Ops, handle: Handle) -> tuple[BaseException, ...]:
    """Close once; an ``OSError`` of the close is returned, anything else raised."""
    try:
        ops.close(handle)
    except OSError as exc:
        return (exc,)
    return ()


def _lookup(ops: Ops, parent: Handle, name: str) -> tuple[Lookup, tuple[BaseException, ...]]:
    try:
        return ops.lookup_entry(ops.new(parent.path, "H_LOOKUP"), parent, name), ()
    except OSError as exc:  # the close of the lookup handle failed
        return Lookup(LookupKind.UNKNOWN, reason=winfileops.describe_error(exc)), (exc,)


def _remove(ops: Ops, handle: Handle, parent: Handle, name: str, creation: Creation) -> None:
    """Remove what this call created (``CREATED_OWNED``, still open): the delete
    disposition once, close once, then the name looked up in ``parent``. ``REMOVED`` only
    for a confirmed disposition, a confirmed close and the name absent; ``LEFT`` while no
    disposition is spent, if it was refused or the lookup finds the same object;
    ``REMOVAL_UNKNOWN`` otherwise."""
    creation.stage = Stage.REMOVE
    creation.state = Created.LEFT  # nothing spent yet: it is there, held
    disposal = Disposal()
    creation.disposal = disposal
    try:
        disposed = ops.dispose(handle, disposal)
    except BaseException:  # the spent flags are set right before each call
        if disposal.result not in (Disposed.NONE, Disposed.REFUSED):
            creation.state = Created.REMOVAL_UNKNOWN
        raise
    if disposed is not Disposed.REFUSED:  # REFUSED: an error status, the object is there
        creation.state = Created.REMOVAL_UNKNOWN  # until the lookup proves more
    releases = _close(ops, handle)
    creation.release_errors += releases
    if disposed is not Disposed.OK or releases:
        return  # not established, or the handle may still be open: no lookup
    lookup, failures = _lookup(ops, parent, name)
    creation.lookup = lookup
    creation.release_errors += failures
    identity = creation.identity
    if lookup.kind is LookupKind.ABSENT:
        creation.state = Created.REMOVED
    elif identity is not None and lookup.file_id == identity.file_id:
        creation.state = Created.LEFT


# --- the temporary file in the JSONL v2 journal (R3B-7B1: nothing calls this yet) -------


@dataclass(frozen=True, slots=True)
class TempRecord:
    """The record that follows the copy intent after ``create_temp``: ``staged`` for a
    ``CREATED`` temporary file, otherwise its terminal ``failed`` or ``skipped``."""

    status: StatusV2
    reason: str | None
    temp_id: int | None
    temp_state: TempState
    diagnostics: Diagnostics | None
    error_code: int | None
    error_message: str | None


_TEMP_STATES = {
    Created.CREATED: TempState.PRESENT,
    Created.REFUSED: TempState.NONE,
    Created.REMOVED: TempState.REMOVED,
    Created.UNCHECKED: TempState.LEFT,
    Created.LEFT: TempState.LEFT,
    Created.UNKNOWN: TempState.UNKNOWN,
    Created.REMOVAL_UNKNOWN: TempState.UNKNOWN,
}


def _error_code(error: BaseException | None) -> int | None:
    for name in ("winerror", "errno"):
        value = getattr(error, name, None)
        if type(value) is int and value:
            return value
    return None


def _lookup_text(found: Lookup | None) -> str | None:
    if found is None:
        return None
    text = str(found.kind)
    if found.file_id is not None:
        text += f" {found.file_id:032x}"
    return f"{text} ({found.reason})" if found.reason else text


def temp_record(creation: Creation) -> TempRecord:
    """The v2 record for ``creation`` (of ``create_temp``) right after its copy intent.

    ``temp_id`` only in ``staged`` (the grammar: null until staged): the identity read
    through the creating handle goes to ``diagnostics.temp_id_observed``, an unknown one
    stays null. ``removed`` only for ``REMOVED``; ``UNKNOWN`` is ``temp_create_uncertain``
    (maybe there, never owned). Never ``done``, never committed."""
    _require(creation.role == "H_TMP", f"temp_record of a {creation.role} creation")
    state = creation.state
    temp_state = _TEMP_STATES[state]
    if state is Created.CREATED:
        identity = creation.identity
        _require(identity is not None, "CREATED without an identity")
        assert identity is not None
        return TempRecord(StatusV2.STAGED, None, identity.file_id, temp_state, None, None, None)
    error = creation.raised if creation.raised is not None else creation.error
    refusal = creation.refusal
    detail = None if refusal is None else refusal.detail
    message = detail
    if error is not None:
        described = winfileops.describe_error(error)
        message = described if detail is None else f"{detail}: {described}"
    if state is Created.REFUSED and creation.outcome is OpenOutcome.NAME_TAKEN:
        status, reason = StatusV2.SKIPPED, TEMP_NAME_TAKEN
    elif state is Created.UNKNOWN:
        status, reason = StatusV2.FAILED, TEMP_CREATE_UNCERTAIN
    else:
        status = StatusV2.FAILED
        reason = str(CreateReason.EXCEPTION if refusal is None else refusal.reason)
    diagnostics = None
    if temp_state in (TempState.LEFT, TempState.UNKNOWN):
        identity = creation.identity
        disposal = "not attempted" if creation.disposal is None else creation.disposal.result
        diagnostics = Diagnostics(
            ntstatus=None if creation.status is None else f"{creation.status:#010x}",
            temp_lookup=_lookup_text(creation.lookup),
            temp_handle=f"{creation.outcome}, handle {creation.handle_state}, owned"
            f" {creation.owned}, stage {creation.stage}",
            cleanup=f"disposition {disposal}, release errors {len(creation.release_errors)}",
            temp_id_observed=None if identity is None else f"{identity.file_id:032x}",
            metadata_violation=(
                detail
                if refusal is not None and refusal.reason is CreateReason.OBJECT_PROBLEM
                else None
            ),
        )
    return TempRecord(status, reason, None, temp_state, diagnostics, _error_code(error), message)


@dataclass(frozen=True, slots=True, eq=False)
class TempJournaled:
    record: TempRecord
    sequence: int | None  # None: the journal did not confirm it
    journal_error: JournalError | None

    @property
    def may_continue(self) -> bool:
        """Only a ``staged`` record the journal confirmed lets the copy go on."""
        return self.sequence is not None and self.record.status is StatusV2.STAGED


def journal_temp(
    journal: JournalV2,
    creation: Creation,
    *,
    intent: int,
    source: Path,
    target: Path,
    source_volume: VolumeRef,
    target_volume: VolumeRef,
    temp: str,
    size: int,
    source_id: int,
) -> TempJournaled:
    """Write ``temp_record(creation)`` after the copy intent ``intent`` (its fields
    repeated). A ``JournalError`` of the write is returned (``may_continue`` False; the
    journal takes nothing more); anything else, the preparation of the record included,
    propagates as itself with ``creation`` attached, and then nothing was written.
    Nothing here closes, removes or recovers anything."""
    record: TempRecord | None = None
    try:
        _require(creation.name == temp, f"the temporary file {creation.name!r} is not {temp!r}")
        record = temp_record(creation)
        sequence = journal.write(
            OperationV2.COPY, record.status, intent=intent, source=source, target=target,
            reason=record.reason, error_code=record.error_code,
            error_message=record.error_message, source_volume=source_volume,
            target_volume=target_volume, temp=temp, size=size, source_id=source_id,
            temp_id=record.temp_id, commit=Commit.NOT_ATTEMPTED, temp_state=record.temp_state,
            diagnostics=record.diagnostics,
        )  # fmt: skip
    except JournalError as exc:
        if record is not None:  # the write itself failed
            return TempJournaled(record, None, exc)
        attach_creation(exc, creation)
        raise
    except BaseException as exc:
        attach_creation(exc, creation)
        raise
    return TempJournaled(record, sequence, None)
