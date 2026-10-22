"""R3B-3 primitives against a scripted ntdll and a fake handle pool (FIX-03 §9.1).

No real NT call is made here (only ``RtlNtStatusToDosError`` for messages): every
handle value is a phantom of ``FakePool``, which hands a released value out again at
once. The ownership stand (``Acquisitions``) checks every bind and every close.
Real NTFS behaviour is in ``test_winprims_ntfs.py``.
"""

from __future__ import annotations

import ctypes
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import Acquisitions, FakeNt, FakePool

from downloads_organizer import winfileops as w
from downloads_organizer import winprims as p
from downloads_organizer.winfileops import HandleState, OpenOutcome

pytestmark = [pytest.mark.windows_only, pytest.mark.fake_api]

CREATE, OPEN = 2, 1
INVALID = ctypes.c_void_p(-1).value
HDR = 88


@pytest.fixture
def nt(fake_nt: tuple[FakeNt, Acquisitions]) -> Iterator[tuple[FakeNt, Acquisitions, w.Handle]]:
    fake, stand = fake_nt
    parent = w.Handle(fake.pool.take(), Path("C:/fake/dir"))  # an R3A owner: one Acq
    yield fake, stand, parent
    if parent.state is HandleState.OPEN:
        parent.close()


def pinned(obj: object) -> bool:
    return any(o is obj for o in w._UNRESOLVED)


def created(parent: w.Handle, name: str = "t.tmp") -> w.Handle:
    handle = w.Handle.new(parent.path / name, "H_TMP")
    assert p.create_into(handle, parent, name) is OpenOutcome.CREATED_OWNED
    return handle


# --- T45 (ERRATA B-01): NEW, CALLING, OPEN -----------------------------------------------


def test_t45_new_handle_closes_to_not_opened(nt: tuple[FakeNt, Acquisitions, w.Handle]) -> None:
    fake, stand, parent = nt
    handle = w.Handle.new(parent.path / "x", "H_TMP")
    handle.close()
    assert handle.state is HandleState.NOT_OPENED
    assert stand.of(handle) == [] and stand.calls == [] and not pinned(handle)
    handle.close()  # still a no-op
    other = w.Handle.new(parent.path / "y", "H_SRC")
    assert other.classify(OPEN) is OpenOutcome.NOT_CALLED
    assert other.state is HandleState.NOT_OPENED and fake.ntdll.log == []


@pytest.mark.parametrize("written", [False, True], ids=["before-writing", "after-out-and-iosb"])
@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_t45_interrupted_call_is_unknown(
    nt: tuple[FakeNt, Acquisitions, w.Handle], written: bool, interrupt: type[BaseException]
) -> None:
    fake, stand, parent = nt
    step: object = {"out": "pool", "raise_after": interrupt()} if written else interrupt()
    fake.ntdll.script["NtCreateFile"] = [step]
    handle = w.Handle.new(parent.path / "t.tmp", "H_TMP")
    with pytest.raises(interrupt):
        p.create_into(handle, parent, "t.tmp")
    assert handle.state is HandleState.UNKNOWN and handle.outcome is OpenOutcome.UNCERTAIN
    assert handle.status is None and pinned(handle)
    assert stand.of(handle) == []
    with pytest.raises(w.HandleUnknownError) as first:
        handle.close()
    with pytest.raises(w.HandleUnknownError) as second:
        handle.close()
    assert first.value is second.value
    out = parent.raw + 4 if written else 0  # the value the fake wrote before raising
    assert first.value.handle_value == out and first.value.winerror == 0
    assert stand.calls == []  # CloseHandle on the phantom out: 0


def test_t45_calling_left_without_status_is_unknown(
    nt: tuple[FakeNt, Acquisitions, w.Handle],
) -> None:
    _, stand, parent = nt
    handle = w.Handle.new(parent.path / "x", "H_TMP")
    handle.state = HandleState.CALLING  # the call was entered and left without a status
    with pytest.raises(w.HandleUnknownError) as first:
        handle.close()
    assert handle.state is HandleState.UNKNOWN and pinned(handle)
    with pytest.raises(w.HandleUnknownError) as second:
        handle.close()
    assert first.value is second.value and stand.calls == []


def test_t45_unknown_error_reaches_release_failures(
    nt: tuple[FakeNt, Acquisitions, w.Handle],
) -> None:
    fake, stand, parent = nt
    fake.ntdll.script["NtCreateFile"] = [{"out": "pool", "raise_after": KeyboardInterrupt()}]
    handle = w.Handle.new(parent.path, "H_LOOKUP")
    with pytest.raises(KeyboardInterrupt) as raised:
        p.lookup_entry(handle, parent, "a.txt")
    failures = w.release_failures(raised.value)
    assert len(failures) == 1 and failures[0] is handle.close_error
    assert isinstance(failures[0], w.HandleUnknownError)
    assert stand.of(handle) == [] and stand.calls == []


def test_t45_open_is_one_acquisition_closed_once(
    nt: tuple[FakeNt, Acquisitions, w.Handle],
) -> None:
    _, stand, parent = nt
    news, inits = stand.news, stand.inits
    handle = created(parent)
    assert stand.news == news + 1 and stand.inits == inits  # 0 __init__ in create_into
    (acq,) = stand.of(handle)
    assert acq.kind == "bind" and acq.raw == handle.raw
    handle.close()
    handle.close()
    assert acq.closes == 1 and handle.state is HandleState.CLOSED


# --- T49: every row of §2.5, F-01 ---------------------------------------------------------

