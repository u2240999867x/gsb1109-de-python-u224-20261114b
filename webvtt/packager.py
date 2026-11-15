"""Offline packager for multilingual, segmented subtitle releases.

A show is split into segments, and the subtitles of each segment arrive
in several languages and at different times. The packager publishes the
longest consecutive prefix of segments that can be played: every required
language of each segment must have a subtitle, or a receipt from the
producer confirming that the segment has no dialogue. An empty file
without such a receipt keeps the prefix from going further. Optional
languages are added when they are ready and never hold a release back.

Every release is written to its own folder with a manifest listing the
files it contains and their hashes, so the release can be rebuilt later.
Re-running the packager with the same inputs does not create another
release: a new release is created only when the content of the playable
prefix changes, and its manifest says which segments were added,
replaced or removed.
"""

import datetime
import hashlib
import json
import pathlib
import re
from dataclasses import dataclass
from typing import Any, cast

from . import utils
from .errors import MalformedFileError
from .webvtt import WebVTT

# the file, in the output folder, that stores the state of the releases
_STATE_NAME = 'pack_manifest.json'

# the folder, in the output folder, that contains the release folders
_RELEASES_FOLDER = 'releases'

# the manifest of each release, inside its folder
_RELEASE_MANIFEST_NAME = 'manifest.json'

# the extensions accepted for a subtitle file
_SUBTITLE_EXTENSIONS = ('.vtt', '.webvtt')

# a sidecar with a receipt from the producer, next to the subtitle files
_RECEIPT_SUFFIX = '.receipt.json'

# the name of a segment folder, such as segment1
_SEGMENT_FOLDER_PATTERN = re.compile(r'segment([1-9][0-9]*)')

# the only receipt status the producer can confirm
_NO_DIALOGUE_STATUS = 'no_dialogue'

# the content written for a no-dialogue language without a subtitle file
_EMPTY_SUBTITLE = b'WEBVTT\n'

# the states of a language of a segment
STATE_READY = 'ready'
STATE_EMPTY = 'empty'
STATE_MISSING = 'missing'
STATE_CONFIRMED_EMPTY = 'confirmed_empty'

# the states that let a required language take part in a release
_PLAYABLE_STATES = frozenset((STATE_READY, STATE_CONFIRMED_EMPTY))


@dataclass(frozen=True)
class SegmentReceipt:
    """
    The subtitles or the producer receipt of a segment and a language.

    :param segment: the segment number, from one
    :param language: the language of the subtitles
    :param subtitle: the path of the subtitle file, or None if there is none
    :param confirmation: the path of the receipt confirming that the segment
        has no dialogue in the language, or None if there is no receipt
    """

    segment: int
    language: str
    subtitle: pathlib.Path | None = None
    confirmation: pathlib.Path | None = None


@dataclass(frozen=True)
class SubtitleManifest:
    """
    Description of the segments of a show and the languages they need.

    :param show: identifier of the show or episode
    :param segment_count: number of segments of the show, above zero
    :param required_languages: languages every released segment must have
        subtitles or a no-dialogue receipt for
    :param optional_languages: languages added to a release when their
        subtitles are ready, without holding it back when they are not
    """

    show: str
    segment_count: int
    required_languages: tuple[str, ...]
    optional_languages: tuple[str, ...] = ()

    @property
    def languages(self) -> tuple[str, ...]:
        """Return every declared language, required ones first."""
        return self.required_languages + self.optional_languages


@dataclass(frozen=True)
class LanguageStatus:
    """
    The state of a language of a segment when the release is planned.

    :param state: one of ``STATE_READY``, ``STATE_EMPTY``,
        ``STATE_MISSING`` or ``STATE_CONFIRMED_EMPTY``
    :param subtitle: the path of the subtitle file, or None
    :param confirmation: the path of the no-dialogue receipt, or None
    :param sha256: the hash of the bytes the release writes for the
        language, or None when it cannot play
    :param receipt_sha256: the hash of the receipt, or None
    """

    state: str
    subtitle: pathlib.Path | None
    confirmation: pathlib.Path | None
    sha256: str | None
    receipt_sha256: str | None


