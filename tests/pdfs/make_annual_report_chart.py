#!/usr/bin/env python
"""Build a deterministic vector-PDF fixture of a printed annual-report chart.

The chart mimics a two-indicator line chart where both indicators are printed
in the same blue and can only be told apart by their dash styles:

* ``营业收入增长率（%）`` -- dotted line ``[1 3]``
* ``净利润增长率（%）`` -- long-dash line ``[8 4]``

Deliberate quirks exercised by the extraction tests:

* Both series share one stroking color; style (not color) carries identity.
* Each legend sample and each subpath that resumes after a gap uses a
  different dash phase, which must not prevent them from joining one series.
* Both series have a genuine internal measurement gap (dotted in 2017,
  long dash in 2020); measurement resumes as an independent stroked
  subpath and the hole must never be connected or interpolated.
* After the two series cross, their 2023-2024 tails are emitted with a shared
  style that matches neither legend sample, so those segments cannot be named
  and must be flagged as ambiguous instead of guessed.
* Light-gray dotted gridlines reuse the dotted dash array but a different
  color, so color filtering is required to avoid mistaking them for data.

The script uses the standard library only and writes its output next to
itself as ``annual-report-chart.pdf``.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Fixture geometry and ground-truth data (imported by the tests)
# ---------------------------------------------------------------------------

PAGE_WIDTH = 612
PAGE_HEIGHT = 792

PLOT_X0 = 90.0
PLOT_X1 = 522.0
PLOT_Y0 = 120.0  # PDF bottom-up coordinate of the lower frame edge
PLOT_Y1 = 520.0  # PDF bottom-up coordinate of the upper frame edge

YEARS = list(range(2014, 2025))

Y_TICK_VALUES = [0, 20, 40, 60, 80, 100]

BLUE = (0.122, 0.467, 0.705)
DOTTED = (1.0, 3.0)
LONG_DASH = (8.0, 4.0)
SHARED_TAIL = (4.0, 3.0)

SERIES_REVENUE = "营业收入增长率（%）"
SERIES_PROFIT = "净利润增长率（%）"

# Ground-truth samples. ``None`` marks a real measurement gap.
REVENUE_SAMPLES = {
    2014: 8.0,
    2015: 10.0,
    2016: 18.0,
    2017: None,
    2018: 30.0,
    2019: 35.0,
    2020: 42.0,
    2021: 48.0,
    2022: 55.0,
    2023: 62.0,
    2024: 70.0,
}
PROFIT_SAMPLES = {
    2014: 65.0,
    2015: 60.0,
    2016: 55.0,
    2017: 50.0,
    2018: 44.0,
    2019: 38.0,
    2020: None,
    2021: 28.0,
    2022: 22.0,
    2023: 16.0,
    2024: 10.0,
}

# Named (style-identifiable) samples stop before the shared-style tail.
AMBIGUOUS_YEARS = [2023, 2024]

# Each series resumes after its gap in an independent stroked object that
# restarts the dash pattern at another phase; identity must ignore phase.
LEGEND_LONG_DASH_PHASE = 7.0
PLOT_LONG_DASH_PHASE = 2.0
PLOT_LONG_DASH_RESUME_PHASE = 5.0
PLOT_DOTTED_PHASE = 0.0
PLOT_DOTTED_RESUME_PHASE = 4.0

TITLE = "2014-2024年度主要指标走势图"


def x_for_year(year: int) -> float:
    return PLOT_X0 + (year - YEARS[0]) * (PLOT_X1 - PLOT_X0) / (len(YEARS) - 1)


def y_for_value(value: float) -> float:
    return PLOT_Y0 + value * (PLOT_Y1 - PLOT_Y0) / max(Y_TICK_VALUES)


# ---------------------------------------------------------------------------
# Minimal single-page PDF writer (standard library only)
# ---------------------------------------------------------------------------


class PdfBuilder:
    def __init__(self) -> None:
        self._objects: list[bytes] = []

    def add(self, body: str | bytes) -> int:
        if isinstance(body, str):
            body = body.encode("latin-1")
        self._objects.append(body)
        return len(self._objects)

    def render(self) -> bytes:
        out = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
        offsets = [0]
        for index, body in enumerate(self._objects, start=1):
            offsets.append(len(out))
            out += f"{index} 0 obj\n".encode("latin-1")
            out += body
            out += b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(self._objects) + 1}\n".encode("latin-1")
        out += b"0000000000 65535 f \n"
        for offset in offsets[1:]:
            out += f"{offset:010d} 00000 n \n".encode("latin-1")
        out += (
            f"trailer\n<< /Size {len(self._objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("latin-1")
        return out


def _escape_latin(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _utf16_hex(text: str) -> str:
    return text.encode("utf-16-be").hex().upper()


def _latin_text(font: str, size: float, x: float, y: float, text: str) -> str:
    return (
        f"BT /{font} {size:g} Tf {x:g} {y:g} Td ({_escape_latin(text)}) Tj ET\n"
    )


def _cid_text(font: str, size: float, x: float, y: float, text: str) -> str:
    return f"BT /{font} {size:g} Tf {x:g} {y:g} Td <{_utf16_hex(text)}> Tj ET\n"


def _stroke(
    rgb: tuple[float, float, float],
    width: float,
    dash: tuple[float, ...] | None,
    phase: float,
    path: str,
) -> str:
    commands = [
        f"{rgb[0]:g} {rgb[1]:g} {rgb[2]:g} RG",
        f"{width:g} w",
    ]
    if dash is None:
        commands.append("[] 0 d")
    else:
        array = " ".join(f"{v:g}" for v in dash)
        commands.append(f"[{array}] {phase:g} d")
    commands.append(path)
    commands.append("S")
    return " ".join(commands) + "\n"


def _polyline(points: list[tuple[float, float]]) -> str:
    head = f"{points[0][0]:g} {points[0][1]:g} m"
    rest = " ".join(f"{x:g} {y:g} l" for x, y in points[1:])
    return f"{head} {rest}"


def build_content_stream() -> bytes:
    stream = ""

    # Title and legend labels (Chinese rendered through the CID font).
    stream += _cid_text("F2", 16, PLOT_X0, 600, TITLE)
    legend_y = 560.0
    stream += _cid_text("F2", 11, 168, legend_y - 3, SERIES_REVENUE)
    stream += _cid_text("F2", 11, 378, legend_y - 3, SERIES_PROFIT)

    # Frame and tick marks, solid black.
    frame = (
        f"{PLOT_X0:g} {PLOT_Y0:g} m {PLOT_X1:g} {PLOT_Y0:g} l "
        f"{PLOT_X1:g} {PLOT_Y1:g} l {PLOT_X0:g} {PLOT_Y1:g} l h"
    )
    stream += _stroke((0.0, 0.0, 0.0), 0.8, None, 0.0, frame)
    for year in YEARS:
        x = x_for_year(year)
        stream += _stroke(
            (0.0, 0.0, 0.0),
            0.8,
            None,
            0.0,
            f"{x:g} {PLOT_Y0:g} m {x:g} {PLOT_Y0 - 5:g} l",
        )
        stream += _latin_text("F1", 9, x - 14, PLOT_Y0 - 20, str(year))
    for value in Y_TICK_VALUES:
        y = y_for_value(value)
        stream += _stroke(
            (0.0, 0.0, 0.0),
            0.8,
            None,
            0.0,
            f"{PLOT_X0:g} {y:g} m {PLOT_X0 - 5:g} {y:g} l",
        )
        stream += _latin_text("F1", 9, PLOT_X0 - 22, y - 3, str(value))

    # Gray dotted gridlines: same dash array as the dotted series but gray.
    for value in Y_TICK_VALUES[1:-1]:
        y = y_for_value(value)
        stream += _stroke(
            (0.75, 0.75, 0.75),
            0.5,
            DOTTED,
            0.0,
            f"{PLOT_X0:g} {y:g} m {PLOT_X1:g} {y:g} l",
        )

    # Legend sample lines: dotted vs long dash, same blue, differing phases.
    stream += _stroke(
        BLUE,
        1.2,
        DOTTED,
        0.0,
        "120 560 m 160 560 l",
    )
    stream += _stroke(
        BLUE,
        1.2,
        LONG_DASH,
        LEGEND_LONG_DASH_PHASE,
        "330 560 m 370 560 l",
    )

    # Dotted series: two independent subpaths around a real gap at 2017,
    # each with its own dash phase but the same dash array.
    revenue_named = {
        year: value
        for year, value in REVENUE_SAMPLES.items()
        if value is not None and year not in AMBIGUOUS_YEARS
    }
    revenue_gap_year = next(
        year
        for year, value in REVENUE_SAMPLES.items()
        if value is None and year not in AMBIGUOUS_YEARS
    )
    revenue_before = [
        (x_for_year(year), y_for_value(value))
        for year, value in revenue_named.items()
        if year < revenue_gap_year
    ]
    revenue_after = [
        (x_for_year(year), y_for_value(value))
        for year, value in revenue_named.items()
        if year > revenue_gap_year
    ]
    stream += _stroke(
        BLUE, 1.2, DOTTED, PLOT_DOTTED_PHASE, _polyline(revenue_before)
    )
    stream += _stroke(
        BLUE,
        1.2,
        DOTTED,
        PLOT_DOTTED_RESUME_PHASE,
        _polyline(revenue_after),
    )

    # Long-dash series: two independent subpaths around a real gap at 2021,
    # each with its own dash phase but the same dash array.
    profit_named = {
        year: value
        for year, value in PROFIT_SAMPLES.items()
        if value is not None and year not in AMBIGUOUS_YEARS
    }
    gap_year = next(
        year
        for year, value in PROFIT_SAMPLES.items()
        if value is None and year not in AMBIGUOUS_YEARS
    )
    profit_before = [
        (x_for_year(year), y_for_value(value))
        for year, value in profit_named.items()
        if year < gap_year
    ]
    profit_after = [
        (x_for_year(year), y_for_value(value))
        for year, value in profit_named.items()
        if year > gap_year
    ]
    stream += _stroke(
        BLUE, 1.2, LONG_DASH, PLOT_LONG_DASH_PHASE, _polyline(profit_before)
    )
    stream += _stroke(
        BLUE,
        1.2,
        LONG_DASH,
        PLOT_LONG_DASH_RESUME_PHASE,
        _polyline(profit_after),
    )

    # Post-crossing tails: shared style matching neither legend sample.
    revenue_tail = [
        (x_for_year(year), y_for_value(REVENUE_SAMPLES[year]))
        for year in AMBIGUOUS_YEARS
    ]
    profit_tail = [
        (x_for_year(year), y_for_value(PROFIT_SAMPLES[year]))
        for year in AMBIGUOUS_YEARS
    ]
    stream += _stroke(BLUE, 1.2, SHARED_TAIL, 0.0, _polyline(revenue_tail))
    stream += _stroke(BLUE, 1.2, SHARED_TAIL, 0.0, _polyline(profit_tail))

    return stream.encode("latin-1")


def build_pdf() -> bytes:
    builder = PdfBuilder()
    catalog = builder.add("<< /Type /Catalog /Pages 2 0 R >>")
    assert catalog == 1
    pages = builder.add("<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    assert pages == 2
    page = builder.add(
        "<< /Type /Page /Parent 2 0 R "
        f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
        "/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> "
        "/Contents 8 0 R >>"
    )
    assert page == 3
    builder.add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    builder.add(
        "<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light "
        "/Encoding /UniGB-UCS2-H /DescendantFonts [6 0 R] >>"
    )
    builder.add(
        "<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> "
        "/FontDescriptor 7 0 R /DW 1000 >>"
    )
    builder.add(
        "<< /Type /FontDescriptor /FontName /STSong-Light /Flags 6 "
        "/FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 880 "
        "/Descent -120 /CapHeight 700 /StemV 80 >>"
    )
    content = build_content_stream()
    builder.add(
        b"<< /Length "
        + str(len(content)).encode("latin-1")
        + b" >>\nstream\n"
        + content
        + b"\nendstream"
    )
    return builder.render()


def main() -> None:
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "annual-report-chart.pdf")
    with open(out_path, "wb") as fh:
        fh.write(build_pdf())
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
