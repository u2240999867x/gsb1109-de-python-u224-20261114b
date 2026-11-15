#!/usr/bin/env python3
"""Generate the synthetic fixture PDFs used by tests/test_actual_text.py."""

import os

HERE = os.path.dirname(os.path.abspath(__file__))


def build_pdf(objects):
    """objects: list of (obj_number, body_bytes). Returns PDF bytes."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num, body in objects:
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num
        out += body
        if not body.endswith(b"\n"):
            out += b"\n"
        out += b"endobj\n"
    xref_at = len(out)
    count = max(offsets) + 1
    out += b"xref\n0 %d\n" % count
    out += b"0000000000 65535 f \n"
    for i in range(1, count):
        if i in offsets:
            out += b"%010d 00000 n \n" % offsets[i]
        else:
            out += b"0000000000 65535 f \n"
    out += b"trailer  << /Size %d /Root 1 0 R >>\n" % count
    out += b"startxref\n%d\n%%%%EOF\n" % xref_at
    return bytes(out)


def stream(data):
    return b"<< /Length %d >>\nstream\n%sendstream" % (len(data), data)


FONT = b"<< /Type /Font /Subtype /Type1 /Name /F1 /BaseFont /Helvetica >>"
RESOURCES = b"/Resources << /ProcSet [/PDF /Text] /Font << /F1 3 0 R >> >>"


def tagged():
    content1 = (
        b"/H1 <</MCID 0>> BDC\n"
        b"BT /F1 18 Tf 72 720 Td (Contents) Tj ET\n"
        b"EMC\n"
        b"/P <</MCID 1>> BDC\n"
        b"BT /F1 12 Tf 72 690 Td (Chapter 1 ..........) Tj ET\n"
        b"EMC\n"
        b"/P <</MCID 2>> BDC\n"
        b"BT /F1 12 Tf 72 676 Td (.......... 5) Tj ET\n"
        b"EMC\n"
        b"/P <</MCID 3>> BDC\n"
        b"BT /F1 12 Tf 72 650 Td (Chapter 2 ..........) Tj ET\n"
        b"EMC\n"
    )
    content2 = (
        b"/P <</MCID 0>> BDC\n"
        b"BT /F1 12 Tf 72 720 Td (...... 10) Tj ET\n"
        b"EMC\n"
        b"/P <</MCID 1>> BDC\n"
        b"BT /F1 12 Tf 72 690 Td (**********) Tj ET\n"
        b"EMC\n"
        b"/P <</MCID 2>> BDC\n"
        b"BT /F1 12 Tf 72 660 Td (The End) Tj ET\n"
        b"EMC\n"
        b"BT /F1 12 Tf 72 600 Td (~~~) Tj ET\n"
    )
    objects = [
        (1, b"<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 4 0 R >>"),
        (
            2,
            b"<< /Type /Pages /MediaBox [0 0 612 792] "
            b"/Kids [10 0 R 20 0 R] /Count 2 >>",
        ),
        (3, FONT),
        (4, b"<< /Type /StructTreeRoot /ParentTree 5 0 R /K [30 0 R] >>"),
        (
            5,
            b"<< /Nums [0 [31 0 R 32 0 R 33 0 R 34 0 R] "
            b"1 [35 0 R 36 0 R 37 0 R]] >>",
        ),
        (
            10,
            b"<< /Type /Page /Parent 2 0 R /Contents 11 0 R "
            b"/StructParents 0 " + RESOURCES + b" >>",
        ),
        (11, stream(content1)),
        (
            20,
            b"<< /Type /Page /Parent 2 0 R /Contents 21 0 R "
            b"/StructParents 1 " + RESOURCES + b" >>",
        ),
        (21, stream(content2)),
        (
            30,
            b"<< /Type /StructElem /S /Document /P 4 0 R "
            b"/K [31 0 R 40 0 R 41 0 R 36 0 R 37 0 R] >>",
        ),
        (31, b"<< /Type /StructElem /S /H1 /P 30 0 R /Pg 10 0 R /K [0] >>"),
        (32, b"<< /Type /StructElem /S /Span /P 40 0 R /Pg 10 0 R /K [1] >>"),
        (33, b"<< /Type /StructElem /S /Span /P 40 0 R /Pg 10 0 R /K [2] >>"),
        (34, b"<< /Type /StructElem /S /Span /P 41 0 R /Pg 10 0 R /K [3] >>"),
        (35, b"<< /Type /StructElem /S /Span /P 41 0 R /Pg 20 0 R /K [0] >>"),
        (
            36,
            b"<< /Type /StructElem /S /Span /P 30 0 R /Pg 20 0 R "
            b"/ActualText () /K [1] >>",
        ),
        (37, b"<< /Type /StructElem /S /P /P 30 0 R /Pg 20 0 R /K [2] >>"),
        (
            40,
            b"<< /Type /StructElem /S /TOCI /P 30 0 R /Pg 10 0 R "
            b"/ActualText (Chapter 1: page 5) /K [32 0 R 33 0 R] >>",
        ),
        (
            41,
            b"<< /Type /StructElem /S /TOCI /P 30 0 R /Pg 10 0 R "
            b"/ActualText (Chapter 2: page 10) /K [34 0 R 35 0 R] >>",
        ),
    ]
    return build_pdf(objects)


def inline():
    content = (
        b"/Span <</ActualText (first)>> BDC\n"
        b"BT /F1 12 Tf 72 720 Td (1st) Tj ET\n"
        b"EMC\n"
        b"BT /F1 12 Tf 72 700 Td (and) Tj ET\n"
        b"/Span <</ActualText ()>> BDC\n"
        b"BT /F1 12 Tf 72 680 Td (~~~) Tj ET\n"
        b"EMC\n"
        b"/Span <</ActualText (outer)>> BDC\n"
        b"BT /F1 12 Tf 72 660 Td (x) Tj ET\n"
        b"/Span <</ActualText (inner)>> BDC\n"
        b"BT /F1 12 Tf 72 650 Td (y) Tj ET\n"
        b"EMC\n"
        b"BT /F1 12 Tf 72 640 Td (z) Tj ET\n"
        b"EMC\n"
    )
    objects = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (
            2,
            b"<< /Type /Pages /MediaBox [0 0 612 792] /Kids [4 0 R] /Count 1 >>",
        ),
        (3, FONT),
        (
            4,
            b"<< /Type /Page /Parent 2 0 R /Contents 5 0 R " + RESOURCES + b" >>",
        ),
        (5, stream(content)),
    ]
    return build_pdf(objects)


if __name__ == "__main__":
    with open(os.path.join(HERE, "actual_text.pdf"), "wb") as f:
        f.write(tagged())
    with open(os.path.join(HERE, "actual_text_inline.pdf"), "wb") as f:
        f.write(inline())
    print("done")
