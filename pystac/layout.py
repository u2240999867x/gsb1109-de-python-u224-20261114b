from __future__ import annotations

import os
import posixpath
import warnings
from abc import ABC, abstractmethod
from collections import OrderedDict
from itertools import chain
from string import Formatter
from typing import TYPE_CHECKING, Any, Callable, Iterator, cast

import pystac
from pystac.errors import LayoutConflictError
from pystac.utils import is_absolute_href, is_file_path, make_absolute_href

if TYPE_CHECKING:
    from pystac.catalog import Catalog
    from pystac.collection import Collection
    from pystac.item import Item
    from pystac.link import Link
    from pystac.stac_object import STACObject


class TemplateError(Exception):
    """DEPRECATED.

    .. deprecated:: 1.7.0
        Use :class:`pystac.errors.TemplateError` instead.

    Exception thrown when an error occurs during converting a template
    string into data for :class:`~pystac.layout.LayoutTemplate`
    """

    def __init__(self, *args, **kwargs) -> None:  # type: ignore
        warnings.warn(
            message=(
                "TemplateError in pystac.layout is deprecated and will be "
                "removed in pystac version 2.0.0. Use TemplateError in "
                "pystac.errors instead."
            ),
            category=DeprecationWarning,
        )
        super().__init__(*args, **kwargs)


