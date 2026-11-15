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


class AmbiguousRegionMappingError(CalendarError):
    """
    Raised when a region code matches several mapping records
    for a given business date and must be left for confirmation.
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
