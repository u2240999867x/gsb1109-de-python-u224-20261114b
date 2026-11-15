"""Tests of the exposure command of the CLI."""

import contextlib
import io
import os
import pathlib
import tempfile
import unittest

from webvtt.cli import main


class CLIExposureTestCase(unittest.TestCase):

    CAPTIONS = (
        'WEBVTT\n'
        '\n'
        'REGION\n'
        'id:fred\n'
        'lines:1\n'
        'scroll:up\n'
        '\n'
        'REGION\n'
        'id:bill\n'
        'lines:2\n'
        '\n'
        '00:00:00.000 --> 00:00:10.000 region:fred\n'
        'first\n'
        '\n'
        '00:00:02.000 --> 00:00:09.000 region:bill\n'
        'other\n'
        '\n'
        '00:00:03.000 --> 00:00:08.000 region:fred\n'
        'second\n'
        )

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.vtt_file = os.path.join(self.temp_dir.name, 'captions.vtt')
        pathlib.Path(self.vtt_file).write_text(self.CAPTIONS)

    def run_cli(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            main(argv)
        return stdout.getvalue()

    def run_error(self, argv):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as cm:
                self.run_cli(argv)
        return cm.exception.code, stderr.getvalue()

    def test_exposure(self):
        self.assertEqual(
            self.run_cli(['exposure', self.vtt_file]),
            'fred\t0\t0\tfirst\t00:00:00.000\t00:00:03.000\tscroll\t'
            '00:00:03.000\t2\n'
            'fred\t2\t0\tsecond\t00:00:03.000\t00:00:08.000\tend\t'
            '00:00:08.000\t-\n'
            'bill\t1\t0\tother\t00:00:02.000\t00:00:09.000\tend\t'
            '00:00:09.000\t-\n'
            )

    def test_window(self):
        self.assertEqual(
            self.run_cli(['exposure', self.vtt_file,
                          '-w', '00:00:01.000', '00:00:04.000']),
            'fred\t0\t0\tfirst\t00:00:01.000\t00:00:03.000\tscroll\t'
            '00:00:03.000\t2\n'
            'fred\t2\t0\tsecond\t00:00:03.000\t00:00:04.000\twindow\t'
            '00:00:08.000\t-\n'
            'bill\t1\t0\tother\t00:00:02.000\t00:00:04.000\twindow\t'
            '00:00:09.000\t-\n'
            )

    def test_region(self):
        self.assertEqual(
            self.run_cli(['exposure', self.vtt_file, '-r', 'bill']),
            'bill\t1\t0\tother\t00:00:02.000\t00:00:09.000\tend\t'
            '00:00:09.000\t-\n'
            )

    def test_more_regions(self):
        output = self.run_cli([
            'exposure', self.vtt_file, '-r', 'bill', '-r', 'fred'
            ])
        self.assertIn('bill\t', output)
        self.assertIn('fred\t', output)

    def test_undefined_region(self):
        code, message = self.run_error([
            'exposure', self.vtt_file, '-r', 'missing'
            ])
        self.assertEqual(code, 1)
        self.assertIn("Region 'missing' is not defined", message)

    def test_line_counts(self):
        self.assertEqual(
            self.run_cli([
                'exposure', self.vtt_file,
                '--line-counts', '1', '--line-counts', '1',
                '--line-counts', '2', '-r', 'fred'
                ]),
            'fred\t0\t0\tfirst\t00:00:00.000\t00:00:03.000\tscroll\t'
            '00:00:03.000\t2\n'
            'fred\t2\t0\tsecond\t00:00:03.000\t00:00:03.000\tscroll\t'
            '00:00:03.000\t2\n'
            'fred\t2\t1\t\t00:00:03.000\t00:00:08.000\tend\t'
            '00:00:08.000\t-\n'
            )

    def test_too_few_line_counts(self):
        code, message = self.run_error([
            'exposure', self.vtt_file, '--line-counts', '1'
            ])
        self.assertEqual(code, 1)
        self.assertIn('fewer line counts than captions', message)

    def test_overflow_lines_are_left_out_by_default(self):
        path = os.path.join(self.temp_dir.name, 'overflow.vtt')
        pathlib.Path(path).write_text(
            'WEBVTT\n\n'
            'REGION\nid:fred\nlines:1\n\n'
            '00:00:00.000 --> 00:00:10.000 region:fred\na\n\n'
            '00:00:01.000 --> 00:00:10.000 region:fred\nb\n'
            )
        self.assertEqual(
            self.run_cli(['exposure', path]),
            'fred\t0\t0\ta\t00:00:00.000\t00:00:10.000\tend\t'
            '00:00:10.000\t-\n'
            )
        self.assertEqual(
            self.run_cli(['exposure', path, '--overflows']),
            'fred\t0\t0\ta\t00:00:00.000\t00:00:10.000\tend\t'
            '00:00:10.000\t-\n'
            'fred\t1\t0\tb\t00:00:01.000\t00:00:01.000\toverflow\t'
            '00:00:01.000\t-\n'
            )

    def test_tabs_in_the_text_are_replaced(self):
        path = os.path.join(self.temp_dir.name, 'tab.vtt')
        pathlib.Path(path).write_text(
            'WEBVTT\n\n'
            'REGION\nid:fred\n\n'
            '00:00:00.000 --> 00:00:10.000 region:fred\na\tb\n'
            )
        self.assertEqual(
            self.run_cli(['exposure', path]),
            'fred\t0\t0\ta b\t00:00:00.000\t00:00:10.000\tend\t'
            '00:00:10.000\t-\n'
            )

    def test_from_stdin(self):
        from unittest import mock
        import sys
        stdin = io.StringIO(self.CAPTIONS)
        with mock.patch.object(sys, 'stdin', stdin):
            self.assertEqual(
                self.run_cli(['exposure', '-', '-r', 'bill']),
                'bill\t1\t0\tother\t00:00:02.000\t00:00:09.000\tend\t'
                '00:00:09.000\t-\n'
                )

    def test_invalid_line_count(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as cm:
                main(['exposure', self.vtt_file, '--line-counts', '-1'])

        self.assertEqual(cm.exception.code, 2)
        self.assertIn('invalid number of lines', stderr.getvalue())

    def test_invalid_window(self):
        code, message = self.run_error([
            'exposure', self.vtt_file,
            '-w', '00:00:05.000', '00:00:01.000'
            ])
        self.assertEqual(code, 1)
        self.assertIn('start of the window is after its end', message)

    def test_no_regions(self):
        path = os.path.join(self.temp_dir.name, 'plain.vtt')
        pathlib.Path(path).write_text(
            'WEBVTT\n\n00:00:00.000 --> 00:00:10.000\nplain\n'
            )
        self.assertEqual(self.run_cli(['exposure', path]), '')
