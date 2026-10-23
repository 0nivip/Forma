# Forma

Local-first desktop file converter. Files never leave the user's computer: there is
no upload, no account and no telemetry.

(The Python package and the settings/log folders are still named `fileconverter` /
`FileConverter`.)

## What it converts

| Kind | Formats | Engine |
|---|---|---|
| Images | PNG, JPEG, WebP, BMP, TIFF | Pillow (bundled Python dependency) |
| Video | MP4, MKV, WebM, MOV, AVI | FFmpeg (installed separately) |
| Audio | MP3, WAV, FLAC, AAC, M4A, OGG, Opus | FFmpeg (installed separately) |
| Documents | DOCX → TXT, DOCX → HTML, XLSX → CSV | mammoth, openpyxl (bundled) |
| PDF | PDF → PNG / JPEG, images → PDF | pypdfium2 / PDFium (bundled) |

No office suite (LibreOffice, Microsoft Office) is needed or used. DOCX → PDF,
PPTX → PDF and DOC ↔ DOCX are **not** supported.

Video files can be converted to other video containers or to any audio format
(audio extraction). Audio files can be converted to other audio formats.

Default codecs per target (the first one available in the installed FFmpeg is used):

| Target | Video codec | Audio codec |
|---|---|---|
| MP4, MOV | H.264 (libx264), else MPEG-4 Part 2 | AAC |
| MKV | H.264, else VP9, else MPEG-4 Part 2 | AAC, else Opus / Vorbis / FLAC |
| WebM | VP9, else VP8 | Opus, else Vorbis |
| AVI | MPEG-4 Part 2 | MP3, else AC-3 |
| MP3 / WAV / FLAC | – | MP3 (LAME) / PCM 16-bit / FLAC |
| AAC / M4A | – | AAC (raw ADTS / in an M4A container) |
| OGG / Opus | – | Vorbis (else Opus) / Opus |

A target is offered only if the installed FFmpeg build has its container and a
suitable encoder; otherwise the application explains what is missing.

### Current audio/video limitations

- Everything is re-encoded (no lossless stream copy yet); lossy targets lose some quality.
- Only the first video and the first audio track are kept; subtitles, extra tracks
  and attachments are dropped (a warning is shown). Cover art is not copied to audio outputs.
- Video is converted to 8-bit 4:2:0 (yuv420p) for compatibility; HDR/10-bit is not preserved.
  Odd frame sizes are reduced by one pixel to even values.
- Metadata (tags, chapters) follows the *Preserve metadata* setting (see below).
- No quality/bitrate settings in the UI yet.

## Metadata and privacy

One setting, **Preserve metadata (may include personal information)**, is off by
default (GUI checkbox; CLI `--preserve-metadata`; stored as `preserve_metadata` in
`settings.json`, older settings files without it count as off). It applies to the
next conversion without a restart. Removal is best effort: only the items listed as
verified below are covered by tests, and format-specific information may remain.

| Backend | Off (default) | On |
|---|---|---|
| Images (Pillow) | EXIF (incl. GPS), XMP, ICC profile and JPEG comments are not written. Verified for JPEG/PNG/WebP sources → JPEG/PNG/WebP/TIFF/BMP. | EXIF (incl. GPS) and ICC are kept for JPEG/PNG/WebP/TIFF targets; XMP only for JPEG/WebP targets (Pillow limitation). JPEG comments, PNG text chunks and EXIF of TIFF sources are not kept. A TIFF with EXIF sub-blocks (e.g. GPS) is saved uncompressed, because the compressing writer cannot store them (a warning says so). |
| Audio/video (FFmpeg) | `-map_metadata -1 -map_metadata:s -1 -map_chapters -1`: global tags (title, artist, location, …), stream tags (titles, languages) and chapters are dropped, including for audio extraction. FFmpeg's own technical tags (encoder name, handler names) remain. | Tags and chapters are copied as FFmpeg does by default. |
| Images → PDF | JPEGs are embedded without EXIF/XMP/ICC/comment segments and without trailing data such as preview images; other images carry no metadata. No author/title is set on the PDF. | JPEGs are embedded byte for byte (their metadata stays inside the image stream, where PDF readers do not show it); metadata of other images cannot be stored in the PDF (a warning says so). |
| Documents, PDF → images | Not affected: these routes do not copy source metadata. | Same. |

In every mode the EXIF orientation is applied to the pixels (or, for JPEGs in a PDF,
to the page placement) exactly once; an orientation tag is never written back.
Without colour profiles, wide-gamut photos may look slightly less saturated.

## Documents and PDF

Some conversions create **several files**; one job owns all of them. The files are
first written to a hidden temporary folder and only moved into place after all of
them were produced; if moving fails half-way, the files already moved are removed
again, and a cancelled or failed job leaves no files behind. Existing files are never
overwritten (`name (1).ext` is used instead).

