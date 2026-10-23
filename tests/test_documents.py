"""DOCX -> TXT / HTML through the real registry and runner (DOCX files are generated)."""

import multiprocessing
import threading
import time
import zipfile

import pytest

from fileconverter.conversion import runner
from fileconverter.conversion.backends import documents
from fileconverter.conversion.backends.documents import (
    check_archive_size,
    html_to_text,
    sanitize_html,
)
from fileconverter.core.errors import (
    ConversionTimeout,
    InvalidInputError,
    NoContentError,
    UnsupportedFormatError,
)
from fileconverter.core.models import ConversionRequest, JobStatus
from helpers import (
    crashing_docx_child,
    docx_link,
    docx_paragraph,
    docx_table,
    hanging_docx_child,
    make_docx,
    temp_files,
)

SAMPLE_BODY = (
    docx_paragraph("Заголовок Größe", style="Heading1")
    + docx_paragraph("Первый абзац <b>не тег</b> & Straße")
    + docx_paragraph("")
    + docx_paragraph("пункт один", num_id=1)
    + docx_paragraph("пункт два", num_id=1)
    + docx_paragraph("шаг", num_id=2)
    + docx_table([["Name", "Wert"], ["Ёлка", "ü"]])
    + docx_link("safe link", "rIdL1")
    + docx_link("evil link", "rIdL2")
)
LINKS = {"rIdL1": "https://example.com/?a=1&b=2", "rIdL2": "javascript:alert(1)"}


def convert(registry, source, target):
    return runner.run_job(ConversionRequest(source, target, source.parent), registry)


@pytest.fixture
def sample(tmp_path):
    return make_docx(tmp_path / "отчёт report.docx", SAMPLE_BODY, LINKS)


def test_docx_is_detected_by_archive_content(registry, sample):
    assert registry.detect_format(sample).id == "docx"
    assert {f.id for f in registry.targets_for("docx")} == {"html", "txt"}


def test_docx_to_txt_follows_documented_policy(registry, sample):
    original = sample.read_bytes()

    result = convert(registry, sample, "txt")

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert result.output_path.name == "отчёт report.txt"
    assert result.output_path.read_bytes().decode("utf-8") == (
        "Заголовок Größe\n"
        "\n"
        "Первый абзац <b>не тег</b> & Straße\n"
        "\n"
        "- пункт один\n"
        "- пункт два\n"
        "\n"
        "1. шаг\n"
        "\n"
        "Name\tWert\n"
        "Ёлка\tü\n"
        "\n"
        "safe link\n"
        "\n"
        "evil link\n"
    )
    assert sample.read_bytes() == original


def test_docx_to_html_is_semantic_utf8_and_safe(registry, sample):
    result = convert(registry, sample, "html")

    page = result.output_path.read_bytes().decode("utf-8")
    assert '<meta charset="utf-8">' in page
    assert "default-src 'none'" in page  # CSP: no scripts, no network
    assert "<h1>Заголовок Größe</h1>" in page
    assert "&lt;b&gt;не тег&lt;/b&gt; &amp; Straße" in page  # text stays text
    assert "<ul><li>пункт один</li><li>пункт два</li></ul>" in page
    assert "<ol><li>шаг</li></ol>" in page
    assert "<table>" in page and "Ёлка" in page
    assert 'href="https://example.com/?a=1&amp;b=2"' in page
    assert "javascript" not in page.lower()
    assert "<a>evil link</a>" in page  # link text kept, dangerous target removed
    assert "<title>отчёт report</title>" in page


def test_empty_document_is_not_a_silent_success(registry, tmp_path):
    empty = make_docx(tmp_path / "empty.docx", docx_paragraph("") + docx_paragraph(""))

    for target in ("txt", "html"):
        result = convert(registry, empty, target)
        assert isinstance(result.error, NoContentError)
    assert temp_files(tmp_path) == []
    assert [p.name for p in tmp_path.iterdir()] == ["empty.docx"]


def test_malformed_document_xml_is_reported(registry, tmp_path):
    path = make_docx(tmp_path / "broken.docx", "<w:p><w:r><w:t>unclosed")

    result = convert(registry, path, "txt")

    assert isinstance(result.error, InvalidInputError)
    assert temp_files(tmp_path) == []


def test_zip_that_is_not_a_word_document(registry, tmp_path):
    path = tmp_path / "fake.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "just a zip")

    result = convert(registry, path, "txt")

    assert isinstance(result.error, UnsupportedFormatError)


