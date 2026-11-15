"""Tests of the line exposure of captions rendered in regions."""

import unittest

from webvtt import Caption, Region, WebVTT
from webvtt.exposure import (
    DEFAULT_REGION_LINES,
    LineExposure,
    RegionExposure,
    line_exposures,
    region_exposure,
    region_exposures
    )
from webvtt.errors import MalformedCaptionError


def caption(start, end, text, region=None, **settings):
    """Return a caption in a region, with the times in milliseconds."""
    settings = ' '.join(
        [f'region:{region}'] if region is not None else []
        + [f'{name}:{value}' for name, value in settings.items()]
        )
    return Caption(start, end, text, settings=settings)


def intervals(exposures):
    """Return the visible intervals of lines as milliseconds."""
    return [
        (exposure.start_in_milliseconds, exposure.end_in_milliseconds,
         exposure.exit, exposure.caption_index, exposure.line_index)
        for exposure in exposures
        ]


class TestLineExposure(unittest.TestCase):

    def test_instantiation(self):
        exposure = LineExposure(
            'fred', 0, 0, 'one', 1000, 4000, 'scroll', 4000, 1
            )
        self.assertEqual(exposure.region_id, 'fred')
        self.assertEqual(exposure.caption_index, 0)
        self.assertEqual(exposure.line_index, 0)
        self.assertEqual(exposure.text, 'one')
        self.assertEqual(exposure.start_in_milliseconds, 1000)
        self.assertEqual(exposure.end_in_milliseconds, 4000)
        self.assertEqual(exposure.exit_reason, 'scroll')
        self.assertEqual(exposure.exit_in_milliseconds, 4000)
        self.assertEqual(exposure.exit_caption_index, 1)

    def test_times_as_timestamps(self):
        exposure = LineExposure(
            'fred', 0, 0, 'one', 0, 3000, 'end', 3000
            )
        self.assertEqual(exposure.start, '00:00:00.000')
        self.assertEqual(exposure.end, '00:00:03.000')
        self.assertEqual(str(exposure.start_time), '00:00:00.000')
        self.assertEqual(str(exposure.end_time), '00:00:03.000')
        self.assertEqual(str(exposure.exit_time), '00:00:03.000')

    def test_durations(self):
        exposure = LineExposure(
            'fred', 0, 0, 'one', 1000, 3500, 'end', 3500
            )
        self.assertEqual(exposure.duration_in_milliseconds, 2500)
        self.assertEqual(exposure.duration, 2.5)

    def test_duration_never_negative(self):
        exposure = LineExposure(
            'fred', 0, 0, 'one', 5000, 4000, 'overflow', 5000
            )
        self.assertEqual(exposure.duration, 0)
        self.assertFalse(exposure.visible)

    def test_exit_is_window_when_cut_short(self):
        exposure = LineExposure(
            'fred', 0, 0, 'one', 4000, 6000, 'end', 8000
            )
        self.assertEqual(exposure.exit, 'window')
        self.assertEqual(exposure.event, 'window')

    def test_displaced_by_only_for_scroll(self):
        scrolled = LineExposure('fred', 0, 0, 'a', 0, 3000, 'scroll', 3000, 1)
        ended = LineExposure('fred', 0, 0, 'a', 0, 3000, 'end', 3000, 0)
        self.assertEqual(scrolled.displaced_by, 1)
        self.assertIsNone(ended.displaced_by)

    def test_to_dict(self):
        exposure = LineExposure(
            'fred', 1, 0, 'two', 3000, 8000, 'end', 8000, 1
            )
        self.assertEqual(
            exposure.to_dict(),
            {
                'region_id': 'fred',
                'caption_index': 1,
                'line_index': 0,
                'text': 'two',
                'start': '00:00:03.000',
                'end': '00:00:08.000',
                'start_in_milliseconds': 3000,
                'end_in_milliseconds': 8000,
                'exit': 'end',
                'exit_reason': 'end',
                'exit_time': '00:00:08.000',
                'exit_in_milliseconds': 8000,
                'exit_caption_index': 1,
                'duration': 5.0,
                'line_count': None,
                }
            )


