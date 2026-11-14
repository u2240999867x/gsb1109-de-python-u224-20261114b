#!/usr/bin/env python

"""Build the character-deduplication regression PDF.

The page deliberately mixes real repeated glyphs with overprint/outline
duplicates that differ only in paint, layer, or sub-point positioning. Run
this script from the repository root after editing the page content.
"""

from pathlib import Path

CONTENT = b"""q
1 0 0 rg 0 0 0 RG
BT /F1 12 Tf 72 760 Td (8) Tj ET
1 0 0 rg 0.5 0 0 RG
BT /F1 12 Tf 72.25 760 Td (8) Tj ET
0 0 0 rg 0.6 0 0 RG
BT /F1 12 Tf 72.300 760 Td (8) Tj ET
0 0 0 rg 0 0 0 RG
/Span <</MCID 0>> BDC
BT /F1 12 Tf 72 720 Td (6) Tj ET
EMC
BT /F1 12 Tf 72.2 720 Td (6) Tj ET
BT /F1 12 Tf 72 680 Td (11) Tj ET
BT /F1 12 Tf 72 640 Td (3) Tj ET
BT /F1 12 Tf 72.9 640 Td (3) Tj ET
BT /F1 12 Tf 73.8 640 Td (3) Tj ET
1 0 0 rg 1 0 0 RG
BT /F1 12 Tf 72 600 Td (7) Tj ET
0 0 1 rg 0 0 1 RG
BT /F1 12 Tf 72 600 Td (7) Tj ET
0 0 0 rg 0 0 0 RG
/Span <</MCID 1>> BDC
BT /F1 12 Tf 72 560 Td (9) Tj ET
EMC
/Span <</MCID 2>> BDC
BT /F1 12 Tf 72.2 560 Td (9) Tj ET
EMC
BT /F1 12 Tf 72 520 Td (5) Tj ET
0 0 0 rg 0 0 0 RG
BT /F1 12 Tf 72.05 520 Td (5) Tj ET
0 0 0 rg 0 0 0 RG
BT /F1 12 Tf 72.1 520 Td (5) Tj ET
Q
"""

OBJECTS = [
    b"<</Type/Catalog/Pages 2 0 R>>",
    b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
    (
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>"
    ),
    b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    b"<</Length %d>>stream\n" % len(CONTENT) + CONTENT + b"endstream",
]


def build() -> bytes:
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(OBJECTS, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(OBJECTS) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (
        len(OBJECTS) + 1,
        xref,
    )
    return bytes(out)


if __name__ == "__main__":
    target = Path("tests/pdfs/dedupe-invoice-glyphs.pdf")
    target.write_bytes(build())
    print(f"wrote {target}")
