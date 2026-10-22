"""User settings (R4-C, FIX-01): read only on load, saved only on request, nothing lost.

Every path is below ``tmp_path``; ``LOCALAPPDATA`` is the isolated one of conftest. Races
and path swaps are made by real separate processes; failures of single steps are injected
by monkeypatching, and each test checks the SHA-256 of what was there before.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from downloads_organizer import settingsio, winfileops, winprims
from downloads_organizer.extensions import ExtensionError, ExtensionErrorReason
from downloads_organizer.i18n import Lang
from downloads_organizer.usersettings import (
    ABSENT,
    LOCK_NAME,
    MAX_DIGEST,
    MAX_SIZE,
    CustomRule,
    SaveError,
    SaveFailure,
    SettingsProblem,
    SettingsStore,
    UserSettings,
    encode,
    load_settings,
    save_settings,
    settings_path,
)

R = ExtensionErrorReason
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def path(isolated_user_dirs: Path) -> Path:
    return isolated_user_dirs / "AppData" / "Local" / "DownloadsOrganizer" / "settings.json"


def tree(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*")) if folder.exists() else []


def snapshot(folder: Path) -> dict[str, str | None]:
    """Every entry below ``folder``: the SHA-256 of a file, ``None`` for a folder."""
    return {
        str(p.relative_to(folder)): None
        if p.is_dir()
        else hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
    }


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


def document(**changes: object) -> str:
    base: dict[str, object] = {
        "schema": 1,
        "app": "DownloadsOrganizer",
        "language": "en",
        "custom_extensions": [{"extension": ".avif", "category": "Images", "enabled": True}],
    }
    return json.dumps({**base, **changes})


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def python(code: str, *args: str, **options: object) -> subprocess.Popen[str]:
    """A separate Python process with this package importable and the isolated profile."""
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **options,  # type: ignore[arg-type]
    )


def run_python(code: str, *args: str) -> str:
    process = python(code, *args)
    out, err = process.communicate(timeout=60)
    assert process.returncode == 0, err
    return out.strip()


def junction(link: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        pytest.skip(f"junction not created: {done.stderr!r}")


# --- the path ----------------------------------------------------------------------------


def test_path_is_below_localappdata(isolated_user_dirs: Path, path: Path) -> None:
    assert settings_path() == path
    assert settings_path({}) is None
    assert settings_path({"LOCALAPPDATA": "relative\\dir"}) is None
    assert path.parent.name == "DownloadsOrganizer" and path.parent.parent.name == "Local"


# --- the rules ---------------------------------------------------------------------------


def test_rules_are_validated_and_one_per_extension() -> None:
    user = UserSettings().with_rule(" AVIF ", "Images").with_rule(".epub", "Office")
    assert [r.extension for r in user.rules] == [".avif", ".epub"]
    assert user.table().category_for(".avif").key == "Images"  # type: ignore[union-attr]
    cases = [
        ("a b", "Images", R.INVALID_CHARACTERS),
        (".jpg", "Images", R.BUILTIN_DUPLICATE),
        (".avif", "Images", R.CUSTOM_DUPLICATE),
        (".avif", "Office", R.CUSTOM_DUPLICATE),  # never two categories
        (".tar.gz", "Archive", R.COMPOUND),
        (".crdownload", "Archive", R.PARTIAL_DOWNLOAD),
        (".heic", "NoSuchCategory", R.UNKNOWN_CATEGORY),
    ]
    for raw, key, reason in cases:
        with pytest.raises(ExtensionError) as info:
            user.with_rule(raw, key)
        assert info.value.reason is reason, raw


def test_a_disabled_rule_still_owns_its_extension() -> None:
    user = UserSettings().with_rule(".heic", "Images").with_enabled(".heic", False)
    assert user.table().category_for(".heic") is None
    with pytest.raises(ExtensionError) as info:
        user.with_rule(".heic", "Office")
    assert info.value.reason is R.CUSTOM_DUPLICATE
    assert user.with_enabled(".heic", True).table().category_for(".heic") is not None


def test_delete_and_restore_keep_the_builtin_set() -> None:
    user = UserSettings(Lang.EN).with_rule(".avif", "Images").with_rule(".jsonl", "Code")
    assert [r.extension for r in user.without_rule(".jsonl").rules] == [".avif"]
    with pytest.raises(ExtensionError):
        user.without_rule(".jpg")  # a built-in extension is no rule
    restored = user.with_defaults()
    assert restored.rules == () and restored.language is Lang.EN
    assert restored.table().category_for(".jpg").key == "Images"  # type: ignore[union-attr]


# --- load: read only ---------------------------------------------------------------------


def test_missing_file_gives_defaults_and_creates_nothing(
    isolated_user_dirs: Path, path: Path
) -> None:
    before = tree(isolated_user_dirs)
    result = load_settings()
    assert result.settings == UserSettings() and not result.found and result.clean
    assert result.baseline == ABSENT
    assert tree(isolated_user_dirs) == before


def test_round_trip(path: Path) -> None:
    user = UserSettings(Lang.EN).with_rule(".avif", "Images").with_rule(".epub", "Office")
    user = user.with_enabled(".epub", False)
    saved = save_settings(user)
    assert saved.path == path and saved.backup is None and saved.notes == ()
    result = load_settings()
    assert result.settings == user and result.clean
    assert result.baseline == saved.baseline and saved.baseline.digest == sha(path.read_bytes())
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["schema"] == 1 and stored["app"] == "DownloadsOrganizer"
    assert stored["language"] == "en"
    assert stored["custom_extensions"][1] == {
        "extension": ".epub",
        "category": "Office",
        "enabled": False,  # a disabled rule is kept, disabled
    }


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (b"{not json", SettingsProblem.CORRUPT),
        (b"\xff\xfe\x00garbage", SettingsProblem.CORRUPT),
        (b" " * (MAX_SIZE + 1), SettingsProblem.TOO_LARGE),
        (b'{"schema": 1, "app": "SomethingElse"}', SettingsProblem.FOREIGN),
        (b'{"schema": 2, "app": "DownloadsOrganizer"}', SettingsProblem.FOREIGN),
        (b'{"schema": true, "app": "DownloadsOrganizer"}', SettingsProblem.FOREIGN),
        (b"[]", SettingsProblem.FOREIGN),
    ],
    ids=["not_json", "not_utf8", "too_large", "other_app", "schema_2", "schema_bool", "list"],
)
def test_bad_files_give_defaults_and_are_left_alone(
    path: Path, data: bytes, problem: SettingsProblem
) -> None:
    write(path, data)
    before = snapshot(path.parent)
    result = load_settings()
    assert result.problem is problem and result.found
    assert result.settings == UserSettings()
    # what was read is the baseline, to be kept aside by a save
    assert result.baseline.read and result.baseline.keep
    assert result.baseline.digest == sha(data)
    assert snapshot(path.parent) == before


def test_a_file_too_large_to_compare_is_not_read_and_blocks_saving(path: Path) -> None:
    write(path, b" " * (MAX_DIGEST + 1))
    before = snapshot(path.parent)
    store = SettingsStore()
    result = store.load()
    assert result.problem is SettingsProblem.TOO_LARGE and not result.baseline.read
    with pytest.raises(SaveError) as info:
        store.save(UserSettings(Lang.EN))
    assert info.value.reason is SaveFailure.NOT_READ and info.value.intact
    assert snapshot(path.parent) == before


def test_deeply_nested_json_is_corrupt_not_a_crash(path: Path) -> None:
    write(path, b"[" * 50_000 + b"]" * 50_000)  # below MAX_SIZE
    assert path.stat().st_size <= MAX_SIZE
    assert load_settings().problem is SettingsProblem.CORRUPT


def test_unknown_language_keeps_the_valid_rules(path: Path) -> None:
    write(path, document(language="de"))
    result = load_settings()
    assert (
        result.problem is SettingsProblem.UNKNOWN_LANGUAGE and result.settings.language is Lang.RU
    )
    assert result.settings.rules == (CustomRule(".avif", "Images"),)
    assert result.baseline.keep  # not clean: a save keeps the file aside


def test_bad_rules_are_rejected_one_by_one(path: Path) -> None:
    rules = [
        {"extension": ".avif", "category": "Images"},
        {"extension": ".jpg", "category": "Office"},  # built-in
        {"extension": "a b", "category": "Images"},  # invalid
        {"extension": ".AVIF", "category": "Office"},  # second category of .avif
        {"extension": ".x", "category": "Images", "extra": 1},  # malformed
        {"extension": ".y", "category": "Images", "enabled": "yes"},  # malformed
        "just a string",
    ]
    write(path, document(custom_extensions=rules))
    result = load_settings()
    # an extension claimed by two categories is used for neither of them
    assert result.settings.rules == () and not result.clean
    reasons = sorted(str(r.reason) for r in result.rejected)
    assert reasons == sorted(
        [
            R.MALFORMED,
            R.MALFORMED,
            R.MALFORMED,
            R.BUILTIN_DUPLICATE,
            R.INVALID_CHARACTERS,
            R.AMBIGUOUS,
            R.AMBIGUOUS,
        ]
    )


def entries(*items: tuple[str, str, bool]) -> list[dict[str, object]]:
    return [{"extension": e, "category": c, "enabled": on} for e, c, on in items]


@pytest.mark.parametrize(
    "items",
    [
        # the regression of the audit: enabled taken from an entry that was rejected
        ((".avif", "NoSuchCategory", True), (".avif", "Images", False)),
        ((".avif", "Images", False), (".avif", "NoSuchCategory", True)),
        ((".avif", "Images", True), (".AVIF", "Images", False)),  # only `enabled` differs
        ((".avif", "Images", False), (".avif", "Office", True)),
    ],
    ids=["unknown_then_disabled", "disabled_then_unknown", "enabled_differs", "two_categories"],
)
def test_disagreeing_entries_give_no_rule_at_all(
    path: Path, items: tuple[tuple[str, str, bool], ...]
) -> None:
    write(path, document(custom_extensions=entries(*items)))
    result = load_settings()
    assert result.settings.rules == ()
    assert result.settings.table().category_for(".avif") is None  # never active
    assert [r.reason for r in result.rejected] == [R.AMBIGUOUS] * len(items)
    assert result.baseline.keep


def test_rules_that_are_kept_carry_their_own_enabled(path: Path) -> None:
    items = entries(
        (".avif", "Images", False),
        (".AVIF", "Images", False),  # exact repeat: rejected, the first kept
        (".heic", "Images", True),
        (".jpg", "Images", True),  # built-in: forbidden
        (".crdownload", "Archive", False),  # partial download: forbidden
        (".zz", "NoSuchCategory", False),  # unknown category, even disabled
    )
    write(path, document(custom_extensions=items))
    result = load_settings()
    assert result.settings.rules == (
        CustomRule(".avif", "Images", enabled=False),
        CustomRule(".heic", "Images", enabled=True),
    )
    table = result.settings.table()
    assert table.category_for(".avif") is None and table.category_for(".heic") is not None
    assert sorted(str(r.reason) for r in result.rejected) == sorted(
        [R.CUSTOM_DUPLICATE, R.BUILTIN_DUPLICATE, R.PARTIAL_DOWNLOAD, R.UNKNOWN_CATEGORY]
    )


def test_rules_that_are_not_a_list(path: Path) -> None:
    write(path, document(custom_extensions={"extension": ".avif"}))
    result = load_settings()
    assert result.settings.rules == () and result.rejected


def test_utf8_bom_is_accepted(path: Path) -> None:
    write(path, b"\xef\xbb\xbf" + document().encode("utf-8"))
    assert load_settings().settings.language is Lang.EN


def test_loading_never_writes(isolated_user_dirs: Path, path: Path) -> None:
    write(path, document())
    before = snapshot(isolated_user_dirs)
    for _ in range(3):
        SettingsStore().load()
    assert snapshot(isolated_user_dirs) == before
    assert names(path.parent) == ["settings.json"]  # no lock, no journal, no backup


# --- links and non-regular files ---------------------------------------------------------


@pytest.mark.windows_only
def test_a_junction_as_the_folder_is_neither_read_nor_written(path: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    junction(path.parent, elsewhere)
    (elsewhere / "settings.json").write_text(document(), encoding="utf-8")
    before = snapshot(elsewhere)
    result = load_settings()
    assert result.problem is SettingsProblem.LINK and result.settings == UserSettings()
    with pytest.raises(SaveError) as info:
        save_settings(UserSettings(Lang.RU))
    assert info.value.reason is SaveFailure.LINK and info.value.intact
    assert snapshot(elsewhere) == before


@pytest.mark.windows_only
def test_a_junction_higher_up_is_refused(
    isolated_user_dirs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOCALAPPDATA itself reached through a junction: not followed, nothing written."""
    real = isolated_user_dirs / "AppData" / "Local"
    write(real / "DownloadsOrganizer" / "settings.json", document())
    before = snapshot(real)
    link = tmp_path / "linked_local"
    junction(link, real)
    monkeypatch.setenv("LOCALAPPDATA", str(link))
    assert load_settings().problem is SettingsProblem.LINK
    with pytest.raises(SaveError) as info:
        save_settings(UserSettings(Lang.RU))
    assert info.value.reason is SaveFailure.LINK
    assert snapshot(real) == before


