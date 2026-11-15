import unittest

from webvtt.models import Caption, Region
from webvtt.quality import (
    Box, LineBox, Overlap, Viewport, overlap_index
    )


VIEWPORT = Viewport(1000, 500)
LINE_HEIGHT = 50


def caption(start, end, text='text', settings=None):
    return Caption(start, end, text, settings=settings)


def index(captions, **kwargs):
    kwargs.setdefault('viewport', VIEWPORT)
    kwargs.setdefault('line_height', LINE_HEIGHT)
    return overlap_index(captions, **kwargs)


class TestBox(unittest.TestCase):

    def test_intersection(self):
        first = Box(0, 0, 100, 100)
        second = Box(50, 50, 100, 100)
        self.assertEqual(first.intersection(second), Box(50, 50, 50, 50))

    def test_intersection_touching_edges(self):
        first = Box(0, 0, 100, 100)
        second = Box(100, 0, 100, 100)
        self.assertIsNone(first.intersection(second))

    def test_intersection_touching_corners(self):
        first = Box(0, 0, 100, 100)
        second = Box(100, 100, 100, 100)
        self.assertIsNone(first.intersection(second))

    def test_intersection_apart(self):
        first = Box(0, 0, 100, 100)
        second = Box(200, 0, 100, 100)
        self.assertIsNone(first.intersection(second))

    def test_str(self):
        self.assertEqual(str(Box(10, 20, 100.5, 50)), '10,20 100.5x50')


