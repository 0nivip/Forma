"""Static descriptions of the file formats the application knows about."""

from dataclasses import dataclass
from enum import Enum

# A signature is a set of (offset, bytes) checks that must ALL match the file header.
# Example: WebP files start with b"RIFF" at offset 0 and contain b"WEBP" at offset 8.
# An offset of None means "anywhere in the header", for fields without a fixed position.
type Signature = tuple[tuple[int | None, bytes], ...]


class FormatFamily(Enum):
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"  # a container that usually holds video (and often audio)
    DOCUMENT = "document"


@dataclass(frozen=True)
class Format:
    id: str  # stable identifier used everywhere in code, e.g. "jpeg"
    display_name: str  # human-readable name, e.g. "JPEG"
    family: FormatFamily
    extensions: tuple[str, ...]  # lowercase, with dot; the first one is used for output files
    # Any one matching signature identifies the format. Output-only formats have none.
    signatures: tuple[Signature, ...]
    # For ZIP-based formats (DOCX, XLSX): an entry the archive must contain.
    zip_member: str | None = None

    @property
    def can_be_source(self) -> bool:
        return bool(self.signatures)

    @property
    def preferred_extension(self) -> str:
        return self.extensions[0]

    def matches_header(self, header: bytes) -> bool:
        return self.match_strength(header) > 0

    def match_strength(self, header: bytes) -> int:
        """Number of signature bytes that matched (0 = no match).

        Several formats share a container (MP4/MOV/M4A, MKV/WebM, Ogg/Opus); the more
        specific signature matches more bytes, so the registry can prefer it.
        """
        return max(
            (
                sum(len(magic) for _, magic in signature)
                for signature in self.signatures
                if all(_matches(header, offset, magic) for offset, magic in signature)
            ),
            default=0,
        )


def _matches(header: bytes, offset: int | None, magic: bytes) -> bool:
    if offset is None:
        return magic in header
    return header[offset : offset + len(magic)] == magic


# --- images -----------------------------------------------------------------

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

# --- video containers ---------------------------------------------------------
# Signatures only pick a likely container; ffprobe validates the actual streams
# before anything is converted.

EBML = b"\x1a\x45\xdf\xa3"  # Matroska/WebM header

MP4 = Format(
    id="mp4",
    display_name="MP4",
    family=FormatFamily.VIDEO,
    extensions=(".mp4", ".m4v"),
    signatures=(((4, b"ftyp"),),),  # generic ISO media; brand-specific formats win
)

MKV = Format(
    id="mkv",
    display_name="MKV",
    family=FormatFamily.VIDEO,
    extensions=(".mkv",),
    signatures=(((0, EBML),),),
)

WEBM = Format(
    id="webm",
    display_name="WebM",
    family=FormatFamily.VIDEO,
    extensions=(".webm",),
    signatures=(((0, EBML), (None, b"webm")),),  # EBML DocType "webm"
)

MOV = Format(
    id="mov",
    display_name="MOV",
    family=FormatFamily.VIDEO,
    extensions=(".mov", ".qt"),
    signatures=(
        ((4, b"ftyp"), (8, b"qt  ")),
        ((4, b"moov"),),  # older QuickTime files without an ftyp box
        ((4, b"wide"),),
        ((4, b"mdat"),),
    ),
)

AVI = Format(
    id="avi",
    display_name="AVI",
    family=FormatFamily.VIDEO,
    extensions=(".avi",),
    signatures=(((0, b"RIFF"), (8, b"AVI ")),),
)

# --- audio --------------------------------------------------------------------

MP3 = Format(
    id="mp3",
    display_name="MP3",
    family=FormatFamily.AUDIO,
    extensions=(".mp3",),
    signatures=(
        ((0, b"ID3"),),  # ID3v2 tag in front of the audio
        ((0, b"\xff\xfb"),),  # MPEG-1 Layer III frame sync
        ((0, b"\xff\xfa"),),
        ((0, b"\xff\xf3"),),  # MPEG-2 Layer III
        ((0, b"\xff\xf2"),),
    ),
)

WAV = Format(
    id="wav",
    display_name="WAV",
    family=FormatFamily.AUDIO,
    extensions=(".wav",),
    signatures=(((0, b"RIFF"), (8, b"WAVE")),),
)

FLAC = Format(
    id="flac",
    display_name="FLAC",
    family=FormatFamily.AUDIO,
    extensions=(".flac",),
    signatures=(((0, b"fLaC"),),),
)

AAC = Format(
    id="aac",
    display_name="AAC",
    family=FormatFamily.AUDIO,
    extensions=(".aac",),
    signatures=tuple(((0, bytes([0xFF, second])),) for second in (0xF1, 0xF9, 0xF0, 0xF8)),
)

M4A = Format(
    id="m4a",
    display_name="M4A",
    family=FormatFamily.AUDIO,
    extensions=(".m4a",),
    signatures=(((4, b"ftyp"), (8, b"M4A ")),),
)

OGG = Format(
    id="ogg",
    display_name="OGG",
    family=FormatFamily.AUDIO,
    extensions=(".ogg", ".oga"),
    signatures=(((0, b"OggS"),),),
)

OPUS = Format(
    id="opus",
    display_name="Opus",
    family=FormatFamily.AUDIO,
    extensions=(".opus",),
    signatures=(((0, b"OggS"), (28, b"OpusHead")),),  # Ogg stream whose first packet is Opus
)

# --- documents ----------------------------------------------------------------

ZIP = b"PK\x03\x04"

PDF = Format(
    id="pdf",
    display_name="PDF",
    family=FormatFamily.DOCUMENT,
    extensions=(".pdf",),
    signatures=(((0, b"%PDF-"),),),
)

DOCX = Format(
    id="docx",
    display_name="DOCX",
    family=FormatFamily.DOCUMENT,
    extensions=(".docx",),
    signatures=(((0, ZIP),),),
    zip_member="word/document.xml",
)

XLSX = Format(
    id="xlsx",
    display_name="XLSX",
    family=FormatFamily.DOCUMENT,
    extensions=(".xlsx",),
    signatures=(((0, ZIP),),),
    zip_member="xl/workbook.xml",
)

# Output-only formats: they are produced, never detected as sources.
TXT = Format("txt", "TXT", FormatFamily.DOCUMENT, (".txt",), ())
HTML = Format("html", "HTML", FormatFamily.DOCUMENT, (".html", ".htm"), ())
CSV = Format("csv", "CSV", FormatFamily.DOCUMENT, (".csv",), ())

IMAGE_FORMATS: tuple[Format, ...] = (PNG, JPEG, WEBP, BMP, TIFF)
VIDEO_FORMATS: tuple[Format, ...] = (MP4, MKV, WEBM, MOV, AVI)
AUDIO_FORMATS: tuple[Format, ...] = (MP3, WAV, FLAC, AAC, M4A, OGG, OPUS)
DOCUMENT_FORMATS: tuple[Format, ...] = (PDF, DOCX, XLSX, TXT, HTML, CSV)
# Order is the display order in the UI; detection does not depend on it.
BUILTIN_FORMATS: tuple[Format, ...] = (
    IMAGE_FORMATS + VIDEO_FORMATS + AUDIO_FORMATS + DOCUMENT_FORMATS
)

# Enough bytes to check every signature above (Opus needs offset 28 + 8,
# the Matroska DocType is usually within the first 40 bytes).
HEADER_SIZE = 64
