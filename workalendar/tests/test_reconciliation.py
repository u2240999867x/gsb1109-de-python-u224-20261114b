from datetime import date, datetime
from unittest import TestCase

from ..core import SAT, SUN, Calendar
from ..exceptions import CalendarError, UnsupportedContract
from ..reconciliation import (
    BOTH,
    NEXT_WORKING_DAY,
    SAME_DAY,
    ReconciliationCalendar,
    ReconciliationWindow,
    get_dispatch_receipts,
    get_reconciliation_windows,
    get_same_day_handovers,
    normalize_contract,
)


class HolidayCalendar(Calendar):
    include_new_years_day = False
    WEEKEND_DAYS = (SAT, SUN)
    HOLIDAYS = ()

    def get_variable_days(self, year):
        days = super().get_variable_days(year)
        days.extend(
            (date(year, month, day), label)
            for month, day, label in self.HOLIDAYS
        )
        return days


class SenderCalendar(HolidayCalendar):
    # June 2024: closed on the 4th and from the 17th to the 21st.
    HOLIDAYS = (
        (6, 4, "Sender holiday"),
        (6, 17, "Sender break"),
        (6, 18, "Sender break"),
        (6, 19, "Sender break"),
        (6, 20, "Sender break"),
        (6, 21, "Sender break"),
    )


class ReceiverCalendar(HolidayCalendar):
    # June 2024: closed on the 6th and from the 10th to the 14th.
    HOLIDAYS = (
        (6, 6, "Receiver holiday"),
        (6, 10, "Receiver break"),
        (6, 11, "Receiver break"),
        (6, 12, "Receiver break"),
        (6, 13, "Receiver break"),
        (6, 14, "Receiver break"),
    )


