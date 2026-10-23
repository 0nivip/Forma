"""Shared test helpers. Test images are generated on the fly, so no binary fixtures."""

import struct
import zlib
from pathlib import Path

from PIL import Image


def make_image(
    path: Path,
    pillow_format: str = "PNG",
    mode: str = "RGB",
    size: tuple[int, int] = (16, 8),
    color: object = (200, 50, 50),
    **save_args: object,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, color).save(path, format=pillow_format, **save_args)
    return path


def write_png16(path: Path, pixel: tuple[int, ...], size: tuple[int, int] = (4, 2)) -> Path:
    """Write a 16-bit-per-channel PNG (RGB or RGBA). Pillow itself cannot save these."""
    color_type = {3: 2, 4: 6}[len(pixel)]
    width, height = size

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data)
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    row = b"\x00" + struct.pack(f">{len(pixel)}H", *pixel) * width  # filter byte + pixels
    ihdr = struct.pack(">IIBBBBB", width, height, 16, color_type, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )
    return path


def write_tiff16(path: Path, pixel: tuple[int, ...], size: tuple[int, int] = (4, 2)) -> Path:
    """Write an uncompressed 16-bit-per-channel RGB or RGBA TIFF."""
    channels = len(pixel)
    width, height = size
    bits_per_sample = struct.pack(f"<{channels}H", *([16] * channels))
    pixels = struct.pack(f"<{channels}H", *pixel) * (width * height)
    bits_offset = 8
    pixels_offset = bits_offset + len(bits_per_sample)
    ifd_offset = pixels_offset + len(pixels)

    short, long = 3, 4
    entries = [
        (256, short, 1, width),
        (257, short, 1, height),
        (258, short, channels, bits_offset),
        (259, short, 1, 1),  # no compression
        (262, short, 1, 2),  # RGB
        (273, long, 1, pixels_offset),
        (277, short, 1, channels),
        (278, short, 1, height),
        (279, long, 1, len(pixels)),
        (284, short, 1, 1),  # chunky pixels
    ]
    if channels == 4:
        entries.append((338, short, 1, 2))  # extra sample = unassociated alpha
    ifd = struct.pack("<H", len(entries))
    for tag, kind, count, value in entries:
        if kind == short and count == 1:
            ifd += struct.pack("<HHIHH", tag, kind, count, value, 0)
        else:
            ifd += struct.pack("<HHII", tag, kind, count, value)
    ifd += struct.pack("<I", 0)  # no next IFD
    path.write_bytes(b"II*\x00" + struct.pack("<I", ifd_offset) + bits_per_sample + pixels + ifd)
    return path


def temp_files(directory: Path) -> list[Path]:
    """Leftover runner temp files in `directory` (should always be empty after a job)."""
    return list(directory.glob(".fileconverter-*"))
