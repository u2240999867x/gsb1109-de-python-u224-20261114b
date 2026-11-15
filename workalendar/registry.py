from importlib import import_module

from .core import Calendar
from .exceptions import AmbiguousRegionMappingError, ISORegistryError


class IsoRegistry:
    """
    Registry for all calendars retrievable
    by ISO 3166-2 codes associated with countries
    where they are used as official calendars.

    Two letter codes are favored for any subdivisions.
    """

    STANDARD_MODULES = (
        # Europe Countries
        'europe',
        # United States of America
        'usa',
        # American continent outside of USA
        'america',
        # African continent
        'africa',
        # Asia
        'asia',
        # Oceania
        'oceania',
    )

    def __init__(self, load_standard_modules=True):
        self.region_registry = dict()
        if load_standard_modules:
            for module_name in self.STANDARD_MODULES:
                module = f'workalendar.{module_name}'
                all_classes = getattr(import_module(module), '__all__')
                self.load_module_from_items(module, all_classes)

    def register(self, iso_code, cls):
        """
        Store the ``cls`` in the region_registry.
        """
        if not issubclass(cls, Calendar):
            raise ISORegistryError(
                f"Class `{cls}` is not a Calendar class"
            )
        self.region_registry[iso_code] = cls

    def load_module_from_items(self, module_name, items):
        """
        Load all registered classes in the registry
        """
        for item in items:
            cls = getattr(import_module(module_name), item)
            iso_stuff = getattr(cls, '__iso_code', None)
            if iso_stuff:
                iso_code, class_name = iso_stuff
                if iso_code and cls.__name__ == class_name:
                    self.register(iso_code, cls)

    def get(self, iso_code):
        """
        Retrieve calendar class associated with given ``iso_code``.

        If calendar of subdivision is not registered
        (for subdivision like ISO codes, e.g. GB-ENG)
        returns calendar of containing region
        (e.g. United Kingdom for ISO code GB) if it's available.

        :rtype: Calendar
        """
        return self.region_registry.get(iso_code)

    def get_subregions(self, iso_code):
        """
        Returns subregion calendar classes for given region iso_code.

        >>> registry = IsoRegistry()
        >>> # assuming calendars registered are: DE, DE-HH, DE-BE
        >>> registry.get_subregions('DE')
        {'DE-HH': <class 'workalendar.europe.germany.Hamburg'>,
        'DE-BE': <class 'workalendar.europe.germany.Berlin'>}
        :rtype dict
        :return dict where keys are ISO codes strings
        and values are calendar classes
        """
        items = dict()
        for key, value in self.region_registry.items():
            if key.startswith(f"{iso_code}-"):
                items[key] = value
        return items

    def get_calendars(self, region_codes=None, include_subregions=False):
        """
        Returns calendar classes for regions

        :param region_codes list of ISO codes for selected regions. If empty,
                            the function will return all items from the
                            registry.
        :param include_subregions boolean if subregions
        of selected regions should be included in result
        :rtype dict
        :return dict where keys are ISO codes strings
        and values are calendar classes
        """
        if not region_codes:
            # Here it contains all subregions
            if include_subregions:
                return self.region_registry.copy()
            items = {k: v for k, v in self.region_registry.items()
                     if '-' not in k}
            return items

        items = dict()
        for code in region_codes:
            try:
                items[code] = self.region_registry[code]
            except KeyError:
                continue
            if include_subregions:
                items.update(self.get_subregions(code))
        return items


class RegionMapping:
    """
    A single effective-dated correspondence record between an external
    region code (e.g. a county code ingested into a data warehouse, or a
    legacy province abbreviation) and the ISO registry.

    A record may carry an ``iso_code`` (a direct mapping rule) and/or a
    ``parent`` code (an explicit parent fallback used to walk the parent
    chain when no direct rule applies). Both ``start`` and ``end`` are
    inclusive bounds of the effective interval; ``None`` means the
    interval is open on that side.
    """

    def __init__(self, code, iso_code=None, parent=None, start=None, end=None):
        if not code:
            raise ISORegistryError(
                "Region mapping records require a non-empty code"
            )
        if iso_code is None and parent is None:
            raise ISORegistryError(
                "Region mapping records require an `iso_code` "
                "and/or a `parent`"
            )
        if start is not None and end is not None and start > end:
            raise ISORegistryError(
                f"Invalid effective interval for code {code!r}: "
                f"{start} > {end}"
            )
        self.code = code
        self.iso_code = iso_code
        self.parent = parent
        self.start = start
        self.end = end

    def covers(self, business_date):
        """
        Return True if the record is effective on ``business_date``.
        """
        if self.start is not None and business_date < self.start:
            return False
        if self.end is not None and business_date > self.end:
            return False
        return True

    def __repr__(self):
        return (
            f"RegionMapping(code={self.code!r}, iso_code={self.iso_code!r}, "
            f"parent={self.parent!r}, start={self.start!r}, end={self.end!r})"
        )


