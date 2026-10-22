"""R3B-7A on the NTFS of ``tmp_path`` (C:): ``aclcreate`` with the real calls.

Only directories and files this test creates under ``tmp_path`` are created, changed
or removed; a junction points at most at the ``sentinel`` directory, which must stay
as it was (the fixture checks it). The descriptor given to ``NtCreateFile`` is taken
from the call itself (a spy around the real ``ntdll.NtCreateFile``); judged are the
binary descriptor and ``AuthzAccessCheck``, SDDL is printed as ``OBSERVED`` only.
The pure checks with a fake ``Ops`` are in ``test_aclcreate_fake``.
"""

from __future__ import annotations

import _winapi
import ctypes
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_aclproof import (
    DIR_ACCESS,
    DIR_SHARE,
    EVERYONE,
    PRIVATE,
    WIDE,
    descriptor,
    observe,
    sddl,
    sentinel,  # noqa: F401  (a fixture: what stays as it is outside tmp_path)
    set_sddl,
    tree,
    unlocked,  # noqa: F401  (autouse: tmp_path stays removable)
)
from test_winfileops import walk

from downloads_organizer import aclcreate as c
from downloads_organizer import aclproof as a
from downloads_organizer import winfileops as w
from downloads_organizer import winprims as p
from downloads_organizer.aclcreate import Created, CreateReason
from downloads_organizer.aclproof import AclReason, Descriptor, Sid
from downloads_organizer.winfileops import HandleState, OpenOutcome
from downloads_organizer.winprims import Disposal, Disposed, LookupKind, Renamed

pytestmark = [pytest.mark.windows_only, pytest.mark.real_ntfs]

CATEGORY, TEMP = "Documents", "a.partial"


@dataclass(eq=False)
class Call:
    """One ``NtCreateFile``: what it was given, its status, the descriptor right after."""

    root: int | None
    name: str
    given: bytes | None  # the descriptor in OBJECT_ATTRIBUTES, as passed
    status: int | None = None
    out: int = 0
    first: bytes | None = None  # read by path right after a successful call
    first_error: OSError | None = None