ROWS = [
    # id, disposition, step, outcome, state, unreliable, pinned
    ("3", CREATE, {}, OpenOutcome.CREATED_OWNED, HandleState.OPEN, False, False),
    ("4-information", CREATE, {"information": 1}, OpenOutcome.OPENED_OWNED, HandleState.OPEN, True, False),  # noqa: E501
    ("4-iosb-status", CREATE, {"iosb_status": 0x103}, OpenOutcome.OPENED_OWNED, HandleState.OPEN, True, False),  # noqa: E501
    ("5", CREATE, {"status": 0xC000_0035}, OpenOutcome.NAME_TAKEN, HandleState.NOT_OPENED, False, False),  # noqa: E501
    ("6", CREATE, {"status": 0xC000_0022}, OpenOutcome.NOT_CREATED_CLAIMED, HandleState.NOT_OPENED, False, False),  # noqa: E501
    ("7", OPEN, {}, OpenOutcome.OPENED_OWNED, HandleState.OPEN, False, False),
    ("8", OPEN, {"information": 2}, OpenOutcome.OPENED_OWNED, HandleState.OPEN, True, False),
    ("9", OPEN, {"status": 0xC000_0034}, OpenOutcome.NOT_OPENED, HandleState.NOT_OPENED, False, False),  # noqa: E501
    ("10-create", CREATE, {"status": 0xC000_0035, "out": "pool"}, OpenOutcome.ANOMALY, HandleState.UNKNOWN, False, True),  # noqa: E501
    ("10-open", OPEN, {"status": 0xC000_0022, "out": "pool"}, OpenOutcome.ANOMALY, HandleState.UNKNOWN, False, True),  # noqa: E501
    ("11-zero", CREATE, {"status": 0, "out": 0}, OpenOutcome.ANOMALY, HandleState.UNKNOWN, False, False),  # noqa: E501
    ("11-minus-one", OPEN, {"status": 0, "out": INVALID}, OpenOutcome.ANOMALY, HandleState.UNKNOWN, False, False),  # noqa: E501
    ("12-pending", CREATE, {"status": 0x103}, OpenOutcome.UNPROVEN, HandleState.UNKNOWN, False, True),  # noqa: E501
    ("12-pending-out", OPEN, {"status": 0x103, "out": "pool"}, OpenOutcome.UNPROVEN, HandleState.UNKNOWN, False, True),  # noqa: E501
    ("12-success", OPEN, {"status": 0x1, "out": "pool"}, OpenOutcome.UNPROVEN, HandleState.UNKNOWN, False, True),  # noqa: E501
    ("12-information", CREATE, {"status": 0x4000_0000}, OpenOutcome.UNPROVEN, HandleState.UNKNOWN, False, True),  # noqa: E501
    ("12-warning", CREATE, {"status": 0x8000_0005, "out": "pool"}, OpenOutcome.UNPROVEN, HandleState.UNKNOWN, False, True),  # noqa: E501
    # F-01: the IOSB never stands in for the status
    ("F01-status-wins", CREATE, {"status": 0xC000_0035, "iosb_status": 0, "information": 2}, OpenOutcome.NAME_TAKEN, HandleState.NOT_OPENED, False, False),  # noqa: E501
    ("F01-iosb-error", CREATE, {"status": 0, "iosb_status": 0xC000_0035, "information": 2}, OpenOutcome.OPENED_OWNED, HandleState.OPEN, True, False),  # noqa: E501
]  # fmt: skip


@pytest.mark.parametrize(
    ("disposition", "step", "outcome", "state", "unreliable", "pin"),
    [row[1:] for row in ROWS],
    ids=[row[0] for row in ROWS],
)
def test_t49_rows(
    nt: tuple[FakeNt, Acquisitions, w.Handle],
    disposition: int,
    step: dict[str, object],
    outcome: OpenOutcome,
    state: HandleState,
    unreliable: bool,
    pin: bool,
) -> None:
    fake, stand, parent = nt
    fake.ntdll.script["NtCreateFile"] = [step]
    role = "H_TMP" if disposition == CREATE else "H_SRC"
    handle = w.Handle.new(parent.path / "f", role)
    opener = p.create_into if disposition == CREATE else p.open_into
    assert opener(handle, parent, "f") is outcome
    assert handle.state is state and handle.unreliable is unreliable
    assert pinned(handle) is pin
    assert handle.classify(disposition) is outcome  # idempotent
    acqs = stand.of(handle)
    assert len(acqs) == (1 if state is HandleState.OPEN else 0)  # _bind only in 3, 4, 7, 8
    if outcome is not OpenOutcome.CREATED_OWNED and state is HandleState.OPEN:
        with pytest.raises(AssertionError):  # CL only in row 3
            p.dispose(handle, p.Disposal())
    calls = len(stand.calls)
    if state is HandleState.UNKNOWN:
        with pytest.raises(w.HandleUnknownError):
            handle.close()
        assert len(stand.calls) == calls
    else:
        handle.close()
        assert len(stand.calls) == calls + (1 if acqs else 0)
        if acqs:
            assert acqs[0].closes == 1


def test_open_arguments_and_sentinels(nt: tuple[FakeNt, Acquisitions, w.Handle]) -> None:
    fake, _, parent = nt
    seen: list[tuple[object, ...]] = []
    handles: list[w.Handle] = []

    def hook(details: dict[str, object]) -> None:
        handle = handles[-1]
        seen.append((handle.state, details["iosb"].Information, details["out"].value))  # type: ignore[attr-defined]

    fake.ntdll.hooks["NtCreateFile"] = hook
    for role, name in (("H_TMP", "t.tmp"), ("H_SRC", "s"), ("H_PROBE", "s"), ("H_LOOKUP", "")):
        handles.append(w.Handle.new(parent.path / name, role))
        opener = p.create_into if role == "H_TMP" else p.open_into
        opener(handles[-1], parent, name)
        handles[-1].close()
    assert seen == [(HandleState.CALLING, 0xFFFF_FFFF, None)] * 4
    calls = fake.ntdll.calls("NtCreateFile")
    sync = 0x20
    tmp, src, probe, lookup = calls
    assert (tmp["access"], tmp["share"], tmp["disposition"]) == (0x0011_0183, 0, CREATE)
    assert tmp["attributes"] == 0x80 and tmp["options"] == 0x0020_0060 and tmp["name"] == "t.tmp"
    assert (src["access"], src["share"], src["disposition"]) == (0x0012_0081, 1, OPEN)
    assert (probe["access"], probe["share"]) == (0x0010_0080, 7)
    assert lookup["options"] == 0x0020_0021 and lookup["name"] == "" and lookup["share"] == 7
    assert all(call["options"] & sync and call["root"] == parent.raw for call in calls)


