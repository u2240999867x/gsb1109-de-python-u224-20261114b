# Stitching tables across page breaks

Manifest tables often span multiple pages, and a single item can be
mangled at the boundary between two pages. The `pdfplumber.seam` module
takes the rows returned by `Page.extract_table` / `Page.extract_tables`
for each page and stitches them back into one manifest, judging each
seam from the table's columns, its (possibly repeated) header, its
business-key columns, and its quantity column.

```python
import pdfplumber
from pdfplumber import seam

with pdfplumber.open("manifest.pdf") as pdf:
    pages = [
        page.extract_table(table_settings) or [] for page in pdf.pages
    ]

result = seam.stitch_pages(
    pages,
    key_columns=[0],       # business key, e.g. item/SKU number
    quantity_column=2,     # quantity column
    header=0,              # header row index on the first page
)
```

## What gets handled

- **Repeated header rows.** An exact repeat of a first-page header row on
  a later page is dropped and recorded in
  `result.duplicate_header_rows`; the original header itself is recorded
  in `result.header_rows` and excluded from the item rows.
- **Reprinted last line.** When the last line of a page is reprinted at
  the top (or just after a continuation fragment) of the next page, the
  copy is counted once. The reprint is recorded in
  `result.reprinted_rows` and on the owning row's `reprints` attribute.
- **Split long descriptions.** A keyless continuation row is joined back
  onto the item it extends, concatenating the split text. Every joined
  row keeps each piece's original `(page_index, row_index)` in
  `row.sources` / `row.lineage`.
- **Separate items with identical descriptions.** Two rows with
  different business keys always remain two items, even when their
  descriptions are identical.
- **Ambiguous keyless fragments.** A fragment with no key that could
  plausibly continue more than one earlier row is never force-joined. It
  is kept as an `ambiguous` row and a `SeamIssue` records the fragment
  location and every candidate row.

A fragment that carries its own quantity is treated as the start of a
new item rather than a continuation.

## Totals and provenance

`result.rows` holds `StitchedRow` objects; `result.to_cells()` returns
the plain list-of-lists view. `result.totals` reports the item count,
joined/ambiguous/reprinted counts, and the sum of the quantity column:

```python
result.totals
# {
#     "row_count": 4,
#     "joined_row_count": 1,
#     "ambiguous_row_count": 0,
#     "reprinted_row_count": 1,
#     "duplicate_header_count": 2,
#     "quantity_total": Decimal("10"),
# }
```

Each row retains both sides of every seam:

```python
row.sources       # [(0, 2), (1, 1)] — original (page_index, row_index)
row.lineage       # same information as Lineage objects
row.reprints      # locations of reprinted copies folded into this row
```

## Confirming a seam manually

After a collector confirms which earlier row an ambiguous fragment
belongs to, pass that one seam to `apply_confirmation`. Only that seam's
row, totals, and provenance change; every other row and issue is
recomputed identically.

```python
result = seam.stitch_pages(pages, key_columns=[0], quantity_column=2)
issue = result.issues[0]
confirmed = result.apply_confirmation(
    issue.page_index,
    issue.row_index,
    *issue.candidate_rows[0],   # chosen (page_index, row_index)
)
```

The target must be one of the issue's recorded candidates. Confirmations
may also be supplied up front through the `confirmations` argument of
`stitch_pages`.