class Creates:
    """A spy around the real ``ntdll.NtCreateFile`` of ``winprims._prims()``.

    ``parents`` maps a raw parent handle to its path, so that the object's descriptor
    is read right after the call; ``interrupt`` is raised once after a call with a
    descriptor (its status is then lost to ``create_with``)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ntdll = p._prims().ntdll
        self.real = self.ntdll.NtCreateFile
        self.calls: list[Call] = []
        self.parents: dict[int, Path] = {}
        self.interrupt: BaseException | None = None
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.length = advapi.GetSecurityDescriptorLength
        self.length.argtypes = [ctypes.c_void_p]
        self.length.restype = ctypes.c_ulong
        monkeypatch.setattr(self.ntdll, "NtCreateFile", self.spy)

    def spy(self, *args: object) -> object:
        attributes = args[2]._obj  # type: ignore[attr-defined]
        text = attributes.ObjectName.contents
        name = ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""
        given = None
        if attributes.SecurityDescriptor:
            address = attributes.SecurityDescriptor
            given = ctypes.string_at(address, self.length(address))
        call = Call(attributes.RootDirectory, name, given)
        self.calls.append(call)
        status = self.real(*args)
        call.status = status & 0xFFFF_FFFF  # type: ignore[operator]
        call.out = args[0]._obj.value or 0  # type: ignore[attr-defined]
        if given is not None and call.status == 0 and call.root in self.parents:
            try:  # READ_CONTROL only: no share check against the creating handle
                call.first = a.read_descriptor_at(self.parents[call.root] / name)
            except OSError as exc:
                call.first_error = exc
        if given is not None and self.interrupt is not None:
            interrupt, self.interrupt = self.interrupt, None
            raise interrupt
        return status

    @property
    def given(self) -> list[Call]:
        return [call for call in self.calls if call.given is not None]


@pytest.fixture
def me() -> Sid:
    return a.creator_sid()


@pytest.fixture
def outside(sentinel: tuple[Path, object]) -> Path:  # noqa: F811
    """The sentinel directory; the fixture compares what is there on teardown."""
    return sentinel[0]


@pytest.fixture
def creates(monkeypatch: pytest.MonkeyPatch) -> Creates:
    return Creates(monkeypatch)


class Ops(c.NtOps):
    """The real calls; ``fail_read``: reading the descriptor of that path fails (after
    ``race``, if given); every ``Handle.new`` is kept in ``handles``."""

    def __init__(self, fail_read: Path | None = None, race=None) -> None:
        self.fail_read, self.race = fail_read, race
        self.handles: list[w.Handle] = []

    def new(self, path: Path, role: str) -> w.Handle:
        handle = super().new(path, role)
        self.handles.append(handle)
        return handle

    def read_descriptor(self, handle: w.Handle) -> bytes:
        if handle.path == self.fail_read:
            if self.race is not None:
                self.race()
            raise OSError(5, "injected: the new object's descriptor is not read")
        return super().read_descriptor(handle)


def held(path: Path, *, share: int = DIR_SHARE, access: int = DIR_ACCESS) -> w.Handle:
    return walk(path, access=access, share=share)


def identity(handle: w.Handle) -> c.Identity:
    return c.Identity.of(w.query(handle))


def inherited(d: Descriptor) -> bool:
    return any(ace.flags & a.INHERITED_ACE for ace in d.dacl or ())


def not_wider(created: Descriptor, original: Descriptor, me, rights: int) -> None:
    """Authz, principal by principal, both ways: the same access as the original."""
    sids = a.principals(created, original, creator=me)
    assert a.wider_principals(created, original, sids, rights) == ()
    assert a.wider_principals(original, created, sids, rights) == ()


def category(ops, parent, source_dir, me, name=CATEGORY) -> c.Creation:
    return c.create_category(ops, parent, identity(parent), name, source_dir, me)


def temp(ops, cat: c.Creation, source, me, name=TEMP) -> c.Creation:
    assert cat.handle is not None and cat.identity is not None
    return c.create_temp(
        ops, cat.handle, cat.identity, name, descriptor(source), descriptor(source.parent), me
    )


def close(*creations: c.Creation | w.Handle | None) -> None:
    for item in creations:
        handle = item.handle if isinstance(item, c.Creation) else item
        if handle is not None:
            handle.close()


def state_of(source: Path) -> tuple[bytes, bytes, bytes]:
    return a.read_descriptor_at(source), a.read_descriptor_at(source.parent), source.read_bytes()


# --- creation with the descriptor -------------------------------------------------------


def test_category_and_temp_get_the_derived_descriptor_in_the_create_call(
    tmp_path, me, record_property, acquisitions, creates, outside
):
    """In a target directory that lets Everyone read: the descriptor in the very
    ``NtCreateFile`` is the derived one, relative to the held parent handle; read right
    after the call, through the handle and by path it is the same, protected, nothing
    inherited, and by Authz no principal gets more (or less) than at the source."""
    source, dst = tree(tmp_path, PRIVATE, WIDE)
    before = state_of(source)
    ops = c.NtOps()
    parent = held(dst)
    cat = tmp = None
    try:
        creates.parents[parent.raw] = dst
        cat = category(ops, parent, descriptor(source.parent), me)
        assert cat.state is Created.CREATED and cat.outcome is OpenOutcome.CREATED_OWNED
        assert cat.handle is not None
        creates.parents[cat.handle.raw] = dst / CATEGORY
        tmp = temp(ops, cat, source, me)
        assert tmp.state is Created.CREATED and tmp.handle is not None
        made = creates.given
        assert [(m.name, m.root, m.status) for m in made] == [
            (CATEGORY, parent.raw, 0),
            (TEMP, cat.handle.raw, 0),
        ]
        for call, creation, original, rights in (
            (made[0], cat, descriptor(source.parent), a.DIRECTORY_RIGHTS),
            (made[1], tmp, descriptor(source), a.FILE_ALL_ACCESS),
        ):
            assert creation.derived is not None and creation.handle is not None
            assert call.given == creation.derived.descriptor
            given = Descriptor.parse(call.given)
            assert given.protected and given.owner is None and not inherited(given)
            assert call.first_error is None and call.first is not None
            first = Descriptor.parse(call.first)  # no window with another descriptor
            through = Descriptor.parse(a.read_descriptor(creation.handle))
            assert first.raw == through.raw
            assert a.check_created(through, creation.derived) is None
            assert through.protected and not inherited(through) and through.owner == me
            assert a.effective_access(through.raw, EVERYONE) == 0
            not_wider(through, original, me, rights)
            observe(record_property, f"created {call.name}", sddl(through.raw))
        (dst / "Plain").mkdir()  # the counterexample: created the ordinary way
        plain = descriptor(dst / "Plain")
        observe(record_property, "plain mkdir", sddl(plain.raw))
        assert inherited(plain) and a.effective_access(plain.raw, EVERYONE) != 0
    finally:
        close(tmp, cat, parent)
    assert state_of(source) == before
    assert sorted(x.name for x in dst.iterdir()) == [CATEGORY, "Plain"]
    assert [x.name for x in (dst / CATEGORY).iterdir()] == [TEMP]


def test_the_descriptor_stays_after_renames_in_the_category_and_into_the_wide_target(
    tmp_path, me, record_property, acquisitions
):
    """The temporary file renamed (``rename_once``) to its final name in the category,
    then into the target that lets Everyone read: the same binary descriptor, ``verify``
    passes through the handle, read by the new path no principal gets more."""
    source, dst = tree(tmp_path, PRIVATE, WIDE)
    before = state_of(source)
    ops = c.NtOps()
    parent = held(dst)
    cat = tmp = None
    try:
        cat = category(ops, parent, descriptor(source.parent), me)
        tmp = temp(ops, cat, source, me)
        assert tmp.state is Created.CREATED and tmp.handle and tmp.derived and cat.handle
        created = a.read_descriptor(tmp.handle)
        for directory, where in ((cat.handle, dst / CATEGORY), (parent, dst)):
            assert p.rename_once(tmp.handle, directory, "a.txt", p.RenameAttempt()) is (Renamed.OK)
            assert a.read_descriptor(tmp.handle) == created
            assert c.verify(
                ops, tmp.handle, tmp.derived, descriptor(source), me, a.FILE_ALL_ACCESS
            ) == (None, None)
            fresh = descriptor(where / "a.txt")
            observe(record_property, f"after rename into {where.name}", sddl(fresh.raw))
            assert fresh.raw == created and not inherited(fresh)
            assert a.effective_access(fresh.raw, EVERYONE) == 0
            not_wider(fresh, descriptor(source), me, a.FILE_ALL_ACCESS)
    finally:
        close(tmp, cat, parent)
    assert state_of(source) == before
    assert not (dst / CATEGORY / "a.txt").exists() and (dst / "a.txt").is_file()


def test_the_creating_handle_is_not_shared(tmp_path, me, acquisitions):
    """Nobody else gets the creating handle's access: the category is not renamed or removed
    while it is held, the file is not opened again for data. Both are sharing violations
    (32): an access denial or an open file inside the category would also refuse them."""
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    ops = c.NtOps()
    parent = held(dst)
    cat = tmp = None
    try:
        cat = category(ops, parent, descriptor(source.parent), me)
        with pytest.raises(PermissionError) as renamed:  # no FILE_SHARE_DELETE, no temp yet
            (dst / CATEGORY).rename(dst / "Renamed")
        assert renamed.value.winerror == 32
        tmp = temp(ops, cat, source, me)
        assert tmp.state is Created.CREATED
        with pytest.raises(PermissionError) as opened:  # share 0, all shares asked for
            _winapi.CloseHandle(
                _winapi.CreateFile(
                    str(dst / CATEGORY / TEMP),
                    _winapi.GENERIC_READ,
                    w.SHARE_ALL,
                    0,
                    _winapi.OPEN_EXISTING,
                    0,
                    0,
                )
            )
        assert opened.value.winerror == 32
    finally:
        close(tmp, cat, parent)
    assert (dst / CATEGORY / TEMP).is_file()


# --- collisions and replaced parents ----------------------------------------------------


def _make(kind: str, link: Path, outside: Path) -> None:
    if kind == "directory":
        link.mkdir()
        set_sddl(link, WIDE)
        (link / "theirs.txt").write_bytes(b"theirs")
    elif kind == "file":
        link.write_bytes(b"theirs")
    elif kind == "junction":
        _winapi.CreateJunction(str(outside), str(link))
    else:
        try:
            directory = kind == "dir-symlink"
            link.symlink_to(outside if directory else outside / "keep.txt", directory)
        except OSError as exc:  # no SeCreateSymbolicLinkPrivilege, no developer mode
            pytest.skip(f"NOT_TESTED: {kind} cannot be created here ({exc})")


def _unmake(kind: str, link: Path) -> None:
    if not (link.is_junction() or link.is_symlink()):
        return  # not made (skipped), or nothing to unmake
    if kind in ("junction", "dir-symlink"):
        link.rmdir()  # the link itself, never its target
    elif kind == "file-symlink":
        link.unlink()


KINDS = ["directory", "file", "junction", "dir-symlink", "file-symlink"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("what", ["category", "temp"])
def test_an_existing_name_is_never_opened(
    tmp_path, me, record_property, acquisitions, creates, outside, kind, what
):
    """``FILE_CREATE``: whatever has the name (a junction or a symlink to the sentinel
    among them) is refused as ``NAME_TAKEN``; nothing of it or behind it changes."""
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    ops = c.NtOps()
    parent = held(dst)
    cat = result = None
    link = dst / CATEGORY if what == "category" else dst / CATEGORY / TEMP
    try:
        if what == "temp":
            cat = category(ops, parent, descriptor(source.parent), me)
            assert cat.state is Created.CREATED
        _make(kind, link, outside)
        seen = (a.read_descriptor_at(link), sorted(x.name for x in link.parent.iterdir()))
        result = (
            category(ops, parent, descriptor(source.parent), me)
            if what == "category"
            else temp(ops, cat, source, me)
        )
        observe(record_property, f"{what} over a {kind}", result.refusal)
        assert result.handle is None and result.refusal is not None
        if what == "temp" and kind in ("directory", "junction", "dir-symlink"):
            # STATUS_FILE_IS_A_DIRECTORY, not a collision: the lookup finds a directory
            # and cannot say whose it is, so not REFUSED either; nothing is closed or removed
            assert result.state is Created.UNKNOWN
            assert result.refusal.reason is CreateReason.OUTCOME_UNKNOWN
            assert result.outcome is OpenOutcome.NOT_CREATED_CLAIMED
            assert creates.given[-1].status == 0xC000_00BA
        else:
            assert result.state is Created.REFUSED
            assert result.refusal.reason is CreateReason.NAME_TAKEN
            assert result.outcome is OpenOutcome.NAME_TAKEN
            assert creates.given[-1].status == 0xC000_0035  # STATUS_OBJECT_NAME_COLLISION
        assert (a.read_descriptor_at(link), sorted(x.name for x in link.parent.iterdir())) == seen
        if kind in ("directory", "file"):
            theirs = link / "theirs.txt" if kind == "directory" else link
            assert theirs.read_bytes() == b"theirs"
    finally:
        close(cat, parent)
        _unmake(kind, link)


def test_a_junction_as_the_parent_is_refused_and_not_followed(
    tmp_path, me, acquisitions, creates, outside
):
    """A handle walked to a junction is the junction itself (never its target): refused
    as a reparse point, even with the junction's own identity expected."""
    source, _dst = tree(tmp_path, PRIVATE, None)
    link = tmp_path / "dst"
    _winapi.CreateJunction(str(outside), str(link))
    parent = None
    try:
        parent = held(link)
        info = w.query(parent)
        assert info.reparse_point
        for expected in (c.Identity.of(info), c.Identity(info.volume_serial, 0)):
            result = c.create_category(
                c.NtOps(), parent, expected, CATEGORY, descriptor(source.parent), me
            )
            assert result.state is Created.REFUSED and result.refusal is not None
            assert result.refusal.reason is CreateReason.PARENT_REPARSE
        assert creates.given == []  # NtCreateFile with a descriptor was never called
    finally:
        close(parent)
        link.rmdir()