def test_open_preconditions(
    nt: tuple[FakeNt, Acquisitions, w.Handle], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, _, parent = nt
    with pytest.raises(AssertionError):
        p.create_into(w.Handle.new(parent.path, "H_SRC"), parent, "x")
    with pytest.raises(AssertionError):
        p.open_into(w.Handle.new(parent.path, "H_TMP"), parent, "x")
    with pytest.raises(ValueError, match="single path component"):
        p.open_into(w.Handle.new(parent.path, "H_SRC"), parent, "")
    handle = created(parent)
    with pytest.raises(AssertionError):  # not NEW: one call per owner
        p.create_into(handle, parent, "t.tmp")
    handle.close()
    async_tmp = p.Profile(0x0011_0183, 0, CREATE, 0x0020_0040, 0x80)  # no SYNC_IO_NONALERT
    monkeypatch.setitem(p.PROFILES, "H_TMP", async_tmp)
    other = w.Handle.new(parent.path / "u", "H_TMP")
    with pytest.raises(AssertionError, match="SYNCHRONOUS"):
        p.create_into(other, parent, "u")
    assert other.state is HandleState.NEW and len(fake.ntdll.calls("NtCreateFile")) == 1
    with pytest.raises(AssertionError, match="role"):
        w.Handle.new(parent.path, "H_OTHER")


def test_w24_fake_pool_reuse_is_counted_per_acquisition(
    nt: tuple[FakeNt, Acquisitions, w.Handle],
) -> None:
    _, stand, parent = nt
    first = w.Handle.new(parent.path / "s", "H_PROBE")
    p.open_into(first, parent, "s")
    raw = first.raw
    first.close()
    second = w.Handle.new(parent.path / "s", "H_PROBE")
    p.open_into(second, parent, "s")
    assert second.raw == raw  # the pool handed the value out again
    second.close()
    assert [a.closes for a in stand.of(first) + stand.of(second)] == [1, 1]
    assert stand.violations == []


def test_w23_fake_close_failure_of_h_tmp(nt: tuple[FakeNt, Acquisitions, w.Handle]) -> None:
    _, stand, parent = nt
    stand.select = lambda owner: getattr(owner, "role", None) == "H_TMP"
    handle = created(parent)
    raw = handle.raw
    with pytest.raises(w.HandleCloseError) as first:
        handle.close()
    with pytest.raises(w.HandleCloseError) as second:
        handle.close()
    assert first.value is second.value and first.value.winerror == 31
    assert handle.state is HandleState.CLOSE_FAILED and stand.calls.count(raw) == 1
    assert raw in stand.live  # stays live until the stand's teardown closes it directly


# --- rename_once (§3.5) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "result", "pin"),
    [
        (0, p.Renamed.OK, False),
        (0xC000_0035, p.Renamed.EXISTS, False),
        (0xC000_0022, p.Renamed.ERROR, False),
        (0xC000_00D4, p.Renamed.ERROR, False),  # STATUS_NOT_SAME_DEVICE
        (0x103, p.Renamed.NOT_ESTABLISHED, True),
        (0x1, p.Renamed.NOT_ESTABLISHED, True),
        (0x4000_0000, p.Renamed.NOT_ESTABLISHED, True),
        (0x8000_0005, p.Renamed.NOT_ESTABLISHED, True),
    ],
)
def test_rename_once_classes(
    nt: tuple[FakeNt, Acquisitions, w.Handle], status: int, result: p.Renamed, pin: bool
) -> None:
    fake, _, parent = nt
    handle = created(parent)
    attempt = p.RenameAttempt()
    spent_at_call: list[bool] = []
    fake.ntdll.hooks["NtSetInformationFile"] = lambda d: spent_at_call.append(attempt.spent)
    fake.ntdll.script["NtSetInformationFile"] = [status]
    assert p.rename_once(handle, parent, "fïnal.txt", attempt) is result
    assert attempt.result is result and attempt.status == status and spent_at_call == [True]
    assert pinned(attempt.call) is pin
    (call,) = fake.ntdll.calls("NtSetInformationFile")
    name = "fïnal.txt".encode("utf-16-le")
    data = call["data"]
    assert call["info_class"] == 10 and call["raw"] == handle.raw
    assert data[:4] == b"\0\0\0\0"  # ReplaceIfExists = FALSE
    assert int.from_bytes(data[8:16], "little") == parent.raw
    assert int.from_bytes(data[16:20], "little") == len(name) and data[20:] == name
    assert call["length"] == 20 + len(name)
    with pytest.raises(AssertionError, match="spent"):
        p.rename_once(handle, parent, "other.txt", attempt)
    assert len(fake.ntdll.calls("NtSetInformationFile")) == 1
    handle.close()


def test_rename_once_lost(nt: tuple[FakeNt, Acquisitions, w.Handle]) -> None:
    fake, _, parent = nt
    handle = created(parent)
    attempt = p.RenameAttempt()
    fake.ntdll.script["NtSetInformationFile"] = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        p.rename_once(handle, parent, "final.txt", attempt)
    assert attempt.spent and attempt.result is p.Renamed.LOST and attempt.status is None
    assert pinned(attempt.call)
    handle.close()


