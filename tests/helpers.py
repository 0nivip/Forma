"""Shared test helpers. Test images are generated on the fly, so no binary fixtures."""

import os
import struct
import time
import zipfile
import zlib
from pathlib import Path
from xml.sax.saxutils import escape

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


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_DOCX_STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W_NS}">
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>
</w:styles>"""

_DOCX_NUMBERING = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="{W_NS}">
  <w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
  <w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>"""


def docx_paragraph(text: str = "", style: str | None = None, num_id: int | None = None) -> str:
    """One <w:p>; `num_id` 1 = bullet list, 2 = numbered list."""
    props = ""
    if style:
        props += f'<w:pStyle w:val="{style}"/>'
    if num_id:
        props += f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr>'
    run = f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>' if text else ""
    return f"<w:p><w:pPr>{props}</w:pPr>{run}</w:p>"


def docx_table(rows: list[list[str]]) -> str:
    cells = "".join(
        "<w:tr>" + "".join(f"<w:tc>{docx_paragraph(c)}</w:tc>" for c in row) + "</w:tr>"
        for row in rows
    )
    return f"<w:tbl>{cells}</w:tbl>"


def docx_link(text: str, rel_id: str) -> str:
    run = f"<w:r><w:t>{escape(text)}</w:t></w:r>"
    return f'<w:p><w:hyperlink r:id="{rel_id}">{run}</w:hyperlink></w:p>'


def make_docx(path: Path, body: str, links: dict[str, str] | None = None) -> Path:
    """Write a minimal but valid DOCX whose <w:body> contains `body`.

    `links` maps relationship ids to external URLs for docx_link().
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rels = "".join(
        f'<Relationship Id="{rid}" Type="{_REL_TYPE}/hyperlink" Target="{escape(url)}" '
        'TargetMode="External"/>'
        for rid, url in (links or {}).items()
    )
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        "_rels/.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="{_REL_TYPE}/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        "word/_rels/document.xml.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdStyles" Type="{_REL_TYPE}/styles" Target="styles.xml"/>
  <Relationship Id="rIdNum" Type="{_REL_TYPE}/numbering" Target="numbering.xml"/>
  {rels}
</Relationships>""",
        "word/styles.xml": _DOCX_STYLES,
        "word/numbering.xml": _DOCX_NUMBERING,
        "word/document.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}"><w:body>{body}</w:body></w:document>""",
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def write_pdf(
    path: Path, pages: list[tuple[float, float, tuple[int, int, int]]], encrypted: bool = False
) -> Path:
    """Write a minimal PDF: one page per (width_pt, height_pt, fill RGB), each filled solid.

    encrypted=True adds a standard-security /Encrypt dictionary whose user password is
    not empty, so readers ask for a password.
    """
    objects: list[bytes] = [b"", b""]  # 1 = catalog, 2 = page tree (filled in below)
    kids = []
    for width, height, (r, g, b) in pages:
        content = f"{r / 255:.3f} {g / 255:.3f} {b / 255:.3f} rg 0 0 {width} {height} re f".encode()
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
        content_ref = len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %s %s] /Contents %d 0 R >>"
            % (str(width).encode(), str(height).encode(), content_ref)
        )
        kids.append(b"%d 0 R" % len(objects))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), len(kids))
    trailer_extra = b""
    if encrypted:
        objects.append(
            b"<< /Filter /Standard /V 1 /R 2 /P -4 /O <%s> /U <%s> >>" % (b"11" * 32, b"22" * 32)
        )
        trailer_extra = b" /Encrypt %d 0 R /ID [<%s> <%s>]" % (len(objects), b"33" * 16, b"33" * 16)

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R%s >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        trailer_extra,
        xref,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def hanging_docx_child(source: str, connection) -> None:
    """Stand-in for the mammoth child process that never answers (like a stuck parser)."""
    time.sleep(120)


def crashing_docx_child(source: str, connection) -> None:
    """Stand-in for a mammoth child process that dies without answering."""
    os._exit(3)


def temp_files(directory: Path) -> list[Path]:
    """Leftover runner temp files in `directory` (should always be empty after a job)."""
    return list(directory.glob(".fileconverter-*"))
