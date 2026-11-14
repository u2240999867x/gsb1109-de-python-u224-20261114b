"""
Provenance-aware working-day resolution.

A business day is often described by more than one source: a group calendar,
a branch or site calendar, and a personal exception list. Those sources do not
carry the same authority, and their notices may be published or withdrawn at
different times. This module keeps every declaration and resolves the one
that was effective for a query, instead of silently overwriting the previous
value.

The public entry points are :class:`Declaration`, :class:`SourceLevel`,
:class:`Resolution` and :class:`LayeredCalendar`.

Resolution rules
----------------

* A declaration is visible at ``as_of`` when it was published at or before
  ``as_of`` and was not withdrawn at or before ``as_of``.
* When the same source publishes several versions for the same business day,
  only the latest active version takes part in the comparison. Older versions
  are reported as shadowed.
* Higher source levels win: ``GROUP < BRANCH < PERSONAL``. Every declaration
  from a lower level is reported as shadowed by the winner.
* Two declarations at the same (winning) level that disagree leave the day
  pending: ``Resolution.working`` is ``None`` and both declarations are listed
  in ``Resolution.conflicts``. No winner is invented.
* A query without an ``as_of`` time means "as of now" (all currently active
  notices).
* If there is no active declaration, an optional base calendar is used as the
  group-level default. This is what makes a group "make-up workday" visible
  again after a branch's temporary stoppage is withdrawn.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
from enum import IntEnum
from typing import Dict, List, Optional, Tuple


__all__ = [
    'Declaration',
    'LayeredCalendar',
    'Notice',
    'Resolution',
    'ResolutionStatus',
    'Rule',
    'Source',
    'SourceLevel',
    'explain',
    'resolve',
]


class SourceLevel(IntEnum):
    """Authority levels used by :class:`Declaration`.

    A more specific level has a higher value and wins over a less specific
    one. The aliases make the intent readable when integrating with existing
    data (``COMPANY`` for a group calendar, ``LOCAL`` for a branch, etc.).
    """

    GROUP = 10
    BRANCH = 20
    PERSONAL = 30

    # Readable aliases. They intentionally share the values above.
    COMPANY = 10
    LOCAL = 20
    INDIVIDUAL = 30

    @property
    def label(self):
        """Return the canonical lowercase name of this level."""
        labels = {
            SourceLevel.GROUP: 'group',
            SourceLevel.BRANCH: 'branch',
            SourceLevel.PERSONAL: 'personal',
        }
        return labels[self]


_LEVEL_ALIASES = {
    'group': SourceLevel.GROUP,
    'global': SourceLevel.GROUP,
    'headquarters': SourceLevel.GROUP,
    'hq': SourceLevel.GROUP,
    'company': SourceLevel.GROUP,
    'corporate': SourceLevel.GROUP,
    'organization': SourceLevel.GROUP,
    'organisation': SourceLevel.GROUP,
    'org': SourceLevel.GROUP,
    '集团': SourceLevel.GROUP,
    '总部': SourceLevel.GROUP,
    'branch': SourceLevel.BRANCH,
    'site': SourceLevel.BRANCH,
    'location': SourceLevel.BRANCH,
    'local': SourceLevel.BRANCH,
    'subsidiary': SourceLevel.BRANCH,
    'store': SourceLevel.BRANCH,
    '分公司': SourceLevel.BRANCH,
    '门店': SourceLevel.BRANCH,
    'personal': SourceLevel.PERSONAL,
    'individual': SourceLevel.PERSONAL,
    'employee': SourceLevel.PERSONAL,
    'self': SourceLevel.PERSONAL,
    'me': SourceLevel.PERSONAL,
    '个人': SourceLevel.PERSONAL,
    '员工': SourceLevel.PERSONAL,
}


def _coerce_level(value):
    """Normalize a source level to :class:`SourceLevel`."""
    if isinstance(value, SourceLevel):
        return value
    if isinstance(value, str):
        token = value.strip().lower().replace('-', '_').replace(' ', '_')
        try:
            return SourceLevel[token.upper()]
        except KeyError:
            pass
        if token in _LEVEL_ALIASES:
            return _LEVEL_ALIASES[token]
    elif isinstance(value, int) and not isinstance(value, bool):
        try:
            return SourceLevel(value)
        except ValueError:
            pass
    raise ValueError(f"Unknown source level: {value!r}")


def _as_date(value):
    """Return a :class:`datetime.date` from a date, datetime or ISO string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise TypeError(f"`{value}` is not a supported business day")


