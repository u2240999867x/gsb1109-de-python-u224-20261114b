# Recovering line-chart data from vector PDFs

`pdfplumber.chart.extract_chart` reconstructs data points from a vector-PDF
line chart, including the common printed-annual-report case where every
indicator uses the same color and series identity is carried only by the
line's dash style.

```python
import pdfplumber
from pdfplumber.chart import extract_chart

with pdfplumber.open("annual-report.pdf") as pdf:
    extraction = extract_chart(pdf.pages[0], target_color=(0.122, 0.467, 0.705))

for record in extraction.to_records():
    print(record)
```

## How identity is resolved

1. **Legend.** Target-colored sample lines above the plot frame are paired
   with the label text beside them. Each legend entry records its dash array,
   its dash phase, and the original sample object.
2. **Plotted paths.** Stroked curves and lines inside the frame are matched
   to legend entries by dash **array**, never by dash phase. A path that
   resumes after a measurement gap typically restarts the dash pattern at
   another phase; the on/off rhythm still identifies the series.
3. **Color filter.** Objects that reuse a data series' dash array in another
   color (for example light-gray gridlines) are excluded.
4. **Axis scales.** Tick marks are paired with nearby numeric labels, then
   fit to linear scales. Pass explicit `x_ticks` / `y_ticks` to override the
   detection, or `frame` to supply the plot rectangle directly.

## Gaps and ambiguous continuations

- Independent stroked subpaths are kept separate. A genuine measurement gap
  is therefore never bridged or interpolated, and `connected_to_next` is
  `False` at the break. Internal gaps are listed in `extraction.gaps`.
- When a segment's style matches no unique legend entry — such as two series
  switching to one shared style after crossing — the segment is returned in
  `extraction.ambiguous_segments` with `status="ambiguous"` instead of being
  assigned a name.
- `candidate_series` records which named series geometrically could continue
  into an ambiguous segment (the series whose last named point sits on the
  preceding tick). When more than one series qualifies, the segment remains
  unnamed; identifiable samples before the crossing are still tabled
  normally.

## Provenance

Every `DataPoint` keeps the live page object it came from (`source`) plus a
stable textual reference (`source_ref`, e.g. `page1:curve[2]`). Extraction is
read-only: the page's object lists are not modified, so the original page
remains the source of truth.