def test_truncated_archive_is_reported(registry, sample):
    data = sample.read_bytes()
    sample.write_bytes(data[: len(data) // 2])  # keeps the PK header, loses the directory

    result = convert(registry, sample, "html")

    assert result.status is JobStatus.FAILED
    assert temp_files(sample.parent) == []


def test_mammoth_warnings_become_one_short_warning(registry, tmp_path):
    path = make_docx(tmp_path / "styled.docx", docx_paragraph("text", style="Fancy"))

    result = convert(registry, path, "html")

    assert result.status is JobStatus.SUCCEEDED
    assert len(result.warnings) == 1
    assert "details in the log" in result.warnings[0]


def test_archive_size_limit(tmp_path, sample):
    with pytest.raises(InvalidInputError, match="too large"):
        check_archive_size(sample, limit=10)


def test_existing_output_is_not_overwritten(registry, sample):
    existing = sample.with_suffix(".txt")
    existing.write_text("keep me", encoding="utf-8")

    result = convert(registry, sample, "txt")

    assert result.output_path.name == "отчёт report (1).txt"
    assert existing.read_text(encoding="utf-8") == "keep me"


# --- isolated mammoth process: timeout, cancellation, crashes -------------------------


def no_child_processes():
    return multiprocessing.active_children() == []


def test_hanging_conversion_times_out_and_leaves_nothing(registry, sample, monkeypatch):
    monkeypatch.setattr(documents, "_CHILD_TARGET", hanging_docx_child)
    monkeypatch.setattr(documents, "DOCX_TIMEOUT_S", 1.0)
    begin = time.monotonic()

    result = convert(registry, sample, "txt")

    assert isinstance(result.error, ConversionTimeout)
    assert time.monotonic() - begin < 15
    assert no_child_processes()  # the stuck child was killed and reaped
    assert sorted(p.name for p in sample.parent.iterdir()) == [sample.name]


def test_cancel_stops_the_child_process(registry, sample, monkeypatch):
    monkeypatch.setattr(documents, "_CHILD_TARGET", hanging_docx_child)
    cancel = threading.Event()
    begin = time.monotonic()

    # Progress None is reported right before the converter starts: cancel then, while
    # the child is busy, so that only killing the child can end the job quickly.
    result = runner.run_job(
        ConversionRequest(sample, "html", sample.parent),
        registry,
        cancel,
        lambda fraction: cancel.set(),
    )

    assert result.status is JobStatus.CANCELLED
    assert time.monotonic() - begin < 15
    assert no_child_processes()
    assert sorted(p.name for p in sample.parent.iterdir()) == [sample.name]


def test_crashing_child_is_a_clear_failure(registry, sample, monkeypatch):
    monkeypatch.setattr(documents, "_CHILD_TARGET", crashing_docx_child)

    result = convert(registry, sample, "txt")

    assert isinstance(result.error, InvalidInputError)
    assert "stopped unexpectedly" in str(result.error) and "exit code 3" in str(result.error)
    assert no_child_processes()
    assert temp_files(sample.parent) == []


def test_successful_conversion_leaves_no_child_process(registry, sample):
    assert convert(registry, sample, "txt").status is JobStatus.SUCCEEDED
    assert no_child_processes()


# --- sanitizer and text policy units -------------------------------------------


@pytest.mark.parametrize(
    ("dirty", "clean"),
    [
        ("<p onclick='x()'>hi</p>", "<p>hi</p>"),
        ("<script>alert(1)</script><p>ok</p>", "<p>ok</p>"),
        ('<a href="JavaScript:alert(1)">x</a>', "<a>x</a>"),
        ('<a href="data:text/html,evil">x</a>', "<a>x</a>"),
        ('<a href="#footnote-1" id="ref-1">[1]</a>', '<a id="ref-1" href="#footnote-1">[1]</a>'),
        ('<img src="https://tracker.example/x.png">', ""),
        ('<img src="data:image/svg+xml;base64,PHN2Zz4=">', ""),
        (
            '<img src="data:image/png;base64,iVBORw0K" alt="a">',
            '<img src="data:image/png;base64,iVBORw0K" alt="a">',
        ),  # noqa: E501
        ('<td colspan="2" style="x">a</td>', '<td colspan="2">a</td>'),
        (
            '<input type="checkbox" checked>',
            '<input type="checkbox" disabled="disabled" checked="checked">',
        ),  # noqa: E501
        ("<iframe src='x'>inner</iframe>", ""),
        ("<p>&lt;tag&gt; &amp;</p>", "<p>&lt;tag&gt; &amp;</p>"),
    ],
)
def test_sanitizer(dirty, clean):
    assert sanitize_html(dirty)[0] == clean


def test_sanitizer_counts_dropped_images():
    _, dropped = sanitize_html('<img src="data:image/x-emf;base64,AAAA"><img src="a.png">')
    assert dropped == 2


def test_text_policy_nested_lists_and_footnotes():
    fragment = (
        "<p>Text<sup><a href='#footnote-1' id='footnote-ref-1'>[1]</a></sup></p>"
        "<ul><li>outer<ul><li>inner</li></ul></li><li>second</li></ul>"
        "<p>line one<br />line two</p>"
        "<ol><li id='footnote-1'><p>Note text <a href='#footnote-ref-1'>↑</a></p></li></ol>"
    )
    assert html_to_text(fragment) == (
        "Text[1]\n\n- outer\n  - inner\n- second\n\nline one\nline two\n\n1. Note text\n"
    )


def test_text_policy_table_cells_are_flattened():
    fragment = "<table><tr><td><p>a</p><p>b</p></td><td>c<br/>d</td></tr></table>"
    assert html_to_text(fragment) == "a b\tc d\n"
