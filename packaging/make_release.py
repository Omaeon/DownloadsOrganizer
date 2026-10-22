"""Pack the portable release of DownloadsOrganizer: the onedir build and its documents.

Run from the project root with the project venv (``build`` group), after the onedir
build of DownloadsOrganizer.spec (and, optionally, the onefile one):

    .venv\\Scripts\\python.exe packaging\\make_release.py
    .venv\\Scripts\\python.exe packaging\\make_release.py `
        --onefile dist\\onefile\\DownloadsOrganizer.exe

It writes only dist\\release:
    DownloadsOrganizer-<version>-win-x64-portable.zip  (one DownloadsOrganizer folder: the
        build, LICENSE.txt, README_RU/EN, RELEASE_NOTES, THIRD_PARTY_NOTICES, licenses,
        SHA256SUMS.txt)
    DownloadsOrganizer-<version>-win-x64-onefile.exe   (with --onefile)
    DownloadsOrganizer-<version>-win-x64-onefile-licenses.zip  (with --onefile: the same
        documents and licenses, to be distributed with the onefile EXE)
    SHA256SUMS.txt                                     (of the files above)
These release files are replaced when they exist; nothing else is written or removed.

Nothing is packed when the version differs between the sources, the EXE and the documents,
or when the build holds a file that must not ship (journals, settings, caches, specs) or
the text of a developer path. The texts looked for are this computer's: the project
folder, the home folder, the user name, the Python installation and the temp folder (and
generic ones such as ".venv"). They are searched in every file, in the ZIP files
(base_library.zip) and in the PyInstaller archive of an EXE: its Python modules (PYZ) and
the ZIP files in it, as UTF-8 and UTF-16.

Links: nothing is read or written through a symbolic link, a junction or another reparse
point. Every file read must be a plain file inside its tree (the onedir build, the release
documents, dist for --onefile) with no link on the way from the project folder; a link in
dist\\release or in place of a release file is refused, and a release file is written as a
new file that then replaces the old one (a link is never written through). The checks are
made before reading and writing: the script trusts the local build computer not to swap
files while it runs, it does not exclude such a concurrent change.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import getpass
import hashlib
import io
import os
import re
import stat
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from ctypes import wintypes
from pathlib import Path
from typing import NamedTuple

from PyInstaller.archive.readers import CArchiveReader
from PyInstaller.loader.pyimod01_archive import ZlibArchiveReader

ROOT = Path(__file__).resolve().parent.parent
NAME = "DownloadsOrganizer"
DIST = ROOT / "dist"
BUILD = DIST / NAME
DOCS = ROOT / "packaging" / "release"
LICENSE = ROOT / "LICENSE"  # MIT, DownloadsOrganizer's own code
OUT = DIST / "release"
DOCUMENTS = ("README_RU.txt", "README_EN.txt", "RELEASE_NOTES.txt", "THIRD_PARTY_NOTICES.txt")
LICENSE_NAME = "LICENSE.txt"  # LICENSE, as it is named in the package
LICENSES = "licenses"
SUMS = "SHA256SUMS.txt"
CARCHIVE = b"MEI\014\013\012\013\016"  # the cookie of the PyInstaller archive in an EXE
NESTING = 4  # archives in archives deeper than this are refused, not searched
REPLACE_ATTEMPTS = 20  # 0.5 s apart: a sync client or an antivirus may hold the old file

# names and suffixes that never ship: run-time data, caches and build leftovers
FORBIDDEN_NAMES = {"settings.json", "settings.json.lock", "__pycache__", ".pytest_cache"}
FORBIDDEN_SUFFIXES = {".log", ".jsonl", ".partial", ".pyc", ".spec", ".pdb", ".toc", ".lock"}
# ("site-packages" is in the standard library, "pytest-of-" in appmodel's journal filter)
GENERIC_MARKERS = (".venv", "scratchpad")


class ReleaseError(Exception):
    """The release cannot be packed; the message says why."""


class Item(NamedTuple):
    """A file of the release: its name in the package ("/"), where it was read, its bytes."""

    name: str
    source: Path
    data: bytes
    mtime: float
    mode: int


def version() -> str:
    """The one version of the program (downloads_organizer/__init__.py), read without import."""
    source = ROOT / "downloads_organizer" / "__init__.py"
    text = load("__init__.py", source, source.parent).data.decode("utf-8").replace("\r\n", "\n")
    found = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if found is None:
        raise ReleaseError("no __version__ in downloads_organizer/__init__.py")
    return found.group(1)


def exe_versions(exe: Path) -> dict[str, str]:
    """FileVersion / ProductVersion (string and fixed) of the EXE's VersionInfo."""
    api = ctypes.WinDLL("version", use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, wintypes.LPDWORD]
    api.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    api.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.PUINT,
    ]
    size = api.GetFileVersionInfoSizeW(str(exe), None)
    data = ctypes.create_string_buffer(size)
    if not size or not api.GetFileVersionInfoW(str(exe), 0, size, data):
        raise ReleaseError(f"{exe.name}: no VersionInfo ({ctypes.get_last_error()})")

    def query(key: str) -> ctypes.c_void_p:
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not api.VerQueryValueW(data, key, ctypes.byref(pointer), ctypes.byref(length)):
            raise ReleaseError(f"{exe.name}: no {key} in VersionInfo")
        return pointer

    result = {}
    for key in ("FileVersion", "ProductVersion", "ProductName"):
        result[key] = ctypes.wstring_at(query(rf"\StringFileInfo\041904B0\{key}"))
    fixed = ctypes.cast(query("\\"), ctypes.POINTER(wintypes.DWORD * 13)).contents
    for key, high, low in (("fixed FileVersion", 2, 3), ("fixed ProductVersion", 4, 5)):
        numbers = (fixed[high] >> 16, fixed[high] & 0xFFFF, fixed[low] >> 16, fixed[low] & 0xFFFF)
        result[key] = ".".join(map(str, numbers))
    return result


