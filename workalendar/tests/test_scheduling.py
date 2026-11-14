from datetime import date

from . import CoreCalendarTest
from ..core import Calendar, FRI, SAT, SUN
from ..exceptions import SchedulingError
from ..scheduling import (
    Station, StationChain,
    forward_schedule, latest_schedule, latest_submission_date,
)


class WeekdayCalendar(Calendar):
    """Monday-to-Friday calendar with explicit extra holidays."""

    WEEKEND_DAYS = (SAT, SUN)
    extra_holidays = ()

    def get_calendar_holidays(self, year):
        return [
            (day, "Station holiday")
            for day in self.extra_holidays
            if day.year == year
        ]


class SchedulingCalendarTest(CoreCalendarTest):
    cal_class = WeekdayCalendar


class StationTest(SchedulingCalendarTest):

    def test_rejects_negative_processing_days(self):
        with self.assertRaises(SchedulingError):
            Station('A', WeekdayCalendar(), -1)

    def test_rejects_empty_name(self):
        with self.assertRaises(SchedulingError):
            Station('', WeekdayCalendar(), 1)

    def test_rejects_calendar_without_working_day_support(self):
        with self.assertRaises(SchedulingError):
            Station('A', object(), 1)

    def test_completion_without_handover_day(self):
        station = Station('A', WeekdayCalendar(), 2)
        # Friday handover does not count; processing starts on Monday.
        self.assertEqual(
            station.completion_date(date(2026, 11, 13)),
            date(2026, 11, 17),
        )

    def test_completion_with_handover_day(self):
        station = Station(
            'A', WeekdayCalendar(), 2, include_handover_day=True
        )
        self.assertEqual(
            station.completion_date(date(2026, 11, 13)),
            date(2026, 11, 16),
        )

    def test_completion_skips_station_holiday(self):
        calendar = WeekdayCalendar()
        calendar.extra_holidays = (date(2026, 11, 17),)
        station = Station('A', calendar, 2)
        self.assertEqual(
            station.completion_date(date(2026, 11, 16)),
            date(2026, 11, 19),
        )

    def test_working_days_are_actual_working_dates(self):
        calendar = WeekdayCalendar()
        calendar.extra_holidays = (date(2026, 11, 17),)
        station = Station('A', calendar, 3)
        handover = date(2026, 11, 13)
        completion = station.completion_date(handover)
        self.assertEqual(
            station.working_days(handover, completion),
            (date(2026, 11, 16), date(2026, 11, 18),
             date(2026, 11, 19)),
        )

    def test_latest_handover_depends_on_contract_convention(self):
        station = Station('A', WeekdayCalendar(), 1)
        self.assertEqual(
            station.latest_handover(date(2026, 11, 20)),
            date(2026, 11, 19),
        )

        station = Station(
            'A', WeekdayCalendar(), 1, include_handover_day=True
        )
        self.assertEqual(
            station.latest_handover(date(2026, 11, 20)),
            date(2026, 11, 20),
        )

    def test_latest_handover_returns_latest_calendar_date_in_holiday_gap(self):
        station = Station('A', WeekdayCalendar(), 1)
        # Friday, Saturday and Sunday all lead to the same Monday processing
        # day.  The latest date that is still feasible is Sunday.
        self.assertEqual(
            station.latest_handover(date(2026, 11, 23)),
            date(2026, 11, 22),
        )
        self.assertEqual(
            station.completion_date(date(2026, 11, 22)),
            date(2026, 11, 23),
        )
        self.assertEqual(
            station.completion_date(date(2026, 11, 13)),
            date(2026, 11, 16),
        )

    def test_latest_handover_maps_all_gap_dates_to_same_completion(self):
        station = Station('A', WeekdayCalendar(), 1)
        deadline = date(2026, 11, 23)
        for handover in (
            date(2026, 11, 20),
            date(2026, 11, 21),
            date(2026, 11, 22),
        ):
            self.assertEqual(
                station.completion_date(handover), deadline
            )
        self.assertEqual(station.latest_handover(deadline), date(2026, 11, 22))

    def test_latest_handover_skips_holiday_on_deadline(self):
        calendar = WeekdayCalendar()
        calendar.extra_holidays = (date(2026, 11, 20),)
        station = Station('A', calendar, 1)
        self.assertEqual(
            station.latest_handover(date(2026, 11, 20)),
            date(2026, 11, 18),
        )

    def test_unusable_calendar_raises_instead_of_looping(self):
        class NeverWorking(WeekdayCalendar):
            def is_working_day(self, day, **kwargs):
                return False

        station = Station('A', NeverWorking(), 1)
        with self.assertRaises(SchedulingError):
            station.completion_date(date(2026, 11, 16))
        with self.assertRaises(SchedulingError):
            station.latest_handover(date(2026, 11, 16))


