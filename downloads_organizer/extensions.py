"""Extension normalization, validation and the effective extension -> category table.

Extensions are compared case-insensitively and stored normalized: a leading dot
followed by lowercase ASCII letters, digits, ``_`` or ``-``. Only the last suffix of a
file name is used for classification (``archive.tar.gz`` -> ``.gz``), as in the legacy
program.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from downloads_organizer.catalog import DEFAULT_CATEGORIES, PARTIAL_DOWNLOAD_EXTENSIONS, Category
from downloads_organizer.pathsafety import check_folder_name

MAX_EXTENSION_LENGTH = 16
"""Maximum length of a normalized extension, including the leading dot."""

_BODY_RE = re.compile(r"[a-z0-9_-]+")


class ExtensionErrorReason(StrEnum):
    EMPTY = "empty"
    INVALID_CHARACTERS = "invalid_characters"
    COMPOUND = "compound"
    TOO_LONG = "too_long"
    PARTIAL_DOWNLOAD = "partial_download"
    BUILTIN_DUPLICATE = "builtin_duplicate"
    CUSTOM_DUPLICATE = "custom_duplicate"
    AMBIGUOUS = "ambiguous"
    UNKNOWN_CATEGORY = "unknown_category"
    NOT_CUSTOM = "not_custom"
    MALFORMED = "malformed"


REASON_MESSAGES: Mapping[ExtensionErrorReason, str] = MappingProxyType(
    {
        ExtensionErrorReason.EMPTY: "Пустое расширение",
        ExtensionErrorReason.INVALID_CHARACTERS: (
            "Допустимы только латинские буквы, цифры, «_» и «-»"
        ),
        ExtensionErrorReason.COMPOUND: (
            "Составные расширения (например, .tar.gz) не поддерживаются"
        ),
        ExtensionErrorReason.TOO_LONG: (
            f"Расширение длиннее {MAX_EXTENSION_LENGTH} символов вместе с точкой"
        ),
        ExtensionErrorReason.PARTIAL_DOWNLOAD: "Это расширение незавершённой загрузки",
        ExtensionErrorReason.BUILTIN_DUPLICATE: "Расширение уже есть во встроенной категории",
        ExtensionErrorReason.CUSTOM_DUPLICATE: "Расширение уже добавлено",
        ExtensionErrorReason.AMBIGUOUS: "Расширение назначено нескольким категориям",
        ExtensionErrorReason.UNKNOWN_CATEGORY: "Неизвестная категория",
        ExtensionErrorReason.NOT_CUSTOM: "Это не пользовательское расширение",
        ExtensionErrorReason.MALFORMED: "Некорректная запись",
    }
)


class ExtensionError(ValueError):
    """A rejected extension; ``reason`` is machine-readable, ``str()`` is for the user."""

    def __init__(self, reason: ExtensionErrorReason, value: object) -> None:
        self.reason = reason
        self.value = value
        super().__init__(f"{REASON_MESSAGES[reason]}: {value!r}")


def normalize_extension(raw: str) -> str:
    """Return ``raw`` as a normalized extension (``" PDF "`` -> ``".pdf"``) or raise.

    Surrounding whitespace and a single leading dot are optional. Raises
    ``ExtensionError`` for empty, compound, too long, non-ASCII or otherwise unsafe
    input and for extensions of partial downloads.
    """
    text = raw.strip()
    body = text.removeprefix(".")
    if not body:
        raise ExtensionError(ExtensionErrorReason.EMPTY, raw)
    if "." in body:
        if body.startswith(".") or body.endswith("."):
            raise ExtensionError(ExtensionErrorReason.INVALID_CHARACTERS, raw)
        raise ExtensionError(ExtensionErrorReason.COMPOUND, raw)
    ext = "." + body.lower()
    if ext in PARTIAL_DOWNLOAD_EXTENSIONS:
        raise ExtensionError(ExtensionErrorReason.PARTIAL_DOWNLOAD, raw)
    if len(ext) > MAX_EXTENSION_LENGTH:
        raise ExtensionError(ExtensionErrorReason.TOO_LONG, raw)
    if not body.isascii() or not _BODY_RE.fullmatch(ext[1:]):
        raise ExtensionError(ExtensionErrorReason.INVALID_CHARACTERS, raw)
    return ext


def extension_of(filename: str) -> str | None:
    """Return the lowercase last suffix of a bare file name, or ``None`` if it has none.

    Dot-files without a further suffix (``.bashrc``) and a lone trailing dot have no
    extension. The result is not validated; unknown suffixes simply match nothing.
    """
    dot = filename.rfind(".")
    if dot <= 0 or dot == len(filename) - 1:
        return None
    return filename[dot:].lower()


@dataclass(frozen=True, slots=True)
class RejectedEntry:
    """A stored custom extension entry that could not be loaded."""

    extension: object
    category: object
    reason: ExtensionErrorReason


class ExtensionTable:
    """Immutable effective mapping of extensions to categories (built-in + custom)."""

    __slots__ = ("_builtin", "_by_key", "_categories", "_custom")

    def __init__(
        self,
        categories: tuple[Category, ...] = DEFAULT_CATEGORIES,
        custom: Mapping[str, str] | None = None,
    ) -> None:
        by_key: dict[str, Category] = {}
        builtin: dict[str, str] = {}
        for category in categories:
            if category.key in by_key:
                raise ValueError(f"duplicate category key {category.key!r}")
            check_folder_name(category.folder)  # again: a Category can be altered after creation
            by_key[category.key] = category
            for ext in category.extensions:
                if normalize_extension(ext) != ext or ext in builtin:
                    raise ValueError(f"invalid or duplicate built-in extension {ext!r}")
                builtin[ext] = category.key
        self._categories = categories
        self._by_key = by_key
        self._builtin = builtin
        self._custom: dict[str, str] = {}
        for ext, key in (custom or {}).items():
            normalized = self.validate_custom(ext, key)
            self._custom[normalized] = key

    @classmethod
    def from_stored(
        cls,
        entries: object,
        categories: tuple[Category, ...] = DEFAULT_CATEGORIES,
    ) -> tuple[ExtensionTable, list[RejectedEntry]]:
        """Build a table from persisted custom extensions; never raises on bad data.

        ``entries`` is expected to be a list or tuple (e.g. a decoded JSON array) whose
        items are ``[extension, category_key]`` pairs: a list or tuple of exactly two
        strings. Any other value is accepted as input but treated as damaged settings:

        * a top level that is not a list or tuple (``None``, a number, a string, a
          dict, ...) yields a table without custom extensions and a single
          ``MALFORMED`` entry with ``extension`` set to that value;
        * an item that is not such a pair is rejected alone as ``MALFORMED``
          (``extension`` is the item, ``category`` is ``None``); a two-item pair with a
          non-string member keeps both members in the rejected entry;
        * a well-formed pair that fails validation is rejected with its reason.

        An extension assigned to more than one category is ambiguous and all of its
        entries are rejected; an exact repeat is rejected as ``CUSTOM_DUPLICATE`` and
        the first occurrence is kept. Rejected entries are listed in the order they are
        detected (the first entry of an ambiguous extension when the conflict appears).
        """
        base = cls(categories)
        if not isinstance(entries, list | tuple):
            return base, [RejectedEntry(entries, None, ExtensionErrorReason.MALFORMED)]
        rejected: list[RejectedEntry] = []
        accepted: dict[str, tuple[str, str]] = {}  # normalized -> (raw extension, key)
        ambiguous: set[str] = set()
        for item in entries:
            if not isinstance(item, list | tuple) or len(item) != 2:
                rejected.append(RejectedEntry(item, None, ExtensionErrorReason.MALFORMED))
                continue
            raw_ext, raw_key = item
            if not isinstance(raw_ext, str) or not isinstance(raw_key, str):
                rejected.append(RejectedEntry(raw_ext, raw_key, ExtensionErrorReason.MALFORMED))
                continue
            try:
                ext = base.validate_custom(raw_ext, raw_key)
            except ExtensionError as exc:
                rejected.append(RejectedEntry(raw_ext, raw_key, exc.reason))
                continue
            if ext in ambiguous:
                rejected.append(RejectedEntry(raw_ext, raw_key, ExtensionErrorReason.AMBIGUOUS))
            elif ext not in accepted:
                accepted[ext] = (raw_ext, raw_key)
            elif accepted[ext][1] == raw_key:
                reason = ExtensionErrorReason.CUSTOM_DUPLICATE
                rejected.append(RejectedEntry(raw_ext, raw_key, reason))
            else:
                ambiguous.add(ext)
                first_ext, first_key = accepted.pop(ext)
                rejected.append(RejectedEntry(first_ext, first_key, ExtensionErrorReason.AMBIGUOUS))
                rejected.append(RejectedEntry(raw_ext, raw_key, ExtensionErrorReason.AMBIGUOUS))
        custom = {ext: key for ext, (_, key) in accepted.items()}
        return cls(categories, custom), rejected

    @property
    def categories(self) -> tuple[Category, ...]:
        return self._categories

    @property
    def custom(self) -> Mapping[str, str]:
        """Read-only view of custom extension -> category key."""
        return MappingProxyType(self._custom)

    def category(self, key: str) -> Category:
        """Return the category with ``key``; raise ``KeyError`` if unknown."""
        return self._by_key[key]

    def custom_extensions(self, key: str) -> tuple[str, ...]:
        """Custom extensions of category ``key``, sorted."""
        if key not in self._by_key:
            raise KeyError(key)
        return tuple(sorted(ext for ext, k in self._custom.items() if k == key))

    def extensions(self, key: str) -> tuple[str, ...]:
        """All extensions of category ``key``: built-in common, rare, then custom."""
        return self._by_key[key].extensions + self.custom_extensions(key)

    def all_extensions(self) -> frozenset[str]:
        return frozenset(self._builtin) | frozenset(self._custom)

    def is_builtin(self, ext: str) -> bool:
        return ext in self._builtin

    def is_custom(self, ext: str) -> bool:
        return ext in self._custom

    def category_for(self, ext: str) -> Category | None:
        """Category of a normalized extension (compared case-insensitively), if any."""
        ext = ext.lower()
        key = self._builtin.get(ext) or self._custom.get(ext)
        return self._by_key[key] if key is not None else None

    def category_for_filename(self, filename: str) -> Category | None:
        ext = extension_of(filename)
        return self.category_for(ext) if ext is not None else None

    def validate_custom(self, raw: str, key: str) -> str:
        """Validate a new custom extension for category ``key``; return it normalized."""
        ext = normalize_extension(raw)
        if key not in self._by_key:
            raise ExtensionError(ExtensionErrorReason.UNKNOWN_CATEGORY, key)
        if ext in self._builtin:
            raise ExtensionError(ExtensionErrorReason.BUILTIN_DUPLICATE, raw)
        if ext in self._custom:
            raise ExtensionError(ExtensionErrorReason.CUSTOM_DUPLICATE, raw)
        return ext

    def with_custom(self, raw: str, key: str) -> ExtensionTable:
        """Return a new table with custom extension ``raw`` added to category ``key``."""
        ext = self.validate_custom(raw, key)
        return ExtensionTable(self._categories, {**self._custom, ext: key})

    def without_custom(self, raw: str) -> ExtensionTable:
        """Return a new table without custom extension ``raw``; built-ins cannot be removed."""
        ext = normalize_extension(raw)
        if ext not in self._custom:
            raise ExtensionError(ExtensionErrorReason.NOT_CUSTOM, raw)
        remaining = {e: k for e, k in self._custom.items() if e != ext}
        return ExtensionTable(self._categories, remaining)
