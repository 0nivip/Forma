"""DOCX -> HTML and DOCX -> TXT with mammoth (pure Python, BSD-2-Clause).

Both outputs come from mammoth's semantic HTML, so headings, lists and tables are
understood the same way in both.

HTML safety: mammoth copies hyperlink targets from the document unchanged (including
"javascript:" links). Its output is therefore re-written through an allow-list
(tags, attributes, URL schemes) and wrapped in a page whose Content-Security-Policy
forbids scripts and any network access.

TXT policy (deterministic):
  * Each paragraph or heading is one line; blocks are separated by one blank line.
    Empty paragraphs are skipped (mammoth's default).
  * List items are consecutive lines prefixed with "- " or "1. ", "2. ", ...;
    nested lists are indented by two spaces per level.
  * Each table row is one line with cells separated by a TAB; line breaks inside a
    cell become spaces.
  * Manual line breaks become new lines. Images are omitted.
  * UTF-8 without BOM, LF line endings, a final newline.
"""

import contextlib
import html
import logging
import multiprocessing
import re
import time
import traceback
import zipfile
from collections.abc import Iterator, Mapping
from html.parser import HTMLParser
from pathlib import Path

import mammoth

from fileconverter.core.converter import JobContext
from fileconverter.core.errors import (
    ConversionError,
    ConversionTimeout,
    InvalidInputError,
    NoContentError,
)
from fileconverter.core.models import OptionValue, Route

logger = logging.getLogger(__name__)

# Declared uncompressed size limit for a DOCX/XLSX archive (zip bomb protection).
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024

NOTE_HTML = "Semantic HTML: headings, lists, tables and links; Word layout is not reproduced."
NOTE_TXT = "Plain text: formatting and images are dropped."


class DocumentConverter:
    def routes(self) -> Iterator[Route]:
        yield Route("docx", "html", notes=(NOTE_HTML,))
        yield Route("docx", "txt", notes=(NOTE_TXT,))

    def convert(
        self,
        source: Path,
        destination: Path,
        route: Route,
        options: Mapping[str, OptionValue],
        ctx: JobContext,
    ) -> None:
        check_archive_size(source)
        body_html = _docx_to_html(source, ctx)
        ctx.raise_if_cancelled()

        text = html_to_text(body_html)
        safe_body, dropped_images = sanitize_html(body_html)
        if not text.strip() and "<img" not in safe_body:
            raise NoContentError(f"'{source.name}' contains no text or images to export.")

        if route.target == "txt":
            content = text
        else:
            content = HTML_PAGE.format(title=html.escape(source.stem), body=safe_body)
            if dropped_images:
                ctx.warn(
                    f"{dropped_images} image(s) in formats that browsers cannot show "
                    "(e.g. EMF/WMF) were left out."
                )
        destination.write_text(content, encoding="utf-8", newline="\n")


def check_archive_size(path: Path, limit: int = MAX_UNCOMPRESSED_BYTES) -> None:
    """Refuse archives that would unpack to an unreasonable size (zip bombs)."""
    try:
        with zipfile.ZipFile(path) as archive:
            total = sum(info.file_size for info in archive.infolist())
    except (zipfile.BadZipFile, OSError) as exc:
        raise InvalidInputError(f"'{path.name}' is damaged (not a valid archive).") from exc
    if total > limit:
        raise InvalidInputError(f"'{path.name}' is too large to process safely.")


def _docx_to_html(source: Path, ctx: JobContext) -> str:
    html_value, messages = _run_mammoth_isolated(source, ctx)
    if messages:
        for kind, text in messages:
            logger.info("mammoth %s for '%s': %s", kind, source, text)
        ctx.warn(
            f"{len(messages)} part(s) of the document could not be converted exactly, "
            f"e.g.: {messages[0][1][:120]} (details in the log)"
        )
    return html_value


# --- running mammoth in a child process -----------------------------------------
#
# mammoth cannot be interrupted, and its author recommends a timeout for untrusted
# documents. A thread cannot be stopped, but a process can: mammoth therefore runs in
# a short-lived child process that only reads the DOCX and sends back the HTML. The
# child never writes files, so killing it on cancel/timeout cannot leave partial
# output; the caller writes the result into the job's temp file as before.
# Cost: starting the child takes a fraction of a second per document.

DOCX_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 0.1


