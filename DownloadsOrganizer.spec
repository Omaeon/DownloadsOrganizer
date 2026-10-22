# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build of DownloadsOrganizer: the window only, no console.

From the project root, with the project venv (``build`` group of pyproject.toml / uv.lock):

    uv sync --locked --group build
    $env:PYINSTALLER_CONFIG_DIR = "build\\pyinstaller-cache"  # PyInstaller's cache stays here

    # onedir (default) -> dist\\DownloadsOrganizer\\DownloadsOrganizer.exe
    .venv\\Scripts\\pyinstaller.exe --noconfirm --clean DownloadsOrganizer.spec

    # onefile -> dist\\onefile\\DownloadsOrganizer.exe
    .venv\\Scripts\\pyinstaller.exe --noconfirm --clean --distpath dist\\onefile `
        --workpath build\\onefile DownloadsOrganizer.spec -- --onefile

Every path is taken relative to this file (SPECPATH); none of the developer's is written
here. The build packages the program as it is: it changes nothing in what it does.
"""

import argparse
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

options = argparse.ArgumentParser(prog="DownloadsOrganizer.spec")
options.add_argument("--onefile", action="store_true", help="one EXE instead of a folder")
ONEFILE = options.parse_args().onefile

ROOT = Path(SPECPATH)  # noqa: F821 - defined by PyInstaller
NAME = "DownloadsOrganizer"
# the one icon of the program: the EXE file here, the windows and the taskbar at run time
# (downloads_organizer.gui.APP_ICON, shipped in the same place); see packaging/make_icon.py
ICON = ROOT / "downloads_organizer" / "assets" / "DownloadsOrganizer.ico"

# The one version of the program (downloads_organizer/__init__.py), read without importing.
VERSION = re.search(
    r'^__version__ = "([^"]+)"$',
    (ROOT / "downloads_organizer" / "__init__.py").read_text(encoding="utf-8"),
    re.MULTILINE,
).group(1)
NUMBERS = tuple(int(n) for n in re.match(r"(\d+)\.(\d+)\.(\d+)", VERSION).groups()) + (0,)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMBERS, prodvers=NUMBERS),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "041904B0",  # Russian, Unicode
                    [
                        StringStruct("ProductName", NAME),
                        StringStruct("FileDescription", NAME),
                        StringStruct("InternalName", NAME),
                        StringStruct("OriginalFilename", f"{NAME}.exe"),
                        StringStruct("FileVersion", VERSION),
                        StringStruct("ProductVersion", VERSION),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0419, 1200])]),
    ],
)

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # themes, fonts and the window icon of CustomTkinter (also what its contrib hook adds)
    datas=collect_data_files("customtkinter") + [(str(ICON), "downloads_organizer/assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,  # keep assert statements as in the source
)
pyz = PYZ(a.pure)

common = dict(
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
    icon=str(ICON),
)

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], runtime_tmpdir=None, **common)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
