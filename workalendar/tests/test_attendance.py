"""
Tests for shift attendance accounting (calendar + time-zone settlement).
"""
from datetime import date, datetime, timedelta, timezone
from unittest import TestCase
from zoneinfo import ZoneInfo

from ..attendance import (
    SEGMENTED, SHIFT_START, Shift, local_candidates,
    settle_shift, settle_shift_candidates,
)
from ..core import FRI, MON, SAT, SUN, THU, TUE, WED, Calendar
from ..exceptions import AmbiguousPunchError, InvalidShiftError
from ..usa.core import UnitedStates

NEW_YORK = "America/New_York"
BERLIN = "Europe/Berlin"
SHANGHAI = "Asia/Shanghai"


class DemoCalendar(Calendar):
    """Mon-Fri working week with one fixed holiday on November 13th."""
    WEEKEND_DAYS = (SAT, SUN)
    FIXED_HOLIDAYS = ((11, 13, "Test Holiday"),)


class LocalCandidatesTest(TestCase):

    def setUp(self):
        self.tz_cal = (NEW_YORK, DemoCalendar())

    def test_unambiguous_punch_has_single_candidate(self):
        tz, _ = self.tz_cal
        candidates = local_candidates(datetime(2026, 11, 2, 10, 0), tz)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].utcoffset(), timedelta(hours=-5))

    def test_fall_back_punch_has_two_ordered_candidates(self):
        tz, _ = self.tz_cal
        candidates = local_candidates(datetime(2026, 11, 1, 1, 30), tz)
        self.assertEqual(len(candidates), 2)
        first, second = candidates
        self.assertEqual(first.utcoffset(), timedelta(hours=-4))
        self.assertEqual(second.utcoffset(), timedelta(hours=-5))
        # Same tzinfo comparison ignores fold, so order on the UTC line.
        self.assertLess(
            first.astimezone(timezone.utc),
            second.astimezone(timezone.utc),
        )

    def test_spring_forward_gap_is_rejected(self):
        tz, _ = self.tz_cal
        with self.assertRaises(InvalidShiftError):
            local_candidates(datetime(2026, 3, 8, 2, 30), tz)

    def test_naive_datetime_required(self):
        tz, _ = self.tz_cal
        with self.assertRaises(InvalidShiftError):
            local_candidates(
                datetime(2026, 11, 1, 1, 30,
                         tzinfo=ZoneInfo(tz)),
                tz,
            )


