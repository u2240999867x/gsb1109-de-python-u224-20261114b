"""
Stitch table rows extracted from separate PDF pages into one manifest.

Manifest tables frequently break across pages. At a page boundary the
extracted data may show:

* a repeated header row,
* the last line of the previous page reprinted at the top of the next
  page (so it must be counted only once),
* a long description split into two fragments that must be joined back
  together, or
* a keyless fragment that could plausibly continue more than one earlier
  row, in which case no join is guessed and an issue is reported instead.

Seams are judged from the table's extracted columns, its (possibly
repeated) header, its business-key columns, and its quantity column.
Every stitched row keeps the original ``(page_index, row_index)`` of each
source row, so provenance is never lost.

The module only consumes the plain row lists returned by
``Page.extract_table`` / ``Page.extract_tables``; it does not open PDFs.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

T_cells = List[Optional[str]]
T_row = Sequence[Optional[str]]
T_location = Tuple[int, int]
T_page = Sequence[T_row]
T_pages = Sequence[T_page]

STATUS_WHOLE = "whole"
STATUS_JOINED = "joined"
STATUS_AMBIGUOUS = "ambiguous"

DEFAULT_LOOKBACK = 5

_NUMBER_RE = re.compile(r"[-+]?[0-9][0-9,]*\.?[0-9]*")


@dataclass(frozen=True)
class Lineage:
    """Where a stitched row (or one of its pieces) came from."""

    page_index: int
    row_index: int

    @property
    def page_number(self) -> int:
        return self.page_index + 1

    @property
    def location(self) -> T_location:
        return (self.page_index, self.row_index)


@dataclass
class StitchedRow:
    """One manifest line, possibly assembled from several source rows."""

    cells: T_cells
    lineage: List[Lineage]
    status: str = STATUS_WHOLE
    joined_at: List[int] = field(default_factory=list)
    reprints: List[Lineage] = field(default_factory=list)

    @property
    def sources(self) -> List[T_location]:
        """Original ``(page_index, row_index)`` of every joined piece."""
        return [line.location for line in self.lineage]

    @property
    def is_joined(self) -> bool:
        return len(self.lineage) > 1

    def cell(self, column: int) -> Optional[str]:
        return self.cells[column] if 0 <= column < len(self.cells) else None

    def quantity(self, quantity_column: Optional[int]) -> Optional[Decimal]:
        if quantity_column is None:
            return None
        return parse_quantity(self.cell(quantity_column))


@dataclass
class SeamIssue:
    """A seam that could not be resolved automatically."""

    page_index: int
    row_index: int
    fragment: T_cells
    candidate_rows: List[T_location]
    reason: str

    @property
    def page_number(self) -> int:
        return self.page_index + 1

    @property
    def location(self) -> T_location:
        return (self.page_index, self.row_index)


def parse_quantity(value: Optional[str]) -> Optional[Decimal]:
    """Parse the leading number of a quantity cell, or ``None``."""
    if value is None:
        return None
    match = _NUMBER_RE.search(value.strip())
    if match is None:
        return None
    try:
        return Decimal(match.group().replace(",", ""))
    except InvalidOperation:
        return None


def _normalize_cell(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_row(row: T_row, columns: int) -> T_cells:
    cells = [_normalize_cell(cell) for cell in row]
    if len(cells) > columns:
        raise ValueError("Row is wider than the resolved table columns")
    return cells + [None] * (columns - len(cells))


def _is_blank_row(cells: T_cells) -> bool:
    return all(cell is None for cell in cells)


def _is_fragment(
    cells: T_cells,
    key_columns: Sequence[int],
    quantity_column: Optional[int],
) -> bool:
    """A continuation piece carries neither a business key nor a quantity."""
    key_empty = not key_columns or all(
        cells[column] is None for column in key_columns
    )
    quantity_empty = (
        quantity_column is None or cells[quantity_column] is None
    )
    return key_empty and quantity_empty


def _combine_cells(
    tail: T_cells,
    head: T_cells,
    key_columns: Sequence[int],
    quantity_column: Optional[int],
) -> Optional[T_cells]:
    """
    Merge a continuation row (``head``) into a candidate earlier row
    (``tail``). Return the merged cells, or ``None`` when the two rows
    conflict (different keys or quantities) or have nothing to join.
    """
    key_set = set(key_columns)
    merged: T_cells = []
    evidence = False
    for column, (tail_cell, head_cell) in enumerate(zip(tail, head)):
        if head_cell is None:
            merged.append(tail_cell)
        elif tail_cell is None:
            merged.append(head_cell)
            evidence = True
        elif tail_cell == head_cell:
            # Same cell reprinted on both sides of the seam: count once.
            merged.append(tail_cell)
        elif column in key_set or column == quantity_column:
            # Two different keys/quantities cannot be one item.
            return None
        else:
            # Long text split across the seam: join it back together.
            merged.append(f"{tail_cell} {head_cell}")
            evidence = True
    return merged if evidence else None


def _row_with_source(
    rows: Sequence[StitchedRow], source: Lineage
) -> Optional[StitchedRow]:
    for row in reversed(rows):
        if source in row.lineage:
            return row
    return None


def _resolve_header(header: Optional[object]) -> Optional[List[int]]:
    if header is None:
        return None
    if isinstance(header, int):
        indices = [header]
    elif isinstance(header, (list, tuple)):
        indices = list(header)
    else:
        raise ValueError("header must be an int, a sequence of ints, or None")
    if any(not isinstance(index, int) or index < 0 for index in indices):
        raise ValueError("header row indices must be non-negative ints")
    return sorted(set(indices))


@dataclass
class StitchResult:
    """The stitched manifest: rows, unresolved issues, and provenance."""

    rows: List[StitchedRow]
    issues: List[SeamIssue]
    columns: int
    key_columns: List[int]
    quantity_column: Optional[int]
    header_rows: List[Lineage]
    duplicate_header_rows: List[Lineage]
    reprinted_rows: List[Lineage]
    confirmations: Dict[T_location, T_location] = field(default_factory=dict)
    _pages: T_pages = field(default_factory=tuple, repr=False)
    _header: Optional[List[int]] = field(default=None, repr=False)
    _lookback: int = DEFAULT_LOOKBACK

    @property
    def totals(self) -> Dict[str, Any]:
        quantity_total: Optional[Decimal] = None
        if self.quantity_column is not None:
            quantity_total = Decimal(0)
            for row in self.rows:
                quantity = row.quantity(self.quantity_column)
                if quantity is not None:
                    quantity_total += quantity
        return {
            "row_count": len(self.rows),
            "joined_row_count": sum(1 for row in self.rows if row.is_joined),
            "ambiguous_row_count": sum(
                1 for row in self.rows if row.status == STATUS_AMBIGUOUS
            ),
            "reprinted_row_count": len(self.reprinted_rows),
            "duplicate_header_count": len(self.duplicate_header_rows),
            "quantity_total": quantity_total,
        }

    def to_cells(self) -> List[T_cells]:
        """Plain list-of-lists view of the stitched item rows."""
        return [list(row.cells) for row in self.rows]

    def apply_confirmation(
        self,
        page_index: int,
        row_index: int,
        target_page_index: int,
        target_row_index: int,
    ) -> "StitchResult":
        """
        Apply one collector-confirmed seam.

        The fragment at ``(page_index, row_index)`` is joined to the
        candidate source row at ``(target_page_index, target_row_index)``.
        Only that seam's row, totals, and provenance change; every other
        row and issue is recomputed identically.
        """
        location = (page_index, row_index)
        target = (target_page_index, target_row_index)
        issue = next(
            (candidate for candidate in self.issues if candidate.location == location),
            None,
        )
        if issue is None:
            raise ValueError(f"No unresolved seam issue at {location}")
        if target not in issue.candidate_rows:
            raise ValueError(
                f"{target} is not a recorded candidate for the issue at {location}"
            )
        if location in self.confirmations:
            raise ValueError(f"Seam at {location} already has a confirmation")
        confirmations = {**self.confirmations, location: target}
        return stitch_pages(
            self._pages,
            key_columns=self.key_columns,
            quantity_column=self.quantity_column,
            header=self._header,
            lookback=self._lookback,
            confirmations=confirmations,
        )


def stitch_pages(
    pages: T_pages,
    *,
    key_columns: Optional[Sequence[int]] = None,
    quantity_column: Optional[int] = None,
    header: Optional[object] = 0,
    lookback: int = DEFAULT_LOOKBACK,
    confirmations: Optional[Dict[T_location, T_location]] = None,
) -> StitchResult:
    """
    Stitch per-page extracted table rows into one manifest.

    :param pages: one list of extracted rows per page; each row is a list
        of cell strings (``None``/blank for empty cells).
    :param key_columns: indices of the business-key column(s), e.g. item
        numbers. Two rows with different keys are always separate items,
        even when their descriptions are identical.
    :param quantity_column: index of the quantity column, used both to
        guard joins (a headed quantity means a complete new line) and to
        total the manifest.
    :param header: row index (or indices) of the header on the first page.
        Exact repeats on later pages are dropped. Pass ``None`` to disable
        header detection.
    :param lookback: how many rows back on the previous page a keyless
        fragment may be matched against.
    :param confirmations: collector-supplied forced seams mapping a
        fragment location to its confirmed source-row location.
    """
    if not pages:
        raise ValueError("pages must contain at least one page")
    if lookback < 1:
        raise ValueError("lookback must be at least 1")

    header_indices = _resolve_header(header)
    key_list = list(key_columns or [])
    confirmations = dict(confirmations or {})

    columns = max(
        (len(row) for page in pages for row in page),
        default=0,
    )
    if columns == 0:
        raise ValueError("pages contain no columns")

    for index in key_list:
        if not 0 <= index < columns:
            raise ValueError(f"key column {index} is out of range")
    if quantity_column is not None and not 0 <= quantity_column < columns:
        raise ValueError(f"quantity column {quantity_column} is out of range")

    normalized: List[List[T_cells]] = [
        [_normalize_row(row, columns) for row in page] for page in pages
    ]
    if header_indices is not None and any(
        index >= len(normalized[0]) for index in header_indices
    ):
        raise ValueError("header row index is out of range on the first page")

    signatures = (
        [normalized[0][index] for index in header_indices]
        if header_indices is not None
        else []
    )
    header_index_set = set(header_indices or [])

    header_rows: List[Lineage] = (
        [Lineage(0, index) for index in header_indices]
        if header_indices is not None
        else []
    )
    duplicate_header_rows: List[Lineage] = []
    logical: List[List[Tuple[int, T_cells]]] = []
    for page_index, page_rows in enumerate(normalized):
        page_logical: List[Tuple[int, T_cells]] = []
        for row_index, cells in enumerate(page_rows):
            if _is_blank_row(cells):
                continue
            if page_index == 0 and row_index in header_index_set:
                continue
            if page_index > 0 and cells in signatures:
                duplicate_header_rows.append(Lineage(page_index, row_index))
                continue
            page_logical.append((row_index, cells))
        logical.append(page_logical)

    output_rows: List[StitchedRow] = []
    issues: List[SeamIssue] = []
    reprinted_rows: List[Lineage] = []

    def append_whole(page_index: int, row_index: int, cells: T_cells) -> None:
        output_rows.append(
            StitchedRow(cells=list(cells), lineage=[Lineage(page_index, row_index)])
        )

    def merge_into(
        target: StitchedRow,
        page_index: int,
        row_index: int,
        head_cells: T_cells,
    ) -> None:
        merged = _combine_cells(
            target.cells, head_cells, key_list, quantity_column
        )
        if merged is None:
            raise ValueError(
                f"Confirmed seam at {(page_index, row_index)} conflicts "
                "with its target row"
            )
        target.joined_at.append(len(target.lineage))
        target.lineage.append(Lineage(page_index, row_index))
        target.cells = merged
        target.status = STATUS_JOINED

    for page_index, page_logical in enumerate(logical):
        if page_index == 0:
            for row_index, cells in page_logical:
                append_whole(page_index, row_index, cells)
            continue

        previous_logical = logical[page_index - 1]
        cursor = 0
        while cursor < len(page_logical):
            row_index, cells = page_logical[cursor]
            location = (page_index, row_index)
            handled = False

            # An exact copy of the previous page's last line is a reprint
            # and must never be counted a second time. It may follow the
            # continuation fragment of that same last line.
            if previous_logical:
                tail_row_index, tail_cells = previous_logical[-1]
                if cells == tail_cells:
                    target = _row_with_source(
                        output_rows, Lineage(page_index - 1, tail_row_index)
                    )
                    reprint = Lineage(page_index, row_index)
                    reprinted_rows.append(reprint)
                    if target is not None:
                        target.reprints.append(reprint)
                    handled = True

            if not handled and location in confirmations:
                target_page, target_row = confirmations[location]
                if target_page != page_index - 1:
                    raise ValueError(
                        f"Confirmed target for {location} must be on the "
                        f"previous page ({page_index - 1}), got page "
                        f"{target_page}"
                    )
                target = _row_with_source(
                    output_rows, Lineage(target_page, target_row)
                )
                if target is None:
                    raise ValueError(
                        f"Confirmed target {(target_page, target_row)} not found"
                    )
                merge_into(target, page_index, row_index, cells)
                handled = True

            if not handled and cursor == 0 and _is_fragment(
                cells, key_list, quantity_column
            ):
                window = previous_logical[-lookback:]
                candidates: List[Tuple[int, T_cells, bool]] = []
                for candidate_index, candidate_cells in reversed(window):
                    if key_list and not any(
                        candidate_cells[column] is not None
                        for column in key_list
                    ):
                        continue
                    if (
                        not key_list
                        and quantity_column is not None
                        and candidate_cells[quantity_column] is None
                    ):
                        continue
                    merged = _combine_cells(
                        candidate_cells,
                        cells,
                        key_list,
                        quantity_column,
                    )
                    if merged is None:
                        continue
                    # A fragment extending text already present on the
                    # nearest item points at that item; one that only fills
                    # blank cells could belong to any blank candidate.
                    overlap = any(
                        candidate_cells[column] is not None
                        and cells[column] is not None
                        for column in range(len(cells))
                    )
                    candidates.append((candidate_index, candidate_cells, overlap))

                if len(candidates) == 1:
                    candidate_index, _, _ = candidates[0]
                    target = _row_with_source(
                        output_rows,
                        Lineage(page_index - 1, candidate_index),
                    )
                    if target is not None:
                        merge_into(target, page_index, row_index, cells)
                        handled = True
                elif len(candidates) > 1 and candidates[0][2]:
                    candidate_index, _, _ = candidates[0]
                    target = _row_with_source(
                        output_rows,
                        Lineage(page_index - 1, candidate_index),
                    )
                    if target is not None:
                        merge_into(target, page_index, row_index, cells)
                        handled = True
                elif len(candidates) > 1:
                    issues.append(
                        SeamIssue(
                            page_index=page_index,
                            row_index=row_index,
                            fragment=list(cells),
                            candidate_rows=[
                                (page_index - 1, candidate_index)
                                for candidate_index, _, _ in candidates
                            ],
                            reason=(
                                "Keyless fragment matches more than one "
                                "candidate row; supply a confirmed seam"
                            ),
                        )
                    )
                    output_rows.append(
                        StitchedRow(
                            cells=list(cells),
                            lineage=[Lineage(page_index, row_index)],
                            status=STATUS_AMBIGUOUS,
                        )
                    )
                    handled = True

            if not handled:
                append_whole(page_index, row_index, cells)
            cursor += 1

    return StitchResult(
        rows=output_rows,
        issues=issues,
        columns=columns,
        key_columns=key_list,
        quantity_column=quantity_column,
        header_rows=header_rows,
        duplicate_header_rows=duplicate_header_rows,
        reprinted_rows=reprinted_rows,
        confirmations=confirmations,
        _pages=pages,
        _header=header_indices,
        _lookback=lookback,
    )
