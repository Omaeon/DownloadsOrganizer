"""The DownloadsOrganizer window (CustomTkinter, R4-A, R4-C).

Everything the window decides comes from ``appmodel``; this module only draws it and
forwards clicks. Work runs in ``appmodel.BackgroundTask`` threads, which never touch a
widget: their results arrive through a queue that the Tk event loop polls (``after``).

Layout: a compact top bar (folders, RU/EN), three areas («Сортировка», «Типы файлов»,
«Журналы») and a footer with the one status line and the main buttons, which stay
visible at any window size.

Order of use: folders and file types → «Предпросмотр» (changes nothing) →
«Переместить…» (only for a fresh, error-free preview, after an explicit confirmation in
``ChoiceDialog``, every time) → result. Any change of a setting, the custom extensions
included, makes the preview stale and execution impossible until the next preview.
Closing the window during a run asks to cancel it and waits for the run to stop after
the current file; it never cuts a move short.

«Сортировка» shows the counts of the folder (all files, in the plan, outside the plan by
extension) under the headline; the full paths, the volume check and what was counted are
under «Подробности». «Типы файлов» scrolls with the mouse wheel anywhere over the cards
and the custom extensions (one handler of the window, see ``_types_wheel``), has a search
by extension and cards that fold to their title; neither changes the selection.

Texts are made when they are drawn, in the language of the window: switching RU/EN
redraws everything from the stored state (the preview, the run outcome, the journals)
and changes no setting of the sorter. The language and the custom extensions are written
to ``settings.json`` (``usersettings``) only by «Сохранить настройки» or by «Сохранить»
when the window is closed with unsaved changes; switching the language or editing a rule
changes only the window (and a rule change makes the preview stale at once). A refused or
failed save is shown as an error with what is on the disk; a file changed by someone else
is never overwritten, the user may load it instead.
"""

from __future__ import annotations

import contextlib
import gc
import os
import queue
import sys
import time
import tkinter as tk
import traceback
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import customtkinter as ctk

from downloads_organizer import APP_NAME, __version__, i18n
from downloads_organizer.appmodel import (
    AppModel,
    BackgroundTask,
    JournalListing,
    JournalSummary,
    Level,
    PreviewResult,
    RunOutcome,
    RunReport,
    TaskDone,
    TaskKind,
    build_preview,
    category_label,
    execute,
    format_time,
    journal_details,
    journal_directory,
    journal_kind_text,
    journal_result_text,
    list_journals,
    run_report,
    shorten_middle,
)
from downloads_organizer.catalog import Category
from downloads_organizer.executor import ItemStatus
from downloads_organizer.extensions import ExtensionError
from downloads_organizer.i18n import Lang, pick, tr
from downloads_organizer.models import CollisionPolicy, Inventory
from downloads_organizer.selection import SelectionState
from downloads_organizer.usersettings import (
    FATAL_PROBLEMS,
    LoadResult,
    SaveError,
    SaveFailure,
    SettingsProblem,
    SettingsStore,
    UserSettings,
)
from downloads_organizer.volumeproof import display_text

# the one icon of the program: the EXE (DownloadsOrganizer.spec), every window, the taskbar
APP_ICON = Path(__file__).resolve().parent / "assets" / "DownloadsOrganizer.ico"
POLL_MS = 100
TWO_COLUMNS_MIN_WIDTH = 720  # categories area width (unscaled pixels) for two card columns
EXTENSIONS_PER_ROW = 4
NAME_LIMIT = 48  # characters of a file name in a table cell (the full name is in the details)
PATH_LIMIT = 72  # characters of a path in a table cell or a label
WHERE_LIMIT = 110  # characters of «Файлы будут разложены в» (the full path is in the details)
COPIED_CLEAR_MS = 3000  # «Путь скопирован» disappears after this
DIALOG_MIN_LIFE_MS = 300  # see ChoiceDialog._finish
WHEEL_STEP = 60  # unscaled pixels per wheel notch (a delta of 120) over «Типы файлов»
# what CTkScrollableFrame binds on the tag «all» (every widget of the window): replaced
CTK_ALL_BINDINGS = (
    "<MouseWheel>",
    "<KeyPress-Shift_L>",
    "<KeyPress-Shift_R>",
    "<KeyRelease-Shift_L>",
    "<KeyRelease-Shift_R>",
)

AREA_SORT, AREA_TYPES, AREA_JOURNALS = "sort", "types", "journals"
AREAS = (AREA_SORT, AREA_TYPES, AREA_JOURNALS)
VIEW_EMPTY, VIEW_PREVIEW, VIEW_RESULT = "empty", "preview", "result"
FILTER_ALL, FILTER_NOT_MOVED = "all", "not_moved"
LANG_LABELS = {Lang.RU: "RU", Lang.EN: "EN"}
POLICY_KEYS = {CollisionPolicy.RENAME: "gui.policy.rename", CollisionPolicy.SKIP: "gui.policy.skip"}
CUSTOM_MARK = " *"
ACCEPT, OTHER = "accept", "other"  # answers of a ChoiceDialog; None is «no»
# a save refused because the file is not the one loaded: loading it again is offered
RELOAD_FAILURES = frozenset({SaveFailure.CONFLICT, SaveFailure.NOT_READ})

# (column id, title key, width, stretch)
Columns = tuple[tuple[str, str, int, bool], ...]
PREVIEW_COLUMNS: Columns = (
    ("name", "gui.col.file", 220, True),
    ("category", "gui.col.category", 160, False),
    ("action", "gui.col.action", 190, False),
    ("reason", "gui.col.reason", 240, True),
    ("target", "gui.col.target", 300, True),
)
RESULT_COLUMNS: Columns = (
    ("name", "gui.col.file", 220, True),
    ("status", "gui.col.status", 150, False),
    ("reason", "gui.col.reason", 300, True),
    ("target", "gui.col.target", 300, True),
)
JOURNAL_COLUMNS: Columns = (
    ("time", "gui.col.time", 150, False),
    ("kind", "gui.col.operation", 150, False),
    ("result", "gui.col.result", 380, True),
    ("files", "gui.col.files", 110, False),
)

LEVEL_COLORS = {
    Level.OK: ("#1e7b34", "#5fd07a"),
    Level.WARNING: ("#9a6700", "#e3b341"),
    Level.ERROR: ("#b42318", "#ff7b72"),
}
TEXT_COLOR = ("#1d1d1d", "#e6e6e6")
MUTED = ("#5f6670", "#9aa4ae")
LINK = ("#1f6aa5", "#5fa8e8")
DANGER = (("#b42318", "#9e2a22"), ("#8f1c13", "#7d201a"))
MOVE = (("#2f7d32", "#2e7d32"), ("#25632a", "#246428"))

Render = Callable[[Lang], str]


def _outline(**options: object) -> dict[str, object]:
    """Options of a secondary (outlined) button."""
    return {"fg_color": "transparent", "border_width": 1, "text_color": TEXT_COLOR, **options}


def _set_icon(window: ctk.CTk | ctk.CTkToplevel, *, default: bool = False) -> None:
    """Show ``APP_ICON`` on ``window`` (and, with ``default``, on the windows made later).

    A CTk window that sets no icon gets CustomTkinter's own a moment after it is created;
    setting it here keeps that from happening, so every window uses the same file.
    """
    if sys.platform != "win32" or not APP_ICON.is_file():
        return
    with contextlib.suppress(tk.TclError):
        if default:
            window.iconbitmap(str(APP_ICON), default=str(APP_ICON))
        else:
            window.iconbitmap(str(APP_ICON))