class TestOverlapIndex(unittest.TestCase):

    def test_no_captions(self):
        self.assertEqual(index([]), [])

    def test_percentage_and_line_number_share_coordinates(self):
        # line:10% of 500 pixels and line:1 of 50 pixels are the same 50
        captions = [
            caption('00:00:00.000', '00:00:02.000', settings='line:10%'),
            caption('00:00:01.000', '00:00:03.000', settings='line:1'),
            ]
        overlaps = index(captions)
        self.assertEqual(len(overlaps), 1)
        overlap = overlaps[0]
        self.assertEqual((overlap.first, overlap.second), (0, 1))
        self.assertEqual(str(overlap.start), '00:00:01.000')
        self.assertEqual(str(overlap.end), '00:00:02.000')
        self.assertEqual(overlap.area, Box(0, 50, 1000, 50))
        self.assertIsNone(overlap.region)

    def test_touching_edges_do_not_overlap(self):
        captions = [
            caption('00:00:00.000', '00:00:02.000',
                    settings='position:0%,line-left size:50%'),
            caption('00:00:00.000', '00:00:02.000',
                    settings='position:100%,line-right size:50%'),
            ]
        self.assertEqual(index(captions), [])

    def test_back_to_back_cues_at_the_same_position_do_not_overlap(self):
        settings = 'position:100%,line-right line:-1 align:right size:30%'
        captions = [
            caption('00:00:01.000', '00:00:02.000', settings=settings),
            caption('00:00:02.000', '00:00:03.000', settings=settings),
            ]
        self.assertEqual(index(captions), [])

    def test_cues_at_the_same_position_overlap(self):
        settings = 'position:100%,line-right line:-1 align:right size:30%'
        captions = [
            caption('00:00:01.000', '00:00:02.000', settings=settings),
            caption('00:00:01.500', '00:00:03.000', settings=settings),
            ]
        overlaps = index(captions)
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(str(overlaps[0].start), '00:00:01.500')
        self.assertEqual(str(overlaps[0].end), '00:00:02.000')
        self.assertEqual(overlaps[0].area, Box(700, 450, 300, 50))

    def test_vertical_cues_are_placed_on_their_own_axis(self):
        # a vertical cue takes line across and position along the viewport:
        # a column 50 pixels wide at the left, 250 pixels tall, centered
        captions = [
            caption('00:00:00.000', '00:00:02.000',
                    settings='vertical:rl line:0 size:50%'),
            caption('00:00:01.000', '00:00:03.000',
                    settings='line:2 position:0%,line-left size:10%'),
            ]
        overlaps = index(captions)
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0].area, Box(0, 125, 50, 25))

    def test_vertical_cues_stacked_from_the_right(self):
        # the default line of vertical:rl is the last column at the right
        captions = [
            caption('00:00:00.000', '00:00:02.000',
                    settings='vertical:rl size:100%'),
            caption('00:00:01.000', '00:00:03.000',
                    settings='position:100%,line-right line:-1 size:10%'),
            ]
        overlaps = index(captions)
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0].area, Box(950, 450, 50, 50))

    def test_regions_bound_the_cues(self):
        regions = [
            Region.from_string(
                'id:fred width:40% lines:3 '
                'regionanchor:0%,100% viewportanchor:10%,90%'
                ),
            Region.from_string(
                'id:bill width:40% lines:3 '
                'regionanchor:100%,100% viewportanchor:90%,90%'
                ),
            ]
        captions = [
            caption('00:00:00.000', '00:00:02.000', settings='region:fred'),
            caption('00:00:00.000', '00:00:02.000', settings='region:bill'),
            caption('00:00:01.000', '00:00:03.000', settings='region:fred'),
            ]
        overlaps = index(captions, regions=regions)
        # the regions only touch at x 500, so only the cues in fred overlap
        self.assertEqual(len(overlaps), 1)
        overlap = overlaps[0]
        self.assertEqual((overlap.first, overlap.second), (0, 2))
        self.assertEqual(overlap.region, 'fred')
        self.assertEqual(overlap.area, Box(100, 400, 400, 50))

    def test_cues_in_different_regions_may_overlap(self):
        regions = [
            Region.from_string(
                'id:top width:100% lines:6 '
                'regionanchor:0%,0% viewportanchor:0%,0%'
                ),
            Region.from_string(
                'id:bottom width:100% lines:6 '
                'regionanchor:0%,100% viewportanchor:0%,100%'
                ),
            ]
        captions = [
            caption('00:00:00.000', '00:00:02.000',
                    settings='region:top line:5'),
            caption('00:00:01.000', '00:00:03.000',
                    settings='region:bottom line:-5'),
            ]
        overlaps = index(captions, regions=regions)
        # both cover the line at y 250, but are not in the same region
        self.assertEqual(len(overlaps), 1)
        self.assertIsNone(overlaps[0].region)
        self.assertEqual(overlaps[0].area, Box(0, 250, 1000, 50))

    def test_measured_line_boxes_place_the_text_in_the_cue_box(self):
        captions = [
            caption('00:00:00.000', '00:00:02.000', settings='align:left'),
            caption('00:00:00.000', '00:00:02.000', settings='align:right'),
            ]
        measured = [[LineBox(100, 50)], [LineBox(100, 50)]]
        # the cue boxes cover the same line, but the text is at the
        # opposite ends of them
        self.assertEqual(index(captions, measured_lines=measured), [])

        captions[1].settings = 'align:left'
        overlaps = index(captions, measured_lines=measured)
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0].area, Box(0, 450, 100, 50))

    def test_measured_line_boxes_as_tuples(self):
        captions = [
            caption('00:00:00.000', '00:00:02.000', settings='align:left'),
            caption('00:00:00.000', '00:00:02.000', settings='align:left'),
            ]
        overlaps = index(captions, measured_lines=[[(100, 50)], [(80, 40)]])
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0].area, Box(0, 460, 80, 40))

    def test_measured_lines_must_be_parallel_to_the_captions(self):
        with self.assertRaises(ValueError):
            index([caption('00:00:00.000', '00:00:01.000')],
                  measured_lines=[])

    def test_overlaps_are_sorted_by_their_start(self):
        captions = [
            caption('00:00:04.000', '00:00:06.000', settings='line:0'),
            caption('00:00:00.000', '00:00:02.000', settings='line:0'),
            caption('00:00:01.000', '00:00:05.000', settings='line:0'),
            ]
        overlaps = index(captions)
        self.assertEqual(
            [(overlap.first, overlap.second) for overlap in overlaps],
            [(1, 2), (0, 2)]
            )

    def test_empty_captions_do_not_overlap(self):
        captions = [
            caption('00:00:00.000', '00:00:02.000', text=''),
            caption('00:00:00.000', '00:00:02.000'),
            ]
        self.assertEqual(index(captions), [])

    def test_defaults(self):
        captions = [
            caption('00:00:00.000', '00:00:02.000'),
            caption('00:00:01.000', '00:00:03.000'),
            ]
        overlaps = overlap_index(captions)
        self.assertEqual(len(overlaps), 1)
        line_height = 1080 * 0.0533
        self.assertEqual(
            overlaps[0].area,
            Box(0, 1080 - line_height, 1920, line_height)
            )

    def test_overlap_str(self):
        captions = [
            caption('00:00:00.000', '00:00:02.000'),
            caption('00:00:01.000', '00:00:03.000'),
            ]
        overlap = index(captions)[0]
        self.assertEqual(
            str(overlap),
            'captions 1 and 2 overlap 00:00:01.000 --> 00:00:02.000 '
            'in the viewport at 0,450 1000x50'
            )

    def test_overlap_str_with_region(self):
        overlap = Overlap(
            0, 1,
            caption('00:00:00.000', '00:00:01.000').start_time,
            caption('00:00:00.000', '00:00:01.000').end_time,
            'fred',
            Box(0, 0, 10, 10)
            )
        self.assertIn('in fred at', str(overlap))


if __name__ == '__main__':
    unittest.main()