def check_versions(release: str, exes: list[Path], items: list[Item]) -> None:
    """The sources, every EXE and every shipped document name the same version."""
    project = (
        load("pyproject.toml", ROOT / "pyproject.toml", ROOT)
        .data.decode("utf-8")
        .replace("\r\n", "\n")
    )
    if f'\nversion = "{release}"\n' not in project:
        raise ReleaseError(f"pyproject.toml does not have version {release}")
    numbers = ".".join(re.match(r"(\d+)\.(\d+)\.(\d+)", release).groups()) + ".0"
    for exe in exes:
        found = exe_versions(exe)
        expected = {"FileVersion": release, "ProductVersion": release, "ProductName": NAME}
        expected |= {"fixed FileVersion": numbers, "fixed ProductVersion": numbers}
        wrong = {key: value for key, value in found.items() if value != expected[key]}
        if wrong:
            raise ReleaseError(f"{exe}: VersionInfo {wrong}, expected {expected}")
    for item in items:
        if item.name in DOCUMENTS and f"{NAME} {release}" not in item.data.decode("utf-8"):
            raise ReleaseError(f"{item.name} does not name {NAME} {release}")


def is_link(path: Path) -> bool:
    """A symbolic link, a junction or another reparse point (the path itself, not followed)."""
    return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def inside(path: Path, tree: Path, *, missing: bool = False) -> Path:
    """The full path when it is in the tree with no link from ROOT down to it; else refuse.

    With missing=True the path itself may not exist yet (a release file to write).
    """
    full = Path(os.path.abspath(path))  # noqa: PTH100 - ".." removed, links not followed
    if not full.is_relative_to(tree) or not tree.is_relative_to(ROOT):
        raise ReleaseError(f"{path}: not inside {tree}")
    for part in (full, *full.parents):
        if not part.is_relative_to(ROOT) or part == ROOT:
            break
        try:
            if is_link(part):
                raise ReleaseError(f"{part}: a link, a junction or another reparse point")
        except FileNotFoundError:
            if not (missing and part == full):
                raise ReleaseError(f"{part}: does not exist") from None
    real = full.resolve()
    if real != full:  # a link this check cannot see, or a short (8.3) name
        raise ReleaseError(f"{path}: leads to {real}")
    return full


