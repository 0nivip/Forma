"""The list of conversion jobs shown in the main window's table."""

from enum import IntEnum

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyle

from fileconverter.core.errors import (
    ConversionError,
    ConversionTimeout,
    InvalidInputError,
    InvalidOptionError,
    MissingDependencyError,
    NoContentError,
    OutputError,
    UnsupportedConversionError,
    UnsupportedFormatError,
)
from fileconverter.services.jobs import Job, JobState

MAX_TOOLTIP_PATHS = 20


class Column(IntEnum):
    FILE = 0
    TARGET = 1
    STATUS = 2
    DETAILS = 3


class JobTableModel(QAbstractTableModel):
    """Owns the Job objects; views read them through the Qt model API.

    All methods must be called from the GUI thread. Worker threads never touch
    the model directly; their signals are delivered to the window first.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._jobs: list[Job] = []

    # --- Qt model API -------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._jobs)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(Column)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            headers = [
                self.tr("File"),
                self.tr("Convert to"),
                self.tr("Status"),
                self.tr("Details"),
            ]
            return headers[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        job = self._jobs[index.row()]
        column = Column(index.column())
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_text(job, column)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(job, column)
        if role == Qt.ItemDataRole.DecorationRole and column == Column.STATUS:
            return self._status_icon(job)
        return None

    # --- job access ---------------------------------------------------------

    def jobs(self) -> list[Job]:
        return list(self._jobs)

    def job_at(self, row: int) -> Job:
        return self._jobs[row]

    def add_job(self, job: Job) -> None:
        self.insert_job(len(self._jobs), job)

    def insert_job(self, row: int, job: Job) -> None:
        self.beginInsertRows(QModelIndex(), row, row)
        self._jobs.insert(row, job)
        self.endInsertRows()

    def remove_rows(self, rows: list[int]) -> None:
        for row in sorted(rows, reverse=True):  # back to front keeps indexes valid
            self.beginRemoveRows(QModelIndex(), row, row)
            del self._jobs[row]
            self.endRemoveRows()

    def job(self, job_id: int) -> Job:
        return self._jobs[self._row_of(job_id)]

    def row_of(self, job_id: int) -> int:
        return self._row_of(job_id)

    def set_state(self, job_id: int, state: JobState, **changes: object) -> None:
        """Change a job's state (validated) and optionally other fields in one update."""
        row = self._row_of(job_id)
        self._jobs[row].transition_to(state)  # raises InvalidTransitionError if not allowed
        self._apply(row, changes)

    def update_job(self, job_id: int, **changes: object) -> None:
        """Change fields other than the state, e.g. target or progress."""
        if "state" in changes:
            raise ValueError("Use set_state() to change a job's state.")
        self._apply(self._row_of(job_id), changes)

    def _apply(self, row: int, changes: dict[str, object]) -> None:
        job = self._jobs[row]
        for name, value in changes.items():
            setattr(job, name, value)
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(Column) - 1))

    def _row_of(self, job_id: int) -> int:
        for row, job in enumerate(self._jobs):
            if job.id == job_id:
                return row
        raise KeyError(f"No job with id {job_id}.")

    # --- presentation -------------------------------------------------------

    def _display_text(self, job: Job, column: Column) -> str:
        if column == Column.FILE:
            name = job.source.name
            if job.extra_sources:
                name = self.tr("{0} + {1} more").format(name, len(job.extra_sources))
            if job.attempt > 1:
                name = self.tr("{0} (attempt {1})").format(name, job.attempt)
            return name
        if column == Column.TARGET:
            return job.target.display_name if job.target else "—"
        if column == Column.STATUS:
            return self.status_text(job)
        return self.details_text(job)

    def _tooltip(self, job: Job, column: Column) -> str | None:
        if column == Column.FILE:
            return "\n".join(str(path) for path in (job.source, *job.extra_sources))
        if column == Column.DETAILS:
            if job.result and job.result.output_paths:
                paths = [str(path) for path in job.result.output_paths]
                if len(paths) > MAX_TOOLTIP_PATHS:
                    hidden = len(paths) - MAX_TOOLTIP_PATHS
                    paths = [*paths[:MAX_TOOLTIP_PATHS], self.tr("… and {0} more").format(hidden)]
                return "\n".join([*paths, *job.result.warnings])
            return self.details_text(job) or None
        return None

    def status_text(self, job: Job) -> str:
        match job.state:
            case JobState.READY:
                return self.tr("Ready")
            case JobState.QUEUED:
                return self.tr("Waiting")
            case JobState.RUNNING if job.progress is None:
                return self.tr("Converting…")
            case JobState.RUNNING:
                return self.tr("Converting {0}%").format(round(job.progress * 100))
            case JobState.CANCELLING:
                return self.tr("Cancelling…")
            case JobState.SUCCEEDED:
                return self.tr("Done")
            case JobState.FAILED if job.retried:
                return self.tr("Failed (retried)")  # a newer attempt is listed below
            case JobState.FAILED:
                return self.tr("Failed")
            case JobState.CANCELLED:
                return self.tr("Cancelled")
            case JobState.UNSUPPORTED:
                return self.tr("Not supported")

    def details_text(self, job: Job) -> str:
        result = job.result
        if job.state is JobState.UNSUPPORTED:
            return job.problem
        if job.state is JobState.SUCCEEDED and result and result.output_path:
            outputs = result.output_paths
            if len(outputs) == 1:
                parts = [self.tr("Saved as {0}").format(outputs[0].name)]
            else:
                parts = [
                    self.tr("Saved {0} files: {1} … {2}").format(
                        len(outputs), outputs[0].name, outputs[-1].name
                    )
                ]
            if job.cancel_requested:
                parts.append(self.tr("The file was already saved when cancellation was requested."))
            return " — ".join([*parts, *result.warnings])
        if job.state is JobState.FAILED and result and result.error:
            # Library messages often lack a final period; add one so the hint reads separately.
            message = str(result.error).rstrip(". ")
            return f"{message}. {self._error_hint(result.error)}"
        if job.state is JobState.CANCELLING:
            return self.tr("Stopping at the next safe point…")
        if job.state is JobState.CANCELLED:
            if result is None:
                return self.tr("Cancelled before it started.")
            return self.tr("Stopped before finishing; no file was saved.")
        return ""

    def _error_hint(self, error: ConversionError) -> str:
        """A short 'what to do next' sentence for each failure category."""
        if isinstance(error, ConversionTimeout):
            return self.tr("The file may be very large, very complex or damaged.")
        if isinstance(error, NoContentError):
            return self.tr("There is nothing to convert.")
        if isinstance(error, MissingDependencyError):
            return self.tr("Install the missing tool, then restart the application.")
        if isinstance(error, InvalidInputError):
            return self.tr("Check that the file exists and is not damaged.")
        if isinstance(error, UnsupportedFormatError):
            return self.tr("This file type is not supported.")
        if isinstance(error, UnsupportedConversionError):
            return self.tr("Choose a different target format.")
        if isinstance(error, InvalidOptionError):
            return self.tr("Check the conversion settings.")
        if isinstance(error, OutputError):
            return self.tr("Check that the output folder exists, is writable and has free space.")
        return self.tr("This is probably a bug in FileConverter.")

    def _status_icon(self, job: Job) -> QIcon | None:
        icon = None
        if job.state is JobState.SUCCEEDED:
            has_warnings = bool(job.result and job.result.warnings)
            icon = (
                QStyle.StandardPixmap.SP_MessageBoxWarning
                if has_warnings
                else QStyle.StandardPixmap.SP_DialogApplyButton
            )
        elif job.state in (JobState.FAILED, JobState.UNSUPPORTED):
            icon = QStyle.StandardPixmap.SP_MessageBoxCritical
        elif job.state is JobState.CANCELLED:
            icon = QStyle.StandardPixmap.SP_DialogCancelButton
        return QApplication.style().standardIcon(icon) if icon is not None else None