@dataclass(frozen=True)
class SegmentStatus:
    """
    The state of every language of a segment and whether it can play.

    :param number: the segment number, from one
    :param languages: the state of each declared language
    :param playable: whether the segment is in the playable prefix: every
        required language is ready or confirmed, and no earlier segment
        blocks it
    :param fingerprint: the hash of the content of the segment
    """

    number: int
    languages: dict[str, LanguageStatus]
    playable: bool
    fingerprint: str


@dataclass(frozen=True)
class ReleasePlan:
    """
    The playable prefix planned from a manifest and receipts.

    :param prefix: the number of consecutive segments that can play,
        zero when the first segment cannot
    :param segments: the state of every segment of the show
    :param fingerprint: the hash of the content of the playable prefix
    """

    prefix: int
    segments: tuple[SegmentStatus, ...]
    fingerprint: str


@dataclass(frozen=True)
class ReleaseChanges:
    """
    The segments changed by a release, compared with the previous one.

    :param added: segments in the release that were not in the previous one
    :param replaced: segments in both releases whose content changed
    :param removed: segments in the previous release that are not in this one
    :param carried_over: segments in both releases with the same content
    """

    added: tuple[int, ...]
    replaced: tuple[int, ...]
    removed: tuple[int, ...]
    carried_over: tuple[int, ...]


@dataclass(frozen=True)
class PackResult:
    """
    The result of running the packager.

    :param release_number: the number of the release describing the current
        inputs, the one created, the existing one with the same content, or
        zero when no segment can play
    :param path: the folder of the release, or None when no segment can play
    :param prefix: the number of consecutive segments that can play
    :param created: whether a new release folder was created
    :param changes: the segments changed since the previous release
    :param segments: the state of every segment of the show
    """

    release_number: int
    path: pathlib.Path | None
    prefix: int
    created: bool
    changes: ReleaseChanges
    segments: tuple[SegmentStatus, ...]


def load_manifest(path: utils._Path) -> SubtitleManifest:
    """
    Load a subtitle manifest from a JSON file.

    The file has the identifier of the show, the number of segments, its
    required languages and its optional languages::

        {
            "show": "show-1",
            "segment_count": 5,
            "required_languages": ["en", "fr"],
            "optional_languages": ["de"]
        }

    :param path: the path of the JSON file
    :returns: the validated `SubtitleManifest`

    :raises ValueError: if the JSON is not a valid manifest
    """
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f'The manifest {path!r} must hold a JSON object')
    return _manifest_from_data(data, path)


def _manifest_from_data(
        data: dict[str, Any], path: utils._Path
        ) -> SubtitleManifest:
    """
    Build and validate a manifest from parsed JSON data.

    :param data: the parsed JSON data
    :param path: the path of the manifest, for error messages
    :returns: the validated manifest

    :raises ValueError: if a field is missing or invalid
    """
    show = _text_field(data, 'show', path)
    segment_count = _segment_count_field(data, path)
    required = _language_field(data, 'required_languages', path)
    if not required:
        raise ValueError(
            f'The manifest {path!r} must list at least one required '
            'language'
            )
    optional = _language_field(
        data, 'optional_languages', path, default=[]
        )
    overlap = sorted(set(required) & set(optional))
    if overlap:
        raise ValueError(
            f'The languages {overlap} of the manifest {path!r} are both '
            'required and optional'
            )
    return SubtitleManifest(show, segment_count, required, optional)


def _text_field(
        data: dict[str, Any], name: str, path: utils._Path
        ) -> str:
    """
    Return a non-empty text field of a manifest.

    :param data: the parsed JSON data
    :param name: the name of the field
    :param path: the path of the manifest, for error messages
    :returns: the text

    :raises ValueError: if the field is missing or not a non-empty text
    """
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(
            f'The field {name!r} of the manifest {path!r} must be a '
            'non-empty string'
            )
    return value


def _segment_count_field(
        data: dict[str, Any], path: utils._Path
        ) -> int:
    """
    Return the segment count of a manifest.

    :param data: the parsed JSON data
    :param path: the path of the manifest, for error messages
    :returns: the segment count

    :raises ValueError: if it is not a whole number above zero
    """
    value = data.get('segment_count')
    if type(value) is bool or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f'The field segment_count of the manifest {path!r} must be a '
            'whole number above zero'
            )
    return value


