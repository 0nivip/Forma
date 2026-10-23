"""The application's main window: file list, target format, output folder, Convert."""

import itertools
import logging
import os
from collections.abc import Iterable
from functools import partial
from pathlib import Path

from PySide6.QtCore import QByteArray, QSignalBlocker, QThreadPool, QTimer
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from fileconverter.core.errors import ConversionError
from fileconverter.core.formats import Format, FormatFamily
from fileconverter.core.models import (
    ChoiceOption,
    ConversionRequest,
    ConversionResult,
    IntOption,
    JobStatus,
    OptionValue,
    Route,
)
from fileconverter.core.registry import FormatRegistry
from fileconverter.gui.job_model import Column, JobTableModel
from fileconverter.gui.worker import ConversionWorker
from fileconverter.infrastructure.settings import Settings, SettingsStore
from fileconverter.services.jobs import ACTIVE_STATES, CancelOutcome, Job, JobQueue, JobState

logger = logging.getLogger(__name__)

# Image conversions are CPU- and memory-bound; a few in parallel is plenty.
DEFAULT_MAX_PARALLEL_JOBS = max(1, min(4, os.cpu_count() or 1))
# How long closing the window waits for running jobs to reach a cancellation point.
DEFAULT_SHUTDOWN_TIMEOUT_MS = 10_000

RESULT_STATES = {
    JobStatus.SUCCEEDED: JobState.SUCCEEDED,
    JobStatus.FAILED: JobState.FAILED,
    JobStatus.CANCELLED: JobState.CANCELLED,
}


