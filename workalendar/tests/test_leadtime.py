from datetime import date, timedelta
from unittest import TestCase

from ..core import WesternCalendar
from ..leadtime import (
    LeadTimeOrder,
    LeadTimeState,
    PausePeriod,
    compute_lead_time,
    elapsed_working_days,
    lead_time_dashboard,
)


class NoHolidayCalendar(WesternCalendar):
    include_new_years_day = False
    include_christmas = False


class LeadTimeTestCase(TestCase):
    def setUp(self):
        self.cal = NoHolidayCalendar()

    def test_elapsed_excludes_weekends(self):
        # Monday to Monday: Tue..Mon = 5 working days.
        self.assertEqual(
            elapsed_working_days(
                date(2024, 1, 1), date(2024, 1, 8), calendar=self.cal),
            5,
        )

    def test_include_start(self):
        self.assertEqual(
            elapsed_working_days(
                date(2024, 1, 1), date(2024, 1, 8),
                calendar=self.cal, include_start=True),
            6,
        )

    def test_elapsed_uses_calendar_holidays(self):
        self.cal = WesternCalendar()
        # The start date is excluded, so the New year holiday does not
        # change the Tue..Mon count of 5 working days.
        self.assertEqual(
            elapsed_working_days(
                date(2024, 1, 1), date(2024, 1, 8), calendar=self.cal),
            5,
        )
        # When the start is included, the New year holiday is skipped.
        self.assertEqual(
            elapsed_working_days(
                date(2024, 1, 1), date(2024, 1, 8),
                calendar=self.cal, include_start=True),
            5,
        )

    def test_single_pause_removes_only_working_days(self):
        # Pause covers Fri 5th to Mon 8th (resume Mon 8th): Fri is removed.
        self.assertEqual(
            elapsed_working_days(
                date(2024, 1, 1), date(2024, 1, 8), calendar=self.cal,
                pauses=[PausePeriod(date(2024, 1, 5), date(2024, 1, 8))]),
            4,
        )

    def test_pause_without_end_is_open_ended(self):
        self.assertEqual(
            elapsed_working_days(
                date(2024, 1, 1), date(2024, 1, 8), calendar=self.cal,
                pauses=[PausePeriod(date(2024, 1, 3))]),
            1,
        )

    def test_pause_across_weekend_only_removes_working_days(self):
        # Pause Fri 5th -> Tue 9th: Fri and Mon are removed.
        self.assertEqual(
            elapsed_working_days(
                date(2024, 1, 1), date(2024, 1, 9), calendar=self.cal,
                pauses=[PausePeriod(date(2024, 1, 5), date(2024, 1, 9))]),
            4,
        )

    def test_overlapping_pauses_are_not_double_counted(self):
        pauses = [
            PausePeriod(date(2024, 1, 2), date(2024, 1, 5)),
            PausePeriod(date(2024, 1, 4), date(2024, 1, 8)),
        ]
        # Tue..Mon is 5 days; paused Tue, Wed, Thu, Fri => 1 day left.
        self.assertEqual(
            elapsed_working_days(
                date(2024, 1, 1), date(2024, 1, 8), calendar=self.cal,
                pauses=pauses),
            1,
        )

    def test_adjacent_pauses_merge(self):
        pauses = [
            PausePeriod(date(2024, 1, 2), date(2024, 1, 4)),
            PausePeriod(date(2024, 1, 4), date(2024, 1, 6)),
        ]
        self.assertEqual(
            elapsed_working_days(
                date(2024, 1, 1), date(2024, 1, 8), calendar=self.cal,
                pauses=pauses),
            1,
        )

    def test_pauses_clipped_to_measurement_range(self):
        pauses = [
            PausePeriod(date(2023, 12, 1), date(2024, 1, 4)),
            PausePeriod(date(2024, 1, 7), date(2024, 2, 1)),
        ]
        # Tuesday 2nd, Wednesday 3rd and Monday 8th are paused.
        self.assertEqual(
            elapsed_working_days(
                date(2024, 1, 1), date(2024, 1, 8), calendar=self.cal,
                pauses=pauses),
            2,
        )

    def test_pause_end_is_a_working_day(self):
        # Tuesday is the resume date and must count.
        self.assertEqual(
            elapsed_working_days(
                date(2024, 1, 1), date(2024, 1, 3), calendar=self.cal,
                pauses=[PausePeriod(date(2024, 1, 2), date(2024, 1, 3))]),
            1,
        )

    def test_reversed_pause_rejected(self):
        with self.assertRaises(ValueError):
            PausePeriod(date(2024, 1, 5), date(2024, 1, 4))

    def test_tuples_and_mappings_are_accepted(self):
        self.assertEqual(
            elapsed_working_days(
                date(2024, 1, 1), date(2024, 1, 8), calendar=self.cal,
                pauses=[(date(2024, 1, 5), date(2024, 1, 8))]),
            4,
        )
        self.assertEqual(
            elapsed_working_days(
                date(2024, 1, 1), date(2024, 1, 8), calendar=self.cal,
                pauses=[{"start": date(2024, 1, 5),
                         "end": date(2024, 1, 8)}]),
            4,
        )