def _language_field(
        data: dict[str, Any],
        name: str,
        path: utils._Path,
        *,
        default: list[str] | None = None
        ) -> tuple[str, ...]:
    """
    Return a tuple of languages from a manifest field.

    :param data: the parsed JSON data
    :param name: the name of the field
    :param path: the path of the manifest, for error messages
    :param default: the value to return when the field is missing
    :returns: the languages

    :raises ValueError: if the field is not a list of non-empty, unique
        strings
    """
    value = data.get(name, default)
    if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value):
        raise ValueError(
            f'The field {name!r} of the manifest {path!r} must be a list '
            'of non-empty strings'
            )
    languages = tuple(value)
    if len(set(languages)) != len(languages):
        raise ValueError(
            f'The field {name!r} of the manifest {path!r} has repeated '
            'languages'
            )
    return languages


def load_receipts(inbox: utils._Path) -> tuple[SegmentReceipt, ...]:
    """
    Scan an inbox folder for the receipts of the segments.

    The inbox has a folder per segment named ``segment1``, ``segment2`` and
    so on. Each one holds a subtitle file per language, such as ``fr.vtt``
    or ``fr.webvtt``, and optionally a ``fr.receipt.json`` sidecar with
    ``{"status": "no_dialogue"}`` confirming that the segment has no
    dialogue in the language. Files and folders that do not follow the
    layout are ignored.

    :param inbox: the folder with the segment folders
    :returns: the receipts found, sorted by segment and language

    :raises FileNotFoundError: if the inbox is not a folder
    :raises ValueError: if a receipt is malformed, a language has two
        subtitle files, or a no-dialogue receipt is next to a subtitle
        file with captions
    """
    inbox_folder = pathlib.Path(inbox)
    if not inbox_folder.is_dir():
        raise FileNotFoundError(f'The inbox {inbox!r} is not a folder')

    receipts: list[SegmentReceipt] = []
    for folder in sorted(inbox_folder.iterdir()):
        match = _SEGMENT_FOLDER_PATTERN.fullmatch(folder.name)
        if match is None or not folder.is_dir():
            continue
        receipts.extend(
            _receipts_in_folder(int(match.group(1)), folder)
            )
    return tuple(receipts)


def _receipts_in_folder(
        number: int, folder: pathlib.Path
        ) -> list[SegmentReceipt]:
    """
    Scan a segment folder for its receipts.

    :param number: the segment number
    :param folder: the segment folder
    :returns: the receipts of the folder, sorted by language

    :raises ValueError: if a receipt is malformed, a language has two
        subtitle files, or a no-dialogue receipt has a subtitle with content
    """
    subtitles: dict[str, pathlib.Path] = {}
    confirmations: dict[str, pathlib.Path] = {}
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        if path.name.endswith(_RECEIPT_SUFFIX):
            language = path.name[:-len(_RECEIPT_SUFFIX)]
            _check_receipt(path, language)
            confirmations[language] = path
            continue
        if path.suffix.lower() in _SUBTITLE_EXTENSIONS:
            language = path.stem
            if language in subtitles:
                raise ValueError(
                    f'The segment {number} has two subtitle files for the '
                    f'language {language!r}'
                    )
            subtitles[language] = path

    receipts = []
    for language in sorted(set(subtitles) | set(confirmations)):
        subtitle = subtitles.get(language)
        confirmation = confirmations.get(language)
        if confirmation is not None and subtitle is not None:
            _check_confirmed_empty(number, language, subtitle)
        receipts.append(
            SegmentReceipt(number, language, subtitle, confirmation)
            )
    return receipts


def _check_receipt(path: pathlib.Path, language: str) -> None:
    """
    Validate a producer receipt sidecar.

    :param path: the path of the sidecar
    :param language: the language the receipt is for

    :raises ValueError: if it is not valid JSON with a no-dialogue status
    """
    if not language:
        raise ValueError(f'The receipt {path!r} has no language')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as error:
        raise ValueError(
            f'The receipt {path!r} is not valid JSON'
            ) from error
    if (not isinstance(data, dict)
            or data.get('status') != _NO_DIALOGUE_STATUS):
        raise ValueError(
            f'The receipt {path!r} must have '
            f'{{"status": "{_NO_DIALOGUE_STATUS}"}}'
            )


