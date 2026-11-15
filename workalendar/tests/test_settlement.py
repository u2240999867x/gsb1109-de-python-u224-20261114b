"""
Tests for the incremental settlement-date audit
(``workalendar.settlement``).
"""
import json
import random
from datetime import date, timedelta
from unittest import TestCase

from ..core import WesternCalendar, SAT, SUN, FRI
from ..exceptions import CalendarError
from ..settlement import (
    AddWorkingDays,
    AuditResult,
    CalendarSnapshot,
    FollowingWorkingDay,
    MonthEndNthWorkingDay,
    SettlementAudit,
    SettlementRecord,
    full_recompute,
    rule_from_dict,
    verify_audit,
)


class MutableCalendar(WesternCalendar):
    """A western calendar whose extra holidays are set at construction."""

    def __init__(self, extra_holidays=(), **kwargs):
        super().__init__(**kwargs)
        self._extra_holidays = tuple(sorted(extra_holidays))

    def get_variable_days(self, year):
        days = super().get_variable_days(year)
        days.extend(
            (day, f"extra holiday {day.isoformat()}")
            for day in self._extra_holidays
            if day.year == year
        )
        return days


# March 2024: Mar 1st is a Friday, Mar 30/31 are a weekend.
MARCH_ANCHOR = date(2024, 3, 15)
HOLIDAY_MAR_28 = date(2024, 3, 28)
HOLIDAY_MAR_29 = date(2024, 3, 29)


class OffsetRuleTest(TestCase):

    def test_add_working_days_matches_calendar(self):
        calendar = MutableCalendar([date(2024, 3, 6)])
        rule = AddWorkingDays(3)
        # Mar 4 (Mon) + 3 working days, skipping Mar 6 -> Mar 8.
        self.assertEqual(
            rule.apply(date(2024, 3, 4), calendar),
            calendar.add_working_days(date(2024, 3, 4), 3),
        )
        self.assertEqual(
            rule.apply(date(2024, 3, 4), calendar), date(2024, 3, 8))

    def test_negative_add_working_days(self):
        calendar = MutableCalendar()
        rule = AddWorkingDays(-5)
        self.assertEqual(
            rule.apply(date(2024, 3, 8), calendar),
            calendar.add_working_days(date(2024, 3, 8), -5),
        )

    def test_month_end_nth_working_day_plain(self):
        calendar = MutableCalendar()
        # Mar 31 is a Sunday: last WD is Mar 29, 2nd Mar 28, 3rd Mar 27.
        self.assertEqual(
            MonthEndNthWorkingDay(1).apply(MARCH_ANCHOR, calendar),
            date(2024, 3, 29))
        self.assertEqual(
            MonthEndNthWorkingDay(3).apply(MARCH_ANCHOR, calendar),
            date(2024, 3, 27))

    def test_month_end_nth_working_day_when_month_end_is_holiday(self):
        # Mar 29 (Friday) becomes a holiday: last WD moves to Mar 28 and
        # the 3rd-last working day moves to Mar 26.
        calendar = MutableCalendar([date(2024, 3, 29)])
        self.assertEqual(
            MonthEndNthWorkingDay(1).apply(MARCH_ANCHOR, calendar),
            date(2024, 3, 28))
        self.assertEqual(
            MonthEndNthWorkingDay(3).apply(MARCH_ANCHOR, calendar),
            date(2024, 3, 26))

    def test_month_end_nth_working_day_requires_positive_nth(self):
        with self.assertRaises(CalendarError):
            MonthEndNthWorkingDay(0)

    def test_following_working_day(self):
        calendar = MutableCalendar()
        rule = FollowingWorkingDay()
        # Mar 9 is a Saturday.
        self.assertEqual(
            rule.apply(date(2024, 3, 9), calendar), date(2024, 3, 11))
        # A regular working day stays untouched.
        self.assertEqual(
            rule.apply(date(2024, 3, 7), calendar), date(2024, 3, 7))

    def test_rule_serialization_roundtrip(self):
        rules = [
            AddWorkingDays(7),
            AddWorkingDays(-3),
            MonthEndNthWorkingDay(3),
            FollowingWorkingDay(),
        ]
        for rule in rules:
            restored = rule_from_dict(rule.to_dict())
            self.assertEqual(restored, rule)

    def test_unknown_rule_type(self):
        with self.assertRaises(CalendarError):
            rule_from_dict({"type": "no-such-rule"})


