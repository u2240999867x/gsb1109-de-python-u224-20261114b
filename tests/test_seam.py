#!/usr/bin/env python
import logging
import unittest
from decimal import Decimal

import pytest

from pdfplumber import seam

logging.disable(logging.ERROR)

HEADER = ["SKU", "Description", "Qty"]


class TestSeam(unittest.TestCase):
    def test_repeated_header_is_dropped_not_counted(self):
        pages = [
            [HEADER, ["A-1", "Gadget", "1"], ["A-2", "Widget", "2"]],
            [HEADER, ["A-3", "Gizmo", "3"]],
        ]
        result = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2
        )
        assert result.to_cells() == [
            ["A-1", "Gadget", "1"],
            ["A-2", "Widget", "2"],
            ["A-3", "Gizmo", "3"],
        ]
        assert result.header_rows == [seam.Lineage(0, 0)]
        assert result.duplicate_header_rows == [seam.Lineage(1, 0)]
        assert result.totals["row_count"] == 3
        assert result.issues == []

    def test_reprinted_last_line_is_counted_once(self):
        pages = [
            [HEADER, ["A-1", "Gadget", "1"], ["A-2", "Widget", "2"]],
            [
                HEADER,
                ["A-2", "Widget", "2"],  # exact reprint of prior last line
                ["A-3", "Gizmo", "3"],
            ],
        ]
        result = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2
        )
        assert result.to_cells() == [
            ["A-1", "Gadget", "1"],
            ["A-2", "Widget", "2"],
            ["A-3", "Gizmo", "3"],
        ]
        widget = result.rows[1]
        assert widget.sources == [(0, 2)]
        assert widget.reprints == [seam.Lineage(1, 1)]
        assert result.reprinted_rows == [seam.Lineage(1, 1)]
        assert result.totals["row_count"] == 3
        assert result.totals["quantity_total"] == Decimal("6")

    def test_split_long_description_is_joined_back(self):
        pages = [
            [
                HEADER,
                ["A-1", "Gadget", "1"],
                ["A-2", "Very long widget description that", "2"],
            ],
            [
                HEADER,
                [None, "wraps onto the next page", None],
                ["A-3", "Gizmo", "3"],
            ],
        ]
        result = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2
        )
        assert len(result.rows) == 3
        joined = result.rows[1]
        assert joined.status == seam.STATUS_JOINED
        assert joined.cells == [
            "A-2",
            "Very long widget description that wraps onto the next page",
            "2",
        ]
        # Both original (page, row) numbers are retained.
        assert joined.sources == [(0, 2), (1, 1)]
        assert result.totals["row_count"] == 3
        assert result.totals["quantity_total"] == Decimal("6")
        assert result.issues == []

    def test_identical_descriptions_remain_two_items(self):
        pages = [
            [
                HEADER,
                ["A-1", "Same description", "1"],
                ["A-2", "Same description", "1"],
            ],
            [HEADER, ["A-3", "Gizmo", "2"]],
        ]
        result = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2
        )
        assert result.totals["row_count"] == 3
        assert [row.cells[0] for row in result.rows] == ["A-1", "A-2", "A-3"]
        assert result.totals["quantity_total"] == Decimal("4")

    def test_keyless_fragment_with_two_candidates_is_not_forced(self):
        pages = [
            [
                HEADER,
                ["A-1", None, "1"],
                ["A-2", None, "1"],
            ],
            [
                HEADER,
                [None, "mystery continuation text", None],
                ["A-3", "Gizmo", "3"],
            ],
        ]
        result = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2
        )
        assert len(result.issues) == 1
        issue = result.issues[0]
        assert issue.location == (1, 1)
        assert issue.candidate_rows == [(0, 2), (0, 1)]
        assert result.rows[-1].status != seam.STATUS_JOINED
        ambiguous = [
            row for row in result.rows if row.status == seam.STATUS_AMBIGUOUS
        ]
        assert len(ambiguous) == 1
        assert ambiguous[0].sources == [(1, 1)]
        # Neither candidate was merged into.
        assert all(not row.is_joined for row in result.rows[:2])

    def test_confirmation_only_changes_that_seam(self):
        pages = [
            [
                HEADER,
                ["A-1", None, "1"],
                ["A-2", None, "1"],
            ],
            [
                HEADER,
                [None, "mystery continuation text", None],
                ["A-3", "Gizmo", "3"],
            ],
        ]
        before = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2
        )
        first_item_before = list(before.rows[0].cells)
        result = before.apply_confirmation(1, 1, 0, 2)

        assert result.issues == []
        target = result.rows[1]
        assert target.status == seam.STATUS_JOINED
        assert target.cells == ["A-2", "mystery continuation text", "1"]
        assert target.sources == [(0, 2), (1, 1)]
        # The other candidate item and every unrelated row are untouched.
        assert result.rows[0].cells == first_item_before
        assert result.rows[0].sources == [(0, 1)]
        assert result.rows[2].cells == ["A-3", "Gizmo", "3"]
        # Only this seam's totals change: two rows became one.
        assert result.totals["row_count"] == before.totals["row_count"] - 1
        assert result.totals["quantity_total"] == Decimal("5")
        assert result.confirmations == {(1, 1): (0, 2)}

    def test_confirmation_validation(self):
        pages = [
            [HEADER, ["A-1", None, "1"], ["A-2", None, "1"]],
            [HEADER, [None, "mystery continuation text", None]],
        ]
        result = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2
        )
        with pytest.raises(ValueError):
            result.apply_confirmation(0, 1, 0, 2)
        with pytest.raises(ValueError):
            # (0, 0) is the dropped header row, not a recorded candidate.
            result.apply_confirmation(1, 1, 0, 0)
        confirmed = result.apply_confirmation(1, 1, 0, 2)
        with pytest.raises(ValueError):
            confirmed.apply_confirmation(1, 1, 0, 2)

    def test_fragment_carrying_quantity_starts_a_new_item(self):
        pages = [
            [HEADER, ["A-1", "Gadget", "1"]],
            [
                HEADER,
                [None, "loose line with its own quantity", "4"],
                ["A-3", "Gizmo", "3"],
            ],
        ]
        result = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2
        )
        assert result.issues == []
        assert result.to_cells() == [
            ["A-1", "Gadget", "1"],
            [None, "loose line with its own quantity", "4"],
            ["A-3", "Gizmo", "3"],
        ]
        assert all(not row.is_joined for row in result.rows)
        assert result.totals["quantity_total"] == Decimal("8")

    def test_conflicting_keys_are_never_joined(self):
        pages = [
            [HEADER, ["A-1", "Gadget", "1"]],
            [HEADER, ["B-9", "Gadget", "1"]],
        ]
        result = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2
        )
        assert result.totals["row_count"] == 2
        assert all(not row.is_joined for row in result.rows)

    def test_original_row_numbers_survive_blank_and_header_rows(self):
        pages = [
            [HEADER, [None, None, None], ["A-1", "Gadget", "1"]],
            [HEADER, [None, None, None], ["A-2", "Widget", "2"]],
        ]
        result = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2
        )
        assert result.rows[0].sources == [(0, 2)]
        assert result.rows[1].sources == [(1, 2)]
        assert result.duplicate_header_rows == [seam.Lineage(1, 0)]

    def test_quantity_parsing_and_total(self):
        pages = [
            [
                HEADER,
                ["A-1", "Gadget", "12 pcs"],
                ["A-2", "Widget", "1,250.00"],
            ],
            [HEADER, ["A-3", "Gizmo", None]],
        ]
        result = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2
        )
        assert result.totals["quantity_total"] == Decimal("1262.00")
        assert result.rows[2].quantity(2) is None

    def test_three_page_manifest_with_all_seam_types(self):
        pages = [
            [
                HEADER,
                ["A-1", "Gadget", "1"],
                ["A-2", "split description", "2"],
            ],
            [
                HEADER,
                [None, "continued here", None],
                ["A-2", "split description", "2"],  # reprint
                ["A-3", "Gizmo", "3"],
            ],
            [
                HEADER,
                ["A-4", "Sprocket", "4"],
            ],
        ]
        result = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2
        )
        assert result.to_cells() == [
            ["A-1", "Gadget", "1"],
            ["A-2", "split description continued here", "2"],
            ["A-3", "Gizmo", "3"],
            ["A-4", "Sprocket", "4"],
        ]
        assert result.rows[1].sources == [(0, 2), (1, 1)]
        assert result.rows[1].reprints == [seam.Lineage(1, 2)]
        assert result.reprinted_rows == [seam.Lineage(1, 2)]
        assert [line.location for line in result.duplicate_header_rows] == [
            (1, 0),
            (2, 0),
        ]
        assert result.totals["row_count"] == 4
        assert result.totals["quantity_total"] == Decimal("10")
        assert result.issues == []

    def test_multi_row_and_disabled_headers(self):
        multi_header = [
            ["Manifest", "Manifest", "Manifest"],
            ["SKU", "Description", "Qty"],
        ]
        pages = [
            multi_header + [["A-1", "Gadget", "1"]],
            multi_header + [["A-2", "Widget", "2"]],
        ]
        result = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2, header=[0, 1]
        )
        assert result.to_cells() == [
            ["A-1", "Gadget", "1"],
            ["A-2", "Widget", "2"],
        ]
        assert result.header_rows == [seam.Lineage(0, 0), seam.Lineage(0, 1)]
        assert [
            line.location for line in result.duplicate_header_rows
        ] == [(1, 0), (1, 1)]

        plain = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2, header=None
        )
        assert plain.totals["row_count"] == 6
        assert plain.header_rows == []
        assert plain.duplicate_header_rows == []

    def test_confirmation_then_reprint_on_same_page(self):
        pages = [
            [
                ["SKU", "Description", "Qty"],
                ["A-1", None, "1"],
                ["A-2", None, "1"],
            ],
            [
                ["SKU", "Description", "Qty"],
                [None, "mystery continuation text", None],
                ["A-2", None, "1"],  # reprint of prior last line
                ["A-3", "Gizmo", "3"],
            ],
        ]
        result = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2
        )
        assert len(result.issues) == 1
        confirmed = result.apply_confirmation(1, 1, 0, 2)
        assert confirmed.issues == []
        assert confirmed.rows[1].cells == [
            "A-2",
            "mystery continuation text",
            "1",
        ]
        assert confirmed.rows[1].sources == [(0, 2), (1, 1)]
        assert confirmed.rows[1].reprints == [seam.Lineage(1, 2)]
        assert confirmed.reprinted_rows == [seam.Lineage(1, 2)]
        assert confirmed.totals["row_count"] == 3
        assert confirmed.totals["quantity_total"] == Decimal("5")

    def test_blank_strings_normalize_to_empty_cells(self):
        pages = [
            [["SKU", "Description", "Qty"], ["A-1", "  ", "1"]],
            [["SKU", "Description", "Qty"], ["", "wrapped text", ""]],
        ]
        result = seam.stitch_pages(
            pages, key_columns=[0], quantity_column=2
        )
        assert len(result.rows) == 1
        assert result.rows[0].cells == ["A-1", "wrapped text", "1"]
        assert result.rows[0].sources == [(0, 1), (1, 1)]
        assert result.issues == []

    def test_invalid_arguments(self):
        with pytest.raises(ValueError):
            seam.stitch_pages([])
        with pytest.raises(ValueError):
            seam.stitch_pages([[["a"]]], key_columns=[5])
        with pytest.raises(ValueError):
            seam.stitch_pages([[["a"]]], quantity_column=5)
        with pytest.raises(ValueError):
            seam.stitch_pages([[["a"]]], lookback=0)
        with pytest.raises(ValueError):
            seam.stitch_pages([[["a"]]], header=-1)
        with pytest.raises(ValueError):
            seam.stitch_pages([[["a"]]], header="nope")


if __name__ == "__main__":
    unittest.main()