class ReconciliationCalendarTest(TestCase):

    def setUp(self):
        super().setUp()
        self.sender = SenderCalendar()
        self.receiver = ReceiverCalendar()
        self.pair = ReconciliationCalendar(self.sender, self.receiver)

    def test_calendar_names_are_recorded(self):
        self.assertEqual(self.pair.sender_calendar, "SenderCalendar")
        self.assertEqual(self.pair.receiver_calendar, "ReceiverCalendar")

    def test_explicit_calendar_names(self):
        pair = ReconciliationCalendar(
            self.sender, self.receiver,
            sender_name="Beijing", receiver_name="Paris")
        window = pair.get_same_day_windows(
            date(2024, 6, 3), date(2024, 6, 3))[0]
        self.assertEqual(window.sender_calendar, "Beijing")
        self.assertEqual(window.receiver_calendar, "Paris")

    def test_common_working_days(self):
        days = self.pair.get_common_working_days(
            date(2024, 6, 3), date(2024, 6, 7))
        self.assertEqual(
            days, [date(2024, 6, 3), date(2024, 6, 5), date(2024, 6, 7)])

    def test_same_day_windows_split_around_holidays(self):
        windows = self.pair.get_same_day_windows(
            date(2024, 6, 3), date(2024, 6, 7))
        self.assertEqual(len(windows), 3)
        self.assertEqual(
            [(w.dispatch_start, w.dispatch_end) for w in windows],
            [
                (date(2024, 6, 3), date(2024, 6, 3)),
                (date(2024, 6, 5), date(2024, 6, 5)),
                (date(2024, 6, 7), date(2024, 6, 7)),
            ],
        )
        for window in windows:
            self.assertTrue(window.is_same_day)
            self.assertEqual(window.contract, SAME_DAY)
            self.assertEqual(window.dispatch_start, window.receipt_start)
            self.assertEqual(window.dispatch_end, window.receipt_end)

    def test_same_day_contiguous_run_is_one_window(self):
        windows = self.pair.get_same_day_windows(
            date(2024, 6, 3), date(2024, 6, 3))
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].dispatch_dates, (date(2024, 6, 3),))

        # June 3rd and 5th are separated by the sender's June 4th holiday.
        # Joining the interval across the 4th must not merge the two runs.
        self.assertEqual(len(self.pair.get_same_day_windows(
            date(2024, 6, 3), date(2024, 6, 5))), 2)

    def test_dispatch_receipt_pairs(self):
        pairs = self.pair.get_dispatch_receipts(
            date(2024, 6, 3), date(2024, 6, 7))
        self.assertEqual(pairs, [
            (date(2024, 6, 3), date(2024, 6, 4)),
            (date(2024, 6, 5), date(2024, 6, 7)),
            (date(2024, 6, 6), date(2024, 6, 7)),
            (date(2024, 6, 7), date(2024, 6, 17)),
        ])

    def test_next_working_day_windows(self):
        windows = self.pair.get_dispatch_windows(
            date(2024, 6, 3), date(2024, 6, 7))
        self.assertEqual(
            [(w.dispatch_start, w.dispatch_end, w.receipt_start)
             for w in windows],
            [
                (date(2024, 6, 3), date(2024, 6, 3), date(2024, 6, 4)),
                (date(2024, 6, 5), date(2024, 6, 6), date(2024, 6, 7)),
                (date(2024, 6, 7), date(2024, 6, 7), date(2024, 6, 17)),
            ],
        )
        self.assertTrue(all(w.is_next_working_day for w in windows))

    def test_next_working_day_never_same_day(self):
        # June 3rd is open on both sides, but the contract still means the
        # receiver signs on the *next* working day.
        window = self.pair.get_dispatch_windows(
            date(2024, 6, 3), date(2024, 6, 3))[0]
        self.assertEqual(window.receipt_start, date(2024, 6, 4))
        self.assertNotEqual(window.receipt_start, window.dispatch_start)

    def test_receiver_long_holiday_collapses_dispatches(self):
        windows = self.pair.get_dispatch_windows(
            date(2024, 6, 10), date(2024, 6, 14))
        self.assertEqual(len(windows), 1)
        window = windows[0]
        self.assertEqual(window.dispatch_start, date(2024, 6, 10))
        self.assertEqual(window.dispatch_end, date(2024, 6, 14))
        self.assertEqual(window.receipt_start, date(2024, 6, 17))
        self.assertEqual(window.receipt_end, date(2024, 6, 17))
        self.assertEqual(len(window.dispatch_dates), 5)
        self.assertEqual(window.receipt_dates, (date(2024, 6, 17),))
        self.assertEqual(window.send_dates, window.dispatch_dates)
        self.assertEqual(window.receive_dates, window.receipt_dates)
        self.assertEqual(window.receipt_date, date(2024, 6, 17))
        with self.assertRaises(ValueError):
            window.dispatch_date

    def test_single_date_accessors_reject_ranges(self):
        window = self.pair.get_same_day_windows(
            date(2024, 6, 3), date(2024, 6, 3))[0]
        self.assertEqual(window.dispatch_date, date(2024, 6, 3))
        self.assertEqual(window.receipt_date, date(2024, 6, 3))
        multi_day = ReconciliationWindow(
            contract=SAME_DAY,
            dispatch_start=date(2024, 6, 3),
            dispatch_end=date(2024, 6, 5),
            receipt_start=date(2024, 6, 3),
            receipt_end=date(2024, 6, 5),
            sender_calendar="A",
            receiver_calendar="B",
        )
        with self.assertRaises(ValueError):
            multi_day.dispatch_date
        with self.assertRaises(ValueError):
            multi_day.receipt_date

    def test_receipt_groups_and_dispatch_map(self):
        groups = self.pair.get_receipt_groups(
            date(2024, 6, 10), date(2024, 6, 14))
        self.assertEqual(list(groups), [date(2024, 6, 17)])
        self.assertEqual(groups[date(2024, 6, 17)], [
            date(2024, 6, 10), date(2024, 6, 11), date(2024, 6, 12),
            date(2024, 6, 13), date(2024, 6, 14),
        ])
        mapping = self.pair.get_dispatch_receipt_map(
            date(2024, 6, 10), date(2024, 6, 14))
        self.assertEqual(mapping[date(2024, 6, 10)], date(2024, 6, 17))
        self.assertEqual(len(mapping), 5)

    def test_sender_long_holiday_has_no_handover(self):
        self.assertEqual(
            self.pair.get_same_day_handovers(
                date(2024, 6, 17), date(2024, 6, 21)),
            [])
        self.assertEqual(
            self.pair.get_same_day_windows(
                date(2024, 6, 17), date(2024, 6, 21)),
            [])
        self.assertEqual(
            self.pair.get_dispatch_windows(
                date(2024, 6, 17), date(2024, 6, 21)),
            [])

    def test_query_boundary_inside_receiver_holiday(self):
        whole = self.pair.get_dispatch_windows(
            date(2024, 6, 10), date(2024, 6, 14))
        part = self.pair.get_dispatch_windows(
            date(2024, 6, 12), date(2024, 6, 13))
        self.assertEqual(len(part), 1)
        self.assertEqual(part[0].receipt_start, whole[0].receipt_start)
        self.assertEqual(part[0].receipt_end, whole[0].receipt_end)
        self.assertEqual(part[0].receipt_start, date(2024, 6, 17))

        # The receipt is outside the query interval; it must not be clipped.
        self.assertGreater(part[0].receipt_start, date(2024, 6, 13))

    def test_partial_queries_union_to_full_interval(self):
        start, end = date(2024, 6, 10), date(2024, 6, 14)
        whole = self.pair.get_dispatch_receipts(start, end)
        parts = (
            self.pair.get_dispatch_receipts(start, date(2024, 6, 12))
            + self.pair.get_dispatch_receipts(date(2024, 6, 13), end)
        )
        self.assertEqual(parts, whole)
        self.assertEqual(
            self.pair.get_dispatch_receipts(
                date(2024, 6, 12), date(2024, 6, 12)),
            [(date(2024, 6, 12), date(2024, 6, 17))],
        )

    def test_partial_queries_union_for_same_day_contract(self):
        start, end = date(2024, 6, 3), date(2024, 6, 7)
        whole = self.pair.get_same_day_handovers(start, end)
        parts = (
            self.pair.get_same_day_handovers(start, date(2024, 6, 4))
            + self.pair.get_same_day_handovers(date(2024, 6, 5), end)
        )
        self.assertEqual(parts, whole)

    def test_query_boundary_inside_sender_holiday(self):
        self.assertEqual(
            self.pair.get_dispatch_receipts(
                date(2024, 6, 15), date(2024, 6, 16)), [])
        self.assertEqual(
            self.pair.get_dispatch_receipts(
                date(2024, 6, 18), date(2024, 6, 20)), [])
        # The sender reopens on June 24th (June 22-23 is a weekend).
        self.assertEqual(
            self.pair.get_dispatch_receipts(
                date(2024, 6, 21), date(2024, 6, 24)),
            [(date(2024, 6, 24), date(2024, 6, 25))],
        )

    def test_reversed_pair_changes_dispatch_direction(self):
        reversed_pair = ReconciliationCalendar(
            self.receiver, self.sender,
            sender_name="ReceiverCalendar", receiver_name="SenderCalendar")
        forward = self.pair.get_dispatch_receipts(
            date(2024, 6, 3), date(2024, 6, 7))
        backward = reversed_pair.get_dispatch_receipts(
            date(2024, 6, 3), date(2024, 6, 7))
        self.assertNotEqual(forward, backward)
        self.assertEqual(backward, [
            (date(2024, 6, 3), date(2024, 6, 5)),
            (date(2024, 6, 4), date(2024, 6, 5)),
            (date(2024, 6, 5), date(2024, 6, 6)),
            (date(2024, 6, 7), date(2024, 6, 10)),
        ])
        # Same-day handovers are symmetric.
        self.assertEqual(
            self.pair.get_same_day_handovers(date(2024, 6, 3),
                                             date(2024, 6, 7)),
            reversed_pair.get_same_day_handovers(date(2024, 6, 3),
                                                 date(2024, 6, 7)),
        )

    def test_get_windows_defaults_to_both_contracts(self):
        windows = self.pair.get_windows(
            date(2024, 6, 3), date(2024, 6, 7))
        self.assertEqual(
            [w.contract for w in windows],
            [SAME_DAY, NEXT_WORKING_DAY] * 3,
        )
        self.assertEqual(
            self.pair.get_windows(date(2024, 6, 3), date(2024, 6, 7),
                                  contract=SAME_DAY),
            self.pair.get_same_day_windows(date(2024, 6, 3),
                                           date(2024, 6, 7)),
        )
        self.assertEqual(
            self.pair.get_windows(date(2024, 6, 3), date(2024, 6, 7),
                                  contract=NEXT_WORKING_DAY),
            self.pair.get_dispatch_windows(date(2024, 6, 3),
                                           date(2024, 6, 7)),
        )

    def test_plan_names_calendars_on_both_contracts(self):
        plan = self.pair.plan(date(2024, 6, 3), date(2024, 6, 14))
        self.assertEqual(plan.sender_calendar, "SenderCalendar")
        self.assertEqual(plan.receiver_calendar, "ReceiverCalendar")
        self.assertEqual(plan.calendar_names, {
            "sender": "SenderCalendar",
            "receiver": "ReceiverCalendar",
        })
        self.assertEqual(plan.by_contract(SAME_DAY), plan.same_day)
        self.assertEqual(plan.by_contract(NEXT_WORKING_DAY),
                         plan.next_working_day)
        self.assertEqual(plan.by_contract(BOTH), plan.windows)

    def test_swapped_interval_bounds(self):
        self.assertEqual(
            self.pair.get_dispatch_receipts(
                date(2024, 6, 7), date(2024, 6, 3)),
            self.pair.get_dispatch_receipts(
                date(2024, 6, 3), date(2024, 6, 7)),
        )

    def test_datetime_inputs_are_accepted(self):
        windows = self.pair.get_dispatch_windows(
            datetime(2024, 6, 10, 9, 30),
            datetime(2024, 6, 14, 18, 0))
        self.assertEqual(windows[0].dispatch_start, date(2024, 6, 10))
        self.assertEqual(windows[0].receipt_start, date(2024, 6, 17))

    def test_single_day_receiver_holiday(self):
        # June 6th is closed on the receiver side only.  June 5th and 7th
        # both dispatch, but June 5th must wait for the receiver to reopen.
        windows = self.pair.get_dispatch_windows(
            date(2024, 6, 5), date(2024, 6, 7))
        self.assertEqual(
            [(w.dispatch_start, w.dispatch_end, w.receipt_start)
             for w in windows],
            [
                (date(2024, 6, 5), date(2024, 6, 6), date(2024, 6, 7)),
                (date(2024, 6, 7), date(2024, 6, 7), date(2024, 6, 17)),
            ],
        )

    def test_no_working_days_at_all(self):
        self.assertEqual(
            self.pair.get_same_day_handovers(
                date(2024, 6, 8), date(2024, 6, 9)), [])
        self.assertEqual(
            self.pair.get_dispatch_receipts(
                date(2024, 6, 8), date(2024, 6, 9)), [])

    def test_contract_aliases(self):
        self.assertEqual(normalize_contract(None), BOTH)
        self.assertEqual(normalize_contract("same-day"), SAME_DAY)
        self.assertEqual(normalize_contract("Same Day Handover"), SAME_DAY)
        self.assertEqual(normalize_contract("next working day"),
                         NEXT_WORKING_DAY)
        self.assertEqual(normalize_contract("dispatch"), NEXT_WORKING_DAY)
        self.assertEqual(normalize_contract("all"), BOTH)

    def test_unknown_contract_is_rejected(self):
        with self.assertRaises(UnsupportedContract):
            normalize_contract("teleport")
        with self.assertRaises(UnsupportedContract):
            self.pair.get_windows(
                date(2024, 6, 3), date(2024, 6, 7), contract="teleport")

    def test_window_validation(self):
        with self.assertRaises(ValueError):
            ReconciliationWindow(
                contract=SAME_DAY,
                dispatch_start=date(2024, 6, 3),
                dispatch_end=date(2024, 6, 3),
                receipt_start=date(2024, 6, 4),
                receipt_end=date(2024, 6, 4),
                sender_calendar="A",
                receiver_calendar="B",
            )
        with self.assertRaises(UnsupportedContract):
            ReconciliationWindow(
                contract=BOTH,
                dispatch_start=date(2024, 6, 3),
                dispatch_end=date(2024, 6, 3),
                receipt_start=date(2024, 6, 3),
                receipt_end=date(2024, 6, 3),
                sender_calendar="A",
                receiver_calendar="B",
            )

    def test_window_as_dict(self):
        window = self.pair.get_dispatch_windows(
            date(2024, 6, 10), date(2024, 6, 14))[0]
        self.assertEqual(window.as_dict(), {
            "contract": NEXT_WORKING_DAY,
            "dispatch_start": "2024-06-10",
            "dispatch_end": "2024-06-14",
            "receipt_start": "2024-06-17",
            "receipt_end": "2024-06-17",
            "sender_calendar": "SenderCalendar",
            "receiver_calendar": "ReceiverCalendar",
        })


