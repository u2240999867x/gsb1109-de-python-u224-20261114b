import json
import pathlib
import tempfile
import unittest

from webvtt import packager
from webvtt.packager import (
    STATE_CONFIRMED_EMPTY,
    STATE_EMPTY,
    STATE_MISSING,
    STATE_READY,
    SegmentReceipt,
    SubtitleManifest
    )


def write_vtt(folder, name='en.vtt', text='a caption'):
    """Write a small WebVTT file and return its path."""
    path = folder / name
    path.write_text(
        'WEBVTT\n\n'
        '00:00:00.000 --> 00:00:01.000\n'
        f'{text}\n'
        )
    return path


def write_manifest(folder, data):
    """Write a JSON manifest file and return its path."""
    path = folder / 'manifest.json'
    path.write_text(json.dumps(data))
    return path


def receipt(status='no_dialogue'):
    """Return the text of a producer receipt."""
    return json.dumps({'status': status})


class ManifestTestCase(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_load_manifest(self):
        path = write_manifest(self.folder, {
            'show': 'show-1',
            'segment_count': 5,
            'required_languages': ['en', 'fr'],
            'optional_languages': ['de']
            })
        manifest = packager.load_manifest(path)
        self.assertEqual(manifest.show, 'show-1')
        self.assertEqual(manifest.segment_count, 5)
        self.assertEqual(manifest.required_languages, ('en', 'fr'))
        self.assertEqual(manifest.optional_languages, ('de',))
        self.assertEqual(manifest.languages, ('en', 'fr', 'de'))

    def test_manifest_without_optional_languages(self):
        path = write_manifest(self.folder, {
            'show': 'show-1',
            'segment_count': 2,
            'required_languages': ['en']
            })
        manifest = packager.load_manifest(path)
        self.assertEqual(manifest.optional_languages, ())
        self.assertEqual(manifest.languages, ('en',))

    def test_manifest_must_hold_an_object(self):
        path = write_manifest(self.folder, ['not', 'an', 'object'])
        with self.assertRaises(ValueError):
            packager.load_manifest(path)

    def test_manifest_requires_a_show(self):
        path = write_manifest(self.folder, {
            'segment_count': 2,
            'required_languages': ['en']
            })
        with self.assertRaises(ValueError):
            packager.load_manifest(path)

    def test_manifest_rejects_an_empty_show(self):
        path = write_manifest(self.folder, {
            'show': '',
            'segment_count': 2,
            'required_languages': ['en']
            })
        with self.assertRaises(ValueError):
            packager.load_manifest(path)

    def test_manifest_rejects_a_non_whole_segment_count(self):
        for count in (True, 2.5, '2'):
            path = write_manifest(self.folder, {
                'show': 'show-1',
                'segment_count': count,
                'required_languages': ['en']
                })
            with self.assertRaises(ValueError):
                packager.load_manifest(path)

    def test_manifest_rejects_a_segment_count_not_above_zero(self):
        path = write_manifest(self.folder, {
            'show': 'show-1',
            'segment_count': 0,
            'required_languages': ['en']
            })
        with self.assertRaises(ValueError):
            packager.load_manifest(path)

    def test_manifest_rejects_required_languages_that_are_not_a_list(self):
        path = write_manifest(self.folder, {
            'show': 'show-1',
            'segment_count': 2,
            'required_languages': 'en'
            })
        with self.assertRaises(ValueError):
            packager.load_manifest(path)

    def test_manifest_rejects_an_empty_required_language(self):
        path = write_manifest(self.folder, {
            'show': 'show-1',
            'segment_count': 2,
            'required_languages': ['en', '']
            })
        with self.assertRaises(ValueError):
            packager.load_manifest(path)

    def test_manifest_requires_at_least_one_required_language(self):
        path = write_manifest(self.folder, {
            'show': 'show-1',
            'segment_count': 2,
            'required_languages': []
            })
        with self.assertRaises(ValueError):
            packager.load_manifest(path)

    def test_manifest_rejects_repeated_languages(self):
        path = write_manifest(self.folder, {
            'show': 'show-1',
            'segment_count': 2,
            'required_languages': ['en', 'en']
            })
        with self.assertRaises(ValueError):
            packager.load_manifest(path)

    def test_manifest_rejects_a_language_both_required_and_optional(self):
        path = write_manifest(self.folder, {
            'show': 'show-1',
            'segment_count': 2,
            'required_languages': ['en'],
            'optional_languages': ['en']
            })
        with self.assertRaises(ValueError):
            packager.load_manifest(path)

    def test_manifest_rejects_bad_optional_languages(self):
        path = write_manifest(self.folder, {
            'show': 'show-1',
            'segment_count': 2,
            'required_languages': ['en'],
            'optional_languages': [42]
            })
        with self.assertRaises(ValueError):
            packager.load_manifest(path)


class ReceiptsTestCase(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.inbox = pathlib.Path(self.temp_dir.name) / 'inbox'
        self.inbox.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_load_receipts_requires_a_folder(self):
        with self.assertRaises(FileNotFoundError):
            packager.load_receipts(self.inbox / 'missing')

    def test_load_receipts_scans_segment_folders(self):
        first = self.inbox / 'segment1'
        second = self.inbox / 'segment2'
        first.mkdir()
        second.mkdir()
        write_vtt(first, 'en.vtt')
        write_vtt(second, 'fr.webvtt')
        receipts = packager.load_receipts(self.inbox)
        self.assertEqual(
            receipts,
            (
                SegmentReceipt(1, 'en', first / 'en.vtt', None),
                SegmentReceipt(2, 'fr', second / 'fr.webvtt', None)
                )
            )

    def test_load_receipts_ignores_files_and_unknown_folders(self):
        (self.inbox / 'notes.txt').write_text('ignore me')
        (self.inbox / 'segment1').mkdir()
        (self.inbox / 'segment1' / 'subfolder').mkdir()
        write_vtt(self.inbox / 'segment1', 'en.vtt')
        # a file with a segment-like name is not a folder
        (self.inbox / 'segment2').write_text('ignore me too')
        receipts = packager.load_receipts(self.inbox)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].segment, 1)

    def test_load_receipts_reads_a_no_dialogue_receipt(self):
        folder = self.inbox / 'segment1'
        folder.mkdir()
        receipt_path = folder / 'fr.receipt.json'
        receipt_path.write_text(receipt())
        receipts = packager.load_receipts(self.inbox)
        self.assertEqual(
            receipts,
            (SegmentReceipt(1, 'fr', None, receipt_path),)
            )

    def test_load_receipts_accepts_an_empty_file_with_a_receipt(self):
        folder = self.inbox / 'segment1'
        folder.mkdir()
        (folder / 'fr.vtt').write_bytes(b'')
        (folder / 'fr.receipt.json').write_text(receipt())
        receipts = packager.load_receipts(self.inbox)
        self.assertEqual(receipts[0].confirmation.name, 'fr.receipt.json')
        self.assertEqual(receipts[0].subtitle.name, 'fr.vtt')

    def test_load_receipts_accepts_a_header_only_file_with_a_receipt(self):
        folder = self.inbox / 'segment1'
        folder.mkdir()
        (folder / 'fr.vtt').write_text('WEBVTT\n')
        (folder / 'fr.receipt.json').write_text(receipt())
        receipts = packager.load_receipts(self.inbox)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].language, 'fr')

    def test_load_receipts_rejects_a_malformed_file_with_a_receipt(self):
        folder = self.inbox / 'segment1'
        folder.mkdir()
        (folder / 'fr.vtt').write_text('not a webvtt file')
        (folder / 'fr.receipt.json').write_text(receipt())
        with self.assertRaises(ValueError):
            packager.load_receipts(self.inbox)

    def test_load_receipts_rejects_a_receipt_with_content(self):
        folder = self.inbox / 'segment1'
        folder.mkdir()
        write_vtt(folder, 'fr.vtt', 'there is dialogue')
        (folder / 'fr.receipt.json').write_text(receipt())
        with self.assertRaises(ValueError):
            packager.load_receipts(self.inbox)

    def test_load_receipts_rejects_a_malformed_receipt(self):
        folder = self.inbox / 'segment1'
        folder.mkdir()
        (folder / 'fr.receipt.json').write_text('not json')
        with self.assertRaises(ValueError):
            packager.load_receipts(self.inbox)

    def test_load_receipts_rejects_a_receipt_with_another_status(self):
        folder = self.inbox / 'segment1'
        folder.mkdir()
        (folder / 'fr.receipt.json').write_text(receipt('pending'))
        with self.assertRaises(ValueError):
            packager.load_receipts(self.inbox)

    def test_load_receipts_rejects_a_receipt_without_a_language(self):
        folder = self.inbox / 'segment1'
        folder.mkdir()
        (folder / '.receipt.json').write_text(receipt())
        with self.assertRaises(ValueError):
            packager.load_receipts(self.inbox)

    def test_load_receipts_ignores_unrelated_files_in_a_segment_folder(self):
        folder = self.inbox / 'segment1'
        folder.mkdir()
        (folder / 'readme.txt').write_text('ignore me')
        write_vtt(folder, 'en.vtt')
        receipts = packager.load_receipts(self.inbox)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].language, 'en')

    def test_load_receipts_rejects_two_subtitles_for_a_language(self):
        folder = self.inbox / 'segment1'
        folder.mkdir()
        write_vtt(folder, 'fr.vtt')
        write_vtt(folder, 'fr.webvtt')
        with self.assertRaises(ValueError):
            packager.load_receipts(self.inbox)