class ComputeLeadTimeTestCase(TestCase):
    def setUp(self):
        self.cal = NoHolidayCalendar()
        self.order = LeadTimeOrder(
            started_on=date(2024, 1, 1),
            sla_working_days=5,
            reference="A-1",
        )

    def test_in_transit_within_limit(self):
        result = compute_lead_time(
            self.order, date(2024, 1, 4), calendar=self.cal)
        self.assertEqual(result.elapsed_working_days, 3)
        self.assertEqual(result.remaining_working_days, 2)
        self.assertFalse(result.overdue)
        self.assertTrue(result.can_still_be_on_time)
        self.assertEqual(result.state, LeadTimeState.IN_TRANSIT)

    def test_exactly_on_limit_is_not_overdue(self):
        result = compute_lead_time(
            self.order, date(2024, 1, 8), calendar=self.cal)
        self.assertEqual(result.elapsed_working_days, 5)
        self.assertFalse(result.overdue)
        self.assertTrue(result.can_still_be_on_time)

    def test_overdue_without_delivery(self):
        result = compute_lead_time(
            self.order, date(2024, 1, 9), calendar=self.cal)
        self.assertEqual(result.elapsed_working_days, 6)
        self.assertTrue(result.overdue)
        self.assertFalse(result.can_still_be_on_time)
        self.assertEqual(result.remaining_working_days, -1)
        self.assertEqual(result.state, LeadTimeState.OVERDUE)

    def test_delivery_freezes_the_clock(self):
        order = LeadTimeOrder(
            started_on=date(2024, 1, 1),
            sla_working_days=5,
            delivered_on=date(2024, 1, 4),
        )
        result = compute_lead_time(order, date(2024, 2, 1),
                                   calendar=self.cal)
        self.assertTrue(result.is_delivered)
        self.assertEqual(result.elapsed_working_days, 3)
        self.assertTrue(result.delivered_on_time)
        self.assertEqual(result.state, LeadTimeState.DELIVERED_ON_TIME)

    def test_late_delivery_is_late(self):
        order = LeadTimeOrder(
            started_on=date(2024, 1, 1),
            sla_working_days=5,
            delivered_on=date(2024, 1, 9),
        )
        result = compute_lead_time(order, date(2024, 2, 1),
                                   calendar=self.cal)
        self.assertTrue(result.overdue)
        self.assertFalse(result.delivered_on_time)
        self.assertEqual(result.state, LeadTimeState.DELIVERED_LATE)

    def test_future_delivery_is_ignored(self):
        order = LeadTimeOrder(
            started_on=date(2024, 1, 1),
            sla_working_days=5,
            delivered_on=date(2024, 1, 30),
        )
        result = compute_lead_time(order, date(2024, 1, 9),
                                   calendar=self.cal)
        self.assertFalse(result.is_delivered)
        self.assertTrue(result.overdue)
        self.assertEqual(result.state, LeadTimeState.OVERDUE)

    def test_future_pause_release_is_ignored(self):
        order = LeadTimeOrder(
            started_on=date(2024, 1, 1),
            sla_working_days=5,
            pauses=[PausePeriod(date(2024, 1, 2), date(2024, 2, 1))],
        )
        # At the 9th the pause is still active: elapsed 0.
        result = compute_lead_time(order, date(2024, 1, 9),
                                   calendar=self.cal)
        self.assertEqual(result.elapsed_working_days, 0)
        self.assertFalse(result.overdue)

        # After the release date the clock resumes normally.
        result = compute_lead_time(order, date(2024, 2, 2),
                                   calendar=self.cal)
        self.assertEqual(result.elapsed_working_days, 2)

    def test_pause_still_running_at_observation(self):
        order = LeadTimeOrder(
            started_on=date(2024, 1, 1),
            sla_working_days=2,
            pauses=[PausePeriod(date(2024, 1, 2))],
        )
        result = compute_lead_time(order, date(2024, 3, 1),
                                   calendar=self.cal)
        self.assertEqual(result.elapsed_working_days, 0)
        self.assertFalse(result.overdue)

    def test_delivery_before_observation_uses_earlier_pause_release(self):
        order = LeadTimeOrder(
            started_on=date(2024, 1, 1),
            sla_working_days=2,
            delivered_on=date(2024, 1, 4),
            pauses=[PausePeriod(date(2024, 1, 2), date(2024, 1, 8))],
        )
        # Delivery occurred while paused, so no working day elapsed.
        result = compute_lead_time(order, date(2024, 2, 1),
                                   calendar=self.cal)
        self.assertEqual(result.elapsed_working_days, 0)
        self.assertTrue(result.delivered_on_time)

    def test_observation_on_weekend_does_not_add_a_working_day(self):
        order = LeadTimeOrder(
            started_on=date(2024, 1, 1),
            sla_working_days=5,
        )
        friday = compute_lead_time(order, date(2024, 1, 5),
                                   calendar=self.cal)
        saturday = compute_lead_time(order, date(2024, 1, 6),
                                     calendar=self.cal)
        sunday = compute_lead_time(order, date(2024, 1, 7),
                                   calendar=self.cal)
        monday = compute_lead_time(order, date(2024, 1, 8),
                                   calendar=self.cal)
        self.assertEqual(friday.elapsed_working_days, 4)
        self.assertEqual(saturday.elapsed_working_days, 4)
        self.assertEqual(sunday.elapsed_working_days, 4)
        self.assertEqual(monday.elapsed_working_days, 5)

    def test_delivery_after_observation_cannot_be_used_early(self):
        order = LeadTimeOrder(
            started_on=date(2024, 1, 1),
            sla_working_days=1,
            delivered_on=date(2024, 1, 8),
        )
        result = compute_lead_time(order, date(2024, 1, 3),
                                   calendar=self.cal)
        self.assertFalse(result.is_delivered)
        self.assertTrue(result.overdue)
        self.assertFalse(result.can_still_be_on_time)

    def test_mapping_order(self):
        result = compute_lead_time(
            {
                "id": "B-2",
                "order_date": date(2024, 1, 1),
                "sla_days": 5,
                "signed_on": date(2024, 1, 4),
                "pauses": [{"from": date(2024, 1, 2),
                            "to": date(2024, 1, 3)}],
            },
            date(2024, 1, 10),
            calendar=self.cal,
        )
        self.assertEqual(result.reference, "B-2")
        self.assertEqual(result.elapsed_working_days, 2)
        self.assertTrue(result.delivered_on_time)