def _check_confirmed_empty(
        number: int, language: str, subtitle: pathlib.Path
        ) -> None:
    """
    Check that a subtitle next to a no-dialogue receipt is empty.

    A file without any caption, including a zero-byte file or a header-only
    WebVTT file, is consistent with no dialogue; a file with captions is not.

    :param number: the segment number
    :param language: the language of the subtitles
    :param subtitle: the subtitle file

    :raises ValueError: if the subtitle file has captions or cannot be read
        as WebVTT
    """
    data = subtitle.read_bytes()
    if data.strip() and _read_captions(subtitle):
        raise ValueError(
            f'The segment {number} has a no-dialogue receipt for '
            f'{language!r} but its subtitle file {subtitle!r} has captions'
            )


def pack(
        inbox: utils._Path,
        output: utils._Path,
        manifest: utils._Path | SubtitleManifest,
        *,
        receipts: tuple[SegmentReceipt, ...] = ()
        ) -> PackResult:
    """
    Scan an inbox and publish its longest playable prefix.

    :param inbox: the folder with the ``segmentN`` folders
    :param output: the folder where the state and the releases are written,
        created when it does not exist
    :param manifest: the path of the JSON manifest, or a `SubtitleManifest`
    :param receipts: additional receipts besides those scanned in the inbox
    :returns: the result of the release, existing and unchanged when the
        inputs give the same playable prefix as the previous run

    :raises FileNotFoundError: if the inbox is not a folder
    :raises ValueError: if the manifest or a receipt is invalid
    """
    manifest_object = (manifest if isinstance(manifest, SubtitleManifest)
                       else load_manifest(manifest))
    return pack_manifest(
        manifest_object, output, (*load_receipts(inbox), *receipts)
        )


def pack_manifest(
        manifest: SubtitleManifest,
        output: utils._Path,
        receipts: tuple[SegmentReceipt, ...] = ()
        ) -> PackResult:
    """
    Publish the longest playable prefix described by receipts.

    The output folder keeps a state file with the hashes of every release,
    and a ``releases`` folder with one folder per release holding its files
    and a rebuildable manifest. The releases are never modified or deleted.
    A run whose playable prefix has the same content as the previous one
    creates no release and returns the existing one.

    :param manifest: the manifest of the show
    :param output: the folder where the state and the releases are written,
        created when it does not exist
    :param receipts: the receipts of the segments and languages
    :returns: the result of the release

    :raises ValueError: if a receipt refers to an unknown segment or
        language, is repeated, or a subtitle file cannot be read
    """
    plan = _plan_release(manifest, receipts)
    output_folder = pathlib.Path(output)
    output_folder.mkdir(parents=True, exist_ok=True)
    state_path = output_folder / _STATE_NAME
    state = _read_state(state_path)

    if state is not None and state['last_fingerprint'] == plan.fingerprint:
        return _pack_result(plan, state, created=False)

    changes = _diff_release(plan, state)
    releases = [] if state is None else list(state['releases'])
    release_path: pathlib.Path | None = None
    if plan.prefix:
        release_number = len(releases) + 1
        release_path = _write_release(
            output_folder, release_number, manifest, plan, changes
            )
        releases.append(
            _release_record(release_number, release_path, plan)
            )
    _write_state(state_path, manifest, plan, releases)
    return PackResult(
        release_number=len(releases),
        path=release_path,
        prefix=plan.prefix,
        created=release_path is not None,
        changes=changes,
        segments=plan.segments
        )


def _plan_release(
        manifest: SubtitleManifest,
        receipts: tuple[SegmentReceipt, ...]
        ) -> ReleasePlan:
    """
    Plan the playable prefix from a manifest and receipts.

    :param manifest: the manifest of the show
    :param receipts: the receipts of the segments and languages
    :returns: the planned release

    :raises ValueError: if a receipt refers to an unknown segment or
        language, is repeated, or a subtitle file cannot be read
    """
    receipts_by_key = _check_receipts(manifest, receipts)
    segments = []
    prefix = 0
    blocked = False
    for number in range(1, manifest.segment_count + 1):
        languages = {
            language: _language_status(
                receipts_by_key.get((number, language))
                )
            for language in manifest.languages
            }
        playable = not blocked and all(
            languages[language].state in _PLAYABLE_STATES
            for language in manifest.required_languages
            )
        status = SegmentStatus(
            number=number,
            languages=languages,
            playable=playable,
            fingerprint=_fingerprint(
                _segment_data(number, languages)
                )
            )
        segments.append(status)
        if playable:
            prefix = number
        else:
            # the prefix ends at the first segment that cannot play, and
            # the segments after it cannot join a consecutive prefix
            blocked = True
    return ReleasePlan(
        prefix=prefix,
        segments=tuple(segments),
        fingerprint=_fingerprint(_prefix_data(prefix, segments))
        )


