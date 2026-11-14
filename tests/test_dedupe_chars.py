#!/usr/bin/env python
import logging
import os
import unittest

import pdfplumber
from pdfplumber import utils

logging.disable(logging.ERROR)

HERE = os.path.abspath(os.path.dirname(__file__))
INVOICE_PATH = os.path.join(HERE, "pdfs/dedupe-invoice-glyphs.pdf")


class Test(unittest.TestCase):
    @classmethod
    def setup_class(self):
        path = os.path.join(HERE, "pdfs/issue-71-duplicate-chars.pdf")
        self.pdf = pdfplumber.open(path)

    @classmethod
    def teardown_class(self):
        self.pdf.close()

    def test_extract_table(self):
        page = self.pdf.pages[0]
        table_without_drop_duplicates = page.extract_table()
        table_with_drop_duplicates = page.dedupe_chars().extract_table()
        last_line_without_drop = table_without_drop_duplicates[1][1].split("\n")[-1]
        last_line_with_drop = table_with_drop_duplicates[1][1].split("\n")[-1]

        assert (
            last_line_without_drop
            == "微微软软 培培训训课课程程：： 名名模模意意义义一一些些有有意意义义一一些些"
        )
        assert last_line_with_drop == "微软 培训课程： 名模意义一些有意义一些"

    def test_extract_words(self):
        page = self.pdf.pages[0]
        x0 = 440.143
        x1_without_drop = 534.992
        x1_with_drop = 534.719
        top_windows = 791.849
        top_linux = 794.357
        bottom = 802.961
        last_words_without_drop = page.extract_words()[-1]
        last_words_with_drop = page.dedupe_chars().extract_words()[-1]

        assert round(last_words_without_drop["x0"], 3) == x0
        assert round(last_words_without_drop["x1"], 3) == x1_without_drop
        assert round(last_words_without_drop["top"], 3) in (top_windows, top_linux)
        assert round(last_words_without_drop["bottom"], 3) == bottom
        assert last_words_without_drop["upright"] == 1
        assert (
            last_words_without_drop["text"]
            == "名名模模意意义义一一些些有有意意义义一一些些"
        )

        assert round(last_words_with_drop["x0"], 3) == x0
        assert round(last_words_with_drop["x1"], 3) == x1_with_drop
        assert round(last_words_with_drop["top"], 3) in (top_windows, top_linux)
        assert round(last_words_with_drop["bottom"], 3) == bottom
        assert last_words_with_drop["upright"] == 1
        assert last_words_with_drop["text"] == "名模意义一些有意义一些"

    def test_extract_text(self):
        page = self.pdf.pages[0]
        last_line_without_drop = page.extract_text().split("\n")[-1]
        last_line_with_drop = page.dedupe_chars().extract_text().split("\n")[-1]

        assert (
            last_line_without_drop
            == "微微软软 培培训训课课程程：： 名名模模意意义义一一些些有有意意义义一一些些"
        )
        assert last_line_with_drop == "微软 培训课程： 名模意义一些有意义一些"

    def test_extract_text2(self):
        path = os.path.join(HERE, "pdfs/issue-71-duplicate-chars-2.pdf")
        pdf = pdfplumber.open(path)
        page = pdf.pages[0]

        assert (
            page.dedupe_chars().extract_text(y_tolerance=6).splitlines()[4]
            == "UE 8. Circulation - Métabolismes"
        )

    def test_extra_attrs(self):
        path = os.path.join(HERE, "pdfs/issue-1114-dedupe-chars.pdf")
        pdf = pdfplumber.open(path)
        page = pdf.pages[0]

        def dup_chars(s: str) -> str:
            return "".join((char if char == " " else char + char) for char in s)

        ground_truth = (
            ("Simple", False, False),
            ("Duplicated", True, True),
            ("Font", "fontname", True),
            ("Size", "size", True),
            ("Italic", "fontname", True),
            ("Weight", "fontname", True),
            ("Horizontal shift", False, "HHoorrizizoonntatal ls shhifitft"),
            ("Vertical shift", False, True),
        )
        gt = []
        for text, should_dedup, dup_text in ground_truth:
            if isinstance(dup_text, bool):
                if dup_text:
                    dup_text = dup_chars(text)
                else:
                    dup_text = text
            gt.append((text, should_dedup, dup_text))

        keys_list = ["no_dedupe", (), ("size",), ("fontname",), ("size", "fontname")]
        for keys in keys_list:
            if keys != "no_dedupe":
                filtered_page = page.dedupe_chars(tolerance=2, extra_attrs=keys)
            else:
                filtered_page = page
            for i, line in enumerate(
                filtered_page.extract_text(y_tolerance=5).splitlines()
            ):
                text, should_dedup, dup_text = gt[i]
                if keys == "no_dedupe":
                    should_dedup = False
                if isinstance(should_dedup, str):
                    if should_dedup in keys:
                        fail_msg = (
                            f"{should_dedup} is not required to match "
                            "so it should be duplicated"
                        )
                        assert line == dup_text, fail_msg
                    else:
                        fail_msg = (
                            "Should not be duplicated "
                            f"when requiring matching {should_dedup}"
                        )
                        assert line == text, fail_msg
                elif should_dedup:
                    assert line == text
                else:
                    assert line == dup_text


