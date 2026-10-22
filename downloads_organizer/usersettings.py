"""User settings: the language and the custom extensions (R4-C, FIX-01).

They live in ``%LOCALAPPDATA%\\DownloadsOrganizer\\settings.json``, next to the journal
folder and never inside it. The built-in categories are not stored: only what the user
added, each rule enabled or disabled.

* ``load_settings`` only reads. A missing, damaged, foreign or linked file gives the
  defaults and says why; nothing is created, changed, renamed or deleted. What it read
  is the ``Baseline`` a later save compares the file with.
* ``save_settings`` is called only for an explicit «Save settings» of the user. The folder
  is walked and held by handles (``settingsio``): no link or junction is followed and no
  component can be swapped until the save is over; every file is opened, created and
  renamed relative to that held folder. Under the lock ``settings.json.lock`` (one save at
  a time, also across processes) the current file must still be the baseline, else the
  save is refused (``CONFLICT``): another version is never overwritten. The new content
  is checked, written to a new temporary file, flushed and read back; only then is the
  old file renamed to ``settings.json.bak-<time>`` and the temporary file renamed to
  ``settings.json`` (neither rename ever replaces a file). A clean old file is deleted
  afterwards; a damaged or partly rejected one is kept for manual analysis.

A failure before the old file was renamed leaves it untouched. A failure of the last
rename puts the old file back; only if that fails as well is there no ``settings.json``,
and the error names the backup (the old content) and the temporary file (the new one).
Nothing is transactional beyond these renames.

``{"schema": 1, "app": "DownloadsOrganizer", "language": "ru",
"custom_extensions": [{"extension": ".avif", "category": "Images", "enabled": true}]}``
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from downloads_organizer import APP_NAME, settingsio, winfileops
from downloads_organizer.extensions import (
    ExtensionError,
    ExtensionErrorReason,
    ExtensionTable,
    RejectedEntry,
    normalize_extension,
)
from downloads_organizer.i18n import DEFAULT_LANG, Lang

SCHEMA = 1
FILE_NAME = "settings.json"
LOCK_NAME = f"{FILE_NAME}.lock"
MAX_SIZE = 256 * 1024  # larger is not parsed
MAX_DIGEST = 16 * 1024 * 1024  # larger is not even read: a save is refused until it is gone


class SettingsProblem(StrEnum):
    NO_LOCALAPPDATA = "no_localappdata"
    LINK = "link"  # a folder or the file is a link, a junction or not a regular one
    TOO_LARGE = "too_large"
    UNREADABLE = "unreadable"
    CORRUPT = "corrupt"  # not UTF-8 JSON
    FOREIGN = "foreign"  # JSON, but not this program's settings of this schema
    UNKNOWN_LANGUAGE = "unknown_language"  # the rest is used


FATAL_PROBLEMS = frozenset(SettingsProblem) - {SettingsProblem.UNKNOWN_LANGUAGE}
"""Problems for which nothing of the file is used."""


@dataclass(frozen=True, slots=True)
class CustomRule:
    extension: str  # normalized
    category: str  # category key
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class UserSettings:
    language: Lang = DEFAULT_LANG
    rules: tuple[CustomRule, ...] = ()  # sorted by extension, one rule per extension

    def table(self) -> ExtensionTable:
        """The built-in categories and the enabled rules."""
        return ExtensionTable(custom={r.extension: r.category for r in self.rules if r.enabled})

    def rule(self, extension: str) -> CustomRule | None:
        return next((r for r in self.rules if r.extension == extension), None)

    def with_language(self, language: Lang) -> UserSettings:
        return replace(self, language=Lang(language))

    def with_rule(self, raw: str, category: str) -> UserSettings:
        """Add an enabled rule; ``ExtensionError`` if invalid, built-in or already a rule.

        Disabled rules count: an extension has one rule, so it never maps to two categories.
        """
        everything = ExtensionTable(custom={r.extension: r.category for r in self.rules})
        ext = everything.validate_custom(raw, category)
        return self._with_rules((*self.rules, CustomRule(ext, category)))

    def with_enabled(self, extension: str, enabled: bool) -> UserSettings:
        rule = self._existing(extension)
        rules = tuple(replace(r, enabled=enabled) if r is rule else r for r in self.rules)
        return self._with_rules(rules)

    def without_rule(self, extension: str) -> UserSettings:
        rule = self._existing(extension)
        return self._with_rules(tuple(r for r in self.rules if r is not rule))

    def with_defaults(self) -> UserSettings:
        """The built-in set only (the language is kept)."""
        return replace(self, rules=())

    def _existing(self, extension: str) -> CustomRule:
        rule = self.rule(extension)
        if rule is None:
            raise ExtensionError(ExtensionErrorReason.NOT_CUSTOM, extension)
        return rule

    def _with_rules(self, rules: tuple[CustomRule, ...]) -> UserSettings:
        return replace(self, rules=tuple(sorted(rules, key=lambda r: r.extension)))


def settings_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """``%LOCALAPPDATA%\\DownloadsOrganizer\\settings.json``; ``None`` if it is unset."""
    env = os.environ if environ is None else environ
    base = env.get("LOCALAPPDATA")
    if not base or not Path(base).is_absolute():
        return None
    return Path(base) / APP_NAME / FILE_NAME


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode(settings: UserSettings) -> bytes:
    document = {
        "schema": SCHEMA,
        "app": APP_NAME,
        "language": str(settings.language),
        "custom_extensions": [
            {"extension": r.extension, "category": r.category, "enabled": r.enabled}
            for r in settings.rules
        ],
    }
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class Baseline:
    """The file as this program last read or wrote it: what a save expects to find.

    ``read`` is False if it could not be read (a save is then refused); ``digest`` is
    the sha256 of the whole file, ``None`` if there was none; ``keep`` asks to keep it as
    a backup when it is replaced (it was damaged or partly rejected).
    """

    read: bool = False
    digest: str | None = None
    keep: bool = False


ABSENT = Baseline(read=True)
"""There was no settings file."""


# --- load ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadResult:
    settings: UserSettings
    path: Path | None
    found: bool = False
    problem: SettingsProblem | None = None
    detail: str | None = None
    rejected: tuple[RejectedEntry, ...] = ()
    baseline: Baseline = Baseline()

    @property
    def clean(self) -> bool:
        return self.problem is None and not self.rejected


def _entry_fields(entry: object) -> tuple[str, str, bool] | None:
    if (
        not isinstance(entry, dict)
        or set(entry) - {"extension", "category", "enabled"}
        or not isinstance(entry.get("extension"), str)
        or not isinstance(entry.get("category"), str)
        or not isinstance(entry.get("enabled", True), bool)
    ):
        return None
    return entry["extension"], entry["category"], entry.get("enabled", True)


def _rules(entries: object) -> tuple[tuple[CustomRule, ...], list[RejectedEntry]]:
    """The rules of the stored entries; a doubtful extension gets no rule at all.

    ``enabled`` is only ever taken from the entry the rule is made of. Every entry of an
    extension is rejected if its entries disagree in any way (category, also an unknown
    one, or ``enabled``): one of them alone is not trusted. An exact repeat is rejected
    as ``CUSTOM_DUPLICATE`` and the first kept. Built-in and invalid extensions are
    rejected with their reason.
    """
    if not isinstance(entries, list):
        return (), [RejectedEntry(entries, None, ExtensionErrorReason.MALFORMED)]
    rejected: list[RejectedEntry] = []
    groups: dict[str, list[tuple[str, str, bool]]] = {}
    for entry in entries:
        fields = _entry_fields(entry)
        if fields is None:
            rejected.append(RejectedEntry(entry, None, ExtensionErrorReason.MALFORMED))
            continue
        try:
            ext = normalize_extension(fields[0])
        except ExtensionError as exc:
            rejected.append(RejectedEntry(fields[0], fields[1], exc.reason))
            continue
        groups.setdefault(ext, []).append(fields)
    base = ExtensionTable()
    rules: list[CustomRule] = []
    for ext, group in groups.items():
        raw, category, enabled = group[0]
        if len({(c, e) for _, c, e in group}) > 1:
            reason = ExtensionErrorReason.AMBIGUOUS
        else:
            try:
                base.validate_custom(raw, category)
                reason = None
            except ExtensionError as exc:
                reason = exc.reason
        if reason is not None:
            rejected += [RejectedEntry(r, c, reason) for r, c, _ in group]
            continue
        rules.append(CustomRule(ext, category, enabled))
        rejected += [
            RejectedEntry(r, c, ExtensionErrorReason.CUSTOM_DUPLICATE) for r, c, _ in group[1:]
        ]
    return tuple(sorted(rules, key=lambda r: r.extension)), rejected


@dataclass(frozen=True, slots=True)
class _Parsed:
    settings: UserSettings
    problem: SettingsProblem | None = None
    detail: str | None = None
    rejected: tuple[RejectedEntry, ...] = ()


def _parse(data: bytes) -> _Parsed:
    defaults = UserSettings()
    try:
        document = json.loads(data.decode("utf-8-sig"))
    except (ValueError, RecursionError) as exc:  # not UTF-8, not JSON, nested too deep
        return _Parsed(defaults, SettingsProblem.CORRUPT, str(exc))
    if (
        not isinstance(document, dict)
        or document.get("app") != APP_NAME
        or type(document.get("schema")) is not int
        or document["schema"] != SCHEMA
    ):
        return _Parsed(defaults, SettingsProblem.FOREIGN)
    rules, rejected = _rules(document.get("custom_extensions", []))
    problem = detail = None
    language = document.get("language", str(DEFAULT_LANG))
    try:
        lang = Lang(language)
    except ValueError:
        lang, problem, detail = DEFAULT_LANG, SettingsProblem.UNKNOWN_LANGUAGE, repr(language)
    return _Parsed(UserSettings(lang, rules), problem, detail, tuple(rejected))


def load_settings(path: Path | None = None) -> LoadResult:
    """Read the settings; on any problem the defaults (or what is valid) and why. Read only.

    The folder is walked by handles without creating anything; a link or junction on
    the way, or a settings file that is a link, a folder or has other names (hard
    links), is ``LINK`` and not read.
    """
    path = settings_path() if path is None else path
    defaults = UserSettings()
    if path is None:
        return LoadResult(defaults, None, problem=SettingsProblem.NO_LOCALAPPDATA)
    try:
        with settingsio.Folder.open(path.parent, create=False) as folder:
            handle = folder.open_file(path.name, for_rename=False)
            if handle is None:
                return LoadResult(defaults, path, baseline=ABSENT)
            with handle:
                data = settingsio.read_all(handle, MAX_DIGEST)
    except FileNotFoundError:
        return LoadResult(defaults, path, baseline=ABSENT)
    except settingsio.Refused as exc:
        problem = SettingsProblem.LINK if exc.kind == exc.LINK else SettingsProblem.UNREADABLE
        return LoadResult(defaults, path, True, problem, exc.detail)
    except OSError as exc:
        return LoadResult(defaults, path, True, SettingsProblem.UNREADABLE, _describe(exc))
    if data is None:
        return LoadResult(defaults, path, True, SettingsProblem.TOO_LARGE, f"> {MAX_DIGEST}")
    if len(data) > MAX_SIZE:
        baseline = Baseline(True, _digest(data), keep=True)
        return LoadResult(
            defaults, path, True, SettingsProblem.TOO_LARGE, str(len(data)), baseline=baseline
        )
    parsed = _parse(data)
    clean = parsed.problem is None and not parsed.rejected
    return LoadResult(
        parsed.settings,
        path,
        True,
        parsed.problem,
        parsed.detail,
        parsed.rejected,
        Baseline(True, _digest(data), keep=not clean),
    )


def _describe(exc: BaseException) -> str:
    return winfileops.describe_error(exc)


# --- save ------------------------------------------------------------------------------


class SaveFailure(StrEnum):
    NO_LOCALAPPDATA = "no_localappdata"
    NOT_READ = "not_read"  # the file could not be read at the start: reload first
    PREPARE_FAILED = "prepare_failed"  # the new content does not read back as the settings
    FOLDER_FAILED = "folder_failed"
    LINK = "link"
    UNVERIFIED = "unverified"  # the volume or the location is not proven
    BUSY = "busy"  # another save (maybe another window) holds the lock
    CONFLICT = "conflict"  # the file is not the one loaded: changed, added or removed
    READ_FAILED = "read_failed"  # the current file cannot be read to compare
    TEMP_FAILED = "temp_failed"
    WRITE_FAILED = "write_failed"  # writing, flushing or reading back the new file
    BACKUP_FAILED = "backup_failed"
    REPLACE_FAILED = "replace_failed"
    VERIFY_FAILED = "verify_failed"  # renamed, but not proven to be the new file


class SaveError(Exception):
    """A refused or failed save, and what is on the disk now.

    ``intact``: ``settings.json`` is exactly the file found before (or still absent).
    ``backup``: where the old file is if it was renamed and kept; ``temp``: a new file
    this save made and could not remove (with the new content if the write succeeded).
    """

    def __init__(
        self,
        reason: SaveFailure,
        detail: str | None = None,
        *,
        intact: bool = True,
        backup: Path | None = None,
        temp: Path | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.intact = intact
        self.backup = backup
        self.temp = temp
        super().__init__(f"{reason}: {detail}" if detail else str(reason))


@dataclass(frozen=True, slots=True)
class SaveResult:
    path: Path
    baseline: Baseline
    backup: Path | None = None  # the old file, kept because it was damaged or not ours
    notes: tuple[str, ...] = ()  # the save succeeded; something after it did not


@dataclass(slots=True)
class SettingsStore:
    """Where the settings are and what this program last read or wrote there."""

    path: Path | None = field(default_factory=settings_path)
    baseline: Baseline = ABSENT

    def load(self) -> LoadResult:
        result = load_settings(self.path)
        self.baseline = result.baseline
        return result

    def save(self, settings: UserSettings) -> SaveResult:
        result = save_settings(settings, self.path, self.baseline)
        self.baseline = result.baseline
        return result


def _backup_name(name: str) -> str:
    moment = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{name}.bak-{moment}"


def _refusal(exc: settingsio.Refused) -> SaveError:
    reason = {
        settingsio.Refused.LINK: SaveFailure.LINK,
        settingsio.Refused.BUSY: SaveFailure.BUSY,
    }.get(exc.kind, SaveFailure.UNVERIFIED)
    return SaveError(reason, exc.detail)


def save_settings(
    settings: UserSettings, path: Path | None = None, baseline: Baseline = ABSENT
) -> SaveResult:
    """Replace the settings file with ``settings`` if it still is ``baseline``.

    ``SaveError`` with what is on the disk if not saved; see the module docstring.
    """
    path = settings_path() if path is None else path
    if path is None:
        raise SaveError(SaveFailure.NO_LOCALAPPDATA)
    data = encode(settings)
    if _parse(data) != _Parsed(settings):
        raise SaveError(SaveFailure.PREPARE_FAILED, repr(settings))
    if not baseline.read:
        raise SaveError(SaveFailure.NOT_READ, str(path))
    try:
        folder = settingsio.Folder.open(path.parent, create=True)
    except settingsio.Refused as exc:
        raise _refusal(exc) from exc
    except OSError as exc:  # LOCALAPPDATA missing, no access, ...
        raise SaveError(SaveFailure.FOLDER_FAILED, _describe(exc)) from exc
    result: SaveResult | None = None
    try:
        with folder:
            try:
                lock = folder.lock(LOCK_NAME)
            except OSError as exc:
                raise SaveError(SaveFailure.FOLDER_FAILED, _describe(exc)) from exc
            with lock:
                result = _save_locked(folder, path.name, data, baseline)
    except settingsio.Refused as exc:
        raise _refusal(exc) from exc
    except OSError as exc:  # releasing a handle failed
        if result is None:
            raise SaveError(SaveFailure.FOLDER_FAILED, _describe(exc)) from exc
        return replace(result, notes=(*result.notes, _describe(exc)))
    assert result is not None
    return result


def _save_locked(
    folder: settingsio.Folder, name: str, data: bytes, baseline: Baseline
) -> SaveResult:
    try:
        old = folder.open_file(name, for_rename=True)
    except OSError as exc:
        raise SaveError(SaveFailure.READ_FAILED, _describe(exc)) from exc
    try:
        if old is None:
            found = None
        else:
            try:
                current = settingsio.read_all(old, MAX_DIGEST)
            except OSError as exc:
                raise SaveError(SaveFailure.READ_FAILED, _describe(exc)) from exc
            found = "unread" if current is None else _digest(current)
        if found != baseline.digest:
            what = (
                "removed" if found is None else "created" if baseline.digest is None else "changed"
            )
            raise SaveError(SaveFailure.CONFLICT, f"{folder.path / name} was {what}")
        temp_name = f"{name}.{uuid.uuid4().hex}.tmp"
        try:
            temp = folder.create(temp_name)
        except settingsio.Refused as exc:  # made, but not a plain new file: left as it is
            raise SaveError(
                SaveFailure.TEMP_FAILED, exc.detail, temp=folder.path / temp_name
            ) from exc
        except OSError as exc:
            raise SaveError(SaveFailure.TEMP_FAILED, _describe(exc)) from exc
        with temp:
            return _install(folder, name, old, temp, data, baseline)
    finally:
        if old is not None:
            old.close()


def _discard(temp: winfileops.Handle) -> Path | None:
    """Delete the temporary file of this save; its path if that failed."""
    try:
        settingsio.delete(temp)
    except OSError:
        return temp.path
    return None


def _install(
    folder: settingsio.Folder,
    name: str,
    old: winfileops.Handle | None,
    temp: winfileops.Handle,
    data: bytes,
    baseline: Baseline,
) -> SaveResult:
    try:
        settingsio.write_all(temp, data)
    except OSError as exc:
        raise SaveError(SaveFailure.WRITE_FAILED, _describe(exc), temp=_discard(temp)) from exc
    backup: Path | None = None
    if old is not None:
        backup_name = _backup_name(name)
        try:
            folder.rename(old, backup_name)
        except OSError as exc:
            raise SaveError(SaveFailure.BACKUP_FAILED, _describe(exc), temp=_discard(temp)) from exc
        backup = folder.path / backup_name
    try:
        folder.rename(temp, name)
    except OSError as exc:
        detail = _describe(exc)
        if old is not None:
            try:
                folder.rename(old, name)  # put the old file back
            except OSError as again:
                raise SaveError(
                    SaveFailure.REPLACE_FAILED,
                    f"{detail}; the old file was not put back: {_describe(again)}",
                    intact=False,
                    backup=backup,
                    temp=temp.path,
                ) from exc
        raise SaveError(SaveFailure.REPLACE_FAILED, detail, temp=_discard(temp)) from exc
    try:
        folder.check()
        if folder.identity(name) != winfileops.query(temp).file_id:
            raise OSError(f"{folder.path / name} is not the new file")
        if settingsio.read_all(temp, len(data)) != data:
            raise OSError(f"{folder.path / name} does not read back as written")
    except (OSError, settingsio.Refused) as exc:
        raise SaveError(SaveFailure.VERIFY_FAILED, str(exc), intact=False, backup=backup) from exc
    notes: tuple[str, ...] = ()
    if old is not None and not baseline.keep:  # the clean old version is not needed
        try:
            settingsio.delete(old)
            backup = None
        except OSError as exc:
            notes = (_describe(exc),)
    return SaveResult(folder.path / name, Baseline(True, _digest(data)), backup, notes)