class CalendarSnapshotTest(TestCase):

    def test_snapshot_behaves_like_calendar(self):
        calendar = MutableCalendar([
            date(2024, 3, 6), date(2024, 12, 26)])
        snapshot = CalendarSnapshot.from_calendar(
            calendar, [2024, 2025], "v1")
        probe = date(2024, 1, 1)
        while probe <= date(2025, 12, 31):
            self.assertEqual(
                snapshot.is_working_day(probe),
                calendar.is_working_day(probe),
                probe,
            )
            probe += timedelta(days=1)
        self.assertEqual(
            snapshot.add_working_days(date(2024, 3, 4), 5),
            calendar.add_working_days(date(2024, 3, 4), 5),
        )
        self.assertEqual(
            snapshot.sub_working_days(date(2024, 3, 20), 4),
            calendar.sub_working_days(date(2024, 3, 20), 4),
        )

    def test_snapshot_keeps_labels(self):
        holiday = date(2024, 3, 6)
        snapshot = CalendarSnapshot.from_calendar(
            MutableCalendar([holiday]), [2024], "v1")
        self.assertIn(holiday, snapshot.holidays)
        labels = snapshot.label_of(holiday)
        self.assertEqual(labels, (f"extra holiday {holiday.isoformat()}",))
        self.assertEqual(snapshot.label_of(date(2024, 3, 7)), ())

    def test_snapshot_dict_roundtrip(self):
        calendar = MutableCalendar([date(2024, 3, 6)])
        snapshot = CalendarSnapshot.from_calendar(calendar, [2024], "v1")
        restored = CalendarSnapshot.from_dict(snapshot.to_dict())
        self.assertEqual(restored, snapshot)
        self.assertEqual(
            restored.add_working_days(date(2024, 3, 4), 3),
            snapshot.add_working_days(date(2024, 3, 4), 3),
        )
        json.dumps(snapshot.to_dict())

    def test_snapshot_rejects_uncovered_year(self):
        snapshot = CalendarSnapshot.from_calendar(
            MutableCalendar(), [2024], "v1")
        with self.assertRaises(CalendarError):
            snapshot.is_working_day(date(2025, 1, 2))
        with self.assertRaises(CalendarError):
            snapshot.add_working_days(date(2024, 12, 30), 5)


