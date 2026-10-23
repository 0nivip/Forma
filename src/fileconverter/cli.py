"""Minimal command-line interface for exercising the conversion core.

This is a debugging aid, not the product UI. Examples:

    python -m fileconverter formats
    python -m fileconverter convert a.png b.tif --to jpeg --quality 80 --out-dir out
"""

import argparse
import logging
import sys
from pathlib import Path

from fileconverter.app import build_registry
from fileconverter.conversion.runner import run_job
from fileconverter.core.models import ConversionRequest, JobStatus
from fileconverter.core.registry import FormatRegistry


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    registry = build_registry()
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
    convert.add_argument("--quality", type=int, help="quality for lossy formats (JPEG, WebP)")
    return parser


def _list_formats(registry: FormatRegistry) -> int:
    for fmt in registry.formats():
        targets = ", ".join(target.id for target in registry.targets_for(fmt.id)) or "-"
        print(f"{fmt.id:6} ({', '.join(fmt.extensions)}) -> {targets}")
    return 0


def _convert(registry: FormatRegistry, args: argparse.Namespace) -> int:
    options = {} if args.quality is None else {"quality": args.quality}
    failures = 0
    for source in args.files:
        output_dir = args.out_dir if args.out_dir is not None else source.parent
        request = ConversionRequest(source, args.target, output_dir, options)
        result = run_job(request, registry)

        if result.status is JobStatus.SUCCEEDED:
            print(f"OK      {source} -> {result.output_path}")
        elif result.status is JobStatus.CANCELLED:
            print(f"CANCEL  {source}")
        else:
            failures += 1
            print(f"FAILED  {source}: {result.error}", file=sys.stderr)
        for warning in result.warnings:
            print(f"        warning: {warning}")
    return 1 if failures else 0