def test_a_held_parent_renamed_and_replaced_by_a_junction_keeps_the_creation_inside(
    tmp_path, me, record_property, acquisitions, outside
):
    """The parent is held (sharing delete, so it can be renamed meanwhile), renamed and
    its path replaced by a junction to the sentinel: the category is created relative
    to the held handle, in the renamed directory; behind the junction nothing changes.
    A handle walked to the path now is refused (a reparse point); a new directory at the
    path is not the expected one (``PARENT_IDENTITY``)."""
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    moved = tmp_path / "moved"
    parent = held(dst, share=w.SHARE_ALL)
    expected = identity(parent)
    cat = again = other = None
    try:
        dst.rename(moved)
        _winapi.CreateJunction(str(outside), str(dst))
        cat = c.create_category(
            c.NtOps(), parent, expected, CATEGORY, descriptor(source.parent), me
        )
        assert cat.state is Created.CREATED, cat.refusal
        assert (moved / CATEGORY).is_dir()
        again = held(dst)
        result = c.create_category(
            c.NtOps(), again, expected, "Other", descriptor(source.parent), me
        )
        assert result.refusal is not None
        assert result.refusal.reason is CreateReason.PARENT_REPARSE
        close(again)
        again = None
        dst.rmdir()  # the junction
        dst.mkdir()
        other = held(dst)
        result = c.create_category(
            c.NtOps(), other, expected, "Other", descriptor(source.parent), me
        )
        observe(record_property, "a new directory at the path", result.refusal)
        assert result.refusal is not None
        assert result.refusal.reason is CreateReason.PARENT_IDENTITY
        assert list(dst.iterdir()) == []
    finally:
        close(cat, again, other, parent)
        if dst.is_junction():
            dst.rmdir()
    assert sorted(x.name for x in moved.iterdir()) == [CATEGORY]


