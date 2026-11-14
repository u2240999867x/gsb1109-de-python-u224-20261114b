"""
Multi-station working-day scheduling.

This module answers the reverse question that ``add_working_days()`` does
not: given a final deadline and a sequence of stations, what is the latest
date an order can be handed to the first station while every station still
finishes on time?

Each station carries its own calendar and processing duration.  The
handover convention is configurable per station because some contracts
count the receipt day as the first processing day and some start counting
on the following working day.
"""
from datetime import timedelta

from .core import cleaned_date, daterange
from .exceptions import SchedulingError

#: Safety bound for scans over calendars with no usable working days.
MAX_SEARCH_DAYS = 3660


class Station:
    """
    One station in a sequential handover chain.

    :param name: Human-readable station name.
    :param calendar: Any object exposing ``is_working_day()``; usually a
        :class:`workalendar.core.Calendar` instance.
    :param processing_days: Number of working days needed by the station.
    :param include_handover_day: When ``True``, the receipt day counts as
        the first processing day if it is a working day for this station.
    """

    def __init__(self, name, calendar, processing_days,
                 include_handover_day=False):
        if not name:
            raise SchedulingError("A station must have a name.")
        if not isinstance(processing_days, int) or processing_days < 0:
            raise SchedulingError(
                "Station processing duration must be a non-negative "
                "integer number of working days."
            )
        if not callable(getattr(calendar, 'is_working_day', None)):
            raise SchedulingError(
                "A station calendar must provide `is_working_day()`."
            )
        self.name = name
        self.calendar = calendar
        self.processing_days = processing_days
        self.include_handover_day = include_handover_day

    def __repr__(self):
        return (
            f"<Station {self.name!r} processing_days="
            f"{self.processing_days} include_handover_day="
            f"{self.include_handover_day}>"
        )

    def is_working_day(self, day):
        return self.calendar.is_working_day(day)

    def first_processing_day(self, handover):
        """Return the first date counted as a processing day."""
        handover = cleaned_date(handover)
        if self.include_handover_day and self.is_working_day(handover):
            return handover
        return self._next_working_day(handover)

    def completion_date(self, handover):
        """Return the completion date for a receipt on ``handover``."""
        handover = cleaned_date(handover)
        if self.processing_days == 0:
            return handover
        completion = self.first_processing_day(handover)
        remaining = self.processing_days - 1
        checked = 0
        while remaining:
            completion += timedelta(days=1)
            checked += 1
            if checked > MAX_SEARCH_DAYS:
                raise SchedulingError(
                    f"Station {self.name!r} has no usable working days."
                )
            if self.is_working_day(completion):
                remaining -= 1
        return completion

    def latest_handover(self, deadline):
        """
        Return the latest receipt date that still completes by ``deadline``.

        ``deadline`` is the latest acceptable completion date.  It does not
        have to be a working day: when it is not, the last processing day
        is the previous working day.

        The result is a calendar date, not necessarily a working day.  For
        a station that does not count its receipt day, several consecutive
        receipt dates can feed into the same first processing day; this
        method returns the latest of them.
        """
        deadline = cleaned_date(deadline)
        if self.processing_days == 0:
            return deadline

        first_processing_day = self._working_day_on_or_before(deadline)
        for _ in range(self.processing_days - 1):
            first_processing_day = self._previous_working_day(
                first_processing_day
            )

        if self.include_handover_day:
            return first_processing_day
        return first_processing_day - timedelta(days=1)

    def working_days(self, handover, completion):
        """
        Return the working dates actually occupied by this station.

        The receipt date is included only when the station counts the
        handover day and that date is a working day for this station.
        """
        handover = cleaned_date(handover)
        completion = cleaned_date(completion)
        if self.processing_days == 0:
            return ()

        days = []
        for day in daterange(handover, completion):
            if day == handover and not self.include_handover_day:
                continue
            if self.is_working_day(day):
                days.append(day)
        if len(days) != self.processing_days:
            raise SchedulingError(
                f"Station {self.name!r} expected {self.processing_days} "
                f"working days but found {len(days)}."
            )
        return tuple(days)

    def _next_working_day(self, day):
        day = cleaned_date(day)
        for _ in range(MAX_SEARCH_DAYS):
            day += timedelta(days=1)
            if self.is_working_day(day):
                return day
        raise SchedulingError(
            f"Station {self.name!r} has no usable working days."
        )

    def _previous_working_day(self, day):
        day = cleaned_date(day)
        for _ in range(MAX_SEARCH_DAYS):
            day -= timedelta(days=1)
            if self.is_working_day(day):
                return day
        raise SchedulingError(
            f"Station {self.name!r} has no usable working days."
        )

    def _working_day_on_or_before(self, day):
        day = cleaned_date(day)
        if self.is_working_day(day):
            return day
        return self._previous_working_day(day)