class TestRegionExposure(unittest.TestCase):

    def test_iteration_and_length(self):
        report = RegionExposure(
            'fred', 2, True,
            [LineExposure('fred', 0, 0, 'a', 0, 1000, 'end', 1000),
             LineExposure('fred', 0, 1, 'b', 0, 1000, 'end', 1000)],
            []
            )
        self.assertEqual(len(report), 2)
        self.assertEqual(
            [exposure.text for exposure in report], ['a', 'b']
            )

    def test_total_duration(self):
        report = RegionExposure(
            'fred', 2, True,
            [LineExposure('fred', 0, 0, 'a', 0, 1000, 'end', 1000),
             LineExposure('fred', 0, 1, 'b', 0, 2500, 'end', 2500)],
            []
            )
        self.assertEqual(report.total_duration_in_milliseconds, 3500)
        self.assertEqual(report.total_duration, 3.5)

    def test_maximum_and_limit_without_window(self):
        report = RegionExposure('fred', 2, True, [], [])
        self.assertIsNone(report.window_duration)
        self.assertIsNone(report.maximum_duration)
        self.assertTrue(report.within_limit)

    def test_maximum_and_limit_with_window(self):
        report = RegionExposure(
            'fred', 2, True,
            [LineExposure('fred', 0, 0, 'a', 0, 4000, 'window', 4000),
             LineExposure('fred', 0, 1, 'b', 0, 4000, 'window', 4000)],
            [],
            0,
            4000
            )
        self.assertEqual(report.window_duration, 4)
        self.assertEqual(report.window_duration_in_milliseconds, 4000)
        self.assertEqual(report.maximum_duration, 8)
        self.assertEqual(report.maximum_duration_in_milliseconds, 8000)
        self.assertTrue(report.within_limit)

    def test_limit_is_enforced(self):
        report = RegionExposure(
            'fred', 1, True,
            [LineExposure('fred', 0, 0, 'a', 0, 5000, 'window', 5000),
             LineExposure('fred', 1, 0, 'b', 5000, 10000, 'window', 10000)],
            [],
            0,
            3000
            )
        self.assertEqual(report.maximum_duration_in_milliseconds, 3000)
        self.assertFalse(report.within_limit)

    def test_to_dict(self):
        exposure = LineExposure(
            'fred', 0, 0, 'one', 0, 1000, 'end', 1000, 0
            )
        report = RegionExposure('fred', 2, True, [exposure], [], 0, 1000)
        data = report.to_dict()
        self.assertEqual(data['region_id'], 'fred')
        self.assertEqual(data['lines'], 2)
        self.assertTrue(data['scroll'])
        self.assertEqual(data['exposures'], [exposure.to_dict()])
        self.assertEqual(data['overflows'], [])
        self.assertEqual(data['window_start_in_milliseconds'], 0)
        self.assertEqual(data['window_end_in_milliseconds'], 1000)
        self.assertTrue(data['within_limit'])


class TestPushOut(unittest.TestCase):
    """The scenario of a ten-second caption pushed out after three."""

    def setUp(self):
        self.region = Region(id='fred', lines='1', scroll='up')
        self.captions = [
            caption('00:00:00.000', '00:00:10.000', 'first', 'fred'),
            caption('00:00:03.000', '00:00:08.000', 'second', 'fred'),
            ]

    def test_first_line_is_pushed_out_at_the_next_caption(self):
        report = region_exposure(self.region, self.captions)
        self.assertEqual(
            intervals(report),
            [(0, 3000, 'scroll', 0, 0),
             (3000, 8000, 'end', 1, 0)]
            )

    def test_pushed_line_names_the_caption_that_pushed_it(self):
        report = region_exposure(self.region, self.captions)
        self.assertEqual(report.exposures[0].displaced_by, 1)
        self.assertEqual(
            report.exposures[0].exit_caption_index, 1
            )

    def test_exit_is_the_push_out_time(self):
        report = region_exposure(self.region, self.captions)
        self.assertEqual(report.exposures[0].exit, 'scroll')
        self.assertEqual(
            str(report.exposures[0].exit_time), '00:00:03.000'
            )

    def test_a_caption_bigger_than_the_region_scrolls_itself_out(self):
        region = Region(id='fred', lines='1', scroll='up')
        captions = [caption(
            '00:00:00.000', '00:00:05.000', ['a', 'b'], 'fred'
            )]
        report = region_exposure(region, captions)
        self.assertEqual(
            intervals(report),
            [(0, 0, 'scroll', 0, 0), (0, 5000, 'end', 0, 1)]
            )

    def test_lines_are_pushed_oldest_first(self):
        region = Region(id='fred', lines='2', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', ['a', 'b'], 'fred'),
            caption('00:00:02.000', '00:00:10.000', 'c', 'fred'),
            caption('00:00:04.000', '00:00:10.000', 'd', 'fred'),
            ]
        report = region_exposure(region, captions)
        self.assertEqual(
            intervals(report),
            [(0, 2000, 'scroll', 0, 0),
             (0, 4000, 'scroll', 0, 1),
             (2000, 10000, 'end', 1, 0),
             (4000, 10000, 'end', 2, 0)]
            )

    def test_a_new_caption_replaces_the_lines_that_end(self):
        region = Region(id='fred', lines='1', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:03.000', 'a', 'fred'),
            caption('00:00:03.000', '00:00:06.000', 'b', 'fred'),
            ]
        report = region_exposure(region, captions)
        self.assertEqual(
            intervals(report),
            [(0, 3000, 'end', 0, 0), (3000, 6000, 'end', 1, 0)]
            )

    def test_a_line_still_showing_is_not_pushed_by_a_caption_that_ends(self):
        region = Region(id='fred', lines='2', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', 'long', 'fred'),
            caption('00:00:04.000', '00:00:06.000', 'short', 'fred'),
            caption('00:00:08.000', '00:00:12.000', 'later', 'fred'),
            ]
        report = region_exposure(region, captions)
        self.assertEqual(
            intervals(report),
            [(0, 10000, 'end', 0, 0),
             (4000, 6000, 'end', 1, 0),
             (8000, 12000, 'end', 2, 0)]
            )


