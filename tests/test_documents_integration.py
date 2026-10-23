"""Phase 5 conversions through the CLI and the GUI, plus PDFium concurrency."""

import threading

import openpyxl
import pytest
from PySide6.QtCore import Qt

from fileconverter.cli import main
from fileconverter.conversion import runner
from fileconverter.core.models import ConversionRequest, JobStatus
from fileconverter.gui.job_model import Column
from fileconverter.gui.main_window import MainWindow
from fileconverter.services.jobs import JobState
from helpers import docx_paragraph, make_docx, make_image, temp_files, write_pdf

TIMEOUT_MS = 30_000


def make_book(path, sheets):
    book = openpyxl.Workbook()
    book.remove(book.active)
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    book.save(path)
    return path


# --- CLI ------------------------------------------------------------------------


def test_cli_pdf_pages_and_dpi(tmp_path, capsys):
    source = write_pdf(tmp_path / "doc.pdf", [(72, 72, (255, 0, 0))] * 3)

    assert main(["convert", str(source), "--to", "png", "--pages", "2-3", "--dpi", "72"]) == 0

    out = capsys.readouterr().out
    assert "doc_page_002.png" in out and "doc_page_003.png" in out
    assert sorted(p.name for p in tmp_path.glob("*.png")) == [
        "doc_page_002.png",
        "doc_page_003.png",
    ]


def test_cli_sheet_selection(tmp_path):
    source = make_book(tmp_path / "book.xlsx", {"A": [[1]], "B": [[2]]})
    assert main(["convert", str(source), "--to", "csv", "--sheet", "B"]) == 0
    assert [p.name for p in tmp_path.glob("*.csv")] == ["book_B.csv"]


def test_cli_combine_images(tmp_path):
    images = [make_image(tmp_path / f"{i}.png") for i in range(3)]
    assert main(["convert", *map(str, images), "--to", "pdf", "--combine"]) == 0
    assert [p.name for p in tmp_path.glob("*.pdf")] == ["0.pdf"]


def test_cli_without_combine_makes_one_pdf_per_image(tmp_path):
    images = [make_image(tmp_path / f"{i}.png") for i in range(2)]
    assert main(["convert", *map(str, images), "--to", "pdf"]) == 0
    assert sorted(p.name for p in tmp_path.glob("*.pdf")) == ["0.pdf", "1.pdf"]


def test_cli_docx_and_option_mismatch(tmp_path, capsys):
    source = make_docx(tmp_path / "a.docx", docx_paragraph("Привет"))

    assert main(["convert", str(source), "--to", "txt"]) == 0
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "Привет\n"
    assert main(["convert", str(source), "--to", "txt", "--dpi", "300"]) == 1  # not a txt option
    assert "Unsupported option" in capsys.readouterr().err


def test_cli_formats_lists_documents(capsys):
    assert main(["formats"]) == 0
    out = capsys.readouterr().out
    assert "pdf    (.pdf) -> png, jpeg" in out
    assert "csv    (.csv) -> (output only)" in out


# --- GUI ------------------------------------------------------------------------


@pytest.fixture
def window(qtbot, registry):
    win = MainWindow(registry)
    qtbot.addWidget(win)
    win.show()
    return win


def wait_done(qtbot, window):
    qtbot.waitUntil(
        lambda: all(job.state not in (JobState.QUEUED, JobState.RUNNING, JobState.CANCELLING)
                    for job in window.model.jobs()),
        timeout=TIMEOUT_MS,
    )  # fmt: skip


def test_options_appear_only_for_applicable_conversions(window, tmp_path):
    window.add_files([write_pdf(tmp_path / "doc.pdf", [(72, 72, (0, 0, 0))])])
    window.table.selectRow(0)
    assert set(window.option_widgets) == {"dpi", "pages"}
    assert not window.route_options.isHidden()
    assert "one image file per page" in window.route_note.text()

    window.add_files([make_docx(tmp_path / "a.docx", docx_paragraph("x"))])
    window.table.selectRow(1)
    assert window.option_widgets == {}
    assert window.route_options.isHidden()

    window.table.selectAll()  # different conversions: no shared options to show
    assert window.option_widgets == {} and window.route_note.isHidden()


def test_pdf_page_options_are_used(window, tmp_path, qtbot):
    source = write_pdf(tmp_path / "doc.pdf", [(72, 72, (0, 0, 255))] * 3)
    window.add_files([source])
    window.table.selectRow(0)
    window.option_widgets["pages"].setText("3")
    window.option_widgets["dpi"].setValue(72)

    window.start_conversion()
    wait_done(qtbot, window)

    job = window.model.job_at(0)
    assert job.state is JobState.SUCCEEDED, job.result.error
    assert [p.name for p in job.result.output_paths] == ["doc_page_003.png"]


def test_multi_output_job_shows_file_count(window, tmp_path, qtbot):
    window.add_files([make_book(tmp_path / "book.xlsx", {"Eins": [[1]], "Zwei": [[2]]})])

    window.start_conversion()
    wait_done(qtbot, window)

    details = window.model.index(0, Column.DETAILS).data()
    assert details.startswith("Saved 2 files: book_Eins.csv … book_Zwei.csv")
    tooltip = window.model.index(0, Column.DETAILS).data(Qt.ItemDataRole.ToolTipRole)
    assert "book_Eins.csv" in tooltip and "book_Zwei.csv" in tooltip


