"""Which extensions the user selected, independent of which widgets are visible.

Group and "select all" states are derived from the selected extensions, so hiding
rare extensions behind an expander can never change or misreport the selection.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from downloads_organizer.catalog import Category
from downloads_organizer.extensions import ExtensionTable


class SelectionState(StrEnum):
    NONE = "none"
    PARTIAL = "partial"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class SelectionSummary:
    state: SelectionState
    selected: int
    total: int


def _summary(selected: int, total: int) -> SelectionSummary:
    if selected == 0:
        state = SelectionState.NONE
    elif selected == total:
        state = SelectionState.ALL
    else:
        state = SelectionState.PARTIAL
    return SelectionSummary(state, selected, total)


class Selection:
    """Mutable set of selected extensions bound to an ``ExtensionTable``.

    Starts empty, as in the legacy program. Unknown extensions raise ``KeyError``.
    """

    def __init__(self, table: ExtensionTable, selected: Iterable[str] = ()) -> None:
        self._table = table
        self._selected: set[str] = set()
        for ext in selected:
            self.set_extension(ext, True)

    @property
    def table(self) -> ExtensionTable:
        return self._table

    @property
    def selected_extensions(self) -> frozenset[str]:
        return frozenset(self._selected)

    def is_selected(self, ext: str) -> bool:
        return ext.lower() in self._selected

    def set_extension(self, ext: str, selected: bool) -> None:
        ext = ext.lower()
        if self._table.category_for(ext) is None:
            raise KeyError(ext)
        if selected:
            self._selected.add(ext)
        else:
            self._selected.discard(ext)

    def set_category(self, key: str, selected: bool) -> None:
        """Select or clear every extension of the category, including rare and custom."""
        for ext in self._table.extensions(key):
            self.set_extension(ext, selected)

    def set_all(self, selected: bool) -> None:
        if selected:
            self._selected = set(self._table.all_extensions())
        else:
            self._selected.clear()

    def category_summary(self, key: str) -> SelectionSummary:
        exts = self._table.extensions(key)
        return _summary(sum(ext in self._selected for ext in exts), len(exts))

    def overall_summary(self) -> SelectionSummary:
        return _summary(len(self._selected), len(self._table.all_extensions()))

    def extension_map(self) -> dict[str, Category]:
        """Selected extension -> category, the input for classification."""
        result: dict[str, Category] = {}
        for ext in self._selected:
            category = self._table.category_for(ext)
            if category is not None:
                result[ext] = category
        return result

    def rebind(self, table: ExtensionTable) -> None:
        """Switch to an updated table (custom extension added or removed).

        Selections of extensions that no longer exist are dropped; newly added
        extensions start unselected, so nothing is moved without an explicit choice.
        """
        self._table = table
        self._selected = {ext for ext in self._selected if table.category_for(ext) is not None}
