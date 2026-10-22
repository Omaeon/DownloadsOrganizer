from __future__ import annotations

import pytest

from downloads_organizer.catalog import DEFAULT_CATEGORIES
from downloads_organizer.extensions import ExtensionTable
from downloads_organizer.selection import Selection, SelectionState, SelectionSummary

S = SelectionState


@pytest.fixture
def selection() -> Selection:
    return Selection(ExtensionTable())


def test_starts_empty_like_legacy(selection):
    assert selection.selected_extensions == frozenset()
    assert selection.overall_summary() == SelectionSummary(S.NONE, 0, 40)
    for category in DEFAULT_CATEGORIES:
        assert selection.category_summary(category.key).state is S.NONE
    assert selection.extension_map() == {}


def test_category_selection_includes_hidden_rare_extensions(selection):
    selection.set_category("Images", True)
    assert selection.is_selected(".gif")
    assert selection.is_selected(".bmp")
    assert selection.category_summary("Images") == SelectionSummary(S.ALL, 6, 6)
    assert set(selection.extension_map()) == set(DEFAULT_CATEGORIES[2].extensions)


def test_select_all_covers_rare_extensions_and_maps_each_to_its_category(selection):
    selection.set_all(True)
    assert selection.overall_summary() == SelectionSummary(S.ALL, 40, 40)
    mapping = selection.extension_map()
    assert len(mapping) == 40
    for category in DEFAULT_CATEGORIES:
        for ext in category.extensions:
            assert mapping[ext] is category


def test_clearing_one_rare_extension_makes_group_and_all_partial(selection):
    selection.set_all(True)
    selection.set_extension(".bmp", False)
    assert selection.category_summary("Images") == SelectionSummary(S.PARTIAL, 5, 6)
    assert selection.overall_summary() == SelectionSummary(S.PARTIAL, 39, 40)
    assert selection.category_summary("PDF").state is S.ALL


def test_selecting_every_extension_individually_reports_all(selection):
    for category in DEFAULT_CATEGORIES:
        for ext in category.extensions:
            selection.set_extension(ext, True)
    assert selection.overall_summary().state is S.ALL


def test_set_all_false_and_set_category_false_clear(selection):
    selection.set_all(True)
    selection.set_category("Code", False)
    assert selection.category_summary("Code").state is S.NONE
    selection.set_all(False)
    assert selection.selected_extensions == frozenset()


def test_extension_matching_is_case_insensitive(selection):
    selection.set_extension(".JPG", True)
    assert selection.is_selected(".jpg")
    assert selection.selected_extensions == {".jpg"}


def test_unknown_extension_or_category_raises(selection):
    with pytest.raises(KeyError):
        selection.set_extension(".heic", True)
    with pytest.raises(KeyError):
        selection.set_category("Nope", True)
    with pytest.raises(KeyError):
        Selection(ExtensionTable(), [".heic"])


def test_initial_selection_from_iterable():
    selection = Selection(ExtensionTable(), [".PDF", ".mp4"])
    assert selection.selected_extensions == {".pdf", ".mp4"}


def test_category_selection_includes_custom_extensions():
    table = ExtensionTable().with_custom("heic", "Images")
    selection = Selection(table)
    selection.set_category("Images", True)
    assert selection.is_selected(".heic")
    assert selection.category_summary("Images") == SelectionSummary(S.ALL, 7, 7)
    assert selection.extension_map()[".heic"].key == "Images"


def test_rebind_after_adding_keeps_selection_and_new_extension_unselected(selection):
    selection.set_category("Images", True)
    selection.set_extension(".pdf", True)
    selection.rebind(selection.table.with_custom("heic", "Images"))
    assert not selection.is_selected(".heic")
    assert selection.category_summary("Images") == SelectionSummary(S.PARTIAL, 6, 7)
    assert selection.is_selected(".pdf")


def test_rebind_after_removing_drops_selection_of_removed_extension():
    table = ExtensionTable().with_custom("heic", "Images")
    selection = Selection(table, [".heic", ".jpg"])
    selection.rebind(table.without_custom("heic"))
    assert selection.selected_extensions == {".jpg"}
    assert ".heic" not in selection.extension_map()