def test_combine_selected_images_into_one_pdf(window, tmp_path, qtbot):
    images = [make_image(tmp_path / f"{i}.png", size=(96, 96)) for i in range(3)]
    other = make_docx(tmp_path / "x.docx", docx_paragraph("x"))
    window.add_files([*images, other])

    window.table.selectRow(3)
    assert not window.combine_button.isEnabled()  # not an image
    window.table.selectAll()
    assert window.combine_button.isEnabled()
    qtbot.mouseClick(window.combine_button, Qt.MouseButton.LeftButton)

    assert window.model.rowCount() == 2  # 3 images became one job; the DOCX stays
    group = window.model.job_at(0)
    assert group.extra_sources == (images[1], images[2])
    assert group.target.id == "pdf"
    assert window.model.index(0, Column.FILE).data() == "0.png + 2 more"

    window.table.clearSelection()
    window.start_conversion()
    wait_done(qtbot, window)
    assert [p.name for p in group.result.output_paths] == ["0.pdf"]


def _block_once(monkeypatch, module, name):
    """Make module.name wait (in the worker thread) until the test releases it."""
    entered, release = threading.Event(), threading.Event()
    real = getattr(module, name)

    def blocked(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, blocked)
    return entered, release


def test_cancel_while_rendering_pages_leaves_no_files(window, tmp_path, qtbot, monkeypatch):
    from fileconverter.conversion.backends import pdf as pdf_backend

    entered, release = _block_once(monkeypatch, pdf_backend, "_render_page")
    window.add_files([write_pdf(tmp_path / "doc.pdf", [(72, 72, (0, 0, 0))] * 20)])
    window.start_conversion()
    qtbot.waitUntil(entered.is_set, timeout=TIMEOUT_MS)  # inside the first page

    window.table.selectRow(0)
    window.cancel_selected()
    assert window.model.index(0, Column.STATUS).data() == "Cancelling…"
    release.set()
    wait_done(qtbot, window)

    assert window.model.job_at(0).state is JobState.CANCELLED
    assert list(tmp_path.glob("*.png")) == []
    assert temp_files(tmp_path) == []


def test_cancel_during_commit_keeps_the_complete_set(window, tmp_path, qtbot, monkeypatch):
    # Once every page is rendered and validated, committing is no longer interrupted:
    # the user gets the full set and an honest note, never a partial set.
    entered, release = _block_once(monkeypatch, runner, "_commit_parts")
    window.add_files([write_pdf(tmp_path / "doc.pdf", [(72, 72, (0, 0, 0))] * 20)])
    window.start_conversion()
    qtbot.waitUntil(entered.is_set, timeout=TIMEOUT_MS)

    window.table.selectRow(0)
    window.cancel_selected()
    release.set()
    wait_done(qtbot, window)

    assert window.model.job_at(0).state is JobState.SUCCEEDED
    assert len(list(tmp_path.glob("doc_page_*.png"))) == 20
    assert "already saved" in window.model.index(0, Column.DETAILS).data()
    assert temp_files(tmp_path) == []


def test_gui_cancels_a_stuck_docx_conversion(window, tmp_path, qtbot, monkeypatch):
    import multiprocessing

    from fileconverter.conversion.backends import documents
    from helpers import hanging_docx_child

    monkeypatch.setattr(documents, "_CHILD_TARGET", hanging_docx_child)
    window.add_files([make_docx(tmp_path / "a.docx", docx_paragraph("x"))])
    window.start_conversion()
    qtbot.waitUntil(lambda: multiprocessing.active_children() != [], timeout=TIMEOUT_MS)

    window.table.selectRow(0)
    window.cancel_selected()
    window.add_files([make_image(tmp_path / "still responsive.png")])  # GUI thread is free
    wait_done(qtbot, window)

    assert window.model.job_at(0).state is JobState.CANCELLED
    assert multiprocessing.active_children() == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.docx", "still responsive.png"]


# --- PDFium concurrency -----------------------------------------------------------


def test_parallel_pdf_jobs_are_safe(registry, tmp_path):
    jobs = []
    for i in range(4):
        folder = tmp_path / f"job{i}"
        jobs.append(write_pdf(folder / "doc.pdf", [(144, 144, (i * 60, 0, 0))] * 5))
        jobs.append(make_image(folder / "img.png", size=(200, 100)))
    results = [None] * len(jobs)

    def work(index, source):
        target = "png" if source.suffix == ".pdf" else "pdf"
        request = ConversionRequest(source, target, source.parent)
        results[index] = runner.run_job(request, registry)

    threads = [threading.Thread(target=work, args=pair) for pair in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert all(r is not None and r.status is JobStatus.SUCCEEDED for r in results), [
        r and r.error for r in results
    ]
    assert sum(len(r.output_paths) for r in results) == 4 * 5 + 4