class ReconciliationShortcutTest(TestCase):

    def setUp(self):
        super().setUp()
        self.sender = SenderCalendar()
        self.receiver = ReceiverCalendar()

    def test_module_level_windows(self):
        windows = get_reconciliation_windows(
            self.sender, self.receiver,
            date(2024, 6, 3), date(2024, 6, 7), contract=SAME_DAY)
        self.assertEqual(len(windows), 3)

    def test_module_level_handovers(self):
        self.assertEqual(
            get_same_day_handovers(
                self.sender, self.receiver,
                date(2024, 6, 3), date(2024, 6, 7)),
            [date(2024, 6, 3), date(2024, 6, 5), date(2024, 6, 7)],
        )

    def test_module_level_dispatch_pairs(self):
        self.assertEqual(
            get_dispatch_receipts(
                self.sender, self.receiver,
                date(2024, 6, 10), date(2024, 6, 14)),
            [
                (date(2024, 6, 10), date(2024, 6, 17)),
                (date(2024, 6, 11), date(2024, 6, 17)),
                (date(2024, 6, 12), date(2024, 6, 17)),
                (date(2024, 6, 13), date(2024, 6, 17)),
                (date(2024, 6, 14), date(2024, 6, 17)),
            ],
        )

    def test_calendar_convenience_methods(self):
        pair = self.sender.reconciliation_calendar(self.receiver)
        self.assertIsInstance(pair, ReconciliationCalendar)
        self.assertEqual(
            self.sender.get_reconciliation_windows(
                self.receiver,
                date(2024, 6, 3), date(2024, 6, 7),
                contract=SAME_DAY),
            pair.get_same_day_windows(date(2024, 6, 3), date(2024, 6, 7)),
        )

    def test_calendar_classes_are_accepted(self):
        pair = ReconciliationCalendar(SenderCalendar, ReceiverCalendar)
        self.assertIsInstance(pair.sender, SenderCalendar)
        self.assertIsInstance(pair.receiver, ReceiverCalendar)
        self.assertEqual(pair.sender_calendar, "SenderCalendar")

    def test_non_calendar_is_rejected(self):
        with self.assertRaises(TypeError):
            ReconciliationCalendar(object(), self.receiver)
        with self.assertRaises(TypeError):
            ReconciliationCalendar(self.sender, "ReceiverCalendar")

    def test_plan_as_dict(self):
        plan = self.sender.get_reconciliation_plan(
            self.receiver, date(2024, 6, 10), date(2024, 6, 14))
        value = plan.as_dict()
        self.assertEqual(value["sender_calendar"], "SenderCalendar")
        self.assertEqual(value["receiver_calendar"], "ReceiverCalendar")
        self.assertEqual(value["start"], "2024-06-10")
        self.assertEqual(value["end"], "2024-06-14")
        self.assertEqual(len(value["next_working_day"]), 1)
        self.assertEqual(
            value["next_working_day"][0]["receipt_start"], "2024-06-17")


class NeverOpenCalendar(Calendar):
    include_new_years_day = False
    WEEKEND_DAYS = tuple(range(7))


class ReceiverWithoutWorkingDaysTest(TestCase):

    def test_receipt_search_is_bounded(self):
        pair = ReconciliationCalendar(
            SenderCalendar(), NeverOpenCalendar(), max_search_days=10)
        with self.assertRaises(CalendarError):
            pair.get_next_working_day(date(2024, 6, 3))

    def test_invalid_search_bound(self):
        with self.assertRaises(ValueError):
            ReconciliationCalendar(
                SenderCalendar(), ReceiverCalendar(), max_search_days=0)
        with self.assertRaises(ValueError):
            ReconciliationCalendar(
                SenderCalendar(), ReceiverCalendar(), max_search_days=1.5)

    def test_short_bound_is_enough_for_a_normal_holiday(self):
        pair = ReconciliationCalendar(
            SenderCalendar(), ReceiverCalendar(), max_search_days=20)
        self.assertEqual(
            pair.get_next_working_day(date(2024, 6, 7)),
            date(2024, 6, 17),
        )
