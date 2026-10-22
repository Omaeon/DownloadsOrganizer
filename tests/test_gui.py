"""Tests of the window (R4-A, R4-C): built, wired to ``appmodel`` and closed cleanly.

A real Tk window is opened for each test (skipped where Tk cannot open one). Every folder
is below ``tmp_path``; ``LOCALAPPDATA`` and ``APPDATA`` are the isolated ones of conftest,
so ``settings.json`` (by an explicit save only) and the journals are written there only.
The confirmation dialog is either the real ``ChoiceDialog`` (answered by a scheduled
click) or ``ask_choice`` replaced by a stub that records what it was asked.
"""

from __future__ import annotations

import gc
import hashlib
import json
import struct
import threading
import time
import tkinter as tk
from collections.abc import Callable, Iterator
from pathlib import Path

import customtkinter as ctk
import pytest
from test_executor import touch
from test_volumeproof import V999, fake_destination

from downloads_organizer import gui, settingsio
from downloads_organizer.appmodel import RunOutcome, TaskKind
from downloads_organizer.i18n import Lang
from downloads_organizer.usersettings import SettingsStore, UserSettings, load_settings

pytestmark = pytest.mark.windows_only


def open_window(capsys: pytest.CaptureFixture[str], **kwargs: object) -> gui.OrganizerApp:
    # Tcl reads its init scripts while Tk starts; under pytest's fd-level capture this
    # failed now and then ("can't find a usable init.tcl"), never without it.
    # Rarely Tcl still reports its existing init files as unreadable; a second start works.
    for attempt in range(3):
        try:
            with capsys.disabled():
                window = gui.OrganizerApp(**kwargs)  # type: ignore[arg-type]
            break
        except tk.TclError as exc:  # no display
            if attempt == 2:
                pytest.skip(f"Tk window cannot be opened: {exc}")
            time.sleep(0.2)
    window.update()
    return window


@pytest.fixture
def app(capsys: pytest.CaptureFixture[str]) -> Iterator[gui.OrganizerApp]:
    window = open_window(capsys)
    yield window
    close(window)


def close(window: gui.OrganizerApp) -> None:
    if window_exists(window):
        window.destroy()


@pytest.fixture
def src(tmp_path: Path) -> Path:
    path = tmp_path / "area" / "Downloads"
    touch(path, "a.pdf", "b.jpg", "notes.txt")
    return path


@pytest.fixture
def settings_file(isolated_user_dirs: Path) -> Path:
    return isolated_user_dirs / "AppData" / "Local" / "DownloadsOrganizer" / "settings.json"


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """``ask_choice`` says yes and records its texts; ``showerror`` fails the test."""
    questions: list[dict[str, str]] = []

    def yes(_master: object, **texts: str) -> bool:
        questions.append(texts)
        return True

    monkeypatch.setattr(gui, "ask_choice", yes)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: pytest.fail(f"{a}"))
    return questions


@pytest.fixture
def clipboard(app: gui.OrganizerApp) -> Iterator[None]:
    """The tests use the real clipboard; what was on it is put back afterwards."""
    try:
        before: str | None = app.clipboard_get()
    except tk.TclError:
        before = None
    yield
    if before is not None:
        try:
            root = tk.Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(before)
            root.update()
            root.destroy()
        except tk.TclError:
            pass


def window_exists(window: tk.Misc) -> bool:
    try:
        return bool(window.winfo_exists())
    except tk.TclError:
        return False