def tree_files(top: Path) -> list[Path]:
    """Every file under top; a link or anything but a file or a folder is refused."""
    found, folders = [], [inside(top, top)]
    while folders:
        folder = folders.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                path, info = folder / entry.name, entry.stat(follow_symlinks=False)
                if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ReleaseError(f"{path}: a link, a junction or another reparse point")
                if stat.S_ISDIR(info.st_mode):
                    folders.append(path)
                elif stat.S_ISREG(info.st_mode):
                    found.append(path)
                else:
                    raise ReleaseError(f"{path}: neither a file nor a folder")
    return found


def load(name: str, source: Path, tree: Path) -> Item:
    """Read a plain file of the tree (see inside); its bytes are the ones checked and packed."""
    full = inside(source, tree)
    info = full.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ReleaseError(f"{source}: not a file")
    return Item(name, full, full.read_bytes(), info.st_mtime, info.st_mode)


def markers() -> list[str]:
    """Lower-case texts of this computer's paths and user that must not ship."""
    found = set(GENERIC_MARKERS)
    paths = (ROOT, Path.home(), Path(sys.base_prefix), Path(sys.prefix))
    for path in (*paths, Path(tempfile.gettempdir()).resolve()):
        # as Windows writes it, with "/", and escaped as in source code ("C:\\Users")
        text = str(path).lower()
        found |= {text, path.as_posix().lower(), text.replace("\\", "\\\\")}
    user = getpass.getuser().lower()
    if len(user) >= 3:  # a shorter name would match everywhere
        found.add(user)
    return sorted(found)


def contents(
    name: str, data: bytes, path: Path | None, depth: int = 0
) -> Iterator[tuple[str, bytes]]:
    """The bytes of a file and of every entry of the archives in it, at every depth: a ZIP
    (base_library.zip) and the PyInstaller archive of an EXE (path: the EXE on the disk),
    with its PYZ of Python modules and the ZIP files in it."""
    yield name, data
    lowered = name.lower()
    nested = lowered.endswith(".zip") or (path is not None and lowered.endswith(".exe"))
    if nested and depth >= NESTING:
        raise ReleaseError(f"{name}: archives nested deeper than {NESTING}")
    if lowered.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.namelist():
                yield from contents(f"{name}/{entry}", archive.read(entry), None, depth + 1)
    elif path is not None and lowered.endswith(".exe") and CARCHIVE in data:
        package = CArchiveReader(str(path))
        for entry, (offset, *_, typecode) in package.toc.items():
            if typecode == "z":  # the PYZ of the Python modules
                modules = ZlibArchiveReader(str(path), package._start_offset + offset)
                for module in modules.toc:
                    yield f"{name}/{entry}/{module}", modules.extract(module, raw=True) or b""
            else:
                yield from contents(f"{name}/{entry}", package.extract(entry), None, depth + 1)


def scan(items: list[Item]) -> int:
    """Refuse a forbidden file or a developer text; return the number of entries read."""
    needles = [(text, text.encode()) for text in markers()]
    needles += [(f"{text} (UTF-16)", text.encode("utf-16-le")) for text in markers()]
    problems, entries = [], 0
    for item in items:
        names = {part.lower() for part in item.name.split("/")}
        if names & FORBIDDEN_NAMES or Path(item.name).suffix.lower() in FORBIDDEN_SUFFIXES:
            problems.append(f"{item.name}: a file that must not ship")
        for entry, data in contents(Path(item.name).name, item.data, item.source):
            entries += 1
            lowered = data.lower()
            problems += [f"{item.name} [{entry}]: {text}" for text, b in needles if b in lowered]
    if problems:
        raise ReleaseError("forbidden content:\n  " + "\n  ".join(problems))
    return entries


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def documents() -> list[Item]:
    """LICENSE.txt, the README / RELEASE_NOTES / THIRD_PARTY_NOTICES and the licenses folder."""
    items = [load(LICENSE_NAME, LICENSE, ROOT)]
    items += [load(name, DOCS / name, DOCS) for name in DOCUMENTS]
    texts = DOCS / LICENSES
    items += [load(f"{LICENSES}/{p.name}", p, texts) for p in tree_files(texts)]
    return items


def package(items: list[Item]) -> list[Item]:
    """The items in a fixed order; two of the same name (or one named SHA256SUMS) refused."""
    names = [item.name.lower() for item in items]
    if len(set(names)) != len(names) or SUMS.lower() in names:
        raise ReleaseError("two files of the package have the same name")
    return sorted(items)