class BasicSettlementTest(TestCase):

    def setUp(self):
        self.calendar = DemoCalendar()

    def test_ten_pm_to_six_am_is_eight_hours_split_at_midnight(self):
        # Tuesday 22:00 -> Wednesday 06:00, both ordinary working days.
        settlement = settle_shift(
            datetime(2026, 11, 10, 22, 0),
            datetime(2026, 11, 11, 6, 0),
            NEW_YORK, self.calendar, contract=SEGMENTED,
        )
        self.assertEqual(settlement.total, timedelta(hours=8))
        self.assertEqual(settlement.working_total, timedelta(hours=8))
        self.assertEqual(settlement.holiday_total, timedelta())
        self.assertEqual(
            sorted(settlement.bookings),
            [date(2026, 11, 10), date(2026, 11, 11)],
        )
        self.assertEqual(
            settlement.bookings[date(2026, 11, 10)].working,
            timedelta(hours=2),
        )
        self.assertEqual(
            settlement.bookings[date(2026, 11, 11)].working,
            timedelta(hours=6),
        )
        self.assertEqual(
            len(settlement.segments), 2
        )

    def test_segments_sum_back_to_actual_duration(self):
        settlement = settle_shift(
            datetime(2026, 11, 10, 22, 0),
            datetime(2026, 11, 11, 6, 0),
            NEW_YORK, self.calendar,
        )
        self.assertEqual(
            sum((s.duration for s in settlement.segments), timedelta()),
            settlement.total,
        )

    def test_shift_start_contract_books_whole_shift_on_clock_in_day(self):
        settlement = settle_shift(
            datetime(2026, 11, 10, 22, 0),
            datetime(2026, 11, 11, 6, 0),
            NEW_YORK, self.calendar, contract=SHIFT_START,
        )
        self.assertEqual(
            list(settlement.bookings), [date(2026, 11, 10)]
        )
        self.assertEqual(
            settlement.bookings[date(2026, 11, 10)].working,
            timedelta(hours=8),
        )
        self.assertEqual(settlement.total, timedelta(hours=8))

    def test_segmented_overnight_into_holiday(self):
        # Thursday evening -> Friday November 13th (fixed holiday).
        settlement = settle_shift(
            datetime(2026, 11, 12, 22, 0),
            datetime(2026, 11, 13, 6, 0),
            NEW_YORK,
            DemoCalendar(),
        )
        self.assertEqual(
            settlement.bookings[date(2026, 11, 12)].working,
            timedelta(hours=2),
        )
        self.assertEqual(
            settlement.bookings[date(2026, 11, 13)].holiday,
            timedelta(hours=6),
        )
        self.assertEqual(settlement.working_total, timedelta(hours=2))
        self.assertEqual(settlement.holiday_total, timedelta(hours=6))

    def test_segmented_holiday_evening_into_working_morning(self):
        # Friday holiday 22:00 -> Saturday 06:00 (weekend, also non-working).
        settlement = settle_shift(
            datetime(2026, 11, 13, 22, 0),
            datetime(2026, 11, 14, 6, 0),
            NEW_YORK,
            DemoCalendar(),
        )
        self.assertEqual(settlement.working_total, timedelta())
        self.assertEqual(settlement.holiday_total, timedelta(hours=8))
        self.assertFalse(
            settlement.segments[0].is_working_day
        )

    def test_shift_start_into_holiday_stays_on_start_day(self):
        settlement = settle_shift(
            datetime(2026, 11, 12, 22, 0),
            datetime(2026, 11, 13, 6, 0),
            NEW_YORK, DemoCalendar(), contract=SHIFT_START,
        )
        # Thursday is a working day: the whole shift counts as working,
        # even though most of it physically runs on the holiday.
        self.assertEqual(settlement.working_total, timedelta(hours=8))
        self.assertEqual(settlement.holiday_total, timedelta())

    def test_day_shift_does_not_split(self):
        settlement = settle_shift(
            datetime(2026, 11, 12, 9, 0),
            datetime(2026, 11, 12, 17, 0),
            NEW_YORK, self.calendar,
        )
        self.assertEqual(len(settlement.segments), 1)
        self.assertEqual(settlement.total, timedelta(hours=8))
        self.assertEqual(settlement.segments[0].day, date(2026, 11, 12))

    def test_timezone_without_dst(self):
        settlement = settle_shift(
            datetime(2026, 11, 10, 22, 0),
            datetime(2026, 11, 11, 6, 0),
            SHANGHAI, self.calendar,
        )
        self.assertEqual(settlement.total, timedelta(hours=8))
        self.assertEqual(
            settlement.start.utcoffset(), timedelta(hours=8)
        )

    def test_real_united_states_calendar(self):
        calendar = UnitedStates()
        # Veterans Day 2026-11-11 is a Wednesday federal holiday.
        self.assertFalse(calendar.is_working_day(date(2026, 11, 11)))
        settlement = settle_shift(
            datetime(2026, 11, 10, 22, 0),
            datetime(2026, 11, 11, 6, 0),
            NEW_YORK, calendar,
        )
        self.assertEqual(settlement.working_total, timedelta(hours=2))
        self.assertEqual(settlement.holiday_total, timedelta(hours=6))