def pump(window: gui.OrganizerApp, until: Callable[[], bool], timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not until():
        assert time.monotonic() < deadline, "timed out"
        window.update()
        time.sleep(0.01)
    if window_exists(window):
        window.update()


def idle(window: gui.OrganizerApp) -> Callable[[], bool]:
    return lambda: window.task is None and window.events.empty()


def state(widget: object) -> str:
    return str(widget.cget("state"))  # type: ignore[attr-defined]


def text(widget: object) -> str:
    return str(widget.cget("text"))  # type: ignore[attr-defined]


def shown(widget: tk.Misc) -> bool:
    return bool(widget.grid_info())


def work_text(window: gui.OrganizerApp) -> str:
    return window.work_text.get("1.0", "end").strip()


def preview(window: gui.OrganizerApp, src: Path, *exts: str) -> None:
    window.source_var.set(str(src))
    for ext in exts:
        window.change(lambda m, e=ext: m.set_extension(e, True))
    window.start_preview()
    pump(window, idle(window))


def run(window: gui.OrganizerApp) -> None:
    window.confirm_and_run()
    pump(window, idle(window))


def click_segment(button: ctk.CTkSegmentedButton, label: str) -> None:
    button._buttons_dict[label].invoke()


def answer_dialog(
    window: gui.OrganizerApp, answer: Callable[[gui.ChoiceDialog], None]
) -> list[dict[str, str]]:
    """Answer the next ``ChoiceDialog`` once it is shown; returns what it showed."""
    seen: list[dict[str, str]] = []
    deadline = time.monotonic() + 10

    def look() -> None:
        dialog = window.active_dialog
        if dialog is None or not dialog.winfo_viewable():
            if time.monotonic() < deadline:
                window.after(20, look)
            elif dialog is not None:
                dialog.decline()  # never leave a test hanging; the test then fails
            return
        box = dialog.details_box
        seen.append(
            {
                "title": dialog.title(),
                "heading": text(dialog.heading_label),
                "body": text(dialog.body_label),
                "details": box.get("1.0", "end") if box is not None else "",
                "accept": text(dialog.accept_button),
                "cancel": text(dialog.cancel_button),
            }
        )
        answer(dialog)
        window.after(3000, lambda: forced(dialog))

    def forced(dialog: gui.ChoiceDialog) -> None:
        if window_exists(dialog) and not dialog._closing:  # the answer did not arrive
            seen.append({"forced": "yes"})
            dialog.decline()

    window.after(20, look)
    return seen


# --- the window -------------------------------------------------------------------------


def test_window_starts_in_russian_and_writes_nothing(
    app: gui.OrganizerApp, isolated_user_dirs: Path
) -> None:
    assert app.title().startswith("DownloadsOrganizer")
    assert app.lang is Lang.RU
    assert app.area_button.cget("values") == ["Сортировка", "Типы файлов", "Журналы"]
    assert text(app.preview_button) == "Предпросмотр" and text(app.execute_button) == "Переместить…"
    assert state(app.preview_button) == "normal"
    assert state(app.execute_button) == "disabled" and state(app.cancel_button) == "disabled"
    assert text(app.selection_label) == "Выбрано типов: 0 из 40"
    assert text(app.effective_label) == "Сначала выберите исходную папку"
    assert text(app.headline_label) == "Предпросмотра ещё нет"
    assert text(app.status_label) == ""
    assert len(app.cards) == 10
    # reading the settings creates nothing: no settings file, no journal folder
    assert not (isolated_user_dirs / "AppData" / "Local" / "DownloadsOrganizer").exists()


def test_preview_enables_execute_and_a_change_disables_it(
    app: gui.OrganizerApp, src: Path, tmp_path: Path
) -> None:
    preview(app, src, ".pdf", ".jpg")
    assert app.area == gui.AREA_SORT and app.view == gui.VIEW_PREVIEW
    assert len(app.preview_tree.get_children()) == 2
    assert state(app.execute_button) == "normal"
    assert text(app.status_label) == "Проверьте план и нажмите «Переместить…»."
    where = str(src / "DEL" / "<категория>")
    shown = gui.shorten_middle(where, gui.WHERE_LIMIT)  # the full path is in «Подробности»
    assert text(app.effective_label) == f"Файлы будут разложены в: {shown}"
    app.details_button.invoke()
    app.update()
    assert f"Куда: {where}" in work_text(app)
    assert f"Откуда: {src}" in work_text(app)
    app.destination_var.set(str(tmp_path / "Sorted"))
    app.update()
    assert state(app.execute_button) == "disabled"
    assert text(app.headline_label).startswith("Настройки изменились: этот предпросмотр устарел")
    sorted_where = str(tmp_path / "Sorted" / "<категория>")
    assert text(app.effective_label) == (
        f"Файлы будут разложены в: {gui.shorten_middle(sorted_where, gui.WHERE_LIMIT)}"
    )


def test_selected_row_shows_its_full_details(app: gui.OrganizerApp, tmp_path: Path) -> None:
    long_name = "отчёт_" + "очень_длинное_имя_" * 6 + ".pdf"
    source = tmp_path / "area" / "Downloads"
    touch(source, long_name)
    preview(app, source, ".pdf")
    (row,) = app.preview_tree.get_children()
    cell = app.preview_tree.set(row, "name")
    assert "…" in cell and len(cell) <= gui.NAME_LIMIT  # shortened in the table only
    app.preview_tree.selection_set(row)
    app.update()
    assert f"Файл: {long_name}" in work_text(app)
    assert str(source / long_name) in work_text(app)


def test_hiding_rare_extensions_keeps_them_selected(app: gui.OrganizerApp) -> None:
    card = next(c for c in app.cards if c.key == "Images")
    card.toggle()
    card.vars[".bmp"].set(1)
    card._extension_clicked(".bmp")
    card.toggle()  # collapse
    assert app.model.selection.is_selected(".bmp")
    assert "выбрано 1" in text(card.more_button)
    card.group_box.toggle()  # partial → all
    assert app.model.selection.category_summary("Images").selected == 6


def test_other_volume_says_copy_is_disabled(
    app: gui.OrganizerApp, src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_destination(monkeypatch, device=V999, volume_serial=0x1234)
    preview(app, src, ".pdf")
    assert text(app.headline_label) == "Предпросмотр: выполнение невозможно"
    assert "Копирование между томами пока отключено" in work_text(app)
    assert state(app.execute_button) == "disabled"


def ico_entries(data: bytes) -> dict[int, bytes]:
    """The images of an .ico file by width (256 is stored as 0)."""
    _, kind, count = struct.unpack_from("<HHH", data)
    assert kind == 1
    entries = {}
    for i in range(count):
        width, height, _, _, _, bits, size, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + 16 * i
        )
        assert width == height and bits == 32
        entries[width or 256] = data[offset : offset + size]
    return entries


def test_app_icon_is_customtkinters_picture_in_every_size() -> None:
    entries = ico_entries(gui.APP_ICON.read_bytes())
    assert sorted(entries) == [16, 20, 24, 32, 40, 48, 64, 128, 256]
    # the picture the window always showed, unchanged: CustomTkinter's 256x256 PNG itself
    ctk_icon = Path(ctk.__file__).parent / "assets" / "icons" / "CustomTkinter_icon_Windows.ico"
    assert entries[256] == ico_entries(ctk_icon.read_bytes())[256]
    assert entries[256].startswith(b"\x89PNG")


def test_every_window_shows_the_app_icon(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, object, object]] = []
    original = tk.Wm.wm_iconbitmap

    def spy(self: tk.Wm, bitmap: object = None, default: object = None) -> object:
        calls.append((type(self).__name__, bitmap, default))
        return original(self, bitmap, default)

    monkeypatch.setattr(tk.Wm, "wm_iconbitmap", spy)
    window = open_window(capsys)
    try:
        dialog = gui.ChoiceDialog(
            window, title="t", heading="h", body="b", details="", accept="a", cancel="c"
        )
        later = time.monotonic() + 0.5  # CustomTkinter sets its own icon after 200 ms
        pump(window, lambda: time.monotonic() > later)
        dialog.decline()
        pump(window, lambda: not window_exists(dialog))
    finally:
        close(window)
    icon = str(gui.APP_ICON)
    assert ("OrganizerApp", icon, icon) in calls  # also the default of later windows
    assert ("ChoiceDialog", icon, None) in calls
    assert all(bitmap == icon for _, bitmap, _ in calls)  # never CustomTkinter's file


# --- the confirmation dialog ------------------------------------------------------------


def test_real_dialog_cancel_moves_nothing(app: gui.OrganizerApp, src: Path) -> None:
    preview(app, src, ".pdf", ".jpg")
    seen = answer_dialog(app, lambda d: d.cancel_button.invoke())
    app.confirm_and_run()
    assert len(seen) == 1
    question = seen[0]
    assert question["title"] == "Подтверждение перемещения"
    assert question["heading"] == "Переместить файлы: 2?"
    assert question["cancel"] == "Отмена" and question["accept"] == "Переместить"
    assert f"Откуда: {src}" in question["details"]
    assert f"Куда: {src / 'DEL' / '<категория>'}" in question["details"]
    assert "не откатываются" in question["details"]
    assert app.task is None and app.active_dialog is None
    assert (src / "a.pdf").is_file() and (src / "b.jpg").is_file()
    assert text(app.status_label) == "Перемещение не начато. Файлы не изменялись."
    assert state(app.execute_button) == "normal"  # the preview is still the confirmed one


def test_real_dialog_closed_or_escaped_is_a_refusal(app: gui.OrganizerApp, src: Path) -> None:
    preview(app, src, ".pdf")

    def close_window(dialog: gui.ChoiceDialog) -> None:
        dialog.tk.call(dialog.protocol("WM_DELETE_WINDOW"))

    def escape(dialog: gui.ChoiceDialog) -> None:
        dialog.focus_force()
        dialog.update()
        dialog.event_generate("<Escape>")

    for answer in (close_window, escape):
        seen = answer_dialog(app, answer)
        app.confirm_and_run()
        assert len(seen) == 1 and app.task is None
        assert (src / "a.pdf").is_file()