class TestNoScroll(unittest.TestCase):

    def test_new_lines_do_not_fit_and_are_not_visible(self):
        region = Region(id='fred', lines='2')
        captions = [
            caption('00:00:00.000', '00:00:10.000', ['a', 'b'], 'fred'),
            caption('00:00:03.000', '00:00:06.000', 'c', 'fred'),
            ]
        report = region_exposure(region, captions)
        self.assertEqual(
            intervals(report),
            [(0, 10000, 'end', 0, 0), (0, 10000, 'end', 0, 1)]
            )
        self.assertEqual(len(report.overflows), 1)
        self.assertEqual(report.overflows[0].caption_index, 1)
        self.assertEqual(report.overflows[0].exit, 'overflow')
        self.assertEqual(report.overflows[0].duration, 0)

    def test_lines_fit_again_when_earlier_ones_end(self):
        region = Region(id='fred', lines='1')
        captions = [
            caption('00:00:00.000', '00:00:02.000', 'a', 'fred'),
            caption('00:00:01.000', '00:00:03.000', 'b', 'fred'),
            caption('00:00:04.000', '00:00:06.000', 'c', 'fred'),
            ]
        report = region_exposure(region, captions)
        self.assertEqual(
            intervals(report),
            [(0, 2000, 'end', 0, 0), (4000, 6000, 'end', 2, 0)]
            )
        self.assertEqual(
            [(overflow.caption_index, overflow.exit)
             for overflow in report.overflows],
            [(1, 'overflow')]
            )


class TestSameTime(unittest.TestCase):

    def test_captions_at_the_same_time_keep_document_order(self):
        region = Region(id='fred', lines='1', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', 'first', 'fred'),
            caption('00:00:00.000', '00:00:10.000', 'second', 'fred'),
            ]
        report = region_exposure(region, captions)
        self.assertEqual(
            intervals(report),
            [(0, 0, 'scroll', 0, 0), (0, 10000, 'end', 1, 0)]
            )

    def test_the_later_caption_in_the_document_pushes_first(self):
        region = Region(id='fred', lines='2', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', 'first', 'fred'),
            caption('00:00:02.000', '00:00:10.000', 'second', 'fred'),
            caption('00:00:02.000', '00:00:10.000', 'third', 'fred'),
            ]
        report = region_exposure(region, captions)
        self.assertEqual(
            intervals(report),
            [(0, 2000, 'scroll', 0, 0),
             (2000, 10000, 'end', 1, 0),
             (2000, 10000, 'end', 2, 0)]
            )

    def test_the_order_is_by_document_not_by_text(self):
        region = Region(id='fred', lines='1', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', 'zzz', 'fred'),
            caption('00:00:00.000', '00:00:10.000', 'aaa', 'fred'),
            ]
        report = region_exposure(region, captions)
        self.assertEqual(
            [(exposure.caption_index, exposure.text)
             for exposure in report.exposures],
            [(0, 'zzz'), (1, 'aaa')]
            )


