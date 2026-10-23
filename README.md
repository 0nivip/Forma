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
- Container metadata (e.g. title/artist tags) is passed through as FFmpeg does by default.
- No quality/bitrate settings in the UI yet.

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
```