def test_real_dialog_accept_moves_and_asks_every_time(
    app: gui.OrganizerApp, src: Path, isolated_user_dirs: Path
) -> None:
    preview(app, src, ".pdf")
    seen = answer_dialog(app, lambda d: d.accept_button.invoke())
    run(app)
    assert len(seen) == 1
    assert (src / "DEL" / "PDF" / "a.pdf").is_file() and (src / "b.jpg").is_file()
    assert app.view == gui.VIEW_RESULT
    assert text(app.headline_label) == "Готово: перемещено файлов — 1"
    assert app.report is not None and app.report.journal_path is not None
    assert app.report.journal_path.is_relative_to(isolated_user_dirs)
    assert state(app.execute_button) == "disabled"  # the preview was used up
    assert not app.model.running
    # a second move needs a new preview and a new confirmation
    touch(src, "c.pdf")
    preview(app, src)
    again = answer_dialog(app, lambda d: d.accept_button.invoke())
    run(app)
    assert len(again) == 1 and (src / "DEL" / "PDF" / "c.pdf").is_file()


def test_confirm_without_a_fresh_preview_does_not_ask(
    app: gui.OrganizerApp, src: Path, asked: list[dict[str, str]]
) -> None:
    app.confirm_and_run()
    preview(app, src, ".pdf")
    app.change(lambda m: m.set_extension(".jpg", True))
    app.confirm_and_run()
    assert asked == [] and app.task is None and (src / "a.pdf").is_file()
    assert text(app.status_label) == "Сначала сделайте предпросмотр без ошибок."


