"""Exception hierarchy for expected, user-facing conversion failures.

Each subclass describes a distinct failure category so that a UI can show
a specific (and later localized) message based on the exception type.
Unexpected exceptions (programming bugs) are NOT wrapped in these classes.
"""


class ConversionError(Exception):
    """Base class for expected conversion failures."""


class InvalidInputError(ConversionError):
    """The source file is missing, unreadable, corrupted or otherwise unusable."""


class UnsupportedFormatError(ConversionError):
    """The source file's format is not recognized."""


class UnsupportedConversionError(ConversionError):
    """No converter supports the requested source -> target pair."""


class InvalidOptionError(ConversionError):
    """A conversion option is unknown or has an invalid value."""


class OutputError(ConversionError):
    """The output could not be written or verified."""


class ConversionCancelled(Exception):
    """Raised inside a converter when the user cancelled the job.

    Deliberately not a ConversionError: cancellation is not a failure.
    """
