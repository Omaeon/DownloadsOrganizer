"""R3B-3 primitives on the real NTFS volume of ``tmp_path`` (FIX-03 §9, W-01..W-25).

Every file is created under ``tmp_path``; nothing else is touched. Other processes
(``sys.executable`` subprocesses) stand for third parties: they open, map or change
the test files only. The ownership stand (``acquisitions``) checks every bind and
every ``CloseHandle`` of the production code. Observations the report needs are
printed as ``OBSERVED <step>: ...`` (run with ``-s``) and kept as ``record_property``.
A run of successful calls never proves that ``STATUS_PENDING`` cannot happen.
"""

from __future__ import annotations

import ctypes
import json
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import Acquisitions
from test_winfileops import walk

from downloads_organizer import winfileops as w
from downloads_organizer import winprims as p
from downloads_organizer.winfileops import HandleState, OpenOutcome

pytestmark = [pytest.mark.windows_only, pytest.mark.real_ntfs]

ROOT = Path(__file__).resolve().parents[1]
DIR_ACCESS = w.FILE_LIST_DIRECTORY | w.FILE_READ_ATTRIBUTES | w.FILE_ADD_FILE
DIR_SHARE = w.FILE_SHARE_READ | w.FILE_SHARE_WRITE  # _HELD_SHARE of R3A

SYNC = 0x0010_0000
READ, WRITE, DELETE = 0x1 | SYNC, 0x2 | SYNC, 0x0001_0000 | SYNC

Note = Callable[[str, object], None]

# A third party: NtCreateFile with a \??\ path, a writable mapping, or changes made
# from outside. argv[1] is a JSON request; one JSON line is printed; "hold" waits for
# a line on stdin before exiting (the process keeps what it opened until then).
FOREIGN = r"""
import ctypes, json, os, sys
from ctypes import wintypes as wt
nt = ctypes.WinDLL("ntdll")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
class US(ctypes.Structure):
    _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT),
                ("Buffer", ctypes.c_void_p)]
class OA(ctypes.Structure):
    _fields_ = [("Length", wt.ULONG), ("RootDirectory", wt.HANDLE),
                ("ObjectName", ctypes.POINTER(US)), ("Attributes", wt.ULONG),
                ("SecurityDescriptor", ctypes.c_void_p),
                ("SecurityQualityOfService", ctypes.c_void_p)]
class IOSB(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]
nt.NtCreateFile.restype = ctypes.c_ulong
nt.NtSetEaFile.restype = ctypes.c_ulong
k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileMappingW.restype = wt.HANDLE
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
k32.CreateFileMappingW.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.DWORD,
                                   wt.LPCWSTR]
k32.CloseHandle.argtypes = [wt.HANDLE]
keep = []
def ntopen(path, access, share, disposition=1, options=0x60):
    name = "\\??\\" + path
    buf = ctypes.create_unicode_buffer(name)
    n = len(name.encode("utf-16-le", "surrogatepass"))
    us = US(n, n + 2, ctypes.addressof(buf))
    oa = OA(ctypes.sizeof(OA), None, ctypes.pointer(us), 0x40, None, None)
    out, iosb = wt.HANDLE(0), IOSB()
    st = nt.NtCreateFile(ctypes.byref(out), access, ctypes.byref(oa), ctypes.byref(iosb),
                         None, 0, share, disposition, options, None, 0)
    keep.append((buf, us, oa, out))
    return st, out.value
def win32(ok):
    return "ok" if ok else f"winerror {ctypes.get_last_error()}"
req = json.loads(sys.argv[1])
res = {}
if req["op"] == "open":
    res["statuses"] = [ntopen(*a)[0] for a in req["opens"]]
elif req["op"] == "map":
    h = k32.CreateFileW(req["path"], 0xC0000000, 7, None, 3, 0x80, None)
    m = k32.CreateFileMappingW(h, None, 4, 0, 0, None)
    v = k32.MapViewOfFile(m, 2, 0, 0, 0)
    res["mapped"] = bool(h and h != ctypes.c_void_p(-1).value and m and v)
    res["file_closed"] = bool(k32.CloseHandle(h))  # the view and the section stay
elif req["op"] == "mutate":
    path, kinds = req["path"], req["kinds"]
    if "attributes" in kinds:
        res["attributes"] = win32(k32.SetFileAttributesW(path, 0x22))
    if "mtime" in kinds:
        try:
            os.utime(path, ns=(1_500_000_000_000_000_000, 1_500_000_000_000_000_000))
            res["mtime"] = "ok"
        except OSError as exc:
            res["mtime"] = f"winerror {exc.winerror}"
    if "hardlink" in kinds:
        res["hardlink"] = win32(k32.CreateHardLinkW(req["link"], path, None))
    if "ads" in kinds:
        try:
            with open(path + ":extra", "wb") as f:
                f.write(b"alternate")
            res["ads"] = "ok"
        except OSError as exc:
            res["ads"] = f"winerror {exc.winerror}"
    if "ea" in kinds:
        h = k32.CreateFileW(path, 0x10 | 0x100000, 7, None, 3, 0x80, None)
        if not h or h == ctypes.c_void_p(-1).value:
            res["ea"] = win32(False)
        else:
            name, value = b"DOTEST", b"v1"
            ea = (0).to_bytes(4, "little") + bytes([0, len(name)])
            ea += len(value).to_bytes(2, "little")
            ea += name + b"\0" + value
            ea += b"\0" * (-len(ea) % 4)
            buf = ctypes.create_string_buffer(ea, len(ea))
            iosb = IOSB()
            st = nt.NtSetEaFile(wt.HANDLE(h), ctypes.byref(iosb), buf, len(ea))
            res["ea"] = "ok" if st == 0 else f"status {st:#010x}"
            k32.CloseHandle(h)
print(json.dumps(res), flush=True)
if req.get("hold"):
    sys.stdin.readline()
"""


