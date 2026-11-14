"""
Core Workalendar Exceptions
"""


class CalendarError(Exception):
    """
    Base Calendar Error
    """


class UnsupportedDateType(CalendarError):
    """
    Raised when trying to use an unsupported date/datetime type.
    """


class ISORegistryError(CalendarError):
    """
    Raised when you are trying to register a non-Calendar object
    """


class ICalExportError(CalendarError):
    """
    Raised when the iCal export is impossible
    """


class ICalExportRangeError(ICalExportError):
    """
    Raised when the iCal export is impossible due to wrong range
    """


class ICalExportTargetPathError(ICalExportError):
    """
    Raised when the iCal export is impossible due to unusable target path
    """


class AttendanceError(CalendarError):
    """
    Base error for shift attendance accounting
    """


class InvalidShiftError(AttendanceError):
    """
    Raised when a shift is invalid (bad punch order or bad input types)
    """


class AmbiguousPunchError(AttendanceError):
    """
    Raised when a local punch time maps to two instants, i.e. the repeated
    wall-clock hour of a daylight-saving-time "fall back" transition.

    The candidate instants are exposed through the ``candidates`` attribute.
    """

    def __init__(self, when, candidates):
        self.when = when
        self.candidates = tuple(candidates)
        labels = ", ".join(str(candidate) for candidate in self.candidates)
        super().__init__(
            f"Punch {when} is ambiguous; candidates: {labels}"
        )
