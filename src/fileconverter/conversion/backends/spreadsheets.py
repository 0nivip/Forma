"""XLSX -> CSV with openpyxl (pure Python, MIT): one CSV file per worksheet.

CSV dialect: UTF-8 with BOM (so Excel recognises the encoding); delimiter semicolon
(default, as expected by e.g. German Excel), comma or tab, the same for every
worksheet; double quotes only where needed (QUOTE_MINIMAL, quotes doubled inside);
CRLF line endings (RFC 4180). Line breaks inside a cell are kept inside quotes.

Values: what Excel last calculated and saved. A formula cell without a saved result
(e.g. the file was produced by a program and never opened in Excel) is exported as
its formula text such as "=SUM(A1:A3)", and a warning says how many cells this was.
Numbers are written as stored (not as displayed, full precision), with a decimal
comma (default, matching the semicolon delimiter of German Excel) or a decimal dot.
Only cells that openpyxl reads as numbers are affected: text that merely looks like
a number ("1.5", "001.50"), dates, times and formula text are never changed.
Dates as ISO 8601, booleans as TRUE/FALSE. Formatting, charts, comments and
merged-cell layout are not exported.

Formula injection: spreadsheet programs run cell text such as "=HYPERLINK(...)" or
"+cmd|..." as a formula when they open a CSV; quoting does not prevent that. In the
default "protect" mode a single quote is put in front of such text cells (see
protect_cell). Numeric cells and text that is just a number ("-5") are never changed.
The "raw" mode writes every value unchanged.
"""

import csv
import datetime
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path

import openpyxl

from fileconverter.conversion.backends.documents import check_archive_size
from fileconverter.core.converter import JobContext, OutputPart
from fileconverter.core.errors import InvalidInputError, InvalidOptionError
from fileconverter.core.models import ChoiceOption, OptionValue, Route, TextOption
from fileconverter.core.output_paths import make_unique, safe_name_part

logger = logging.getLogger(__name__)

SHEET_OPTION = TextOption(
    "sheet", "Worksheet", placeholder="All worksheets (or a number / name)", max_length=100
)
DELIMITERS = {"semicolon": ";", "comma": ",", "tab": "\t"}
DELIMITER_OPTION = ChoiceOption(
    "delimiter",
    "Delimiter",
    (("semicolon", "Semicolon (;)"), ("comma", "Comma (,)"), ("tab", "Tab")),
    default="semicolon",
)
DECIMAL_OPTION = ChoiceOption(
    "decimal",
    "Decimal separator",
    (("comma", "Comma (,)"), ("dot", "Dot (.)")),
    default="comma",
    help=(
        "Used for numeric cells only (1.5 becomes 1,5 with Comma). Text cells, dates and "
        "formulas are written unchanged."
    ),
)
FORMULAS_OPTION = ChoiceOption(
    "formulas",
    "Formula-like text",
    (("protect", "Protect (prefix ')"), ("raw", "Keep unchanged (raw)")),
    default="protect",
    help=(
        "Protect: text cells starting with = + - @ are prefixed with ' so that spreadsheet "
        "programs show them as text instead of running them as formulas (the ' may be "
        "visible). Raw: values are written exactly as stored."
    ),
)
CANCEL_CHECK_ROWS = 1000
NOTE_CSV = (
    "Creates one CSV file per worksheet (UTF-8 with BOM). Formatting, charts and live "
    "formulas are not kept."
)

# Characters that make a spreadsheet treat a cell as a formula (incl. full-width forms).
_FORMULA_START = frozenset("=+-@＝＋－＠")
_LEADING_SPACE = " \t\r\n 　"
_PLAIN_NUMBER = re.compile(r"^[+-]?(\d+([.,]\d*)?|[.,]\d+)([eE][+-]?\d+)?$")


class SpreadsheetConverter:
    def routes(self) -> Iterator[Route]:
        options = (SHEET_OPTION, DELIMITER_OPTION, DECIMAL_OPTION, FORMULAS_OPTION)
        yield Route("xlsx", "csv", options=options, notes=(NOTE_CSV,), multi_output=True)

    def convert(
        self,
        source: Path,
        destination: Path,
        route: Route,
        options: Mapping[str, OptionValue],
        ctx: JobContext,
    ) -> list[OutputPart]:
        check_archive_size(source)
        values_book = _open_workbook(source, data_only=True)  # saved results
        try:
            formulas_book = _open_workbook(source, data_only=False)  # formula text
            try:
                return self._export(values_book, formulas_book, destination, options, ctx)
            finally:
                formulas_book.close()
        finally:
            values_book.close()

    def _export(self, values_book, formulas_book, folder, options, ctx) -> list[OutputPart]:
        indexes = _select_sheets(values_book.sheetnames, str(options.get("sheet", "")))
        names = [values_book.sheetnames[i] for i in indexes]
        if len(values_book.sheetnames) == 1:
            suffixes = [""]  # "book.csv" rather than "book_Sheet1.csv"
        else:
            suffixes = make_unique(
                [
                    safe_name_part(name, f"sheet{i + 1}")
                    for name, i in zip(names, indexes, strict=True)
                ]
            )

        delimiter = DELIMITERS[str(options.get("delimiter", DELIMITER_OPTION.default))]
        decimal_comma = options.get("decimal", DECIMAL_OPTION.default) == "comma"
        protect = options.get("formulas", FORMULAS_OPTION.default) == "protect"
        parts = []
        for done, (index, name, suffix) in enumerate(zip(indexes, names, suffixes, strict=True)):
            ctx.raise_if_cancelled()
            path = folder / f"{done:04d}.csv"  # temporary name; the runner names the result
            stats = _write_csv(
                values_book.worksheets[index],
                formulas_book.worksheets[index],
                path,
                delimiter,
                decimal_comma,
                protect,
                ctx,
            )
            if stats.rows == 0:
                ctx.warn(f"Worksheet '{name}' is empty; its CSV file is empty.")
            if stats.unsaved:
                ctx.warn(
                    f"Worksheet '{name}': {stats.unsaved} formula cell(s) had no saved result "
                    "(the file was not recalculated in Excel), so their formula text was "
                    "exported."
                )
            if stats.protected:
                ctx.warn(
                    f"Worksheet '{name}': {stats.protected} text cell(s) starting with "
                    "=, +, - or @ were prefixed with ' so they open as text, not formulas."
                )
            parts.append(OutputPart(path, suffix))
            ctx.report_progress((done + 1) / len(indexes))
        return parts