class ChoiceDialog(ctk.CTkToplevel):
    """A question whose safe answer is the default: closing it, Escape and Enter say «no».

    Only a click on the accept button says «yes» (``ACCEPT``); the optional ``other``
    button gives ``OTHER``. The answer is in ``choice`` (``None``: «no») and ``result``
    (``True`` for ``ACCEPT``) once the window is destroyed (``ask_choices`` waits for that).
    """

    def __init__(
        self,
        master: OrganizerApp,
        *,
        title: str,
        heading: str,
        body: str,
        details: str,
        accept: str,
        cancel: str,
        other: str = "",
        accept_color: tuple[tuple[str, str], tuple[str, str]] | None = DANGER,
    ) -> None:
        super().__init__(master)
        self.choice: str | None = None
        self.result = False
        self._born = time.monotonic()
        self._closing = False
        self._after_ids: list[str] = []
        self.title(title)
        _set_icon(self)
        self.transient(master)
        self.minsize(420, 200)
        self.grid_columnconfigure(0, weight=1)
        self.protocol("WM_DELETE_WINDOW", self.decline)

        self.heading_label = ctk.CTkLabel(
            self,
            text=heading,
            anchor="w",
            justify="left",
            wraplength=500,
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        self.heading_label.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        self.body_label = ctk.CTkLabel(self, text=body, anchor="w", justify="left", wraplength=500)
        self.body_label.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))
        self.details_box: ctk.CTkTextbox | None = None
        if details:
            self.details_box = ctk.CTkTextbox(self, width=520, height=170, wrap="word")
            self.details_box.insert("1.0", details)
            self.details_box.configure(state="disabled")  # still selectable and copyable
            self.details_box.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 8))
            self.grid_rowconfigure(2, weight=1)
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="e", padx=16, pady=(4, 14))
        self.cancel_button = ctk.CTkButton(buttons, text=cancel, width=130, command=self.decline)
        self.cancel_button.grid(row=0, column=0, padx=(0, 8))
        self.other_button: ctk.CTkButton | None = None
        if other:
            self.other_button = ctk.CTkButton(
                buttons, text=other, width=130, command=self.other, **_outline()
            )
            self.other_button.grid(row=0, column=1, padx=(0, 8))
        colors = (
            {}
            if accept_color is None
            else {"fg_color": accept_color[0], "hover_color": accept_color[1]}
        )
        self.accept_button = ctk.CTkButton(
            buttons, text=accept, width=130, command=self.accept, **colors
        )
        self.accept_button.grid(row=0, column=2)
        self.bind("<Escape>", lambda _event: self.decline())
        self.bind("<Return>", lambda _event: self.decline())
        self._place_over(master)
        self._later(50, self._take_focus)

    def _later(self, ms: int, func: Callable[[], None]) -> None:
        self._after_ids.append(self.after(ms, func))

    def _place_over(self, master: tk.Misc) -> None:
        with contextlib.suppress(tk.TclError):
            self.update_idletasks()
            width, height = self.winfo_reqwidth(), self.winfo_reqheight()
            x = master.winfo_rootx() + max(0, (master.winfo_width() - width) // 2)
            y = master.winfo_rooty() + max(0, (master.winfo_height() - height) // 3)
            self.geometry(f"+{x}+{y}")

    def _take_focus(self) -> None:
        """Grab the input once the window is shown (CTk shows it a moment after creation)."""
        if self._closing:
            return
        try:
            self.lift()
            self.focus_set()
            self.grab_set()
        except tk.TclError:  # not viewable yet
            if time.monotonic() - self._born < 2:
                self._later(50, self._take_focus)

    def accept(self) -> None:
        self._finish(ACCEPT)

    def other(self) -> None:
        self._finish(OTHER)

    def decline(self) -> None:
        self._finish(None)

    def _finish(self, choice: str | None) -> None:
        """Hide now, destroy a little later.

        CTkToplevel schedules callbacks on itself for the first moments of its life (title
        bar colour and icon) that touch the window; destroying it before they ran would
        make them fail. The answer is known at once; only the destroy waits.
        """
        if self._closing:
            return
        self._closing = True
        self.choice = choice
        self.result = choice == ACCEPT
        with contextlib.suppress(tk.TclError):
            self.grab_release()
        with contextlib.suppress(tk.TclError):
            self.withdraw()
        lived = int((time.monotonic() - self._born) * 1000)
        self.after(max(0, DIALOG_MIN_LIFE_MS - lived), self.destroy)

    def destroy(self) -> None:
        for after_id in self._after_ids:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(after_id)
        self._after_ids.clear()
        # CTk's pending «show again after the title bar colour» must not touch a dead window
        self._windows_set_titlebar_color_called = False
        super().destroy()
        collect_widget_garbage()


def collect_widget_garbage() -> None:
    """Collect destroyed widgets here, on the Tk thread.

    A destroyed CustomTkinter widget stays in reference cycles with its Tk variables.
    Left to the cycle collector, it may be freed in whatever thread allocates next, a
    worker included, and ``Variable.__del__`` would then call Tk from that thread
    (without a running mainloop: a wait and «main thread is not in main loop» for each).
    Called after widgets are destroyed and before a worker starts.
    """
    gc.collect()


def ask_choice(
    master: OrganizerApp,
    *,
    title: str,
    heading: str,
    body: str = "",
    details: str = "",
    accept: str,
    cancel: str,
) -> bool:
    """Show a ``ChoiceDialog`` and wait for it; ``True`` only for the accept button."""
    choice = ask_choices(
        master, title=title, heading=heading, body=body, details=details, accept=accept,
        cancel=cancel,
    )  # fmt: skip
    return choice == ACCEPT


def ask_choices(
    master: OrganizerApp,
    *,
    title: str,
    heading: str,
    body: str = "",
    details: str = "",
    accept: str,
    cancel: str,
    other: str = "",
    accept_color: tuple[tuple[str, str], tuple[str, str]] | None = DANGER,
) -> str | None:
    """Show a ``ChoiceDialog`` and wait for it: ``ACCEPT``, ``OTHER`` or ``None`` («no»)."""
    dialog = ChoiceDialog(
        master,
        title=title,
        heading=heading,
        body=body,
        details=details,
        accept=accept,
        cancel=cancel,
        other=other,
        accept_color=accept_color,
    )
    master.active_dialog = dialog
    try:
        master.wait_window(dialog)
    finally:
        master.active_dialog = None
    return dialog.choice


class CategoryCard(ctk.CTkFrame):
    """One category: its checkbox, its common and custom extensions, an expander for the rest.

    Custom extensions are shown with the common ones and marked with «*». The card folds
    to its title line (``toggle_folded``); a search shows the cards that match and unfolds
    them while it lasts. Neither ever changes the selection.
    """

    def __init__(
        self,
        master: tk.Misc,
        app: OrganizerApp,
        category: Category,
        expanded: bool = False,
        folded: bool = False,
    ) -> None:
        super().__init__(master, corner_radius=10)
        self.app = app
        self.category = category
        self.key = category.key
        table = app.model.table
        custom = table.custom_extensions(category.key)
        self.common = (*category.common, *custom)
        self.more = tuple(e for e in table.extensions(category.key) if e not in self.common)
        self.custom = frozenset(custom)
        self.vars: dict[str, tk.IntVar] = {}
        self.boxes: list[ctk.CTkCheckBox] = []
        self.expanded = False
        self.folded = folded
        self.found = True  # shown by the search of «Типы файлов»

        self.grid_columnconfigure(0, weight=1)
        self.group_var = tk.IntVar(value=0)
        self.group_box = ctk.CTkCheckBox(
            self,
            text="",
            variable=self.group_var,
            command=self._group_clicked,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.group_box.grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))
        self.count_label = ctk.CTkLabel(self, text="", text_color=MUTED)
        self.count_label.grid(row=0, column=1, sticky="e", padx=(12, 0), pady=(10, 4))
        self.fold_button = ctk.CTkButton(
            self,
            text="▾",
            width=28,
            height=24,
            fg_color="transparent",
            text_color=LINK,
            hover_color=("#e3e8ee", "#2b3138"),
            command=self.toggle_folded,
        )
        self.fold_button.grid(row=0, column=2, sticky="e", padx=(2, 8), pady=(10, 4))

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid_columnconfigure(0, weight=1)
        self.common_frame = ctk.CTkFrame(self.body, fg_color="transparent")
        self.common_frame.grid(row=0, column=0, sticky="ew", padx=12)
        self._add_boxes(self.common_frame, self.common)

        self.more_frame = ctk.CTkFrame(self.body, fg_color="transparent")
        self._add_boxes(self.more_frame, self.more)
        self.more_button: ctk.CTkButton | None = None
        if self.more:
            self.more_button = ctk.CTkButton(
                self.body,
                text="",
                width=10,
                height=24,
                fg_color="transparent",
                text_color=LINK,
                hover_color=("#e3e8ee", "#2b3138"),
                anchor="w",
                command=self.toggle,
            )
            self.more_button.grid(row=2, column=0, sticky="w", padx=6, pady=(0, 8))
        else:
            ctk.CTkFrame(self.body, height=6, fg_color="transparent").grid(row=2, column=0)
        if expanded:
            self.toggle()
        self.show_body()
        self.refresh()

    def _add_boxes(self, frame: ctk.CTkFrame, extensions: tuple[str, ...]) -> None:
        for index, ext in enumerate(extensions):
            var = tk.IntVar(value=0)
            box = ctk.CTkCheckBox(
                frame,
                text=ext + (CUSTOM_MARK if ext in self.custom else ""),
                variable=var,
                width=80,
                checkbox_width=20,
                checkbox_height=20,
                command=lambda e=ext: self._extension_clicked(e),
            )
            row, column = divmod(index, EXTENSIONS_PER_ROW)
            box.grid(row=row, column=column, sticky="w", padx=(0, 8), pady=3)
            self.vars[ext] = var
            self.boxes.append(box)

    def toggle(self) -> None:
        """Show or hide the rare extensions; the selection is never touched."""
        self.expanded = not self.expanded
        if self.expanded:
            self.more_frame.grid(row=1, column=0, sticky="ew", padx=12)
        else:
            self.more_frame.grid_remove()
        self.refresh()

    def toggle_folded(self) -> None:
        """Fold the card to its title line or unfold it; the selection is never touched."""
        self.folded = not self.folded
        self.show_body()

    def show_body(self) -> None:
        """The extensions are shown unless the card is folded; a search shows them anyway."""
        if self.folded and not self.app.types_query():
            self.body.grid_remove()
        else:
            self.body.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.fold_button.configure(text="▸" if self.folded else "▾")

    def search(self, query: str) -> bool:
        """Is this card shown for ``query`` (casefolded; part of an extension or the name)?

        A match among the rare extensions only shows them, so the found box is visible."""
        hit_more = bool(query) and any(extension_matches(query, e) for e in self.more)
        self.found = (
            not query
            or query in category_label(self.category, self.app.lang).casefold()
            or hit_more
            or any(extension_matches(query, e) for e in self.common)
        )
        if hit_more and not self.expanded:
            self.toggle()
        self.show_body()
        return self.found

    def _group_clicked(self) -> None:
        state = self.app.model.selection.category_summary(self.key).state
        self.app.change(lambda m: m.set_category(self.key, state is not SelectionState.ALL))

    def _extension_clicked(self, ext: str) -> None:
        self.app.change(lambda m: m.set_extension(ext, bool(self.vars[ext].get())))

    def refresh(self) -> None:
        lang = self.app.lang
        selection = self.app.model.selection
        for ext, var in self.vars.items():
            var.set(1 if selection.is_selected(ext) else 0)
        summary = selection.category_summary(self.key)
        self.group_box.configure(text=category_label(self.category, lang))
        self.group_var.set(1 if summary.state is SelectionState.ALL else 0)
        self.count_label.configure(
            text=tr("gui.card.count", lang, selected=summary.selected, total=summary.total)
        )
        if self.more_button is not None:
            hidden = sum(selection.is_selected(e) for e in self.more)
            if self.expanded:
                text = tr("gui.card.less", lang, count=len(self.more))
            elif hidden:
                text = tr("gui.card.more_selected", lang, count=len(self.more), selected=hidden)
            else:
                text = tr("gui.card.more", lang, count=len(self.more))
            self.more_button.configure(text=text)

    def set_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.group_box.configure(state=state)
        for box in self.boxes:
            box.configure(state=state)


def search_query(raw: str) -> str:
    """The search text as compared: trimmed and casefolded; «.» alone finds everything."""
    text = raw.strip().casefold()
    return "" if text in ("", ".") else text


def extension_matches(query: str, extension: str) -> bool:
    """Does ``extension`` («.pdf») match the search ``query`` (casefolded, the dot optional)?"""
    return query.lstrip(".") in extension.casefold().lstrip(".") and (
        not query.startswith(".") or extension.casefold().startswith(query)
    )


class OrganizerApp(ctk.CTk):
    def __init__(self, model: AppModel | None = None, store: SettingsStore | None = None) -> None:
        super().__init__()
        self.store = SettingsStore() if store is None else store
        loaded = self.store.load()  # reading creates nothing
        self.user: UserSettings = loaded.settings  # what the window uses
        self.saved_user: UserSettings = loaded.settings  # what settings.json holds for it
        self.lang: Lang = self.user.language
        self.model = AppModel(self.user.table()) if model is None else model
        self.events: queue.Queue[TaskDone] = queue.Queue()
        self.task: BackgroundTask | None = None  # preview or run
        self.journal_task: BackgroundTask | None = None
        self.journal_listing: JournalListing | None = None
        self.journals: tuple[JournalSummary, ...] = ()
        self.close_pending = False
        self.active_dialog: ChoiceDialog | None = None
        self.area = AREA_SORT
        self.view = VIEW_EMPTY
        self.shown_preview: PreviewResult | None = None  # the preview drawn in the work area
        self.outcome: RunOutcome | None = None  # the last run, drawn in the work area
        self.run_inventory: Inventory | None = None  # the count of the preview that was run
        self.details_open = False  # «Подробности» of «Сортировка»
        self.report: RunReport | None = None  # self.outcome in the current language
        self.work_note: tuple[str, Level | None] | None = None  # replaces the preview headline
        self.result_filter = FILTER_ALL
        self._status: tuple[Render | None, Level | None] = (None, None)
        self._rule_message: tuple[Render | None, Level | None] = (None, None)
        self._row_details: dict[str, Render] = {}
        self._texts: list[tuple[tk.Misc, str]] = []  # widgets whose text is a MESSAGES key
        self._headings: list[tuple[ttk.Treeview, Columns]] = []
        self.cards: list[CategoryCard] = []
        self.rule_widgets: list[tk.Misc] = []
        self._columns = 0
        self._wanted_columns = 2
        self._placed: tuple[str, ...] = ()  # the cards placed, in order (see _place_cards)
        self._wheel_rest = 0.0  # the part of a wheel notch not scrolled yet
        self._poll_id: str | None = None
        self._copied_id: str | None = None
        self._idle_ids: dict[Callable[[], None], str] = {}  # pending re-wraps, see _soon

        self.title(f"{APP_NAME} {__version__}")
        _set_icon(self, default=True)
        self.geometry("1000x720")
        self.minsize(760, 560)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._style_tables()
        self._build_top()
        self._build_switcher()
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 4))
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)
        self.frames = {
            AREA_SORT: self._build_sort(),
            AREA_TYPES: self._build_types(),
            AREA_JOURNALS: self._build_journals(),
        }
        self._build_footer()
        self._show_loaded(loaded)
        self.show_area(AREA_SORT)
        self.apply_language()
        self._poll_id = self.after(POLL_MS, self.poll)

    # --- small helpers ------------------------------------------------------------------

    def _t(self, widget: tk.Misc, key: str) -> tk.Misc:
        """Register ``widget`` as showing the sentence ``key`` (redrawn on a language change)."""
        self._texts.append((widget, key))
        return widget

    def _keep_wrapped(
        self,
        label: ctk.CTkLabel,
        container: ctk.CTkFrame,
        cells: tuple[int, int, int, int],
        pad: int = 0,
    ) -> Callable[[], None]:
        """Wrap ``label`` to the width of its grid ``cells`` (column, row, column2, row2).

        The cells are in a column with weight, so their width is the room left by the other
        columns, never the label's own text. ``wraplength`` is set in CTk units: CTk scales
        it itself, while ``grid_bbox`` reports screen pixels. Returns the update function
        for changes that do not resize ``container``.
        """

        def update() -> None:
            width = container.grid_bbox(*cells)[2]
            if width > 1:
                scaling = ctk.ScalingTracker.get_widget_scaling(label)
                label.configure(wraplength=max(100, int(width / scaling) - pad))

        container.bind("<Configure>", lambda _event: self._soon(update), add="+")
        return update

    def _soon(self, func: Callable[[], None]) -> None:
        """Run ``func`` once when Tk is idle (after the grid has placed everything)."""
        if func in self._idle_ids:
            return

        def run() -> None:
            self._idle_ids.pop(func, None)
            func()

        self._idle_ids[func] = self.after_idle(run)

    def _label(self, parent: tk.Misc, **options: object) -> ctk.CTkLabel:
        return ctk.CTkLabel(parent, **{"text": "", "anchor": "w", "justify": "left", **options})

    def _style_tables(self) -> None:
        dark = ctk.get_appearance_mode() == "Dark"
        bg, fg, head, select = (
            ("#2b2b2b", "#e6e6e6", "#363636", "#1f538d")
            if dark
            else ("#ffffff", "#1d1d1d", "#e8e8e8", "#cfe3f7")
        )
        self.colors = {
            "skip": "#9aa4ae" if dark else "#6b7280",
            "rename": "#5fa8e8" if dark else "#1f6aa5",
            "bad": "#ff7b72" if dark else "#b42318",
        }
        body = tkfont.nametofont("TkDefaultFont")
        row_height = body.metrics("linespace") + 10
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Organizer.Treeview",
            background=bg,
            fieldbackground=bg,
            foreground=fg,
            rowheight=row_height,
            borderwidth=0,
        )
        style.map(
            "Organizer.Treeview",
            background=[("selected", select)],
            foreground=[("selected", "#ffffff" if dark else "#000000")],
        )
        style.configure("Organizer.Treeview.Heading", background=head, foreground=fg, relief="flat")

    def _tree(self, parent: tk.Misc, columns: Columns) -> tuple[ctk.CTkFrame, ttk.Treeview]:
        """A table with scrollbars in a frame of its own (placed by the caller)."""
        holder = ctk.CTkFrame(parent, fg_color="transparent")
        holder.grid_columnconfigure(0, weight=1)
        holder.grid_rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            holder,
            columns=[c[0] for c in columns],
            show="headings",
            style="Organizer.Treeview",
            selectmode="browse",
        )
        for key, _title, width, stretch in columns:
            tree.heading(key, text="", anchor="w")
            tree.column(key, width=width, minwidth=60, stretch=stretch, anchor="w")
        vertical = ctk.CTkScrollbar(holder, orientation="vertical", command=tree.yview)
        horizontal = ctk.CTkScrollbar(holder, orientation="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        for tag, color in self.colors.items():
            tree.tag_configure(tag, foreground=color)
        self._headings.append((tree, columns))
        return holder, tree

    def _text(self, parent: tk.Misc, height: int) -> ctk.CTkTextbox:
        box = ctk.CTkTextbox(parent, height=height, wrap="word", activate_scrollbars=True)
        box.configure(state="disabled")  # read only; the text can still be selected and copied
        return box

    @staticmethod
    def _set_text(box: ctk.CTkTextbox, text: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    # --- layout -------------------------------------------------------------------------

    def _build_top(self) -> None:
        box = ctk.CTkFrame(self, corner_radius=10)
        box.grid(row=0, column=0, sticky="ew", padx=16, pady=(10, 6))
        box.grid_columnconfigure(1, weight=1)

        self.source_var = tk.StringVar()
        self.destination_var = tk.StringVar()
        self._t(self._label(box), "gui.source").grid(
            row=0, column=0, sticky="w", padx=(12, 8), pady=(8, 4)
        )
        self.source_entry = ctk.CTkEntry(box, textvariable=self.source_var)
        self.source_entry.grid(row=0, column=1, sticky="ew", pady=(8, 4))
        self.source_button = ctk.CTkButton(
            box, text="", width=90, command=lambda: self.browse(self.source_var)
        )
        self._t(self.source_button, "gui.browse").grid(row=0, column=2, padx=(8, 4), pady=(8, 4))
        self.language_button = ctk.CTkSegmentedButton(
            box, values=list(LANG_LABELS.values()), width=90, command=self._language_clicked
        )
        self.language_button.grid(row=0, column=3, padx=(4, 12), pady=(8, 4))

        self._t(self._label(box), "gui.destination").grid(
            row=1, column=0, sticky="w", padx=(12, 8), pady=(4, 8)
        )
        self.destination_entry = ctk.CTkEntry(box, textvariable=self.destination_var)
        self.destination_entry.grid(row=1, column=1, sticky="ew", pady=(4, 8))
        self.destination_button = ctk.CTkButton(
            box, text="", width=90, command=lambda: self.browse(self.destination_var)
        )
        self._t(self.destination_button, "gui.browse").grid(
            row=1, column=2, padx=(8, 4), pady=(4, 8)
        )
        self.reset_button = ctk.CTkButton(
            box, text="", width=90, command=lambda: self.destination_var.set(""), **_outline()
        )
        self._t(self.reset_button, "gui.reset").grid(row=1, column=3, padx=(4, 12), pady=(4, 8))

        self.source_var.trace_add(
            "write", lambda *_: self.change(lambda m: m.set_source(self.source_var.get()))
        )
        self.destination_var.trace_add(
            "write", lambda *_: self.change(lambda m: m.set_destination(self.destination_var.get()))
        )

    def _build_switcher(self) -> None:
        """The areas, and on the same row «Сохранить настройки» with its unsaved mark."""
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))
        bar.grid_columnconfigure(1, weight=1)
        self.area_button = ctk.CTkSegmentedButton(bar, values=["-"], command=self._area_clicked)
        self.area_button.grid(row=0, column=0, sticky="w")
        self.unsaved_label = self._label(
            bar, text_color=LEVEL_COLORS[Level.WARNING], anchor="e", justify="right"
        )
        self.unsaved_label.grid(row=0, column=1, sticky="ew", padx=(10, 8))
        self.save_button = ctk.CTkButton(
            bar, text="", width=170, command=self.save_settings, **_outline()
        )
        self._t(self.save_button, "gui.settings.save").grid(row=0, column=2)

    def _build_sort(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.content, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(6, weight=1)

        options = ctk.CTkFrame(frame, fg_color="transparent")
        options.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        options.grid_columnconfigure(2, weight=1)
        self._t(self._label(options), "gui.policy").grid(row=0, column=0, padx=(0, 8))
        self.policy_button = ctk.CTkSegmentedButton(
            options, values=["-"], command=self._policy_clicked
        )
        self.policy_button.grid(row=0, column=1, sticky="w")
        self.sort_selection_label = self._label(options, text_color=MUTED, anchor="e")
        self.sort_selection_label.grid(row=0, column=2, sticky="e", padx=8)
        self.to_types_button = ctk.CTkButton(
            options, text="", width=120, command=lambda: self.show_area(AREA_TYPES), **_outline()
        )
        self._t(self.to_types_button, "gui.to_types").grid(row=0, column=3)

        where = ctk.CTkFrame(frame, fg_color="transparent")
        where.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        where.grid_columnconfigure(0, weight=1)
        self.effective_label = self._label(where, text_color=MUTED)
        self.effective_label.grid(row=0, column=0, sticky="ew")
        self._keep_wrapped(self.effective_label, where, (0, 0, 0, 0), pad=8)
        self.details_button = ctk.CTkButton(
            where,
            text="",
            width=130,
            height=24,
            fg_color="transparent",
            text_color=LINK,
            hover_color=("#e3e8ee", "#2b3138"),
            command=self.toggle_details,
        )
        self.details_button.grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.headline_label = self._label(frame, font=ctk.CTkFont(size=14, weight="bold"))
        self.headline_label.grid(row=2, column=0, sticky="ew")
        self._keep_wrapped(self.headline_label, frame, (0, 2, 0, 2))

        bar = ctk.CTkFrame(frame, fg_color="transparent")
        bar.grid(row=3, column=0, sticky="ew", pady=(2, 4))
        bar.grid_columnconfigure(0, weight=1)
        self.counts_label = self._label(bar)
        self.counts_label.grid(row=0, column=0, sticky="ew")
        self._wrap_counts = self._keep_wrapped(self.counts_label, bar, (0, 0, 0, 0), pad=8)
        self.filter_button = ctk.CTkSegmentedButton(bar, values=["-"], command=self._filter_clicked)
        self.repreview_button = ctk.CTkButton(bar, text="", command=self.start_preview)
        self._t(self.repreview_button, "gui.repreview")

        # outside the plan (preview) or changed after the preview (result)
        self.note_label = self._label(frame, text_color=LEVEL_COLORS[Level.WARNING])
        self._wrap_note = self._keep_wrapped(self.note_label, frame, (0, 4, 0, 4))

        self.journal_bar = ctk.CTkFrame(frame, fg_color="transparent")
        self.journal_bar.grid_columnconfigure(0, weight=1)
        self.result_journal_label = self._label(self.journal_bar, text_color=MUTED)
        self.result_journal_label.grid(row=0, column=0, sticky="ew")
        self.copy_result_button = ctk.CTkButton(
            self.journal_bar,
            text="",
            width=150,
            command=lambda: self.copy_path(self.report.journal_path if self.report else None),
            **_outline(),
        )
        self._t(self.copy_result_button, "gui.copy_path").grid(row=0, column=1, padx=(8, 0))

        self.preview_holder, self.preview_tree = self._tree(frame, PREVIEW_COLUMNS)
        self.result_holder, self.result_tree = self._tree(frame, RESULT_COLUMNS)
        self.preview_holder.grid(row=6, column=0, sticky="nsew")
        self.result_holder.grid(row=6, column=0, sticky="nsew")
        self.result_holder.grid_remove()
        for tree in (self.preview_tree, self.result_tree):
            tree.bind("<<TreeviewSelect>>", lambda _event, t=tree: self._row_selected(t))
        self.work_text = self._text(frame, 96)
        self.work_text.grid(row=7, column=0, sticky="nsew", pady=(6, 0))
        return frame

    def _build_types(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.content, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        bar = ctk.CTkFrame(frame, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        bar.grid_columnconfigure(3, weight=1)
        # no textvariable: the placeholder shows; searching changes no setting
        self.search_entry = ctk.CTkEntry(bar, width=220)
        self.search_entry.grid(row=0, column=0, padx=(0, 10))
        self.search_entry.bind("<KeyRelease>", lambda _event: self.apply_search())
        self.search_entry.bind("<Escape>", lambda _event: self.clear_search())
        self.all_button = ctk.CTkButton(
            bar, text="", width=110, command=lambda: self.change(lambda m: m.set_all(True))
        )
        self._t(self.all_button, "gui.select_all").grid(row=0, column=1, padx=(0, 8))
        self.none_button = ctk.CTkButton(
            bar,
            text="",
            width=110,
            command=lambda: self.change(lambda m: m.set_all(False)),
            **_outline(),
        )
        self._t(self.none_button, "gui.select_none").grid(row=0, column=2)
        self.selection_label = self._label(bar, text_color=MUTED, anchor="e")
        self.selection_label.grid(row=0, column=3, sticky="e")
        self.search_label = self._label(bar, text_color=MUTED)
        self._keep_wrapped(self.search_label, bar, (0, 1, 3, 1))

        add = ctk.CTkFrame(frame, corner_radius=10)
        add.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        add.grid_columnconfigure(4, weight=1)
        self._t(self._label(add), "gui.rules.add_label").grid(
            row=0, column=0, padx=(12, 8), pady=(8, 4)
        )
        self.rule_entry = ctk.CTkEntry(add, width=110)  # no textvariable: the placeholder shows
        self.rule_entry.grid(row=0, column=1, pady=(8, 4))
        self.rule_entry.bind("<Return>", lambda _event: self.add_rule())
        self.rule_category_key = self.model.table.categories[0].key
        self.rule_category = ctk.CTkOptionMenu(
            add, values=["-"], width=200, dynamic_resizing=False, command=self._category_chosen
        )
        self.rule_category.grid(row=0, column=2, padx=8, pady=(8, 4))
        self.add_rule_button = ctk.CTkButton(add, text="", width=110, command=self.add_rule)
        self._t(self.add_rule_button, "gui.rules.add").grid(row=0, column=3, pady=(8, 4))
        self.rule_message_label = self._label(add)
        self.rule_message_label.grid(
            row=1, column=0, columnspan=5, sticky="ew", padx=12, pady=(0, 8)
        )
        self._keep_wrapped(self.rule_message_label, add, (0, 1, 4, 1), pad=24)

        self.cards_frame = ctk.CTkScrollableFrame(frame, fg_color="transparent")
        self.cards_frame.grid(row=2, column=0, sticky="nsew")
        self._own_wheel()
        self.rules_frame = ctk.CTkFrame(self.cards_frame, corner_radius=10)
        self.rules_frame.grid_columnconfigure(0, weight=1)
        self.cards_frame.bind("<Configure>", self._layout_cards, add="+")
        self._build_cards()
        return frame

    # --- the mouse wheel over «Типы файлов» ------------------------------------------------

    def _own_wheel(self) -> None:
        """One wheel handler for the scrolled area of «Типы файлов».

        CTkScrollableFrame binds the wheel on the tag «all» and scrolls 1/6 of a pixel per
        unit of delta, i.e. some 20 px a notch. Its bindings are replaced by one handler of
        this window (``_types_wheel``) that scrolls only when the pointer is over that area,
        widgets created later included, and is removed when the window is destroyed. The
        scrollbar of the area has its own wheel binding in CTk: replaced by the same one.
        """
        for sequence in CTK_ALL_BINDINGS:
            self.tk.call("bind", "all", sequence, "")
        tk.Misc.bind_all(self, "<MouseWheel>", self._types_wheel)  # CTk forbids bind_all
        self.cards_frame._scrollbar._canvas.bind("<MouseWheel>", self._types_wheel)
        self.cards_frame._parent_canvas.configure(yscrollincrement=1)

    def in_types_area(self, widget: object) -> bool:
        """Is ``widget`` (a widget or its Tk path) in the scrolled area of «Типы файлов»?"""
        name, area = str(widget), str(self.cards_frame._parent_frame)
        return name == area or name.startswith(area + ".")

    def _types_wheel(self, event: tk.Event) -> str | None:
        """Scroll «Типы файлов» by ``WHEEL_STEP`` a notch; other widgets get the wheel as
        before (lists, text boxes, dialogs: their own class bindings)."""
        if not self.in_types_area(event.widget):
            return None
        canvas = self.cards_frame._parent_canvas
        if tuple(canvas.yview()) == (0.0, 1.0):  # everything is visible
            self._wheel_rest = 0.0
            return "break"
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        self._wheel_rest += -event.delta / 120 * WHEEL_STEP * scaling
        units = int(self._wheel_rest)
        self._wheel_rest -= units
        if units:
            canvas.yview_scroll(units, "units")
        return "break"

    def _build_journals(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.content, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=3)
        frame.grid_rowconfigure(2, weight=2)
        bar = ctk.CTkFrame(frame, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        bar.grid_columnconfigure(2, weight=1)
        self.journals_button = ctk.CTkButton(bar, text="", width=110, command=self.load_journals)
        self._t(self.journals_button, "gui.journals.refresh").grid(row=0, column=0, padx=(0, 8))
        self.copy_journal_button = ctk.CTkButton(
            bar, text="", width=150, command=self._copy_selected_journal, **_outline()
        )
        self._t(self.copy_journal_button, "gui.copy_path").grid(row=0, column=1, padx=(0, 10))
        self.journals_label = self._label(bar, text_color=MUTED)
        self.journals_label.grid(row=0, column=2, sticky="ew")
        self._keep_wrapped(self.journals_label, bar, (2, 0, 2, 0))
        holder, self.journal_tree = self._tree(frame, JOURNAL_COLUMNS)
        holder.grid(row=1, column=0, sticky="nsew")
        self.journal_tree.bind("<<TreeviewSelect>>", lambda _event: self.render_journal_details())
        self.journal_text = self._text(frame, 150)
        self.journal_text.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        return frame

    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=16, pady=(4, 12))
        footer.grid_columnconfigure(0, weight=1)
        self.status_label = self._label(footer)
        self.status_label.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self._wrap_status = self._keep_wrapped(self.status_label, footer, (0, 0, 0, 0), pad=10)
        self.progress = ctk.CTkProgressBar(footer, mode="indeterminate", width=120)
        self.preview_button = ctk.CTkButton(footer, text="", width=140, command=self.start_preview)
        self._t(self.preview_button, "gui.preview").grid(row=0, column=2, padx=4)
        self.execute_button = ctk.CTkButton(
            footer,
            text="",
            width=140,
            fg_color=MOVE[0],
            hover_color=MOVE[1],
            command=self.confirm_and_run,
        )
        self._t(self.execute_button, "gui.move").grid(row=0, column=3, padx=4)
        self.cancel_button = ctk.CTkButton(
            footer,
            text="",
            width=100,
            fg_color=DANGER[0],
            hover_color=DANGER[1],
            command=self.cancel_run,
        )
        self._t(self.cancel_button, "gui.cancel").grid(row=0, column=4, padx=(4, 0))

    # --- areas ---------------------------------------------------------------------------

    def _area_labels(self) -> dict[str, str]:
        return {area: tr(f"gui.area.{area}", self.lang) for area in AREAS}

    def _area_clicked(self, label: str) -> None:
        area = next((a for a, text in self._area_labels().items() if text == label), AREA_SORT)
        self.show_area(area)

    def show_area(self, area: str) -> None:
        self.area = area
        for key, frame in self.frames.items():
            if key == area:
                frame.grid(row=0, column=0, sticky="nsew")
            else:
                frame.grid_remove()
        self.area_button.set(self._area_labels()[area])
        if area == AREA_JOURNALS and self.journal_listing is None:
            self.load_journals()

    # --- cards and custom rules ----------------------------------------------------------

    def _build_cards(self) -> None:
        """(Re)create the category cards from the current table; expanded stays expanded,
        folded stays folded, the search stays applied."""
        expanded = {card.key for card in self.cards if card.expanded}
        folded = {card.key for card in self.cards if card.folded}
        for card in self.cards:
            card.destroy()
        collect_widget_garbage()
        self.cards = [
            CategoryCard(self.cards_frame, self, c, c.key in expanded, c.key in folded)
            for c in self.model.table.categories
        ]
        self._columns = 0
        self.apply_search()

    def _layout_cards(self, event: tk.Event) -> None:
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        self._wanted_columns = 2 if event.width / scaling >= TWO_COLUMNS_MIN_WIDTH else 1
        self._place_cards(self._wanted_columns)

    def _place_cards(self, columns: int) -> None:
        """The cards found by the search in ``columns`` columns, then the custom extensions."""
        shown = [card for card in self.cards if card.found]
        placed = tuple(card.key for card in shown)
        if columns == self._columns and placed == self._placed:
            return
        self._columns, self._placed = columns, placed
        for column in range(2):
            self.cards_frame.grid_columnconfigure(
                column,
                weight=1 if column < columns else 0,
                uniform="cards" if column < columns else "",
            )
        for card in self.cards:
            if not card.found:
                card.grid_remove()
        for index, card in enumerate(shown):
            row, column = divmod(index, columns)
            card.grid(row=row, column=column, sticky="nsew", padx=4, pady=4)
        rows = -(-len(shown) // columns)
        self.rules_frame.grid(row=rows, column=0, columnspan=2, sticky="ew", padx=4, pady=4)

    def types_query(self) -> str:
        """The search text of «Типы файлов» as compared (see ``search_query``)."""
        entry = getattr(self, "search_entry", None)
        return search_query(entry.get()) if entry is not None else ""

    def apply_search(self) -> None:
        """Show the cards that match the search; nothing of the selection changes."""
        query = self.types_query()
        found = sum(card.search(query) for card in self.cards)
        self._place_cards(self._wanted_columns)
        self._render_search(query, found)
        if query:
            self.cards_frame._parent_canvas.yview_moveto(0)

    def _render_search(self, query: str, found: int) -> None:
        lang = self.lang
        if not query:
            self.search_label.configure(text="")
            self.search_label.grid_remove()
            return
        text = (
            tr("gui.types.found", lang, count=found)
            if found
            else tr("gui.types.not_found", lang, text=display_text(self.search_entry.get().strip()))
        )
        self.search_label.configure(
            text=text, text_color=MUTED if found else LEVEL_COLORS[Level.WARNING]
        )
        self.search_label.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(4, 0))

    def clear_search(self) -> None:
        self.search_entry.delete(0, "end")
        self.apply_search()

    def _category_labels(self) -> dict[str, str]:
        return {c.key: category_label(c, self.lang) for c in self.model.table.categories}

    def _render_rules(self) -> None:
        """The list of custom extensions with their switches (drawn from ``self.user``)."""
        for widget in self.rule_widgets:
            widget.destroy()
        self.rule_widgets = []
        collect_widget_garbage()
        lang, frame = self.lang, self.rules_frame
        labels = self._category_labels()
        title = self._label(frame, font=ctk.CTkFont(size=14, weight="bold"))
        title.configure(text=tr("gui.rules.title", lang))
        title.grid(row=0, column=0, columnspan=3, sticky="ew", padx=12, pady=(10, 4))
        self.rule_widgets.append(title)
        row = 1
        if not self.user.rules:
            empty = self._label(frame, text_color=MUTED)
            empty.configure(text=tr("gui.rules.empty", lang))
            empty.grid(row=row, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 4))
            self.rule_widgets.append(empty)
            row += 1
        for rule in self.user.rules:
            category = labels.get(rule.category, display_text(rule.category))
            text = tr("gui.rules.rule", lang, extension=rule.extension, category=category)
            label = self._label(frame, text_color=TEXT_COLOR if rule.enabled else MUTED)
            label.configure(text=text)
            label.grid(row=row, column=0, sticky="ew", padx=12, pady=2)
            switch = ctk.CTkSwitch(
                frame,
                text=tr("gui.rules.enabled", lang),
                command=lambda e=rule.extension: self._rule_switched(e),
            )
            if rule.enabled:
                switch.select()
            switch.grid(row=row, column=1, padx=8, pady=2)
            delete = ctk.CTkButton(
                frame,
                text=tr("gui.rules.delete", lang),
                width=90,
                height=26,
                command=lambda e=rule.extension: self.delete_rule(e),
                **_outline(),
            )
            delete.grid(row=row, column=2, padx=(0, 12), pady=2)
            self.rule_widgets.extend((label, switch, delete))
            row += 1
        footer = ctk.CTkFrame(frame, fg_color="transparent")
        footer.grid(row=row, column=0, columnspan=3, sticky="ew", padx=12, pady=(4, 10))
        footer.grid_columnconfigure(1, weight=1)
        self.restore_button = ctk.CTkButton(
            footer,
            text=tr("gui.rules.restore", lang),
            command=self.restore_defaults,
            **_outline(),
        )
        self.restore_button.grid(row=0, column=0, sticky="w")
        legend = self._label(footer, text_color=MUTED)
        legend.configure(text=tr("gui.rules.legend", lang))
        legend.grid(row=0, column=1, sticky="ew", padx=(12, 0))
        self.rule_widgets.extend((footer, self.restore_button, legend))
        self._rule_controls()

    def _rule_controls(self) -> None:
        state = "disabled" if self.busy else "normal"
        for widget in (self.rule_entry, self.rule_category, self.add_rule_button):
            widget.configure(state=state)
        for widget in self.rule_widgets:
            if isinstance(widget, ctk.CTkSwitch | ctk.CTkButton):
                widget.configure(state=state)
        if not self.user.rules:
            self.restore_button.configure(state="disabled")

    def _set_rule_message(self, render: Render | None, level: Level | None = None) -> None:
        self._rule_message = (render, level)
        self._render_rule_message()

    def _render_rule_message(self) -> None:
        render, level = self._rule_message
        color = LEVEL_COLORS[level] if level else MUTED
        self.rule_message_label.configure(
            text=render(self.lang) if render else tr("gui.rules.hint", self.lang),
            text_color=color,
        )

    def add_rule(self) -> None:
        if self.busy:
            return
        raw = self.rule_entry.get()
        try:
            user = self.user.with_rule(raw, self.rule_category_key)
        except ExtensionError as exc:
            shown, reason = display_text(raw.strip()), exc.reason
            self._set_rule_message(
                lambda lang: tr(
                    "gui.rules.invalid",
                    lang,
                    value=shown,
                    reason=pick(i18n.EXTENSION_ERROR_TEXT, reason, lang),
                ),
                Level.ERROR,
            )
            return
        added = next(r for r in user.rules if r not in self.user.rules)
        self.rule_entry.delete(0, "end")
        if self._apply_user(user):
            category = self.model.table.category(added.category)
            self._set_rule_message(
                lambda lang: tr(
                    "gui.rules.added",
                    lang,
                    extension=added.extension,
                    category=category_label(category, lang),
                ),
                Level.OK,
            )

    def _category_chosen(self, label: str) -> None:
        for key, text in self._category_labels().items():
            if text == label:
                self.rule_category_key = key

    def _rule_switched(self, extension: str) -> None:
        rule = self.user.rule(extension)
        if rule is not None:
            self._apply_user(self.user.with_enabled(extension, not rule.enabled))

    def delete_rule(self, extension: str) -> None:
        if self.user.rule(extension) is not None and self._apply_user(
            self.user.without_rule(extension)
        ):
            self._set_rule_message(
                lambda lang: tr("gui.rules.deleted", lang, extension=extension), Level.OK
            )

    def restore_defaults(self) -> None:
        if self.busy or not self.user.rules:
            return
        lang = self.lang
        if not ask_choice(
            self,
            title=tr("gui.restore.title", lang),
            heading=tr("gui.restore.heading", lang, count=len(self.user.rules)),
            body=tr("gui.restore.body", lang),
            accept=tr("gui.restore.accept", lang),
            cancel=tr("gui.cancel", lang),
        ):
            return
        if self._apply_user(self.user.with_defaults()):
            self._set_rule_message(lambda lang: tr("gui.rules.restored", lang), Level.OK)

    def _apply_user(self, user: UserSettings) -> bool:
        """Use the custom extensions of ``user`` in the window; not during a preview or run.

        Nothing is written: «Сохранить настройки» does that. A changed table makes the
        preview stale at once.
        """
        if self.busy:
            self.refresh_all()
            return False
        self.user = user
        table = user.table()
        if dict(table.custom) != dict(self.model.table.custom):
            dropped = self.model.set_table(table)
            self._build_cards()
            if dropped:
                self._preview_dropped()
        self._render_rules()
        self.refresh_all()
        return True

    # --- settings file ---------------------------------------------------------------------

    def _show_loaded(self, loaded: LoadResult) -> None:
        """Say what was wrong with ``settings.json``; the file itself is never changed here."""
        problem = loaded.problem
        if problem is SettingsProblem.NO_LOCALAPPDATA:
            self.say("settings.no_localappdata", Level.WARNING)
        elif problem in FATAL_PROBLEMS:
            self.set_status(
                lambda lang: tr("settings.defaults", lang, problem=tr(f"settings.{problem}", lang)),
                Level.WARNING,
            )
        elif problem is not None or loaded.rejected:
            count = len(loaded.rejected)

            def render(lang: Lang) -> str:
                parts = []
                if problem is not None:
                    parts.append(tr(f"settings.{problem}", lang))
                if count:
                    parts.append(tr("settings.rejected", lang, count=count))
                return tr("settings.partly", lang, problem="; ".join(parts))

            self.set_status(render, Level.WARNING)

    @property
    def unsaved(self) -> bool:
        """The language or the custom extensions differ from what was loaded or saved."""
        return self.user != self.saved_user

    def _render_unsaved(self) -> None:
        self.unsaved_label.configure(
            text=tr("gui.settings.unsaved", self.lang) if self.unsaved else ""
        )

    def save_settings(self, *, offer_reload: bool = True) -> bool:
        """Write the settings of the window to ``settings.json``: only on the user's request.

        ``False`` (and an error in the status line) if not saved. A file that is not the one
        loaded is never overwritten: with ``offer_reload`` the user may load it instead.
        """
        user = self.user
        try:
            result = self.store.save(user)
        except SaveError as exc:
            self._save_failed(exc)
            self._render_unsaved()
            if offer_reload and exc.reason in RELOAD_FAILURES:
                self._offer_reload()
            return False
        self.saved_user = user
        path = display_text(str(result.path))
        if result.backup is not None:
            backup = display_text(str(result.backup))
            self.say("settings.saved_backup", Level.WARNING, path=path, backup=backup)
        elif result.notes:
            detail = display_text("; ".join(result.notes))
            self.say("settings.saved_notes", Level.WARNING, path=path, detail=detail)
        else:
            self.say("settings.saved", Level.OK, path=path)
        self._render_unsaved()
        return True

    def _save_failed(self, exc: SaveError) -> None:
        """Say that nothing was saved, why, and what is on the disk now."""
        reason, intact = exc.reason, exc.intact
        detail = display_text(exc.detail) if exc.detail else ""
        backup = display_text(str(exc.backup)) if exc.backup is not None else None
        temp = display_text(str(exc.temp)) if exc.temp is not None else None

        def render(lang: Lang) -> str:
            why = tr(f"settings.fail.{reason}", lang) + (f" ({detail})" if detail else "")
            state = [tr("settings.state.intact" if intact else "settings.state.broken", lang)]
            if backup is not None:
                state.append(tr("settings.state.backup", lang, path=backup))
            if temp is not None:
                state.append(tr("settings.state.temp", lang, path=temp))
            return tr("settings.save_failed", lang, reason=why, state=" ".join(state))

        self.set_status(render, Level.ERROR)

    def _offer_reload(self) -> None:
        if self.busy:  # the table cannot change now; the error stays in the status line
            return
        lang = self.lang
        if ask_choice(
            self,
            title=tr("gui.reload.title", lang),
            heading=tr("gui.reload.heading", lang),
            body=tr("gui.reload.body", lang),
            accept=tr("gui.reload.accept", lang),
            cancel=tr("gui.reload.keep", lang),
        ):
            self.reload_settings()

    def reload_settings(self) -> None:
        """Read ``settings.json`` again and use it; the edits of the window are dropped."""
        if self.busy:
            self.say("settings.reload_busy", Level.WARNING)
            return
        loaded = self.store.load()  # reading creates nothing
        self.saved_user = loaded.settings
        self._apply_user(loaded.settings)
        if loaded.settings.language is not self.lang:
            self.lang = loaded.settings.language
            self.apply_language()
        self.say("settings.reloaded", Level.OK)
        self._show_loaded(loaded)  # a problem of the file replaces that line

    # --- language ------------------------------------------------------------------------

    def _language_clicked(self, label: str) -> None:
        lang = next((code for code, text in LANG_LABELS.items() if text == label), self.lang)
        self.set_language(lang)

    def set_language(self, lang: Lang) -> None:
        """Redraw everything in ``lang``; nothing is written, nothing of the sorter changes."""
        lang = Lang(lang)
        if lang is self.lang:
            return
        self.lang = lang
        self.user = self.user.with_language(lang)
        self.apply_language()

    def apply_language(self) -> None:
        lang = self.lang
        for widget, key in self._texts:
            widget.configure(text=tr(key, lang))
        self.language_button.set(LANG_LABELS[lang])
        self.area_button.configure(values=list(self._area_labels().values()))
        self.area_button.set(self._area_labels()[self.area])
        policies = {p: tr(k, lang) for p, k in POLICY_KEYS.items()}
        self.policy_button.configure(values=list(policies.values()))
        self.policy_button.set(policies[self.model.policy])
        self.filter_button.configure(
            values=[tr("gui.filter.all", lang), tr("gui.filter.not_moved", lang)]
        )
        self.filter_button.set(tr(f"gui.filter.{self.result_filter}", lang))
        for tree, columns in self._headings:
            for key, title, _width, _stretch in columns:
                tree.heading(key, text=tr(title, lang))
        labels = self._category_labels()
        self.rule_category.configure(values=list(labels.values()))
        self.rule_category.set(labels[self.rule_category_key])
        self.rule_entry.configure(placeholder_text=tr("gui.rules.placeholder", lang))
        self.search_entry.configure(placeholder_text=tr("gui.types.search", lang))
        self.apply_search()  # a category name may match in one language only
        self.title(f"{APP_NAME} {__version__}")
        self._render_rules()
        self._render_rule_message()
        self.render_work()
        self.render_journals()
        self._render_status()
        self.refresh_all()

    # --- state ---------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self.task is not None

    @property
    def running(self) -> bool:
        return self.task is not None and self.task.kind is TaskKind.RUN

    @property
    def task_cancelled(self) -> bool:
        return self.task is not None and self.task.cancelled()

    def change(self, action: Callable[[AppModel], bool]) -> None:
        """Apply a setting change; a preview made for other settings becomes stale."""
        if self.busy:  # controls are disabled; a late event must not change a running plan
            self.refresh_all()
            return
        if action(self.model):
            self._preview_dropped()
        self.refresh_all()

    def _preview_dropped(self) -> None:
        self.work_note = ("gui.preview.stale", Level.WARNING)
        self.say("gui.status.changed")
        self.render_work()

    def refresh_all(self) -> None:
        lang = self.lang
        for card in self.cards:
            card.refresh()
        summary = self.model.selection.overall_summary()
        text = tr("gui.selected", lang, selected=summary.selected, total=summary.total)
        self.selection_label.configure(text=text)
        self.sort_selection_label.configure(text=text)
        where = self.model.effective_destination(lang)
        shown = shorten_middle(where, WHERE_LIMIT)
        self.effective_label.configure(
            text=tr("gui.effective", lang, path=shown) if self.model.source.strip() else where
        )
        self.details_button.configure(
            text=tr("gui.details.hide" if self.details_open else "gui.details.show", lang)
        )
        self._render_unsaved()
        self.refresh_controls()

    def refresh_controls(self) -> None:
        idle = not self.busy
        state = "normal" if idle else "disabled"
        for widget in (
            self.source_entry,
            self.destination_entry,
            self.source_button,
            self.destination_button,
            self.reset_button,
            self.policy_button,
            self.all_button,
            self.none_button,
            self.preview_button,
            self.repreview_button,
            self.save_button,  # saving while busy: only when the window is closed
        ):
            widget.configure(state=state)
        for card in self.cards:
            card.set_enabled(idle)
        self._rule_controls()
        self.execute_button.configure(
            state="normal" if self.model.can_execute and idle else "disabled"
        )
        self.cancel_button.configure(
            state="normal" if self.running and not self.task_cancelled else "disabled"
        )
        self.copy_journal_button.configure(
            state="normal" if self._selected_journal() is not None else "disabled"
        )
        if self.busy:
            self.progress.grid(row=0, column=1, padx=8)
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.grid_remove()
        self._soon(self._wrap_status)

    def set_status(self, render: Render | None, level: Level | None = None) -> None:
        """The one status line of the window; ``render`` makes its text in a language."""
        self._status = (render, level)
        self._render_status()

    def say(self, key: str, level: Level | None = None, **values: object) -> None:
        self.set_status(lambda lang: tr(key, lang, **values), level)

    def _render_status(self) -> None:
        render, level = self._status
        self.status_label.configure(
            text=render(self.lang) if render else "",
            text_color=LEVEL_COLORS[level] if level else TEXT_COLOR,
        )

    def browse(self, var: tk.StringVar) -> None:
        current = var.get().strip() or self.source_var.get().strip()
        initial = current if current and Path(current).is_dir() else str(Path.home())
        chosen = filedialog.askdirectory(
            parent=self, mustexist=True, initialdir=initial, title=tr("gui.browse.title", self.lang)
        )
        if chosen:
            var.set(os.path.normpath(chosen))

    def _policy_clicked(self, label: str) -> None:
        policy = next(
            (p for p, key in POLICY_KEYS.items() if tr(key, self.lang) == label), self.model.policy
        )
        self.change(lambda m: m.set_policy(policy))

    def _filter_clicked(self, label: str) -> None:
        self.result_filter = (
            FILTER_NOT_MOVED if label == tr("gui.filter.not_moved", self.lang) else FILTER_ALL
        )
        self.render_work()

    # --- clipboard -------------------------------------------------------------------------

    def copy_path(self, path: Path | None) -> None:
        """Put the full ``path`` on the clipboard; the file itself is not opened or touched."""
        if path is None:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(str(path))
        except tk.TclError as exc:
            self.say("gui.copy_failed", Level.ERROR, reason=display_text(str(exc)))
            return
        self.say("gui.copied", Level.OK)
        shown = self._status
        if self._copied_id is not None:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(self._copied_id)
        self._copied_id = self.after(COPIED_CLEAR_MS, lambda: self._clear_copied(shown))

    def _clear_copied(self, shown: tuple[Render | None, Level | None]) -> None:
        self._copied_id = None
        if self._status is shown:  # nothing newer was said meanwhile
            self.set_status(None)

    # --- the work area: preview and result ---------------------------------------------

    def render_work(self) -> None:
        """Draw the preview or the result in the current language."""
        lang = self.lang
        self._row_details = {}
        self.report = None
        for tree in (self.preview_tree, self.result_tree):
            tree.delete(*tree.get_children())
        self.filter_button.grid_remove()
        self.repreview_button.grid_remove()
        self.journal_bar.grid_remove()
        self._note(())
        if self.view == VIEW_RESULT and self.outcome is not None:
            self.preview_holder.grid_remove()
            self.result_holder.grid()
            self._render_result(run_report(self.outcome, lang, self.run_inventory))
        else:
            self.result_holder.grid_remove()
            self.preview_holder.grid()
            self._render_preview()
        self._soon(self._wrap_counts)

    def toggle_details(self) -> None:
        """Show or hide «Подробности»: full paths, the volume check, what was counted."""
        self.details_open = not self.details_open
        self.refresh_all()
        self.render_work()

    def _note(self, lines: tuple[str, ...] | list[str]) -> None:
        """The warning line under the counts; hidden when there is nothing to say."""
        if lines:
            self.note_label.configure(text="\n".join(lines))
            self.note_label.grid(row=4, column=0, sticky="ew", pady=(0, 4))
            self._soon(self._wrap_note)
        else:
            self.note_label.configure(text="")
            self.note_label.grid_remove()

    def _details_lines(self) -> list[str]:
        """«Подробности» of the settings: the full source and destination paths."""
        lang = self.lang
        source = self.model.source.strip()
        return [
            tr("gui.details.title", lang),
            tr("gui.confirm.from", lang, path=display_text(source) if source else "—"),
            tr("gui.confirm.to", lang, path=self.model.effective_destination(lang)),
        ]

    def _headline(self, text: str, level: Level | None) -> None:
        if self.work_note is not None:
            key, level = self.work_note
            text = tr(key, self.lang)
        self.headline_label.configure(
            text=text, text_color=LEVEL_COLORS[level] if level else TEXT_COLOR
        )

    def _render_preview(self) -> None:
        lang, preview = self.lang, self.shown_preview
        if preview is None:
            self._headline(tr("gui.preview.none", lang), None)
            self.counts_label.configure(text=tr("gui.preview.how", lang))
            lines = [tr("gui.preview.safe", lang)]
            if self.details_open:
                lines += ["", *self._details_lines()]
            self._work_info = "\n".join(lines)
            self._set_text(self.work_text, self._work_info)
            return
        errors = preview.errors(lang)
        level = Level.ERROR if errors else Level.OK if preview.executable else Level.WARNING
        self._headline(preview.headline(lang), level)
        self.counts_label.configure(text=preview.summary_counts(lang))
        self._note(preview.outside_lines(lang))
        for index, row in enumerate(preview.rows(lang)):
            tag = "skip" if row.skipped else "rename" if row.renamed else ""
            iid = str(index)
            self.preview_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    shorten_middle(row.name, NAME_LIMIT),
                    row.category,
                    row.action,
                    row.reason,
                    shorten_middle(row.target, PATH_LIMIT),
                ),
                tags=(tag,) if tag else (),
            )
            self._row_details[iid] = lambda lg, r=row: "\n".join(
                (
                    tr("gui.detail.file", lg, value=r.name),
                    tr("gui.detail.source", lg, value=r.source),
                    tr("gui.detail.target", lg, value=r.target),
                    tr("gui.detail.category", lg, value=r.category),
                    tr("gui.detail.action", lg, value=r.action),
                    *((tr("gui.detail.reason", lg, value=r.reason),) if r.reason else ()),
                )
            )
        lines: list[str] = []
        if errors:  # always shown: they say why nothing can be moved
            lines.append(tr("preview.cannot_run", lang))
            lines.extend(f"• {e}" for e in errors)
        if self.details_open:
            for part in (
                self._details_lines(),
                preview.inventory_details(lang),
                preview.volume_lines(lang),
            ):
                if part:
                    lines += ["", *part] if lines else list(part)
        if not lines:
            lines.append(tr("gui.preview.select_row", lang))
        self._work_info = "\n".join(lines)
        self._set_text(self.work_text, self._work_info)

    def _render_result(self, report: RunReport) -> None:
        lang = self.lang
        self.report = report
        self._headline_result(report)
        lines = list(report.lines)
        self.counts_label.configure(text=lines.pop(0) if report.counts and lines else "")
        if report.source_changed:  # why they were not moved and what to do: under the counts
            changed = tr("run.source_changed", lang, count=report.source_changed)
            lines = [line for line in lines if line != changed]
            self._note((changed,))
        if self.details_open:
            lines += ["", *self._details_lines()]
        if report.rows:
            self.filter_button.grid(row=0, column=1, padx=(8, 0))
        if report.not_moved and not self.busy:
            self.repreview_button.grid(row=0, column=2, padx=(8, 0))
        if report.journal_path is not None:
            path = display_text(str(report.journal_path))
            self.result_journal_label.configure(
                text=tr("journal.path", lang, path=shorten_middle(path, PATH_LIMIT))
            )
            self.journal_bar.grid(row=5, column=0, sticky="ew", pady=(0, 4))
        only_not_moved = self.result_filter == FILTER_NOT_MOVED
        for index, row in enumerate(report.rows):
            if only_not_moved and row.status_code is ItemStatus.MOVED:
                continue
            tag = (
                "bad"
                if row.attention
                else "skip"
                if row.status_code is not ItemStatus.MOVED
                else ""
            )
            iid = str(index)
            self.result_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    shorten_middle(row.name, NAME_LIMIT),
                    row.status,
                    row.reason,
                    shorten_middle(row.target, PATH_LIMIT),
                ),
                tags=(tag,) if tag else (),
            )
            self._row_details[iid] = lambda lg, r=row: "\n".join(
                (
                    tr("gui.detail.file", lg, value=r.name),
                    tr("gui.detail.status", lg, value=r.status),
                    *((tr("gui.detail.reason", lg, value=r.reason),) if r.reason else ()),
                    tr("gui.detail.source", lg, value=r.source),
                    tr("gui.detail.target", lg, value=r.target),
                )
            )
        self._work_info = "\n".join(lines)
        self._set_text(self.work_text, self._work_info)

    def _headline_result(self, report: RunReport) -> None:
        self.headline_label.configure(text=report.headline, text_color=LEVEL_COLORS[report.level])

    def _row_selected(self, tree: ttk.Treeview) -> None:
        selected = tree.selection()
        render = self._row_details.get(selected[0]) if selected else None
        if render is not None:
            info = getattr(self, "_work_info", "")
            text = render(self.lang) + (f"\n\n{info}" if info else "")
            self._set_text(self.work_text, text)

    # --- preview ---------------------------------------------------------------------------

    def start_preview(self) -> None:
        if self.busy:
            return
        settings = self.model.settings()
        table = self.model.table
        self.model.clear_preview()
        self.task = BackgroundTask(
            TaskKind.PREVIEW, lambda _cancelled: build_preview(settings, table), self.events
        )
        collect_widget_garbage()
        self.task.start()
        self.work_note = ("gui.preview.updating", None)
        self.say("gui.status.previewing")
        self.render_work()
        self.refresh_controls()

    def show_preview(self, result: PreviewResult) -> None:
        self.shown_preview = result
        self.view = VIEW_PREVIEW
        self.work_note = None
        self.render_work()
        if result.has_errors:
            self.say("gui.status.fix", Level.ERROR)
        elif result.executable:
            self.say("gui.status.ready")
        else:
            self.set_status(None)
        self.show_area(AREA_SORT)

    # --- run -------------------------------------------------------------------------------

    def confirm_and_run(self) -> None:
        preview = self.model.preview
        if self.busy or preview is None or not self.model.can_execute:
            self.say("gui.status.need_preview", Level.WARNING)
            self.refresh_controls()
            return
        lang = self.lang
        journals = journal_directory()
        journal = (
            tr("gui.confirm.journal", lang, path=display_text(str(journals)))
            if journals
            else tr("gui.confirm.journal_unknown", lang)
        )
        details = "\n".join(
            (
                tr("gui.confirm.from", lang, path=display_text(preview.settings.source.strip())),
                tr("gui.confirm.to", lang, path=self.model.effective_destination(lang)),
                "",
                tr("gui.confirm.move", lang),
                tr("no_rollback", lang),
                journal,
            )
        )
        accepted = ask_choice(
            self,
            title=tr("gui.confirm.title", lang),
            heading=tr("gui.confirm.heading", lang, count=preview.moves + preview.renames),
            body=tr("gui.confirm.counts", lang, renames=preview.renames, skipped=preview.skipped),
            details=details,
            accept=tr("gui.confirm.accept", lang),
            cancel=tr("gui.cancel", lang),
        )
        if not accepted:
            self.say("gui.status.declined")
            return
        # the dialog ran the event loop: run only the very preview that was confirmed
        if self.busy or self.model.preview is not preview or not self.model.can_execute:
            self.say("gui.status.stale_after_dialog", Level.WARNING)
            self.refresh_controls()
            return
        self.run_inventory = preview.inventory  # «вне плана» of the result: of this preview
        plan = self.model.start_run()
        table = self.model.table
        self.task = BackgroundTask(
            TaskKind.RUN, lambda cancelled: execute(plan, table, cancelled), self.events
        )
        collect_widget_garbage()
        self.task.start()
        self.work_note = ("gui.preview.used", None)
        self.say("gui.status.running")
        self.render_work()
        self.refresh_all()

    def cancel_run(self) -> None:
        if self.running and self.task is not None:
            self.task.cancel()
            self.say("gui.status.cancel_requested", Level.WARNING)
            self.refresh_controls()

    def show_outcome(self, outcome: RunOutcome) -> None:
        self.outcome = outcome
        self.view = VIEW_RESULT
        self.result_filter = FILTER_ALL
        self.work_note = None
        self.filter_button.set(tr("gui.filter.all", self.lang))
        self.set_status(None)
        self.render_work()
        self.show_area(AREA_SORT)

    # --- journals --------------------------------------------------------------------------

    def load_journals(self) -> None:
        if self.journal_task is not None:
            return
        self.journal_task = BackgroundTask(
            TaskKind.JOURNALS, lambda _cancelled: list_journals(), self.events
        )
        collect_widget_garbage()
        self.journal_task.start()
        self.journals_button.configure(state="disabled")
        self.journals_label.configure(text=tr("gui.journals.loading", self.lang))

    def show_journals(self, listing: JournalListing) -> None:
        self.journal_listing = listing
        self.journals = listing.journals
        self.render_journals()

    def render_journals(self) -> None:
        lang, listing = self.lang, self.journal_listing
        tree = self.journal_tree
        selected = tree.selection()
        tree.delete(*tree.get_children())
        if listing is None:
            if self.journal_task is not None:
                self.journals_label.configure(text=tr("gui.journals.loading", lang))
            self.render_journal_details()
            return
        for index, journal in enumerate(listing.journals):
            checks = []
            if journal.test_journal:
                checks.append(tr("gui.journals.check.test", lang))
            if journal.recovery:
                checks.append(tr("gui.journals.check.recovery", lang, count=journal.recovery))
            if journal.missing_objects:
                checks.append(tr("gui.journals.check.missing", lang))
            when = format_time(journal.started, lang) if journal.started else "—"
            result = journal.error(lang) or journal_result_text(journal.result, lang)
            if checks:
                result += f" ({'; '.join(checks)})"
            total, done = journal.file_count, journal.done_count
            files = (
                "—"
                if not total
                else str(total)
                if done == total
                else tr("gui.journals.files.done", lang, done=done, total=total)
            )
            tag = (
                "bad"
                if journal.attention and not journal.test_journal
                else ("skip" if journal.test_journal else "")
            )
            tree.insert(
                "",
                "end",
                iid=str(index),
                values=(when, journal_kind_text(journal.kind, lang), result, files),
                tags=(tag,) if tag else (),
            )
        where = (
            display_text(str(listing.directory))
            if listing.directory
            else tr("gui.journals.unknown", lang)
        )
        text = tr("gui.journals.summary", lang, path=where, count=len(listing.journals))
        note = listing.note_text(lang)
        if note:
            text += f" {note}"
        tests = sum(j.test_journal for j in listing.journals)
        if tests:
            text += " " + tr("gui.journals.tests", lang, count=tests)
        self.journals_label.configure(text=text)
        if selected and tree.exists(selected[0]):
            tree.selection_set(selected[0])  # its details are redrawn by <<TreeviewSelect>>
        self.render_journal_details()

    def _selected_journal(self) -> JournalSummary | None:
        selected = self.journal_tree.selection()
        if not selected:
            return None
        index = int(selected[0])
        return self.journals[index] if index < len(self.journals) else None

    def render_journal_details(self) -> None:
        journal = self._selected_journal()
        text = (
            journal_details(journal, self.lang)
            if journal is not None
            else tr("gui.journals.hint", self.lang)
        )
        self._set_text(self.journal_text, text)
        self.copy_journal_button.configure(state="normal" if journal is not None else "disabled")

    def _copy_selected_journal(self) -> None:
        journal = self._selected_journal()
        self.copy_path(journal.path if journal is not None else None)

    # --- events from the workers -----------------------------------------------------------

    def poll(self) -> None:
        """Take the results of finished tasks (runs in the Tk event loop only)."""
        self._poll_id = None
        try:
            while True:
                self.handle(self.events.get_nowait())
        except queue.Empty:
            pass
        if self.close_pending and self.task is None:
            self.destroy()
            return
        self._poll_id = self.after(POLL_MS, self.poll)

    def handle(self, done: TaskDone) -> None:
        if done.kind is TaskKind.JOURNALS:
            self.journal_task = None
            self.journals_button.configure(state="normal")
            if done.error is not None:
                error = done.error
                self.journals_label.configure(
                    text=tr("gui.journals.failed", self.lang, error=error)
                )
            elif isinstance(done.value, JournalListing):
                self.show_journals(done.value)
            return
        self.task = None
        if done.kind is TaskKind.PREVIEW:
            self.work_note = None
            if done.error is not None:
                self.say("preview.failed", Level.ERROR, error=done.error)
                self.render_work()
            elif isinstance(done.value, PreviewResult):
                if self.model.accept_preview(done.value):
                    self.show_preview(done.value)
                else:
                    self.work_note = ("gui.preview.stale", Level.WARNING)
                    self.say("gui.status.changed_during_preview", Level.WARNING)
                    self.render_work()
        else:  # RUN
            self.model.finish_run()
            if done.error is not None:
                outcome = RunOutcome(error=done.error)
            elif isinstance(done.value, RunOutcome):
                outcome = done.value
            else:  # not expected from execute(): shown as an unexpected result
                outcome = RunOutcome(done.value)
            self.journal_listing = None  # a new journal: read again when the area is opened
            self.show_outcome(outcome)
        self.refresh_all()

    def on_close(self) -> None:
        if self.close_pending:
            return
        if self.active_dialog is not None:  # answer the open question first
            with contextlib.suppress(tk.TclError):
                self.active_dialog.lift()
            return
        if self.unsaved and not self._settle_unsaved():
            return
        task = self.task
        if task is not None and task.kind is TaskKind.RUN:
            lang = self.lang
            if not ask_choice(
                self,
                title=tr("gui.close.title", lang),
                heading=tr("gui.close.heading", lang),
                body=tr("gui.close.body", lang),
                accept=tr("gui.close.accept", lang),
                cancel=tr("gui.close.keep", lang),
            ):
                return
            # the dialog runs the event loop: poll() may have taken the run's result meanwhile
            if self.task is task:
                task.cancel()
                self.close_pending = True
                self.say("gui.status.closing", Level.WARNING)
                self.refresh_controls()
                return
        # the run is over, or a preview or a journal read changes nothing: just close
        self.destroy()

    def _settle_unsaved(self) -> bool:
        """Save, drop or keep the unsaved settings before closing; ``False``: stay open.

        Closing the question, Escape, Enter and «Отмена» keep the window; a failed save
        keeps it as well, with the error in the status line.
        """
        lang = self.lang
        choice = ask_choices(
            self,
            title=tr("gui.unsaved.title", lang),
            heading=tr("gui.unsaved.heading", lang),
            body=tr("gui.unsaved.body", lang),
            accept=tr("gui.unsaved.save", lang),
            other=tr("gui.unsaved.discard", lang),
            cancel=tr("gui.cancel", lang),
            accept_color=None,
        )
        if choice == ACCEPT:
            return self.save_settings(offer_reload=False)
        return choice == OTHER

    def destroy(self) -> None:
        for after_id in (self._poll_id, self._copied_id):
            if after_id is not None:
                with contextlib.suppress(tk.TclError):
                    self.after_cancel(after_id)
        self._poll_id = self._copied_id = None
        for idle_id in self._idle_ids.values():
            with contextlib.suppress(tk.TclError):
                self.after_cancel(idle_id)
        self._idle_ids.clear()
        with contextlib.suppress(tk.TclError):
            self.tk.call("bind", "all", "<MouseWheel>", "")  # see _own_wheel
        # CustomTkinter schedules its own callbacks (scaling, title bar); Tcl timers belong
        # to the thread, so one left here would fire in a later window and fail there
        with contextlib.suppress(tk.TclError):
            for after_id in self.tk.splitlist(self.tk.call("after", "info")):
                with contextlib.suppress(tk.TclError):
                    self.tk.call("after", "cancel", after_id)
        super().destroy()

    def report_callback_exception(
        self, exc: type[BaseException], val: BaseException, tb: object
    ) -> None:
        """An error in a callback of the window: shown, not hidden."""
        traceback.print_exception(exc, val, tb, file=sys.stderr)  # type: ignore[arg-type]
        with contextlib.suppress(tk.TclError):
            messagebox.showerror(
                APP_NAME,
                tr("gui.internal_error", self.lang, error=f"{exc.__name__}: {val}"),
                parent=self,
            )


def main() -> None:
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    app = OrganizerApp()
    app.mainloop()