class TestRegionsApart(unittest.TestCase):

    def test_another_region_does_not_push_this_one(self):
        regions = [
            Region(id='fred', lines='1', scroll='up'),
            Region(id='bill', lines='1', scroll='up'),
            ]
        captions = [
            caption('00:00:00.000', '00:00:10.000', 'fred', 'fred'),
            caption('00:00:02.000', '00:00:09.000', ['bill 1', 'bill 2'],
                    'bill'),
            ]
        reports = region_exposures(captions, regions)
        self.assertEqual(
            [(report.region_id, intervals(report)) for report in reports],
            [('fred', [(0, 10000, 'end', 0, 0)]),
             ('bill', [(2000, 2000, 'scroll', 1, 0),
                       (2000, 9000, 'end', 1, 1)])]
            )

    def test_captions_without_a_region_are_left_out(self):
        region = Region(id='fred', lines='1', scroll='up')
        captions = [
            Caption('00:00:00.000', '00:00:10.000', 'no region'),
            caption('00:00:01.000', '00:00:10.000', 'in region', 'fred'),
            ]
        report = region_exposure(region, captions)
        self.assertEqual(
            intervals(report), [(1000, 10000, 'end', 1, 0)]
            )

    def test_an_undefined_region_shows_nothing(self):
        region = Region(id='fred', lines='1', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', 'text', 'bill'),
            ]
        self.assertEqual(region_exposure(region, captions).exposures, [])

    def test_duplicate_region_settings_are_reported_once(self):
        region = Region(id='fred', lines='1', scroll='up')
        reports = region_exposures(
            [caption('00:00:00.000', '00:00:10.000', 'text', 'fred')],
            [region, Region(id='fred', lines='2', scroll='up')]
            )
        self.assertEqual([report.region_id for report in reports], ['fred'])

    def test_a_region_without_identifier_shows_nothing(self):
        report = region_exposure(
            Region(lines='1'),
            [caption('00:00:00.000', '00:00:10.000', 'text', None)]
            )
        self.assertEqual(report.region_id, '')
        self.assertEqual(report.lines, 1)
        self.assertEqual(report.exposures, [])


class TestDefaults(unittest.TestCase):

    def test_default_number_of_lines(self):
        region = Region(id='fred')
        captions = [
            caption('00:00:00.000', '00:00:10.000', ['a', 'b', 'c'], 'fred'),
            caption('00:00:01.000', '00:00:10.000', 'd', 'fred'),
            ]
        report = region_exposure(region, captions)
        self.assertEqual(report.lines, DEFAULT_REGION_LINES)
        self.assertEqual(report.lines, 3)
        self.assertFalse(report.scrolling)
        self.assertEqual(
            intervals(report),
            [(0, 10000, 'end', 0, 0),
             (0, 10000, 'end', 0, 1),
             (0, 10000, 'end', 0, 2)]
            )
        self.assertEqual(len(report.overflows), 1)

    def test_zero_lines_shows_nothing(self):
        region = Region(id='fred', lines='0', scroll='up')
        captions = [caption('00:00:00.000', '00:00:10.000', 'a', 'fred')]
        report = region_exposure(region, captions)
        self.assertEqual(
            intervals(report), [(0, 0, 'scroll', 0, 0)]
            )

    def test_captions_that_end_before_they_start_show_nothing(self):
        region = Region(id='fred', lines='1', scroll='up')
        captions = [
            caption('00:00:05.000', '00:00:04.000', 'backwards', 'fred'),
            ]
        self.assertEqual(region_exposure(region, captions).exposures, [])

    def test_captions_without_lines_show_nothing(self):
        region = Region(id='fred', lines='1', scroll='up')
        captions = [caption('00:00:00.000', '00:00:10.000', '', 'fred')]
        self.assertEqual(region_exposure(region, captions).exposures, [])