def test_rename_once_preconditions(nt: tuple[FakeNt, Acquisitions, w.Handle]) -> None:
    fake, _, parent = nt
    attempt = p.RenameAttempt()
    source = w.Handle.new(parent.path / "s", "H_SRC")
    p.open_into(source, parent, "s")
    with pytest.raises(AssertionError):
        p.rename_once(source, parent, "x", attempt)
    source.close()
    handle = created(parent)
    with pytest.raises(ValueError, match="single path component"):
        p.rename_once(handle, parent, "a\\b", attempt)
    assert not attempt.spent and attempt.result is None
    assert fake.ntdll.calls("NtSetInformationFile") == []
    handle.close()


# --- dispose (§3.6, CL-1) ---------------------------------------------------------------

POSIX_DATA = (3).to_bytes(4, "little")  # DELETE | POSIX_SEMANTICS, no IGNORE_READONLY


@pytest.mark.parametrize(
    ("statuses", "result", "classes"),
    [
        ([0], p.Disposed.OK, [64]),
        ([0xC000_0003, 0], p.Disposed.OK, [64, 13]),
        ([0xC000_0002, 0], p.Disposed.OK, [64, 13]),
        ([0xC000_00BB, 0xC000_0121], p.Disposed.REFUSED, [64, 13]),
        ([0xC000_00BB, 0x103], p.Disposed.UNSURE, [64, 13]),
        ([0xC000_0121], p.Disposed.REFUSED, [64]),
        ([0xC000_0022], p.Disposed.REFUSED, [64]),
        ([0xC000_0043], p.Disposed.REFUSED, [64]),
        ([0xC000_000D], p.Disposed.REFUSED, [64]),
        ([0xC000_0034], p.Disposed.REFUSED, [64]),
        ([0x103], p.Disposed.UNSURE, [64]),
        ([0x8000_0005], p.Disposed.UNSURE, [64]),
    ],
)
def test_dispose_fallback_only_after_listed_refusals(
    nt: tuple[FakeNt, Acquisitions, w.Handle],
    statuses: list[int],
    result: p.Disposed,
    classes: list[int],
) -> None:
    fake, _, parent = nt
    handle = created(parent)
    disposal = p.Disposal()
    assert disposal.result is p.Disposed.NONE
    fake.ntdll.script["NtSetInformationFile"] = list(statuses)
    assert p.dispose(handle, disposal) is result
    calls = fake.ntdll.calls("NtSetInformationFile")
    assert [c["info_class"] for c in calls] == classes
    assert calls[0]["data"] == POSIX_DATA
    if len(calls) == 2:
        assert calls[1]["data"] == b"\x01"
    assert disposal.posix_spent and disposal.classic_spent is (len(calls) == 2)
    assert disposal.statuses == (statuses[0], statuses[1] if len(calls) == 2 else None)
    with pytest.raises(AssertionError, match="spent"):
        p.dispose(handle, disposal)
    assert len(fake.ntdll.calls("NtSetInformationFile")) == len(classes)
    handle.close()


@pytest.mark.parametrize("statuses", [[KeyboardInterrupt()], [0xC000_0003, KeyboardInterrupt()]])
def test_dispose_interrupted_is_unsure(
    nt: tuple[FakeNt, Acquisitions, w.Handle], statuses: list[object]
) -> None:
    fake, _, parent = nt
    handle = created(parent)
    disposal = p.Disposal()
    fake.ntdll.script["NtSetInformationFile"] = list(statuses)
    with pytest.raises(KeyboardInterrupt):
        p.dispose(handle, disposal)
    assert disposal.result is p.Disposed.UNSURE and disposal.posix_spent
    assert disposal.classic_spent is (len(statuses) == 2)
    lost = disposal.classic if disposal.classic_spent else disposal.posix
    assert pinned(lost)
    handle.close()


def test_dispose_classic_only_and_preconditions(nt: tuple[FakeNt, Acquisitions, w.Handle]) -> None:
    fake, _, parent = nt
    handle = created(parent)
    disposal = p.Disposal()
    assert p.dispose(handle, disposal, posix=False) is p.Disposed.OK
    assert not disposal.posix_spent and disposal.classic_spent
    assert [c["info_class"] for c in fake.ntdll.calls("NtSetInformationFile")] == [13]
    handle.close()
    with pytest.raises(AssertionError):  # closed
        p.dispose(handle, p.Disposal())
    fake.ntdll.script["NtCreateFile"] = [{"information": 1}]  # row 4: not CREATED_OWNED
    other = w.Handle.new(parent.path / "u", "H_TMP")
    assert p.create_into(other, parent, "u") is OpenOutcome.OPENED_OWNED
    with pytest.raises(AssertionError):
        p.dispose(other, p.Disposal())
    other.close()
    assert len(fake.ntdll.calls("NtSetInformationFile")) == 1


# --- I/O and queries -------------------------------------------------------------------


def grew(before: int) -> bool:
    return len(w._UNRESOLVED) == before + 1 and isinstance(w._UNRESOLVED[-1], p._Call)


def test_read_at(nt: tuple[FakeNt, Acquisitions, w.Handle]) -> None:
    fake, _, parent = nt
    handle = created(parent)
    fake.ntdll.script["NtReadFile"] = [
        {"data": b"abc"},
        0xC000_0011,
        {"data": b"abc", "information": 9},
        0xC000_0022,
    ]
    assert p.read_at(handle, 7, 8) == b"abc"
    assert fake.ntdll.calls("NtReadFile")[0]["offset"] == 7
    assert p.read_at(handle, 100, 8) == b""
    with pytest.raises(p.MalformedInfoError):
        p.read_at(handle, 0, 8)
    with pytest.raises(p.StatusError) as error:
        p.read_at(handle, 0, 8)
    assert error.value.ntstatus == 0xC000_0022 and error.value.winerror == 5
    before = len(w._UNRESOLVED)
    fake.ntdll.script["NtReadFile"] = [0x103]
    with pytest.raises(p.UnsupportedStatusError):
        p.read_at(handle, 0, 8)
    assert grew(before)
    before = len(w._UNRESOLVED)
    fake.ntdll.script["NtReadFile"] = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        p.read_at(handle, 0, 8)
    assert grew(before)
    with pytest.raises(AssertionError):
        p.read_at(handle, -1, 8)
    handle.close()