def _as_datetime(value, end_of_day=False):
    """Normalize a timestamp to a naive UTC datetime.

    Dates are interpreted at the start of the day, except for query cut-offs
    where ``end_of_day=True`` means "as of the end of that day". This makes
    ``as_of=date(2026, 5, 8)`` include every notice published on May 8th.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.max if end_of_day else time.min)
    elif isinstance(value, str):
        text = value.strip()
        try:
            moment = datetime.fromisoformat(
                text[:-1] + '+00:00' if text.endswith('Z') else text
            )
        except ValueError:
            raise ValueError(f"`{value}` is not an ISO date/time")
        if end_of_day and len(text) == 10:
            moment = datetime.combine(moment.date(), time.max)
    else:
        raise TypeError(f"`{value}` is not a supported timestamp")

    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True, init=False)
class Declaration:
    """A dated statement about whether a business day is a working day.

    Parameters
    ----------
    day:
        The business day this statement is about.
    working:
        ``True`` for a working day (for example a make-up Saturday),
        ``False`` for a non-working day (a holiday or a temporary stoppage).
        ``is_working_day`` is accepted as an alias.
    source:
        Stable name of the source, for example ``"group"`` or
        ``"branch-shanghai"``.
    level:
        :class:`SourceLevel` or one of ``"group"``, ``"branch"``,
        ``"personal"``. Higher levels win.
    published_at:
        When the notice was published. A declaration without a timestamp is
        treated as always published. Dates and ISO strings are accepted.
    withdrawn_at:
        When the notice stopped being effective. ``None`` means it is still
        active.
    version:
        Version number within the same ``(source, level, day)``. Only the
        latest active version participates in resolution.
    label:
        Optional human-readable label, for example ``"make-up workday"``.
    notice_id:
        Optional external notice identifier, preserved in explanations.
    rule:
        Optional rule name, for example ``"group-calendar"``.
    id:
        Optional unique declaration identifier. It is generated when absent.
    """

    day: date
    working: bool
    source: str = 'unknown'
    level: SourceLevel = SourceLevel.GROUP
    published_at: Optional[datetime] = None
    withdrawn_at: Optional[datetime] = None
    version: int = 1
    label: Optional[str] = None
    notice_id: Optional[str] = None
    rule: Optional[str] = None
    id: str = ''

    def __init__(self, day=None, working=None, source='unknown',
                 level=SourceLevel.GROUP, published_at=None,
                 withdrawn_at=None, version=1, label=None, notice_id=None,
                 rule=None, id=None, is_working_day=None, **kwargs):
        # Accept the field names commonly used by collected notice records.
        aliases = {
            'date': 'day',
            'business_day': 'day',
            'day_date': 'day',
            'notice_version': 'version',
            'source_level': 'level',
            'source_layer': 'level',
            'authority': 'level',
            'scope': 'level',
            'is_working': 'working',
            'effective': 'working',
            'published': 'published_at',
            'published_time': 'published_at',
            'issued_at': 'published_at',
            'retracted_at': 'withdrawn_at',
            'revoked_at': 'withdrawn_at',
            'withdrawal_time': 'withdrawn_at',
        }
        for alias, canonical in aliases.items():
            if alias not in kwargs:
                continue
            value = kwargs.pop(alias)
            if canonical == 'day':
                if day is not None:
                    raise TypeError(
                        f"Provide only one of `day` and `{alias}`")
                day = value
            elif canonical == 'working' and working is None \
                    and is_working_day is None:
                working = value
            elif canonical == 'version' and version == 1:
                version = value
            elif canonical == 'level' and level == SourceLevel.GROUP:
                level = value
            elif canonical == 'published_at' and published_at is None:
                published_at = value
            elif canonical == 'withdrawn_at' and withdrawn_at is None:
                withdrawn_at = value

        status = kwargs.pop('status', None)
        if status is not None and working is None and is_working_day is None:
            token = str(status).strip().lower()
            if token in ('working', 'workday', 'open', 'yes', 'true'):
                working = True
            elif token in ('not_working', 'non_working', 'holiday',
                           'closed', 'no', 'false'):
                working = False
            else:
                raise ValueError(f"Unknown declaration status: {status!r}")

        if day is None:
            raise TypeError("Declaration requires `day` "
                            "(or `date` / `business_day`)")
        if 'declaration_id' in kwargs:
            if id is not None:
                raise TypeError("Provide only one of `id` and "
                                "`declaration_id`")
            id = kwargs.pop('declaration_id')
        if kwargs:
            unexpected = ', '.join(sorted(kwargs))
            raise TypeError(
                f"Unexpected declaration argument(s): {unexpected}")

        if working is None:
            if is_working_day is None:
                raise TypeError("Declaration requires `working` "
                                "(or `is_working_day`)")
            working = is_working_day
        elif is_working_day is not None \
                and bool(is_working_day) != bool(working):
            raise ValueError("`working` and `is_working_day` disagree")

        if isinstance(source, Source):
            if level == SourceLevel.GROUP:
                level = source.level
            source = source.name

        day = _as_date(day)
        source = str(source) if source is not None else 'unknown'
        level = _coerce_level(level)
        published_at = _as_datetime(published_at)
        withdrawn_at = _as_datetime(withdrawn_at)

        if published_at is not None and withdrawn_at is not None \
                and withdrawn_at < published_at:
            raise ValueError("A declaration cannot be withdrawn before it "
                             "was published")

        try:
            version = int(version)
        except (TypeError, ValueError):
            raise ValueError(f"`version` must be an integer, got {version!r}")
        if version < 0:
            raise ValueError("`version` must not be negative")

        if id is None:
            id = '{}:{}:{}:v{}'.format(
                source, level.label, day.isoformat(), version)

        object.__setattr__(self, 'day', day)
        object.__setattr__(self, 'working', bool(working))
        object.__setattr__(self, 'source', source)
        object.__setattr__(self, 'level', level)
        object.__setattr__(self, 'published_at', published_at)
        object.__setattr__(self, 'withdrawn_at', withdrawn_at)
        object.__setattr__(self, 'version', version)
        object.__setattr__(self, 'label', label)
        object.__setattr__(self, 'notice_id', notice_id)
        object.__setattr__(self, 'rule', rule)
        object.__setattr__(self, 'id', str(id))

    @property
    def is_working_day(self):
        """Alias for :attr:`working`."""
        return self.working

    @property
    def status(self):
        """``"working"`` or ``"not_working"``."""
        return 'working' if self.working else 'not_working'

    @property
    def level_priority(self):
        """Numeric priority of the declaration's source level."""
        return int(self.level)

    @property
    def version_group(self):
        """Key used to select the latest version of one source's notice."""
        return (self.source, self.level, self.day)

    def published_by(self, as_of=None):
        """Whether this declaration was published by ``as_of``."""
        return self.published_at is None or as_of is None \
            or self.published_at <= as_of

    def withdrawn_by(self, as_of=None):
        """Whether this declaration was withdrawn by ``as_of``."""
        return self.withdrawn_at is not None and (
            as_of is None or self.withdrawn_at <= as_of
        )

    def active_at(self, as_of=None):
        """Whether this declaration is visible and still effective."""
        return self.published_by(as_of) and not self.withdrawn_by(as_of)

    def as_dict(self):
        """Return a JSON-friendly representation."""
        return {
            'id': self.id,
            'day': self.day.isoformat(),
            'working': self.working,
            'status': self.status,
            'source': self.source,
            'level': self.level.label,
            'version': self.version,
            'published_at': (self.published_at.isoformat()
                             if self.published_at is not None else None),
            'withdrawn_at': (self.withdrawn_at.isoformat()
                             if self.withdrawn_at is not None else None),
            'label': self.label,
            'notice_id': self.notice_id,
            'rule': self.rule,
        }

    def __repr__(self):
        return (
            "Declaration(day={!r}, working={!r}, source={!r}, level={!r}, "
            "version={!r}, published_at={!r}, withdrawn_at={!r})".format(
                self.day, self.working, self.source, self.level,
                self.version, self.published_at, self.withdrawn_at,
            )
        )