class TestLineCounts(unittest.TestCase):

    def test_lines_can_be_given_per_caption(self):
        region = Region(id='fred', lines='3', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', ['a', 'b', 'c'], 'fred'),
            caption('00:00:01.000', '00:00:10.000', 'd', 'fred'),
            ]
        report = region_exposure(region, captions)
        self.assertEqual(
            intervals(report),
            [(0, 1000, 'scroll', 0, 0),
             (0, 10000, 'end', 0, 1),
             (0, 10000, 'end', 0, 2),
             (1000, 10000, 'end', 1, 0)]
            )

    def test_a_wrapped_line_takes_more_room(self):
        region = Region(id='fred', lines='2', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', 'first', 'fred'),
            caption('00:00:03.000', '00:00:10.000', ['second', 'more'],
                    'fred'),
            ]
        report = region_exposure(
            region, captions, line_counts=[1, 2]
            )
        self.assertEqual(
            intervals(report),
            [(0, 3000, 'scroll', 0, 0),
             (3000, 10000, 'end', 1, 0),
             (3000, 10000, 'end', 1, 1)]
            )

    def test_a_mapping_gives_counts_by_index(self):
        region = Region(id='fred', lines='2', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', 'first', 'fred'),
            caption('00:00:03.000', '00:00:10.000', 'second', 'fred'),
            ]
        report = region_exposure(
            region, captions, line_counts={1: 2}
            )
        self.assertEqual(
            intervals(report),
            [(0, 3000, 'scroll', 0, 0),
             (3000, 10000, 'end', 1, 0),
             (3000, 10000, 'end', 1, 1)]
            )

    def test_a_function_gives_counts_per_caption(self):
        region = Region(id='fred', lines='2', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', 'first', 'fred'),
            caption('00:00:03.000', '00:00:10.000', 'second', 'fred'),
            ]
        report = region_exposure(
            region, captions,
            line_counts=lambda item: len(item.lines) + 1
            )
        self.assertEqual(
            intervals(report),
            [(0, 3000, 'scroll', 0, 0),
             (0, 3000, 'scroll', 0, 1),
             (3000, 10000, 'end', 1, 0),
             (3000, 10000, 'end', 1, 1)]
            )

    def test_a_caption_that_takes_no_line_is_not_shown(self):
        region = Region(id='fred', lines='1', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', 'a', 'fred'),
            caption('00:00:03.000', '00:00:10.000', 'b', 'fred'),
            ]
        report = region_exposure(
            region, captions, line_counts=[1, 0]
            )
        self.assertEqual(
            intervals(report), [(0, 10000, 'end', 0, 0)]
            )

    def test_a_count_that_is_not_an_integer_is_an_error(self):
        for count in ('2', 2.0, None, True):
            with self.subTest(count=count):
                with self.assertRaises(TypeError):
                    region_exposure(
                        Region(id='fred'),
                        [caption('00:00:00.000', '00:00:10.000', 'a')],
                        line_counts=[count]
                        )

    def test_a_negative_count_is_an_error(self):
        with self.assertRaises(ValueError):
            region_exposure(
                Region(id='fred'),
                [caption('00:00:00.000', '00:00:10.000', 'a')],
                line_counts=[-1]
                )

    def test_too_few_counts_is_an_error(self):
        with self.assertRaises(ValueError):
            region_exposure(
                Region(id='fred'),
                [caption('00:00:00.000', '00:00:10.000', 'a')],
                line_counts=[]
                )

    def test_counts_of_captions_in_other_regions_are_checked(self):
        # the counts are given for every caption, not only those of a region
        with self.assertRaises(ValueError):
            region_exposures(
                [caption('00:00:00.000', '00:00:10.000', 'a', 'bill')],
                [Region(id='fred')],
                line_counts=[]
                )

    def test_a_bool_is_not_a_count(self):
        with self.assertRaises(TypeError):
            region_exposure(
                Region(id='fred'),
                [caption('00:00:00.000', '00:00:10.000', 'a')],
                line_counts=[True]
                )