def foreign(request: dict[str, object]) -> dict[str, object]:
    done = subprocess.run(
        [sys.executable, "-I", "-c", FOREIGN, json.dumps(request)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(done.stdout)


@contextmanager
def held(request: dict[str, object]) -> Iterator[dict[str, object]]:
    """A third party that keeps what it opened (or mapped) until the block ends."""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", FOREIGN, json.dumps({**request, "hold": True})],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        yield json.loads(process.stdout.readline())
    finally:
        process.communicate("\n", timeout=60)


def foreign_statuses(path: Path, *accesses: int) -> list[int]:
    opens = [[str(path), access, 7] for access in accesses]
    return foreign({"op": "open", "opens": opens})["statuses"]  # type: ignore[return-value]


@pytest.fixture
def note(record_property: Callable[[str, object], None]) -> Note:
    def note(step: str, observed: object) -> None:
        print(f"OBSERVED {step}: {observed}")
        record_property(step, repr(observed))

    return note


@pytest.fixture
def room(tmp_path: Path, acquisitions: Acquisitions) -> Iterator[tuple[Path, w.Handle]]:
    """A directory of ``tmp_path`` held as R3A holds one, with ``FILE_ADD_FILE``."""
    path = tmp_path / "room"
    path.mkdir()
    directory = walk(path, access=DIR_ACCESS, share=DIR_SHARE)
    yield path, directory
    directory.close()


def create(directory: w.Handle, name: str) -> w.Handle:
    handle = w.Handle.new(directory.path / name, "H_TMP")
    assert p.create_into(handle, directory, name) is OpenOutcome.CREATED_OWNED
    return handle


def opened(directory: w.Handle, name: str, role: str) -> w.Handle:
    handle = w.Handle.new(directory.path / name, role)
    assert p.open_into(handle, directory, name) is OpenOutcome.OPENED_OWNED
    assert not handle.unreliable
    return handle


def iosb(handle: w.Handle) -> tuple[int, int]:
    status = handle._iosb.Status or 0  # type: ignore[union-attr]
    return w.signed32(status) & 0xFFFF_FFFF, handle._iosb.Information  # type: ignore[union-attr]


def names(path: Path) -> list[str]:
    return sorted(entry.name for entry in path.iterdir())


def lookup(directory: w.Handle, name: str) -> p.Lookup:
    return p.lookup_entry(w.Handle.new(directory.path, "H_LOOKUP"), directory, name)


# --- T22: the W sequence of one temporary file (W-01..W-11, W-15, W-16, W-19, W-20) ----


def test_t22_w_sequence(
    room: tuple[Path, w.Handle], acquisitions: Acquisitions, note: Note
) -> None:
    path, directory = room
    news, inits = acquisitions.news, acquisitions.inits
    (path / "other.txt").write_bytes(b"other")

    # W-01, W-19, W-20: CREATE, share 0, a pre-filled IOSB
    tmp = create(directory, "t.tmp")
    assert tmp.status == 0 and iosb(tmp)[1] == 2
    note("W-01/W-19/W-20 CREATE", {"st": tmp.status, "iosb": iosb(tmp)})
    assert iosb(tmp)[0] == 0  # W-19: the IOSB status agrees here (it decides nothing)

    # W-02: a third party gets a sharing violation for READ, WRITE and DELETE
    assert foreign_statuses(path / "t.tmp", READ, WRITE, DELETE) == [0xC000_0043] * 3

    # W-03 / T23: opens that only read metadata, while share 0 is held
    probe = w.Handle.new(path / "t.tmp", "H_PROBE")
    probe_outcome = p.open_into(probe, directory, "t.tmp")
    note("W-03/T23 H_PROBE on share 0", (probe_outcome, hex(probe.status or 0)))
    if probe.state is HandleState.OPEN:
        assert w.query(probe).file_id == w.query(tmp).file_id
        probe.close()
    source = w.Handle.new(path / "t.tmp", "H_SRC")
    assert p.open_into(source, directory, "t.tmp") is OpenOutcome.NOT_OPENED
    assert source.status == p.STATUS_SHARING_VIOLATION and acquisitions.of(source) == []
    assert p.open_error(source).winerror == 32  # ERROR_SHARING_VIOLATION

    # W-04: writes and reads at offsets
    p.write_at(tmp, 0, b"head")
    p.write_at(tmp, 10, b"tail")
    assert p.read_at(tmp, 0, 64) == b"head" + b"\0" * 6 + b"tail"
    assert p.read_at(tmp, 10, 2) == b"ta" and p.read_at(tmp, 14, 8) == b""
    assert w.query(tmp).size == 14

    # W-05, W-06: rename (no replace) while the directory is held with READ | WRITE
    attempt = p.RenameAttempt()
    assert p.rename_once(tmp, directory, "final.txt", attempt) is p.Renamed.OK
    assert attempt.status == 0 and p.last_component(p.query_name(tmp)) == "final.txt"
    assert sorted(names(path)) == ["final.txt", "other.txt"]

    # W-07: flush without FILE_APPEND_DATA
    p.flush(tmp)

    # W-08: the final name has the temporary file id; the temporary name is gone
    found = lookup(directory, "final.txt")
    assert found.kind is p.LookupKind.FOUND and found.file_id == w.query(tmp).file_id
    assert lookup(directory, "t.tmp").kind is p.LookupKind.ABSENT

    # W-09: two queries on the same H_LOOKUP
    h_lookup = opened(directory, "", "H_LOOKUP")
    first, second = (p.query_directory(h_lookup, "final.txt") for _ in range(2))
    h_lookup.close()
    note("W-09 second query", [(hex(x.status or 0), x.entries) for x in (first, second)])
    assert first.status == second.status == 0 and first.entries == second.entries

    # W-10: still share 0 under the final name
    assert foreign_statuses(path / "final.txt", READ) == [0xC000_0043]

    # W-11: a collision is refused and both files stay as they were
    collision = p.RenameAttempt()
    assert p.rename_once(tmp, directory, "other.txt", collision) is p.Renamed.EXISTS
    assert collision.status == 0xC000_0035
    assert p.last_component(p.query_name(tmp)) == "final.txt"
    tmp.close()
    assert (path / "other.txt").read_bytes() == b"other"
    assert (path / "final.txt").read_bytes() == b"head" + b"\0" * 6 + b"tail"

    # W-15: OPEN reports FILE_OPENED
    src = opened(directory, "final.txt", "H_SRC")
    assert src.status == 0 and iosb(src) == (0, 1)
    note("W-15 OPEN", {"st": src.status, "iosb": iosb(src)})
    src.close()

    # W-16, W-20: CREATE on a taken name
    taken = w.Handle.new(path / "final.txt", "H_TMP")
    assert p.create_into(taken, directory, "final.txt") is OpenOutcome.NAME_TAKEN
    assert taken.status == 0xC000_0035 and (taken._out.value or 0) == 0  # type: ignore[union-attr]
    note("W-16/W-20 CREATE taken", {"st": hex(taken.status), "iosb": iosb(taken)})
    assert acquisitions.of(taken) == []

    # T63: one Handle.new per open, no __init__, every Acq closed once
    assert acquisitions.inits == inits and acquisitions.news == news + 8
    assert all(acq.closes == 1 for acq in acquisitions.acqs if acq.kind == "bind")


# --- open outcomes on their own (W-17, W-18, T29) ---------------------------------------


def test_w18_sharing_violation(room: tuple[Path, w.Handle], acquisitions: Acquisitions) -> None:
    path, directory = room
    (path / "busy.txt").write_bytes(b"x")
    with held({"op": "open", "opens": [[str(path / "busy.txt"), READ, 0]]}) as third:
        assert third["statuses"] == [0]
        source = w.Handle.new(path / "busy.txt", "H_SRC")
        assert p.open_into(source, directory, "busy.txt") is OpenOutcome.NOT_OPENED
        assert source.status == p.STATUS_SHARING_VIOLATION
        assert acquisitions.of(source) == [] and source.state is HandleState.NOT_OPENED


def test_w17_deny_ace_is_access_denied(
    room: tuple[Path, w.Handle], acquisitions: Acquisitions, note: Note
) -> None:
    path, directory = room
    target = path / "denied.txt"
    target.write_bytes(b"x")
    icacls = shutil.which("icacls")
    assert icacls is not None
    subprocess.run([icacls, str(target), "/deny", "*S-1-1-0:(RD)"], check=True, capture_output=True)
    try:
        source = w.Handle.new(target, "H_SRC")
        assert p.open_into(source, directory, "denied.txt") is OpenOutcome.NOT_OPENED
        assert source.status == p.STATUS_ACCESS_DENIED and acquisitions.of(source) == []
        probe = w.Handle.new(target, "H_PROBE")
        note("W-17 H_PROBE under deny RD", (p.open_into(probe, directory, "denied.txt"),))
        probe.close()
    finally:
        subprocess.run(
            [icacls, str(target), "/remove:d", "*S-1-1-0"], check=True, capture_output=True
        )
    assert target.read_bytes() == b"x"


def test_t29_writable_mapping_against_h_src(
    room: tuple[Path, w.Handle], acquisitions: Acquisitions, note: Note
) -> None:
    path, directory = room
    (path / "mapped.bin").write_bytes(b"\0" * 4096)
    with held({"op": "map", "path": str(path / "mapped.bin")}) as third:
        assert third == {"mapped": True, "file_closed": True}
        source = w.Handle.new(path / "mapped.bin", "H_SRC")
        outcome = p.open_into(source, directory, "mapped.bin")
        note("T29 H_SRC with a writable mapping elsewhere", (outcome, hex(source.status or 0)))
        assert outcome in (OpenOutcome.OPENED_OWNED, OpenOutcome.NOT_OPENED)
        source.close()


# --- W-12a / W-12b, W-14: disposition (T58, T59) --------------------------------------


@pytest.mark.parametrize("posix", [True, False], ids=["W-12a-T58-posix", "W-12b-T59-classic"])
def test_w12_dispose_deletes_after_close(
    room: tuple[Path, w.Handle], posix: bool, note: Note
) -> None:
    path, directory = room
    name = "posix.tmp" if posix else "classic.tmp"
    tmp = create(directory, name)
    p.write_at(tmp, 0, b"data")
    disposal = p.Disposal()
    assert p.dispose(tmp, disposal, posix=posix) is p.Disposed.OK
    info = w.query(tmp)
    after = lookup(directory, name)
    note(f"W-12 posix={posix}", {"P": disposal.statuses, "info": info, "lookup": after})
    assert info.delete_pending and disposal.statuses == ((0, None) if posix else (None, 0))
    if posix:  # links 0 at once; the entry stays listed until this handle closes
        assert info.links == 0 and p.read_at(tmp, 0, 4) == b"data"
    # observed on NTFS: lookup is still FOUND while the delete is pending, so FOUND does
    # not mean "not delete pending" (recorded above; the caller must query the handle)
    tmp.close()
    assert lookup(directory, name).kind is p.LookupKind.ABSENT
    assert names(path) == []


def test_w14_readonly_is_refused_without_fallback(room: tuple[Path, w.Handle]) -> None:
    path, directory = room
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    tmp = create(directory, "ro.tmp")
    assert kernel32.SetFileAttributesW(str(path / "ro.tmp"), 0x1)  # READONLY, from a path
    try:
        disposal = p.Disposal()
        assert p.dispose(tmp, disposal) is p.Disposed.REFUSED
        assert disposal.statuses == (p.STATUS_CANNOT_DELETE, None) and not disposal.classic_spent
        assert not w.query(tmp).delete_pending
    finally:
        assert kernel32.SetFileAttributesW(str(path / "ro.tmp"), 0x80)
        tmp.close()
    assert names(path) == ["ro.tmp"]


# --- metadata: W-13, T39, space ---------------------------------------------------------


def snapshot(handle: w.Handle) -> dict[str, object]:
    times, info = p.query_times(handle), w.query(handle)
    return {
        "attributes": times.attributes,
        "mtime": times.last_write,
        "change": times.change,
        "links": info.links,
        "streams": p.query_streams(handle),
        "ea": p.query_ea_size(handle),
    }


def test_w13_outside_changes_are_seen(
    room: tuple[Path, w.Handle], tmp_path: Path, note: Note
) -> None:
    path, directory = room
    (path / "src.txt").write_bytes(b"source")
    source = opened(directory, "src.txt", "H_SRC")
    before = snapshot(source)
    assert before["streams"] == (p.Stream("::$DATA", 6),) and before["ea"] == 0
    detected = {
        "attributes": lambda a, b: a["attributes"] != b["attributes"],
        "mtime": lambda a, b: a["mtime"] != b["mtime"],
        "hardlink": lambda a, b: b["links"] == a["links"] + 1,
        "ads": lambda a, b: len(b["streams"]) == len(a["streams"]) + 1,
        "ea": lambda a, b: b["ea"] > a["ea"],
    }
    results: dict[str, object] = {}
    for kind, seen in detected.items():
        current = snapshot(source)
        done = foreign(
            {"op": "mutate", "path": str(path / "src.txt"), "kinds": [kind],
             "link": str(tmp_path / "room" / "link.txt")}
        )  # fmt: skip
        after = snapshot(source)
        results[kind] = done[kind]
        if done[kind] == "ok":  # what got through while H_SRC is held must be seen
            assert seen(current, after), kind
            assert after["change"] != current["change"] or kind == "ads", kind
    note("W-13 outside changes while H_SRC (share READ) is held", results)
    assert p.read_at(source, 0, 16) == b"source"
    source.close()


def test_t39_set_mtime_is_exact(room: tuple[Path, w.Handle], note: Note) -> None:
    path, directory = room
    tmp = create(directory, "t.tmp")
    p.write_at(tmp, 0, b"x")
    before = p.query_times(tmp)
    mtime_ns = 1_234_567_890_123_456_789
    p.set_mtime(tmp, mtime_ns)
    after = p.query_times(tmp)
    assert after.mtime_ns == mtime_ns // 100 * 100
    assert (after.creation, after.attributes) == (before.creation, before.attributes)
    note("T39 ChangeTime", {"before": before.change, "after": after.change})
    p.flush(tmp)
    tmp.close()
    assert (path / "t.tmp").stat().st_mtime_ns == mtime_ns // 100 * 100


def reply_size(handle: w.Handle, struct: type[ctypes.Structure], info_class: int) -> int:
    """``IOSB.Information`` of one raw fixed-size query (what the filesystem reports)."""
    api = p._prims()
    result, block = struct(), api.base.IoStatusBlock()
    status = api.ntdll.NtQueryInformationFile(
        handle.raw, ctypes.byref(block), ctypes.byref(result), ctypes.sizeof(result), info_class
    )
    assert status == 0
    return block.Information


def test_m01_ea_on_ntfs(room: tuple[Path, w.Handle], note: Note) -> None:
    """R3B3-M01: NTFS answers ``FileEaInformation`` (and ``FileBasicInformation``) with
    exactly the structure size, both without and with an EA; the strict check holds."""
    path, directory = room
    (path / "plain.txt").write_bytes(b"plain")
    (path / "ea.txt").write_bytes(b"with ea")
    assert foreign({"op": "mutate", "path": str(path / "ea.txt"), "kinds": ["ea"]}) == {"ea": "ok"}
    api = p._prims()
    observed: dict[str, object] = {}
    for name in ("plain.txt", "ea.txt"):
        handle = opened(directory, name, "H_SRC")
        observed[name] = {
            "ea_size": p.query_ea_size(handle),
            "ea_information": reply_size(handle, api.EaInfo, 7),
            "basic_information": reply_size(handle, api.base.BasicInfo, 4),
        }
        p.query_times(handle)
        handle.close()
    note("M01 FileEaInformation / FileBasicInformation replies", observed)
    assert observed["plain.txt"] == {"ea_size": 0, "ea_information": 4, "basic_information": 40}
    ea = observed["ea.txt"]
    assert ea["ea_size"] > 0 and ea["ea_information"] == 4  # type: ignore[index, operator]


def test_query_space_matches_the_volume(room: tuple[Path, w.Handle]) -> None:
    path, directory = room
    space = p.query_space(directory)
    usage = shutil.disk_usage(path)
    assert space.total == usage.total and 0 < space.available <= space.total
    assert abs(space.available - usage.free) < 1 << 30


# --- lookup_entry on NTFS: W-25, T55 ----------------------------------------------------


def test_w25_layout_and_ids(room: tuple[Path, w.Handle]) -> None:
    _path, directory = room
    name = "w25 ä😀.txt"
    tmp = create(directory, name)
    assert p.DirInfo.FileName.offset == 88 and p.DirInfo.FileId.offset == 72
    h_lookup = opened(directory, "", "H_LOOKUP")
    listing = p.query_directory(h_lookup, name)
    h_lookup.close()
    assert listing.status == 0 and listing.entries is not None
    (entry,) = listing.entries
    assert entry.name == name.encode("utf-16-le")  # FileNameLength == len(encoded)
    assert entry.file_id == w.query(tmp).file_id
    tmp.close()


@pytest.mark.parametrize(
    "name",
    ["plain.txt", "Ünïcödé.txt", "😀 outside the BMP.txt", "x" * 253 + "😀"],
    ids=["ascii", "latin", "astral", "255-units"],
)
def test_t55_unicode_names_are_found(room: tuple[Path, w.Handle], name: str) -> None:
    _, directory = room
    tmp = create(directory, name)
    file_id = w.query(tmp).file_id
    tmp.close()
    assert lookup(directory, name) == p.Lookup(p.LookupKind.FOUND, file_id)


def test_t55_lone_surrogate(room: tuple[Path, w.Handle], note: Note) -> None:
    path, directory = room
    name = "lone\udc00.txt"
    tmp = w.Handle.new(path / "lone", "H_TMP")
    outcome = p.create_into(tmp, directory, name)
    note("T55 lone surrogate CREATE", (outcome, hex(tmp.status or 0)))
    if outcome is not OpenOutcome.CREATED_OWNED:
        pytest.skip(f"NOT_TESTED: NTFS refused a lone surrogate ({tmp.status:#010x})")
    file_id = w.query(tmp).file_id
    tmp.close()
    assert lookup(directory, name) == p.Lookup(p.LookupKind.FOUND, file_id)
    h_lookup = opened(directory, "", "H_LOOKUP")
    (entry,) = p.query_directory(h_lookup, name).entries or ()
    h_lookup.close()
    assert entry.name == name.encode("utf-16-le", "surrogatepass")


def test_t55_other_spellings_are_never_found(room: tuple[Path, w.Handle], note: Note) -> None:
    path, directory = room
    nfc = unicodedata.normalize("NFC", "café.txt")
    nfd = unicodedata.normalize("NFD", nfc)
    long_name = "LongFileName Of Test.txt"
    for name in (nfc, long_name, "final.txt"):
        create(directory, name).close()
    assert lookup(directory, "FINAL.TXT").kind is p.LookupKind.UNKNOWN  # same file, case
    assert lookup(directory, "Final.txt").reason == "the entry has another name"
    nfd_result = lookup(directory, nfd)
    note("T55 NFD of an NFC name", nfd_result)
    assert nfd_result.kind is not p.LookupKind.FOUND
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_unicode_buffer(300)
    assert kernel32.GetShortPathNameW(str(path / long_name), buffer, 300)
    short = Path(buffer.value).name
    if short == long_name:
        note("T55 8.3", "NOT_TESTED: no short names on this volume")
    else:
        short_result = lookup(directory, short)
        note("T55 8.3", {"short": short, "lookup": short_result})
        assert short_result.kind is p.LookupKind.UNKNOWN  # never FOUND by its short name


def test_t55_rename_between_lookups(room: tuple[Path, w.Handle]) -> None:
    path, directory = room
    tmp = create(directory, "before.txt")
    file_id = w.query(tmp).file_id
    tmp.close()
    assert lookup(directory, "before.txt").file_id == file_id
    (path / "before.txt").rename(path / "after.txt")  # someone else renames it
    assert lookup(directory, "before.txt").kind is p.LookupKind.ABSENT
    assert lookup(directory, "after.txt") == p.Lookup(p.LookupKind.FOUND, file_id)


def test_lookup_refuses_directories(room: tuple[Path, w.Handle]) -> None:
    path, directory = room
    (path / "sub").mkdir()
    result = lookup(directory, "sub")
    assert result.kind is p.LookupKind.UNKNOWN and "attributes" in result.reason


# --- ownership on NTFS: W-21..W-24, interrupts ------------------------------------------


def test_w21_no_pending_seen_in_runs(room: tuple[Path, w.Handle], note: Note) -> None:
    """1000 calls of each kind return a final status. This is an observation only: it
    does not prove that STATUS_PENDING (0x103) is impossible (A-SYNC is not proven)."""
    path, directory = room
    create(directory, "probe.txt").close()
    statuses: dict[str, set[int]] = {"open": set(), "query": set(), "read": set()}
    for _ in range(1000):
        probe = w.Handle.new(path / "probe.txt", "H_PROBE")
        p.open_into(probe, directory, "probe.txt")
        statuses["open"].add(probe.status)  # type: ignore[arg-type]
        probe.close()
    h_lookup = opened(directory, "", "H_LOOKUP")
    for _ in range(1000):
        statuses["query"].add(p.query_directory(h_lookup, "probe.txt").status)  # type: ignore[arg-type]
    h_lookup.close()
    source = opened(directory, "probe.txt", "H_SRC")
    for _ in range(1000):
        p.read_at(source, 0, 16)  # raises on any status but 0 and END_OF_FILE
        statuses["read"].add(0)
    source.close()
    note("W-21 statuses of 3x1000 calls (A-SYNC not proven)", statuses)
    assert statuses == {"open": {0}, "query": {0}, "read": {0}}


W22 = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from downloads_organizer import winfileops as w, winprims as p
room = Path(sys.argv[2])
handle = w.open_anchor(Path(room.anchor))
for index, part in enumerate(room.parts[1:], start=2):
    final = index == len(room.parts)
    child, _ = w.open_relative(handle, part, directory=True,
        access=0x83 if final else 0x80, share=3 if final else 7)
    handle.close()
    handle = child
closes = []
kernel32 = w._api().kernel32
real_close = kernel32.CloseHandle
def spy(raw):
    closes.append(raw)
    return real_close(raw)
kernel32.CloseHandle = spy
api = p._prims()
real = api.ntdll.NtCreateFile
def shim(*args):
    real(*args)
    raise KeyboardInterrupt
api.ntdll.NtCreateFile = shim
tmp = w.Handle.new(room / "w22.tmp", "H_TMP")
try:
    p.create_into(tmp, handle, "w22.tmp")
    interrupted = False
except KeyboardInterrupt:
    interrupted = True
api.ntdll.NtCreateFile = real
try:
    tmp.close()
    unknown = False
except w.HandleUnknownError:
    unknown = True
print(json.dumps({"interrupted": interrupted, "state": tmp.state.name, "unknown": unknown,
    "outcome": tmp.outcome.name, "out_written": bool(tmp._out.value), "closes": len(closes),
    "pinned": any(o is tmp for o in w._UNRESOLVED), "exists": os.path.exists(room / "w22.tmp")}))
"""


def test_w22_interrupt_after_a_real_create(tmp_path: Path) -> None:
    room = tmp_path / "w22"
    room.mkdir()
    done = subprocess.run(
        [sys.executable, "-I", "-c", W22, str(ROOT), str(room)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(done.stdout)
    assert result == {
        "interrupted": True,
        "state": "UNKNOWN",
        "unknown": True,
        "outcome": "UNCERTAIN",
        "out_written": True,
        "closes": 0,
        "pinned": True,
        "exists": True,
    }
    assert names(room) == ["w22.tmp"]  # the process exit closed the leaked handle


def test_w23_close_failure_of_h_tmp(
    room: tuple[Path, w.Handle], acquisitions: Acquisitions
) -> None:
    _, directory = room
    acquisitions.select = lambda owner: getattr(owner, "role", None) == "H_TMP"
    tmp = create(directory, "t.tmp")
    raw, start = tmp.raw, len(acquisitions.calls)
    with pytest.raises(w.HandleCloseError) as first:
        tmp.close()
    with pytest.raises(w.HandleCloseError) as second:
        tmp.close()
    assert first.value is second.value and tmp.state is HandleState.CLOSE_FAILED
    assert acquisitions.calls[start:] == [raw] and raw in acquisitions.live  # 1 close
    assert tmp.close_error is first.value and first.value.winerror == 31
    # the stand's teardown closes the raw value directly (never through Handle.close)


def test_w24_reused_raw_values_are_separate_acquisitions(
    room: tuple[Path, w.Handle], acquisitions: Acquisitions, note: Note
) -> None:
    _path, directory = room
    create(directory, "probe.txt").close()
    seen: list[int] = []
    for _ in range(64):
        probe = opened(directory, "probe.txt", "H_PROBE")
        seen.append(probe.raw)
        probe.close()
        if len(seen) > 1 and seen[-1] in seen[:-1]:
            break
    reused = len(set(seen)) < len(seen)
    note("W-24 raw value reused by the kernel", {"reused": reused, "opens": len(seen)})
    if not reused:
        pytest.skip("NOT_TESTED: the kernel did not hand a value out again in 64 opens")
    acqs = [acq for acq in acquisitions.acqs if acq.raw == seen[-1] and acq.kind == "bind"]
    assert len(acqs) >= 2 and all(acq.closes == 1 for acq in acqs)
    assert acquisitions.violations == []


def test_interrupt_in_a_real_query_closes_h_lookup_once(
    room: tuple[Path, w.Handle], acquisitions: Acquisitions, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, directory = room
    create(directory, "a.txt").close()
    ntdll = p._prims().ntdll
    real = ntdll.NtQueryDirectoryFile

    def shim(*args: object) -> int:
        real(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(ntdll, "NtQueryDirectoryFile", shim)
    before = len(w._UNRESOLVED)
    h_lookup = w.Handle.new(directory.path, "H_LOOKUP")
    with pytest.raises(KeyboardInterrupt) as raised:
        p.lookup_entry(h_lookup, directory, "a.txt")
    assert w.release_failures(raised.value) == []
    (acq,) = acquisitions.of(h_lookup)
    assert acq.closes == 1 and h_lookup.state is HandleState.CLOSED
    assert len(w._UNRESOLVED) == before + 1  # the query's buffers are pinned


def test_interrupt_in_a_real_rename_is_lost_not_undone(
    room: tuple[Path, w.Handle], note: Note
) -> None:
    path, directory = room
    tmp = create(directory, "t.tmp")
    ntdll = p._prims().ntdll
    real = ntdll.NtSetInformationFile

    def shim(*args: object) -> int:
        real(*args)
        raise KeyboardInterrupt

    attempt = p.RenameAttempt()
    with pytest.MonkeyPatch.context() as patch, pytest.raises(KeyboardInterrupt):
        patch.setattr(ntdll, "NtSetInformationFile", shim)
        p.rename_once(tmp, directory, "final.txt", attempt)
    assert attempt.result is p.Renamed.LOST and attempt.spent
    note("rename interrupted after the real call", names(path))
    assert names(path) == ["final.txt"]  # LOST: it happened, the status was lost
    tmp.close()