# --- refused before anything is created -------------------------------------------------


REFUSED_BEFORE = {
    "deny-source-dir": ("dir", PRIVATE + "(D;CI;0x40;;;WD)", None, AclReason.UNSUPPORTED_DENY),
    "deny-source": ("temp", PRIVATE, "D:P(D;;0x2;;;WD)(A;;FA;;;{me})", AclReason.UNSUPPORTED_DENY),
    "conditional-source": (
        "temp", PRIVATE, "D:P(XA;;FR;;;WD;(Member_of {{SID(BA)}}))(A;;FA;;;OW)",
        AclReason.UNKNOWN_ACE,
    ),
    "wide-category": ("wide", PRIVATE, None, AclReason.DIRECTORY_WIDER),
}  # fmt: skip


@pytest.mark.parametrize("case", list(REFUSED_BEFORE))
def test_a_refusal_of_the_policy_creates_nothing(
    tmp_path, me, record_property, acquisitions, creates, case
):
    """A deny ACE or a conditional ACE at the source, or a category that would give
    Everyone more than the source directory: refused, no ``NtCreateFile`` with a
    descriptor, nothing in the target, the source as it was."""
    what, source_dir, source_sddl, reason = REFUSED_BEFORE[case]
    source, dst = tree(tmp_path, source_dir, PRIVATE)
    if source_sddl is not None:
        set_sddl(source, source_sddl.format(me=me))
    before = state_of(source)
    ops = c.NtOps()
    parent = held(dst)
    directory = None
    try:
        if what == "dir":
            result = category(ops, parent, descriptor(source.parent), me)
        else:
            (dst / CATEGORY).mkdir()  # an existing category, opened with READ_CONTROL
            set_sddl(dst / CATEGORY, WIDE if what == "wide" else PRIVATE)
            directory = held(dst / CATEGORY, access=DIR_ACCESS | a.READ_CONTROL)
            result = c.create_temp(
                ops, directory, identity(directory), TEMP, descriptor(source),
                descriptor(source.parent), me,
            )  # fmt: skip
        observe(record_property, case, result.refusal)
        assert result.state is Created.REFUSED and result.refusal is not None
        assert result.refusal.reason is reason
        assert creates.given == []
    finally:
        close(directory, parent)
    assert state_of(source) == before
    assert [x.name for x in dst.rglob("*")] == ([] if what == "dir" else [CATEGORY])