def _open_workbook(source: Path, data_only: bool):
    try:
        # read_only streams rows instead of loading the whole workbook into memory.
        return openpyxl.load_workbook(source, read_only=True, data_only=data_only)
    except Exception as exc:
        # openpyxl reports malformed files through many exception types.
        logger.info("openpyxl failed for '%s'", source, exc_info=True)
        raise InvalidInputError(
            f"'{source.name}' is damaged or not a supported Excel workbook."
        ) from exc


def _select_sheets(sheetnames: list[str], choice: str) -> list[int]:
    """'' = all worksheets; otherwise a 1-based number or a worksheet name."""
    if not sheetnames:
        raise InvalidInputError("The workbook contains no worksheets.")
    if not choice:
        return list(range(len(sheetnames)))
    if choice.isdigit() and 1 <= int(choice) <= len(sheetnames):
        return [int(choice) - 1]
    for i, name in enumerate(sheetnames):
        if name == choice:
            return [i]
    for i, name in enumerate(sheetnames):
        if name.casefold() == choice.casefold():
            return [i]
    available = ", ".join(f"{i + 1}: {name}" for i, name in enumerate(sheetnames[:10]))
    raise InvalidOptionError(f"No worksheet '{choice}'. Available: {available}.")


@dataclass
class _SheetStats:
    rows: int = 0
    unsaved: int = 0  # formula cells without a saved result (formula text exported)
    protected: int = 0  # text cells prefixed with ' (protect mode)


def _write_csv(
    values_sheet,
    formulas_sheet,
    path: Path,
    delimiter: str,
    decimal_comma: bool,
    protect: bool,
    ctx: JobContext,
) -> _SheetStats:
    """Write one worksheet as CSV."""
    # The declared size keeps normal sheets rectangular; resetting it afterwards makes
    # openpyxl read every row even if that declaration is wrong (it can be too small).
    width = values_sheet.max_column or 0
    values_sheet.reset_dimensions()
    formulas_sheet.reset_dimensions()

    stats = _SheetStats()
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(
            file,
            delimiter=delimiter,
            quotechar='"',
            doublequote=True,
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\r\n",
        )
        pairs = zip_longest(
            values_sheet.iter_rows(values_only=True), formulas_sheet.iter_rows(), fillvalue=()
        )
        for values, formula_cells in pairs:
            row = []
            for value, cell in zip_longest(values, formula_cells):
                if value is None and cell is not None and cell.data_type == "f":
                    stats.unsaved += 1
                    value = getattr(cell.value, "text", cell.value)  # ArrayFormula -> text
                text = _format(value, decimal_comma)
                if protect and isinstance(value, str):  # numbers, dates etc. stay as they are
                    safe = protect_cell(text)
                    stats.protected += safe != text
                    text = safe
                row.append(text)
            row += [""] * (width - len(row))
            writer.writerow(row)
            stats.rows += 1
            if stats.rows % CANCEL_CHECK_ROWS == 0:
                ctx.raise_if_cancelled()
    return stats


def protect_cell(text: str) -> str:
    """Prefix ' to text that a spreadsheet program would run as a formula.

    Affected: text whose first non-blank character is = + - @ (or a full-width form),
    and text starting with a tab or carriage return. Not affected: text that is just
    a number such as "-5" or "+1,5", which cannot be a formula.
    """
    if not text:
        return text
    if text[0] in "\t\r":
        return "'" + text
    stripped = text.lstrip(_LEADING_SPACE)
    if stripped and stripped[0] in _FORMULA_START and not _PLAIN_NUMBER.match(stripped):
        return "'" + text
    return text


def _format(value: object, decimal_comma: bool = False) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime.datetime):
        if value.time() == datetime.time(0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, float):
        # Shortest exact form, e.g. 0.1 rather than 0.10000000000000001. A float's repr
        # holds only digits, a sign, one "." and an exponent ("1.5e+20"), so swapping
        # the "." cannot touch anything else. Integers have no decimal part.
        text = repr(value)
        return text.replace(".", ",") if decimal_comma else text
    return str(value)  # text (incl. number-like text), ints and anything else unchanged
