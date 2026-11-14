"""Provenance tracing along ``derived_from`` links.

This module walks the ``derived_from`` link graph of STAC Items and
produces an auditable source list for a derived product. It distinguishes:

* root sources: Items reachable through ``derived_from`` links that do not
  themselves have any ``derived_from`` links (genuine source material, never
  a derived product);
* unresolved links: ``derived_from`` targets whose external catalog was not
  delivered with the package and therefore cannot be inspected;
* cycles: relationships where two (or more) derived products point at each
  other, so no root can be established along that branch.

Both the number of distinct root sources and the number of times each root
is actually referenced along the graph are reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pystac
from pystac.item import Item
from pystac.link import Link


@dataclass(frozen=True)
class SourceReference:
    """A single root source reached while tracing provenance.

    Attributes:
        id : ID of the root source Item.
        href : Self href of the root source Item, if known.
        reference_count : Number of distinct paths from the traced product
            to this root source (each path is one actual citation).
    """

    id: str
    href: Optional[str]
    reference_count: int


@dataclass(frozen=True)
class UnresolvedReference:
    """A ``derived_from`` link whose target cannot be inspected.

    Attributes:
        source_id : ID of the Item holding the unresolvable link.
        target_href : HREF of the target that could not be resolved, or
            ``None`` if the link carries no href information.
    """

    source_id: str
    target_href: Optional[str]


@dataclass(frozen=True)
class CyclicRelationship:
    """One directed relationship that participates in a cycle.

    Attributes:
        source_id : ID of the Item the ``derived_from`` link originates from.
        target_id : ID of the Item the link points at.
        target_href : HREF of the target, if known.
    """

    source_id: str
    target_id: str
    target_href: Optional[str]


@dataclass
class ProvenanceReport:
    """Result of tracing the provenance of one derived product.

    Attributes:
        product_id : ID of the Item whose provenance was traced.
        roots : Root sources with their actual reference counts.
        unresolved : ``derived_from`` links that could not be resolved and
            are pending analysis by the cataloger.
        cycles : Individual relationships forming cycles, for the cataloger
            to inspect.
    """

    product_id: str
    roots: list[SourceReference] = field(default_factory=list)
    unresolved: list[UnresolvedReference] = field(default_factory=list)
    cycles: list[CyclicRelationship] = field(default_factory=list)

    @property
    def root_source_count(self) -> int:
        """Number of distinct, confirmed root source Items."""
        return len(self.roots)

    @property
    def total_reference_count(self) -> int:
        """Total number of citations of root sources across all paths."""
        return sum(root.reference_count for root in self.roots)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the report to a plain JSON-compatible dictionary."""
        return {
            "product_id": self.product_id,
            "root_source_count": self.root_source_count,
            "total_reference_count": self.total_reference_count,
            "roots": [
                {
                    "id": root.id,
                    "href": root.href,
                    "reference_count": root.reference_count,
                }
                for root in self.roots
            ],
            "unresolved": [
                {
                    "source_id": ref.source_id,
                    "target_href": ref.target_href,
                }
                for ref in self.unresolved
            ],
            "cycles": [
                {
                    "source_id": rel.source_id,
                    "target_id": rel.target_id,
                    "target_href": rel.target_href,
                }
                for rel in self.cycles
            ],
        }


def _node_key(item: Item) -> str:
    href = item.get_self_href()
    return href if href is not None else f"item-id:{item.id}"


def _resolve_target(link: Link) -> Optional[Item]:
    target = link.target
    if isinstance(target, Item):
        return target
    try:
        resolved = link.resolve_stac_object().target
    except Exception:
        return None
    if isinstance(resolved, Item):
        return resolved
    return None


def trace_provenance(product: Item) -> ProvenanceReport:
    """Trace every ``derived_from`` chain starting at ``product``.

    The walk follows ``derived_from`` links depth-first. An Item without any
    ``derived_from`` links is a confirmed root source; derived products are
    never counted as roots themselves. Unresolvable links (for example when
    an external catalog was not delivered with the package) are recorded as
    pending resolution rather than guessed at. When two generations of
    products point at each other, the specific relationships forming the
    cycle are reported while any independently confirmed roots are still
    listed.

    Args:
        product : The derived product Item to trace.

    Returns:
        ProvenanceReport: Root sources (with distinct count and per-source
            reference counts), unresolved links, and cyclic relationships.
    """
    report = ProvenanceReport(product_id=product.id)

    root_counts: dict[str, int] = {}
    root_hrefs: dict[str, Optional[str]] = {}
    unresolved_seen: set[tuple[str, Optional[str]]] = set()
    cycle_seen: set[frozenset[tuple[str, str]]] = set()

    # Each stack frame is (item, path). A path entry holds an Item and the
    # derived_from Link (if any) that was followed to reach it. The path-based
    # cycle check alone guarantees termination: every explored path is simple,
    # so parallel links and diamond-shaped merges each count as separate
    # citations instead of being collapsed by edge dedup.
    stack: list[tuple[Item, list[tuple[Item, Optional[Link]]]]] = [
        (product, [(product, None)])
    ]

    while stack:
        current, path = stack.pop()
        path_keys = [_node_key(item) for item, _ in path]
        links = current.get_links(pystac.RelType.DERIVED_FROM)

        for link in links:
            target = _resolve_target(link)
            if target is None:
                target_href = link.get_target_str()
                marker = (current.id, target_href)
                if marker not in unresolved_seen:
                    unresolved_seen.add(marker)
                    report.unresolved.append(
                        UnresolvedReference(
                            source_id=current.id, target_href=target_href
                        )
                    )
                continue

            target_key = _node_key(target)

            if target_key in path_keys:
                cycle_start = path_keys.index(target_key)
                cycle_edges: list[tuple[str, str, Optional[str]]] = []
                for idx in range(cycle_start + 1, len(path)):
                    source_item = path[idx - 1][0]
                    target_item, incoming_link = path[idx]
                    cycle_edges.append(
                        (
                            source_item.id,
                            target_item.id,
                            (
                                incoming_link.get_target_str()
                                if incoming_link is not None
                                else None
                            ),
                        )
                    )
                cycle_edges.append(
                    (current.id, target.id, link.get_target_str())
                )
                cycle_marker = frozenset(
                    (source_id, edge_target_id)
                    for source_id, edge_target_id, _ in cycle_edges
                )
                if cycle_marker not in cycle_seen:
                    cycle_seen.add(cycle_marker)
                    report.cycles.extend(
                        CyclicRelationship(
                            source_id=source_id,
                            target_id=edge_target_id,
                            target_href=edge_href,
                        )
                        for source_id, edge_target_id, edge_href in cycle_edges
                    )
                continue

            derived_links = target.get_links(pystac.RelType.DERIVED_FROM)
            if not derived_links:
                root_counts[target.id] = root_counts.get(target.id, 0) + 1
                root_hrefs.setdefault(target.id, target.get_self_href())
            else:
                stack.append((target, path + [(target, link)]))

    for root_id in sorted(root_counts):
        report.roots.append(
            SourceReference(
                id=root_id,
                href=root_hrefs[root_id],
                reference_count=root_counts[root_id],
            )
        )

    return report