def test_write_at_and_flush(nt: tuple[FakeNt, Acquisitions, w.Handle]) -> None:
    fake, _, parent = nt
    handle = created(parent)
    p.write_at(handle, 4096, b"hello")
    (call,) = fake.ntdll.calls("NtWriteFile")
    assert call["data"] == b"hello" and call["offset"] == 4096
    fake.ntdll.script["NtWriteFile"] = [{"information": 2}, 0xC000_007F]
    with pytest.raises(OSError, match="short write"):
        p.write_at(handle, 0, b"hello")
    with pytest.raises(p.StatusError) as error:
        p.write_at(handle, 0, b"hello")
    assert error.value.ntstatus == 0xC000_007F  # STATUS_DISK_FULL
    with pytest.raises(AssertionError):
        p.write_at(handle, 0, b"")
    p.flush(handle)
    fake.ntdll.script["NtFlushBuffersFile"] = [0xC000_0185]
    with pytest.raises(p.StatusError):
        p.flush(handle)
    handle.close()


def stream_entry(name: str, size: int, following: int = 0) -> bytes:
    raw = name.encode("utf-16-le")
    head = following.to_bytes(4, "little") + len(raw).to_bytes(4, "little")
    head += size.to_bytes(8, "little") + size.to_bytes(8, "little")
    data = head + raw
    return data + b"\0" * (-len(data) % 8)


def test_query_streams(nt: tuple[FakeNt, Acquisitions, w.Handle]) -> None:
    fake, _, parent = nt
    handle = created(parent)
    first = stream_entry("::$DATA", 15, following=len(stream_entry("::$DATA", 15)))
    good = first + stream_entry(":Zone.Identifier:$DATA", 26)
    fake.ntdll.script["NtQueryInformationFile"] = [0x8000_0005, {"data": good}]
    assert p.query_streams(handle) == (
        p.Stream("::$DATA", 15),
        p.Stream(":Zone.Identifier:$DATA", 26),
    )
    lengths = [c["length"] for c in fake.ntdll.calls("NtQueryInformationFile")]
    assert lengths == [4096, 16384]  # the buffer grew after BUFFER_OVERFLOW
    bad = [
        stream_entry("::$DATA", 1, following=12) + b"\0" * 32,  # not 8-aligned
        stream_entry("::$DATA", 1, following=8) + b"\0" * 32,  # inside the entry
        stream_entry("::$DATA", 1, following=4096),  # beyond the data
        b"\0" * 4 + (3).to_bytes(4, "little") + b"\0" * 24,  # odd name length
        b"\0" * 4 + (400).to_bytes(4, "little") + b"\0" * 24,  # beyond the data
    ]
    for data in bad:
        fake.ntdll.script["NtQueryInformationFile"] = [{"data": data}]
        with pytest.raises(p.MalformedInfoError):
            p.query_streams(handle)
    fake.ntdll.script["NtQueryInformationFile"] = [{"data": b"\0" * 8, "information": 9000}]
    with pytest.raises(p.MalformedInfoError):
        p.query_streams(handle)
    handle.close()


def test_query_name_and_ea(nt: tuple[FakeNt, Acquisitions, w.Handle]) -> None:
    fake, _, parent = nt
    handle = created(parent)
    name = "\\dir\\fïle😀.txt".encode("utf-16-le", "surrogatepass")
    fake.ntdll.script["NtQueryInformationFile"] = [
        {"data": len(name).to_bytes(4, "little") + name},
        {"data": (len(name) + 2).to_bytes(4, "little") + name},
        {"data": (3).to_bytes(4, "little") + name},
        {"data": (12).to_bytes(4, "little")},
    ]
    assert p.query_name(handle) == "\\dir\\fïle😀.txt"
    assert p.last_component(p.query_name.__doc__ and "\\dir\\a.txt") == "a.txt"
    for _ in range(2):
        with pytest.raises(p.MalformedInfoError):
            p.query_name(handle)
    assert p.query_ea_size(handle) == 12
    handle.close()


# --- R3B3-M01: a fixed-size reply is taken only at exactly its size ------------------------

BASIC = (1, 2, 3, 4, 0x20)  # creation, last access, last write, change, attributes


def basic_bytes() -> bytes:
    return bytes(p._prims().base.BasicInfo(*BASIC))


def full_size_bytes() -> bytes:
    return bytes(p._prims().FullSizeInfo(1000, 400, 500, 8, 512))


FIXED = {
    # name: (query, API, info class, a correct reply, what it gives)
    "ea": (p.query_ea_size, "NtQueryInformationFile", 7, (12).to_bytes(4, "little"), 12),
    "times": (p.query_times, "NtQueryInformationFile", 4, None, p.Times(*BASIC)),
    "space": (
        p.query_space, "NtQueryVolumeInformationFile", 7, None, p.Space(400 * 4096, 1000 * 4096)
    ),
}  # fmt: skip


@pytest.mark.parametrize("which", list(FIXED))
def test_m01_fixed_reply_must_be_complete(
    nt: tuple[FakeNt, Acquisitions, w.Handle], which: str
) -> None:
    fake, _, parent = nt
    handle = created(parent)
    query, api, info_class, good, value = FIXED[which]
    good = good or (basic_bytes() if which == "times" else full_size_bytes())
    size = len(good)
    assert size == {"ea": 4, "times": 40, "space": 32}[which]
    fake.ntdll.script[api] = [
        {"data": good},  # the correct reply
        {"data": good, "information": 0},  # Information=0: nothing was written
        {},  # status 0, no data, Information=0 (the zeroed IOSB)
        {"data": good[:2], "information": 2},  # a truncated reply
        {"data": good[: size - 4], "information": size - 4},
        {"data": good, "information": size + 4},  # more than the buffer
        0xC000_0022,  # an error status
    ]
    assert query(handle) == value
    (first, *_) = fake.ntdll.calls(api)
    assert first["info_class"] == info_class and first["length"] == size
    for _ in range(5):
        with pytest.raises(p.MalformedInfoError):
            query(handle)
    with pytest.raises(p.StatusError) as error:
        query(handle)
    assert error.value.ntstatus == 0xC000_0022
    before = len(w._UNRESOLVED)
    fake.ntdll.script[api] = [0x103]
    with pytest.raises(p.UnsupportedStatusError):
        query(handle)
    assert grew(before)
    handle.close()


