# Reading order

## The problem

`Page.extract_text(...)` and friends collate characters using page-global
coordinates. On a simple single-column page that is exactly right. But many
reports use a layout like this:

```
+--------------------------------------+
|          spanning title band         |
+------------------+-------------------+
|  upper-left      |  upper-right      |
|  column          |  column           |
|                  |                   |
+------------------+-------------------+
|     centered spanning subheading     |
+--------------------------------------+
|                                      |
|        lower full-width column       |
|                                      |
+--------------------------------------+
```

A page-global coordinate sort cannot represent this structure. In particular,
sorting by `x` first can place the **upper-right column after the lower
full-width block**, because the lower block starts further left. Reading order
is instead a property of the *frames*: read each framed block as a unit, then
order the units.

## `Page.get_reading_order(...)`

`get_reading_order` segments the page into **reading boundaries** — full-width
bands and side-by-side columns — and orders them from geometric evidence:

- **Top-to-bottom across bands.** Vertically separated frames are read in
  vertical order, so anything above a lower band (including an upper-right
  column) always comes first.
- **Left-to-right inside a band.** Frames that overlap vertically and sit
  beside one another form one horizontal band, read left-to-right.
- **Spanning bands interrupt the flow.** A frame with no side-by-side peer —
  including thin centered subheadings — is treated as a full-width boundary.

There is deliberately **no page-global x sort**.

### Frame detection

By default the method detects frames from the page's rectangle objects.
Rectangles that fully enclose another rectangle (such as an outer page border)
are removed. Frames must be at least `frame_min_width_ratio` of the page width
and `frame_min_height_ratio` of the page height; the height default is small so
that thin spanning bands survive.

You can bypass detection and supply the frame bounding boxes yourself:

```python
order = page.get_reading_order(
    frames=[(72, 130, 296, 400), (316, 130, 540, 400), (72, 490, 540, 720)]
)
```

When a page has no usable frames and none are supplied, the method falls back
to the text geometry. It infers columns from baselines whose words are
separated by a wide gutter and treats lines above/below the column region,
lines crossing a gutter, and lone centered lines as spanning bands. The
inferred ("virtual") frames are reported in `ReadingOrder.frames`.

Column widths and band heights are measured independently on every page, so a
narrow column on one page and a wide column on the next are handled correctly.

### Returned objects

`get_reading_order` returns a `ReadingOrder`:

- `ReadingOrder.segments`: the ordered `ReadingSegment` objects.
- `ReadingSegment.segment_id`: a stable 1-based id from the geometric default
  order.
- `ReadingSegment.bbox`: the **original** frame bounding box
  `(x0, top, x1, bottom)`.
- `ReadingSegment.layout`: `"column"` or `"full_width"`.
- `ReadingSegment.text` and `ReadingSegment.chars`: the text extracted from the
  characters assigned to that frame. Extra keyword arguments are forwarded to
  `pdfplumber.utils.extract_text`.
- `ReadingOrder.frames`: metadata for every frame, including its detected,
  explicit, or virtual `source`.
- `ReadingOrder.ambiguities`: positions where the geometry does not settle the
  order.

`ReadingOrder.extract_text(separator="\n\n", **kwargs)` joins the segment
texts in reading order, and `ReadingOrder.to_dict(...)` serializes the result.

## Ambiguous positions

Sometimes two frames are horizontally disjoint but overlap vertically only
slightly, or overlap in both dimensions, so that reading them either way is
consistent with the geometry. Rather than guess, the method records a
`ReadingAmbiguity` at that adjacent position:

- `segment_a`, `segment_b`: the two segment ids involved;
- `bbox_a`, `bbox_b`: their frame boxes;
- `reason`: why the order is not settled;
- `candidates`: the two defensible orders, e.g. `[[1, 2], [2, 1]]`.

The segments are still emitted in a deterministic geometric default order; the
ambiguity simply marks where a human may want to override.

## Fixed precedence

Pin the few relations that geometry cannot settle with a list of
`(earlier_segment_id, later_segment_id)` pairs:

```python
order = page.get_reading_order(precedence=[(2, 1)])
```

Segment ids come from the geometric default order and stay stable when the
constraints are applied; only the ordering changes. Constraints that reference
unknown ids, a segment itself, or that form a cycle raise `ValueError`.
Ambiguities are recomputed against the final order.