class TestInvoiceOverprint(unittest.TestCase):
    """Regression cases for fill/outline overprints on invoice-like digits.

    The fixture paints, in order:

    - ``8`` at x=72.00 (fill red, stroke black)
    - ``8`` at x=72.25 (fill red, stroke dark red) -- same fill, new outline
    - ``8`` at x=72.30 (fill black, stroke dark red) -- neither channel matches
    - ``6`` at x=72.00 with MCID 0, then the same ``6`` at x=72.20 with no tag
    - ``11`` where the second ``1`` is a real glyph one advance away
    - ``333`` drawn at x=72.0, 72.9, 73.8 -- a chain, but 0 and 1.8 > tolerance
    - ``7`` drawn twice at the same spot, first red then blue
    - ``9`` drawn twice at x=72.0 and 72.2 under two different MCIDs
    - ``5`` drawn at x=72.00, 72.05, 72.10 -- three genuinely stacked draws
    """

    @classmethod
    def setup_class(self):
        self.pdf = pdfplumber.open(INVOICE_PATH)
        self.page = self.pdf.pages[0]
        self.deduped = self.page.dedupe_chars()

    @classmethod
    def teardown_class(self):
        self.pdf.close()

    def groups(self):
        return {
            group["record"]["glyph"]["text"]: group["record"]
            for group in utils.dedupe_char_groups(self.page.chars)
        }

    def test_outline_duplicate_keeps_anchor_and_both_sources(self):
        groups = self.groups()
        eight = groups["8"]

        assert eight["glyph"] == {
            "upright": True,
            "text": "8",
            "fontname": "Helvetica",
            "size": 12.0,
        }
        assert eight["kept_index"] == 0
        assert [draw["index"] for draw in eight["draws"]] == [0, 1]
        assert [draw["x0"] for draw in eight["draws"]] == [72.0, 72.25]

        removed = eight["removed"]
        assert [entry["index"] for entry in removed] == [1]
        evidence = removed[0]["checks"]
        assert evidence["offset"] == {
            "delta": (0.25, 0.0),
            "tolerance": 1,
            "within": True,
        }
        assert evidence["color"]["stroking_match"] is False
        assert evidence["color"]["non_stroking_match"] is True
        assert evidence["color"]["matching_channels"] == ("non_stroking_color",)
        assert evidence["layer"]["compatible"] is True

        # The third 8 has a different fill and is reported as a candidate.
        assert [(c["index"], c["reason"]) for c in eight["candidates"]] == [
            (2, "color_mismatch")
        ]

    def test_outline_record_is_attached_to_kept_char(self):
        eight = next(c for c in self.deduped.chars if c["text"] == "8")
        record = eight["dedupe"]
        assert record["kept_index"] == 0
        assert [(draw["index"], draw["x0"]) for draw in record["draws"]] == [
            (0, 72.0),
            (1, 72.25),
        ]
        removed = record["removed"]
        assert removed[0]["draw"]["index"] == 1
        assert removed[0]["draw"]["matrix"][4] == 72.25
        assert removed[0]["draw"]["stroking_color"] == (0.5, 0.0, 0.0)
        assert removed[0]["draw"]["non_stroking_color"] == (1.0, 0.0, 0.0)

    def test_real_adjacent_ones_survive(self):
        groups = self.groups()
        ones = groups["1"]
        assert ones["draws"][0]["x0"] == 72.0
        assert ones["candidates"][0]["index"] == 6
        assert ones["candidates"][0]["reason"] == "advance_guard"
        loose = self.page.dedupe_chars(tolerance=3)
        assert loose.extract_text().splitlines()[2] == "11"

    def test_micro_offset_chain_does_not_swallow_far_end(self):
        groups = self.groups()
        threes = groups["3"]
        assert threes["draws"][0]["x0"] == 72.0
        assert [entry["index"] for entry in threes["removed"]] == [8]
        assert [entry["reason"] for entry in threes["candidates"]] == [
            "offset_exceeds_tolerance"
        ]
        assert round(threes["candidates"][0]["checks"]["offset"]["delta"][0], 3) == 1.8
        assert threes["candidates"][0]["checks"]["offset"]["within"] is False
        assert self.deduped.extract_text().splitlines()[3] == "33"

    def test_three_stacked_draws_collapse_to_one(self):
        groups = self.groups()
        fives = groups["5"]
        assert [draw["x0"] for draw in fives["draws"]] == [72.0, 72.05, 72.1]
        assert [entry["index"] for entry in fives["removed"]] == [15, 16]
        assert self.deduped.extract_text().splitlines()[-1] == "5"

    def test_layer_and_color_conflicts_stay(self):
        groups = self.groups()
        assert groups["7"]["candidates"][0]["reason"] == "color_mismatch"
        assert groups["9"]["candidates"][0]["reason"] == "layer_mismatch"
        assert self.deduped.extract_text().splitlines()[4:] == ["77", "99", "5"]

    def test_rejected_candidate_carries_full_checks(self):
        groups = self.groups()
        candidate = groups["9"]["candidates"][0]
        checks = candidate["checks"]
        assert checks["offset"]["within"] is True
        assert checks["color"]["matching_channels"] == (
            "stroking_color",
            "non_stroking_color",
        )
        assert checks["layer"] == {
            "anchor": {"page_number": 1, "tag": "Span", "mcid": 1},
            "candidate": {"page_number": 1, "tag": "Span", "mcid": 2},
            "compatible": False,
        }

    def test_provenance_can_be_disabled(self):
        plain = self.page.dedupe_chars(include_provenance=False)
        assert all("dedupe" not in char for char in plain.chars)
        assert len(plain.chars) == len(self.deduped.chars)

    def test_existing_fixture_numbers_survive(self):
        path = os.path.join(HERE, "pdfs/issue-71-duplicate-chars.pdf")
        with pdfplumber.open(path) as pdf:
            page = pdf.pages[0]
            deduped = page.dedupe_chars()
            text = deduped.extract_text()
            assert "微软 培训课程： 名模意义一些有意义一些" in text
            assert all(
                "dedupe" in char
                for char in deduped.chars
                if char["text"] in ("本", "科", "微", "软")
            )