def mammoth_child(source: str, connection) -> None:
    """Runs in the child process: convert, then send ("ok", html, messages) or an error."""
    try:
        with open(source, "rb") as file:
            # external_file_access=False: never read files a document merely links to.
            result = mammoth.convert_to_html(file, external_file_access=False)
        messages = [(message.type, message.message) for message in result.messages]
        connection.send(("ok", result.value, messages))
    except Exception as exc:
        # mammoth reports malformed documents through many exception types.
        connection.send(("error", f"{type(exc).__name__}: {exc}", traceback.format_exc()))
    finally:
        connection.close()


_CHILD_TARGET = mammoth_child  # replaced in tests to simulate hanging or crashing children


def _run_mammoth_isolated(source: Path, ctx: JobContext) -> tuple[str, list[tuple[str, str]]]:
    spawn = multiprocessing.get_context("spawn")  # the only start method on Windows
    receiver, sender = spawn.Pipe(duplex=False)
    # daemon=True: the child is terminated if Forma exits while it still runs.
    child = spawn.Process(
        target=_CHILD_TARGET, args=(str(source), sender), daemon=True, name="forma-docx"
    )
    try:
        child.start()
    except OSError as exc:
        raise ConversionError(f"Cannot start the document converter: {exc}") from exc
    finally:
        sender.close()  # only the child writes; this way a dying child shows up as EOF

    deadline = time.monotonic() + DOCX_TIMEOUT_S
    message = None
    try:
        while True:
            if receiver.poll(POLL_INTERVAL_S):
                with contextlib.suppress(EOFError):  # EOF: the child died without answering
                    message = receiver.recv()
                break
            ctx.raise_if_cancelled()
            if time.monotonic() > deadline:
                raise ConversionTimeout(
                    f"Converting '{source.name}' took longer than {DOCX_TIMEOUT_S:.0f} seconds "
                    "and was stopped."
                )
    finally:
        receiver.close()
        if child.is_alive():
            child.kill()
        child.join(timeout=10)

    if message is None:
        raise InvalidInputError(
            f"The document converter stopped unexpectedly on '{source.name}' "
            f"(exit code {child.exitcode})."
        )
    status, payload, details = message
    if status != "ok":
        logger.info("mammoth failed for '%s': %s\n%s", source, payload, details)
        raise InvalidInputError(f"'{source.name}' is damaged or not a supported Word document.")
    return payload, details