def _check_receipts(
        manifest: SubtitleManifest,
        receipts: tuple[SegmentReceipt, ...]
        ) -> dict[tuple[int, str], SegmentReceipt]:
    """
    Validate receipts and index them by segment and language.

    :param manifest: the manifest of the show
    :param receipts: the receipts to validate
    :returns: the receipts indexed by ``(segment, language)``

    :raises ValueError: if a receipt refers to an unknown segment or
        language, or is repeated
    """
    languages = set(manifest.languages)
    receipts_by_key: dict[tuple[int, str], SegmentReceipt] = {}
    for receipt in receipts:
        if not 1 <= receipt.segment <= manifest.segment_count:
            raise ValueError(
                f'The receipt for segment {receipt.segment} and language '
                f'{receipt.language!r} is outside the '
                f'{manifest.segment_count} segments of the show'
                )
        if receipt.language not in languages:
            raise ValueError(
                f'The language {receipt.language!r} of a receipt of '
                f'segment {receipt.segment} is not in the manifest'
                )
        key = (receipt.segment, receipt.language)
        if key in receipts_by_key:
            raise ValueError(
                'There is more than one receipt for segment '
                f'{receipt.segment} and language {receipt.language!r}'
                )
        receipts_by_key[key] = receipt
    return receipts_by_key


def _language_status(
        receipt: SegmentReceipt | None
        ) -> LanguageStatus:
    """
    Resolve the state of a language of a segment.

    An empty subtitle file without a no-dialogue receipt is not playable,
    so it is marked empty and keeps the prefix from going further, instead
    of stopping the release of the segments before it.

    :param receipt: the receipt of the language, or None if it has none
    :returns: the state of the language

    :raises ValueError: if the subtitle file cannot be read as WebVTT
    """
    if receipt is None:
        return LanguageStatus(STATE_MISSING, None, None, None, None)
    if receipt.confirmation is not None:
        return _confirmed_status(receipt)
    assert receipt.subtitle is not None
    data = receipt.subtitle.read_bytes()
    if not data.strip() or not _read_captions(receipt.subtitle):
        return LanguageStatus(
            STATE_EMPTY, receipt.subtitle, None, None, None
            )
    return LanguageStatus(
        STATE_READY,
        receipt.subtitle,
        None,
        _hash_bytes(data),
        None
        )


def _read_captions(subtitle: pathlib.Path) -> int:
    """
    Return the number of captions of a subtitle file.

    :param subtitle: the subtitle file
    :returns: the number of captions, zero for a file without any

    :raises ValueError: if the file cannot be read as WebVTT
    """
    try:
        return len(WebVTT.read(subtitle).captions)
    except MalformedFileError as error:
        raise ValueError(
            f'The subtitle file {subtitle!r} cannot be read: {error}'
            ) from error


def _confirmed_status(receipt: SegmentReceipt) -> LanguageStatus:
    """
    Resolve the state of a language with a no-dialogue receipt.

    The release always writes a minimal empty WebVTT file, even when the
    inbox has a zero-byte file, so every released file is valid WebVTT, and
    hashes the bytes it writes.

    :param receipt: the receipt of the language
    :returns: the confirmed-empty state
    """
    assert receipt.confirmation is not None
    return LanguageStatus(
        STATE_CONFIRMED_EMPTY,
        receipt.subtitle,
        receipt.confirmation,
        _hash_bytes(_EMPTY_SUBTITLE),
        _hash_bytes(receipt.confirmation.read_bytes())
        )