**DOCX → TXT** (UTF-8, no BOM, LF line endings, final newline):
- Each paragraph or heading is one line; blocks are separated by one blank line;
  empty paragraphs are skipped.
- List items are consecutive lines with `- ` or `1. ` prefixes, nested lists indented
  by two spaces. Table rows are one line each, cells separated by a TAB.
- Images are omitted; footnotes appear as a numbered list at the end.
- A document without any text is reported as "nothing to convert" (no empty file).

**DOCX → HTML** (UTF-8): semantic HTML (headings, paragraphs, lists, tables, links,
bold/italic, images embedded as `data:` URIs). The output is re-written through an
allow-list: only `http`, `https`, `mailto` and in-page links are kept (e.g.
`javascript:` links are removed), and the page carries a Content-Security-Policy that
blocks scripts and any network access. Word layout (fonts, sizes, page setup) is not
reproduced.

DOCX limitations: text boxes, shapes, headers/footers, comments, complex fields and
EMF/WMF images may be missing or simplified; converter warnings are summarised in the
job status and listed in the log. Password-protected (encrypted) and legacy `.doc`
files are not supported.

DOCX time limit and cancellation: mammoth cannot be interrupted, so each DOCX is
parsed in a short-lived child process (Python `multiprocessing`, "spawn"). Cancelling
the job or exceeding the time limit (120 seconds) kills that process; it never writes
files itself, so nothing partial can be left behind, and the job is reported as
cancelled or failed ("took longer than … seconds"). Starting the process costs about
0.2 s per document. If Forma itself is killed, the child (a daemon process) ends with
it on a normal exit, but not necessarily on a hard kill.

**XLSX → CSV** — one CSV per worksheet (`book_<sheet>.csv`, or `book.csv` if the
workbook has a single worksheet). Options: one worksheet by number or name; delimiter;
handling of formula-like text. GUI and CLI use the same options and defaults.
- Encoding: UTF-8 **with BOM** (so Excel detects the encoding). Line endings CRLF
  (RFC 4180); line breaks inside a cell are kept inside double quotes.
- Delimiter: **semicolon (default**, as expected by e.g. German Excel), comma, or tab —
  the same for every worksheet of an export. Fields are quoted only when they contain
  the delimiter, a quote or a line break; quotes inside are doubled.
- Values: the results Excel last saved. Formula cells without a saved result (files
  never recalculated in Excel) are exported as their formula text, with a warning.
  Numbers as stored (not as displayed; always `.` as decimal point, which some
  regional Excel versions may read as a date or text), dates as ISO 8601, booleans as
  TRUE/FALSE.
- Sheet names are made safe for Windows file names (`<>:"/\|?*`, reserved names
  such as `CON`) and de-duplicated (`_2`, `_3`, ...).
- Not exported: formatting, charts, comments, merged-cell layout, or several
  worksheets in one file.

**Formula injection.** Spreadsheet programs run cell text such as `=HYPERLINK(...)`
or `+cmd|...` as a formula when they open a CSV, and quoting does not prevent it. In
the default **protect** mode, a `'` is put in front of a text cell when its first
non-blank character is `=`, `+`, `-` or `@` (also the full-width forms), or when it
starts with a tab or carriage return; the job reports how many cells were changed.
Numeric cells and text that is just a number (`-5`, `+1,5`) are never changed. This
also turns exported formula text (see above) into plain text. Trade-off: protected
cells differ from the stored value, and some programs show the `'`. The **raw** mode
(`--formulas raw`) writes every value unchanged — only use it for files you trust or
that are not opened in a spreadsheet program.

**PDF → PNG / JPEG** — one image per page (`doc_page_001.png`, ...). Options: page
range (e.g. `1-3, 5`, default all) and resolution (36–600 DPI, default 150). At most
1000 pages per job; pages above ~60 megapixels are rendered at a lower resolution
(with a warning). The images are raster renderings, not editable text or vectors.
Password-protected PDFs are reported as such and not opened.

**Images → PDF** — one page per image at the image's resolution (96 DPI if unknown).
Several images can be combined into one PDF in the given order (GUI: select them and
press *Combine into One PDF*; CLI: `--combine`). JPEGs are embedded without
re-compression; other images are stored losslessly. EXIF orientation is applied;
transparency is flattened onto white; metadata follows the *Preserve metadata*
setting (see above). Colour profiles (ICC) are not applied. Multi-frame images
contribute their first frame.

PDFium is not thread-safe, so PDF jobs run one at a time even when several jobs are
queued.

## FFmpeg

Audio and video need **both `ffmpeg` and `ffprobe`**. Forma does **not** bundle FFmpeg.

The executables are searched in this order (the first working pair wins):

