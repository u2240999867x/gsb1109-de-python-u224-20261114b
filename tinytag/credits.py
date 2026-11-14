# SPDX-FileCopyrightText: 2014-2026 tinytag Contributors
# SPDX-License-Identifier: MIT

"""Scoped attribution (credits) built from TinyTag metadata.

TinyTag exposes the primary value of a metadata field through its common
attributes (e.g. ``tag.artist``), and any additional values through the
``other`` dictionary (e.g. ``tag.other['artist']``). This module combines
both into structured, scoped attribution relationships ("credits"):

- Album-scoped credits describe who is responsible for the release as a
  whole (e.g. "Various Artists" on a compilation).
- Track-scoped credits describe who performed on an individual track
  (performers, accompaniment, composers, ...), so a compilation track is
  attributed to its own artists without re-attributing the whole album.

Multi-value separators are interpreted according to the convention of the
tagging source the values came from. Sources with native multi-value
fields (ID3v2.4, Vorbis comments, MP4, ASF/WMA, ...) never require
in-string splitting, so a band genuinely named "AC/DC" is kept intact.
Only sources whose convention joins values inside a single string
(ID3v2.2/2.3, which use "/") are split, and even then names listed in
``protected_names`` (e.g. "AC/DC") are never cut apart.

Sort names (e.g. "artistsort") are associated with a role's names only
when their counts match exactly, giving a unique positional
correspondence. Otherwise they are left unassociated.

The ``CreditIndex`` class provides search over extracted credits:
searching a name finds the tracks it is credited on, while albums are
only found through album-scoped credits, so a compilation album is not
listed among a guest performer's albums.
"""

from __future__ import annotations

from dataclasses import dataclass

from .tinytag import TinyTag

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Hashable, Iterable, Iterator
    from typing import Optional, Tuple

TRACK_SCOPE = 'track'
ALBUM_SCOPE = 'album'

#: In-string value separators per tagging source convention. Sources
#: with native multi-value fields map to None (no in-string splitting).
SOURCE_SEPARATORS: dict[str, str | None] = {
    'id3v2.2': '/',
    'id3v2.3': '/',
    'id3v2.4': None,
    'id3v1': None,
    'vorbis': None,
    'ogg': None,
    'opus': None,
    'flac': None,
    'mp4': None,
    'm4a': None,
    'asf': None,
    'wma': None,
    'ape': None,
    'wav': None,
    'aiff': None,
}

#: Names that legitimately contain a separator character and must never
#: be split, regardless of the source convention.
DEFAULT_PROTECTED_NAMES: frozenset[str] = frozenset({'AC/DC'})


@dataclass(frozen=True)
class Credit:
    """A single scoped attribution of a name to a role."""
    name: str
    role: str
    scope: str
    sort_name: str | None = None


@dataclass(frozen=True)
class RoleSpec:
    """Maps a credit role to the tag fields providing its names."""
    role: str
    scope: str
    fields: Tuple[str, ...]
    sort_fields: Tuple[str, ...] = ()


#: Default roles: album artist at album scope, performers, accompaniment,
#: composers and conductors at track scope.
DEFAULT_ROLES: Tuple[RoleSpec, ...] = (
    RoleSpec(
        'album_artist', ALBUM_SCOPE, ('albumartist',),
        ('albumartistsort', 'albumartist_sort', 'tso2')),
    RoleSpec(
        'performer', TRACK_SCOPE, ('artist',),
        ('artistsort', 'artist_sort', 'tsop')),
    RoleSpec(
        'accompaniment', TRACK_SCOPE, ('accompaniment', 'ensemble'),
        ('accompanimentsort', 'accompaniment_sort')),
    RoleSpec(
        'composer', TRACK_SCOPE, ('composer',),
        ('composersort', 'composer_sort', 'tsoc')),
    RoleSpec(
        'conductor', TRACK_SCOPE, ('conductor',),
        ('conductorsort', 'conductor_sort')),
)


@dataclass(frozen=True)
class Credits:
    """The scoped credits of a single track."""
    album: Tuple[Credit, ...] = ()
    track: Tuple[Credit, ...] = ()

    def __iter__(self) -> Iterator[Credit]:
        return iter(self.album + self.track)

    def by_scope(self, scope: str) -> Tuple[Credit, ...]:
        """Return credits for the given scope."""
        if scope == ALBUM_SCOPE:
            return self.album
        if scope == TRACK_SCOPE:
            return self.track
        raise ValueError(f'Unknown scope: {scope!r}')

    def by_role(self, role: str) -> Tuple[Credit, ...]:
        """Return credits for the given role."""
        return tuple(credit for credit in self if credit.role == role)