class LayoutTemplate:
    """Represents a template that can be used for deriving paths or other information
    based on properties of STAC objects supplied as a template string.

    Template strings use variables that can be pulled out of Catalogs, Collections
    and Items. The variable names can represent properties on the object itself,
    or keys in the dictionaries of the properties or extra_fields on the
    object (if they exist). The search order is as follows:

    - The object's attributes
    - Keys in the ``properties`` attribute, if it exists.
    - Keys in the ``extra_fields`` attribute, if it exists.

    Some special keys can be used in template variables:

    +--------------------+--------------------------------------------------------+
    | Template variable  | Meaning                                                |
    +====================+========================================================+
    | ``year``           | The year of an Item's datetime, or                     |
    |                    | start_datetime if datetime is null                     |
    +--------------------+--------------------------------------------------------+
    | ``month``          | The month of an Item's datetime, or                    |
    |                    | start_datetime if datetime is null                     |
    +--------------------+--------------------------------------------------------+
    | ``day``            | The day of an Item's datetime, or                      |
    |                    | start_datetime if datetime is null                     |
    +--------------------+--------------------------------------------------------+
    | ``date``           | The date (iso format) of an Item's                     |
    |                    | datetime, or start_datetime if datetime is null        |
    +--------------------+--------------------------------------------------------+
    | ``collection``     | The collection ID of an Item's collection.             |
    +--------------------+--------------------------------------------------------+

    The forward slash (``/``) should be used as path separator in the template
    string regardless of the system path separator (thus both in POSIX-compliant
    and Windows environments).

    Examples::

        # Uses the year, month and day of the item
        template = LayoutTemplate("${year}/${month}/${day}")

        # Uses a custom extension properties found on in the item properties.
        template = LayoutTemplate("${landsat:path}/${landsat:row}")

        # Uses the collection ID and a common metadata property for an item.
        template = LayoutTemplate("${collection}/${common_metadata.license}")

    Args:
        template : The template string to use.
        defaults : A dictionary of template vars to values. These values
            will be used in case a value cannot be derived from a stac object.
    """

    template: str
    """The template string to use."""

    defaults: dict[str, str]
    """A dictionary of template vars to values. These values will be used in case a
    value cannot be derived from a stac object."""

    template_vars: list[str]
    """List of template vars to use when templating."""

    # Special template vars specific to Items
    ITEM_TEMPLATE_VARS = ["date", "year", "month", "day", "collection"]

    def __init__(self, template: str, defaults: dict[str, str] | None = None) -> None:
        self.template = template
        self.defaults = defaults or {}

        # Generate list of template vars
        template_vars = []
        for formatter_parse_result in Formatter().parse(template):
            v = formatter_parse_result[1]
            if v is not None:
                if formatter_parse_result[2] != "":
                    v = f"{v}:{formatter_parse_result[2]}"
                template_vars.append(v)
        self.template_vars = template_vars

    def _get_template_value(self, stac_object: STACObject, template_var: str) -> Any:
        if template_var in self.ITEM_TEMPLATE_VARS:
            if isinstance(stac_object, pystac.Item):
                # Datetime
                dt = stac_object.datetime
                if dt is None:
                    dt = stac_object.common_metadata.start_datetime
                if dt is None:
                    raise pystac.TemplateError(
                        "Item {} does not have a datetime or "
                        "datetime range set; cannot template {} in {}".format(
                            stac_object, template_var, self.template
                        )
                    )

                if template_var == "year":
                    return dt.year
                if template_var == "month":
                    return dt.month
                if template_var == "day":
                    return dt.day
                if template_var == "date":
                    return dt.date().isoformat()

                # Collection
                if template_var == "collection":
                    if stac_object.collection_id is not None:
                        return stac_object.collection_id
                    raise pystac.TemplateError(
                        f"Item {stac_object} does not have a collection ID set; "
                        f"cannot template {template_var} in {self.template}"
                    )
            else:
                raise pystac.TemplateError(
                    '"{}" cannot be used to template non-Item {} in {}'.format(
                        template_var, stac_object, self.template
                    )
                )

        # Allow dot-notation properties for arbitrary object values.
        props = template_var.split(".")
        prop_source: pystac.STACObject | dict[str, Any] | None = None
        error = pystac.TemplateError(
            "Cannot find property {} on {} for template {}".format(
                template_var, stac_object, self.template
            )
        )

        try:
            if hasattr(stac_object, props[0]):
                prop_source = stac_object

            if prop_source is None and hasattr(stac_object, "properties"):
                obj_props: dict[str, Any] | None = stac_object.properties
                if obj_props is not None and props[0] in obj_props:
                    prop_source = obj_props

            if prop_source is None and hasattr(stac_object, "extra_fields"):
                extra_fields: dict[str, Any] | None = stac_object.extra_fields
                if extra_fields is not None and props[0] in extra_fields:
                    prop_source = extra_fields

            if prop_source is None:
                raise error

            v: Any = prop_source
            for prop in template_var.split("."):
                if isinstance(v, dict):
                    if prop not in v:
                        raise error
                    v = v[prop]
                else:
                    if not hasattr(v, prop):
                        raise error
                    v = getattr(v, prop)
        except pystac.TemplateError as e:
            if template_var in self.defaults:
                return self.defaults[template_var]
            raise e

        return v

    def get_template_values(self, stac_object: STACObject) -> dict[str, Any]:
        """Gets a dictionary of template variables to values derived from
        the given stac_object. If the template vars cannot be found in the
        stac object, and defaults was supplied to this template, a default
        value is used; if there is no default then this will raise an error.

        Args:
            stac_object : The STACObject to derive template
                variable values from.

        Returns:
            [dict]: A dictionary with keys being the template variables
            and values being the respective values based on the given
            stac object.

        Raises:
            pystac.TemplateError: If a value for a template variable cannot be
                derived from the stac object and there is no default,
                this error will be raised.
        """
        return OrderedDict(
            [(k, self._get_template_value(stac_object, k)) for k in self.template_vars]
        )

    def substitute(self, stac_object: STACObject) -> str:
        """Substitutes the values derived from
        :meth:`~pystac.layout.LayoutTemplate.get_template_values` into
        the template string for this template.

        Args:
            stac_object : The STACObject to derive template
                variable values from.

        Returns:
            str: The original template supplied to this LayoutTemplate
            with template variables replaced by the values derived
            from this stac object.

        Raises:
            pystac.TemplateError: If a value for a template variable cannot be
                derived from the stac object and there is no default,
                this error will be raised.
        """
        parts = self.get_template_values(stac_object)

        s = self.template
        for key, value in parts.items():
            s = s.replace("${" + f"{key}" + "}", f"{value}")
        return s