def test_settings_changed_while_the_dialog_is_open(
    app: gui.OrganizerApp, src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview(app, src, ".pdf")

    def change_then_accept(_master: object, **_texts: str) -> bool:
        app.model.set_extension(".jpg", True)  # e.g. a late event of the main window
        return True

    monkeypatch.setattr(gui, "ask_choice", change_then_accept)
    app.confirm_and_run()
    assert app.task is None and (src / "a.pdf").is_file()
    assert text(app.status_label).startswith("План изменился, пока было открыто подтверждение")


# --- skipped files and a new preview of the rest ------------------------------------------


def test_source_changed_is_skipped_and_the_rest_needs_a_new_preview(
    app: gui.OrganizerApp, src: Path, asked: list[dict[str, str]]
) -> None:
    preview(app, src, ".pdf", ".jpg")
    (src / "a.pdf").write_bytes(b"changed after the preview, longer than before")
    run(app)
    assert len(asked) == 1
    assert (src / "a.pdf").is_file() and (src / "DEL" / "Images" / "b.jpg").is_file()
    report = app.report
    assert report is not None and report.not_moved == 1
    assert text(app.headline_label).startswith("Выполнено частично")
    assert "Пропущено: 1. Ошибки: 0." in text(app.counts_label)
    skipped = [r for r in report.rows if r.name == "a.pdf"]
    assert len(skipped) == 1
    assert skipped[0].reason.startswith(
        "Файл изменился после предпросмотра. Перемещение отменено для безопасности"
    )
    assert skipped[0].status == "Пропущено"
    click_segment(app.filter_button, "Не перемещённые")
    (row,) = app.result_tree.get_children()
    assert app.result_tree.set(row, "name") == "a.pdf"
    assert shown(app.repreview_button) and state(app.repreview_button) == "normal"

    app.repreview_button.invoke()
    pump(app, idle(app))
    assert app.view == gui.VIEW_PREVIEW and len(asked) == 1  # nothing was moved again
    assert (src / "a.pdf").is_file()
    assert len(app.preview_tree.get_children()) == 1
    assert state(app.execute_button) == "normal"
    run(app)  # only after a second, separate confirmation
    assert len(asked) == 2 and (src / "DEL" / "PDF" / "a.pdf").is_file()


# --- languages ----------------------------------------------------------------------------


def test_language_switch_redraws_every_area_and_writes_nothing(
    app: gui.OrganizerApp,
    src: Path,
    settings_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    preview(app, src, ".jpg")
    (row,) = app.preview_tree.get_children()
    assert "Изображения" in app.preview_tree.set(row, "category")
    assert text(app.unsaved_label) == ""
    click_segment(app.language_button, "EN")
    app.update()
    assert app.lang is Lang.EN
    assert app.area_button.cget("values") == ["Sorting", "File types", "Journals"]
    assert text(app.preview_button) == "Preview" and text(app.execute_button) == "Move…"
    assert text(app.cancel_button) == "Cancel" and text(app.save_button) == "Save settings"
    assert text(app.headline_label) == "Preview ready"
    assert text(app.status_label) == "Check the plan and press «Move…»."
    assert app.preview_tree.heading("name")["text"] == "File"
    assert "Images" in app.preview_tree.set(row, "category")  # the category is translated
    assert state(app.execute_button) == "normal"  # the language is no setting of the plan
    assert text(app.selection_label) == "Types selected: 1 of 40"
    assert text(app.add_rule_button) == "Add" and text(app.journals_button) == "Refresh"
    assert app.rule_entry.cget("placeholder_text") == "e.g. .avif"
    assert app.policy_button.get() == "New name"
    images = next(c for c in app.cards if c.key == "Images")
    assert text(images.count_label) == "1 of 6"
    app.show_area(gui.AREA_JOURNALS)
    pump(app, lambda: app.journal_task is None)
    assert app.journal_tree.heading("time")["text"] == "Time"
    # switched at once, written never: no settings folder, the change is marked unsaved
    assert not settings_file.parent.exists()
    assert text(app.unsaved_label) == "● Not saved"
    second = open_window(capsys)
    try:
        assert second.lang is Lang.RU
    finally:
        close(second)

    app.save_button.invoke()
    app.update()
    assert load_settings(settings_file).settings.language is Lang.EN
    assert text(app.status_label) == f"Settings saved: {settings_file}"
    assert text(app.unsaved_label) == ""
    third = open_window(capsys)
    try:
        assert third.lang is Lang.EN and text(third.preview_button) == "Preview"
        assert text(third.unsaved_label) == ""
    finally:
        close(third)
    click_segment(app.language_button, "RU")
    app.update()
    assert text(app.preview_button) == "Предпросмотр"
    assert text(app.unsaved_label) == "● Не сохранено"
    assert load_settings(settings_file).settings.language is Lang.EN  # until saved again
    click_segment(app.language_button, "EN")
    app.update()
    assert text(app.unsaved_label) == ""  # back to what is saved


def test_result_and_dialog_follow_the_language(
    app: gui.OrganizerApp, src: Path, asked: list[dict[str, str]]
) -> None:
    app.set_language(Lang.EN)
    preview(app, src, ".pdf")
    run(app)
    assert asked[0]["heading"] == "Move 1 files?" and asked[0]["accept"] == "Move"
    assert text(app.headline_label) == "Done: files moved — 1"
    app.set_language(Lang.RU)
    assert text(app.headline_label) == "Готово: перемещено файлов — 1"
    assert app.result_tree.heading("status")["text"] == "Итог"


# --- custom extensions ----------------------------------------------------------------------


def add(window: gui.OrganizerApp, raw: str, category_key: str) -> None:
    window.rule_entry.delete(0, "end")
    window.rule_entry.insert(0, raw)
    window._category_chosen(window._category_labels()[category_key])
    window.add_rule_button.invoke()
    window.update()


def test_adding_a_custom_extension_is_unselected_and_saved_on_request(
    app: gui.OrganizerApp, settings_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add(app, ".AVIF", "Images")
    assert text(app.rule_message_label).startswith(".avif добавлено в «")
    images = next(c for c in app.cards if c.key == "Images")
    assert [text(b) for b in images.boxes].count(".avif *") == 1
    assert images.vars[".avif"].get() == 0
    assert not app.model.selection.is_selected(".avif")
    assert text(app.selection_label) == "Выбрано типов: 0 из 41"
    assert not settings_file.parent.exists()  # only the editor changed
    assert text(app.unsaved_label) == "● Не сохранено"

    assert app.save_settings()
    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    assert {"extension": ".avif", "category": "Images", "enabled": True} in stored[
        "custom_extensions"
    ]
    assert text(app.unsaved_label) == ""
    second = open_window(capsys)
    try:
        assert second.model.table.category_for(".avif").key == "Images"  # type: ignore[union-attr]
        assert any(".avif" in text(w) for w in second.rule_widgets)
    finally:
        close(second)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a b", "Не добавлено «a b»"),
        (".jpg", "Не добавлено «.jpg»"),  # a built-in extension
        ("", "Не добавлено «»"),
        (".toolongextension1", "Не добавлено «.toolongextension1»"),
    ],
)
def test_invalid_or_duplicate_extensions_are_refused(
    app: gui.OrganizerApp, settings_file: Path, raw: str, expected: str
) -> None:
    add(app, raw, "Images")
    assert text(app.rule_message_label).startswith(expected)
    assert app.user.rules == () and not settings_file.exists()


def test_one_extension_never_has_two_categories(app: gui.OrganizerApp) -> None:
    add(app, ".heic", "Images")
    add(app, "heic", "Office")
    assert text(app.rule_message_label).startswith("Не добавлено «heic»")
    assert [(r.extension, r.category) for r in app.user.rules] == [(".heic", "Images")]
    app._rule_switched(".heic")  # disabled, still the extension's one rule
    add(app, ".heic", "Office")
    assert text(app.rule_message_label).startswith("Не добавлено «.heic»")


def test_disable_delete_and_restore_rules(
    app: gui.OrganizerApp, settings_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(app, ".epub", "Office")
    add(app, ".jsonl", "Code")
    app._rule_switched(".epub")
    assert app.model.table.category_for(".epub") is None
    assert not app.user.rule(".epub").enabled  # type: ignore[union-attr]
    assert not settings_file.parent.exists()
    assert app.save_settings()
    rules = load_settings(settings_file).settings.rules
    assert [(r.extension, r.enabled) for r in rules] == [(".epub", False), (".jsonl", True)]
    app._rule_switched(".epub")
    assert app.model.table.category_for(".epub").key == "Office"  # type: ignore[union-attr]
    app.delete_rule(".jsonl")
    assert [r.extension for r in app.user.rules] == [".epub"]
    assert load_settings(settings_file).settings.rules == rules  # not saved yet

    answers = iter((False, True))
    monkeypatch.setattr(gui, "ask_choice", lambda _m, **_t: next(answers))
    app.restore_defaults()  # declined
    assert len(app.user.rules) == 1
    app.restore_defaults()
    assert app.user.rules == () and dict(app.model.table.custom) == {}
    assert state(app.restore_button) == "disabled"
    assert load_settings(settings_file).settings.rules == rules
    assert app.save_settings()
    assert load_settings(settings_file).settings.rules == ()
    assert text(app.unsaved_label) == ""


def test_a_rule_change_makes_the_preview_stale(app: gui.OrganizerApp, src: Path) -> None:
    preview(app, src, ".pdf")
    assert state(app.execute_button) == "normal"
    add(app, ".avif", "Images")
    assert app.model.preview is None and state(app.execute_button) == "disabled"
    assert text(app.headline_label).startswith("Настройки изменились")


def test_rules_cannot_change_during_a_run(
    app: gui.OrganizerApp,
    src: Path,
    asked: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    def slow(_plan: object, _table: object, _cancelled: Callable[[], bool]) -> RunOutcome:
        release.wait(10)
        return RunOutcome()

    monkeypatch.setattr(gui, "execute", slow)
    preview(app, src, ".pdf")
    app.confirm_and_run()
    assert app.running and state(app.add_rule_button) == "disabled"
    assert state(app.save_button) == "disabled"
    add(app, ".avif", "Images")
    assert app.user.rules == ()
    release.set()
    pump(app, idle(app))
    assert state(app.add_rule_button) == "normal"


# --- settings.json --------------------------------------------------------------------------


def test_corrupt_settings_use_defaults_and_are_kept(
    settings_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings_file.parent.mkdir(parents=True)
    settings_file.write_bytes(b"{not json")
    window = open_window(capsys)
    try:
        assert window.lang is Lang.RU and window.user.rules == ()
        assert text(window.status_label).startswith("Файл настроек повреждён.")
        assert settings_file.read_bytes() == b"{not json"  # reading never changes it
        add(window, ".avif", "Images")
        assert settings_file.read_bytes() == b"{not json"  # an edit is no save
        window.save_button.invoke()  # the explicit save: written, the old file kept
        window.update()
        backups = list(settings_file.parent.glob("settings.json.bak-*"))
        assert len(backups) == 1 and backups[0].read_bytes() == b"{not json"
        assert text(window.status_label) == (
            f"Настройки сохранены: {settings_file}. "
            f"Прежний файл оставлен для проверки: {backups[0]}"
        )
        assert load_settings(settings_file).settings.rules[0].extension == ".avif"
    finally:
        close(window)


def test_a_failed_save_is_an_error_and_stays_unsaved(
    app: gui.OrganizerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOCALAPPDATA")
    app.store = SettingsStore()
    app.set_language(Lang.EN)
    assert app.lang is Lang.EN  # the change lasts for this session
    assert not app.save_settings()
    assert text(app.status_label) == (
        "The settings were NOT saved: LOCALAPPDATA is not set. The settings file is unchanged."
        " The changes apply only in this window."
    )
    assert app._status[1] is gui.Level.ERROR
    assert text(app.unsaved_label) == "● Not saved"


def test_a_failed_write_names_what_is_on_the_disk(
    app: gui.OrganizerApp, settings_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(app, ".avif", "Images")
    assert app.save_settings()
    before = settings_file.read_bytes()

    def broken(*_args: object) -> None:
        raise OSError("injected failure")

    monkeypatch.setattr(settingsio, "write_all", broken)
    add(app, ".heic", "Images")
    assert not app.save_settings()
    assert text(app.status_label) == (
        "Настройки НЕ сохранены: не удалось записать временный файл (OSError: injected failure)."
        " Файл настроек не изменён. Изменения действуют только в этом окне."
    )
    assert app._status[1] is gui.Level.ERROR and app.unsaved
    assert settings_file.read_bytes() == before
    assert sorted(p.name for p in settings_file.parent.iterdir()) == [
        "settings.json",
        "settings.json.lock",
    ]


@pytest.mark.parametrize("answer", [True, False], ids=["load", "keep"])
def test_a_file_changed_elsewhere_is_kept_and_can_be_loaded(
    app: gui.OrganizerApp,
    src: Path,
    settings_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: bool,
) -> None:
    add(app, ".avif", "Images")
    assert app.save_settings()
    other = SettingsStore()  # another window of the program saves meanwhile
    other.load()
    other.save(UserSettings(Lang.EN).with_rule(".heic", "Images"))
    before = settings_file.read_bytes()
    add(app, ".epub", "Office")
    preview(app, src, ".pdf")
    questions: list[dict[str, str]] = []

    def ask(_master: object, **texts: str) -> bool:
        questions.append(texts)
        return answer

    monkeypatch.setattr(gui, "ask_choice", ask)
    assert not app.save_settings()
    assert settings_file.read_bytes() == before  # never overwritten
    assert [q["accept"] for q in questions] == ["Загрузить из файла"]
    assert questions[0]["cancel"] == "Оставить мои изменения"
    if answer:
        assert app.lang is Lang.EN
        assert app.user == UserSettings(Lang.EN).with_rule(".heic", "Images")
        assert app.model.table.category_for(".epub") is None
        assert app.model.preview is None and state(app.execute_button) == "disabled"
        assert text(app.status_label) == "The settings were loaded from the file again."
        assert not app.unsaved
        assert app.save_settings()  # the file is the baseline again
    else:
        assert app.lang is Lang.RU and app.user.rule(".epub") is not None
        assert text(app.status_label).startswith(
            "Настройки НЕ сохранены: файл настроек изменён после загрузки"
        )
        assert app.unsaved and text(app.unsaved_label) == "● Не сохранено"


# --- copying paths ----------------------------------------------------------------------


@pytest.mark.usefixtures("clipboard")
def test_copy_the_journal_path_of_a_result(
    app: gui.OrganizerApp, src: Path, asked: list[dict[str, str]]
) -> None:
    preview(app, src, ".pdf")
    run(app)
    assert shown(app.journal_bar)
    journal = app.report.journal_path  # type: ignore[union-attr]
    assert journal is not None
    before = hashlib.sha256(journal.read_bytes()).hexdigest()
    app.copy_result_button.invoke()
    assert app.clipboard_get() == str(journal)
    assert text(app.status_label) == "Путь скопирован"
    assert hashlib.sha256(journal.read_bytes()).hexdigest() == before


@pytest.mark.usefixtures("clipboard")
def test_copy_a_path_in_the_journals_area(
    app: gui.OrganizerApp, src: Path, asked: list[dict[str, str]]
) -> None:
    preview(app, src, ".pdf")
    run(app)
    app.show_area(gui.AREA_JOURNALS)
    pump(app, lambda: app.journal_task is None)
    assert state(app.copy_journal_button) == "disabled"  # nothing is selected yet
    (row,) = app.journal_tree.get_children()
    app.journal_tree.selection_set(row)
    app.update()
    journal = app.journals[0].path
    before = journal.read_bytes(), journal.stat().st_mtime_ns
    app.copy_journal_button.invoke()
    assert app.clipboard_get() == str(journal)
    assert text(app.status_label) == "Путь скопирован"
    assert (journal.read_bytes(), journal.stat().st_mtime_ns) == before


def test_an_unavailable_clipboard_is_reported(
    app: gui.OrganizerApp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise tk.TclError("CLIPBOARD selection doesn't exist")

    monkeypatch.setattr(app, "clipboard_clear", broken)
    app.copy_path(tmp_path / "x.jsonl")
    assert text(app.status_label).startswith("Не удалось скопировать путь")
    assert window_exists(app)


# --- runs, cancel and close ---------------------------------------------------------------


def test_closing_during_a_run_cancels_and_waits(
    app: gui.OrganizerApp,
    src: Path,
    asked: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()
    seen: list[bool] = []

    def slow(_plan: object, _table: object, cancelled: Callable[[], bool]) -> RunOutcome:
        started.set()
        release.wait(10)
        seen.append(cancelled())
        return RunOutcome()

    monkeypatch.setattr(gui, "execute", slow)
    preview(app, src, ".pdf")
    app.confirm_and_run()
    assert started.wait(10) and app.running
    assert state(app.preview_button) == "disabled" and state(app.cancel_button) == "normal"
    assert state(app.language_button) == "normal"  # the language can still be switched
    app.on_close()
    assert asked[-1]["accept"] == "Отменить и закрыть"
    assert asked[-1]["cancel"] == "Продолжить перемещение"
    assert app.close_pending and window_exists(app)  # still open: the run is not cut off
    app.update()
    assert window_exists(app)
    release.set()
    pump(app, lambda: not window_exists(app))
    assert seen == [True]


@pytest.mark.parametrize("answer", [True, False])
def test_run_ending_while_close_is_asked(
    app: gui.OrganizerApp,
    src: Path,
    asked: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    answer: bool,
) -> None:
    """The run ends while «Идёт перемещение» is open: the window's own loop takes the result."""
    release = threading.Event()

    def slow(_plan: object, _table: object, _cancelled: Callable[[], bool]) -> RunOutcome:
        release.wait(10)
        return RunOutcome()

    def ask(_master: object, **_texts: str) -> bool:
        release.set()
        pump(app, lambda: app.task is None)  # poll() runs inside the dialog's event loop
        return answer

    monkeypatch.setattr(gui, "execute", slow)
    preview(app, src, ".pdf")
    app.confirm_and_run()
    assert app.running
    monkeypatch.setattr(gui, "ask_choice", ask)
    app.on_close()
    if answer:  # the user wanted to close and nothing is running any more
        assert not window_exists(app)
    else:
        assert window_exists(app) and not app.close_pending
        assert app.view == gui.VIEW_RESULT


def test_close_while_a_dialog_is_open_answers_nothing(app: gui.OrganizerApp, src: Path) -> None:
    preview(app, src, ".pdf")
    closed: list[bool] = []

    def try_close_then_cancel(dialog: gui.ChoiceDialog) -> None:
        app.on_close()  # the main window's [x] while the question is open
        closed.append(window_exists(app))
        dialog.cancel_button.invoke()

    seen = answer_dialog(app, try_close_then_cancel)
    app.confirm_and_run()
    assert len(seen) == 1 and closed == [True]
    assert window_exists(app) and (src / "a.pdf").is_file()


def test_closing_without_changes_asks_nothing_and_writes_nothing(
    app: gui.OrganizerApp, isolated_user_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gui, "ask_choices", lambda *a, **k: pytest.fail("asked"))
    app.set_language(Lang.EN)
    app.set_language(Lang.RU)  # back as it was: nothing to save
    app.on_close()
    assert not window_exists(app)
    assert not (isolated_user_dirs / "AppData" / "Local" / "DownloadsOrganizer").exists()


@pytest.mark.parametrize("choice", [gui.ACCEPT, gui.OTHER, None], ids=["save", "discard", "cancel"])
def test_closing_with_unsaved_settings_asks(
    app: gui.OrganizerApp,
    settings_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    choice: str | None,
) -> None:
    add(app, ".avif", "Images")
    seen: list[dict[str, object]] = []

    def ask(_master: object, **texts: object) -> str | None:
        seen.append(texts)
        return choice

    monkeypatch.setattr(gui, "ask_choices", ask)
    app.on_close()
    assert len(seen) == 1
    assert (seen[0]["accept"], seen[0]["other"], seen[0]["cancel"]) == (
        "Сохранить",
        "Не сохранять",
        "Отмена",
    )
    assert seen[0]["heading"] == "Сохранить изменения настроек?"
    if choice == gui.ACCEPT:
        assert not window_exists(app)
        assert [r.extension for r in load_settings(settings_file).settings.rules] == [".avif"]
    elif choice == gui.OTHER:
        assert not window_exists(app) and not settings_file.parent.exists()
    else:
        assert window_exists(app) and app.unsaved and not settings_file.parent.exists()


def test_a_failed_save_on_close_keeps_the_window(
    app: gui.OrganizerApp, settings_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(app, ".avif", "Images")
    monkeypatch.setattr(gui, "ask_choices", lambda _m, **_t: gui.ACCEPT)
    monkeypatch.setattr(gui, "ask_choice", lambda *a, **k: pytest.fail("reload offered"))

    def broken(*_args: object) -> None:
        raise OSError("injected failure")

    monkeypatch.setattr(settingsio, "write_all", broken)
    app.on_close()
    assert window_exists(app) and app.unsaved
    assert text(app.status_label).startswith("Настройки НЕ сохранены: ")
    assert not settings_file.exists()


def test_cancel_button_requests_cancel(
    app: gui.OrganizerApp,
    src: Path,
    asked: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    seen: list[bool] = []

    def slow(_plan: object, _table: object, cancelled: Callable[[], bool]) -> RunOutcome:
        release.wait(10)
        seen.append(cancelled())
        return RunOutcome()

    monkeypatch.setattr(gui, "execute", slow)
    preview(app, src, ".pdf")
    app.confirm_and_run()
    assert app.task is not None and app.task.kind is TaskKind.RUN
    app.cancel_run()
    assert state(app.cancel_button) == "disabled"
    release.set()
    pump(app, idle(app))
    assert seen == [True] and window_exists(app)


def test_a_crashed_run_is_shown(
    app: gui.OrganizerApp,
    src: Path,
    asked: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(_plan: object, _table: object, _cancelled: Callable[[], bool]) -> RunOutcome:
        raise RuntimeError("boom")

    monkeypatch.setattr(gui, "execute", crash)
    preview(app, src, ".pdf")
    run(app)
    assert app.view == gui.VIEW_RESULT
    assert text(app.headline_label) == "Выполнение прервано ошибкой"
    assert not app.model.running and state(app.preview_button) == "normal"


# --- journals -----------------------------------------------------------------------------


def test_journals_area_lists_without_creating(
    app: gui.OrganizerApp, isolated_user_dirs: Path
) -> None:
    click_segment(app.area_button, "Журналы")
    pump(app, lambda: app.journal_task is None)
    assert app.area == gui.AREA_JOURNALS
    assert app.journal_tree.get_children() == ()
    assert "журналов: 0" in text(app.journals_label)
    assert not (isolated_user_dirs / "AppData" / "Local" / "DownloadsOrganizer").exists()


# --- layout -------------------------------------------------------------------------------


def inside(window: gui.OrganizerApp, widget: tk.Misc) -> bool:
    """``widget`` is shown entirely within the window's client area."""
    left, top = window.winfo_rootx(), window.winfo_rooty()
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    return (
        widget.winfo_ismapped()
        and x >= left
        and y >= top
        and x + widget.winfo_width() <= left + window.winfo_width()
        and y + widget.winfo_height() <= top + window.winfo_height()
    )


@pytest.mark.parametrize("widget_scaling", [1.0, 1.2])
def test_compact_window_keeps_the_main_buttons_visible(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, widget_scaling: float
) -> None:
    """760x560 (CTk units) at the system scaling and at 1.2 times it (125% → 150%)."""
    ctk.set_widget_scaling(widget_scaling)
    window = open_window(capsys)
    try:
        window.geometry("760x560")
        window.source_var.set(str(tmp_path / ("очень_длинное_имя_папки_" * 8)))
        window.set_status(lambda _lang: "Длинное сообщение о состоянии " * 12)
        labels = (window.effective_label, window.status_label)

        def fit() -> bool:  # the text (inner Tk label) is not wider than the place it got
            return all(lb._label.winfo_reqwidth() <= lb.winfo_width() for lb in labels)

        pump(window, fit, timeout=5)
        buttons = (
            window.preview_button,
            window.execute_button,
            window.cancel_button,
            window.language_button,
        )
        pump(window, lambda: all(inside(window, b) for b in buttons), timeout=5)
        for lang in (Lang.EN, Lang.RU):
            window.set_language(lang)
            pump(window, lambda: all(inside(window, b) for b in buttons), timeout=5)
        window.show_area(gui.AREA_TYPES)
        pump(window, lambda: window._columns == 1, timeout=5)
        assert inside(window, window.add_rule_button)
    finally:
        close(window)
        ctk.set_widget_scaling(1.0)


def test_cards_use_two_columns_in_a_wide_window(app: gui.OrganizerApp) -> None:
    app.show_area(gui.AREA_TYPES)
    app.geometry("760x560")
    pump(app, lambda: app._columns == 1, timeout=5)
    app.geometry("1200x720")
    pump(app, lambda: app._columns == 2, timeout=5)


# --- «Сортировка»: what the folder holds, «Подробности» (R4-D) -------------------------


def test_preview_says_how_many_files_there_are_and_why_some_are_left(
    app: gui.OrganizerApp, src: Path
) -> None:
    touch(src, "s1.ps1", "s2.ps1")
    touch(src / "sub", "inner.pdf")
    preview(app, src, ".pdf", ".jpg")
    assert text(app.counts_label) == (
        "Файлов в папке: 5. В плане: 2. Вне плана: 3."
        " Будет перемещено: 2 (с новым именем: 0). Пропущено: 0."
    )  # «В плане» once
    assert shown(app.note_label)
    assert text(app.note_label).splitlines()[:2] == [
        "Нет правила: .ps1 — 2.",
        "Тип не выбран: .txt — 1.",
    ]
    # the technical details only on request; a row's details on selection
    assert work_text(app) == "Выберите строку, чтобы увидеть полный путь файла."
    assert text(app.details_button) == "Подробности ▸"
    app.details_button.invoke()
    assert text(app.details_button) == "Подробности ▾"
    details = work_text(app)
    assert f"Откуда: {src}" in details
    assert "Вложенных папок: 1 — они не открываются и не перемещаются." in details
    assert "  .ps1 — 2" in details and "  .txt — 1 (тип не выбран)" in details
    app.details_button.invoke()
    assert "Откуда:" not in work_text(app)
    first = app.preview_tree.get_children()[0]
    app.preview_tree.selection_set(first)
    app.update()
    assert work_text(app).startswith("Файл: ")


def test_no_note_when_every_file_is_in_the_plan(app: gui.OrganizerApp, tmp_path: Path) -> None:
    folder = tmp_path / "only"
    touch(folder, "a.pdf", "b.jpg")
    preview(app, folder, ".pdf", ".jpg")
    assert text(app.counts_label).startswith("Файлов в папке: 2. В плане: 2. Вне плана: 0.")
    assert not shown(app.note_label)


def test_long_paths_are_shortened_and_complete_in_the_details(
    app: gui.OrganizerApp, tmp_path: Path
) -> None:
    folder = tmp_path / ("очень_длинное_имя_папки_" * 6) / "Downloads"
    touch(folder, "a.pdf")
    preview(app, folder, ".pdf")
    where = str(folder / "DEL" / "<категория>")
    assert len(where) > gui.WHERE_LIMIT
    label = text(app.effective_label)
    assert where not in label and "…" in label
    assert len(label) <= len("Файлы будут разложены в: ") + gui.WHERE_LIMIT
    app.details_button.invoke()
    assert f"Куда: {where}" in work_text(app)


def test_the_result_says_how_many_stayed_outside_the_plan(
    app: gui.OrganizerApp, src: Path, asked: list[dict[str, str]]
) -> None:
    preview(app, src, ".pdf", ".jpg")
    run(app)
    assert text(app.counts_label) == (
        "Перемещено: 2. Пропущено: 0. Ошибки: 0. Вне плана (при предпросмотре): 1."
    )
    assert not shown(app.note_label) and not shown(app.repreview_button)
    assert (src / "notes.txt").is_file()
    # the journal line is below the note line, not on it
    assert shown(app.journal_bar) and app.journal_bar.grid_info()["row"] == 5


def test_files_changed_after_the_preview_are_explained_once(
    app: gui.OrganizerApp, src: Path, asked: list[dict[str, str]]
) -> None:
    touch(src, "c.pdf", "d.pdf")
    preview(app, src, ".pdf", ".jpg")
    for name in ("a.pdf", "c.pdf"):
        (src / name).write_bytes(b"changed after the preview, longer than before")
    run(app)
    note = text(app.note_label)
    assert shown(app.note_label) and note.startswith("Изменились после предпросмотра: 2.")
    assert "«Обновить предпросмотр оставшихся»" in note
    assert "Изменились после предпросмотра" not in work_text(app)  # not twice
    assert text(app.repreview_button) == "Обновить предпросмотр оставшихся"
    app.repreview_button.invoke()
    pump(app, idle(app))
    assert len(asked) == 1 and app.view == gui.VIEW_PREVIEW  # a new plan, nothing moved
    names = [app.preview_tree.set(row, "name") for row in app.preview_tree.get_children()]
    assert sorted(names) == ["a.pdf", "c.pdf"]
    assert (src / "a.pdf").is_file() and (src / "c.pdf").is_file()
    assert text(app.counts_label).startswith("Файлов в папке: 3. В плане: 2. Вне плана: 1.")


# --- «Типы файлов»: the mouse wheel, search, folding (R4-D) ------------------------------


def types_scrollable(window: gui.OrganizerApp) -> tk.Canvas:
    window.geometry("760x560")
    window.show_area(gui.AREA_TYPES)
    canvas = window.cards_frame._parent_canvas
    pump(window, lambda: canvas.yview()[1] < 1.0, timeout=5)
    return canvas


def wheel(widget: tk.Misc, notches: int = -1) -> None:
    for _ in range(abs(notches)):
        widget.event_generate("<MouseWheel>", delta=120 if notches > 0 else -120)
    widget.update()


def test_the_wheel_binding_of_all_widgets_is_the_windows_only(app: gui.OrganizerApp) -> None:
    for sequence in gui.CTK_ALL_BINDINGS[1:]:
        assert app.tk.call("bind", "all", sequence) == ""
    script = app.tk.call("bind", "all", "<MouseWheel>")
    assert script and "_types_wheel" in script
    assert str(app.cards_frame._parent_canvas.cget("yscrollincrement")) == "1"


def test_the_wheel_scrolls_the_types_wherever_the_pointer_is_in_them(
    app: gui.OrganizerApp,
) -> None:
    canvas = types_scrollable(app)
    step = int(gui.WHEEL_STEP * ctk.ScalingTracker.get_widget_scaling(app))
    card = app.cards[0]
    targets = {
        "card": card,
        "title": card.group_box,
        "count": card.count_label,
        "checkbox": card.boxes[0],
        "checkbox inside": card.boxes[0]._canvas,
        "body": card.body,
        "fold": card.fold_button,
        "custom rules": app.rules_frame,
        "between cards": app.cards_frame,
        "scrollbar": app.cards_frame._scrollbar._canvas,
    }
    for name, widget in targets.items():
        canvas.yview_moveto(0)
        app.update()
        wheel(widget, -1)
        assert canvas.canvasy(0) == step, name
        wheel(widget, +1)
        assert canvas.canvasy(0) == 0, name
    wheel(card.boxes[0], -3)
    assert canvas.canvasy(0) == 3 * step


def test_the_wheel_elsewhere_does_not_move_the_types(app: gui.OrganizerApp) -> None:
    canvas = types_scrollable(app)
    canvas.yview_moveto(0)
    app.update()
    for widget in (
        app.search_entry,
        app.rule_entry,
        app.preview_tree,
        app.work_text,
        app.journal_tree,
        app.status_label,
        app,
    ):
        assert not app.in_types_area(widget)
        wheel(widget, -2)
        assert canvas.canvasy(0) == 0, widget
    assert app.in_types_area(app.cards[-1].boxes[-1])


def test_cards_made_again_get_the_wheel_too(app: gui.OrganizerApp) -> None:
    canvas = types_scrollable(app)
    app.set_language(Lang.EN)  # the cards are drawn anew
    pump(app, lambda: canvas.yview()[1] < 1.0, timeout=5)
    canvas.yview_moveto(0)
    app.update()
    wheel(app.cards[1].boxes[0], -1)
    assert canvas.canvasy(0) > 0


def test_the_wheel_binding_is_removed_with_the_window(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    window = open_window(capsys)
    left: list[str] = []
    real = ctk.CTk.destroy

    def spy(self: ctk.CTk) -> None:
        left.append(self.tk.call("bind", "all", "<MouseWheel>"))
        real(self)

    monkeypatch.setattr(ctk.CTk, "destroy", spy)
    window.destroy()
    assert left == [""]


def found_cards(window: gui.OrganizerApp) -> list[str]:
    return [card.key for card in window.cards if card.found and shown(card)]


def search(window: gui.OrganizerApp, raw: str) -> None:
    window.search_entry.delete(0, "end")
    window.search_entry.insert(0, raw)
    window.apply_search()
    window.update()


def test_search_shows_the_matching_categories_and_keeps_the_selection(
    app: gui.OrganizerApp,
) -> None:
    app.show_area(gui.AREA_TYPES)
    app.change(lambda m: m.set_extension(".pdf", True))
    app.change(lambda m: m.set_extension(".bmp", True))
    selected = app.model.settings().selected
    search(app, "mp")
    assert found_cards(app) == ["Images", "Video", "Audio"]  # .bmp, .mp4, .mp3
    assert text(app.search_label) == "Найдено категорий: 3" and shown(app.search_label)
    search(app, ".MP")  # with the dot: the extension starts with it; case does not matter
    assert found_cards(app) == ["Video", "Audio"]
    search(app, "аудио")  # the name of the category
    assert found_cards(app) == ["Audio"]
    search(app, ".wmv")  # a rare extension: shown, not hidden behind «ещё»
    (video,) = [c for c in app.cards if c.key == "Video"]
    assert found_cards(app) == ["Video"] and video.expanded and shown(video.more_frame)
    search(app, ".ps1")
    assert found_cards(app) == []
    assert text(app.search_label) == (
        "Расширения «.ps1» нет в таблице — его можно добавить как своё правило."
    )
    assert shown(app.rules_frame) and app.rules_frame.grid_info()["row"] == 0
    assert app.model.settings().selected == selected
    app.clear_search()
    app.update()
    assert len(found_cards(app)) == 10 and not shown(app.search_label)
    assert app.search_entry.get() == "" and app.model.settings().selected == selected
    assert text(app.selection_label) == "Выбрано типов: 2 из 40"


def test_escape_in_the_search_clears_it(app: gui.OrganizerApp) -> None:
    app.show_area(gui.AREA_TYPES)
    search(app, "pdf")
    assert found_cards(app) == ["PDF"]
    app.search_entry._entry.focus_force()
    app.update()
    app.search_entry._entry.event_generate("<Escape>")
    app.update()
    assert app.search_entry.get() == "" and len(found_cards(app)) == 10


def test_typing_in_the_search_filters_at_once(app: gui.OrganizerApp) -> None:
    app.show_area(gui.AREA_TYPES)
    app.search_entry.insert(0, "torrent")
    app.search_entry._entry.focus_force()
    app.update()
    app.search_entry._entry.event_generate("<KeyRelease>")
    app.update()
    assert found_cards(app) == ["Torrent"]


def test_folding_a_card_keeps_its_selection_and_a_search_unfolds_it(
    app: gui.OrganizerApp,
) -> None:
    app.show_area(gui.AREA_TYPES)
    (audio,) = [c for c in app.cards if c.key == "Audio"]
    audio.boxes[0].toggle()
    assert app.model.selection.is_selected(".mp3")
    count = text(audio.count_label)
    audio.fold_button.invoke()
    app.update()
    assert audio.folded and not shown(audio.body) and text(audio.fold_button) == "▸"
    assert app.model.selection.is_selected(".mp3") and text(audio.count_label) == count
    search(app, ".mp3")
    assert shown(audio.body)  # the found extension is visible
    app.clear_search()
    app.update()
    assert not shown(audio.body)
    audio.fold_button.invoke()
    app.update()
    assert shown(audio.body) and text(audio.fold_button) == "▾"
    assert app.model.selection.is_selected(".mp3")


# --- «Журналы»: date, operation, result, files (R4-D) ------------------------------------


def test_journal_list_shows_when_what_result_and_how_many_files(
    app: gui.OrganizerApp, src: Path, asked: list[dict[str, str]]
) -> None:
    touch(src, "c.pdf")
    preview(app, src, ".pdf", ".jpg")
    (src / "c.pdf").write_bytes(b"changed after the preview, longer than before")
    run(app)
    app.show_area(gui.AREA_JOURNALS)
    pump(app, lambda: app.journal_task is None)
    columns = ("time", "kind", "result", "files")
    assert tuple(app.journal_tree.cget("columns")) == columns
    headings = [app.journal_tree.heading(c)["text"] for c in columns]
    assert headings == ["Время", "Операция", "Результат", "Файлов"]
    (row,) = app.journal_tree.get_children()
    assert app.journal_tree.set(row, "files") == "2 из 3"
    assert app.journal_tree.set(row, "kind") and app.journal_tree.set(row, "result")
    journal = app.journals[0].path
    assert str(journal) not in " ".join(map(str, app.journal_tree.item(row, "values")))
    app.journal_tree.selection_set(row)
    app.update()
    assert str(journal) in app.journal_text.get("1.0", "end")  # the full path on selection
    assert state(app.copy_journal_button) == "normal"


def freed_by_another_thread(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Run the cycle collector in a thread of its own: the Tk variables it frees there.

    Their ``__del__`` would call Tk from that thread; the spy only records the name.
    """
    freed: list[str] = []
    with monkeypatch.context() as patch:
        patch.setattr(tk.Variable, "__del__", lambda self: freed.append(self._name))
        collector = threading.Thread(target=gc.collect)
        collector.start()
        collector.join()
    return freed


def test_destroyed_cards_are_collected_on_the_tk_thread(
    app: gui.OrganizerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    gc.collect()  # what earlier windows left
    gc.disable()  # only the window's own collection counts, not one that happens to run
    try:
        app.rule_category_key = "Code"
        app.rule_entry.insert(0, ".gs")
        app.add_rule()  # cards and custom rules are drawn anew
    finally:
        gc.enable()
    assert app.model.table.category_for(".gs") is not None  # the rule was added
    assert freed_by_another_thread(monkeypatch) == []


def test_a_worker_starts_with_no_tk_garbage_left_for_it(
    app: gui.OrganizerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    gc.collect()  # what earlier windows left
    cycle: list[object] = [tk.BooleanVar(app)]
    cycle.append(cycle)  # a Tk variable that only the cycle collector frees
    del cycle
    app.load_journals()
    assert freed_by_another_thread(monkeypatch) == []
    pump(app, lambda: app.journal_task is None)
