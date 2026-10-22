from __future__ import annotations

import tomllib
from pathlib import Path

import downloads_organizer
from downloads_organizer import gui

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_version_matches_pyproject():
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert downloads_organizer.__version__ == project["version"]


def test_app_name():
    assert downloads_organizer.APP_NAME == "DownloadsOrganizer"


def test_exe_and_windows_use_one_icon():
    """The EXE icon of the spec is the file the windows show (gui.APP_ICON), shipped with it."""
    root = PYPROJECT.parent
    spec = (root / "DownloadsOrganizer.spec").read_text(encoding="utf-8")
    assert 'ICON = ROOT / "downloads_organizer" / "assets" / "DownloadsOrganizer.ico"' in spec
    assert "icon=str(ICON)," in spec
    assert '[(str(ICON), "downloads_organizer/assets")]' in spec
    icon = root / "downloads_organizer" / "assets" / "DownloadsOrganizer.ico"
    assert icon.is_file()
    assert icon.resolve() == gui.APP_ICON
