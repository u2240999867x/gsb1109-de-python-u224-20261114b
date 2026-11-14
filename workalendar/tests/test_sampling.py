from datetime import date
from unittest import TestCase

from ..core import MON, WesternCalendar
from ..sampling import (
    BaselineRule,
    DayZeroPolicy,
    FifthMondayPolicy,
    OffsetRule,
    SamplingPlan,
    SamplingRule,
    SamplingSchedule,
    SamplingStatus,
    SamplingTask,
    SamplingWindow,
    merge_tasks,
)


class SamplingCalendar(WesternCalendar):
    """A predictable calendar with one holiday that can be a fifth Monday."""

    FIXED_HOLIDAYS = (
        (5, 29, "Late May holiday"),
    )


class SamplingPlanTest(TestCase):

    def setUp(self):
        super().setUp()
        self.calendar = SamplingCalendar()

    def plan(self, **kwargs):
        kwargs.setdefault("contract_id", "contract-1")
        return SamplingPlan(self.calendar, **kwargs)

    # Baseline step

    def test_missing_fifth_monday_is_skipped(self):
        plan = self.plan(missing_policy=FifthMondayPolicy.SKIP)
        task = plan.evaluate_month(2021, 4)

        self.assertEqual(task.status, SamplingStatus.BASELINE_MISSING)
        self.assertTrue(task.baseline_missing)
        self.assertIsNone(task.baseline)
        self.assertIsNone(task.baseline_kind)
        self.assertIsNone(task.date)
        self.assertFalse(task.is_valid)
        self.assertFalse(task.used_fallback)

    def test_missing_fifth_monday_uses_last_monday(self):
        plan = self.plan(missing_policy=FifthMondayPolicy.LAST_MONDAY)
        task = plan.evaluate_month(2021, 4)

        self.assertEqual(task.status, SamplingStatus.OK)
        self.assertEqual(task.baseline, date(2021, 4, 26))
        self.assertEqual(task.baseline_kind, "last_monday")
        self.assertEqual(task.date, date(2021, 4, 28))
        self.assertTrue(task.used_fallback)
        self.assertTrue(task.is_valid)

    def test_fifth_monday_is_preferred_over_last_monday(self):
        plan = self.plan(missing_policy=FifthMondayPolicy.LAST_MONDAY)
        task = plan.evaluate_month(2023, 5)

        self.assertEqual(task.baseline, date(2023, 5, 29))
        self.assertEqual(task.baseline_kind, "fifth_monday")
        self.assertFalse(task.used_fallback)

    # Offset step

    def test_second_business_day_after_fifth_monday(self):
        plan = self.plan()
        task = plan.evaluate_month(2023, 5)

        self.assertEqual(task.baseline, date(2023, 5, 29))
        self.assertEqual(task.date, date(2023, 5, 31))
        self.assertEqual(task.status, SamplingStatus.OK)
        self.assertFalse(task.rolled_over_month)

    def test_rollover_after_offset_is_flagged(self):
        plan = self.plan()
        task = plan.evaluate_month(2021, 5)

        self.assertEqual(task.baseline, date(2021, 5, 31))
        self.assertEqual(task.date, date(2021, 6, 2))
        self.assertEqual(task.status, SamplingStatus.ROLLED_OVER_MONTH)
        self.assertTrue(task.rolled_over_month)
        self.assertTrue(task.crosses_month)
        self.assertTrue(task.is_valid)

    def test_day_zero_includes_holiday_baseline(self):
        plan = self.plan(day_zero_policy=DayZeroPolicy.INCLUDE)
        task = plan.evaluate_month(2023, 5)

        self.assertEqual(task.baseline, date(2023, 5, 29))
        self.assertTrue(self.calendar.is_holiday(task.baseline))
        self.assertEqual(task.date, date(2023, 5, 31))
        self.assertEqual(task.status, SamplingStatus.OK)

    def test_day_zero_can_exclude_holiday_baseline(self):
        plan = self.plan(day_zero_policy=DayZeroPolicy.EXCLUDE)
        task = plan.evaluate_month(2023, 5)

        self.assertEqual(task.baseline, date(2023, 5, 29))
        self.assertTrue(self.calendar.is_holiday(task.baseline))
        self.assertEqual(task.date, date(2023, 6, 1))
        self.assertEqual(task.status, SamplingStatus.ROLLED_OVER_MONTH)

    def test_day_zero_has_no_effect_on_business_baseline(self):
        include = self.plan(day_zero_policy=DayZeroPolicy.INCLUDE)
        exclude = self.plan(day_zero_policy=DayZeroPolicy.EXCLUDE)

        self.assertEqual(
            include.evaluate_month(2021, 4).date,
            exclude.evaluate_month(2021, 4).date,
        )

    def test_offset_can_be_configured(self):
        plan = self.plan(offset=1)
        self.assertEqual(
            plan.evaluate_month(2023, 5).date, date(2023, 5, 30)
        )

    # Excluded windows

    def test_whole_window_excluded(self):
        plan = self.plan(
            excluded_windows=[
                SamplingWindow(date(2021, 5, 1), date(2021, 5, 31))
            ]
        )
        task = plan.evaluate_month(2021, 5)

        self.assertEqual(task.status, SamplingStatus.WINDOW_EXCLUDED)
        self.assertTrue(task.window_excluded)
        self.assertTrue(task.excluded)
        self.assertIsNone(task.date)
        self.assertFalse(task.is_valid)

    def test_whole_window_excluded_by_active_period(self):
        plan = self.plan(active_from=date(2021, 6, 1))
        task = plan.evaluate_month(2021, 5)

        self.assertEqual(task.status, SamplingStatus.WINDOW_EXCLUDED)
        self.assertTrue(task.window_excluded)

    def test_whole_window_excluded_by_excluded_month(self):
        plan = self.plan(excluded_months=["2021-05"])
        task = plan.evaluate_month(2021, 5)

        self.assertEqual(task.status, SamplingStatus.WINDOW_EXCLUDED)

    def test_partial_window_exclusion_does_not_remove_task(self):
        plan = self.plan(
            excluded_windows=[
                SamplingWindow(date(2021, 5, 1), date(2021, 5, 10))
            ]
        )
        task = plan.evaluate_month(2021, 5)

        self.assertEqual(task.status, SamplingStatus.ROLLED_OVER_MONTH)
        self.assertEqual(task.date, date(2021, 6, 2))

    def test_excluded_target_is_not_scheduled(self):
        plan = self.plan(
            excluded_dates=[date(2021, 6, 2)]
        )
        task = plan.evaluate_month(2021, 5)

        self.assertEqual(task.status, SamplingStatus.WINDOW_EXCLUDED)
        self.assertEqual(task.target_date, date(2021, 6, 2))
        self.assertIsNone(task.date)

    # Quarter/month parity

    def test_quarter_and_month_queries_are_identical(self):
        plans = (
            self.plan(missing_policy=FifthMondayPolicy.SKIP),
            self.plan(missing_policy=FifthMondayPolicy.LAST_MONDAY),
            self.plan(
                missing_policy=FifthMondayPolicy.LAST_MONDAY,
                day_zero_policy=DayZeroPolicy.EXCLUDE,
            ),
        )
        for plan in plans:
            for year in (2020, 2021, 2023, 2024, 2025, 2026):
                for quarter in range(1, 5):
                    with self.subTest(
                        year=year, quarter=quarter, rule=plan.rule.signature
                    ):
                        quarter_tasks = plan.tasks_for_quarter(year, quarter)
                        month_tasks = plan.merge_tasks([
                            plan.tasks_for_month(year, month)
                            for month in range(
                                (quarter - 1) * 3 + 1,
                                (quarter - 1) * 3 + 4,
                            )
                        ])
                        self.assertEqual(quarter_tasks, month_tasks)
                        self.assertEqual(
                            [task.task_id for task in quarter_tasks],
                            [task.task_id for task in month_tasks],
                        )
                        self.assertEqual(
                            [task.date for task in quarter_tasks],
                            [task.date for task in month_tasks],
                        )

    def test_quarter_accepts_strings(self):
        plan = self.plan()
        self.assertEqual(
            plan.tasks_for_quarter("2021-Q2"),
            plan.tasks_for_quarter(2021, 2),
        )
        self.assertEqual(
            plan.tasks_for_month("2021-05"),
            plan.tasks_for_month(2021, 5),
        )

    def test_task_identity_is_stable_across_queries(self):
        plan = self.plan()
        monthly = plan.tasks_for_month(2021, 5)[0]
        quarterly = plan.tasks_for_quarter(2021, 2)
        yearly = plan.tasks_for_year(2021)

        self.assertIn(monthly, quarterly)
        self.assertIn(monthly, yearly)
        self.assertEqual(
            monthly.task_id,
            plan.evaluate_month(2021, 5).task_id,
        )
        # Identity does not include the query range.
        self.assertNotIn("quarter", monthly.task_id)
        self.assertNotIn("year", monthly.task_id)

    def test_records_for_quarter_include_rejections(self):
        plan = self.plan()
        records = plan.evaluate_quarter(2021, 2)

        self.assertEqual(len(records), 3)
        self.assertEqual(
            [record.period for record in records],
            [(2021, 4), (2021, 5), (2021, 6)],
        )
        self.assertEqual(
            [record.status for record in records],
            [
                SamplingStatus.BASELINE_MISSING,
                SamplingStatus.ROLLED_OVER_MONTH,
                SamplingStatus.BASELINE_MISSING,
            ],
        )

    def test_merge_deduplicates_by_task_identity(self):
        plan = self.plan()
        task = plan.tasks_for_month(2021, 5)[0]
        merged = merge_tasks([task, [task], (task,)])
        self.assertEqual(merged, [task])

    def test_merge_sorts_by_date_then_identity(self):
        plan = self.plan()
        tasks = (
            plan.tasks_for_month(2023, 5)
            + plan.tasks_for_month(2021, 5)
            + plan.tasks_for_month(2021, 4)
        )
        merged = merge_tasks(tasks)
        self.assertEqual(
            [task.date for task in merged],
            sorted(task.date for task in merged),
        )

    # Inspection helpers

    def test_record_is_inspectable(self):
        plan = self.plan()
        task = plan.evaluate_month(2021, 5)
        payload = task.as_dict()

        self.assertIsInstance(task, SamplingTask)
        self.assertEqual(payload["status"], "rolled_over_month")
        self.assertEqual(payload["date"], date(2021, 6, 2))
        self.assertEqual(payload["baseline"], date(2021, 5, 31))
        self.assertTrue(payload["rolled_over_month"])
        self.assertIn("rule", payload)
        self.assertIn("crossed", task.explain())
        self.assertEqual(
            task.statuses,
            (SamplingStatus.ROLLED_OVER_MONTH,),
        )

    def test_baseline_rule_and_offset_rule_are_separate(self):
        rule = SamplingRule(
            baseline_rule=BaselineRule(
                ordinal=5,
                weekday=MON,
                missing_policy=FifthMondayPolicy.LAST_MONDAY,
            ),
            offset_rule=OffsetRule(
                business_days=2,
                day_zero_policy=DayZeroPolicy.EXCLUDE,
            ),
        )
        self.assertEqual(rule.offset, 2)
        self.assertEqual(
            rule.missing_policy, FifthMondayPolicy.LAST_MONDAY
        )
        self.assertEqual(
            rule.day_zero_policy, DayZeroPolicy.EXCLUDE
        )
        self.assertIn("last_monday", rule.signature)
        self.assertIn("exclude", rule.signature)
        self.assertIn("5th Monday", rule.explain())
        inspection = rule.inspect()
        self.assertEqual(
            inspection["baseline"]["missing_policy"], "last_monday"
        )
        self.assertEqual(
            inspection["offset"]["day_zero_policy"], "exclude"
        )

    def test_schedule_merges_contracts(self):
        first = self.plan(contract_id="first")
        second = self.plan(
            contract_id="second",
            missing_policy=FifthMondayPolicy.LAST_MONDAY,
        )
        schedule = SamplingSchedule([first, second])
        tasks = schedule.tasks_for_quarter(2021, 2)

        self.assertEqual(
            [task.contract_id for task in tasks],
            ["second", "first", "second", "second"],
        )
        self.assertEqual(
            len({task.task_id for task in tasks}), len(tasks)
        )

    def test_calendar_can_be_passed_as_class(self):
        plan = SamplingPlan(SamplingCalendar, contract_id="class")
        self.assertEqual(
            plan.tasks_for_month(2023, 5)[0].date,
            date(2023, 5, 31),
        )

    def test_rule_accepts_string_policies(self):
        plan = self.plan(
            missing_policy="last_monday",
            day_zero_policy="exclude",
        )
        self.assertEqual(
            plan.rule.missing_policy, FifthMondayPolicy.LAST_MONDAY
        )
        self.assertEqual(
            plan.rule.day_zero_policy, DayZeroPolicy.EXCLUDE
        )

    def test_invalid_policy_raises(self):
        with self.assertRaises(ValueError):
            SamplingRule(missing_policy="unknown")
        with self.assertRaises(ValueError):
            SamplingRule(day_zero_policy="unknown")
        with self.assertRaises(ValueError):
            SamplingPlan(self.calendar, offset=-1)
