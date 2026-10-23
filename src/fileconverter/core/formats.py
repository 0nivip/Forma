"""Static descriptions of the file formats the application knows about."""

from dataclasses import dataclass
from enum import Enum

# A signature is a set of (offset, bytes) checks that must ALL match the file header.
# Example: WebP files start with b"RIFF" at offset 0 and contain b"WEBP" at offset 8.
type Signature = tuple[tuple[int, bytes], ...]


class FormatFamily(Enum):
    IMAGE = "image"


@dataclass(frozen=True)
class Format:
    id: str  # stable identifier used everywhere in code, e.g. "jpeg"
    display_name: str  # human-readable name, e.g. "JPEG"
    family: FormatFamily
    extensions: tuple[str, ...]  # lowercase, with dot; the first one is used for output files
    signatures: tuple[Signature, ...]  # any one matching signature identifies the format

    @property
    def preferred_extension(self) -> str:
        return self.extensions[0]

    def matches_header(self, header: bytes) -> bool:
        return any(
            all(header[offset : offset + len(magic)] == magic for offset, magic in signature)
            for signature in self.signatures
        )


PNG = Format(
    id="png",
    display_name="PNG",
    family=FormatFamily.IMAGE,
    extensions=(".png",),
    signatures=(((0, b"\x89PNG\r\n\x1a\n"),),),
)

JPEG = Format(
    id="jpeg",
    display_name="JPEG",
    family=FormatFamily.IMAGE,
    extensions=(".jpg", ".jpeg", ".jpe", ".jfif"),
    signatures=(((0, b"\xff\xd8\xff"),),),
)

WEBP = Format(
    id="webp",
    display_name="WebP",
    family=FormatFamily.IMAGE,
    extensions=(".webp",),
    signatures=(((0, b"RIFF"), (8, b"WEBP")),),
)

BMP = Format(
    id="bmp",
    display_name="BMP",
    family=FormatFamily.IMAGE,
    extensions=(".bmp", ".dib"),
    signatures=(((0, b"BM"),),),
)

TIFF = Format(
    id="tiff",
    display_name="TIFF",
    family=FormatFamily.IMAGE,
    extensions=(".tif", ".tiff"),
    signatures=(
        ((0, b"II*\x00"),),  # little-endian
        ((0, b"MM\x00*"),),  # big-endian
    ),
)

BUILTIN_FORMATS: tuple[Format, ...] = (PNG, JPEG, WEBP, BMP, TIFF)

# Enough bytes to check every signature above.
HEADER_SIZE = 16
