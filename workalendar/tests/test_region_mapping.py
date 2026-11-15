from datetime import date
from unittest import TestCase

from ..core import FRI, SAT, SUN, Calendar
from ..exceptions import AmbiguousRegionMappingError, ISORegistryError
from ..registry import EffectiveRegionMap, IsoRegistry, RegionMapping


class ProvinceAlpha(Calendar):
    'Province Alpha'
    WEEKEND_DAYS = (SAT, SUN)


class ProvinceBeta(Calendar):
    'Province Beta'
    WEEKEND_DAYS = (FRI, SAT)


class RegionMappingRecordTest(TestCase):

    def test_record_requires_code(self):
        with self.assertRaises(ISORegistryError):
            RegionMapping('', iso_code='RA')

    def test_record_requires_rule_or_parent(self):
        with self.assertRaises(ISORegistryError):
            RegionMapping('C001')

    def test_record_rejects_inverted_interval(self):
        with self.assertRaises(ISORegistryError):
            RegionMapping(
                'C001', iso_code='RA',
                start=date(2001, 1, 1), end=date(2000, 12, 31))

    def test_covers_bounds_are_inclusive(self):
        record = RegionMapping(
            'C001', iso_code='RA',
            start=date(2000, 1, 1), end=date(2010, 12, 31))
        self.assertFalse(record.covers(date(1999, 12, 31)))
        self.assertTrue(record.covers(date(2000, 1, 1)))
        self.assertTrue(record.covers(date(2010, 12, 31)))
        self.assertFalse(record.covers(date(2011, 1, 1)))

    def test_covers_open_interval(self):
        record = RegionMapping('C001', iso_code='RA')
        self.assertTrue(record.covers(date(1970, 1, 1)))
        self.assertTrue(record.covers(date(2070, 1, 1)))