class SettlementAuditTest(TestCase):

    def _records(self):
        return [
            SettlementRecord(
                "TX-ADD", date(2024, 3, 4), AddWorkingDays(3), "v1"),
            SettlementRecord(
                "TX-END", MARCH_ANCHOR, MonthEndNthWorkingDay(3), "v1"),
            SettlementRecord(
                "TX-FOLLOW", date(2024, 3, 9),
                FollowingWorkingDay(), "v1"),
            SettlementRecord(
                "TX-STORED", date(2024, 3, 4),
                AddWorkingDays(3), "v1",
                last_settlement=date(2024, 3, 7)),
        ]

    def test_holiday_added_inside_interval(self):
        old_cal = MutableCalendar()
        new_cal = MutableCalendar([date(2024, 3, 6)])
        audit = SettlementAudit.from_calendars(
            old_cal, new_cal, self._records(), "v1", "v2")
        result = audit.audit(self._records())

        self.assertIsInstance(result, AuditResult)
        self.assertEqual(audit.calendar_diff.added, {date(2024, 3, 6)})
        change = next(
            item for item in result.changed
            if item.transaction_id == "TX-ADD")
        self.assertEqual(change.old_settlement, date(2024, 3, 7))
        self.assertEqual(change.new_settlement, date(2024, 3, 8))
        self.assertEqual(change.delta_days, 1)
        self.assertEqual(
            change.reasons[0].code, "holiday_added")
        self.assertIn(date(2024, 3, 6), change.reasons[0].dates)
        # The full recomputation must match, record by record.
        self.assertEqual(
            verify_audit(result, self._records(), old_cal, new_cal), [])
        json.dumps(result.to_dict())

    def test_correction_outside_query_interval_moves_month_end(self):
        # The anchor query is mid-March; the correction lands on Mar 29, at
        # the very end of the month, yet it moves the 3rd-last working day.
        old_cal = MutableCalendar()
        new_cal = MutableCalendar([HOLIDAY_MAR_29])
        records = [SettlementRecord(
            "TX-END", MARCH_ANCHOR,
            MonthEndNthWorkingDay(3), "v1")]
        result = SettlementAudit.from_calendars(
            old_cal, new_cal, records, "v1", "v2").audit(records)

        self.assertEqual(len(result.changed), 1)
        change = result.changed[0]
        self.assertEqual(change.old_settlement, date(2024, 3, 27))
        self.assertEqual(change.new_settlement, date(2024, 3, 26))
        self.assertEqual(change.reasons[0].code, "holiday_added")
        self.assertIn(HOLIDAY_MAR_29, change.reasons[0].dates)
        self.assertEqual(
            verify_audit(result, records, old_cal, new_cal), [])

    def test_swapped_holidays_can_leave_settlement_unchanged(self):
        # Old: holiday on Mar 28. New: holiday moved to Mar 29.
        old_cal = MutableCalendar([HOLIDAY_MAR_28])
        new_cal = MutableCalendar([HOLIDAY_MAR_29])
        old_snapshot = CalendarSnapshot.from_calendar(
            old_cal, [2024], "v1")
        new_snapshot = CalendarSnapshot.from_calendar(
            new_cal, [2024], "v2")
        diff = SettlementAudit(old_snapshot, new_snapshot).calendar_diff
        self.assertEqual(diff.added, {HOLIDAY_MAR_29})
        self.assertEqual(diff.removed, {HOLIDAY_MAR_28})
        self.assertFalse(diff.is_empty)

        # 3rd-last working day: both calendars land on Mar 26.
        month_end_record = SettlementRecord(
            "TX-SWAP-END", MARCH_ANCHOR,
            MonthEndNthWorkingDay(3), "v1")
        result = SettlementAudit.from_calendars(
            old_cal, new_cal, [month_end_record], "v1", "v2"
        ).audit([month_end_record])
        self.assertEqual(result.changed, ())
        self.assertEqual(
            result.unchanged, ("TX-SWAP-END",))
        self.assertEqual(
            result.all_settlements["TX-SWAP-END"],
            (date(2024, 3, 26), date(2024, 3, 26)))
        self.assertEqual(
            verify_audit(
                result, [month_end_record], old_cal, new_cal), [])

        # The very same swap does move an add-working-days settlement.
        add_record = SettlementRecord(
            "TX-SWAP-ADD", date(2024, 3, 25),
            AddWorkingDays(3), "v1")
        result_add = SettlementAudit.from_calendars(
            old_cal, new_cal, [add_record], "v1", "v2"
        ).audit([add_record])
        self.assertEqual(len(result_add.changed), 1)
        self.assertEqual(
            result_add.changed[0].old_settlement, date(2024, 3, 29))
        self.assertEqual(
            result_add.changed[0].new_settlement, date(2024, 3, 28))

    def test_holiday_removed_reason(self):
        old_cal = MutableCalendar([date(2024, 3, 6)])
        new_cal = MutableCalendar()
        records = [SettlementRecord(
            "TX-REMOVE", date(2024, 3, 4), AddWorkingDays(3), "v1")]
        result = SettlementAudit.from_calendars(
            old_cal, new_cal, records, "v1", "v2").audit(records)
        change = result.changed[0]
        self.assertEqual(
            change.old_settlement, date(2024, 3, 8))
        self.assertEqual(
            change.new_settlement, date(2024, 3, 7))
        self.assertEqual(change.reasons[0].code, "holiday_removed")

    def test_following_working_day_change(self):
        old_cal = MutableCalendar()
        new_cal = MutableCalendar([date(2024, 3, 11)])
        records = [SettlementRecord(
            "TX-FOLLOW", date(2024, 3, 9),
            FollowingWorkingDay(), "v1")]
        result = SettlementAudit.from_calendars(
            old_cal, new_cal, records, "v1", "v2").audit(records)
        change = result.changed[0]
        self.assertEqual(
            change.old_settlement, date(2024, 3, 11))
        self.assertEqual(
            change.new_settlement, date(2024, 3, 12))

    def test_year_boundary_uses_next_year_snapshot(self):
        # Anchor Dec 30 2024, Jan 1 2025 is a built-in holiday.
        old_cal = MutableCalendar()
        new_cal = MutableCalendar([date(2025, 1, 2)])
        records = [SettlementRecord(
            "TX-YEAR", date(2024, 12, 30),
            AddWorkingDays(3), "v1")]
        result = SettlementAudit.from_calendars(
            old_cal, new_cal, records, "v1", "v2").audit(records)
        change = result.changed[0]
        # Old: Dec 31, Jan 2, Jan 3. New: Dec 31, Jan 3, Jan 6.
        self.assertEqual(
            change.old_settlement, date(2025, 1, 3))
        self.assertEqual(
            change.new_settlement, date(2025, 1, 6))
        self.assertEqual(
            verify_audit(result, records, old_cal, new_cal), [])

    def test_unknown_calendar_version_is_reported(self):
        old_cal = MutableCalendar()
        new_cal = MutableCalendar([date(2024, 3, 6)])
        records = [SettlementRecord(
            "TX-OLD", date(2024, 3, 4), AddWorkingDays(3), "v0")]
        result = SettlementAudit.from_calendars(
            old_cal, new_cal, records, "v1", "v2").audit(records)
        self.assertEqual(result.unknown_version, ("TX-OLD",))
        self.assertEqual(result.changed, ())
        self.assertNotIn("TX-OLD", result.all_settlements)

    def test_verify_audit_detects_stale_stored_settlement(self):
        old_cal = MutableCalendar()
        new_cal = MutableCalendar([date(2024, 3, 6)])
        records = [SettlementRecord(
            "TX-STALE", date(2024, 3, 4), AddWorkingDays(3), "v1",
            last_settlement=date(2024, 3, 1))]
        result = SettlementAudit.from_calendars(
            old_cal, new_cal, records, "v1", "v2").audit(records)
        errors = verify_audit(result, records, old_cal, new_cal)
        self.assertEqual(len(errors), 1)
        self.assertIn("TX-STALE", errors[0])

    def test_record_serialization_roundtrip(self):
        record = SettlementRecord(
            "TX-1", MARCH_ANCHOR, MonthEndNthWorkingDay(3), "v1",
            last_settlement=date(2024, 3, 27))
        restored = SettlementRecord.from_dict(record.to_dict())
        self.assertEqual(restored, record)
        json.dumps(record.to_dict())

    def test_identical_calendars_change_nothing(self):
        old_cal = MutableCalendar([date(2024, 3, 6)])
        new_cal = MutableCalendar([date(2024, 3, 6)])
        # Drop the record carrying a stored settlement: it was computed
        # against a calendar without the Mar 6 holiday.
        records = [
            record for record in self._records()
            if record.transaction_id != "TX-STORED"
        ]
        result = SettlementAudit.from_calendars(
            old_cal, new_cal, records, "v1", "v2").audit(records)
        self.assertTrue(result.calendar_diff.is_empty)
        self.assertEqual(result.changed, ())
        self.assertEqual(
            set(result.unchanged), {"TX-ADD", "TX-END", "TX-FOLLOW"})
        self.assertEqual(
            verify_audit(result, records, old_cal, new_cal), [])