def _segment_data(
        number: int, languages: dict[str, LanguageStatus]
        ) -> dict[str, Any]:
    """
    Return the canonical data of a segment for hashing.

    :param number: the segment number
    :param languages: the state of its languages
    :returns: the data of the segment, including only languages that would
        be released
    """
    return {
        'number': number,
        'languages': {
            language: {
                'state': status.state,
                'sha256': status.sha256,
                'receipt_sha256': status.receipt_sha256
                }
            for language, status in sorted(languages.items())
            if status.state in _PLAYABLE_STATES
            }
        }


def _prefix_data(
        prefix: int, segments: list[SegmentStatus]
        ) -> list[dict[str, Any]]:
    """
    Return the canonical data of a playable prefix for hashing.

    :param prefix: the number of segments in the prefix
    :param segments: the state of every segment
    :returns: the data of the segments of the prefix
    """
    return [
        {'number': segment.number, 'fingerprint': segment.fingerprint}
        for segment in segments[:prefix]
        ]


def _fingerprint(data: Any) -> str:
    """
    Return the hash of canonical JSON data.

    :param data: the JSON-compatible data to hash
    :returns: the SHA-256 hash of the data
    """
    encoded = json.dumps(data, sort_keys=True).encode('utf-8')
    return _hash_bytes(encoded)


def _hash_bytes(data: bytes) -> str:
    """
    Return the SHA-256 hash of bytes.

    :param data: the bytes to hash
    :returns: the hexadecimal hash
    """
    return hashlib.sha256(data).hexdigest()


def _diff_release(
        plan: ReleasePlan, state: dict[str, Any] | None
        ) -> ReleaseChanges:
    """
    Compare a plan with the last release of the state.

    :param plan: the planned release
    :param state: the state of earlier releases, or None for the first one
    :returns: the added, replaced, removed and carried-over segments
    """
    if state is None or not state['releases']:
        old: dict[int, str] = {}
    else:
        last_release = state['releases'][-1]
        old = {
            segment['number']: segment['fingerprint']
            for segment in last_release['segments']
            }
    new = {
        segment.number: segment.fingerprint
        for segment in plan.segments[:plan.prefix]
        }
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    replaced = sorted(
        number for number in set(new) & set(old)
        if new[number] != old[number]
        )
    carried_over = sorted(
        number for number in set(new) & set(old)
        if new[number] == old[number]
        )
    return ReleaseChanges(
        added=tuple(added),
        replaced=tuple(replaced),
        removed=tuple(removed),
        carried_over=tuple(carried_over)
        )


def _write_release(
        output_folder: pathlib.Path,
        release_number: int,
        manifest: SubtitleManifest,
        plan: ReleasePlan,
        changes: ReleaseChanges
        ) -> pathlib.Path:
    """
    Write a new release folder with its files and manifest.

    :param output_folder: the output folder
    :param release_number: the number of the release
    :param manifest: the manifest of the show
    :param plan: the planned release
    :param changes: the changes since the previous release
    :returns: the folder of the release

    :raises FileExistsError: if the release folder already exists
    """
    release_folder = (
        output_folder / _RELEASES_FOLDER
        / f'release-{release_number:04d}'
        )
    release_folder.mkdir(parents=True)
    for segment in plan.segments[:plan.prefix]:
        _write_segment(release_folder, segment)
    manifest_data = _release_manifest_data(
        release_number, manifest, plan, changes
        )
    with open(release_folder / _RELEASE_MANIFEST_NAME,
              'w', encoding='utf-8') as f:
        json.dump(manifest_data, f, indent=2, sort_keys=True)
        f.write('\n')
    return release_folder


def _write_segment(
        release_folder: pathlib.Path, segment: SegmentStatus
        ) -> None:
    """
    Write the subtitle files of a segment of a release.

    :param release_folder: the folder of the release
    :param segment: the state of the segment
    """
    segment_folder = release_folder / f'segment{segment.number:04d}'
    for language, status in sorted(segment.languages.items()):
        if status.state not in _PLAYABLE_STATES or status.sha256 is None:
            continue
        segment_folder.mkdir(exist_ok=True)
        target = segment_folder / f'{language}{_release_extension(status)}'
        target.write_bytes(_release_content(status))


def _release_extension(status: LanguageStatus) -> str:
    """
    Return the extension of the file written for a language.

    :param status: the state of the language
    :returns: the extension of its subtitle file, ``.vtt`` when the
        no-dialogue language has no subtitle file
    """
    if status.subtitle is not None:
        return status.subtitle.suffix
    return '.vtt'