class FallBackTest(TestCase):

    def setUp(self):
        self.calendar = DemoCalendar()

    def test_night_shift_through_fall_back_lasts_nine_hours(self):
        # Saturday 22:00 EDT -> Sunday 06:00 EST, the repeated hour
        # makes the actual elapsed time nine hours, not eight.
        settlement = settle_shift(
            datetime(2026, 10, 31, 22, 0),
            datetime(2026, 11, 1, 6, 0),
            NEW_YORK, self.calendar,
        )
        self.assertEqual(settlement.total, timedelta(hours=9))
        by_day = {segment.day: segment.duration
                  for segment in settlement.segments}
        self.assertEqual(by_day[date(2026, 10, 31)], timedelta(hours=2))
        self.assertEqual(by_day[date(2026, 11, 1)], timedelta(hours=7))
        self.assertEqual(
            sum(by_day.values(), timedelta()), timedelta(hours=9)
        )

    def test_ambiguous_punches_raise_with_candidates(self):
        shift = Shift(
            datetime(2026, 11, 1, 1, 15),
            datetime(2026, 11, 1, 1, 45),
            NEW_YORK,
        )
        self.assertTrue(shift.is_ambiguous)
        with self.assertRaises(AmbiguousPunchError) as caught:
            settle_shift(
                datetime(2026, 11, 1, 1, 15),
                datetime(2026, 11, 1, 1, 45),
                NEW_YORK, self.calendar,
            )
        self.assertEqual(len(caught.exception.candidates), 2)
        offsets = {candidate.utcoffset()
                   for candidate in caught.exception.candidates}
        self.assertEqual(
            offsets, {timedelta(hours=-4), timedelta(hours=-5)}
        )

    def test_ambiguous_settlements_are_enumerated(self):
        settlements = settle_shift_candidates(
            datetime(2026, 11, 1, 1, 15),
            datetime(2026, 11, 1, 1, 45),
            NEW_YORK, self.calendar,
        )
        # 2 x 2 combinations, minus the one where the second clock-in
        # occurrence is later than the first clock-out occurrence.
        self.assertEqual(len(settlements), 3)
        durations = sorted(settlement.total for settlement in settlements)
        self.assertEqual(
            durations,
            [timedelta(minutes=30), timedelta(minutes=30),
             timedelta(minutes=90)],
        )
        for settlement in settlements:
            self.assertEqual(
                sum((s.duration for s in settlement.segments),
                    timedelta()),
                settlement.total,
            )

    def test_ambiguous_candidate_contract_is_honoured(self):
        settlements = settle_shift_candidates(
            datetime(2026, 10, 31, 23, 30),
            datetime(2026, 11, 1, 1, 30),
            NEW_YORK, self.calendar, contract=SHIFT_START,
        )
        self.assertTrue(settlements)
        for settlement in settlements:
            self.assertEqual(
                list(settlement.bookings), [date(2026, 10, 31)]
            )

    def test_european_fall_back_ambiguity(self):
        # Europe/Berlin falls back on 2026-10-25, 03:00 -> 02:00.
        shift = Shift(
            datetime(2026, 10, 25, 2, 30),
            datetime(2026, 10, 25, 3, 30),
            BERLIN,
        )
        self.assertTrue(shift.is_ambiguous)
        settlements = shift.candidates(self.calendar)
        self.assertEqual(len(settlements), 2)
        self.assertEqual(
            sorted(s.total for s in settlements),
            [timedelta(hours=1), timedelta(hours=2)],
        )

    def test_unambiguous_shift_candidates_return_single_settlement(self):
        settlements = settle_shift_candidates(
            datetime(2026, 11, 10, 22, 0),
            datetime(2026, 11, 11, 6, 0),
            NEW_YORK, self.calendar,
        )
        self.assertEqual(len(settlements), 1)


class InvalidInputTest(TestCase):

    def setUp(self):
        self.calendar = DemoCalendar()

    def test_clock_out_before_clock_in_is_rejected(self):
        with self.assertRaises(InvalidShiftError):
            Shift(
                datetime(2026, 11, 12, 10, 0),
                datetime(2026, 11, 12, 9, 0),
                NEW_YORK,
            )

    def test_aware_datetime_rejected(self):
        from zoneinfo import ZoneInfo
        with self.assertRaises(InvalidShiftError):
            Shift(
                datetime(2026, 11, 12, 9, 0, tzinfo=ZoneInfo(NEW_YORK)),
                datetime(2026, 11, 12, 17, 0),
                NEW_YORK,
            )

    def test_bad_type_rejected(self):
        with self.assertRaises(InvalidShiftError):
            Shift(date(2026, 11, 12), datetime(2026, 11, 12, 17),
                  NEW_YORK)

    def test_bad_time_zone_rejected(self):
        with self.assertRaises(InvalidShiftError):
            Shift(
                datetime(2026, 11, 12, 9, 0),
                datetime(2026, 11, 12, 17, 0),
                1234,
            )

    def test_unknown_contract_rejected(self):
        shift = Shift(
            datetime(2026, 11, 12, 9, 0),
            datetime(2026, 11, 12, 17, 0),
            NEW_YORK,
        )
        with self.assertRaises(InvalidShiftError):
            shift.candidates(self.calendar, contract="monthly")

    def test_spring_forward_shift_rejected(self):
        with self.assertRaises(InvalidShiftError):
            settle_shift(
                datetime(2026, 3, 8, 2, 0),
                datetime(2026, 3, 8, 6, 0),
                NEW_YORK, self.calendar,
            )


class ContractConstantsTest(TestCase):

    def test_weekday_constants_used_in_test_calendar(self):
        # Sanity check on the dates used across the tests.
        self.assertEqual(date(2026, 11, 12).weekday(), THU)
        self.assertEqual(date(2026, 11, 13).weekday(), FRI)
        self.assertEqual(date(2026, 11, 1).weekday(), SUN)
        self.assertEqual(date(2026, 11, 14).weekday(), SAT)
        self.assertEqual(date(2026, 11, 10).weekday(), TUE)
        self.assertEqual(date(2026, 11, 11).weekday(), WED)
        self.assertEqual(date(2026, 10, 31).weekday(), SAT)
        self.assertEqual(MON, 0)
