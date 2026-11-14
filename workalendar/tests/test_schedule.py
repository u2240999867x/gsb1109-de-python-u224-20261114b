from datetime import date, datetime, timedelta
from unittest import TestCase

from ..europe import France
from ..core import WesternCalendar
from ..exceptions import (
    NoWorkingDayInPeriodError, UnknownPeriodError,
)
from ..schedule import (
    RESTRICTED, UNRESTRICTED, PeriodScheduler, RunSchedule, ScheduledRun,
    build_product_schedules, build_uid, month_periods,
)


class ControlledCalendar(WesternCalendar):
    """Western calendar with a few deterministic month-end holidays."""
    FIXED_HOLIDAYS = WesternCalendar.FIXED_HOLIDAYS + (
        (7, 31, "Month-end collection holiday"),
        (10, 31, "Autumn shutdown"),
    )


def whole_month_holidays(year, month):
    from calendar import monthrange
    last = monthrange(year, month)[1]
    first = date(year, month, 1)
    return [first + timedelta(days=i) for i in range(last)]


class AnchorDateTest(TestCase):

    def setUp(self):
        self.scheduler = PeriodScheduler(ControlledCalendar())

    def test_default_anchor_is_month_end(self):
        self.assertEqual(
            self.scheduler.anchor_date(2024, 2), date(2024, 2, 29)
        )
        self.assertEqual(
            self.scheduler.anchor_date(2024, 4), date(2024, 4, 30)
        )

    def test_anchor_day_is_clamped_to_month_length(self):
        scheduler = PeriodScheduler(ControlledCalendar(), anchor_day=31)
        self.assertEqual(scheduler.anchor_date(2024, 2), date(2024, 2, 29))
        self.assertEqual(scheduler.anchor_date(2024, 7), date(2024, 7, 31))

    def test_invalid_policy(self):
        with self.assertRaises(ValueError):
            PeriodScheduler(ControlledCalendar(), policy="nonsense")


class RestrictedPolicyTest(TestCase):

    def setUp(self):
        self.scheduler = PeriodScheduler(ControlledCalendar())

    def test_working_anchor_runs_same_day(self):
        run = self.scheduler.schedule_period(2024, 5, product="loan")
        self.assertIsInstance(run, ScheduledRun)
        self.assertEqual(run.anchor_date, date(2024, 5, 31))
        self.assertEqual(run.run_date, date(2024, 5, 31))
        self.assertFalse(run.carried_over)
        self.assertEqual(run.period, (2024, 5))
        self.assertEqual(run.uid, "loan:2024-05")

    def test_postpone_within_month_keeps_following_working_day(self):
        # Anchor on Sunday the 28th, next working day (29th) is still in July.
        scheduler = PeriodScheduler(ControlledCalendar(), anchor_day=28)
        run = scheduler.schedule_period(2024, 7)
        self.assertEqual(run.anchor_date, date(2024, 7, 28))
        self.assertEqual(run.run_date, date(2024, 7, 29))
        self.assertTrue(run.carried_over)

    def test_weekend_month_end_falls_back_to_last_working_day(self):
        # 2024-08-31 is Saturday; Monday would cross into September.
        run = self.scheduler.schedule_period(2024, 8)
        self.assertEqual(run.anchor_date, date(2024, 8, 31))
        self.assertEqual(run.run_date, date(2024, 8, 30))
        # The run never leaves its nominal month.
        self.assertEqual(
            (run.run_date.year, run.run_date.month), (2024, 8)
        )

    def test_holiday_month_end_falls_back_to_last_working_day(self):
        # 2024-07-31 is a Wednesday but a holiday; next working day is in
        # August, so the restricted policy falls back to July 30th.
        run = self.scheduler.schedule_period(2024, 7)
        self.assertEqual(run.run_date, date(2024, 7, 30))

    def test_no_working_day_in_month_raises_explicit_error(self):
        # Every day of February 2024 is marked as a holiday. The fallback
        # must report "no solution" rather than quietly using a January slot.
        extra_holidays = whole_month_holidays(2024, 2)
        scheduler = PeriodScheduler(
            ControlledCalendar(), extra_holidays=extra_holidays
        )
        with self.assertRaises(NoWorkingDayInPeriodError) as cm:
            scheduler.schedule_period(2024, 2, product="loan")
        error = cm.exception
        self.assertEqual(error.year, 2024)
        self.assertEqual(error.month, 2)
        self.assertEqual(error.product, "loan")
        self.assertIn("2024-02", str(error))
        self.assertIn("loan", str(error))

    def test_no_silent_takeover_of_previous_month(self):
        # Even with a free slot late in January, the February run must not
        # be scheduled onto a January working day.
        extra_holidays = whole_month_holidays(2024, 2)
        scheduler = PeriodScheduler(
            ControlledCalendar(), extra_holidays=extra_holidays
        )
        with self.assertRaises(NoWorkingDayInPeriodError):
            scheduler.schedule_period(2024, 2)


