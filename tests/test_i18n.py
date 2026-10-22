"""The Russian and English texts (R4-C): complete, matching and never applied to codes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from downloads_organizer import i18n
from downloads_organizer.extensions import DEFAULT_CATEGORIES
from downloads_organizer.i18n import Lang, pick, placeholders, tr

SOURCES = Path(__file__).resolve().parent.parent / "downloads_organizer"


@pytest.mark.parametrize("key", sorted(i18n.MESSAGES))
def test_both_languages_have_the_same_placeholders(key: str) -> None:
    ru, en = i18n.MESSAGES[key]
    assert ru.strip() and en.strip()
    assert placeholders(ru) == placeholders(en)


@pytest.mark.parametrize(
    ("enum", "table"), i18n.CODE_TABLES, ids=lambda v: getattr(v, "__name__", "")
)
def test_every_code_has_a_text_in_both_languages(enum: type, table: dict) -> None:
    for lang in Lang:
        assert set(table[lang]) == set(enum), lang
        assert all(text.strip() for text in table[lang].values())


def test_every_category_has_a_text_and_the_folders_stay_untranslated() -> None:
    keys = {c.key for c in DEFAULT_CATEGORIES}
    for lang in Lang:
        assert set(i18n.CATEGORY_TEXT[lang]) == keys
    # the folder on disk is a fixed name, not a translated one
    russian = set(i18n.CATEGORY_TEXT[Lang.RU].values()) - set(i18n.CATEGORY_TEXT[Lang.EN].values())
    assert all(c.folder.isascii() and c.folder not in russian for c in DEFAULT_CATEGORIES)
    assert pick(i18n.CATEGORY_TEXT, "Images", Lang.RU) == "Изображения"
    assert pick(i18n.CATEGORY_TEXT, "Images", Lang.EN) == "Images"


def test_keys_built_from_codes_exist() -> None:
    from downloads_organizer.gui import AREAS, FILTER_ALL, FILTER_NOT_MOVED
    from downloads_organizer.usersettings import SaveFailure, SettingsProblem

    keys = [f"settings.{p}" for p in SettingsProblem]
    keys += [f"settings.fail.{f}" for f in SaveFailure]
    keys += [f"gui.area.{a}" for a in AREAS]
    keys += [f"gui.filter.{f}" for f in (FILTER_ALL, FILTER_NOT_MOVED)]
    assert [k for k in keys if k not in i18n.MESSAGES] == []


def test_unknown_codes_and_keys_are_shown_as_they_are() -> None:
    assert pick(i18n.SKIP_TEXT, "some_future_reason", Lang.RU) == "some_future_reason"
    assert pick(i18n.EXEC_TEXT, "x_y", Lang.EN) == "x_y"
    assert tr("no.such.key", Lang.RU) == "no.such.key"


def test_texts_of_the_task() -> None:
    assert tr("gui.copied", Lang.RU) == "Путь скопирован"
    assert tr("gui.repreview", Lang.RU) == "Обновить предпросмотр оставшихся"
    assert pick(i18n.EXEC_TEXT, i18n.ExecReason.SOURCE_CHANGED, Lang.RU) == (
        "Файл изменился после предпросмотра. Перемещение отменено для безопасности"
    )


def test_every_key_used_in_the_code_exists() -> None:
    """Literal keys passed to ``tr``/``say`` and the like are all in ``MESSAGES``."""
    pattern = re.compile(r"""["']((?:gui|settings|report|journals?|preview|run)\.[a-z_.]+)["']""")
    used: set[str] = set()
    for source in SOURCES.glob("*.py"):
        if source.name == "i18n.py":
            continue
        used |= set(pattern.findall(source.read_text(encoding="utf-8")))
    assert used, "no key found: the pattern is wrong"
    used -= {"settings.json"}  # the file name
    missing = sorted(k for k in used if k not in i18n.MESSAGES and not k.endswith("."))
    assert missing == []