def test_an_existing_category_as_private_as_the_source_takes_the_temp(tmp_path, me, acquisitions):
    """``create_temp`` in a category this run did not create (held with
    ``READ_CONTROL``) when it gives nobody more than the source directory."""
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    (dst / CATEGORY).mkdir()
    set_sddl(dst / CATEGORY, PRIVATE)
    directory = held(dst / CATEGORY, access=DIR_ACCESS | a.READ_CONTROL)
    result = None
    try:
        result = c.create_temp(
            c.NtOps(), directory, identity(directory), TEMP, descriptor(source),
            descriptor(source.parent), me,
        )  # fmt: skip
        assert result.state is Created.CREATED
    finally:
        close(result, directory)


@pytest.mark.parametrize("what", ["category", "temp"])
def test_a_create_denied_by_the_parent_is_create_failed(
    tmp_path, me, record_property, acquisitions, creates, what
):
    """``NtCreateFile`` fails (the parent's DACL no longer lets the user add): nothing
    was opened, the name is looked up and absent: ``CREATE_FAILED``, not created."""
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    ops = c.NtOps()
    parent = held(dst)
    cat = None
    try:
        if what == "category":
            holder, where, name = parent, dst, CATEGORY
        else:
            cat = category(ops, parent, descriptor(source.parent), me)
            assert cat.handle is not None
            holder, where, name = cat.handle, dst / CATEGORY, TEMP
        expected = identity(holder)
        set_sddl(
            where, "D:P(A;OICI;0x1600a9;;;OW)(A;OICI;FA;;;SY)"
        )  # list, read, WRITE_DAC: no add
        if what == "category":
            result = c.create_category(ops, holder, expected, name, descriptor(source.parent), me)
        else:  # the category check reads the narrower category: not wider
            result = temp(ops, cat, source, me)
        observe(record_property, f"{what} denied", result.refusal)
        assert result.state is Created.REFUSED and result.refusal is not None
        assert result.refusal.reason is CreateReason.CREATE_FAILED
        assert result.lookup is not None and result.lookup.kind is LookupKind.ABSENT
        assert isinstance(result.error, OSError) and result.error.errno == 13  # EACCES
        assert creates.given[-1].status == 0xC000_0022  # STATUS_ACCESS_DENIED
        assert not (where / name).exists()
    finally:
        close(cat, parent)


