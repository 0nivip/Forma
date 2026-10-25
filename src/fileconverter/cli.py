"""Minimal command-line interface for exercising the conversion core.

This is a debugging aid, not the product UI. Examples:

    python -m fileconverter formats
    python -m fileconverter convert a.png b.tif --to jpeg --quality 80 --out-dir out
    python -m fileconverter convert report.pdf --to png --pages 1-3 --dpi 300
    python -m fileconverter convert book.xlsx --to csv --sheet 2 --delimiter comma
    python -m fileconverter convert photo.jpg --to png --preserve-metadata
    python -m fileconverter convert 1.jpg 2.png 3.jpg --to pdf --combine
"""

import argparse
import logging
import sys
from pathlib import Path

from fileconverter.app import build_registry
from fileconverter.conversion.backends.images import PRESET_LABELS, PRESET_QUALITY, QUALITY_OPTIONS
from fileconverter.conversion.backends.spreadsheets import (
    DECIMAL_OPTION,
    DELIMITER_OPTION,
    FORMULAS_OPTION,
)
from fileconverter.conversion.runner import run_job
from fileconverter.core.models import ConversionRequest, JobStatus
from fileconverter.core.registry import FormatRegistry
from fileconverter.infrastructure.ffmpeg import find_ffmpeg


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    registry = build_registry(find_ffmpeg())
    if args.command == "formats":
        return _list_formats(registry)
    return _convert(registry, args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fileconverter", description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logging")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("formats", help="list supported conversions")

    convert = commands.add_parser("convert", help="convert one or more files")
    convert.add_argument("files", nargs="+", type=Path, help="source files")
    convert.add_argument("--to", required=True, dest="target", help="target format id, e.g. jpeg")
    convert.add_argument(
        "--out-dir", type=Path, help="output folder (default: next to each source file)"
    )
    quality_help = ", ".join(
        f"{fmt.upper()} {option.minimum}-{option.maximum} (default {option.default})"
        for fmt, option in QUALITY_OPTIONS.items()
    )
    convert.add_argument("--quality", type=int, help=f"quality for lossy formats: {quality_help}")
    preset_help = "; ".join(
        f"{codec.upper()} " + ", ".join(f"{name} {value}" for name, value in values.items())
        for codec, values in PRESET_QUALITY.items()
    )
    convert.add_argument(
        "--preset",
        choices=[value for value, _ in PRESET_LABELS],
        help=(
            f"quality preset for JPEG/WebP/AVIF ({preset_help}); 'custom' (default) uses "
            "--quality. Combining another preset with a different --quality is an error"
        ),
    )
    convert.add_argument("--pages", help="PDF to images: page range, e.g. 1-3,5 (default: all)")
    convert.add_argument("--dpi", type=int, help="PDF to images: resolution (default: 150)")
    convert.add_argument(
        "--sheet", help="XLSX to CSV: worksheet number or name (default: all worksheets)"
    )
    convert.add_argument(
        "--delimiter",
        choices=[value for value, _ in DELIMITER_OPTION.choices],
        help=f"XLSX to CSV: field delimiter (default: {DELIMITER_OPTION.default})",
    )
    convert.add_argument(
        "--decimal",
        choices=[value for value, _ in DECIMAL_OPTION.choices],
        help=(
            "XLSX to CSV: decimal separator for numeric cells; text, dates and formulas are "
            f"never changed (default: {DECIMAL_OPTION.default})"
        ),
    )
    convert.add_argument(
        "--formulas",
        choices=[value for value, _ in FORMULAS_OPTION.choices],
        help=(
            "XLSX to CSV: 'protect' prefixes text starting with = + - @ with ' so spreadsheet "
            f"programs do not run it; 'raw' keeps values unchanged "
            f"(default: {FORMULAS_OPTION.default})"
        ),
    )
    convert.add_argument(
        "--combine",
        action="store_true",
        help="combine all given images, in order, into one PDF (with --to pdf)",
    )
    convert.add_argument(
        "--preserve-metadata",
        action="store_true",
        help="keep metadata such as EXIF/GPS or media tags (may include personal information)",
    )
    return parser


def _list_formats(registry: FormatRegistry) -> int:
    for fmt in registry.formats():
        if not fmt.can_be_source:
            targets = "(output only)"
        else:
            targets = ", ".join(target.id for target in registry.targets_for(fmt.id)) or "-"
        print(f"{fmt.id:6} ({', '.join(fmt.extensions)}) -> {targets}")
        if reason := registry.unavailable_reason(fmt.id):
            print(f"       note: {reason}")
    return 0


def _options(args: argparse.Namespace) -> dict[str, object]:
    """Only options given on the command line; the route supplies defaults for the rest."""
    given = {
        "preset": args.preset,
        "quality": args.quality,
        "pages": args.pages,
        "dpi": args.dpi,
        "sheet": args.sheet,
        "delimiter": args.delimiter,
        "decimal": args.decimal,
        "formulas": args.formulas,
    }
    return {name: value for name, value in given.items() if value is not None}


def _convert(registry: FormatRegistry, args: argparse.Namespace) -> int:
    options = _options(args)
    if args.combine:
        groups = [(args.files[0], tuple(args.files[1:]))]
    else:
        groups = [(source, ()) for source in args.files]

    failures = 0
    for source, extra in groups:
        output_dir = args.out_dir if args.out_dir is not None else source.parent
        request = ConversionRequest(
            source, args.target, output_dir, options, extra, args.preserve_metadata
        )
        result = run_job(request, registry)

        if result.status is JobStatus.SUCCEEDED:
            outputs = result.output_paths
            print(f"OK      {source} -> {outputs[0]}")
            for path in outputs[1:]:
                print(f"        {' ' * len(str(source))}    {path}")
        elif result.status is JobStatus.CANCELLED:
            print(f"CANCEL  {source}")
        else:
            failures += 1
            print(f"FAILED  {source}: {result.error}", file=sys.stderr)
        for warning in result.warnings:
            print(f"        warning: {warning}")
    return 1 if failures else 0