class StationChainTest(SchedulingCalendarTest):

    def make_chain(self):
        return StationChain([
            Station(
                'A', WeekdayCalendar(), 2, include_handover_day=True
            ),
            Station(
                'B', WeekdayCalendar(), 3, include_handover_day=False
            ),
            Station(
                'C', WeekdayCalendar(), 2, include_handover_day=True
            ),
        ])

    def test_latest_schedule(self):
        chain = self.make_chain()
        schedule = chain.latest_schedule(date(2026, 11, 20))

        self.assertTrue(schedule.feasible)
        self.assertEqual(schedule.submission_date, date(2026, 11, 13))
        self.assertEqual(schedule.completion_date, date(2026, 11, 20))
        self.assertEqual(
            [step.name for step in schedule.steps],
            ['A', 'B', 'C'],
        )
        self.assertEqual(
            [step.completion for step in schedule.steps],
            [date(2026, 11, 16), date(2026, 11, 19),
             date(2026, 11, 20)],
        )
        self.assertEqual(
            [step.working_days_count for step in schedule.steps],
            [2, 3, 2],
        )

    def test_latest_submission_date_matches_schedule(self):
        chain = self.make_chain()
        self.assertEqual(
            chain.latest_submission_date(date(2026, 11, 20)),
            date(2026, 11, 13),
        )

    def test_latest_schedule_uses_each_station_calendar(self):
        station_a_calendar = WeekdayCalendar()
        station_a_calendar.extra_holidays = (date(2026, 11, 16),)
        station_b_calendar = WeekdayCalendar()
        station_b_calendar.extra_holidays = (date(2026, 11, 17),)
        station_c_calendar = WeekdayCalendar()
        station_c_calendar.extra_holidays = (date(2026, 11, 19),)

        chain = StationChain([
            Station(
                'A', station_a_calendar, 2, include_handover_day=True
            ),
            Station(
                'B', station_b_calendar, 2, include_handover_day=False
            ),
            Station(
                'C', station_c_calendar, 2, include_handover_day=True
            ),
        ])
        schedule = chain.latest_schedule(date(2026, 11, 20))

        self.assertTrue(schedule.feasible)
        self.assertEqual(schedule.submission_date, date(2026, 11, 12))
        self.assertEqual(
            schedule.steps[0].working_days,
            (date(2026, 11, 12), date(2026, 11, 13)),
        )
        self.assertEqual(
            schedule.steps[1].working_days,
            (date(2026, 11, 16), date(2026, 11, 18)),
        )
        self.assertEqual(
            schedule.steps[2].working_days,
            (date(2026, 11, 18), date(2026, 11, 20)),
        )

    def test_shift_latest_submission_by_one_day(self):
        chain = self.make_chain()
        result = chain.shift_latest_submission(
            date(2026, 11, 20), days=1
        )
        self.assertFalse(result.shifted.feasible)
        self.assertTrue(result.deadline_missed)
        self.assertEqual(result.shifted.submission_date, date(2026, 11, 14))
        self.assertIsNotNone(result.first_late_station)
        self.assertEqual(result.first_late_station.name, 'C')
        self.assertEqual(
            [step.name for step in result.late_stations],
            ['C'],
        )
        self.assertEqual(
            result.as_dict()['first_late_station'], 'C'
        )
        self.assertIsNotNone(result.first_behind_schedule_station)
        self.assertEqual(
            result.first_behind_schedule_station.name, 'A'
        )
        self.assertEqual(
            [step.name for step in result.behind_schedule_stations],
            ['A', 'B', 'C'],
        )

    def test_shift_can_report_a_later_first_late_station(self):
        # All three stations process one day.  Only station C counts its
        # receipt day, so a Thursday submission is absorbed by A and B, but
        # the extra day makes C complete after Monday's deadline.
        chain = StationChain([
            Station(
                'A', WeekdayCalendar(), 1, include_handover_day=False
            ),
            Station(
                'B', WeekdayCalendar(), 1, include_handover_day=False
            ),
            Station(
                'C', WeekdayCalendar(), 1, include_handover_day=True
            ),
        ])
        deadline = date(2026, 9, 21)
        result = chain.shift_latest_submission(deadline, days=1)

        self.assertEqual(result.original.submission_date, date(2026, 9, 17))
        self.assertEqual(result.shifted.submission_date, date(2026, 9, 18))
        self.assertIsNotNone(result.first_late_station)
        self.assertEqual(result.first_late_station.name, 'B')
        self.assertEqual(
            [step.name for step in result.late_stations],
            ['B', 'C'],
        )
        self.assertEqual(
            result.as_dict()['first_late_station'], 'B'
        )

    def test_forward_schedule_can_be_earlier_than_latest(self):
        chain = self.make_chain()
        schedule = chain.forward_schedule(
            date(2026, 11, 10), deadline=date(2026, 11, 20)
        )
        self.assertTrue(schedule.feasible)
        self.assertEqual(schedule.completion_date, date(2026, 11, 17))

    def test_shift_rejects_zero_or_non_integer(self):
        chain = self.make_chain()
        with self.assertRaises(SchedulingError):
            chain.shift_latest_submission(date(2026, 11, 20), days=0)
        with self.assertRaises(SchedulingError):
            chain.shift_latest_submission(date(2026, 11, 20), days='1')