class TestWindow(unittest.TestCase):

    def setUp(self):
        self.region = Region(id='fred', lines='1', scroll='up')
        self.captions = [
            caption('00:00:00.000', '00:00:10.000', 'first', 'fred'),
            caption('00:00:03.000', '00:00:08.000', 'second', 'fred'),
            ]

    def test_a_window_cuts_the_lines_short(self):
        report = region_exposure(
            self.region, self.captions,
            start='00:00:01.000', end='00:00:05.000'
            )
        self.assertEqual(
            intervals(report),
            [(1000, 3000, 'scroll', 0, 0),
             (3000, 5000, 'window', 1, 0)]
            )

    def test_the_exit_reason_is_kept_outside_the_window(self):
        report = region_exposure(
            self.region, self.captions,
            start='00:00:01.000', end='00:00:05.000'
            )
        self.assertEqual(report.exposures[1].exit, 'window')
        self.assertEqual(report.exposures[1].exit_reason, 'end')
        self.assertEqual(
            report.exposures[1].exit_in_milliseconds, 8000
            )

    def test_a_line_that_ends_before_the_window_is_left_out(self):
        report = region_exposure(
            self.region, self.captions,
            start='00:00:03.000', end='00:00:06.000'
            )
        self.assertEqual(
            intervals(report), [(3000, 6000, 'window', 1, 0)]
            )

    def test_a_line_starting_after_the_window_is_left_out(self):
        report = region_exposure(
            self.region, self.captions,
            start='00:00:00.000', end='00:00:02.000'
            )
        self.assertEqual(
            intervals(report), [(0, 2000, 'window', 0, 0)]
            )

    def test_a_line_touching_the_end_is_left_out(self):
        report = region_exposure(
            self.region, [self.captions[0]],
            start='00:00:05.000', end='00:00:10.000'
            )
        self.assertEqual(intervals(report), [(5000, 10000, 'end', 0, 0)])

    def test_a_line_ending_at_the_start_is_left_out(self):
        report = region_exposure(
            self.region, [self.captions[0]],
            start='00:00:03.000', end='00:00:10.000'
            )
        self.assertEqual(intervals(report), [(3000, 10000, 'end', 0, 0)])

    def test_a_window_without_start_or_end(self):
        report = region_exposure(
            self.region, self.captions, start='00:00:01.000'
            )
        self.assertIsNone(report.end_in_milliseconds)
        self.assertEqual(
            intervals(report),
            [(1000, 3000, 'scroll', 0, 0), (3000, 8000, 'end', 1, 0)]
            )

    def test_times_can_be_numbers_or_timestamps(self):
        from webvtt.models import Timestamp
        for start, end in ((1, 5), (Timestamp.from_string('00:00:01.000'),
                                    5)):
            with self.subTest(start=start):
                report = region_exposure(
                    self.region, self.captions, start=start, end=end
                    )
                self.assertEqual(
                    intervals(report),
                    [(1000, 3000, 'scroll', 0, 0),
                     (3000, 5000, 'window', 1, 0)]
                    )

    def test_a_start_after_the_end_is_an_error(self):
        with self.assertRaises(ValueError):
            region_exposure(
                self.region, self.captions,
                start='00:00:05.000', end='00:00:01.000'
                )

    def test_an_invalid_time_is_an_error(self):
        with self.assertRaises(MalformedCaptionError):
            region_exposure(self.region, self.captions, start='bad')

    def test_the_window_times_are_kept(self):
        report = region_exposure(
            self.region, self.captions,
            start='00:00:01.000', end='00:00:05.000'
            )
        self.assertEqual(report.start_in_milliseconds, 1000)
        self.assertEqual(report.end_in_milliseconds, 5000)