class UnrestrictedPolicyTest(TestCase):

    def test_postpones_unconditionally_across_month(self):
        scheduler = PeriodScheduler(ControlledCalendar(), policy=UNRESTRICTED)
        # 2024-07-31 is a holiday; unconditional postponement goes to Aug 1st.
        run = scheduler.schedule_period(2024, 7, product="premium")
        self.assertEqual(run.run_date, date(2024, 8, 1))
        self.assertEqual(run.period, (2024, 7))
        self.assertEqual(run.policy, UNRESTRICTED)
        self.assertTrue(run.carried_over)

    def test_weekend_postpones_into_next_month(self):
        scheduler = PeriodScheduler(ControlledCalendar(), policy=UNRESTRICTED)
        run = scheduler.schedule_period(2024, 8)
        self.assertEqual(run.run_date, date(2024, 9, 2))

    def test_crosses_a_fully_closed_month(self):
        # An unrestricted product may roll over a month with no working day.
        extra_holidays = whole_month_holidays(2024, 2) + [date(2024, 1, 31)]
        scheduler = PeriodScheduler(
            ControlledCalendar(),
            policy=UNRESTRICTED,
            extra_holidays=extra_holidays,
        )
        january = scheduler.schedule_period(2024, 1)
        february = scheduler.schedule_period(2024, 2)
        # Both periods land on the first working day of March.
        self.assertEqual(january.run_date, date(2024, 3, 1))
        self.assertEqual(february.run_date, date(2024, 3, 1))
        # Yet they keep distinct, period-bound identities.
        self.assertNotEqual(january.uid, february.uid)
        self.assertEqual(january.period, (2024, 1))
        self.assertEqual(february.period, (2024, 2))