# A notice (and a rule) is the same concept under a different name.
Notice = Declaration
Rule = Declaration


@dataclass(frozen=True)
class Source:
    """Identity of a declaration source.

    ``Source`` is a convenience wrapper; :class:`Declaration` accepts
    ``source=Source(...)`` and uses its name and level.
    """

    name: str
    level: SourceLevel = SourceLevel.GROUP
    description: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, 'name', str(self.name))
        object.__setattr__(self, 'level', _coerce_level(self.level))

    def __str__(self):
        return self.name


class ResolutionStatus:
    """String constants used by :attr:`Resolution.status`."""

    RESOLVED = 'resolved'
    PENDING = 'pending'
    NO_DATA = 'no_data'


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving one business day at a given publication time.

    ``working`` is a boolean when a declaration wins, and ``None`` when the
    result is pending (same-level disagreement) or when no declaration exists.
    ``winner`` and ``shadowed`` are always present in the explanation, even
    when they are empty.
    """

    day: date
    as_of: Optional[datetime]
    status: str
    working: Optional[bool]
    winner: Optional[Declaration] = None
    conflicts: Tuple[Declaration, ...] = ()
    active: Tuple[Declaration, ...] = ()
    shadowed: Tuple[Declaration, ...] = ()
    withdrawn: Tuple[Declaration, ...] = ()
    not_published: Tuple[Declaration, ...] = ()
    reason: str = ''
    shadow_reasons: Tuple[str, ...] = ()

    @property
    def is_working_day(self):
        """Alias for :attr:`working`."""
        return self.working

    @property
    def is_resolved(self):
        return self.status == ResolutionStatus.RESOLVED

    @property
    def is_pending(self):
        return self.status == ResolutionStatus.PENDING

    @property
    def is_no_data(self):
        return self.status == ResolutionStatus.NO_DATA

    @property
    def effective(self):
        """The declarations that were visible at the query time."""
        return self.active

    @property
    def winning(self):
        """The winning declaration, or ``None`` when pending."""
        return self.winner

    @property
    def pending(self):
        """Declarations held for adjudication."""
        return self.conflicts

    @property
    def shadowed_ids(self):
        """Identifiers of every shadowed declaration."""
        return tuple(item.id for item in self.shadowed)

    @property
    def active_ids(self):
        """Identifiers of every active declaration."""
        return tuple(item.id for item in self.active)

    @property
    def winner_id(self):
        """Identifier of the winning declaration, or ``None``."""
        return None if self.winner is None else self.winner.id

    def reason_for(self, declaration):
        """Return why ``declaration`` was shadowed, if it was."""
        for candidate, reason in zip(self.shadowed, self.shadow_reasons):
            if candidate == declaration:
                return reason
        return None

    def to_dict(self):
        """Return a JSON-friendly explanation of this resolution."""
        return {
            'day': self.day.isoformat(),
            'as_of': (self.as_of.isoformat()
                      if self.as_of is not None else None),
            'status': self.status,
            'working': self.working,
            'is_working_day': self.working,
            'winner': (self.winner.as_dict()
                       if self.winner is not None else None),
            'conflicts': [item.as_dict() for item in self.conflicts],
            'active': [item.as_dict() for item in self.active],
            'shadowed': [
                {'declaration': item.as_dict(), 'reason': reason}
                for item, reason in zip(self.shadowed, self.shadow_reasons)
            ],
            'withdrawn': [item.as_dict() for item in self.withdrawn],
            'not_published': [item.as_dict() for item in self.not_published],
            'reason': self.reason,
        }

    def explain(self):
        """Render a compact, human-readable audit of the resolution."""
        lines = [
            '{} ({}): {}'.format(
                self.day.isoformat(),
                self.day.strftime('%a').upper(),
                self.status,
            )
        ]
        lines.append('  as of: {}'.format(self.as_of.isoformat()))
        if self.working is None:
            lines.append('  working day: unknown')
        else:
            lines.append('  working day: {}'.format(
                'yes' if self.working else 'no'))

        if self.winner is not None:
            lines.append('  winner: {}'.format(
                _format_declaration(self.winner)))
        else:
            lines.append('  winner: none')

        if self.conflicts:
            lines.append('  pending same-level conflict:')
            for item in self.conflicts:
                lines.append('    {}'.format(_format_declaration(item)))
        else:
            lines.append('  pending same-level conflict: none')

        if self.shadowed:
            lines.append('  shadowed:')
            for item, reason in zip(self.shadowed, self.shadow_reasons):
                lines.append('    {} -> {}'.format(
                    _format_declaration(item), reason))
        else:
            lines.append('  shadowed: none')

        if self.withdrawn:
            lines.append('  withdrawn:')
            for item in self.withdrawn:
                lines.append('    {} (withdrawn at {})'.format(
                    _format_declaration(item),
                    item.withdrawn_at.isoformat()
                    if item.withdrawn_at is not None else 'unknown'))
        if self.not_published:
            lines.append('  not yet published:')
            for item in self.not_published:
                lines.append('    {} (published at {})'.format(
                    _format_declaration(item),
                    item.published_at.isoformat()
                    if item.published_at is not None else 'unknown'))

        if self.reason:
            lines.append('  reason: {}'.format(self.reason))
        return '\n'.join(lines)

    def __str__(self):
        return self.explain()


def _format_declaration(declaration):
    return '{} [{}] v{} -> {} (published {}){}'.format(
        declaration.source,
        declaration.level.label,
        declaration.version,
        declaration.status,
        declaration.published_at.isoformat()
        if declaration.published_at is not None else 'always',
        ' ({})'.format(declaration.label) if declaration.label else '',
    )


def _resolve_declarations(declarations, day, as_of, base_calendar=None):
    day = _as_date(day)
    as_of = _as_datetime(as_of, end_of_day=True)
    if as_of is None:
        as_of = _now()

    same_day = [item for item in declarations if item.day == day]
    active = [item for item in same_day if item.active_at(as_of)]
    withdrawn = [item for item in same_day if item.withdrawn_by(as_of)]
    not_published = [
        item for item in same_day
        if not item.published_by(as_of)
    ]

    def all_active():
        items = list(active)
        if base is not None:
            items.append(base)
        return tuple(_sort_declarations(items))

    # Select the latest active version per source/day. Older versions remain
    # visible in the audit trail but cannot override their newer sibling.
    groups: Dict[tuple, List[Declaration]] = {}
    for item in active:
        groups.setdefault(item.version_group, []).append(item)

    # If the caller supplied a base calendar, treat it as the group-level
    # default unless a group-level notice is already in force. It remains a
    # normal (lower-priority) candidate, so a branch stoppage shadows it and
    # withdrawing that stoppage reveals it again.
    base = None
    if base_calendar is not None and not any(
            item.level_priority == SourceLevel.GROUP
            for item in active):
        base = Declaration(
            day=day,
            working=bool(base_calendar.is_working_day(day)),
            source=getattr(
                base_calendar, 'name', type(base_calendar).__name__),
            level=SourceLevel.GROUP,
            version=0,
            label='calendar default',
            rule='base-calendar',
        )
        groups.setdefault(base.version_group, []).append(base)

    candidates: List[Declaration] = []
    shadowed: List[Declaration] = []
    shadow_reasons: List[str] = []

    for group in groups.values():
        # Latest version wins within one source. Publication time breaks
        # ties between two versions that were both left active.
        latest_version = max(item.version for item in group)
        version_group = [
            item for item in group if item.version == latest_version
        ]
        latest = max(
            version_group,
            key=lambda item: (
                item.published_at or datetime.min,
                item.id,
            ),
        )
        candidates.append(latest)
        for item in group:
            if item is latest:
                continue
            shadowed.append(item)
            if item.version < latest_version:
                reason = 'superseded by version {} from {}'.format(
                    latest_version, item.source)
            else:
                reason = 'superseded by a later notice from {}'.format(
                    item.source)
            shadow_reasons.append(reason)

    if not candidates:
        return Resolution(
            day=day,
            as_of=as_of,
            status=ResolutionStatus.NO_DATA,
            working=None,
            active=(),
            withdrawn=tuple(_sort_declarations(withdrawn)),
            not_published=tuple(_sort_declarations(not_published)),
            reason='no active declaration for this business day',
        )

    top_priority = max(item.level_priority for item in candidates)
    top = [item for item in candidates
           if item.level_priority == top_priority]
    lower = [item for item in candidates
             if item.level_priority < top_priority]

    conclusions = {item.working for item in top}
    if len(conclusions) > 1:
        for item in lower:
            shadowed.append(item)
            shadow_reasons.append(
                'overridden by a higher-priority source ({})'.format(
                    top[0].level.label)
            )
        return Resolution(
            day=day,
            as_of=as_of,
            status=ResolutionStatus.PENDING,
            working=None,
            winner=None,
            conflicts=tuple(_sort_declarations(top)),
            active=all_active(),
            shadowed=tuple(shadowed),
            withdrawn=tuple(_sort_declarations(withdrawn)),
            not_published=tuple(_sort_declarations(not_published)),
            reason='same-level sources disagree; awaiting adjudication',
            shadow_reasons=tuple(shadow_reasons),
        )

    winner = max(
        top,
        key=lambda item: (
            item.published_at or datetime.min,
            item.version,
            item.source,
            item.id,
        ),
    )

    for item in top:
        if item is winner:
            continue
        shadowed.append(item)
        shadow_reasons.append(
            'same level and same conclusion; tie-broken in favour of '
            '{} v{}'.format(winner.source, winner.version)
        )
    for item in lower:
        shadowed.append(item)
        shadow_reasons.append(
            'overridden by higher-priority source {} ({})'.format(
                winner.source, winner.level.label)
        )

    return Resolution(
        day=day,
        as_of=as_of,
        status=ResolutionStatus.RESOLVED,
        working=winner.working,
        winner=winner,
        active=all_active(),
        shadowed=tuple(shadowed),
        withdrawn=tuple(_sort_declarations(withdrawn)),
        not_published=tuple(_sort_declarations(not_published)),
        reason='resolved from {} declaration(s) at {} level'.format(
            len(top), winner.level.label),
        shadow_reasons=tuple(shadow_reasons),
    )


def _sort_declarations(declarations):
    return sorted(
        declarations,
        key=lambda item: (
            item.level_priority,
            item.source,
            item.version,
            item.id,
        ),
    )


class LayeredCalendar:
    """Resolve declarations from group, branch and personal sources.

    Parameters
    ----------
    declarations:
        Optional iterable of :class:`Declaration` objects (or dictionaries
        accepted by :meth:`add`).
    base_calendar:
        Optional calendar used as the group default when no declaration is
        active. It only needs an ``is_working_day(day)`` method. The base
        calendar is not consulted while a declaration is active.
    """

    def __init__(self, declarations=None, base_calendar=None,
                 region=None):
        self._declarations: List[Declaration] = []
        self.base_calendar = base_calendar
        if region is not None:
            if base_calendar is not None:
                raise TypeError("Provide either `region` or "
                                "`base_calendar`, not both")
            from .registry import registry
            calendar_class = registry.get(region)
            if calendar_class is None:
                raise ValueError(f"Unknown calendar region: {region!r}")
            self.base_calendar = calendar_class()
        for declaration in declarations or ():
            self.add(declaration)

    def __len__(self):
        return len(self._declarations)

    def __iter__(self):
        return iter(self._declarations)

    @property
    def declarations(self):
        """All declarations, in insertion order."""
        return tuple(self._declarations)

    @property
    def calendar(self):
        """Base calendar used as the group default, if any."""
        return self.base_calendar

    def add(self, declaration=None, **kwargs):
        """Add a declaration.

        ``declaration`` may be a :class:`Declaration`, a mapping, or ``None``
        when the fields are supplied as keyword arguments.
        """
        if declaration is None:
            declaration = Declaration(**kwargs)
        elif isinstance(declaration, dict):
            data = dict(declaration)
            data.update(kwargs)
            declaration = Declaration(**data)
        elif kwargs:
            declaration = replace(declaration, **kwargs)

        if not isinstance(declaration, Declaration):
            raise TypeError("Expected a Declaration")
        self._declarations.append(declaration)
        return declaration

    def extend(self, declarations):
        """Add several declarations and return them."""
        return tuple(self.add(item) for item in declarations)

    def declare(self, day, working=None, **kwargs):
        """Create and add a declaration in one call."""
        return self.add(Declaration(day, working=working, **kwargs))

    def for_day(self, day):
        """Return all declarations concerning ``day``."""
        day = _as_date(day)
        return tuple(item for item in self._declarations if item.day == day)

    def withdraw(self, declaration, at=None):
        """Withdraw a declaration and return the updated copy.

        ``declaration`` may be a :class:`Declaration`, its ``id`` or its
        ``notice_id``. ``at`` defaults to the current UTC time. The stored
        declaration is replaced; it is not deleted, so earlier queries can
        still be explained.
        """
        at = _now() if at is None else at
        if isinstance(declaration, Declaration):
            declaration_id = declaration.id
        else:
            declaration_id = str(declaration)

        for index, item in enumerate(self._declarations):
            if declaration_id in (item.id, item.notice_id):
                updated = replace(item, withdrawn_at=at)
                self._declarations[index] = updated
                return updated
        raise KeyError(f"Unknown declaration: {declaration!r}")

    # ``revoke`` is the common name in notice-based systems.
    revoke = withdraw

    def remove(self, declaration):
        """Delete a declaration. Prefer :meth:`withdraw` for an audit trail."""
        if isinstance(declaration, Declaration):
            self._declarations.remove(declaration)
        else:
            for index, item in enumerate(self._declarations):
                if str(declaration) in (item.id, item.notice_id):
                    del self._declarations[index]
                    return
            raise KeyError(f"Unknown declaration: {declaration!r}")

    def resolve(self, day=None, as_of=None, **kwargs):
        """Resolve one business day at a notification publication time.

        ``day`` is the business day. ``as_of`` is the publication-time
        cut-off: notices published after it, and notices already withdrawn,
        do not participate. ``business_day`` is accepted as an alias for
        ``day``. ``notification_time`` and ``publication_time`` are accepted
        as aliases for ``as_of``.
        """
        if day is None:
            day = kwargs.pop('business_day', None)
        if day is None:
            raise TypeError("`day` (or `business_day`) is required")

        if as_of is None:
            for alias in ('notification_time', 'publication_time'):
                if alias in kwargs:
                    as_of = kwargs.pop(alias)
                    break
        if kwargs:
            unexpected = ', '.join(sorted(kwargs))
            raise TypeError(f"Unexpected resolve argument(s): {unexpected}")

        return _resolve_declarations(
            self._declarations, day, as_of, self.base_calendar)

    def query(self, day=None, as_of=None, **kwargs):
        """Alias for :meth:`resolve`."""
        return self.resolve(day=day, as_of=as_of, **kwargs)

    def explain(self, day=None, as_of=None, **kwargs):
        """Return the textual explanation for one query."""
        return self.resolve(day=day, as_of=as_of, **kwargs).explain()

    def is_working_day(self, day, as_of=None, **kwargs):
        """Return the resolved working-day value, or ``None`` if pending."""
        return self.resolve(day=day, as_of=as_of, **kwargs).working

    def is_holiday(self, day, as_of=None, **kwargs):
        """Return the inverse of :meth:`is_working_day`, or ``None``."""
        working = self.is_working_day(day, as_of=as_of, **kwargs)
        if working is None:
            return None
        return not working


def resolve(declarations, day, as_of=None, base_calendar=None):
    """Resolve ``day`` from a collection of declarations."""
    return LayeredCalendar(
        declarations, base_calendar=base_calendar
    ).resolve(day, as_of=as_of)


def explain(declarations, day, as_of=None, base_calendar=None):
    """Return the textual explanation for a collection of declarations."""
    return resolve(
        declarations, day, as_of=as_of, base_calendar=base_calendar
    ).explain()