class SettlementAuditFuzzTest(TestCase):
    """Random calendars and records must always match full recomputation."""

    def _random_calendar(self, rng, holiday_pool):
        holidays = rng.sample(holiday_pool, rng.randrange(0, 12))
        return MutableCalendar(holidays)

    def _random_records(self, rng):
        records = []
        for index in range(40):
            anchor = date(2024, 1, 1) + timedelta(
                days=rng.randrange(0, 150))
            rule_kind = rng.randrange(3)
            if rule_kind == 0:
                rule = AddWorkingDays(rng.randrange(-10, 11))
            elif rule_kind == 1:
                rule = MonthEndNthWorkingDay(rng.randrange(1, 6))
            else:
                rule = FollowingWorkingDay()
            records.append(SettlementRecord(
                f"TX-{index:03d}", anchor, rule, "v1"))
        return records

    def test_randomized_audit_matches_full_recomputation(self):
        rng = random.Random(20241114)
        pool = [
            date(2024, 1, 1) + timedelta(days=offset)
            for offset in range(182)
        ]
        for _ in range(50):
            old_cal = self._random_calendar(rng, pool)
            new_cal = self._random_calendar(rng, pool)
            records = self._random_records(rng)

            result = SettlementAudit.from_calendars(
                old_cal, new_cal, records, "v1", "v2").audit(records)

            errors = verify_audit(
                result, records, old_cal, new_cal)
            self.assertEqual(errors, [])

            expected_old = full_recompute(records, old_cal)
            expected_new = full_recompute(records, new_cal)
            expected_changed = {
                transaction_id
                for transaction_id in expected_old
                if expected_old[transaction_id]
                != expected_new[transaction_id]
            }
            self.assertEqual(
                {change.transaction_id for change in result.changed},
                expected_changed,
            )
            # Every explanation must point at an actual holiday change:
            # the generic fallback reason must never trigger here.
            for change in result.changed:
                codes = {reason.code for reason in change.reasons}
                self.assertTrue(
                    codes & {"holiday_added", "holiday_removed"})
                for reason in change.reasons:
                    if reason.code in ("holiday_added", "holiday_removed"):
                        self.assertTrue(reason.dates)
                        self.assertTrue(reason.labels)