class TestConservation(unittest.TestCase):
    """The total exposure cannot exceed the lines times the window."""

    def check(self, captions, regions, start, end):
        for report in region_exposures(
                captions, regions, start=start, end=end):
            with self.subTest(region=report.region_id):
                self.assertTrue(report.within_limit)
                self.assertLessEqual(
                    report.total_duration_in_milliseconds,
                    report.maximum_duration_in_milliseconds
                    )

    def test_a_simple_window(self):
        self.check(
            [caption('00:00:00.000', '00:00:10.000', 'a', 'fred'),
             caption('00:00:03.000', '00:00:08.000', 'b', 'fred')],
            [Region(id='fred', lines='1', scroll='up')],
            0, 10000
            )

    def test_a_long_caption_with_a_scrolling_region(self):
        self.check(
            [caption('00:00:00.000', '00:01:00.000', 'a', 'fred'),
             caption('00:00:02.000', '00:00:58.000', ['b', 'c'], 'fred'),
             caption('00:00:04.000', '00:00:56.000', 'd', 'fred')],
            [Region(id='fred', lines='2', scroll='up')],
            0, 60000
            )

    def test_a_region_that_does_not_scroll(self):
        self.check(
            [caption('00:00:00.000', '00:00:10.000', ['a', 'b'], 'fred'),
             caption('00:00:01.000', '00:00:09.000', 'c', 'fred')],
            [Region(id='fred', lines='2')],
            0, 10000
            )

    def test_a_window_around_the_captions(self):
        self.check(
            [caption('00:00:00.000', '00:00:10.000', 'a', 'fred')],
            [Region(id='fred', lines='3', scroll='up')],
            2000, 4000
            )

    def test_captions_at_the_same_time(self):
        self.check(
            [caption('00:00:00.000', '00:00:10.000', 'a', 'fred'),
             caption('00:00:00.000', '00:00:10.000', ['b', 'c'], 'fred'),
             caption('00:00:00.000', '00:00:10.000', 'd', 'fred')],
            [Region(id='fred', lines='2', scroll='up')],
            0, 10000
            )

    def test_many_regions(self):
        self.check(
            [caption('00:00:00.000', '00:00:10.000', 'a', 'fred'),
             caption('00:00:00.000', '00:00:10.000', 'b', 'bill'),
             caption('00:00:03.000', '00:00:08.000', 'c', 'fred'),
             caption('00:00:02.000', '00:00:09.000', 'd', 'bill')],
            [Region(id='fred', lines='1', scroll='up'),
             Region(id='bill', lines='1', scroll='up')],
            0, 10000
            )

    def test_random_captions(self):
        import random
        rng = random.Random(1109)
        regions = [
            Region(id='fred', lines=str(rng.randint(0, 4)),
                   scroll='up' if rng.random() < 0.5 else None),
            Region(id='bill', lines=str(rng.randint(0, 4)),
                   scroll='up' if rng.random() < 0.5 else None),
            ]
        for _ in range(50):
            captions = []
            for _ in range(rng.randint(0, 8)):
                start = rng.randint(0, 15000)
                end = start + rng.randint(0, 10000)
                lines = rng.randint(0, 4)
                region = rng.choice(('fred', 'bill', None))
                captions.append(caption(
                    start, end,
                    [str(number) for number in range(lines)],
                    region
                    ))
            start = rng.randint(0, 5000)
            end = start + rng.randint(0, 20000)
            with self.subTest(start=start, end=end):
                self.check(captions, regions, start, end)


class TestChoosing(unittest.TestCase):

    def test_no_region_has_no_report(self):
        self.assertEqual(region_exposures([], []), [])

    def test_regions_are_in_the_order_given(self):
        regions = [Region(id='bill'), Region(id='fred')]
        caption('00:00:00.000', '00:00:10.000', 'a', 'fred')
        self.assertEqual(
            [report.region_id for report in region_exposures([], regions)],
            ['bill', 'fred']
            )

    def test_one_list_of_lines(self):
        regions = [Region(id='fred'), Region(id='bill')]
        captions = [
            caption('00:00:00.000', '00:00:10.000', 'a', 'fred'),
            caption('00:00:01.000', '00:00:10.000', 'b', 'bill'),
            caption('00:00:02.000', '00:00:10.000', 'c', 'fred'),
            ]
        self.assertEqual(
            [(exposure.region_id, exposure.caption_index,
              exposure.start_in_milliseconds)
             for exposure in line_exposures(captions, regions)],
            [('fred', 0, 0), ('fred', 2, 2000), ('bill', 1, 1000)]
            )


class TestWebVTT(unittest.TestCase):

    def setUp(self):
        self.vtt = WebVTT(captions=[
            caption('00:00:00.000', '00:00:10.000', 'fred', 'fred'),
            caption('00:00:02.000', '00:00:10.000', 'bill', 'bill'),
            caption('00:00:03.000', '00:00:08.000', 'next fred', 'fred'),
            ], regions=[
            Region(id='fred', lines='1', scroll='up'),
            Region(id='bill', lines='1', scroll='up'),
            ])

    def test_exposures(self):
        self.assertEqual(
            [report.region_id for report in self.vtt.region_exposures()],
            ['fred', 'bill']
            )

    def test_exposure_by_identifier(self):
        report = self.vtt.region_exposure('fred')
        self.assertEqual(
            intervals(report),
            [(0, 3000, 'scroll', 0, 0), (3000, 8000, 'end', 2, 0)]
            )

    def test_exposure_by_region(self):
        report = self.vtt.region_exposure(self.vtt.regions[1])
        self.assertEqual(
            intervals(report), [(2000, 10000, 'end', 1, 0)]
            )

    def test_undefined_region(self):
        with self.assertRaises(ValueError):
            self.vtt.region_exposure('missing')

    def test_lines_of_the_regions(self):
        self.assertEqual(
            [(exposure.region_id, exposure.caption_index)
             for exposure in self.vtt.line_exposures()],
            [('fred', 0), ('fred', 2), ('bill', 1)]
            )

    def test_window(self):
        report = self.vtt.region_exposure(
            'fred', start='00:00:01.000', end='00:00:04.000'
            )
        self.assertEqual(
            intervals(report),
            [(1000, 3000, 'scroll', 0, 0),
             (3000, 4000, 'window', 2, 0)]
            )