def _tag_values(tag: TinyTag, field_name: str) -> list[str]:
    values = []
    primary = getattr(tag, field_name, None)
    if isinstance(primary, str) and primary:
        values.append(primary)
    extra = tag.other.get(field_name)
    if extra:
        values.extend(value for value in extra if value)
    return values


def _split_names(values: Iterable[str], separator: str | None,
                 protected_names: frozenset[str]) -> list[str]:
    names = []
    for value in values:
        if separator is None or separator not in value:
            names.append(value)
            continue
        # Protect names that legitimately contain the separator
        # (e.g. "AC/DC") by masking them before splitting.
        masked = {}
        text = value
        for protected in protected_names:
            if separator in protected and protected in text:
                placeholder = f'\x00{len(masked)}\x00'
                text = text.replace(protected, placeholder)
                masked[placeholder] = protected
        for part in text.split(separator):
            part = part.strip()
            for placeholder, protected in masked.items():
                part = part.replace(placeholder, protected)
            if part:
                names.append(part)
    return names


def _dedupe(names: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for name in names:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _role_names(tag: TinyTag, fields: Iterable[str], separator: str | None,
                protected_names: frozenset[str]) -> list[str]:
    values = []
    for field_name in fields:
        values.extend(_tag_values(tag, field_name))
    return _dedupe(_split_names(values, separator, protected_names))


def extract_credits(
        tag: TinyTag,
        source: str | None = None,
        roles: Tuple[RoleSpec, ...] = DEFAULT_ROLES,
        protected_names: frozenset[str] | None = None) -> Credits:
    """Build scoped credits from a TinyTag's metadata.

    ``source`` names the tagging convention used to interpret in-string
    multi-value separators (see ``SOURCE_SEPARATORS``), e.g. 'id3v2.3'.
    Unknown or omitted sources default to no in-string splitting, which
    keeps names like "AC/DC" intact.
    """
    separator = None
    if source is not None:
        separator = SOURCE_SEPARATORS.get(source.lower())
    if protected_names is None:
        protected_names = DEFAULT_PROTECTED_NAMES
    album_credits = []
    track_credits = []
    for spec in roles:
        names = _role_names(tag, spec.fields, separator, protected_names)
        if not names:
            continue
        sort_names: list[Optional[str]] = []
        if spec.sort_fields:
            sort_names = _role_names(
                tag, spec.sort_fields, separator, protected_names)
        # Only associate sort names when they correspond to the role's
        # names one-to-one; otherwise the mapping would be ambiguous.
        if len(sort_names) != len(names):
            sort_names = [None] * len(names)
        target = album_credits if spec.scope == ALBUM_SCOPE else track_credits
        for name, sort_name in zip(names, sort_names):
            target.append(Credit(name, spec.role, spec.scope, sort_name))
    return Credits(tuple(album_credits), tuple(track_credits))


class CreditIndex:
    """A searchable index of scoped credits.

    Track-scoped credits make tracks findable by the credited name,
    while album-scoped credits make albums findable. A performer on a
    compilation is therefore linked to their tracks only, not to the
    compilation album itself.
    """

    def __init__(self) -> None:
        self._tracks_by_name: dict[str, set[Hashable]] = {}
        self._albums_by_name: dict[str, set[Hashable]] = {}
        self._credits_by_track: dict[Hashable, Credits] = {}

    def add(self, track_id: Hashable, credits: Credits,
            album_id: Hashable | None = None) -> None:
        """Add a track's credits to the index.

        ``album_id`` identifies the release the track belongs to, and is
        required for album-scoped credits to be indexed.
        """
        self._credits_by_track[track_id] = credits
        for credit in credits.track:
            self._tracks_by_name.setdefault(credit.name, set()).add(track_id)
        if album_id is not None:
            for credit in credits.album:
                self._albums_by_name.setdefault(
                    credit.name, set()).add(album_id)

    def add_tag(self, track_id: Hashable, tag: TinyTag,
                source: str | None = None,
                album_id: Hashable | None = None,
                roles: Tuple[RoleSpec, ...] = DEFAULT_ROLES) -> Credits:
        """Extract credits from a tag and add them to the index."""
        credits = extract_credits(tag, source=source, roles=roles)
        self.add(track_id, credits, album_id=album_id)
        return credits

    def tracks(self, name: str) -> frozenset[Hashable]:
        """Return the IDs of tracks the name is credited on."""
        return frozenset(self._tracks_by_name.get(name, ()))

    def albums(self, name: str) -> frozenset[Hashable]:
        """Return the IDs of albums the name holds album credits for."""
        return frozenset(self._albums_by_name.get(name, ()))

    def credits(self, track_id: Hashable) -> Credits | None:
        """Return the credits stored for a track, if any."""
        return self._credits_by_track.get(track_id)

    def names(self) -> frozenset[str]:
        """Return all credited names in the index."""
        return frozenset(self._tracks_by_name) | frozenset(
            self._albums_by_name)
