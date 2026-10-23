# FileConverter

Local-first desktop file converter. Files never leave the user's computer.

Current status: conversion core + image backend (Pillow) + minimal desktop GUI + debugging CLI.

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

## Development setup (Windows, PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Run tests and lint

GUI tests run with Qt's offscreen platform, so no windows appear.

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

## Debugging CLI

```powershell
.\.venv\Scripts\python.exe -m fileconverter formats
.\.venv\Scripts\python.exe -m fileconverter convert photo.png --to jpeg --quality 85 --out-dir out
```
