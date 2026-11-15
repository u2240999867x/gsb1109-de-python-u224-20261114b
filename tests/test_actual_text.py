#!/usr/bin/env python3

import os
import unittest

import pdfplumber
from pdfplumber.structure import PDFStructTree

HERE = os.path.abspath(os.path.dirname(__file__))

TREE = [
    {
        "type": "Document",
        "children": [
            {"type": "H1", "page_number": 1, "mcids": [0]},
            {
                "type": "TOCI",
                "actual_text": "Chapter 1: page 5",
                "page_number": 1,
                "children": [
                    {"type": "Span", "page_number": 1, "mcids": [1]},
                    {"type": "Span", "page_number": 1, "mcids": [2]},
                ],
            },
            {
                "type": "TOCI",
                "actual_text": "Chapter 2: page 10",
                "page_number": 1,
                "children": [
                    {"type": "Span", "page_number": 1, "mcids": [3]},
                    {"type": "Span", "page_number": 2, "mcids": [0]},
                ],
            },
            # An empty ActualText is kept distinct from no ActualText
            {"type": "Span", "actual_text": "", "page_number": 2, "mcids": [1]},
            {"type": "P", "page_number": 2, "mcids": [2]},
        ],
    }
]

TREE1 = [
    {
        "type": "Document",
        "children": [
            {"type": "H1", "mcids": [0]},
            {
                "type": "TOCI",
                "actual_text": "Chapter 1: page 5",
                "children": [
                    {"type": "Span", "mcids": [1]},
                    {"type": "Span", "mcids": [2]},
                ],
            },
            {
                "type": "TOCI",
                "actual_text": "Chapter 2: page 10",
                "children": [
                    {"type": "Span", "mcids": [3]},
                ],
            },
        ],
    }
]

TREE2 = [
    {
        "type": "Document",
        "children": [
            {
                "type": "TOCI",
                "actual_text": "Chapter 2: page 10",
                "children": [
                    {"type": "Span", "mcids": [0]},
                ],
            },
            {"type": "Span", "actual_text": "", "mcids": [1]},
            {"type": "P", "mcids": [2]},
        ],
    }
]