class EffectiveRegionMapTest(TestCase):

    def setUp(self):
        self.iso_registry = IsoRegistry(load_standard_modules=False)
        self.iso_registry.register('RA', ProvinceAlpha)
        self.iso_registry.register('RB', ProvinceBeta)
        self.region_map = EffectiveRegionMap(self.iso_registry)

    def test_legacy_province_abbreviation(self):
        # Old data mixes province-level abbreviations
        self.region_map.add('PA', iso_code='RA')
        self.assertEqual(
            'RA', self.region_map.resolve('PA', date(2020, 6, 1)))
        calendar_class = self.region_map.get_calendar('PA', date(2020, 6, 1))
        self.assertEqual(ProvinceAlpha, calendar_class)

    def test_county_parent_chain_across_two_adjustments(self):
        # The county code changed parents in two district adjustments
        self.region_map.add('PA', iso_code='RA')
        self.region_map.add('PB', iso_code='RB')
        self.region_map.add('PC', iso_code='RA')
        self.region_map.add('C001', parent='PA', end=date(1999, 12, 31))
        self.region_map.add(
            'C001', parent='PB',
            start=date(2000, 1, 1), end=date(2010, 12, 31))
        self.region_map.add('C001', parent='PC', start=date(2011, 1, 1))
        self.assertEqual(
            'RA', self.region_map.resolve('C001', date(1995, 6, 1)))
        self.assertEqual(
            'RB', self.region_map.resolve('C001', date(2005, 6, 1)))
        self.assertEqual(
            'RA', self.region_map.resolve('C001', date(2015, 6, 1)))

    def test_direct_rule_beats_parent_chain(self):
        # Only when the county level lacks a rule should the lookup
        # follow the parent chain effective on the business date.
        self.region_map.add('PA', iso_code='RA')
        self.region_map.add('C002', parent='PA')
        self.region_map.add(
            'C002', iso_code='RB',
            start=date(2005, 1, 1), end=date(2005, 12, 31))
        # Direct county rule effective: parent chain not consulted
        self.assertEqual(
            'RB', self.region_map.resolve('C002', date(2005, 6, 1)))
        # County rule missing for this date: fall back to the parent
        self.assertEqual(
            'RA', self.region_map.resolve('C002', date(2006, 6, 1)))

    def test_ambiguous_intervals_left_for_confirmation(self):
        # The same old code falls into two overlapping effective intervals
        self.region_map.add(
            'OLD', iso_code='RA',
            start=date(2000, 1, 1), end=date(2005, 12, 31))
        self.region_map.add(
            'OLD', iso_code='RB',
            start=date(2003, 1, 1), end=date(2008, 12, 31))
        with self.assertRaises(AmbiguousRegionMappingError):
            self.region_map.resolve('OLD', date(2004, 6, 1))
        # Outside of the overlap, resolution is unambiguous
        self.assertEqual(
            'RA', self.region_map.resolve('OLD', date(2001, 6, 1)))
        self.assertEqual(
            'RB', self.region_map.resolve('OLD', date(2007, 6, 1)))

    def test_ambiguous_parent_assignments_left_for_confirmation(self):
        self.region_map.add(
            'C004', parent='PA',
            start=date(2000, 1, 1), end=date(2005, 12, 31))
        self.region_map.add(
            'C004', parent='PB',
            start=date(2003, 1, 1), end=date(2008, 12, 31))
        with self.assertRaises(AmbiguousRegionMappingError):
            self.region_map.resolve('C004', date(2004, 6, 1))

    def test_duplicate_identical_rules_are_not_ambiguous(self):
        self.region_map.add('PA', iso_code='RA')
        self.region_map.add('PA', iso_code='RA')
        self.assertEqual(
            'RA', self.region_map.resolve('PA', date(2020, 6, 1)))

    def test_unknown_code_returns_none(self):
        self.assertIsNone(self.region_map.resolve('XX', date(2020, 6, 1)))
        self.assertIsNone(
            self.region_map.get_calendar('XX', date(2020, 6, 1)))

    def test_registered_code_resolves_to_itself(self):
        self.assertEqual(
            'RA', self.region_map.resolve('RA', date(2020, 6, 1)))
        calendar_class = self.region_map.get_calendar('RA', date(2020, 6, 1))
        self.assertEqual(ProvinceAlpha, calendar_class)

    def test_cyclic_parent_chain_detected(self):
        self.region_map.add('CX', parent='CY')
        self.region_map.add('CY', parent='CX')
        with self.assertRaises(ISORegistryError):
            self.region_map.resolve('CX', date(2020, 6, 1))

    def test_backfill_keeps_unrelated_years_and_source(self):
        self.region_map.add('PA', iso_code='RA')
        self.region_map.add('C003', parent='PA', start=date(2000, 1, 1))
        # No belonging record for 1993 yet
        self.assertIsNone(self.region_map.resolve('C003', date(1993, 6, 4)))
        # Workdays of an unrelated year, before the backfill
        calendar_class = self.region_map.get_calendar('C003', date(2005, 6, 3))
        self.assertEqual(ProvinceAlpha, calendar_class)
        was_working_day = calendar_class().is_working_day(date(2005, 6, 3))
        self.assertTrue(was_working_day)
        # Snapshot the original source
        source_before = dict(self.iso_registry.region_registry)
        # Later, backfill a belonging record for 1990-1999
        self.region_map.add('PB', iso_code='RB')
        self.region_map.add(
            'C003', parent='PB',
            start=date(1990, 1, 1), end=date(1999, 12, 31))
        # The backfilled segment now resolves
        calendar_class = self.region_map.get_calendar('C003', date(1993, 6, 4))
        self.assertEqual(ProvinceBeta, calendar_class)
        self.assertFalse(calendar_class().is_working_day(date(1993, 6, 4)))
        # Unrelated years are unchanged
        calendar_class = self.region_map.get_calendar('C003', date(2005, 6, 3))
        self.assertEqual(ProvinceAlpha, calendar_class)
        self.assertEqual(
            was_working_day,
            calendar_class().is_working_day(date(2005, 6, 3)))
        # The original source is unchanged
        self.assertEqual(source_before, self.iso_registry.region_registry)
        self.assertEqual(ProvinceAlpha, self.iso_registry.get('RA'))
        self.assertEqual(ProvinceBeta, self.iso_registry.get('RB'))