# --- removal of what was created, after a failed check ----------------------------------


@pytest.mark.parametrize("what", ["category", "temp"])
def test_a_failed_check_after_create_removes_only_the_new_object(
    tmp_path, me, record_property, acquisitions, what
):
    """The descriptor of the new object cannot be read: never a pass; the object this
    call created is removed through its creating handle and proven absent."""
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    before = state_of(source)
    parent = held(dst)
    cat = None
    try:
        if what == "category":
            ops = Ops(fail_read=dst / CATEGORY)
            result = category(ops, parent, descriptor(source.parent), me)
            gone = dst / CATEGORY
        else:
            cat = category(c.NtOps(), parent, descriptor(source.parent), me)
            ops = Ops(fail_read=dst / CATEGORY / TEMP)
            result = temp(ops, cat, source, me)
            gone = dst / CATEGORY / TEMP
        observe(record_property, f"{what} unreadable", result.refusal)
        assert result.state is Created.REMOVED and result.handle is None
        assert result.refusal is not None and result.refusal.reason is AclReason.UNREADABLE
        assert isinstance(result.error, OSError) and result.release_errors == ()
        assert result.disposal is not None and result.disposal.result is Disposed.OK
        assert result.lookup is not None and result.lookup.kind is LookupKind.ABSENT
        assert not gone.exists()
        (created,) = (h for h in ops.handles if h.role in ("H_DIR", "H_TMP"))
        assert created.state is HandleState.CLOSED
    finally:
        close(cat, parent)
    assert state_of(source) == before
    assert [x.name for x in dst.iterdir()] == ([] if what == "category" else [CATEGORY])


def test_a_file_raced_into_the_new_category_keeps_it_and_is_left(
    tmp_path, me, record_property, acquisitions
):
    """Someone puts a file into the new category before its check fails: the removal
    is refused by NTFS (not empty, ``LEFT``); their file is not touched."""
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    theirs = dst / CATEGORY / "theirs.txt"
    parent = held(dst)
    try:
        ops = Ops(fail_read=dst / CATEGORY, race=lambda: theirs.write_bytes(b"theirs"))
        result = category(ops, parent, descriptor(source.parent), me)
        assert result.disposal is not None
        observe(record_property, "raced category", (result.state, result.disposal.statuses))
        assert result.state is Created.LEFT and result.handle is None
        assert result.disposal.result is Disposed.REFUSED
        assert result.disposal.statuses == (None, 0xC000_0101)  # STATUS_DIRECTORY_NOT_EMPTY
        assert theirs.read_bytes() == b"theirs"
    finally:
        close(parent)