def package_files() -> list[Item]:
    """The portable package: the onedir build and the documents."""
    if not (BUILD / f"{NAME}.exe").is_file() or not (BUILD / "_internal").is_dir():
        raise ReleaseError(f"no onedir build in {BUILD}; run DownloadsOrganizer.spec first")
    build = [load(p.relative_to(BUILD).as_posix(), p, BUILD) for p in tree_files(BUILD)]
    return package(build + documents())


def write_zip(folder: str, items: list[Item]) -> bytes:
    """A ZIP of one folder: the items, then SHA256SUMS.txt of all of them."""
    sums, buffer = [], io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for item in items:
            sums.append(f"{sha256(item.data)}  {item.name}")
            info = zipfile.ZipInfo(f"{folder}/{item.name}", time.localtime(item.mtime)[:6])
            info.external_attr = (item.mode & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, item.data)
        text = "\n".join(sums) + "\n"
        info = zipfile.ZipInfo(f"{folder}/{SUMS}", date_time=info.date_time)
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, text.encode("ascii"))
    with zipfile.ZipFile(buffer) as archive:
        if archive.testzip() is not None:
            raise ReleaseError(f"{folder}: the ZIP is damaged")
    return buffer.getvalue()


def output_folder() -> Path:
    """dist\\release: a real folder, with no link on the way to it and no link in it."""
    inside(DIST, DIST)
    with contextlib.suppress(FileExistsError):
        OUT.mkdir()
    inside(OUT, OUT)
    links = sorted(path.name for path in OUT.iterdir() if is_link(path))
    if links:
        raise ReleaseError(f"{OUT}: unexpected links {links}")
    return OUT


def write_file(target: Path, data: bytes) -> None:
    """Write data as a new file of the release folder, then put it in place of target.

    The old target must be a plain file (or absent): a link is never written through.
    """
    inside(target, OUT, missing=True)
    if os.path.lexists(target) and not stat.S_ISREG(target.lstat().st_mode):
        raise ReleaseError(f"{target}: not a plain file")
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=OUT)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                Path(temporary).replace(target)
                break
            except PermissionError as error:  # the old file is open elsewhere (sync, antivirus)
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise ReleaseError(
                        f"{target.name} is in use and was not replaced ({error.strerror}); "
                        f"{OUT} is incomplete, run the script again"
                    ) from None
                time.sleep(0.5)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    options = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    options.add_argument("--onefile", type=Path, help="the onefile EXE to ship next to the ZIP")
    args = options.parse_args(argv)
    try:
        release = version()
        files = package_files()
        exes = [BUILD / f"{NAME}.exe"]
        if args.onefile:
            onefile = load(f"{NAME}.exe", args.onefile, DIST)
            if onefile.source.is_relative_to(OUT):
                raise ReleaseError(f"{args.onefile}: the onefile EXE must not be in {OUT}")
            exes.append(onefile.source)
            notices = package(documents())
        check_versions(release, exes, files)
        entries = scan(files)
        if args.onefile:
            entries += scan([onefile])
        output_folder()
        prefix = OUT / f"{NAME}-{release}-win-x64"
        shipped = [(prefix.with_name(f"{prefix.name}-portable.zip"), write_zip(NAME, files))]
        if args.onefile:
            shipped.append((prefix.with_name(f"{prefix.name}-onefile.exe"), onefile.data))
            notices_zip = write_zip(f"{NAME}-{release}-licenses", notices)
            shipped.append((prefix.with_name(f"{prefix.name}-onefile-licenses.zip"), notices_zip))
        for target, data in shipped:
            write_file(target, data)
        lines = [f"{sha256(data)}  {target.name}" for target, data in shipped]
        write_file(OUT / SUMS, ("\n".join(lines) + "\n").encode("ascii"))
    except ReleaseError as error:
        print(f"release refused: {error}", file=sys.stderr)
        return 1
    print(f"{NAME} {release}: {len(files) + 1} files packed, {entries} entries scanned")
    for line in lines:
        print(line)
    for target, data in shipped:
        print(f"{target.name}: {len(data)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
