"""The documents of the portable release and the checks of packaging/make_release.py."""

import _winapi
import hashlib
import io
import re
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from downloads_organizer import APP_NAME, __version__

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "packaging" / "release"
DOCUMENTS = ("README_RU.txt", "README_EN.txt", "RELEASE_NOTES.txt", "THIRD_PARTY_NOTICES.txt")
MIT = """\
MIT License

Copyright (c) 2026 Omaeon

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


@pytest.fixture(scope="module")
def make_release():
    pytest.importorskip("PyInstaller", reason="the build group is not installed")
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        import make_release
    finally:
        sys.path.remove(str(ROOT / "packaging"))
    return make_release


@pytest.fixture
def project(make_release, tmp_path, monkeypatch):
    """A project folder in tmp_path: a fake onedir build, the release documents, LICENSE."""
    root = tmp_path / "project"
    build = root / "dist" / APP_NAME
    (build / "_internal").mkdir(parents=True)
    (build / f"{APP_NAME}.exe").write_bytes(b"MZ fake launcher")
    (build / "_internal" / "python314.dll").write_bytes(b"MZ fake runtime")
    shutil.copytree(DOCS, root / "packaging" / "release")
    shutil.copy(ROOT / "LICENSE", root / "LICENSE")
    for name, value in {
        "ROOT": root,
        "DIST": root / "dist",
        "BUILD": build,
        "DOCS": root / "packaging" / "release",
        "LICENSE": root / "LICENSE",
        "OUT": root / "dist" / "release",
    }.items():
        monkeypatch.setattr(make_release, name, value)
    monkeypatch.setattr(make_release, "version", lambda: __version__)
    monkeypatch.setattr(make_release, "check_versions", lambda *args: None)  # fake EXEs
    return root


def junction(link: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(target), str(link))


def symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"no privilege to create a symbolic link: {error}")


def item(make_release, path: Path):
    return make_release.Item(path.name, path, path.read_bytes(), 0.0, 0o100666)


def snapshot(folder: Path) -> dict[str, bytes]:
    return {
        p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize("name", DOCUMENTS)
def test_every_document_names_the_release_version(name):
    text = (DOCS / name).read_text(encoding="utf-8")
    assert f"{APP_NAME} {__version__}" in text


@pytest.mark.parametrize("name", ["README_RU.txt", "README_EN.txt"])
def test_the_readme_names_the_zip_and_the_data_folder(name):
    text = (DOCS / name).read_text(encoding="utf-8")
    assert f"{APP_NAME}-{__version__}-win-x64-portable.zip" in text
    assert r"%LOCALAPPDATA%\DownloadsOrganizer\settings.json" in text
    assert r"%LOCALAPPDATA%\DownloadsOrganizer\logs" in text


def test_every_license_the_notices_name_is_shipped():
    notices = (DOCS / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    named = set(re.findall(r"licenses\\([\w.-]+\.(?:txt))", notices))
    shipped = {path.name for path in (DOCS / "licenses").iterdir()}
    assert named
    assert named <= shipped
    assert shipped <= named, "a license file the notices do not mention"


def test_the_license_is_the_mit_text_without_an_e_mail():
    text = (ROOT / "LICENSE").read_text(encoding="ascii")
    assert text == MIT
    assert "@" not in text


def test_the_project_metadata_names_the_mit_license():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]


@pytest.mark.parametrize("name", DOCUMENTS)
def test_every_document_names_the_mit_license_and_its_file(name):
    text = (DOCS / name).read_text(encoding="utf-8")
    assert "MIT" in text
    assert "LICENSE.txt" in text
    assert "Copyright (c) 2026 Omaeon" in text


def test_the_version_check_reads_the_source_version(make_release):
    assert make_release.version() == __version__


def test_a_clean_file_passes_the_scan(make_release, tmp_path):
    (tmp_path / "a.txt").write_text("DownloadsOrganizer", encoding="utf-8")
    assert make_release.scan([item(make_release, tmp_path / "a.txt")]) == 1


@pytest.mark.parametrize("name", ["run.jsonl", "settings.json", "x.log", "a.partial", "m.pyc"])
def test_a_file_that_must_not_ship_is_refused(make_release, tmp_path, name):
    (tmp_path / name).write_bytes(b"")
    with pytest.raises(make_release.ReleaseError, match="must not ship"):
        make_release.scan([item(make_release, tmp_path / name)])


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le"])
def test_a_developer_path_in_a_file_is_refused(make_release, tmp_path, encoding):
    text = f"built in {str(Path.home()).upper()}\\project"
    (tmp_path / "a.dll").write_bytes(b"\0MZ" + text.encode(encoding))
    with pytest.raises(make_release.ReleaseError, match="forbidden content"):
        make_release.scan([item(make_release, tmp_path / "a.dll")])


def test_a_developer_path_inside_a_zip_entry_is_refused(make_release, tmp_path):
    with zipfile.ZipFile(tmp_path / "base_library.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mod.py", f"PATH = {str(ROOT)!r}\n" * 50)
    with pytest.raises(make_release.ReleaseError, match=r"base_library\.zip/mod\.py"):
        make_release.scan([item(make_release, tmp_path / "base_library.zip")])


def test_a_developer_path_in_a_zip_inside_a_zip_is_refused(make_release, tmp_path):
    with zipfile.ZipFile(tmp_path / "inner.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mod.py", f"PATH = {str(ROOT)!r}\n")
    with zipfile.ZipFile(tmp_path / "outer.zip", "w") as archive:
        archive.write(tmp_path / "inner.zip", "inner.zip")
    with pytest.raises(make_release.ReleaseError, match=r"outer\.zip/inner\.zip/mod\.py"):
        make_release.scan([item(make_release, tmp_path / "outer.zip")])


def test_archives_nested_too_deep_are_refused(make_release, tmp_path):
    data = b"clean"
    for _ in range(make_release.NESTING + 1):
        path = tmp_path / "a.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("a.zip", data)
        data = path.read_bytes()
    with pytest.raises(make_release.ReleaseError, match="nested deeper"):
        make_release.scan([item(make_release, path)])


def carchive(exe: Path, text: str) -> None:
    """A PyInstaller archive, as at the end of an onefile EXE, holding a base_library.zip."""
    from PyInstaller.archive.writers import CArchiveWriter

    library = exe.with_name("base_library.zip")
    with zipfile.ZipFile(library, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mod.py", text)
    CArchiveWriter(str(exe), [("base_library.zip", str(library), True, "x")], "python314.dll")
    library.unlink()


def test_a_developer_path_in_the_base_library_of_an_exe_is_refused(make_release, tmp_path):
    carchive(tmp_path / "app.exe", f"PATH = {str(ROOT)!r}\n")
    with pytest.raises(make_release.ReleaseError, match=r"app\.exe/base_library\.zip/mod\.py"):
        make_release.scan([item(make_release, tmp_path / "app.exe")])


def test_the_base_library_of_an_exe_is_searched_when_clean(make_release, tmp_path):
    carchive(tmp_path / "app.exe", "X = 1\n")
    # the EXE, its base_library.zip, mod.py
    assert make_release.scan([item(make_release, tmp_path / "app.exe")]) == 3


def test_the_portable_package_holds_the_build_the_license_and_the_documents(make_release, project):
    names = [entry.name for entry in make_release.package_files()]
    assert f"{APP_NAME}.exe" in names
    assert "_internal/python314.dll" in names
    assert {"LICENSE.txt", *DOCUMENTS} <= set(names)
    assert {f"licenses/{p.name}" for p in (DOCS / "licenses").iterdir()} <= set(names)
    data = make_release.write_zip(APP_NAME, make_release.package_files())
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.read(f"{APP_NAME}/LICENSE.txt").decode("ascii") == MIT


def test_a_junction_in_the_build_is_refused(make_release, project, tmp_path):
    (tmp_path / "elsewhere" / "secret.txt").parent.mkdir()
    (tmp_path / "elsewhere" / "secret.txt").write_text("not for the package")
    junction(project / "dist" / APP_NAME / "_internal" / "linked", tmp_path / "elsewhere")
    with pytest.raises(make_release.ReleaseError, match="reparse point"):
        make_release.package_files()


def test_a_symbolic_link_in_the_build_is_refused(make_release, project, tmp_path):
    (tmp_path / "secret.txt").write_text("not for the package")
    symlink(project / "dist" / APP_NAME / "_internal" / "secret.txt", tmp_path / "secret.txt")
    with pytest.raises(make_release.ReleaseError, match="reparse point"):
        make_release.package_files()


def test_a_build_reached_through_a_junction_is_refused(make_release, project, tmp_path):
    build = project / "dist" / APP_NAME
    shutil.move(build, tmp_path / "moved")
    junction(build, tmp_path / "moved")
    with pytest.raises(make_release.ReleaseError, match="reparse point"):
        make_release.package_files()


def test_a_file_outside_its_tree_is_refused(make_release, project, tmp_path):
    (tmp_path / "outside.exe").write_bytes(b"MZ")
    build = project / "dist" / APP_NAME
    for path in (tmp_path / "outside.exe", build / ".." / ".." / ".." / "outside.exe"):
        with pytest.raises(make_release.ReleaseError, match="not inside"):
            make_release.load("x.exe", path, build)


def test_an_onefile_exe_outside_dist_is_refused(make_release, project, tmp_path):
    (tmp_path / "outside.exe").write_bytes(b"MZ")
    assert make_release.main(["--onefile", str(tmp_path / "outside.exe")]) == 1
    assert not (project / "dist" / "release").exists()


def test_the_release_is_written_only_in_the_release_folder(make_release, project, capsys):
    (project / "dist" / "onefile").mkdir()
    (project / "dist" / "onefile" / f"{APP_NAME}.exe").write_bytes(b"MZ fake onefile")
    before = snapshot(project)
    onefile = str(project / "dist" / "onefile" / f"{APP_NAME}.exe")
    assert make_release.main(["--onefile", onefile]) == 0, capsys.readouterr().err
    after = snapshot(project)
    written = {name: data for name, data in after.items() if name not in before}
    assert {name: after[name] for name in before} == before
    prefix = f"dist/release/{APP_NAME}-{__version__}-win-x64"
    assert set(written) == {
        f"{prefix}-portable.zip",
        f"{prefix}-onefile.exe",
        f"{prefix}-onefile-licenses.zip",
        "dist/release/SHA256SUMS.txt",
    }
    sums = written.pop("dist/release/SHA256SUMS.txt").decode("ascii").splitlines()
    assert sorted(sums) == sorted(
        f"{hashlib.sha256(data).hexdigest()}  {name.rsplit('/', 1)[1]}"
        for name, data in written.items()
    )
    with zipfile.ZipFile(project / f"{prefix}-onefile-licenses.zip") as archive:
        names = {name.split("/", 1)[1] for name in archive.namelist()}
    assert {"LICENSE.txt", "SHA256SUMS.txt", *DOCUMENTS} <= names
    assert not any(name.endswith((".exe", ".dll")) for name in names)


def test_a_release_file_replaces_the_old_one_and_leaves_no_temporary(make_release, project):
    out = make_release.output_folder()
    (out / "a.zip").write_bytes(b"old")
    make_release.write_file(out / "a.zip", b"new")
    assert snapshot(out) == {"a.zip": b"new"}


def test_a_junction_as_the_release_folder_is_refused_and_nothing_is_written(
    make_release, project, tmp_path
):
    junction(project / "dist" / "release", tmp_path / "elsewhere")
    assert make_release.main([]) == 1
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_a_junction_in_place_of_a_release_file_is_refused(make_release, project, tmp_path):
    out = project / "dist" / "release"
    out.mkdir()
    zip_name = f"{APP_NAME}-{__version__}-win-x64-portable.zip"
    junction(out / zip_name, tmp_path / "elsewhere")
    assert make_release.main([]) == 1
    assert list((tmp_path / "elsewhere").iterdir()) == []
    assert [p.name for p in out.iterdir()] == [zip_name]


def test_a_symbolic_link_in_place_of_a_release_file_is_not_written_through(
    make_release, project, tmp_path
):
    out = project / "dist" / "release"
    out.mkdir()
    (tmp_path / "victim.txt").write_bytes(b"someone else's file")
    symlink(out / "SHA256SUMS.txt", tmp_path / "victim.txt")
    assert make_release.main([]) == 1
    with pytest.raises(make_release.ReleaseError, match="reparse point"):
        make_release.write_file(out / "SHA256SUMS.txt", b"x")
    assert (tmp_path / "victim.txt").read_bytes() == b"someone else's file"


def test_a_release_file_in_use_is_retried_then_refused_without_a_temporary(
    make_release, project, monkeypatch
):
    out = make_release.output_folder()
    (out / "a.zip").write_bytes(b"old")
    calls = []

    def in_use(self, target):
        calls.append(target)
        raise PermissionError(13, "in use")

    monkeypatch.setattr(make_release, "REPLACE_ATTEMPTS", 3)
    monkeypatch.setattr(make_release.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(Path, "replace", in_use)
    with pytest.raises(make_release.ReleaseError, match="in use and was not replaced"):
        make_release.write_file(out / "a.zip", b"new")
    assert len(calls) == 3
    assert snapshot(out) == {"a.zip": b"old"}


def test_a_folder_in_place_of_a_release_file_is_refused(make_release, project):
    out = make_release.output_folder()
    (out / "a.zip").mkdir()
    with pytest.raises(make_release.ReleaseError, match="not a plain file"):
        make_release.write_file(out / "a.zip", b"new")
    assert [p.name for p in out.iterdir()] == ["a.zip"]


def test_the_version_is_read_from_a_plain_file(make_release, tmp_path, monkeypatch):
    (tmp_path / "downloads_organizer").mkdir()
    (tmp_path / "downloads_organizer" / "__init__.py").write_text('__version__ = "9.8.7"\n')
    monkeypatch.setattr(make_release, "ROOT", tmp_path)
    assert make_release.version() == "9.8.7"


def test_the_version_is_not_read_through_a_junction(make_release, tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    junction(root / "downloads_organizer", tmp_path / "outside")
    (tmp_path / "outside" / "__init__.py").write_text('__version__ = "1.0.0"\n')
    monkeypatch.setattr(make_release, "ROOT", root)
    with pytest.raises(make_release.ReleaseError, match="reparse point"):
        make_release.version()


def test_pyproject_is_not_read_through_a_symbolic_link(make_release, tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
    monkeypatch.setattr(make_release, "ROOT", root)
    symlink(root / "pyproject.toml", tmp_path / "pyproject.toml")
    with pytest.raises(make_release.ReleaseError, match="reparse point"):
        make_release.check_versions("1.0.0", [], [])


def test_pyproject_of_another_version_is_refused(make_release, tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.9.0"\n')
    monkeypatch.setattr(make_release, "ROOT", tmp_path)
    make_release.check_versions("0.9.0", [], [])
    with pytest.raises(make_release.ReleaseError, match=r"does not have version 1\.0\.0"):
        make_release.check_versions("1.0.0", [], [])