class StationStep:
    """The scheduled receipt, completion and working dates for a station."""

    def __init__(self, station, handover, completion, working_days,
                 allowed_completion=None):
        self.station = station
        self.handover = handover
        self.completion = completion
        self.working_days = tuple(working_days)
        self.allowed_completion = allowed_completion

    @property
    def name(self):
        return self.station.name

    @property
    def working_days_count(self):
        return len(self.working_days)

    def as_dict(self):
        return {
            'station': self.name,
            'handover': self.handover,
            'completion': self.completion,
            'working_days_count': self.working_days_count,
            'working_days': list(self.working_days),
            'allowed_completion': self.allowed_completion,
        }

    def __repr__(self):
        return (
            f"<StationStep {self.name!r} handover={self.handover} "
            f"completion={self.completion} "
            f"working_days={self.working_days_count}>"
        )


class Schedule:
    """A forward schedule for a chain of stations."""

    def __init__(self, steps, submission_date, deadline, feasible):
        self.steps = tuple(steps)
        self.submission_date = submission_date
        self.deadline = deadline
        self.feasible = feasible

    @property
    def first_station(self):
        return self.steps[0]

    @property
    def final_station(self):
        return self.steps[-1]

    @property
    def completion_date(self):
        return self.final_station.completion

    def as_dict(self):
        return {
            'submission_date': self.submission_date,
            'completion_date': self.completion_date,
            'deadline': self.deadline,
            'feasible': self.feasible,
            'stations': [step.as_dict() for step in self.steps],
        }

    def __repr__(self):
        return (
            f"<Schedule submission={self.submission_date} "
            f"completion={self.completion_date} feasible={self.feasible}>"
        )


class ShiftResult:
    """
    Comparison between the latest plan and a later-submission plan.

    ``late_stations`` and ``first_late_station`` describe stations whose
    completion falls after the final deadline.  ``behind_schedule_stations``
    describe stations that slipped past the completion date allocated to
    them by the original backward plan, even if the final deadline is still
    met.
    """

    def __init__(self, original, shifted):
        self.original = original
        self.shifted = shifted

    @property
    def late_stations(self):
        deadline = self.shifted.deadline
        return tuple(
            step for step in self.shifted.steps
            if deadline is not None and step.completion > deadline
        )

    @property
    def first_late_station(self):
        late = self.late_stations
        return late[0] if late else None

    @property
    def deadline_missed(self):
        deadline = self.shifted.deadline
        return deadline is not None and self.shifted.completion_date > deadline

    @property
    def behind_schedule_stations(self):
        return tuple(
            step for step in self.shifted.steps
            if step.allowed_completion is not None
            and step.completion > step.allowed_completion
        )

    @property
    def first_behind_schedule_station(self):
        behind = self.behind_schedule_stations
        return behind[0] if behind else None

    def as_dict(self):
        late = self.first_late_station
        behind = self.first_behind_schedule_station
        return {
            'original': self.original.as_dict(),
            'shifted': self.shifted.as_dict(),
            'first_late_station': late.name if late else None,
            'late_stations': [step.name for step in self.late_stations],
            'deadline_missed': self.deadline_missed,
            'first_behind_schedule_station': (
                behind.name if behind else None
            ),
            'behind_schedule_stations': [
                step.name for step in self.behind_schedule_stations
            ],
        }

    def __repr__(self):
        late = self.first_late_station
        late_name = late.name if late else None
        return (
            f"<ShiftResult first_late_station={late_name} "
            f"deadline_missed={self.deadline_missed}>"
        )