class SchedulingHelpersTest(SchedulingCalendarTest):

    def test_zero_day_station_has_no_working_days(self):
        station = Station('A', WeekdayCalendar(), 0)
        handover = date(2026, 11, 13)
        self.assertEqual(station.completion_date(handover), handover)
        self.assertEqual(station.working_days(handover, handover), ())
        self.assertEqual(station.latest_handover(handover), handover)

    def test_zero_day_station_does_not_consume_days(self):
        chain = StationChain([
            Station('A', WeekdayCalendar(), 0),
            Station('B', WeekdayCalendar(), 1),
        ])
        schedule = chain.latest_schedule(date(2026, 11, 16))
        self.assertEqual(schedule.submission_date, date(2026, 11, 15))
        self.assertEqual(schedule.steps[0].working_days_count, 0)
        self.assertEqual(
            schedule.steps[1].handover, schedule.steps[0].completion
        )
        self.assertEqual(schedule.steps[1].completion, date(2026, 11, 16))

    def test_empty_chain_is_rejected(self):
        with self.assertRaises(SchedulingError):
            StationChain([])

    def test_chain_rejects_non_station_values(self):
        with self.assertRaises(SchedulingError):
            StationChain([Station('A', WeekdayCalendar(), 1), object()])

    def test_allowed_completions_must_match_station_count(self):
        chain = StationChain([
            Station('A', WeekdayCalendar(), 1),
            Station('B', WeekdayCalendar(), 1),
        ])
        with self.assertRaises(SchedulingError):
            chain.forward_schedule(
                date(2026, 11, 16),
                deadline=date(2026, 11, 20),
                allowed_completions=[date(2026, 11, 20)],
            )

    def test_module_level_helpers_delegate_to_chain(self):
        stations = [
            Station('A', WeekdayCalendar(), 1),
            Station('B', WeekdayCalendar(), 1),
        ]
        deadline = date(2026, 11, 18)
        self.assertEqual(
            latest_submission_date(stations, deadline),
            date(2026, 11, 16),
        )
        schedule = latest_schedule(stations, deadline)
        self.assertTrue(schedule.feasible)
        self.assertEqual(schedule.completion_date, deadline)
        forward = forward_schedule(
            stations, date(2026, 11, 16), deadline=deadline
        )
        self.assertEqual(forward.submission_date, date(2026, 11, 16))
        self.assertEqual(forward.completion_date, deadline)

    def test_chain_representation(self):
        chain = StationChain([
            Station('A', WeekdayCalendar(), 1),
            Station('B', WeekdayCalendar(), 1),
        ])
        self.assertEqual(repr(chain), '<StationChain [A, B]>')

    def test_station_representation(self):
        station = Station(
            'A', WeekdayCalendar(), 2, include_handover_day=True
        )
        self.assertIn("Station 'A'", repr(station))
        self.assertIn('processing_days=2', repr(station))
        self.assertIn('include_handover_day=True', repr(station))

    def test_step_and_schedule_representations(self):
        chain = StationChain([Station('A', WeekdayCalendar(), 1)])
        schedule = chain.latest_schedule(date(2026, 11, 16))
        self.assertIn('handover=2026-11-15', repr(schedule.steps[0]))
        self.assertIn('submission=2026-11-15', repr(schedule))
        self.assertEqual(
            schedule.as_dict()['submission_date'], date(2026, 11, 15)
        )
        self.assertEqual(
            schedule.steps[0].as_dict()['working_days_count'], 1
        )