class DashboardTestCase(TestCase):
    def setUp(self):
        self.cal = NoHolidayCalendar()
        self.orders = [
            LeadTimeOrder(
                reference="on-time",
                started_on=date(2024, 1, 1),
                sla_working_days=5,
                delivered_on=date(2024, 1, 4),
            ),
            LeadTimeOrder(
                reference="late",
                started_on=date(2024, 1, 1),
                sla_working_days=2,
                delivered_on=date(2024, 1, 5),
            ),
            LeadTimeOrder(
                reference="overdue",
                started_on=date(2024, 1, 1),
                sla_working_days=1,
            ),
            LeadTimeOrder(
                reference="in-transit",
                started_on=date(2024, 1, 8),
                sla_working_days=5,
            ),
            LeadTimeOrder(
                reference="paused",
                started_on=date(2024, 1, 1),
                sla_working_days=1,
                pauses=[PausePeriod(date(2024, 1, 2))],
            ),
        ]

    def test_dashboard_counts(self):
        dashboard = lead_time_dashboard(
            self.orders, date(2024, 1, 10), calendar=self.cal)
        self.assertEqual(dashboard.total_count, 5)
        self.assertEqual(dashboard.delivered_count, 2)
        self.assertEqual(dashboard.delivered_on_time_count, 1)
        self.assertEqual(dashboard.delivered_late_count, 1)
        self.assertEqual(dashboard.overdue_count, 2)
        self.assertEqual(dashboard.in_transit_count, 3)
        # Overdue results are excluded from "can still be on time".
        self.assertEqual(dashboard.can_still_be_on_time_count, 2)
        self.assertEqual(
            [result.reference for result in dashboard.overdue_orders],
            ["late", "overdue"],
        )
        self.assertEqual(
            [result.reference
             for result in dashboard.can_still_be_on_time_orders],
            ["in-transit", "paused"],
        )

    def test_dashboard_delivery_after_cutoff_is_still_in_transit(self):
        dashboard = lead_time_dashboard(
            self.orders, date(2024, 1, 3), calendar=self.cal)
        states = {
            result.reference: result.state
            for result in dashboard.orders
        }
        self.assertEqual(states["on-time"], LeadTimeState.IN_TRANSIT)
        self.assertEqual(states["late"], LeadTimeState.IN_TRANSIT)
        self.assertFalse(
            dashboard.orders[0].is_delivered)

    def test_dashboard_as_dict(self):
        payload = lead_time_dashboard(
            self.orders, date(2024, 1, 10), calendar=self.cal).as_dict()
        self.assertEqual(payload["as_of"], date(2024, 1, 10))
        self.assertEqual(payload["overdue_count"], 2)
        self.assertEqual(payload["can_still_be_on_time_count"], 2)
        self.assertEqual(len(payload["orders"]), 5)

    def test_moving_cutoff_over_a_rest_day_adds_no_workday(self):
        order = LeadTimeOrder(
            reference="still-moving",
            started_on=date(2024, 1, 1),
            sla_working_days=5,
        )
        friday = lead_time_dashboard(
            [order], date(2024, 1, 5), calendar=self.cal).orders[0]
        monday = lead_time_dashboard(
            [order], date(2024, 1, 8), calendar=self.cal).orders[0]
        self.assertEqual(friday.elapsed_working_days, 4)
        self.assertEqual(monday.elapsed_working_days, 5)
        self.assertFalse(friday.overdue)
        self.assertFalse(monday.overdue)

    def test_moving_cutoff_over_a_holiday_adds_no_workday(self):
        calendar = WesternCalendar()
        order = LeadTimeOrder(
            reference="still-moving",
            started_on=date(2024, 12, 23),
            sla_working_days=5,
        )
        before = compute_lead_time(
            order, date(2024, 12, 24), calendar=calendar)
        after = compute_lead_time(
            order, date(2024, 12, 25), calendar=calendar)
        self.assertEqual(before.elapsed_working_days,
                         after.elapsed_working_days)
        self.assertFalse(before.overdue)
        self.assertFalse(after.overdue)


