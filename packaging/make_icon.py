"""Build ``downloads_organizer/assets/DownloadsOrganizer.ico`` from CustomTkinter's icon.

The window of the program has always shown CustomTkinter's blue icon (CustomTkinter sets
it on every window that sets none); the EXE, the window and the taskbar now use one file
with that same picture. CustomTkinter ships it as a single 256x256 PNG; this script keeps
that image byte for byte as the 256x256 entry and adds the smaller sizes Windows asks for,
scaled down from it, as 32-bit BMP entries. The result is checked into the repository;
run this only to make it again (Pillow is needed here only, not by the program):

    uv run --no-project --with customtkinter==6.0.0 --with pillow==12.2.0 `
        python packaging/make_icon.py
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

import customtkinter
from PIL import Image

SOURCE = Path(customtkinter.__file__).parent / "assets" / "icons" / "CustomTkinter_icon_Windows.ico"
TARGET = Path(__file__).resolve().parent.parent / "downloads_organizer" / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128)  # plus 256 (the original)


def source_png(ico: bytes) -> bytes:
    """The one 256x256 PNG entry of CustomTkinter's icon."""
    _, kind, count = struct.unpack_from("<HHH", ico)
    assert (kind, count) == (1, 1), "CustomTkinter's icon is expected to have one entry"
    width, height, _, _, _, _, size, offset = struct.unpack_from("<BBBBHHII", ico, 6)
    png = ico[offset : offset + size]
    assert (width, height) == (0, 0) and png.startswith(b"\x89PNG"), "a 256x256 PNG entry"
    return png


def bmp_entry(image: Image.Image) -> bytes:
    """A 32-bit icon bitmap: BITMAPINFOHEADER, BGRA rows bottom-up, the AND mask."""
    width, height = image.size
    header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = image.tobytes("raw", "BGRA", 0, -1)  # bottom-up
    mask_row = ((width + 31) // 32) * 4
    return header + pixels + bytes(mask_row * height)  # alpha decides; the mask is clear


def main() -> None:
    png = source_png(SOURCE.read_bytes())
    base = Image.open(io.BytesIO(png)).convert("RGBA")
    entries = [
        (size, bmp_entry(base.resize((size, size), Image.Resampling.LANCZOS))) for size in SIZES
    ]
    entries.append((256, png))
    out = bytearray(struct.pack("<HHH", 0, 1, len(entries)))
    offset = 6 + 16 * len(entries)
    for size, data in entries:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in entries:
        out += data
    TARGET.mkdir(exist_ok=True)
    (TARGET / "DownloadsOrganizer.ico").write_bytes(out)


if __name__ == "__main__":
    main()