def test_the_directory_disposition_never_removes_a_directory_that_is_not_empty(
    tmp_path, me, acquisitions
):
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    parent = held(dst)
    cat = None
    try:
        cat = category(c.NtOps(), parent, descriptor(source.parent), me)
        assert cat.handle is not None
        (dst / CATEGORY / "child.txt").write_bytes(b"child")
        disposal = Disposal()
        assert c.dispose_directory(cat.handle, disposal) is Disposed.REFUSED
        with pytest.raises(AssertionError, match="spent"):
            c.dispose_directory(cat.handle, disposal)  # once
    finally:
        close(cat, parent)
    assert (dst / CATEGORY / "child.txt").read_bytes() == b"child"


# --- failed releases and lost results ---------------------------------------------------


def test_a_failed_close_after_the_disposition_is_unknown_and_kept(tmp_path, me, acquisitions):
    """``CloseHandle`` of the creating handle fails after the disposition: not called
    removed (``REMOVAL_UNKNOWN``), the failure kept, the handle never closed twice."""
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    parent = held(dst)
    acquisitions.select = lambda owner: getattr(owner, "role", None) == "H_DIR"
    try:
        ops = Ops(fail_read=dst / CATEGORY)
        result = category(ops, parent, descriptor(source.parent), me)
        assert result.state is Created.REMOVAL_UNKNOWN and result.lookup is None
        (error,) = result.release_errors
        assert isinstance(error, w.HandleCloseError) and error.winerror == 31
        (created,) = (h for h in ops.handles if h.role == "H_DIR")
        assert created.state is HandleState.CLOSE_FAILED
        assert [acq.closes for acq in acquisitions.of(created)] == [1]
    finally:
        close(parent)


def test_a_failed_close_of_the_lookup_is_unknown_and_kept(tmp_path, me, acquisitions):
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    parent = held(dst)
    acquisitions.select = lambda owner: getattr(owner, "role", None) == "H_LOOKUP"
    try:
        result = category(Ops(fail_read=dst / CATEGORY), parent, descriptor(source.parent), me)
        assert result.state is Created.REMOVAL_UNKNOWN
        assert result.disposal is not None and result.disposal.result is Disposed.OK
        assert result.lookup is not None and result.lookup.kind is LookupKind.UNKNOWN
        (error,) = result.release_errors
        assert isinstance(error, w.HandleCloseError) and error.winerror == 31
    finally:
        close(parent)
    assert not (dst / CATEGORY).exists()  # removed indeed; only not proven by the lookup


@pytest.mark.parametrize("what", ["category", "temp"])
def test_a_lost_create_status_is_unknown_never_closed_or_removed(
    tmp_path, me, record_property, creates, what
):
    """An interrupt right after the real ``NtCreateFile`` (its status never reaches
    ``create_with``): the handle is ``UNKNOWN``, not closed, nothing removed; the
    interrupt is raised. Only the test closes the raw value it saw, for its cleanup."""
    source, dst = tree(tmp_path, PRIVATE, PRIVATE)
    parent = held(dst)
    cat = None
    ops = Ops()
    try:
        if what == "temp":
            cat = category(c.NtOps(), parent, descriptor(source.parent), me)
        creates.interrupt = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            if what == "category":
                category(ops, parent, descriptor(source.parent), me)
            else:
                temp(ops, cat, source, me)
        (lost,) = ops.handles
        call = creates.given[-1]
        observe(record_property, f"lost {what}", (lost.state, lost.outcome, call.status))
        assert call.status == 0 and call.out
        assert lost.state is HandleState.UNKNOWN and lost.outcome is OpenOutcome.UNCERTAIN
        with pytest.raises(w.HandleUnknownError):
            lost.close()  # never closed
        assert (dst / CATEGORY).is_dir()
        if what == "temp":
            assert (dst / CATEGORY / TEMP).is_file()
    finally:
        if ops.handles and ops.handles[-1].state is HandleState.UNKNOWN and creates.given:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            assert kernel32.CloseHandle(creates.given[-1].out)  # the test's own cleanup
        close(cat, parent)