class PackagerTestCase(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp_dir.name)
        self.inbox = root / 'inbox'
        self.output = root / 'output'
        self.inbox.mkdir()
        self.manifest = SubtitleManifest(
            'show-1', 5, ('en', 'fr'), ('de',)
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def segment(self, number):
        folder = self.inbox / f'segment{number}'
        folder.mkdir(exist_ok=True)
        return folder

    def confirm_no_dialogue(self, number, language):
        path = self.segment(number) / f'{language}.receipt.json'
        path.write_text(receipt())
        return path

    def pack(self, receipts=()):
        return packager.pack(
            self.inbox, self.output, self.manifest, receipts=receipts
            )

    def release_manifest(self, number):
        path = (
            self.output / 'releases'
            / f'release-{number:04d}' / 'manifest.json'
            )
        return json.loads(path.read_text())

    def test_publishes_the_longest_playable_prefix(self):
        for number in range(1, 6):
            write_vtt(self.segment(number), 'en.vtt', f'en {number}')
        write_vtt(self.segment(1), 'fr.vtt')
        write_vtt(self.segment(2), 'fr.vtt')
        # segment 3 has no French subtitles, so the prefix stops at 2
        result = self.pack()
        self.assertEqual(result.prefix, 2)
        self.assertTrue(result.created)
        self.assertEqual(result.release_number, 1)
        self.assertEqual(
            result.changes.added, (1, 2)
            )
        self.assertEqual(len(result.segments), 5)
        self.assertTrue(result.segments[0].playable)
        self.assertTrue(result.segments[1].playable)
        self.assertFalse(result.segments[2].playable)
        self.assertFalse(result.segments[3].playable)
        self.assertEqual(
            result.segments[2].languages['fr'].state, STATE_MISSING
            )

    def test_optional_language_does_not_block_the_release(self):
        for number in range(1, 4):
            write_vtt(self.segment(number), 'en.vtt')
            write_vtt(self.segment(number), 'fr.vtt')
        write_vtt(self.segment(1), 'de.vtt')
        result = self.pack()
        self.assertEqual(result.prefix, 3)
        self.assertEqual(
            result.segments[1].languages['de'].state, STATE_MISSING
            )
        self.assertEqual(
            result.segments[0].languages['de'].state, STATE_READY
            )

    def test_an_empty_file_without_a_receipt_blocks_the_prefix(self):
        write_vtt(self.segment(1), 'en.vtt')
        write_vtt(self.segment(1), 'fr.vtt')
        write_vtt(self.segment(2), 'en.vtt')
        (self.segment(2) / 'fr.vtt').write_bytes(b'')
        result = self.pack()
        self.assertEqual(result.prefix, 1)
        self.assertFalse(result.segments[1].playable)
        self.assertEqual(
            result.segments[1].languages['fr'].state, STATE_EMPTY
            )

    def test_a_header_only_file_without_a_receipt_blocks_the_prefix(self):
        write_vtt(self.segment(1), 'en.vtt')
        write_vtt(self.segment(1), 'fr.vtt')
        write_vtt(self.segment(2), 'en.vtt')
        (self.segment(2) / 'fr.vtt').write_text('WEBVTT\n')
        result = self.pack()
        self.assertEqual(result.prefix, 1)
        self.assertEqual(
            result.segments[1].languages['fr'].state, STATE_EMPTY
            )

    def test_a_whitespace_file_without_a_receipt_blocks_the_prefix(self):
        write_vtt(self.segment(1), 'en.vtt')
        write_vtt(self.segment(1), 'fr.vtt')
        write_vtt(self.segment(2), 'en.vtt')
        (self.segment(2) / 'fr.vtt').write_bytes(b'  \n\n')
        result = self.pack()
        self.assertEqual(result.prefix, 1)
        self.assertEqual(
            result.segments[1].languages['fr'].state, STATE_EMPTY
            )

    def test_a_malformed_subtitle_raises(self):
        write_vtt(self.segment(1), 'en.vtt')
        (self.segment(1) / 'fr.vtt').write_text('not a webvtt file')
        with self.assertRaises(ValueError):
            self.pack()

    def test_a_confirmed_no_dialogue_segment_can_play(self):
        write_vtt(self.segment(1), 'en.vtt')
        write_vtt(self.segment(1), 'fr.vtt')
        write_vtt(self.segment(2), 'en.vtt')
        self.confirm_no_dialogue(2, 'fr')
        (self.segment(2) / 'fr.vtt').write_bytes(b'')
        result = self.pack()
        self.assertEqual(result.prefix, 2)
        status = result.segments[1].languages['fr']
        self.assertEqual(status.state, STATE_CONFIRMED_EMPTY)

    def test_a_receipt_without_a_subtitle_file_can_play(self):
        write_vtt(self.segment(1), 'en.vtt')
        self.confirm_no_dialogue(1, 'fr')
        result = self.pack()
        self.assertEqual(result.prefix, 1)
        self.assertEqual(
            result.segments[0].languages['fr'].state,
            STATE_CONFIRMED_EMPTY
            )

    def test_rerun_with_the_same_inputs_creates_no_release(self):
        write_vtt(self.segment(1), 'en.vtt')
        write_vtt(self.segment(1), 'fr.vtt')
        first = self.pack()
        second = self.pack()
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.release_number, 1)
        self.assertEqual(second.path, first.path)
        self.assertEqual(len(list((self.output / 'releases').iterdir())), 1)

    def test_a_revision_creates_a_release_listing_the_replaced_segment(self):
        write_vtt(self.segment(1), 'en.vtt')
        write_vtt(self.segment(1), 'fr.vtt')
        write_vtt(self.segment(2), 'en.vtt')
        write_vtt(self.segment(2), 'fr.vtt', 'old translation')
        first = self.pack()
        self.assertEqual(first.prefix, 2)

        write_vtt(self.segment(2), 'fr.vtt', 'revised translation')
        second = self.pack()
        self.assertTrue(second.created)
        self.assertEqual(second.release_number, 2)
        self.assertEqual(second.changes.replaced, (2,))
        self.assertEqual(second.changes.carried_over, (1,))
        manifest = self.release_manifest(2)
        self.assertEqual(manifest['changes']['replaced'], [2])
        self.assertEqual(manifest['changes']['carried_over'], [1])

    def test_extending_the_prefix_lists_added_segments(self):
        write_vtt(self.segment(1), 'en.vtt')
        write_vtt(self.segment(1), 'fr.vtt')
        self.pack()
        write_vtt(self.segment(2), 'en.vtt')
        write_vtt(self.segment(2), 'fr.vtt')
        result = self.pack()
        self.assertEqual(result.prefix, 2)
        self.assertEqual(result.changes.added, (2,))
        self.assertEqual(result.changes.carried_over, (1,))
        self.assertEqual(result.release_number, 2)

    def test_a_shorter_prefix_lists_removed_segments(self):
        for number in range(1, 4):
            write_vtt(self.segment(number), 'en.vtt')
            write_vtt(self.segment(number), 'fr.vtt')
        self.pack()
        (self.segment(2) / 'fr.vtt').unlink()
        result = self.pack()
        self.assertEqual(result.prefix, 1)
        self.assertEqual(result.changes.removed, (2, 3))
        self.assertEqual(result.changes.carried_over, (1,))
        self.assertTrue(result.created)

    def test_no_playable_segment_creates_no_release(self):
        result = self.pack()
        self.assertEqual(result.prefix, 0)
        self.assertEqual(result.release_number, 0)
        self.assertIsNone(result.path)
        self.assertFalse(result.created)
        self.assertFalse((self.output / 'releases').exists())

    def test_rerun_without_any_release_stays_at_release_zero(self):
        self.pack()
        result = self.pack()
        self.assertEqual(result.release_number, 0)
        self.assertIsNone(result.path)
        self.assertFalse(result.created)

    def test_first_release_after_empty_runs_is_number_one(self):
        self.pack()
        write_vtt(self.segment(1), 'en.vtt')
        write_vtt(self.segment(1), 'fr.vtt')
        result = self.pack()
        self.assertEqual(result.release_number, 1)
        self.assertTrue(result.created)

    def test_pack_accepts_a_manifest_object_and_extra_receipts(self):
        write_vtt(self.segment(1), 'en.vtt')
        # the receipt is supplied out of band, not scanned from the inbox
        receipt_path = (
            pathlib.Path(self.temp_dir.name) / 'fr.receipt.json'
            )
        receipt_path.write_text(receipt())
        receipts = packager.load_receipts(self.inbox) + (
            SegmentReceipt(1, 'fr', None, receipt_path),
            )
        result = packager.pack_manifest(
            self.manifest, self.output, receipts
            )
        self.assertEqual(result.prefix, 1)

    def test_pack_accepts_a_manifest_path(self):
        root = pathlib.Path(self.temp_dir.name)
        manifest_path = write_manifest(root, {
            'show': 'show-1',
            'segment_count': 5,
            'required_languages': ['en', 'fr'],
            'optional_languages': ['de']
            })
        write_vtt(self.segment(1), 'en.vtt')
        write_vtt(self.segment(1), 'fr.vtt')
        result = packager.pack(self.inbox, self.output, manifest_path)
        self.assertEqual(result.prefix, 1)

    def test_receipt_outside_the_show_is_rejected(self):
        receipt = SegmentReceipt(6, 'en', None, None)
        with self.assertRaises(ValueError):
            packager.pack_manifest(
                self.manifest, self.output, (receipt,)
                )

    def test_receipt_for_an_unknown_language_is_rejected(self):
        receipt = SegmentReceipt(1, 'es', None, None)
        with self.assertRaises(ValueError):
            packager.pack_manifest(
                self.manifest, self.output, (receipt,)
                )

    def test_duplicate_receipt_is_rejected(self):
        path = write_vtt(self.segment(1), 'en.vtt')
        receipts = (
            SegmentReceipt(1, 'en', path, None),
            SegmentReceipt(1, 'en', path, None)
            )
        with self.assertRaises(ValueError):
            packager.pack_manifest(
                self.manifest, self.output, receipts
                )

    def test_release_contains_the_subtitle_files_and_manifest(self):
        write_vtt(self.segment(1), 'en.vtt', 'english')
        write_vtt(self.segment(1), 'fr.vtt', 'french')
        self.confirm_no_dialogue(1, 'de')
        self.pack()
        release = self.output / 'releases' / 'release-0001'
        self.assertEqual(
            (release / 'segment0001' / 'en.vtt').read_text(),
            (self.segment(1) / 'en.vtt').read_text()
            )
        self.assertTrue((release / 'segment0001' / 'fr.vtt').is_file())
        self.assertEqual(
            (release / 'segment0001' / 'de.vtt').read_bytes(),
            b'WEBVTT\n'
            )
        manifest = self.release_manifest(1)
        self.assertEqual(manifest['show'], 'show-1')
        self.assertEqual(manifest['prefix_segments'], 1)
        self.assertEqual(manifest['required_languages'], ['en', 'fr'])
        self.assertEqual(manifest['optional_languages'], ['de'])
        entry = manifest['segments'][0]['languages']
        self.assertEqual(entry['en']['state'], STATE_READY)
        self.assertEqual(entry['de']['state'], STATE_CONFIRMED_EMPTY)
        self.assertIn('sha256', entry['en'])
        self.assertIn('receipt', entry['de'])

    def test_old_release_keeps_the_old_content_after_a_revision(self):
        write_vtt(self.segment(1), 'en.vtt')
        write_vtt(self.segment(2), 'en.vtt')
        write_vtt(self.segment(1), 'fr.vtt')
        write_vtt(self.segment(2), 'fr.vtt', 'old translation')
        self.pack()
        old_file = (
            self.output / 'releases' / 'release-0001'
            / 'segment0002' / 'fr.vtt'
            )
        self.assertIn('old translation', old_file.read_text())

        write_vtt(self.segment(2), 'fr.vtt', 'revised translation')
        self.pack()
        self.assertIn('old translation', old_file.read_text())
        new_file = (
            self.output / 'releases' / 'release-0002'
            / 'segment0002' / 'fr.vtt'
            )
        self.assertIn('revised translation', new_file.read_text())