class WeekendChangeTest(TestCase):

    class FridayWeekendCalendar(MutableCalendar):
        WEEKEND_DAYS = (FRI, SAT)

    def test_weekend_definition_change_is_reported(self):
        old_cal = MutableCalendar()
        new_cal = self.FridayWeekendCalendar()
        self.assertEqual(old_cal.get_weekend_days(), (SAT, SUN))
        self.assertEqual(new_cal.get_weekend_days(), (FRI, SAT))
        records = [SettlementRecord(
            "TX-WEEKEND", date(2024, 3, 6),
            AddWorkingDays(2), "v1")]
        result = SettlementAudit.from_calendars(
            old_cal, new_cal, records, "v1", "v2").audit(records)
        self.assertTrue(result.calendar_diff.weekend_changed)
        change = result.changed[0]
        self.assertTrue(any(
            reason.code == "weekend_changed" for reason in change.reasons))
        self.assertEqual(
            verify_audit(result, records, old_cal, new_cal), [])
        # Old (Sat/Sun): Mar 7, Mar 8. New (Fri/Sat): Mar 7, Mar 10,
        # because Mar 8 is a Friday and becomes part of the weekend.
        self.assertEqual(
            change.old_settlement, date(2024, 3, 8))
        self.assertEqual(
            change.new_settlement, date(2024, 3, 10))