class RunScheduleTest(TestCase):

    def _build(self, policy=RESTRICTED, extra_holidays=None):
        scheduler = PeriodScheduler(
            ControlledCalendar(),
            policy=policy,
            extra_holidays=extra_holidays,
        )
        return scheduler.build_schedule(
            "premium", list(month_periods((2024, 1), (2024, 3)))
        )

    def test_schedule_keeps_order_and_identity(self):
        schedule = self._build()
        self.assertEqual(len(schedule), 3)
        self.assertEqual(
            schedule.periods, ((2024, 1), (2024, 2), (2024, 3))
        )
        self.assertEqual(
            [run.uid for run in schedule],
            ["premium:2024-01", "premium:2024-02", "premium:2024-03"],
        )
        self.assertIn((2024, 2), schedule)
        self.assertNotIn((2024, 9), schedule)

    def test_duplicate_periods_are_rejected(self):
        run = ScheduledRun(
            product="p", period=(2024, 1),
            anchor_date=date(2024, 1, 31), run_date=date(2024, 1, 31),
            policy=RESTRICTED, uid=build_uid("p", (2024, 1)),
        )
        with self.assertRaises(ValueError):
            RunSchedule("p", [run, run])

    def test_colliding_periods_keep_separate_task_identities(self):
        extra_holidays = whole_month_holidays(2024, 2) + [date(2024, 1, 31)]
        schedule = self._build(
            policy=UNRESTRICTED, extra_holidays=extra_holidays
        )
        collision = schedule.on(date(2024, 3, 1))
        self.assertEqual(len(collision), 2)
        self.assertEqual(
            {run.period for run in collision},
            {(2024, 1), (2024, 2)},
        )
        # datetime inputs are normalized to dates.
        self.assertEqual(
            len(schedule.on(datetime(2024, 3, 1, 10, 0))), 2
        )

    def test_resend_targets_only_the_specified_period(self):
        extra_holidays = whole_month_holidays(2024, 2) + [date(2024, 1, 31)]
        schedule = self._build(
            policy=UNRESTRICTED, extra_holidays=extra_holidays
        )
        resent = schedule.resend((2024, 2))
        self.assertEqual(resent.period, (2024, 2))
        self.assertEqual(resent.run_date, date(2024, 3, 1))
        # The other period sharing that execution day is not selected.
        self.assertIsNot(resent, schedule.resend((2024, 1)))

    def test_resend_unknown_period_raises(self):
        schedule = self._build()
        with self.assertRaises(UnknownPeriodError) as cm:
            schedule.resend((2030, 12))
        self.assertEqual(cm.exception.period, (2030, 12))
        self.assertEqual(cm.exception.product, "premium")

    def test_as_dicts(self):
        schedule = self._build()
        rows = schedule.as_dicts()
        self.assertEqual(rows[0]["product"], "premium")
        self.assertEqual(rows[0]["period"], (2024, 1))
        self.assertEqual(rows[0]["run_date"], date(2024, 1, 31))


class MultiProductTest(TestCase):

    def test_each_product_uses_its_own_policy(self):
        calendar = ControlledCalendar()
        periods = [(2024, 7), (2024, 8)]
        schedules = build_product_schedules(
            calendar,
            {"standard": RESTRICTED, "premium": UNRESTRICTED},
            periods,
        )
        self.assertEqual(
            schedules["standard"].get((2024, 7)).run_date,
            date(2024, 7, 30),
        )
        self.assertEqual(
            schedules["premium"].get((2024, 7)).run_date,
            date(2024, 8, 1),
        )
        self.assertEqual(
            schedules["standard"].get((2024, 8)).run_date,
            date(2024, 8, 30),
        )
        self.assertEqual(
            schedules["premium"].get((2024, 8)).run_date,
            date(2024, 9, 2),
        )


class MonthPeriodsTest(TestCase):

    def test_range_within_year(self):
        self.assertEqual(
            list(month_periods((2024, 1), (2024, 4))),
            [(2024, 1), (2024, 2), (2024, 3), (2024, 4)],
        )

    def test_range_across_year_boundary(self):
        self.assertEqual(
            list(month_periods((2023, 11), (2024, 2))),
            [(2023, 11), (2023, 12), (2024, 1), (2024, 2)],
        )

    def test_accepts_dates(self):
        self.assertEqual(
            list(month_periods(date(2024, 1, 15), date(2024, 2, 20))),
            [(2024, 1), (2024, 2)],
        )

    def test_invalid_range(self):
        with self.assertRaises(ValueError):
            list(month_periods((2024, 3), (2024, 1)))


class FranceCalendarIntegrationTest(TestCase):

    def test_long_weekend_month_end(self):
        calendar = France()
        # March 31st 2024 is a Sunday.
        restricted = PeriodScheduler(calendar, RESTRICTED)
        unrestricted = PeriodScheduler(calendar, UNRESTRICTED)
        self.assertEqual(
            restricted.schedule_period(2024, 3).run_date,
            date(2024, 3, 29),
        )
        # April 1st is Easter Monday (holiday in France), roll to April 2nd.
        self.assertEqual(
            unrestricted.schedule_period(2024, 3).run_date,
            date(2024, 4, 2),
        )