# --- HTML sanitizing ----------------------------------------------------------

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font-family: sans-serif; max-width: 50em; margin: 2em auto; padding: 0 1em; line-height: 1.5; }}
table {{ border-collapse: collapse; }}
td, th {{ border: 1px solid #999; padding: 0.25em 0.5em; vertical-align: top; }}
img {{ max-width: 100%; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""  # noqa: E501

_ALLOWED_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "table", "thead", "tbody",
    "tr", "td", "th", "a", "strong", "em", "b", "i", "u", "s", "sub", "sup", "br", "img",
    "blockquote", "pre", "code", "hr", "input",
}  # fmt: skip
_VOID_TAGS = {"br", "img", "hr", "input"}
_DROP_CONTENT_TAGS = {"script", "style", "iframe", "object", "embed", "noscript", "template"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_:.-]{1,100}$")
_SAFE_IMAGE = re.compile(r"^data:image/(png|jpeg|gif|bmp|webp);base64,[A-Za-z0-9+/=\s]+$")
_SAFE_SCHEMES = ("http://", "https://", "mailto:")


def sanitize_html(fragment: str) -> tuple[str, int]:
    """Re-serialize mammoth's HTML keeping only safe tags/attributes.

    Returns the safe HTML and the number of images that had to be dropped.
    """
    writer = _SafeHtmlWriter()
    writer.feed(fragment)
    writer.close()
    return "".join(writer.out), writer.dropped_images


class _SafeHtmlWriter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.dropped_images = 0
        self._skip_depth = 0  # inside <script> or similar: drop everything

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth or tag not in _ALLOWED_TAGS:
            return
        safe = self._safe_attributes(tag, dict(attrs))
        if safe is None:
            return
        rendered = "".join(f' {name}="{html.escape(value)}"' for name, value in safe)
        self.out.append(f"<{tag}{rendered}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif not self._skip_depth and tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.out.append(html.escape(data, quote=False))

    def _safe_attributes(
        self, tag: str, attrs: dict[str, str | None]
    ) -> list[tuple[str, str]] | None:
        """Allowed attributes for `tag`, or None to drop the element itself."""
        safe = []
        element_id = attrs.get("id")
        if element_id and _SAFE_ID.match(element_id):
            safe.append(("id", element_id))
        if tag == "a":
            href = (attrs.get("href") or "").strip()
            if href.startswith("#") or href.lower().startswith(_SAFE_SCHEMES):
                safe.append(("href", href))
        elif tag == "img":
            src = attrs.get("src") or ""
            if not _SAFE_IMAGE.match(src):
                self.dropped_images += 1
                return None
            safe += [("src", src), ("alt", attrs.get("alt") or "")]
        elif tag in ("td", "th"):
            for name in ("colspan", "rowspan"):
                value = attrs.get(name) or ""
                if value.isdigit() and len(value) <= 3:
                    safe.append((name, value))
        elif tag == "input":
            if attrs.get("type") != "checkbox":
                return None
            safe += [("type", "checkbox"), ("disabled", "disabled")]
            if "checked" in attrs:
                safe.append(("checked", "checked"))
        return safe


# --- plain text ---------------------------------------------------------------


def html_to_text(fragment: str) -> str:
    """Plain text following the TXT policy described in the module docstring."""
    writer = _TextWriter()
    writer.feed(fragment)
    writer.close()
    return writer.result()


class _TextWriter(HTMLParser):
    _BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocks: list[str] = []  # finished top-level blocks
        self._lines: list[str] = []  # lines of the current top-level block
        self._line: list[str] = []  # text of the current line
        self._lists: list[list[int] | None] = []  # per open list: [counter] or None (bullets)
        self._table_depth = 0
        self._row: list[str] = []
        self._cell: list[str] = []
        self._skip_depth = 0  # footnote back-links ("↑")

    # structure

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a" and (dict(attrs).get("href") or "").startswith(
            ("#footnote-ref", "#endnote-ref")
        ):
            self._skip_depth += 1
        elif self._table_depth:
            self._start_in_table(tag)
        elif tag in ("ul", "ol"):
            if not self._lists:
                self._end_block()
            self._lists.append([0] if tag == "ol" else None)
        elif tag == "li":
            self._end_line()
            counter = self._lists[-1] if self._lists else None
            indent = "  " * max(0, len(self._lists) - 1)
            if counter is not None:
                counter[0] += 1
                self._line.append(f"{indent}{counter[0]}. ")
            else:
                self._line.append(f"{indent}- ")
        elif tag == "table":
            self._end_block()
            self._table_depth = 1
        elif tag == "br":
            self._end_line()
        elif tag in self._BLOCKS and not self._lists:
            self._end_block()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._skip_depth:
            self._skip_depth -= 1
        elif self._table_depth:
            self._end_in_table(tag)
        elif tag in ("ul", "ol") and self._lists:
            self._end_line()
            self._lists.pop()
            if not self._lists:
                self._end_block()
        elif tag == "li":
            self._end_line()
        elif tag in self._BLOCKS and not self._lists:
            self._end_block()

    def _start_in_table(self, tag: str) -> None:
        if tag == "table":
            self._table_depth += 1  # nested table: its text flows into the outer cell
        elif tag == "tr" and self._table_depth == 1:
            self._row = []
        elif tag in ("td", "th") and self._table_depth == 1:
            self._cell = []
        else:
            self._cell.append(" ")  # p, br, li inside a cell

    def _end_in_table(self, tag: str) -> None:
        if tag == "table":
            self._table_depth -= 1
            if self._table_depth == 0:
                self._end_block()
        elif tag in ("td", "th") and self._table_depth == 1:
            self._row.append(" ".join("".join(self._cell).split()))
        elif tag == "tr" and self._table_depth == 1:
            self._lines.append("\t".join(self._row))
        else:
            self._cell.append(" ")

    # text

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._table_depth:
            self._cell.append(data)
        else:
            self._line.append(data)

    def _end_line(self) -> None:
        line = "".join(self._line).rstrip()
        if line.strip():
            self._lines.append(line)
        self._line = []

    def _end_block(self) -> None:
        self._end_line()
        if self._lines:
            self._blocks.append("\n".join(self._lines))
        self._lines = []

    def result(self) -> str:
        self._end_block()
        return "\n\n".join(self._blocks) + "\n" if self._blocks else ""
