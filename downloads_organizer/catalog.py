"""Built-in file categories and extensions.

The ten categories and forty extensions, and the category of every extension, are
carried over unchanged from the legacy ``file_organizer.py``. Each category splits its
extensions into ``common`` (always shown) and ``rare`` (shown behind an expander); the
split and the order inside each part are the approved display order, which differs
from the flat legacy order. They affect display only, not classification.
"""

from __future__ import annotations

from dataclasses import dataclass

from downloads_organizer.pathsafety import check_folder_name


@dataclass(frozen=True, slots=True)
class Category:
    """A target category. ``key`` is the stable identifier, ``folder`` the directory name.

    ``folder`` must be exactly one safe Windows directory name (no separators, drive,
    ``..``, reserved device name, ...); otherwise ``UnsafeFolderNameError`` is raised.
    """

    key: str
    folder: str
    icon: str
    common: tuple[str, ...]
    rare: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        check_folder_name(self.folder)

    @property
    def extensions(self) -> tuple[str, ...]:
        """All built-in extensions of the category, common first."""
        return self.common + self.rare


DEFAULT_CATEGORIES: tuple[Category, ...] = (
    Category("Torrent", "Torrent", "⬇", (".torrent",)),
    Category("PDF", "PDF", "📄", (".pdf",)),
    Category("Images", "Images", "🖼", (".jpg", ".jpeg", ".png", ".gif", ".webp"), (".bmp",)),
    Category("Office", "Office", "📊", (".docx", ".xlsx", ".pptx", ".csv"), (".doc", ".xls")),
    Category("EXE", "EXE", "⚙", (".exe", ".msi")),
    Category("Video", "Video", "🎬", (".mp4", ".avi", ".mkv", ".mov"), (".wmv",)),
    Category("TXT", "TXT", "📝", (".txt", ".md"), (".log",)),
    Category("Archive", "Archive", "🗜", (".zip", ".rar", ".7z"), (".tar", ".gz")),
    Category("Audio", "Audio", "🎵", (".mp3", ".wav", ".flac"), (".aac", ".ogg")),
    Category("Code", "Code", "💻", (".py", ".js", ".ts", ".html", ".css", ".json")),
)

# Extensions of downloads that are still in progress (browsers, torrent and download
# managers). Such files are never organizer inputs and cannot be added as custom
# extensions.
PARTIAL_DOWNLOAD_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".part",
        ".partial",
        ".crdownload",
        ".download",
        ".opdownload",
        ".tmp",
        ".!ut",
        ".!qb",
        ".aria2",
    }
)


def category_by_key(key: str, categories: tuple[Category, ...] = DEFAULT_CATEGORIES) -> Category:
    """Return the category with ``key``; raise ``KeyError`` if there is none."""
    for category in categories:
        if category.key == key:
            return category
    raise KeyError(key)
