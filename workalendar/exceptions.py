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


class SchedulingError(CalendarError):
    """
    Base error for nominal-period scheduling.
    """


class NoWorkingDayInPeriodError(SchedulingError):
    """
    Raised when a nominal month has no working day a run could be scheduled on.

    The fallback policy never silently moves a run into another nominal
    month (which would steal another period's slot), so this situation has to
    be reported explicitly instead.
    """

    def __init__(self, year, month, product=None, period=None):
        self.year = year
        self.month = month
        self.product = product
        self.period = period
        location = f"{year}-{month:02d}"
        if product is not None:
            location = f"{product} / {location}"
        if period is not None:
            location = f"{location} (period {period})"
        super().__init__(
            f"No working day available in nominal month {location}. "
            "Refusing to fall back into another nominal month."
        )


class UnknownPeriodError(SchedulingError):
    """
    Raised when asked to resend a period that is not part of the run table.
    """

    def __init__(self, period, product=None):
        self.period = period
        self.product = product
        target = f"product {product}, " if product is not None else ""
        super().__init__(
            f"Unknown nominal period {period!r} for {target}"
            "cannot resend it."
        )
