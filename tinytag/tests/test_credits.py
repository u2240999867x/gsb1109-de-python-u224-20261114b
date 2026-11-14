# SPDX-FileCopyrightText: 2014-2026 tinytag Contributors
# SPDX-License-Identifier: MIT

# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=missing-module-docstring

from unittest import TestCase

from tinytag import OtherFields, TinyTag
from tinytag.credits import (
    ALBUM_SCOPE, TRACK_SCOPE, Credit, CreditIndex, Credits, RoleSpec,
    extract_credits
)


def make_tag(**kwargs):
    tag = TinyTag()
    other = kwargs.pop('other', None)
    for key, value in kwargs.items():
        setattr(tag, key, value)
    if other:
        tag.other = OtherFields(other)
    return tag


class TestCredits(TestCase):

    def test_compilation_scopes(self):
        tag = make_tag(
            albumartist='Various Artists',
            artist='AC/DC',
            other={
                'accompaniment': ['The Rhythm Section'],
                'artistsort': ['AC/DC'],
            })
        credits = extract_credits(tag, source='vorbis')
        self.assertEqual(
            credits.album,
            (Credit('Various Artists', 'album_artist', ALBUM_SCOPE),))
        self.assertEqual(
            credits.track,
            (Credit('AC/DC', 'performer', TRACK_SCOPE, 'AC/DC'),
             Credit('The Rhythm Section', 'accompaniment', TRACK_SCOPE)))

    def test_primary_and_other_values_combine(self):
        tag = make_tag(artist='Main Artist',
                       other={'artist': ['Another Artist']})
        credits = extract_credits(tag)
        self.assertEqual(
            [credit.name for credit in credits.by_role('performer')],
            ['Main Artist', 'Another Artist'])

    def test_separator_source_splits_values(self):
        tag = make_tag(artist='Björk/Matmos')
        credits = extract_credits(tag, source='id3v2.3')
        self.assertEqual(
            [credit.name for credit in credits.by_role('performer')],
            ['Björk', 'Matmos'])

    def test_ac_dc_not_split(self):
        # Sources with native multi-value fields never split in-string
        for source in (None, 'vorbis', 'mp4', 'id3v2.4', 'wma'):
            tag = make_tag(artist='AC/DC')
            credits = extract_credits(tag, source=source)
            self.assertEqual(
                [credit.name for credit in credits.by_role('performer')],
                ['AC/DC'], source)
        # Even for sources with a slash convention, AC/DC is protected
        tag = make_tag(artist='AC/DC')
        credits = extract_credits(tag, source='id3v2.3')
        self.assertEqual(
            [credit.name for credit in credits.by_role('performer')],
            ['AC/DC'])
        # ... including when joined with other names
        tag = make_tag(artist='AC/DC/Brian Johnson')
        credits = extract_credits(tag, source='id3v2.3')
        self.assertEqual(
            [credit.name for credit in credits.by_role('performer')],
            ['AC/DC', 'Brian Johnson'])

    def test_sort_names_paired_when_counts_match(self):
        tag = make_tag(
            artist='Main Artist',
            other={
                'artist': ['Another Artist'],
                'artistsort': ['Artist, Main', 'Artist, Another'],
            })
        credits = extract_credits(tag)
        self.assertEqual(
            credits.by_role('performer'),
            (Credit('Main Artist', 'performer', TRACK_SCOPE, 'Artist, Main'),
             Credit('Another Artist', 'performer', TRACK_SCOPE,
                    'Artist, Another')))

    def test_sort_names_not_paired_when_counts_differ(self):
        for sort_values in (['Only One'], ['One', 'Two', 'Three']):
            tag = make_tag(
                artist='Main Artist',
                other={
                    'artist': ['Another Artist'],
                    'artistsort': sort_values,
                })
            credits = extract_credits(tag)
            self.assertEqual(
                credits.by_role('performer'),
                (Credit('Main Artist', 'performer', TRACK_SCOPE),
                 Credit('Another Artist', 'performer', TRACK_SCOPE)))

    def test_custom_roles(self):
        roles = (
            RoleSpec('remixer', TRACK_SCOPE, ('remixer',), ('remixersort',)),
        )
        tag = make_tag(other={'remixer': ['DJ'], 'remixersort': ['DJ, The']})
        credits = extract_credits(tag, roles=roles)
        self.assertEqual(
            credits.track,
            (Credit('DJ', 'remixer', TRACK_SCOPE, 'DJ, The'),))
        self.assertEqual(credits.album, ())

    def test_credits_container(self):
        credits = Credits(
            album=(Credit('Various Artists', 'album_artist', ALBUM_SCOPE),),
            track=(Credit('AC/DC', 'performer', TRACK_SCOPE),))
        self.assertEqual(len(list(credits)), 2)
        self.assertEqual(credits.by_scope(ALBUM_SCOPE), credits.album)
        self.assertEqual(credits.by_scope(TRACK_SCOPE), credits.track)
        self.assertEqual(credits.by_role('performer'), credits.track)
        with self.assertRaises(ValueError):
            credits.by_scope('unknown')

    def test_index_finds_tracks_not_compilation_album(self):
        index = CreditIndex()
        compilation_track = make_tag(
            albumartist='Various Artists', artist='AC/DC')
        other_track = make_tag(
            albumartist='Various Artists', artist='Someone Else')
        own_album_track = make_tag(albumartist='AC/DC', artist='AC/DC')
        index.add_tag('comp-1', compilation_track, album_id='compilation')
        index.add_tag('comp-2', other_track, album_id='compilation')
        index.add_tag('own-1', own_album_track, album_id='acdc-album')

        # The band is found through the tracks it performed on
        self.assertEqual(index.tracks('AC/DC'), {'comp-1', 'own-1'})
        # ... but the compilation is not listed among its albums
        self.assertEqual(index.albums('AC/DC'), {'acdc-album'})
        # The compilation belongs to the album artist only
        self.assertEqual(index.albums('Various Artists'), {'compilation'})
        self.assertEqual(index.tracks('Various Artists'), set())
        self.assertEqual(index.tracks('Someone Else'), {'comp-2'})
        self.assertEqual(
            index.names(),
            {'AC/DC', 'Various Artists', 'Someone Else'})
        self.assertIsNotNone(index.credits('comp-1'))
        self.assertIsNone(index.credits('missing'))

    def test_index_without_album_id(self):
        index = CreditIndex()
        credits = extract_credits(
            make_tag(albumartist='Various Artists', artist='AC/DC'))
        index.add('track-1', credits)
        self.assertEqual(index.tracks('AC/DC'), {'track-1'})
        self.assertEqual(index.albums('Various Artists'), set())