class TestWebVTTParse(unittest.TestCase):

    def test_exposure_of_parsed_regions(self):
        import webvtt
        vtt = webvtt.from_string(
            'WEBVTT\n\n'
            'REGION\nid:fred\nlines:1\nscroll:up\n\n'
            '00:00:00.000 --> 00:00:10.000 region:fred\nfirst\n\n'
            '00:00:03.000 --> 00:00:08.000 region:fred\nsecond\n'
            )
        report = vtt.region_exposure('fred')
        self.assertEqual(
            intervals(report),
            [(0, 3000, 'scroll', 0, 0), (3000, 8000, 'end', 1, 0)]
            )

    def test_exposure_of_parsed_region_without_lines(self):
        import webvtt
        vtt = webvtt.from_string(
            'WEBVTT\n\n'
            'REGION\nid:fred\nscroll:up\n\n'
            '00:00:00.000 --> 00:00:10.000 region:fred\na\n\n'
            '00:00:01.000 --> 00:00:10.000 region:fred\nb\n'
            )
        report = vtt.region_exposure('fred')
        self.assertEqual(report.lines, DEFAULT_REGION_LINES)
        self.assertEqual(
            [(exposure.caption_index, exposure.exit)
             for exposure in report.exposures],
            [(0, 'end'), (1, 'end')]
            )


class TestProperties(unittest.TestCase):

    def test_a_line_is_visible_until_it_leaves(self):
        region = Region(id='fred', lines='1', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', 'a', 'fred'),
            caption('00:00:03.000', '00:00:08.000', 'b', 'fred'),
            ]
        report = region_exposure(region, captions)
        for exposure in report.exposures:
            self.assertLessEqual(
                exposure.end_in_milliseconds,
                exposure.exit_in_milliseconds
                )
            self.assertLessEqual(
                exposure.start_in_milliseconds,
                exposure.end_in_milliseconds
                )

    def test_each_line_leaves_once(self):
        region = Region(id='fred', lines='2', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', ['a', 'b'], 'fred'),
            caption('00:00:02.000', '00:00:10.000', 'c', 'fred'),
            ]
        report = region_exposure(region, captions)
        self.assertEqual(len(report.exposures), 3)
        self.assertEqual(
            len({(exposure.caption_index, exposure.line_index)
                 for exposure in report.exposures}),
            3
            )

    def test_the_visible_interval_is_the_part_of_the_life_in_the_window(self):
        region = Region(id='fred', lines='1', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', 'a', 'fred'),
            ]
        report = region_exposure(
            region, captions, start='00:00:02.000', end='00:00:07.000'
            )
        exposure = report.exposures[0]
        self.assertEqual(exposure.start_in_milliseconds, 2000)
        self.assertEqual(exposure.end_in_milliseconds, 7000)
        self.assertEqual(exposure.exit_reason, 'end')

    def test_the_original_captions_are_not_changed(self):
        region = Region(id='fred', lines='1', scroll='up')
        captions = [
            caption('00:00:00.000', '00:00:10.000', 'a', 'fred'),
            caption('00:00:03.000', '00:00:08.000', 'b', 'fred'),
            ]
        before = [(item.start, item.end, item.lines) for item in captions]
        region_exposure(region, captions)
        self.assertEqual(
            [(item.start, item.end, item.lines) for item in captions],
            before
            )

    def test_text_of_the_lines_is_kept(self):
        region = Region(id='fred', lines='2', scroll='up')
        captions = [caption(
            '00:00:00.000', '00:00:10.000', ['<v Bob>Hi', 'there'], 'fred'
            )]
        report = region_exposure(region, captions)
        self.assertEqual(
            [exposure.text for exposure in report.exposures],
            ['<v Bob>Hi', 'there']
            )


if __name__ == '__main__':
    unittest.main()