class StationChain:
    """An ordered sequence of stations, each handing over to the next."""

    def __init__(self, stations):
        stations = tuple(stations)
        if not stations:
            raise SchedulingError("A station chain cannot be empty.")
        if not all(isinstance(station, Station) for station in stations):
            raise SchedulingError(
                "A station chain must only contain Station instances."
            )
        self.stations = stations

    def __repr__(self):
        names = ', '.join(station.name for station in self.stations)
        return f"<StationChain [{names}]>"

    def latest_submission_date(self, deadline):
        """
        Return the latest first-station receipt date meeting ``deadline``.
        """
        return self.latest_schedule(deadline).submission_date

    def latest_schedule(self, deadline):
        """
        Build the latest feasible schedule from a final deadline.

        The backward pass determines the latest receipt date for every
        station.  A forward pass is then run from the latest first-station
        receipt date, which yields the dates actually occupied by each
        station and preserves the handover chain.
        """
        deadline = cleaned_date(deadline)
        allowed_completion = deadline
        allowed_completions = [None] * len(self.stations)
        latest_handovers = [None] * len(self.stations)

        for index in range(len(self.stations) - 1, -1, -1):
            station = self.stations[index]
            allowed_completions[index] = allowed_completion
            handover = station.latest_handover(allowed_completion)
            latest_handovers[index] = handover
            allowed_completion = handover

        return self.forward_schedule(
            latest_handovers[0], deadline=deadline,
            allowed_completions=allowed_completions,
        )

    def forward_schedule(self, submission_date, deadline=None,
                         allowed_completions=None):
        """Run the chain forward from a first-station receipt date."""
        submission_date = cleaned_date(submission_date)
        if deadline is not None:
            deadline = cleaned_date(deadline)
        if allowed_completions is None:
            allowed_completions = [None] * len(self.stations)
        elif len(allowed_completions) != len(self.stations):
            raise SchedulingError(
                "One allowed completion date is required per station."
            )

        handover = submission_date
        steps = []
        for station, allowed in zip(self.stations, allowed_completions):
            completion = station.completion_date(handover)
            working_days = station.working_days(handover, completion)
            steps.append(StationStep(
                station=station,
                handover=handover,
                completion=completion,
                working_days=working_days,
                allowed_completion=allowed,
            ))
            handover = completion

        completion_date = steps[-1].completion
        if deadline is None:
            feasible = True
        else:
            feasible = completion_date <= deadline
        return Schedule(
            steps=steps,
            submission_date=submission_date,
            deadline=deadline,
            feasible=feasible,
        )

    def shift_latest_submission(self, deadline, days=1):
        """
        Shift the latest submission by ``days`` and run the chain forward.

        The returned :class:`ShiftResult` reports both the stations whose
        completion misses the final deadline (``first_late_station``) and
        the stations that fall behind their backward-plan allocation
        (``first_behind_schedule_station``).
        """
        if not isinstance(days, int) or days == 0:
            raise SchedulingError(
                "Shift must be a non-zero integer number of calendar days."
            )
        original = self.latest_schedule(deadline)
        shifted_submission = original.submission_date + timedelta(days=days)
        shifted = self.forward_schedule(
            shifted_submission,
            deadline=original.deadline,
            allowed_completions=[
                step.allowed_completion for step in original.steps
            ],
        )
        return ShiftResult(original=original, shifted=shifted)


def latest_submission_date(stations, deadline):
    """Return the latest first-station receipt date for ``stations``."""
    return StationChain(stations).latest_submission_date(deadline)


def latest_schedule(stations, deadline):
    """Build the latest schedule for ``stations`` from ``deadline``."""
    return StationChain(stations).latest_schedule(deadline)


def forward_schedule(stations, submission_date, deadline=None,
                     allowed_completions=None):
    """Run ``stations`` forward from ``submission_date``."""
    return StationChain(stations).forward_schedule(
        submission_date,
        deadline=deadline,
        allowed_completions=allowed_completions,
    )