class CalendarIntegrationTestCase(TestCase):
    def setUp(self):
        self.cal = NoHolidayCalendar()

    def test_calendar_elapsed_helper(self):
        self.assertEqual(
            self.cal.get_lead_time_elapsed(
                date(2024, 1, 1), date(2024, 1, 8),
                pauses=[PausePeriod(date(2024, 1, 5), date(2024, 1, 8))]),
            4,
        )

    def test_calendar_lead_time_helper(self):
        order = LeadTimeOrder(
            started_on=date(2024, 1, 1),
            sla_working_days=2,
        )
        result = self.cal.get_lead_time(order, date(2024, 1, 9))
        self.assertTrue(result.overdue)

    def test_calendar_dashboard_helper(self):
        orders = [
            LeadTimeOrder(
                reference="a", started_on=date(2024, 1, 1),
                sla_working_days=1),
            LeadTimeOrder(
                reference="b", started_on=date(2024, 1, 8),
                sla_working_days=5),
        ]
        summary = self.cal.get_lead_time_dashboard(
            orders, date(2024, 1, 10))
        self.assertEqual(summary.total_count, 2)
        self.assertEqual(summary.overdue_count, 1)
        self.assertEqual(summary.can_still_be_on_time_count, 1)
        self.assertEqual(summary["can_still_be_on_time_count"], 1)

    def test_clock_wrapper(self):
        from ..leadtime import LeadTimeClock
        clock = LeadTimeClock(calendar=self.cal)
        self.assertEqual(
            clock.elapsed(date(2024, 1, 1), date(2024, 1, 8)), 5)
        order = LeadTimeOrder(
            started_on=date(2024, 1, 1), sla_working_days=3)
        self.assertEqual(clock.measure(order, date(2024, 1, 4)).overdue, False)
        result = clock.measure(order, date(2024, 1, 5))
        self.assertTrue(result.overdue)

    def test_sla_module_aliases(self):
        from .. import sla
        order = sla.LeadTimeOrder(
            started_on=date(2024, 1, 1), sla_working_days=2)
        result = sla.compute_lead_time(
            order, date(2024, 1, 5), calendar=self.cal)
        self.assertTrue(result.overdue)
        self.assertEqual(sla.Order, sla.LeadTimeOrder)
        self.assertEqual(sla.Pause, sla.PausePeriod)

    def test_summarize_lead_times(self):
        from ..leadtime import summarize_lead_times
        payload = summarize_lead_times(
            [LeadTimeOrder(
                reference="x", started_on=date(2024, 1, 1),
                sla_working_days=2)],
            date(2024, 1, 9),
            calendar=self.cal,
        )
        self.assertEqual(payload["total_count"], 1)
        self.assertEqual(payload["overdue_count"], 1)
        self.assertEqual(payload["overdue_working_days"], 4)

    def test_order_accepts_datetime_and_string_friendly_mapping(self):
        result = compute_lead_time(
            {"order_id": "DT", "created": date(2024, 1, 1),
             "allowed": 5},
            date(2024, 1, 8),
            calendar=self.cal,
        )
        self.assertEqual(result.reference, "DT")
        self.assertEqual(result.elapsed_working_days, 5)
        self.assertFalse(result.overdue)

    def test_exactly_on_limit_still_on_time(self):
        order = LeadTimeOrder(
            started_on=date(2024, 1, 1), sla_working_days=5)
        self.assertTrue(
            compute_lead_time(order, date(2024, 1, 8),
                              calendar=self.cal).can_still_be_on_time)

    def test_delivered_late_keeps_overdue_days(self):
        order = LeadTimeOrder(
            started_on=date(2024, 1, 1), sla_working_days=1,
            delivered_on=date(2024, 1, 3))
        result = compute_lead_time(order, date(2024, 1, 10),
                                   calendar=self.cal)
        self.assertTrue(result.overdue)
        self.assertEqual(result.overdue_working_days, 1)
        self.assertEqual(result.state, LeadTimeState.DELIVERED_LATE)