class TestStructureActualText(unittest.TestCase):
    """Test ActualText declared on structure elements, using a synthetic
    accessible table-of-contents PDF."""

    @classmethod
    def setup_class(cls):
        cls.path = os.path.join(HERE, "pdfs/actual_text.pdf")

    def test_structure_tree(self):
        with pdfplumber.open(self.path) as pdf:
            assert pdf.structure_tree == TREE
            assert pdf.pages[0].structure_tree == TREE1
            assert pdf.pages[1].structure_tree == TREE2

    def test_page_text_is_unchanged(self):
        # The original page text is still available for display
        with pdfplumber.open(self.path) as pdf:
            assert pdf.pages[0].extract_text() == (
                "Contents\n"
                "Chapter 1 ..........\n"
                ".......... 5\n"
                "Chapter 2 .........."
            )
            assert pdf.pages[1].extract_text() == "...... 10\n**********\nThe End\n~~~"

    def test_logical_text_document(self):
        # Each ActualText is counted once at its owning node, even when
        # the node's content spans lines (Chapter 1) or pages (Chapter 2)
        with pdfplumber.open(self.path) as pdf:
            stree = PDFStructTree(pdf)
            assert stree.logical_text() == (
                "Contents\nChapter 1: page 5\nChapter 2: page 10\nThe End"
            )

    def test_logical_text_pages(self):
        with pdfplumber.open(self.path) as pdf:
            stree1 = PDFStructTree(pdf, pdf.pages[0])
            assert stree1.logical_text() == (
                "Contents\nChapter 1: page 5\nChapter 2: page 10"
            )
            stree2 = PDFStructTree(pdf, pdf.pages[1])
            assert stree2.logical_text() == "Chapter 2: page 10\nThe End"

    def test_review_replacement_term(self):
        with pdfplumber.open(self.path) as pdf:
            stree = PDFStructTree(pdf)
            review = stree.review_term("Chapter 1: page 5")
            assert review["term"] == "Chapter 1: page 5"
            assert review["found"]
            assert review["logical_text"] == stree.logical_text()
            assert len(review["matches"]) == 1
            match = review["matches"][0]
            assert match["element"].type == "TOCI"
            assert match["actual_text"] == "Chapter 1: page 5"
            # The term replaces all glyphs drawn for the entry, on both
            # of the lines it spans
            assert "".join(c["text"] for c in match["chars"]) == (
                "Chapter 1 " + "." * 20 + " 5"
            )
            assert set(c["page_number"] for c in match["chars"]) == {1}

    def test_review_cross_page_term(self):
        with pdfplumber.open(self.path) as pdf:
            stree = PDFStructTree(pdf)
            review = stree.review_term("Chapter 2: page 10")
            assert len(review["matches"]) == 1
            match = review["matches"][0]
            # The replaced glyphs are located on both pages
            assert "".join(c["text"] for c in match["chars"]) == (
                "Chapter 2 " + "." * 16 + " 10"
            )
            assert set(c["page_number"] for c in match["chars"]) == {1, 2}

    def test_review_direct_term(self):
        # A term that was not replaced matches the drawn glyphs directly
        with pdfplumber.open(self.path) as pdf:
            stree = PDFStructTree(pdf)
            review = stree.review_term("The End")
            assert review["found"]
            assert len(review["matches"]) == 1
            match = review["matches"][0]
            assert match["actual_text"] is None
            assert "".join(c["text"] for c in match["chars"]) == "The End"

    def test_review_term_not_found(self):
        with pdfplumber.open(self.path) as pdf:
            stree = PDFStructTree(pdf)
            for term in ["Chapter 3", "..........", ""]:
                review = stree.review_term(term)
                assert not review["found"]
                assert review["matches"] == []

    def test_review_decorative_chars(self):
        # Decorative characters do not enter the logical field: the
        # "**********" covered by an empty ActualText, and the untagged
        # "~~~"
        with pdfplumber.open(self.path) as pdf:
            stree = PDFStructTree(pdf)
            review = stree.review_term("Chapter 1: page 5")
            decorative = review["decorative_chars"]
            assert "".join(c["text"] for c in decorative) == "**********~~~"
            logical = review["logical_text"]
            for c in decorative:
                assert c["text"] not in logical


class TestInlineActualText(unittest.TestCase):
    """Test ActualText declared by marked-content sequences."""

    @classmethod
    def setup_class(cls):
        cls.path = os.path.join(HERE, "pdfs/actual_text_inline.pdf")

    def test_actual_text_spans(self):
        with pdfplumber.open(self.path) as pdf:
            page = pdf.pages[0]
            # The replacement text is stored once per span, in order of
            # appearance; an empty ActualText is kept distinct from none
            assert page.actual_text_spans == [
                {"tag": "Span", "actual_text": "first"},
                {"tag": "Span", "actual_text": ""},
                {"tag": "Span", "actual_text": "outer"},
                {"tag": "Span", "actual_text": "inner"},
            ]

    def test_span_scope(self):
        # Objects reference their span by index; nested spans override
        # the outer one for their own scope only
        with pdfplumber.open(self.path) as pdf:
            page = pdf.pages[0]
            by_span = {}
            for c in page.chars:
                by_span.setdefault(c["actual_text_span"], []).append(c["text"])
            assert by_span == {
                0: ["1", "s", "t"],
                None: ["a", "n", "d"],
                1: ["~", "~", "~"],
                2: ["x", "z"],
                3: ["y"],
            }

    def test_page_text_is_unchanged(self):
        with pdfplumber.open(self.path) as pdf:
            page = pdf.pages[0]
            assert page.extract_text() == "1st\nand\n~~~\nx\ny\nz"

    def test_derived_pages(self):
        with pdfplumber.open(self.path) as pdf:
            page = pdf.pages[0]
            cropped = page.crop((0, 0, 300, 792))
            assert cropped.actual_text_spans == page.actual_text_spans
            filtered = page.filter(lambda o: o.get("text") != "x")
            assert filtered.actual_text_spans == page.actual_text_spans

    def test_no_actual_text(self):
        path = os.path.join(HERE, "pdfs/hello_structure.pdf")
        with pdfplumber.open(path) as pdf:
            page = pdf.pages[0]
            assert page.actual_text_spans == []
            assert set(c["actual_text_span"] for c in page.chars) == {None}