def test_m01_ea_zero_only_from_a_complete_reply(
    nt: tuple[FakeNt, Acquisitions, w.Handle],
) -> None:
    """``EaSize == 0`` ("no EA") is believed only from a 4-byte reply."""
    fake, _, parent = nt
    handle = created(parent)
    fake.ntdll.script["NtQueryInformationFile"] = [
        {"data": (0).to_bytes(4, "little")},
        {"data": b"", "information": 0},
        {"data": (0).to_bytes(4, "little"), "information": 3},
    ]
    assert p.query_ea_size(handle) == 0
    for _ in range(2):
        with pytest.raises(p.MalformedInfoError):
            p.query_ea_size(handle)
    handle.close()


def test_m01_empty_stream_reply_is_malformed(nt: tuple[FakeNt, Acquisitions, w.Handle]) -> None:
    """An empty ``FileStreamInformation`` is not "no streams": it proves nothing."""
    fake, _, parent = nt
    handle = created(parent)
    fake.ntdll.script["NtQueryInformationFile"] = [{}, {"data": bytes(8), "information": 0}]
    for _ in range(2):
        with pytest.raises(p.MalformedInfoError):
            p.query_streams(handle)
    handle.close()


# --- R3B3-L01: offsets and sizes are checked before ctypes -------------------------------

MAX = 2**63 - 1
BAD_RANGES = [
    (-1, 1),
    (-(2**63), 1),
    (2**63, 1),
    (2**64, 1),
    (2**64 + 7, 1),
    (MAX, 2),  # the range crosses 2**63
    (MAX - 2, 4),
    (True, 1),
    (False, 1),
    (0, True),
    (1.0, 1),
    (0, 0),
    (0, -1),
    (0, 2**32),
    (0, 1.0),
]


@pytest.mark.parametrize(("offset", "size"), BAD_RANGES, ids=repr)
def test_l01_read_at_rejects_before_the_api(
    nt: tuple[FakeNt, Acquisitions, w.Handle], offset: object, size: object
) -> None:
    fake, _, parent = nt
    handle = created(parent)
    fake.ntdll.log.clear()
    with pytest.raises(AssertionError):
        p.read_at(handle, offset, size)  # type: ignore[arg-type]
    assert fake.ntdll.calls("NtReadFile") == []
    handle.close()


@pytest.mark.parametrize(
    ("offset", "size"), [(o, s) for o, s in BAD_RANGES if type(s) is int and 0 < s <= 8], ids=repr
)
def test_l01_write_at_rejects_before_the_api(
    nt: tuple[FakeNt, Acquisitions, w.Handle], offset: object, size: int
) -> None:
    fake, _, parent = nt
    handle = created(parent)
    with pytest.raises(AssertionError):
        p.write_at(handle, offset, b"x" * size)  # type: ignore[arg-type]
    assert fake.ntdll.calls("NtWriteFile") == []
    handle.close()


def test_l01_write_at_rejects_bad_data(nt: tuple[FakeNt, Acquisitions, w.Handle]) -> None:
    fake, _, parent = nt
    handle = created(parent)
    for data in (b"", "text", None, 5):
        with pytest.raises((AssertionError, TypeError)):
            p.write_at(handle, 0, data)  # type: ignore[arg-type]
    assert fake.ntdll.calls("NtWriteFile") == []
    handle.close()


@pytest.mark.parametrize(("offset", "size"), [(MAX, 1), (MAX - 3, 4), (0, 0xFFFF_FFFF)], ids=repr)
def test_l01_largest_ranges_reach_the_api_unchanged(
    nt: tuple[FakeNt, Acquisitions, w.Handle], offset: int, size: int
) -> None:
    fake, _, parent = nt
    handle = created(parent)
    fake.ntdll.script["NtReadFile"] = [0xC000_0011]  # END_OF_FILE
    assert p.read_at(handle, offset, min(size, 16)) == b""
    if size <= 16:
        p.write_at(handle, offset, b"y" * size)
        (call,) = fake.ntdll.calls("NtWriteFile")
        assert call["offset"] == offset  # no wrap in c_int64
    (read,) = fake.ntdll.calls("NtReadFile")
    assert read["offset"] == offset
    handle.close()


# --- T54: lookup_entry on fake buffers ----------------------------------------------------


def dir_entry(
    name: str | bytes,
    *,
    file_id: int = 0x1234,
    attributes: int = 0x20,
    tag: int = 0,
    name_length: int | None = None,
    following: int = 0,
) -> bytes:
    struct = p.DirInfo()
    raw = name if isinstance(name, bytes) else name.encode("utf-16-le", "surrogatepass")
    struct.NextEntryOffset = following
    struct.FileAttributes = attributes
    struct.FileNameLength = len(raw) if name_length is None else name_length
    struct.ReparsePointTag = tag
    ctypes.memmove(struct.FileId, file_id.to_bytes(16, "little"), 16)
    data = bytes(struct)[:HDR] + raw
    return data + b"\0" * (-len(data) % 8)


def chain(*entries: bytes) -> bytes:
    """Entries linked with NextEntryOffset (the last one 0)."""
    out = b""
    for index, entry in enumerate(entries):
        following = len(entry) if index < len(entries) - 1 else 0
        out += following.to_bytes(4, "little") + entry[4:]
    return out