class RebuildTestCase(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp_dir.name)
        self.inbox = root / 'inbox'
        self.output = root / 'output'
        self.destination = root / 'rebuilt'
        self.inbox.mkdir()
        self.manifest = SubtitleManifest('show-1', 3, ('en', 'fr'))

    def tearDown(self):
        self.temp_dir.cleanup()

    def segment(self, number):
        folder = self.inbox / f'segment{number}'
        folder.mkdir(exist_ok=True)
        return folder

    def publish_two_releases(self):
        write_vtt(self.segment(1), 'en.vtt')
        write_vtt(self.segment(2), 'en.vtt')
        write_vtt(self.segment(1), 'fr.vtt')
        write_vtt(self.segment(2), 'fr.vtt', 'old translation')
        packager.pack(self.inbox, self.output, self.manifest)
        write_vtt(self.segment(2), 'fr.vtt', 'revised translation')
        packager.pack(self.inbox, self.output, self.manifest)

    def test_rebuild_without_state_raises(self):
        with self.assertRaises(FileNotFoundError):
            packager.rebuild_release(self.output, self.destination)

    def test_rebuild_without_any_release_raises(self):
        packager.pack(self.inbox, self.output, self.manifest)
        with self.assertRaises(ValueError):
            packager.rebuild_release(self.output, self.destination)

    def test_rebuild_unknown_release_raises(self):
        self.publish_two_releases()
        with self.assertRaises(ValueError):
            packager.rebuild_release(
                self.output, self.destination, release=9
                )

    def test_rebuild_latest_release_by_default(self):
        self.publish_two_releases()
        packager.rebuild_release(self.output, self.destination)
        content = (
            self.destination / 'segment0002' / 'fr.vtt'
            ).read_text()
        self.assertIn('revised translation', content)

    def test_rebuild_an_old_release_points_at_its_old_content(self):
        self.publish_two_releases()
        packager.rebuild_release(
            self.output, self.destination, release=1
            )
        content = (
            self.destination / 'segment0002' / 'fr.vtt'
            ).read_text()
        self.assertIn('old translation', content)
        self.assertNotIn('revised translation', content)

    def test_rebuild_regenerates_a_no_dialogue_file(self):
        write_vtt(self.segment(1), 'en.vtt')
        receipt_path = self.segment(1) / 'fr.receipt.json'
        receipt_path.write_text(receipt())
        packager.pack(self.inbox, self.output, self.manifest)
        packager.rebuild_release(self.output, self.destination)
        self.assertEqual(
            (self.destination / 'segment0001' / 'fr.vtt').read_bytes(),
            b'WEBVTT\n'
            )

    def test_rebuild_detects_a_missing_preserved_file(self):
        self.publish_two_releases()
        preserved = (
            self.output / 'releases' / 'release-0002'
            / 'segment0002' / 'fr.vtt'
            )
        preserved.unlink()
        with self.assertRaises(FileNotFoundError):
            packager.rebuild_release(self.output, self.destination)

    def test_rebuild_detects_a_changed_preserved_file(self):
        self.publish_two_releases()
        preserved = (
            self.output / 'releases' / 'release-0002'
            / 'segment0002' / 'fr.vtt'
            )
        preserved.write_bytes(preserved.read_bytes() + b'changed\n')
        with self.assertRaises(ValueError):
            packager.rebuild_release(self.output, self.destination)


if __name__ == '__main__':
    unittest.main()