class MainWindow(QMainWindow):
    def __init__(
        self,
        registry: FormatRegistry,
        settings_store: SettingsStore | None = None,
        max_parallel_jobs: int = DEFAULT_MAX_PARALLEL_JOBS,
        shutdown_timeout_ms: int = DEFAULT_SHUTDOWN_TIMEOUT_MS,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._settings_store = settings_store
        self._model = JobTableModel(self)
        self._queue = JobQueue(max_parallel_jobs)
        # The queue decides when a job starts, so the pool never needs to queue work itself.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max_parallel_jobs)
        # Keeps each worker (and its signals object) alive until its job has finished.
        self._workers: dict[int, ConversionWorker] = {}
        self._job_ids = itertools.count(1)
        self._preferred_target: str | None = None  # last target the user picked

        self._shutting_down = False
        self._close_allowed = False
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setSingleShot(True)
        self._shutdown_timer.setInterval(shutdown_timeout_ms)
        self._shutdown_timer.timeout.connect(self._finish_shutdown)

        self.setWindowTitle(self.tr("FileConverter"))
        self.setAcceptDrops(True)
        self.resize(820, 480)
        self._build_ui()
        self._restore_settings()
        self._refresh_target_choice()
        self._update_buttons()

    # --- layout -------------------------------------------------------------

    def _build_ui(self) -> None:
        self.add_button = QPushButton(self.tr("Add Files…"))
        self.remove_button = QPushButton(self.tr("Remove Selected"))
        self.cancel_button = QPushButton(self.tr("Cancel Selected"))
        self.combine_button = QPushButton(self.tr("Combine into One PDF"))
        self.combine_button.setToolTip(
            self.tr("Merge the selected images, in list order, into a single PDF file.")
        )
        self.add_button.clicked.connect(self._choose_files)
        self.remove_button.clicked.connect(self._remove_selected)
        self.cancel_button.clicked.connect(self.cancel_selected)
        self.combine_button.clicked.connect(self.combine_selected_into_pdf)

        top_row = QHBoxLayout()
        top_row.addWidget(self.add_button)
        top_row.addWidget(self.remove_button)
        top_row.addWidget(self.cancel_button)
        top_row.addWidget(self.combine_button)
        top_row.addStretch()
        top_row.addWidget(QLabel(self.tr("Tip: you can also drop files onto this window.")))

        self.table = QTableView()
        self.table.setModel(self._model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        for column in (Column.FILE, Column.TARGET, Column.STATUS):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        # A row's state change (e.g. Running -> Done) can change which buttons apply.
        self._model.dataChanged.connect(self._update_buttons)

        self.target_combo = QComboBox()
        self.target_combo.setPlaceholderText(self.tr("Choose…"))
        self.target_combo.setMinimumContentsLength(8)
        self.target_combo.activated.connect(self._apply_target)

        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText(self.tr("Same folder as each file"))
        self.output_edit.setClearButtonEnabled(True)
        self.browse_button = QPushButton(self.tr("Browse…"))
        self.browse_button.clicked.connect(self._choose_output_dir)

        options_row = QHBoxLayout()
        options_row.addWidget(QLabel(self.tr("Convert to:")))
        options_row.addWidget(self.target_combo)
        options_row.addSpacing(16)
        options_row.addWidget(QLabel(self.tr("Output folder:")))
        options_row.addWidget(self.output_edit, stretch=1)
        options_row.addWidget(self.browse_button)

        # Options of the chosen conversion (pages, resolution, worksheet, quality, ...),
        # rebuilt from Route.options whenever the selection or target changes.
        self.route_options = QWidget()
        self._route_options_layout = QHBoxLayout(self.route_options)
        self._route_options_layout.setContentsMargins(0, 0, 0, 0)
        self.option_widgets: dict[str, QSpinBox | QLineEdit | QComboBox] = {}
        self.route_note = QLabel()
        self.route_note.setWordWrap(True)

        # The one application-wide privacy setting; read at Convert time, so changing it
        # applies to the next conversions without a restart.
        self.preserve_metadata_box = QCheckBox(
            self.tr("Preserve metadata (may include personal information)")
        )
        metadata_help = self.tr(
            "When disabled, Forma attempts to remove transferable metadata during "
            "conversion. Some format-specific information may remain."
        )
        self.preserve_metadata_box.setToolTip(metadata_help)
        self.preserve_metadata_box.toggled.connect(self._save_settings)
        self.metadata_help = QLabel(metadata_help)
        self.metadata_help.setWordWrap(True)
        self.metadata_help.setEnabled(False)  # rendered as secondary, greyed-out text
        metadata_row = QHBoxLayout()
        metadata_row.addWidget(self.preserve_metadata_box)
        metadata_row.addWidget(self.metadata_help, stretch=1)

        self.convert_button = QPushButton(self.tr("Convert"))
        self.convert_button.setDefault(True)
        self.convert_button.clicked.connect(self.start_conversion)
        bottom_row = QHBoxLayout()
        bottom_row.addStretch()
        bottom_row.addWidget(self.convert_button)

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addWidget(self.table, stretch=1)
        layout.addLayout(options_row)
        layout.addWidget(self.route_options)
        layout.addWidget(self.route_note)
        layout.addLayout(metadata_row)
        layout.addLayout(bottom_row)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

    # --- adding and removing files -----------------------------------------

    @property
    def model(self) -> JobTableModel:
        return self._model

    def add_files(self, paths: Iterable[Path]) -> None:
        if self._shutting_down:
            return
        waiting = {job.source for job in self._model.jobs() if job.state is JobState.READY}
        for path in paths:
            if path in waiting:
                continue  # already in the list and not converted yet
            self._model.add_job(self._make_job(path))
            waiting.add(path)
        self._refresh_target_choice()
        self._update_buttons()

    def _make_job(self, path: Path) -> Job:
        job_id = next(self._job_ids)
        if not path.is_file():
            problem = self.tr("Folders are not supported yet; add the files inside instead.")
            if not path.exists():
                problem = self.tr("The file does not exist.")
            return Job(job_id, path, state=JobState.UNSUPPORTED, problem=problem)
        try:
            source_format = self._registry.detect_format(path)
        except ConversionError as exc:
            return Job(job_id, path, state=JobState.UNSUPPORTED, problem=str(exc))

        targets = self._registry.targets_for(source_format.id)
        if not targets:
            # E.g. "needs FFmpeg, which was not found" - more useful than a generic text.
            problem = self._registry.unavailable_reason(source_format.id) or self.tr(
                "{0} files cannot be converted yet."
            ).format(source_format.display_name)
            return Job(job_id, path, state=JobState.UNSUPPORTED, problem=problem)
        target = next((t for t in targets if t.id == self._preferred_target), targets[0])
        return Job(job_id, path, targets, target, source_format=source_format)

    def _choose_files(self) -> None:
        def patterns(family: FormatFamily | None) -> str:
            return " ".join(
                f"*{extension}"
                for fmt in self._registry.formats()
                if fmt.can_be_source and (family is None or fmt.family is family)
                for extension in fmt.extensions
            )

        file_filter = ";;".join(
            [
                self.tr("All supported files ({0})").format(patterns(None)),
                self.tr("Images ({0})").format(patterns(FormatFamily.IMAGE)),
                self.tr("Video ({0})").format(patterns(FormatFamily.VIDEO)),
                self.tr("Audio ({0})").format(patterns(FormatFamily.AUDIO)),
                self.tr("Documents ({0})").format(patterns(FormatFamily.DOCUMENT)),
                self.tr("All files (*)"),
            ]
        )
        names, _ = QFileDialog.getOpenFileNames(self, self.tr("Add Files"), "", file_filter)
        self.add_files(Path(name) for name in names)

    def _remove_selected(self) -> None:
        rows = [
            row
            for row in self._selected_rows()
            if self._model.job_at(row).state not in ACTIVE_STATES
        ]
        self._model.remove_rows(rows)
        self._refresh_target_choice()
        self._update_buttons()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and not self._shutting_down:
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        paths = [Path(url.toLocalFile()) for url in urls if url.isLocalFile()]
        if paths:
            self.add_files(paths)
            event.acceptProposedAction()

    # --- target format ------------------------------------------------------

    def _rows_for_target_choice(self) -> list[int]:
        """Selected rows that can still be changed, or all such rows if none is selected."""
        rows = self._selected_rows() or range(self._model.rowCount())
        return [row for row in rows if self._model.job_at(row).state is JobState.READY]

    def _refresh_target_choice(self) -> None:
        """Offer only targets that every affected file supports."""
        jobs = [self._model.job_at(row) for row in self._rows_for_target_choice()]
        choices: list[Format] = []
        if jobs:
            common = set.intersection(*({t.id for t in job.targets} for job in jobs))
            choices = [fmt for fmt in self._registry.formats() if fmt.id in common]

        self.target_combo.clear()
        for fmt in choices:
            self.target_combo.addItem(fmt.display_name, fmt.id)
        current = {job.target.id for job in jobs if job.target}
        index = self.target_combo.findData(current.pop()) if len(current) == 1 else -1
        self.target_combo.setCurrentIndex(index)  # -1 shows the "Choose…" placeholder
        self.target_combo.setEnabled(bool(choices) and not self._shutting_down)
        self._refresh_route_options()

    def _apply_target(self, index: int) -> None:
        target = self._registry.get_format(self.target_combo.itemData(index))
        for row in self._rows_for_target_choice():
            self._model.update_job(self._model.job_at(row).id, target=target)
        self._preferred_target = target.id
        self._refresh_route_options()

    # --- conversion options -------------------------------------------------

    def _route_of(self, job: Job) -> Route | None:
        if job.source_format is None or job.target is None:
            return None
        try:
            return self._registry.find_route(job.source_format.id, job.target.id)[0]
        except ConversionError:
            return None

    def _refresh_route_options(self) -> None:
        """Show option fields only when every affected file uses the same conversion."""
        while self._route_options_layout.count():
            widget = self._route_options_layout.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self.option_widgets = {}

        jobs = [self._model.job_at(row) for row in self._rows_for_target_choice()]
        routes = {self._route_of(job) for job in jobs}
        route = routes.pop() if len(routes) == 1 else None
        self.route_note.setText(" ".join(route.notes) if route else "")
        self.route_note.setHidden(not (route and route.notes))
        self.route_options.setHidden(not (route and route.options))
        if not (route and route.options):
            return

        values = jobs[0].options
        for option in route.options:
            if isinstance(option, IntOption):
                widget = QSpinBox()
                widget.setRange(option.minimum, option.maximum)
                widget.setValue(int(values.get(option.name, option.default)))
                widget.valueChanged.connect(partial(self._set_option, option.name))
            elif isinstance(option, ChoiceOption):
                widget = QComboBox()
                for value, label in option.choices:
                    widget.addItem(self.tr(label), value)
                widget.setCurrentIndex(widget.findData(values.get(option.name, option.default)))
                widget.setToolTip(self.tr(option.help))
                widget.currentIndexChanged.connect(
                    lambda index, combo=widget, name=option.name: self._set_option(
                        name, combo.itemData(index)
                    )
                )
            else:
                widget = QLineEdit(str(values.get(option.name, option.default)))
                widget.setPlaceholderText(option.placeholder)
                widget.setMaxLength(option.max_length)
                widget.textChanged.connect(partial(self._set_option, option.name))
            self._route_options_layout.addWidget(QLabel(self.tr("{0}:").format(option.label)))
            self._route_options_layout.addWidget(widget)
            self.option_widgets[option.name] = widget
        self._route_options_layout.addStretch()

    def _set_option(self, name: str, value: OptionValue) -> None:
        for row in self._rows_for_target_choice():
            job = self._model.job_at(row)
            self._model.update_job(job.id, options={**job.options, name: value})

    # --- combining images ---------------------------------------------------

    def _combinable_rows(self) -> list[int]:
        """Selected, not yet converted single files that can become PDF pages."""
        return [
            row
            for row in self._selected_rows()
            if (job := self._model.job_at(row)).state is JobState.READY
            and not job.extra_sources
            and any(target.id == "pdf" for target in job.targets)
        ]

    def combine_selected_into_pdf(self) -> None:
        """Replace the selected images by one job that turns them into one PDF."""
        rows = self._combinable_rows()
        if len(rows) < 2 or self._shutting_down:
            return
        jobs = [self._model.job_at(row) for row in rows]
        pdf = self._registry.get_format("pdf")
        group = Job(
            next(self._job_ids),
            jobs[0].source,
            [pdf],
            pdf,
            source_format=jobs[0].source_format,
            extra_sources=tuple(job.source for job in jobs[1:]),
        )
        self._model.remove_rows(rows)
        self._model.insert_job(rows[0], group)
        self.table.selectRow(rows[0])
        self._refresh_target_choice()
        self._update_buttons()

    # --- output folder ------------------------------------------------------

    def _choose_output_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, self.tr("Choose Output Folder"), self.output_edit.text()
        )
        if folder:
            self.output_edit.setText(str(Path(folder)))

    # --- running jobs -------------------------------------------------------

    def start_conversion(self) -> None:
        if self._shutting_down:
            return
        text = self.output_edit.text().strip()
        output_dir = Path(text) if text else None  # None: next to each source file
        if output_dir is not None and not (output_dir.is_absolute() and output_dir.is_dir()):
            QMessageBox.warning(
                self,
                self.tr("Output folder not found"),
                self.tr("The output folder does not exist:\n{0}").format(text),
            )
            return

        # Only READY jobs are picked, and queuing moves them out of READY, so pressing
        # Convert again can never schedule the same job twice.
        ready = [job for job in self._model.jobs() if job.state is JobState.READY and job.target]
        for job in ready:
            request = ConversionRequest(
                job.source,
                job.target.id,
                output_dir or job.source.parent,
                dict(job.options),
                job.extra_sources,
                preserve_metadata=self.preserve_metadata_box.isChecked(),
            )
            self._model.set_state(job.id, JobState.QUEUED, request=request)
            self._queue.submit(job.id)
        if ready:
            self.statusBar().showMessage(self.tr("Converting {0} file(s)…").format(len(ready)))
            self._save_settings()
        self._start_jobs()
        self._refresh_target_choice()
        self._update_buttons()

    def _start_jobs(self) -> None:
        """Hand jobs to worker threads while the queue has free slots."""
        for job_id, cancel_event in self._queue.take_startable():
            worker = ConversionWorker(
                job_id, self._model.job(job_id).request, self._registry, cancel_event
            )
            worker.signals.progress.connect(self._on_job_progress)
            worker.signals.finished.connect(self._on_job_finished)
            self._workers[job_id] = worker
            self._model.set_state(job_id, JobState.RUNNING)
            self._pool.start(worker)

    def cancel_selected(self) -> None:
        for row in self._selected_rows():
            self._cancel_job(self._model.job_at(row).id)
        self._update_buttons()

    def _cancel_job(self, job_id: int) -> None:
        job = self._model.job(job_id)
        if job.state not in (JobState.QUEUED, JobState.RUNNING):
            return  # finished, not started, or already cancelling
        outcome = self._queue.cancel(job_id)
        if outcome is CancelOutcome.REMOVED:
            self._model.set_state(job_id, JobState.CANCELLED)
        elif outcome is CancelOutcome.REQUESTED:
            # The worker stops at its next checkpoint; the result arrives later.
            self._model.set_state(job_id, JobState.CANCELLING, cancel_requested=True)

    def _on_job_progress(self, job_id: int, fraction: float | None) -> None:
        self._model.update_job(job_id, progress=fraction)

    def _on_job_finished(self, job_id: int, result: ConversionResult) -> None:
        # Release the job's slot and worker first, so that even if recording the
        # result failed, the queue would keep going.
        self._queue.finished(job_id)
        self._workers.pop(job_id, None)
        try:
            self._model.set_state(job_id, RESULT_STATES[result.status], result=result)
        finally:
            self._start_jobs()
            if self._queue.is_idle():
                self._show_summary()
                if self._shutting_down:
                    self._finish_shutdown()
            self._update_buttons()

    def _show_summary(self) -> None:
        states = [job.state for job in self._model.jobs()]
        self.statusBar().showMessage(
            self.tr("Finished: {0} converted, {1} failed, {2} cancelled.").format(
                states.count(JobState.SUCCEEDED),
                states.count(JobState.FAILED),
                states.count(JobState.CANCELLED),
            )
        )

    def has_running_jobs(self) -> bool:
        return self._queue.running_count > 0

    # --- shutdown -----------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        """Shutdown policy.

        * No active jobs: save settings and close.
        * Active jobs: ask the user. If confirmed, drop queued jobs, request cancellation
          of running ones, and close once they have stopped - without blocking the event
          loop. If a job does not reach a cancellation point within the timeout, close
          anyway; the caller (app.main) then decides how to end the process.
        """
        if self._close_allowed or self._queue.is_idle():
            self._save_settings()
            event.accept()
            return
        event.ignore()
        if self._shutting_down or not self._confirm_stop():
            return
        self._shutting_down = True
        for job in self._model.jobs():
            self._cancel_job(job.id)
        self.statusBar().showMessage(
            self.tr("Stopping conversions… The window will close when they have stopped.")
        )
        self._refresh_target_choice()
        self._update_buttons()
        if self._queue.is_idle():
            self._finish_shutdown()
        else:
            self._shutdown_timer.start()

    def _confirm_stop(self) -> bool:
        answer = QMessageBox.question(
            self,
            self.tr("Conversions in progress"),
            self.tr("Some files are still being converted. Cancel them and close?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _finish_shutdown(self) -> None:
        if self._close_allowed:
            return
        self._shutdown_timer.stop()
        if self.has_running_jobs():
            logger.warning(
                "Closing with %d conversion(s) that did not stop within the timeout",
                self._queue.running_count,
            )
        self._close_allowed = True
        self.close()

    # --- settings -----------------------------------------------------------

    def _restore_settings(self) -> None:
        if self._settings_store is None:
            return
        settings = self._settings_store.load()
        if settings.output_dir and Path(settings.output_dir).is_dir():
            self.output_edit.setText(settings.output_dir)
        if any(fmt.id == settings.preferred_target for fmt in self._registry.formats()):
            self._preferred_target = settings.preferred_target
        if settings.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(settings.window_geometry.encode("ascii")))
        # No toggled() signal here: it would save while the window is half restored.
        with QSignalBlocker(self.preserve_metadata_box):
            self.preserve_metadata_box.setChecked(settings.preserve_metadata)

    def _save_settings(self) -> None:
        if self._settings_store is None:
            return
        geometry = bytes(self.saveGeometry().toBase64().data()).decode("ascii")
        self._settings_store.save(
            Settings(
                output_dir=self.output_edit.text().strip(),
                preferred_target=self._preferred_target or "",
                window_geometry=geometry,
                preserve_metadata=self.preserve_metadata_box.isChecked(),
            )
        )

    # --- helpers ------------------------------------------------------------

    def _selected_rows(self) -> list[int]:
        return sorted(index.row() for index in self.table.selectionModel().selectedRows())

    def _on_selection_changed(self) -> None:
        self._refresh_target_choice()
        self._update_buttons()

    def _update_buttons(self) -> None:
        open_for_changes = not self._shutting_down
        selected = [self._model.job_at(row) for row in self._selected_rows()]
        self.add_button.setEnabled(open_for_changes)
        self.convert_button.setEnabled(
            open_for_changes
            and any(job.state is JobState.READY and job.target for job in self._model.jobs())
        )
        self.remove_button.setEnabled(
            open_for_changes and any(job.state not in ACTIVE_STATES for job in selected)
        )
        self.cancel_button.setEnabled(
            any(job.state in (JobState.QUEUED, JobState.RUNNING) for job in selected)
        )
        self.combine_button.setEnabled(open_for_changes and len(self._combinable_rows()) >= 2)