class HrefLayoutStrategy(ABC):
    """Base class for HREF Layout strategies."""

    def get_href(
        self, stac_object: STACObject, parent_dir: str, is_root: bool = False
    ) -> str:
        if is_file_path(parent_dir):
            parent_dir = os.path.dirname(parent_dir)

        if isinstance(stac_object, pystac.Item):
            return self.get_item_href(stac_object, parent_dir)
        elif isinstance(stac_object, pystac.Collection):
            return self.get_collection_href(stac_object, parent_dir, is_root)
        elif isinstance(stac_object, pystac.Catalog):
            return self.get_catalog_href(stac_object, parent_dir, is_root)
        else:
            raise pystac.STACError(f"Unknown STAC object type {stac_object}")

    @abstractmethod
    def get_catalog_href(self, cat: Catalog, parent_dir: str, is_root: bool) -> str:
        raise NotImplementedError

    @abstractmethod
    def get_collection_href(
        self, col: Collection, parent_dir: str, is_root: bool
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def get_item_href(self, item: Item, parent_dir: str) -> str:
        raise NotImplementedError


class CustomLayoutStrategy(HrefLayoutStrategy):
    """Layout strategy that allows users to supply functions to dictate
    stac object paths.

    Args:
        catalog_func : A function that takes
            an catalog, a parent directory, and a flag specifying whether
            or not this catalog is the root. If it is the root, its usually
            best to not create a subdirectory and put the Catalog file directly
            in the parent directory. Must return the string path.
        collection_func : A function that
            is used for collections in the same manner at ``catalog_func``. This
            takes the same parameters.
        item_func  : A function that takes
            an item and a parent directory and returns the path to be used
            for the item.
        fallback_strategy : The fallback strategy to
            use if a function is not provided for a stac object type. Defaults to
            :class:`~pystac.layout.BestPracticesLayoutStrategy`
    """

    catalog_func: Callable[[Catalog, str, bool], str] | None
    """A function that takes a :class:`~pystac.Catalog`, a parent directory, and a
    boolean specifying whether or not this Catalog is the root. If it is the root, it
    is usually best to not create a subdirectory and put the Catalog file directly
    in the parent directory. Must return the string path."""

    collection_func: Callable[[Collection, str, bool], str] | None
    """A function that is used for collections in the same manner as
    :attr:`~catalog_func`. This takes the same parameters."""

    fallback_strategy: HrefLayoutStrategy
    """The fallback strategy to use if a function is not provided for a stac object
    type. Defaults to :class:`~pystac.layout.BestPracticesLayoutStrategy`."""

    item_func: Callable[[Item, str], str] | None
    """An optional function that takes an :class:`~pystac.Item` and a parent directory
    and returns the path to be used for the Item."""

    def __init__(
        self,
        catalog_func: Callable[[Catalog, str, bool], str] | None = None,
        collection_func: Callable[[Collection, str, bool], str] | None = None,
        item_func: Callable[[Item, str], str] | None = None,
        fallback_strategy: HrefLayoutStrategy | None = None,
    ):
        self.item_func = item_func
        self.collection_func = collection_func
        self.catalog_func = catalog_func
        if fallback_strategy is None:
            fallback_strategy = BestPracticesLayoutStrategy()
        self.fallback_strategy = fallback_strategy

    def get_catalog_href(self, cat: Catalog, parent_dir: str, is_root: bool) -> str:
        if self.catalog_func is not None:
            result = self.catalog_func(cat, parent_dir, is_root)
            if result is not None:
                return result
        return self.fallback_strategy.get_catalog_href(cat, parent_dir, is_root)

    def get_collection_href(
        self, col: Collection, parent_dir: str, is_root: bool
    ) -> str:
        if self.collection_func is not None:
            result = self.collection_func(col, parent_dir, is_root)
            if result is not None:
                return result
        return self.fallback_strategy.get_collection_href(col, parent_dir, is_root)

    def get_item_href(self, item: Item, parent_dir: str) -> str:
        if self.item_func is not None:
            result = self.item_func(item, parent_dir)
            if result is not None:
                return result
        return self.fallback_strategy.get_item_href(item, parent_dir)


class TemplateLayoutStrategy(HrefLayoutStrategy):
    """Layout strategy that can take strings to be supplied to
    :class:`~pystac.layout.LayoutTemplate` s to derive paths. Template strings
    can be supplied for catalogs, collections and items separately. If no
    template is supplied, a fallback layout strategy is supplied, which defaults
    to the :class:`~pystac.layout.BestPracticesLayoutStrategy`.

    All templated paths will be joined with the parent directory of the stac
    object.

    Args:
        catalog_template : The template string to use for catalog paths.
            Must be a valid template string that can be used by
            :class:`~pystac.layout.LayoutTemplate`
        collection_template : The template string to use for collection paths.
            Must be a valid template string that can be used by
            :class:`~pystac.layout.LayoutTemplate`
        item_template : The template string to use for item paths.
            Must be a valid template string that can be used by
            :class:`~pystac.layout.LayoutTemplate`
        fallback_strategy : The fallback strategy to
            use if a template is not provided. Defaults to
            :class:`~pystac.layout.BestPracticesLayoutStrategy`
    """

    catalog_template: LayoutTemplate | None
    """The template string to use for catalog paths. Must be a valid template string
    that can be used by :class:`~pystac.layout.LayoutTemplate`."""

    collection_template: LayoutTemplate | None
    """The template string to use for collection paths. Must be a valid template string
    that can be used by :class:`~pystac.layout.LayoutTemplate`."""

    fallback_strategy: HrefLayoutStrategy
    """The fallback strategy to use if a template is not provided. Defaults to
    :class:`~pystac.layout.BestPracticesLayoutStrategy`."""

    item_template: LayoutTemplate | None
    """The template string to use for item paths. Must be a valid template string that
    can be used by :class:`~pystac.layout.LayoutTemplate`."""

    def __init__(
        self,
        catalog_template: str | None = None,
        collection_template: str | None = None,
        item_template: str | None = None,
        fallback_strategy: HrefLayoutStrategy | None = None,
    ):
        self.catalog_template = (
            LayoutTemplate(catalog_template) if catalog_template is not None else None
        )
        self.collection_template = (
            LayoutTemplate(collection_template)
            if collection_template is not None
            else None
        )
        self.item_template = (
            LayoutTemplate(item_template) if item_template is not None else None
        )

        if fallback_strategy is None:
            fallback_strategy = BestPracticesLayoutStrategy()
        self.fallback_strategy = fallback_strategy

    def get_catalog_href(self, cat: Catalog, parent_dir: str, is_root: bool) -> str:
        if is_root or self.catalog_template is None:
            return self.fallback_strategy.get_catalog_href(cat, parent_dir, is_root)
        else:
            template_path = self.catalog_template.substitute(cat)
            if not template_path.endswith(".json"):
                template_path = posixpath.join(template_path, cat.DEFAULT_FILE_NAME)

            return posixpath.join(parent_dir, template_path)

    def get_collection_href(
        self, col: Collection, parent_dir: str, is_root: bool
    ) -> str:
        if is_root or self.collection_template is None:
            return self.fallback_strategy.get_collection_href(col, parent_dir, is_root)
        else:
            template_path = self.collection_template.substitute(col)
            if not template_path.endswith(".json"):
                template_path = posixpath.join(template_path, col.DEFAULT_FILE_NAME)

            return posixpath.join(parent_dir, template_path)

    def get_item_href(self, item: Item, parent_dir: str) -> str:
        if self.item_template is None:
            return self.fallback_strategy.get_item_href(item, parent_dir)
        else:
            template_path = self.item_template.substitute(item)
            if not template_path.endswith(".json"):
                template_path = posixpath.join(template_path, f"{item.id}.json")

            return posixpath.join(parent_dir, template_path)


class BestPracticesLayoutStrategy(HrefLayoutStrategy):
    """Layout strategy that represents the catalog layout described
    in the :stac-spec:`STAC Best Practices documentation
    <best-practices.md>`

    For a root catalog or collection, this will use the filename 'catalog.json'
    or 'collection.json' to the given directory. For a non-root catalog or collection,
    the ID will be used as a subdirectory, e.g. ``${id}/catalog.json`` or
    ``${id}/collection.json``. For items, a subdirectory with a name of the item
    ID will be made, and the item ID will be used in the filename, i.e.
    ``${id}/${id}.json``

    All paths are appended to the parent directory.
    """

    def get_catalog_href(self, cat: Catalog, parent_dir: str, is_root: bool) -> str:
        if is_root:
            cat_root = parent_dir
        else:
            cat_root = posixpath.join(parent_dir, f"{cat.id}")

        return posixpath.join(cat_root, cat.DEFAULT_FILE_NAME)

    def get_collection_href(
        self, col: Collection, parent_dir: str, is_root: bool
    ) -> str:
        if is_root:
            col_root = parent_dir
        else:
            col_root = posixpath.join(parent_dir, f"{col.id}")

        return posixpath.join(col_root, col.DEFAULT_FILE_NAME)

    def get_item_href(self, item: Item, parent_dir: str) -> str:
        item_root = posixpath.join(parent_dir, f"{item.id}")

        return posixpath.join(item_root, f"{item.id}.json")


class AsIsLayoutStrategy(HrefLayoutStrategy):
    """Layout strategy that simply preserves the current href of all objects.

    If any object doesn't have a self href, a ValueError is raised.
    """

    def get_catalog_href(self, cat: Catalog, parent_dir: str, is_root: bool) -> str:
        href = cat.self_href
        if href is None:
            raise ValueError(
                f"Catalog is missing href, required for AsIsLayoutStrategy: {cat}"
            )
        else:
            return href

    def get_collection_href(
        self, col: Collection, parent_dir: str, is_root: bool
    ) -> str:
        href = col.self_href
        if href is None:
            raise ValueError(
                f"Collection is missing href, required for AsIsLayoutStrategy: {col}"
            )
        else:
            return href

    def get_item_href(self, item: Item, parent_dir: str) -> str:
        href = item.self_href
        if href is None:
            raise ValueError(
                f"Item is missing href, required for AsIsLayoutStrategy: {item}"
            )
        else:
            return href


class APILayoutStrategy(HrefLayoutStrategy):
    """Layout strategy that represents the STAC API endpoint layout described
    in the `STAC API Specifications
    <https://github.com/radiantearth/stac-api-spec/blob/v1.0.0/overview.md#endpoints>`_

    The URL of the root catalog will be the base URL of the API.
    Other catalogs will be listed underneath their parent catalog
    at ``./${id}``.
    Collections will be found underneath the root or their parent
    catalog at ``./collections/${id}``. Collection cannot be the root themselves.
    Items will be found underneath their collections at
    ``./collections/${collection}/items/${id}``.
    """

    def get_catalog_href(self, cat: Catalog, parent_dir: str, is_root: bool) -> str:
        if is_root:
            cat_href = parent_dir
        else:
            cat_href = posixpath.join(parent_dir, f"{cat.id}")

        return cat_href

    def get_collection_href(
        self, col: Collection, parent_dir: str, is_root: bool
    ) -> str:
        if is_root:
            raise ValueError("Collections cannot be root")

        col_root = posixpath.join(parent_dir, "collections")

        return posixpath.join(col_root, f"{col.id}")

    def get_item_href(self, item: Item, parent_dir: str) -> str:
        item_root = posixpath.join(parent_dir, "items")

        return posixpath.join(item_root, f"{item.id}")


class PlannedObject:
    """The planned relocation of a single STAC object, as computed by
    :func:`~pystac.layout.plan_layout`.

    Attributes:
        stac_object : The object that would be relocated.
        target_href : The HREF the object would be published at.
        internal_links : Links of the object whose targets are also part of
            the plan. The HREFs of these links would be rewritten when the
            layout is published.
        external_links : Links of the object whose targets are not part of
            the plan (e.g. links to absolute URLs, or to STAC objects outside
            of the relocated tree). These links would be kept as-is.
    """

    stac_object: STACObject
    """The object that would be relocated."""

    target_href: str
    """The HREF the object would be published at."""

    internal_links: list[Link]
    """Links to objects that are part of the same plan; these would be
    rewritten to the planned HREFs of their targets."""

    external_links: list[Link]
    """Links to targets outside of the plan; these would be kept as-is."""

    def __init__(
        self,
        stac_object: STACObject,
        target_href: str,
        internal_links: list[Link],
        external_links: list[Link],
    ) -> None:
        self.stac_object = stac_object
        self.target_href = target_href
        self.internal_links = internal_links
        self.external_links = external_links

    def __repr__(self) -> str:
        return f"<PlannedObject {self.stac_object!r} target_href={self.target_href}>"


class LayoutPlan:
    """The result of an offline dry-run of HREF normalization, as computed by
    :func:`~pystac.layout.plan_layout`.

    A plan lists, for every object in a catalog tree, the HREF it would be
    published at, which of its links would be rewritten (internal references)
    and which would be kept (external references). Computing a plan never
    mutates the catalog and never writes any files, so conflicts - two
    distinct objects mapping to the same HREF - can be inspected via
    :attr:`~pystac.layout.LayoutPlan.conflicts` before anything is published.

    Use :meth:`~pystac.layout.LayoutPlan.raise_for_conflicts` to guard a
    subsequent publish, and :meth:`~pystac.layout.LayoutPlan.verify` to check
    that a (re-read) catalog matches the relations predicted by the plan.
    """

    root_href: str
    """The absolute HREF the plan was computed against."""

    entries: list[PlannedObject]
    """The planned relocation of every object in the catalog tree, in the
    order the objects were visited."""

    def __init__(self, root_href: str, entries: list[PlannedObject]) -> None:
        self.root_href = root_href
        self.entries = entries
        self._target_by_id = {
            id(entry.stac_object): entry.target_href for entry in entries
        }

    def __iter__(self) -> Iterator[PlannedObject]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def target_href_for(self, stac_object: STACObject) -> str | None:
        """Returns the planned target HREF for the given object, or ``None``
        if the object is not part of this plan."""
        return self._target_by_id.get(id(stac_object))

    @property
    def conflicts(self) -> dict[str, list[STACObject]]:
        """A mapping of target HREFs to the distinct STAC objects that would
        be published at that HREF, for every HREF claimed by more than one
        object. Multiple links pointing to the same object do not constitute
        a conflict; only distinct objects computing to the same path do.
        """
        by_href: dict[str, list[STACObject]] = {}
        for entry in self.entries:
            objects = by_href.setdefault(entry.target_href, [])
            if all(entry.stac_object is not obj for obj in objects):
                objects.append(entry.stac_object)
        return {href: objs for href, objs in by_href.items() if len(objs) > 1}

    def raise_for_conflicts(self) -> None:
        """Raises a :class:`~pystac.errors.LayoutConflictError` if this plan
        would publish two or more distinct objects at the same HREF. Intended
        to be called before any files are written.

        Raises:
            LayoutConflictError: If the plan contains conflicts.
        """
        conflicts = self.conflicts
        if conflicts:
            lines = ["Layout plan assigns the same HREF to multiple STAC objects:"]
            for href, objects in sorted(conflicts.items()):
                lines.append(f"  {href}: " + ", ".join(repr(obj) for obj in objects))
            raise LayoutConflictError("\n".join(lines))

    def expected_relations(self) -> dict[str, set[tuple[str, str]]]:
        """Computes the link relations every object is expected to have once
        the plan is published, keyed by the planned target HREF of each
        object. Relations are ``(rel, absolute target HREF)`` pairs; internal
        references are mapped to the planned HREFs of their targets, external
        references are kept as-is. ``self`` links are not included, as the
        planned target HREF itself is the expected self address.
        """
        relations: dict[str, set[tuple[str, str]]] = {}
        for entry in self.entries:
            rels: set[tuple[str, str]] = set()
            for link in entry.internal_links:
                target_href = self._target_by_id.get(id(link.target))
                if target_href is not None:
                    rels.add((str(link.rel), target_href))
            for link in entry.external_links:
                href = link.get_absolute_href()
                if href is not None:
                    rels.add((str(link.rel), href))
            relations[entry.target_href] = rels
        return relations

    def verify(self, catalog: Catalog) -> list[str]:
        """Compares the object relations of the given catalog against the
        relations predicted by this plan.

        This is intended to be used on a catalog that was published with a
        conflict-free plan and read back from its new location: every planned
        object must be found at its planned HREF, with exactly the internal
        and external relations the plan predicted.

        Args:
            catalog : The catalog to check, e.g. one read back from the
                location the plan was published to.

        Returns:
            list[str]: A list of mismatch descriptions. An empty list means
            the catalog's object relations match the plan.
        """
        expected = self.expected_relations()
        actual: dict[str, set[tuple[str, str]]] = {}
        mismatches: list[str] = []
        seen: set[int] = set()
        for cat, children, items in catalog.walk():
            for obj in chain([cat], children, items):
                if id(obj) in seen:
                    continue
                seen.add(id(obj))
                self_href = obj.get_self_href()
                if self_href is None:
                    mismatches.append(f"{obj!r} has no self HREF")
                    continue
                rels: set[tuple[str, str]] = set()
                for link in obj.get_links():
                    if link.rel == pystac.RelType.SELF:
                        continue
                    href = link.get_absolute_href()
                    if href is not None:
                        rels.add((str(link.rel), href))
                actual[self_href] = rels

        for href in sorted(expected):
            if href not in actual:
                mismatches.append(f"missing object at planned HREF {href}")
            elif actual[href] != expected[href]:
                missing = sorted(expected[href] - actual[href])
                unexpected = sorted(actual[href] - expected[href])
                mismatches.append(
                    f"relations differ at {href}: "
                    f"missing {missing}, unexpected {unexpected}"
                )
        for href in sorted(actual):
            if href not in expected:
                mismatches.append(f"unexpected object at {href}")
        return mismatches


def plan_layout(
    catalog: Catalog,
    root_href: str,
    strategy: HrefLayoutStrategy | None = None,
    skip_unresolved: bool = False,
) -> LayoutPlan:
    """Performs an offline dry-run of HREF normalization for the given
    catalog, without mutating the catalog or writing any files.

    The traversal and HREF computation mirror
    :meth:`Catalog.normalize_hrefs <pystac.Catalog.normalize_hrefs>`: every
    object in the tree is visited exactly once, even if several links point
    to it, and the given (or the catalog's own) layout strategy is used to
    compute the HREF each object would be published at. The resulting
    :class:`~pystac.layout.LayoutPlan` records, for every object, its target
    HREF, the internal references that would be rewritten and the external
    references that would be kept. Distinct objects computing to the same
    HREF are reported via :attr:`LayoutPlan.conflicts
    <pystac.layout.LayoutPlan.conflicts>` so the conflict can be resolved
    before anything is published.

    Args:
        catalog : The root of the catalog tree to plan the layout for.
        root_href : The absolute HREF that all HREFs will be planned
            against. Relative HREFs are made absolute against the current
            working directory.
        strategy : The layout strategy to use. If not provided, defaults to
            the layout strategy of the catalog or its root, falling back to
            :class:`~pystac.layout.BestPracticesLayoutStrategy`.
        skip_unresolved : Skip unresolved links when walking the tree.
            Defaults to False.

    Returns:
        LayoutPlan: The plan describing the target HREF and link rewrites
        for every object in the catalog tree.
    """
    _strategy = catalog._get_strategy(strategy)

    # Planning requires an absolute path
    if not is_absolute_href(root_href):
        root_href = make_absolute_href(root_href, os.getcwd(), start_is_dir=True)

    planned: list[tuple[STACObject, str]] = []
    planned_ids: set[int] = set()

    def process_item(
        item: Item, _root_href: str, is_root: bool, parent: Catalog | None
    ) -> None:
        if not skip_unresolved:
            item.resolve_links()

        # Abort as the intended parent is not the actual parent
        # https://github.com/stac-utils/pystac/issues/1116
        if parent is not None and item.get_parent() != parent:
            return

        # Several links may point to the same object; plan it only once
        if id(item) in planned_ids:
            return
        planned_ids.add(id(item))

        planned.append((item, _strategy.get_href(item, _root_href, is_root)))

    def process_catalog(
        cat: Catalog,
        _root_href: str,
        is_root: bool,
        parent: Catalog | None = None,
    ) -> None:
        if not skip_unresolved:
            cat.resolve_links()

        # Abort as the intended parent is not the actual parent
        # https://github.com/stac-utils/pystac/issues/1116
        if parent is not None and cat.get_parent() != parent:
            return

        # Several links may point to the same object; plan it only once
        if id(cat) in planned_ids:
            return
        planned_ids.add(id(cat))

        new_self_href = _strategy.get_href(cat, _root_href, is_root)
        planned.append((cat, new_self_href))

        for link in cat.get_links():
            if skip_unresolved and not link.is_resolved():
                continue
            elif link.rel == pystac.RelType.ITEM:
                link.resolve_stac_object(root=catalog.get_root())
                process_item(
                    cast(pystac.Item, link.target), new_self_href, is_root, cat
                )
            elif link.rel == pystac.RelType.CHILD:
                link.resolve_stac_object(root=catalog.get_root())
                process_catalog(
                    cast(pystac.Catalog, link.target),
                    new_self_href,
                    is_root=False,
                    parent=cat,
                )

    process_catalog(catalog, root_href, is_root=True)

    entries: list[PlannedObject] = []
    for stac_object, target_href in planned:
        internal_links: list[Link] = []
        external_links: list[Link] = []
        for link in stac_object.get_links():
            if link.rel == pystac.RelType.SELF:
                # The self link is the object's own address; it is
                # represented by the planned target HREF.
                continue
            if link.is_resolved() and id(link.target) in planned_ids:
                internal_links.append(link)
            else:
                external_links.append(link)
        entries.append(
            PlannedObject(stac_object, target_href, internal_links, external_links)
        )

    return LayoutPlan(root_href, entries)