def _release_content(status: LanguageStatus) -> bytes:
    """
    Return the bytes written for a language of a release.

    A no-dialogue language always gets a minimal empty WebVTT file; other
    languages get the bytes of their subtitle file.

    :param status: the state of the language
    :returns: the bytes of its subtitle file
    """
    if status.state == STATE_CONFIRMED_EMPTY:
        return _EMPTY_SUBTITLE
    assert status.subtitle is not None
    return status.subtitle.read_bytes()


def _release_manifest_data(
        release_number: int,
        manifest: SubtitleManifest,
        plan: ReleasePlan,
        changes: ReleaseChanges
        ) -> dict[str, Any]:
    """
    Build the rebuildable manifest data of a release.

    :param release_number: the number of the release
    :param manifest: the manifest of the show
    :param plan: the planned release
    :param changes: the changes since the previous release
    :returns: the JSON-compatible manifest data
    """
    return {
        'release': release_number,
        'show': manifest.show,
        'created_at': datetime.datetime.now(
            datetime.timezone.utc
            ).isoformat(),
        'prefix_segments': plan.prefix,
        'required_languages': list(manifest.required_languages),
        'optional_languages': list(manifest.optional_languages),
        'changes': {
            'added': list(changes.added),
            'replaced': list(changes.replaced),
            'removed': list(changes.removed),
            'carried_over': list(changes.carried_over)
            },
        'segments': [
            _release_segment_data(segment)
            for segment in plan.segments[:plan.prefix]
            ]
        }


def _release_segment_data(segment: SegmentStatus) -> dict[str, Any]:
    """
    Build the manifest data of a segment of a release.

    :param segment: the state of the segment
    :returns: the JSON-compatible data of its released languages
    """
    languages = {}
    for language, status in sorted(segment.languages.items()):
        if status.state not in _PLAYABLE_STATES or status.sha256 is None:
            continue
        entry: dict[str, Any] = {
            'state': status.state,
            'filename': f'{language}{_release_extension(status)}',
            'source': (str(status.subtitle.resolve())
                       if status.subtitle is not None else None),
            'sha256': status.sha256
            }
        if status.confirmation is not None:
            entry['receipt'] = {
                'source': str(status.confirmation.resolve()),
                'sha256': status.receipt_sha256
                }
        languages[language] = entry
    return {'number': segment.number, 'languages': languages}


def _release_record(
        release_number: int,
        release_path: pathlib.Path,
        plan: ReleasePlan
        ) -> dict[str, Any]:
    """
    Build the record of a release for the state file.

    :param release_number: the number of the release
    :param release_path: the folder of the release
    :param plan: the planned release
    :returns: the JSON-compatible record
    """
    return {
        'release': release_number,
        'folder': str(release_path),
        'prefix': plan.prefix,
        'segments': [
            {'number': segment.number, 'fingerprint': segment.fingerprint}
            for segment in plan.segments[:plan.prefix]
            ]
        }


def _read_state(
        state_path: pathlib.Path
        ) -> dict[str, Any] | None:
    """
    Read the state file of the output folder.

    :param state_path: the path of the state file
    :returns: the state, or None if there is no state file
    """
    if not state_path.is_file():
        return None
    with open(state_path, encoding='utf-8') as f:
        return cast(dict[str, Any], json.load(f))