1. The folder in the `FORMA_FFMPEG_DIR` environment variable.
2. An `ffmpeg\` or `ffmpeg\bin\` folder next to a packaged application (reserved
   for a possible future bundled build; unused in development).
3. `PATH`. On Windows only real `.exe` files are accepted (no `.bat`/`.cmd` wrappers).

FFmpeg is looked up once at startup; restart the application after installing it.

Installation on Windows (one option; ffmpeg.org lists the available builds at
<https://ffmpeg.org/download.html>):

```powershell
winget install --id Gyan.FFmpeg.Essentials --exact
```

### Licensing note

This is not legal advice. Summary of the official sources
(<https://ffmpeg.org/legal.html>, <https://ffmpeg.org/download.html>):

- FFmpeg is LGPL 2.1+ by default; if GPL-only parts are enabled (e.g. libx264,
  libx265), the GPL applies to that FFmpeg build. `--enable-nonfree` builds are not
  redistributable.
- ffmpeg.org itself ships only source code. Windows binaries come from third parties;
  for example, the gyan.dev builds are licensed GPLv3, and BtbN offers both GPL and
  LGPL variants (the LGPL ones lack libx264/libx265).
- Forma runs FFmpeg as a separate program and does not link or ship it, so today it
  does not redistribute any FFmpeg code. **Shipping FFmpeg with Forma is an open
  decision**: it would mean complying with that build's license (for an LGPL build,
  the checklist on ffmpeg.org/legal.html; for a GPL build, the GPL), and codec patent
  questions (H.264, AAC, ...) need separate legal review for commercial distribution.

### Licenses of bundled Python dependencies

Technical summary, not legal advice. When distributing Forma, ship the license texts
of these packages (they are included in each package's `*.dist-info/licenses`).

| Package | License | Notes |
|---|---|---|
| Pillow | MIT-CMU (HPND) | |
| PySide6-Essentials | LGPL-3.0 (also GPL/commercial) | dynamic linking, replaceable Qt libraries |
| mammoth (+ cobble) | BSD-2-Clause | |
| openpyxl (+ et-xmlfile) | MIT | |
| pypdfium2 | Apache-2.0 or BSD-3-Clause | bundles the PDFium library (BSD-3-Clause) with FreeType (FreeType License: credit in the documentation required), ICU (Unicode License v3), libjpeg-turbo (IJG/BSD), libpng, zlib, lcms2, OpenJPEG, Abseil and others — all permissive, each requiring its notice |

## Run the application

```powershell
.\.venv\Scripts\python.exe -m fileconverter.gui
```

User data (Windows):

| What | Where |
|---|---|
| Settings | `%APPDATA%\FileConverter\settings.json` |
| Diagnostic log | `%LOCALAPPDATA%\FileConverter\logs\fileconverter.log` (1 MB, 3 backups) |

```powershell
Get-Content "$env:LOCALAPPDATA\FileConverter\logs\fileconverter.log" -Tail 50
```

The log records which FFmpeg was found and the error output of failed FFmpeg runs.

## Development setup (Windows, PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Run tests and lint

GUI tests run with Qt's offscreen platform, so no windows appear. Tests that need a
real FFmpeg (`tests/test_media_integration.py`) are skipped when it is not installed;
all process handling is also covered by tests that do not need FFmpeg.

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest -rs     # also list skipped tests and why
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

## Debugging CLI

```powershell
.\.venv\Scripts\python.exe -m fileconverter formats
.\.venv\Scripts\python.exe -m fileconverter convert photo.png --to jpeg --quality 85 --out-dir out
.\.venv\Scripts\python.exe -m fileconverter convert "my song.wav" --to mp3
.\.venv\Scripts\python.exe -m fileconverter convert report.docx --to txt
.\.venv\Scripts\python.exe -m fileconverter convert report.docx --to html --out-dir out
.\.venv\Scripts\python.exe -m fileconverter convert book.xlsx --to csv                # every sheet
.\.venv\Scripts\python.exe -m fileconverter convert book.xlsx --to csv --sheet "Q1"   # one sheet
.\.venv\Scripts\python.exe -m fileconverter convert book.xlsx --to csv --delimiter comma --formulas raw
.\.venv\Scripts\python.exe -m fileconverter convert photo.jpg --to png --preserve-metadata
.\.venv\Scripts\python.exe -m fileconverter convert doc.pdf --to png --pages 1-3 --dpi 300
.\.venv\Scripts\python.exe -m fileconverter convert doc.pdf --to jpeg --quality 85
.\.venv\Scripts\python.exe -m fileconverter convert a.jpg b.png c.jpg --to pdf --combine
.\.venv\Scripts\python.exe -m fileconverter convert a.jpg b.png --to pdf             # one PDF each
```