NAME = "Fïle.txt"
FOUND_ID = 0x0005_0000_0000_0042


@pytest.fixture
def same_directory(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """``winfileops.query`` of the fake handles: by default every handle is the same
    directory (file id ``ids["lookup"]`` for H_LOOKUP, ``ids["directory"]`` else)."""
    ids = {"lookup": 0x77, "directory": 0x77}

    def query(handle: w.Handle) -> w.HandleInfo:
        file_id = ids["lookup"] if handle.role == "H_LOOKUP" else ids["directory"]
        return w.HandleInfo(9, file_id, 0x10, 0, 0, 0, 1, False, True)

    monkeypatch.setattr(w, "query", query)
    return ids


LISTINGS = [
    # id, step, kind, reason fragment
    ("found", {"data": dir_entry(NAME, file_id=FOUND_ID)}, "found", ""),
    ("no-such-file", 0xC000_000F, "absent", "absent"),
    ("no-more-files", 0x8000_0006, "absent", "absent"),
    ("buffer-overflow", {"status": 0x8000_0005, "data": dir_entry(NAME)}, "unknown", "0x80000005"),
    ("pending", {"status": 0x103, "data": dir_entry(NAME)}, "unknown", "0x00000103"),
    ("access-denied", 0xC000_0022, "unknown", "0xc0000022"),
    ("other-case", {"data": dir_entry(NAME.upper())}, "unknown", "another name"),
    ("short-name", {"data": dir_entry("FLE~1.TXT")}, "unknown", "another name"),
    ("two-entries", {"data": chain(dir_entry(NAME), dir_entry("x.txt"))}, "unknown", "2 entries"),
    ("exact-twice", {"data": chain(dir_entry(NAME), dir_entry(NAME))}, "unknown", "2 entries"),
    ("directory", {"data": dir_entry(NAME, attributes=0x10)}, "unknown", "attributes"),
    ("reparse-attribute", {"data": dir_entry(NAME, attributes=0x420)}, "unknown", "attributes"),
    ("reparse-tag", {"data": dir_entry(NAME, tag=0xA000_000C)}, "unknown", "reparse tag"),
    ("name-length-0", {"data": dir_entry(NAME, name_length=0)}, "unknown", "name length"),
    ("name-length-odd", {"data": dir_entry(NAME, name_length=15)}, "unknown", "name length"),
    ("name-past-information", {"data": dir_entry(NAME), "information": HDR + 4}, "unknown", "name length"),  # noqa: E501
    ("information-too-small", {"data": dir_entry(NAME), "information": HDR + 1}, "unknown", "information"),  # noqa: E501
    ("information-too-large", {"data": dir_entry(NAME), "information": 65537}, "unknown", "information"),  # noqa: E501
    ("next-unaligned", {"data": dir_entry(NAME, following=108) + b"\0" * 200}, "unknown", "next entry"),  # noqa: E501
    ("next-inside-entry", {"data": dir_entry(NAME, following=96) + b"\0" * 200}, "unknown", "next entry"),  # noqa: E501
    ("next-past-information", {"data": dir_entry(NAME, following=112)}, "unknown", "next entry"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("step", "kind", "reason"), [row[1:] for row in LISTINGS], ids=[row[0] for row in LISTINGS]
)
def test_t54_lookup_entry_buffers(
    nt: tuple[FakeNt, Acquisitions, w.Handle],
    same_directory: dict[str, int],
    step: object,
    kind: str,
    reason: str,
) -> None:
    fake, stand, parent = nt
    fake.ntdll.script["NtQueryDirectoryFile"] = [step]
    before = len(w._UNRESOLVED)
    handle = w.Handle.new(parent.path, "H_LOOKUP")
    result = p.lookup_entry(handle, parent, NAME)
    assert result.kind == kind and reason in result.reason
    assert result.file_id == (FOUND_ID if kind == "found" else None)
    (query,) = fake.ntdll.calls("NtQueryDirectoryFile")
    assert query["info_class"] == 60 and query["single"] is False and query["restart"] is True
    assert query["pattern"] == NAME and query["length"] == 65536 and query["event"] is None
    (open_call,) = fake.ntdll.calls("NtCreateFile")
    assert open_call["name"] == "" and open_call["options"] & 0x1  # the directory itself
    (acq,) = stand.of(handle)
    assert acq.closes == 1 and handle.state is HandleState.CLOSED
    status = step if isinstance(step, int) else step.get("status", 0)  # type: ignore[union-attr]
    nonstandard = p.status_class(status) == "nonstandard"  # warnings too (fail-closed)
    assert len(w._UNRESOLVED) == before + (1 if nonstandard else 0)


@pytest.mark.parametrize(
    "name",
    ["", "a*", "?", "a<b", "a>b", 'a"b', "a\\b", "a/b", "a:b", "a\0b", "x" * 256, "😀" * 128],
)
def test_t54_lookup_name_rejected_before_any_call(
    nt: tuple[FakeNt, Acquisitions, w.Handle], name: str
) -> None:
    fake, stand, parent = nt
    handle = w.Handle.new(parent.path, "H_LOOKUP")
    result = p.lookup_entry(handle, parent, name)
    assert result.kind == "unknown" and fake.ntdll.log == []
    assert handle.state is HandleState.NOT_OPENED and stand.of(handle) == []


def test_t54_lookup_longest_name_is_queried(
    nt: tuple[FakeNt, Acquisitions, w.Handle], same_directory: dict[str, int]
) -> None:
    fake, _, parent = nt
    name = "x" * 253 + "😀"  # 255 UTF-16 units
    fake.ntdll.script["NtQueryDirectoryFile"] = [{"data": dir_entry(name, file_id=5)}]
    assert p.lookup_entry(w.Handle.new(parent.path, "H_LOOKUP"), parent, name).file_id == 5


def test_t54_lookup_other_directory_is_unknown(
    nt: tuple[FakeNt, Acquisitions, w.Handle], same_directory: dict[str, int]
) -> None:
    fake, stand, parent = nt
    same_directory["lookup"] = 0x78
    handle = w.Handle.new(parent.path, "H_LOOKUP")
    result = p.lookup_entry(handle, parent, NAME)
    assert result.kind == "unknown" and "another directory" in result.reason
    assert fake.ntdll.calls("NtQueryDirectoryFile") == []
    assert stand.of(handle)[0].closes == 1


def test_t54_lookup_unknown_h_lookup_is_not_closed(
    nt: tuple[FakeNt, Acquisitions, w.Handle], same_directory: dict[str, int]
) -> None:
    fake, stand, parent = nt
    fake.ntdll.script["NtCreateFile"] = [{"status": 0x103, "out": "pool"}]
    handle = w.Handle.new(parent.path, "H_LOOKUP")
    result = p.lookup_entry(handle, parent, NAME)
    assert result.kind == "unknown" and "unproven" in result.reason
    assert handle.state is HandleState.UNKNOWN and pinned(handle)
    assert stand.of(handle) == [] and stand.calls == []
    assert fake.ntdll.calls("NtQueryDirectoryFile") == []


def test_t54_layout_mismatch_is_always_unknown(
    nt: tuple[FakeNt, Acquisitions, w.Handle], same_directory: dict[str, int]
) -> None:
    fake, _, parent = nt

    class Shifted(ctypes.Structure):  # FileName at 90 instead of 88
        _fields_ = [(n, t) for n, t in p.DirInfo._fields_ if n not in ("FileId", "FileName")]
        _fields_ += [("FileId", ctypes.c_ubyte * 18), ("FileName", ctypes.c_wchar * 1)]

    assert Shifted.FileName.offset == 90 and not p._check_layout(Shifted)
    assert p._check_layout(p.DirInfo)
    fake.prims.DirInfo = Shifted
    fake.ntdll.script["NtQueryDirectoryFile"] = [{"data": dir_entry(NAME)}] * 2
    for _ in range(2):
        handle = w.Handle.new(parent.path, "H_LOOKUP")
        result = p.lookup_entry(handle, parent, NAME)
        assert result.kind == "unknown" and result.reason == "layout_mismatch"
    assert fake.ntdll.calls("NtQueryDirectoryFile") == []


# --- T63 (primitive level, F) and T67 (the stand itself) -------------------------------


def test_t63_one_owner_per_open(
    nt: tuple[FakeNt, Acquisitions, w.Handle], same_directory: dict[str, int]
) -> None:
    fake, stand, parent = nt
    news, inits = stand.news, stand.inits
    fake.ntdll.script["NtQueryDirectoryFile"] = [{"data": dir_entry("final.txt")}]
    source = w.Handle.new(parent.path / "s", "H_SRC")
    p.open_into(source, parent, "s")
    tmp = created(parent)
    p.write_at(tmp, 0, b"data")
    p.flush(tmp)
    p.set_mtime(tmp, 1_600_000_000_000_000_000)
    assert p.rename_once(tmp, parent, "final.txt", p.RenameAttempt()) is p.Renamed.OK
    lookup = w.Handle.new(parent.path, "H_LOOKUP")
    assert p.lookup_entry(lookup, parent, "final.txt").kind == "found"
    tmp.close()
    source.close()
    assert stand.news == news + 3 and stand.inits == inits
    for owner in (source, tmp, lookup):
        (acq,) = stand.of(owner)
        assert acq.kind == "bind" and acq.closes == 1
    assert stand.violations == []


def run_on_stand(accounting: str, teardown: str, *, fail_tmp_close: bool) -> list[str]:
    with pytest.MonkeyPatch.context() as mp:
        pool = FakePool()
        stand = Acquisitions(mp, pool=pool, accounting=accounting, teardown=teardown)
        FakeNt(mp, pool)
        parent = w.Handle(pool.take(), Path("C:/fake/dir"))
        if fail_tmp_close:
            stand.select = lambda owner: getattr(owner, "role", None) == "H_TMP"
        for _ in range(2):  # the second open gets the first one's value again
            probe = w.Handle.new(parent.path / "s", "H_PROBE")
            p.open_into(probe, parent, "s")
            probe.close()
        tmp = created(parent)
        try:
            tmp.close()
        except w.HandleCloseError:
            assert fail_tmp_close
        parent.close()
        return stand.finish()


def test_t67_stand_counts_acquisitions_not_raw_values() -> None:
    assert run_on_stand("acq", "direct", fail_tmp_close=False) == []
    problems = run_on_stand("raw", "direct", fail_tmp_close=False)  # mutant
    assert any("closed twice" in problem for problem in problems)  # a false alarm


def test_t67_stand_teardown_closes_directly() -> None:
    assert run_on_stand("acq", "direct", fail_tmp_close=True) == []
    problems = run_on_stand("acq", "handle_close", fail_tmp_close=True)  # mutant
    assert any("left open" in problem for problem in problems)  # Handle.close leaks it


def test_t67_stand_reports_misuse() -> None:
    with pytest.MonkeyPatch.context() as mp:
        pool = FakePool()
        stand = Acquisitions(mp, pool=pool)
        raw = pool.take()
        first = w.Handle(raw, Path("C:/a"))
        second = w.Handle(raw, Path("C:/b"))  # a second owner of a live value
        stand._close(pool.BASE + 400)  # no acquisition
        first.close()
        never = w.Handle(pool.take(), Path("C:/c"))
        problems = stand.finish()
    assert any("while live" in problem for problem in problems)
    assert any("without a live acquisition" in problem for problem in problems)
    assert any("never closed" in problem and "C:" in problem for problem in problems)
    assert second.state is HandleState.OPEN and never.state is HandleState.OPEN