def _write_state(
        state_path: pathlib.Path,
        manifest: SubtitleManifest,
        plan: ReleasePlan,
        releases: list[dict[str, Any]]
        ) -> None:
    """
    Write the state file atomically.

    :param state_path: the path of the state file
    :param manifest: the manifest of the show
    :param plan: the planned release
    :param releases: the records of the releases
    """
    state = {
        'show': manifest.show,
        'segment_count': manifest.segment_count,
        'required_languages': list(manifest.required_languages),
        'optional_languages': list(manifest.optional_languages),
        'last_fingerprint': plan.fingerprint,
        'releases': releases
        }
    temporary_path = state_path.with_name(f'{state_path.name}.tmp')
    with open(temporary_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write('\n')
    temporary_path.replace(state_path)


def _pack_result(
        plan: ReleasePlan,
        state: dict[str, Any],
        *,
        created: bool
        ) -> PackResult:
    """
    Build the result of a run that creates no new release folder.

    :param plan: the planned release
    :param state: the state of earlier releases
    :param created: whether a release was created
    :returns: the result pointing at the latest release, if any
    """
    releases = state['releases']
    if releases:
        last_release = releases[-1]
        path: pathlib.Path | None = pathlib.Path(last_release['folder'])
        release_number = last_release['release']
    else:
        path = None
        release_number = 0
    return PackResult(
        release_number=release_number,
        path=path,
        prefix=plan.prefix,
        created=created,
        changes=_diff_release(plan, state),
        segments=plan.segments
        )


def rebuild_release(
        output: utils._Path,
        destination: utils._Path,
        *,
        release: int | None = None
        ) -> pathlib.Path:
    """
    Rebuild a release from its manifest and its preserved files.

    Each release folder is an immutable snapshot: the subtitle files are
    copied there when the release is created, even when later revisions
    replace the source files in the inbox. Rebuilding copies those files to
    the destination, in the same segment folders, and checks their hashes
    against the release manifest. The ``source`` of the manifest only
    records where each file came from. A no-dialogue language is rebuilt as
    an empty WebVTT file.

    :param output: the output folder holding the releases
    :param destination: the folder where the release is rebuilt, created
        when it does not exist
    :param release: the number of the release to rebuild, the latest one
        by default
    :returns: the destination folder

    :raises FileNotFoundError: if there is no state or release, or a file
        preserved with the release is missing
    :raises ValueError: if the release number is unknown or a rebuilt file
        does not match the hash of the manifest
    """
    state_path = pathlib.Path(output) / _STATE_NAME
    state = _read_state(state_path)
    if state is None:
        raise FileNotFoundError(
            f'The output folder {output!r} has no release state'
            )
    record = _release_record_for(state, release)
    release_folder = pathlib.Path(record['folder'])
    release_manifest_path = release_folder / _RELEASE_MANIFEST_NAME
    with open(release_manifest_path, encoding='utf-8') as f:
        release_manifest = json.load(f)

    destination_folder = pathlib.Path(destination)
    destination_folder.mkdir(parents=True, exist_ok=True)
    for segment in release_manifest['segments']:
        _rebuild_segment(release_folder, destination_folder, segment)
    return destination_folder


def _release_record_for(
        state: dict[str, Any], release: int | None
        ) -> dict[str, Any]:
    """
    Return the state record of a release.

    :param state: the state of the releases
    :param release: the number of the release, or None for the latest one
    :returns: the record of the release

    :raises ValueError: if the release number is unknown, or there is no
        release
    """
    records = {
        record['release']: record for record in state['releases']
        }
    if release is None:
        if not records:
            raise ValueError('The state has no release')
        return cast(dict[str, Any], state['releases'][-1])
    if release not in records:
        raise ValueError(
            f'The release {release} is unknown; the releases are '
            f'{sorted(records)}'
            )
    return cast(dict[str, Any], records[release])


def _rebuild_segment(
        release_folder: pathlib.Path,
        destination_folder: pathlib.Path,
        segment: dict[str, Any]
        ) -> None:
    """
    Rebuild the files of a segment from a release snapshot and its entry.

    :param release_folder: the immutable folder of the release
    :param destination_folder: the folder where the release is rebuilt
    :param segment: the segment entry of the release manifest

    :raises FileNotFoundError: if a file preserved with the release is
        missing
    :raises ValueError: if a rebuilt file does not match its hash
    """
    segment_name = f"segment{segment['number']:04d}"
    target_folder = destination_folder / segment_name
    for language, entry in sorted(segment['languages'].items()):
        if entry['state'] == STATE_CONFIRMED_EMPTY:
            content = _EMPTY_SUBTITLE
        else:
            preserved = release_folder / segment_name / entry['filename']
            if not preserved.is_file():
                raise FileNotFoundError(
                    f'The file {preserved!r} preserved with the release '
                    'is missing'
                    )
            content = preserved.read_bytes()
        if _hash_bytes(content) != entry['sha256']:
            raise ValueError(
                'The rebuilt file for segment '
                f"{segment['number']} and language {language!r} does not "
                'match the hash of its release manifest'
                )
        target_folder.mkdir(exist_ok=True)
        (target_folder / entry['filename']).write_bytes(content)