class EffectiveRegionMap:
    """
    A query layer on top of an ``IsoRegistry`` resolving external region
    codes with correspondence records that are effective for a given
    business date.

    Resolution of a code for a business date works as follows:

    1. If exactly one direct mapping rule (a record with an ``iso_code``)
       is effective for the code on this date, it wins and the parent
       chain is *not* consulted.
    2. Only when the code lacks a direct rule, the lookup follows the
       parent chain effective on this date (records carrying a
       ``parent``), until a direct rule or a registered ISO code is
       found.
    3. If the same code is covered by several effective intervals
       yielding conflicting targets, the resolution is left for
       confirmation by raising ``AmbiguousRegionMappingError``.

    Records are append-only and scoped to their own effective interval:
    backfilling a belonging record for a given period never changes the
    resolution of unrelated dates, and the underlying registry is only
    read, never mutated.
    """

    def __init__(self, iso_registry=None):
        self.iso_registry = iso_registry
        self.records = []

    def _get_registry(self):
        if self.iso_registry is not None:
            return self.iso_registry
        return registry

    def add(self, code, iso_code=None, parent=None, start=None, end=None):
        """
        Append a correspondence record to the map.

        :param code: the external region code.
        :param iso_code: optional ISO code this record maps ``code`` to.
        :param parent: optional explicit parent code used as a fallback
                       when no direct rule is effective for ``code``.
        :param start: first business date (inclusive) of effectiveness.
        :param end: last business date (inclusive) of effectiveness.
        :rtype: RegionMapping
        """
        record = RegionMapping(
            code, iso_code=iso_code, parent=parent, start=start, end=end)
        self.records.append(record)
        return record

    def _effective_records(self, code, business_date):
        return [
            record for record in self.records
            if record.code == code and record.covers(business_date)
        ]

    def resolve(self, code, business_date):
        """
        Resolve an external region ``code`` to an ISO code effective on
        ``business_date``.

        Returns ``None`` when the code cannot be resolved. Raises
        ``AmbiguousRegionMappingError`` when conflicting records are
        effective on this date (left for confirmation).

        :rtype: str or None
        """
        registry = self._get_registry()
        visited = []
        current = code
        while True:
            if current in visited:
                chain = " -> ".join(visited + [current])
                raise ISORegistryError(
                    f"Cyclic parent chain detected: {chain}"
                )
            visited.append(current)
            records = self._effective_records(current, business_date)
            iso_codes = {r.iso_code for r in records if r.iso_code}
            if len(iso_codes) > 1:
                raise AmbiguousRegionMappingError(
                    f"Code {current!r} matches several mapping rules on "
                    f"{business_date}: {sorted(iso_codes)}; "
                    "left for confirmation"
                )
            if iso_codes:
                return iso_codes.pop()
            parents = {r.parent for r in records if r.parent}
            if len(parents) > 1:
                raise AmbiguousRegionMappingError(
                    f"Code {current!r} matches several parent assignments "
                    f"on {business_date}: {sorted(parents)}; "
                    "left for confirmation"
                )
            if parents:
                current = parents.pop()
                continue
            # No rule and no parent for this date: the code may itself be
            # a registered ISO code.
            if registry.get(current) is not None:
                return current
            return None

    def get_calendar(self, code, business_date):
        """
        Retrieve the calendar class for an external region ``code``
        effective on ``business_date``.

        Returns ``None`` when the code cannot be resolved.

        :rtype: Calendar
        """
        iso_code = self.resolve(code, business_date)
        if iso_code is None:
            return None
        return self._get_registry().get(iso_code)


registry = IsoRegistry()