def test_a_folder_named_settings_json_is_refused(path: Path) -> None:
    path.mkdir(parents=True)
    store = SettingsStore()
    assert store.load().problem is SettingsProblem.LINK
    with pytest.raises(SaveError) as info:
        store.save(UserSettings())
    assert info.value.reason is SaveFailure.NOT_READ  # never read: never replaced
    with pytest.raises(SaveError) as info:
        save_settings(UserSettings())  # even without the load
    assert info.value.reason is SaveFailure.LINK
    assert path.is_dir() and names(path.parent) == ["settings.json", LOCK_NAME]


@pytest.mark.windows_only
def test_a_hard_linked_settings_file_is_refused(path: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    other.write_text(document(), encoding="utf-8")
    path.parent.mkdir(parents=True)
    os.link(other, path)
    assert load_settings().problem is SettingsProblem.LINK
    with pytest.raises(SaveError) as info:
        save_settings(UserSettings(Lang.RU))
    assert info.value.reason is SaveFailure.LINK
    assert other.read_text(encoding="utf-8") == document()


# --- path swaps by another process while a save holds the folder -------------------------

ATTACK = r"""
import os, subprocess, sys
victim, aside, target = sys.argv[1:4]
try:
    os.rename(victim, aside)
except OSError as exc:
    print("refused", getattr(exc, "winerror", None))
    raise SystemExit(0)
done = subprocess.run(["cmd", "/c", "mklink", "/J", victim, target], capture_output=True)
print("swapped", done.returncode)
"""


@pytest.mark.windows_only
@pytest.mark.parametrize("level", [0, 1, 2], ids=["settings_folder", "localappdata", "appdata"])
def test_a_swap_during_the_save_is_impossible(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: int
) -> None:
    """Between pinning and writing, another process tries to swap a folder for a junction."""
    store = SettingsStore()
    store.load()
    store.save(UserSettings(Lang.EN))
    victim = path.parents[level]  # DownloadsOrganizer, Local, AppData
    target = tmp_path / "attacker"
    target.mkdir()
    outputs: list[str] = []
    real_lock = settingsio.Folder.lock

    def lock_then_attack(self: settingsio.Folder, name: str, wait: float = 3.0) -> object:
        handle = real_lock(self, name, wait)
        outputs.append(run_python(ATTACK, str(victim), str(victim) + "-aside", str(target)))
        return handle

    monkeypatch.setattr(settingsio.Folder, "lock", lock_then_attack)
    user = UserSettings(Lang.RU).with_rule(".avif", "Images")
    saved = store.save(user)
    assert outputs and outputs[0].startswith("refused"), outputs
    assert saved.path == path and path.read_bytes() == encode(user)
    assert tree(target) == [] and not Path(str(victim) + "-aside").exists()


@pytest.mark.windows_only
def test_a_swap_before_the_save_is_refused(path: Path, tmp_path: Path) -> None:
    """The folder replaced by a junction after the load: not followed, nothing written."""
    store = SettingsStore()
    store.load()
    store.save(UserSettings(Lang.EN))
    (tmp_path / "x").mkdir()
    out = run_python(ATTACK, str(path.parent), str(path.parent) + "-aside", str(tmp_path / "x"))
    assert out == "swapped 0"
    aside = snapshot(Path(str(path.parent) + "-aside"))
    with pytest.raises(SaveError) as info:
        store.save(UserSettings(Lang.RU))
    assert info.value.reason is SaveFailure.LINK
    assert tree(tmp_path / "x") == []
    assert snapshot(Path(str(path.parent) + "-aside")) == aside


# --- two processes, one file -------------------------------------------------------------

RACER = r"""
import sys, time
from pathlib import Path
from downloads_organizer import settingsio, usersettings
from downloads_organizer.i18n import Lang
ready, go, lang = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
store = usersettings.SettingsStore()
store.load()
real = settingsio.write_all
def slow(handle, data):
    time.sleep(1.0)  # holds the lock while the other process wants it
    real(handle, data)
settingsio.write_all = slow
ready.write_text("ready")
while not go.exists():
    time.sleep(0.005)
try:
    store.save(usersettings.UserSettings(Lang(lang)))
    print("saved", lang)
except usersettings.SaveError as exc:
    print(exc.reason, lang)
"""


@pytest.mark.windows_only
def test_two_processes_with_the_same_baseline_never_both_save(path: Path, tmp_path: Path) -> None:
    save_settings(UserSettings(Lang.RU).with_rule(".avif", "Images"))
    go = tmp_path / "go"
    racers = []
    for index, lang in enumerate(("en", "ru")):
        ready = tmp_path / f"ready{index}"
        racers.append((ready, python(RACER, str(ready), str(go), lang)))
    for ready, process in racers:
        while not ready.exists():
            assert process.poll() is None, process.communicate()
            time.sleep(0.01)
    go.write_text("go")
    results = []
    for _ready, process in racers:
        out, err = process.communicate(timeout=60)
        assert process.returncode == 0, err
        results.append(out.split())
    winners = [lang for status, lang in results if status == "saved"]
    losers = [status for status, _lang in results if status != "saved"]
    assert len(winners) == 1 and losers in (["conflict"], ["busy"]), results
    assert load_settings().settings == UserSettings(Lang(winners[0]))  # one complete version
    assert names(path.parent) == ["settings.json", LOCK_NAME]  # no temporary file left


@pytest.mark.windows_only
def test_a_held_lock_makes_the_save_busy(path: Path) -> None:
    store = SettingsStore()
    store.load()
    store.save(UserSettings(Lang.EN))
    old = path.read_bytes()
    holder = python(
        "import sys, time\nf = open(sys.argv[1], 'rb')\nprint('held', flush=True)\ntime.sleep(30)",
        str(path.parent / LOCK_NAME),
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        with pytest.raises(SaveError) as info:
            store.save(UserSettings(Lang.RU))
        assert info.value.reason is SaveFailure.BUSY and info.value.intact
    finally:
        holder.kill()
        holder.communicate()
    assert path.read_bytes() == old
    assert store.save(UserSettings(Lang.RU)).path == path  # free again: saved


# --- save: only over the version that was loaded -----------------------------------------


def test_save_needs_localappdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA")
    with pytest.raises(SaveError) as info:
        save_settings(UserSettings())
    assert info.value.reason is SaveFailure.NO_LOCALAPPDATA
    assert load_settings().problem is SettingsProblem.NO_LOCALAPPDATA


def test_save_makes_only_its_own_folder(isolated_user_dirs: Path, tmp_path: Path) -> None:
    missing = tmp_path / "no" / "such" / "DownloadsOrganizer" / "settings.json"
    with pytest.raises(SaveError) as info:
        save_settings(UserSettings(), missing)
    assert info.value.reason is SaveFailure.FOLDER_FAILED
    assert not (tmp_path / "no").exists()


def test_a_corrupt_file_is_kept_aside_before_writing(path: Path) -> None:
    write(path, b"{not json")
    store = SettingsStore()
    assert store.load().problem is SettingsProblem.CORRUPT
    saved = store.save(UserSettings(Lang.EN))
    assert saved.backup is not None and saved.backup.read_bytes() == b"{not json"
    assert saved.backup.name.startswith("settings.json.bak-")
    assert load_settings().settings.language is Lang.EN
    assert names(path.parent) == sorted(["settings.json", saved.backup.name, LOCK_NAME])


def test_own_file_is_replaced_without_a_backup(path: Path) -> None:
    store = SettingsStore()
    store.load()
    first = store.save(UserSettings(Lang.EN))
    second = store.save(UserSettings(Lang.RU))
    assert first.backup is None and second.backup is None
    assert names(path.parent) == ["settings.json", LOCK_NAME]
    assert (path.parent / LOCK_NAME).stat().st_size == 0  # the lock is never written
    assert path.read_bytes() == encode(UserSettings(Lang.RU))


@pytest.mark.parametrize("change", ["changed", "removed"])
def test_a_file_changed_by_someone_else_is_not_overwritten(path: Path, change: str) -> None:
    store = SettingsStore()
    store.load()
    store.save(UserSettings(Lang.EN))
    if change == "changed":
        path.write_bytes(document(language="ru").encode("utf-8"))  # another window, an editor
    else:
        path.unlink()
    before = snapshot(path.parent)
    with pytest.raises(SaveError) as info:
        store.save(UserSettings(Lang.EN).with_rule(".avif", "Images"))
    assert info.value.reason is SaveFailure.CONFLICT and info.value.intact
    assert change in str(info.value)
    assert snapshot(path.parent) == before
    # after loading it again the save is possible
    store.load()
    assert store.save(UserSettings(Lang.EN)).path == path


def test_a_file_appearing_after_an_empty_load_is_not_overwritten(path: Path) -> None:
    store = SettingsStore()
    store.load()  # nothing there
    write(path, document())
    before = snapshot(path.parent)
    with pytest.raises(SaveError) as info:
        store.save(UserSettings())
    assert info.value.reason is SaveFailure.CONFLICT and "created" in str(info.value)
    # the settings file is untouched; only the (empty, never written) lock file is new
    assert snapshot(path.parent) == {**before, LOCK_NAME: sha(b"")}


def test_settings_that_do_not_read_back_are_not_written(path: Path) -> None:
    unsorted = UserSettings(rules=(CustomRule(".heic", "Images"), CustomRule(".avif", "Images")))
    with pytest.raises(SaveError) as info:
        save_settings(unsorted)
    assert info.value.reason is SaveFailure.PREPARE_FAILED
    assert not path.parent.exists()


# --- save: failures of single steps leave a usable state ---------------------------------


@pytest.fixture
def saved(path: Path) -> tuple[SettingsStore, dict[str, str | None]]:
    """A store with its own clean file, and the SHA-256 of the folder."""
    store = SettingsStore()
    store.load()
    store.save(UserSettings(Lang.EN).with_rule(".avif", "Images"))
    return store, snapshot(path.parent)


def failing(*_args: object, **_kwargs: object) -> None:
    raise OSError("injected failure")


@pytest.mark.parametrize(
    ("target", "name", "reason"),
    [
        (settingsio.Folder, "create", SaveFailure.TEMP_FAILED),
        (winprims, "write_at", SaveFailure.WRITE_FAILED),
        (winprims, "flush", SaveFailure.WRITE_FAILED),
    ],
    ids=["temp", "write", "flush"],
)
def test_a_failure_before_the_rename_changes_nothing(
    path: Path,
    saved: tuple[SettingsStore, dict[str, str | None]],
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    name: str,
    reason: SaveFailure,
) -> None:
    store, before = saved
    with monkeypatch.context() as patch:
        patch.setattr(target, name, failing)
        with pytest.raises(SaveError) as info:
            store.save(UserSettings(Lang.RU))
    assert info.value.reason is reason and info.value.intact
    assert info.value.backup is None and info.value.temp is None  # the temporary file is gone
    assert snapshot(path.parent) == before
    assert store.save(UserSettings(Lang.RU)).backup is None  # the baseline still holds


def test_a_short_read_back_is_a_failed_write(
    path: Path, saved: tuple[SettingsStore, dict[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, before = saved
    real = winprims.write_at
    monkeypatch.setattr(winprims, "write_at", lambda h, o, d: real(h, o, d[: len(d) // 2]))
    with pytest.raises(SaveError) as info:
        store.save(UserSettings(Lang.RU))
    assert info.value.reason is SaveFailure.WRITE_FAILED and info.value.intact
    assert snapshot(path.parent) == before


def renames_failing(monkeypatch: pytest.MonkeyPatch, *failing_calls: int) -> list[str]:
    """Make the given calls (1-based) of ``Folder.rename`` fail; returns the names asked."""
    real = settingsio.Folder.rename
    asked: list[str] = []

    def rename(self: settingsio.Folder, handle: winfileops.Handle, name: str) -> None:
        asked.append(name)
        if len(asked) in failing_calls:
            raise OSError(f"injected failure of rename {len(asked)}")
        real(self, handle, name)

    monkeypatch.setattr(settingsio.Folder, "rename", rename)
    return asked


def test_a_failed_backup_rename_changes_nothing(
    path: Path, saved: tuple[SettingsStore, dict[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, before = saved
    renames_failing(monkeypatch, 1)
    with pytest.raises(SaveError) as info:
        store.save(UserSettings(Lang.RU))
    assert info.value.reason is SaveFailure.BACKUP_FAILED and info.value.intact
    assert info.value.temp is None and snapshot(path.parent) == before


def test_a_failed_replace_puts_the_old_file_back(
    path: Path, saved: tuple[SettingsStore, dict[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, before = saved
    with monkeypatch.context() as patch:
        asked = renames_failing(patch, 2)
        with pytest.raises(SaveError) as info:
            store.save(UserSettings(Lang.RU))
    assert info.value.reason is SaveFailure.REPLACE_FAILED and info.value.intact
    assert asked[0].startswith("settings.json.bak-") and asked[1:] == ["settings.json"] * 2
    assert info.value.temp is None and snapshot(path.parent) == before
    assert store.save(UserSettings(Lang.RU)).path == path


def test_a_failed_replace_and_put_back_names_both_versions(
    path: Path, saved: tuple[SettingsStore, dict[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, before = saved
    assert sorted(before) == ["settings.json", LOCK_NAME]
    old = path.read_bytes()
    renames_failing(monkeypatch, 2, 3)
    with pytest.raises(SaveError) as info:
        store.save(UserSettings(Lang.RU))
    error = info.value
    assert error.reason is SaveFailure.REPLACE_FAILED and not error.intact
    assert error.backup is not None and error.temp is not None
    assert error.backup.read_bytes() == old  # no version lost
    assert error.temp.read_bytes() == encode(UserSettings(Lang.RU))
    assert not path.exists()
    assert names(path.parent) == sorted([LOCK_NAME, error.backup.name, error.temp.name])
    # the next save does not take the backup for its own file: it reports the conflict
    with pytest.raises(SaveError) as again:
        store.save(UserSettings(Lang.RU))
    assert again.value.reason is SaveFailure.CONFLICT and "removed" in str(again.value)
    assert error.backup.read_bytes() == old and len(before) == 2  # still there


def test_a_failed_check_after_the_replace_keeps_the_backup(
    path: Path, saved: tuple[SettingsStore, dict[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _before = saved
    old = path.read_bytes()
    monkeypatch.setattr(settingsio.Folder, "identity", lambda self, name: -1)
    with pytest.raises(SaveError) as info:
        store.save(UserSettings(Lang.RU))
    error = info.value
    assert error.reason is SaveFailure.VERIFY_FAILED and not error.intact
    assert error.backup is not None and error.backup.read_bytes() == old
    assert path.read_bytes() == encode(UserSettings(Lang.RU))


def test_a_failed_delete_of_the_clean_old_file_is_a_note(
    path: Path, saved: tuple[SettingsStore, dict[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _before = saved
    old = path.read_bytes()
    monkeypatch.setattr(settingsio, "delete", failing)
    result = store.save(UserSettings(Lang.RU))
    assert result.notes and result.backup is not None and result.backup.read_bytes() == old
    assert path.read_bytes() == encode(UserSettings(Lang.RU))
    assert result.baseline.digest == sha(path.read_bytes())


def test_save_writes_a_complete_file_before_any_rename(
    path: Path, saved: tuple[SettingsStore, dict[str, str | None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _before = saved
    seen: list[tuple[str, bytes]] = []
    real = settingsio.Folder.rename

    def spy(self: settingsio.Folder, handle: winfileops.Handle, name: str) -> None:
        seen.append((name, settingsio.read_all(handle, MAX_SIZE) or b""))
        real(self, handle, name)

    monkeypatch.setattr(settingsio.Folder, "rename", spy)
    user = UserSettings().with_rule(".heic", "Images")
    store.save(user)
    (backup_name, old_data), (final_name, new_data) = seen
    assert backup_name.startswith("settings.json.bak-") and final_name == "settings.json"
    assert old_data == encode(UserSettings(Lang.EN).with_rule(".avif", "Images"))
    assert new_data == encode(user) == path.read_bytes()
    assert names(path.parent) == ["settings.json", LOCK_NAME]  # the clean backup is deleted
