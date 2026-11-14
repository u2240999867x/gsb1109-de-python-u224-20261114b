#!/usr/bin/env python
"""Tests for separate AcroForm field-value / widget-appearance collection."""

import json
import os
from io import BytesIO
from typing import List

import pdfplumber

HERE = os.path.abspath(os.path.dirname(__file__))


def build_pdf() -> bytes:
    """
    Hand-crafted 3-page AcroForm PDF exercising:

    * one field with a widget on each page (radio group, /V=/Yes) where the
      page-2 widget shows /Off
    * empty text field vs filled text field sharing the partial name "Name"
    * a field whose /V is inherited from a parent node
    * a widget with an unresolvable /Parent reference
    * an orphan widget whose parent is outside AcroForm /Fields
    * a checkbox whose /AS contradicts the field's /V
    """
    bodies: List[bytes] = []

    def add(body) -> int:
        if isinstance(body, str):
            body = body.encode("latin-1")
        bodies.append(body)
        return len(bodies)

    add(  # 1 catalog
        "<< /Type /Catalog /Pages 2 0 R /AcroForm 9 0 R >>"
    )
    add("<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R] /Count 3 >>")  # 2
    add(  # 3 page 1
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Contents 6 0 R /Annots [19 0 R 21 0 R 25 0 R 28 0 R 32 0 R] >>"
    )
    add(  # 4 page 2
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Contents 7 0 R /Annots [20 0 R 26 0 R] >>"
    )
    add(  # 5 page 3
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Contents 8 0 R /Annots [27 0 R 29 0 R 30 0 R 31 0 R] >>"
    )
    add(b"<< /Length 0 >>\nstream\n\nendstream")  # 6
    add(b"<< /Length 0 >>\nstream\n\nendstream")  # 7
    add(b"<< /Length 0 >>\nstream\n\nendstream")  # 8
    add(  # 9 AcroForm
        "<< /Fields [10 0 R 12 0 R 14 0 R 15 0 R 17 0 R] "
        "/NeedAppearances true >>"
    )
    add("<< /T (form1) /Kids [11 0 R] >>")  # 10 non-terminal
    add(  # 11 empty text field form1.Name
        "<< /T (Name) /FT /Tx /V () /Kids [19 0 R 20 0 R] >>"
    )
    add("<< /T (form2) /Kids [13 0 R] >>")  # 12 non-terminal
    add(  # 13 filled text field form2.Name (duplicate partial name)
        "<< /T (Name) /FT /Tx /V (Alice) /Kids [21 0 R] >>"
    )
    add(  # 14 radio group with logical value /Yes
        "<< /FT /Btn /Ff 32768 /T (Choice) /V /Yes "
        "/Kids [25 0 R 26 0 R 31 0 R 32 0 R] >>"
    )
    add(  # 15 parent carrying the value inherited by object 16
        "<< /T (Block) /V (inherited text) /Kids [16 0 R] >>"
    )
    add("<< /T (Note) /FT /Tx /Kids [27 0 R] >>")  # 16 no /V of its own
    add(  # 17 checkbox whose widget contradicts /V
        "<< /FT /Btn /T (Agree) /V /Yes /Kids [28 0 R] >>"
    )
    add("<< /T (OutsideTree) >>")  # 18 reachable parent, not in /Fields
    add(  # 19 page-1 widget of empty text field
        "<< /Type /Annot /Subtype /Widget /Parent 11 0 R /P 3 0 R "
        "/Rect [10 10 30 30] >>"
    )
    add(  # 20 page-2 widget of the same empty text field
        "<< /Type /Annot /Subtype /Widget /Parent 11 0 R /P 4 0 R "
        "/Rect [10 10 30 30] >>"
    )
    add(  # 21 widget of form2.Name
        "<< /Type /Annot /Subtype /Widget /Parent 13 0 R /P 3 0 R "
        "/Rect [40 10 60 30] >>"
    )
    xobject = (
        "<< /Type /XObject /Subtype /Form /BBox [0 0 20 20] "
        "/Resources << >> /Length 0 >>\nstream\n\nendstream"
    )
    add(xobject)  # 22 /Yes appearance for the repeated field widgets
    add(xobject)  # 23 /Off appearance for the repeated field widgets
    add(xobject)  # 24 /No appearance for the other-option radio widget
    add(  # 25 page-1 radio widget, capable of /Yes and checked
        "<< /Type /Annot /Subtype /Widget /Parent 14 0 R /P 3 0 R "
        "/Rect [70 10 90 30] /AS /Yes "
        "/AP << /N << /Yes 22 0 R /Off 23 0 R >> >> >>"
    )
    add(  # 26 page-2 radio widget of the same field, capable of /Yes but Off
        "<< /Type /Annot /Subtype /Widget /Parent 14 0 R /P 4 0 R "
        "/Rect [70 10 90 30] /AS /Off "
        "/AP << /N << /Yes 22 0 R /Off 23 0 R >> >> >>"
    )
    add(  # 27 widget of the inherited-value field
        "<< /Type /Annot /Subtype /Widget /Parent 16 0 R /P 5 0 R "
        "/Rect [10 10 30 30] >>"
    )
    add(  # 28 checkbox widget whose appearance contradicts /V
        "<< /Type /Annot /Subtype /Widget /Parent 17 0 R /P 3 0 R "
        "/Rect [100 10 120 30] /AS /No >>"
    )
    add(  # 29 widget with a dangling /Parent reference
        "<< /Type /Annot /Subtype /Widget /Parent 99 0 R /P 5 0 R "
        "/Rect [40 10 60 30] /AS /X >>"
    )
    add(  # 30 orphan widget (parent 18 is outside AcroForm /Fields)
        "<< /Type /Annot /Subtype /Widget /Parent 18 0 R /P 5 0 R "
        "/Rect [70 10 90 30] >>"
    )
    add(  # 31 other-option radio widget: only capable of /No, correctly Off
        "<< /Type /Annot /Subtype /Widget /Parent 14 0 R /P 5 0 R "
        "/Rect [100 10 120 30] /AS /Off "
        "/AP << /N << /No 24 0 R /Off 23 0 R >> >> >>"
    )
    add(  # 32 radio widget with no /AS and no /AP at all
        "<< /Type /Annot /Subtype /Widget /Parent 14 0 R /P 3 0 R "
        "/Rect [130 10 150 30] >>"
    )

    out = BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, body in enumerate(bodies, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(bodies) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def build_duplicate_path_pdf() -> bytes:
    """Two distinct top-level fields sharing both partial and full names."""
    bodies: List[bytes] = []

    def add(body) -> int:
        if isinstance(body, str):
            body = body.encode("latin-1")
        bodies.append(body)
        return len(bodies)

    add("<< /Type /Catalog /Pages 2 0 R /AcroForm 3 0 R >>")  # 1
    add(  # 2
        "<< /Type /Pages /Kids [4 0 R] /Count 1 >>"
    )
    add("<< /Fields [5 0 R 6 0 R] >>")  # 3 AcroForm
    add(  # 4 page
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Annots [7 0 R 8 0 R] >>"
    )
    add("<< /FT /Tx /T (Same) /V (one) /Kids [7 0 R] >>")  # 5
    add("<< /FT /Tx /T (Same) /V (two) /Kids [8 0 R] >>")  # 6
    add(  # 7
        "<< /Type /Annot /Subtype /Widget /Parent 5 0 R /P 4 0 R "
        "/Rect [10 10 30 30] >>"
    )
    add(  # 8
        "<< /Type /Annot /Subtype /Widget /Parent 6 0 R /P 4 0 R "
        "/Rect [40 10 60 30] >>"
    )

    out = BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, body in enumerate(bodies, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(bodies) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def open_scratch(data: bytes) -> pdfplumber.PDF:
    return pdfplumber.open(BytesIO(data))


def test_field_tree_logical_values_separate_from_appearances() -> None:
    with open_scratch(build_pdf()) as pdf:
        fields = {f["object_ref"]: f for f in pdf.form_fields}
        widgets = pdf.form_widgets

        # Five terminal fields; pure widget nodes are not fields.
        assert set(fields) == {11, 13, 14, 16, 17}

        choice = fields[14]
        assert choice["field_type"] == "Btn"
        assert choice["is_radio"] is True
        assert choice["value"] == "Yes"

        choice_widgets = [w for w in widgets if w["field_ref"] == 14]
        assert {w["page_number"] for w in choice_widgets} == {1, 2, 3}
        checked = next(w for w in choice_widgets if w["page_number"] == 1)
        unchecked = next(w for w in choice_widgets if w["page_number"] == 2)
        other_option = next(w for w in choice_widgets if w["page_number"] == 3)
        assert checked["appearance_state"] == "Yes"
        assert checked["status"] == "ok"
        # The page-2 widget shows /Off although it can display /Yes; that is a
        # missing page appearance, but it must not erase the logical value
        # collected from the field tree.
        assert unchecked["appearance_state"] == "Off"
        assert unchecked["on_states"] == ["Yes"]
        assert unchecked["logical_value"] == "Yes"
        assert unchecked["status"] == "appearance_missing"
        # A widget representing another radio option only offers /No, so its
        # /Off state is normal rather than a missing appearance.
        assert other_option["appearance_state"] == "Off"
        assert other_option["on_states"] == ["No"]
        assert other_option["status"] == "ok"

        no_appearance = next(
            w
            for w in choice_widgets
            if w["object_ref"] == 32
        )
        assert no_appearance["appearance_state"] is None
        assert no_appearance["appearance_source"] == "missing"
        assert no_appearance["on_states"] is None
        # Missing on-page appearance still leaves the logical value intact.
        assert no_appearance["logical_value"] == "Yes"
        assert no_appearance["status"] == "appearance_missing"
        assert choice["value"] == "Yes"


def test_empty_inherited_and_duplicate_name_records() -> None:
    with open_scratch(build_pdf()) as pdf:
        fields = {f["object_ref"]: f for f in pdf.form_fields}

        empty_field = fields[11]
        assert empty_field["value"] == ""
        assert empty_field["path"] == "form1.Name"

        filled_field = fields[13]
        assert filled_field["value"] == "Alice"
        assert filled_field["path"] == "form2.Name"

        # Repeated partial names are disambiguated via the parent path.
        assert empty_field["local_name"] == filled_field["local_name"] == "Name"
        assert empty_field["duplicate_name"] is True
        assert filled_field["duplicate_name"] is True
        assert empty_field["duplicate_path"] is False
        assert filled_field["duplicate_path"] is False

        inherited = fields[16]
        assert inherited["value"] == "inherited text"
        assert inherited["value_source"] == "inherited"
        assert inherited["value_defined_at"] == 15

        statuses = {}
        for row in pdf.field_report:
            statuses.setdefault(row["status"], []).append(row)

        empty_rows = statuses["empty"]
        assert len(empty_rows) == 1
        assert empty_rows[0]["field_ref"] == 11

        inherited_rows = statuses["inherited"]
        assert len(inherited_rows) == 1
        assert inherited_rows[0]["field_ref"] == 16

        duplicate_rows = statuses["duplicate_name"]
        assert {row["field_ref"] for row in duplicate_rows} == {11, 13}
        assert {row["field_path"] for row in duplicate_rows} == {
            "form1.Name",
            "form2.Name",
        }


def test_report_rows_trace_back_to_page_and_object() -> None:
    with open_scratch(build_pdf()) as pdf:
        by_status = {}
        for row in pdf.field_report:
            by_status.setdefault(row["status"], []).append(row)

        missing_appearance = [
            row
            for row in by_status["appearance_missing"]
            if row["object_ref"] == 26
        ]
        assert len(missing_appearance) == 1
        row = missing_appearance[0]
        assert row["page_number"] == 2
        assert row["object_ref"] == 26
        assert row["field_ref"] == 14
        assert row["parent_ref"] == 14
        assert row["value"] == "Yes"
        assert row["appearance_state"] == "Off"

        # A widget with no appearance at all is flagged on its page too.
        no_appearance = [
            row
            for row in by_status["appearance_missing"]
            if row["object_ref"] == 32
        ]
        assert len(no_appearance) == 1
        assert no_appearance[0]["page_number"] == 1
        assert no_appearance[0]["appearance_state"] is None

        conflicts = by_status["conflict"]
        assert len(conflicts) == 1
        assert conflicts[0]["page_number"] == 1
        assert conflicts[0]["object_ref"] == 28
        assert conflicts[0]["value"] == "Yes"
        assert conflicts[0]["appearance_state"] == "No"

        missing_parent = by_status["missing_parent"]
        assert len(missing_parent) == 1
        assert missing_parent[0]["page_number"] == 3
        assert missing_parent[0]["object_ref"] == 29
        assert missing_parent[0]["parent_ref"] == 99

        orphans = by_status["orphan_widget"]
        assert len(orphans) == 1
        assert orphans[0]["page_number"] == 3
        assert orphans[0]["object_ref"] == 30
        assert orphans[0]["parent_ref"] == 18

        # Every report row carries the references needed for triage.
        for row in pdf.field_report:
            assert "object_ref" in row
            assert "details" in row and row["details"]

        # Rows are plain JSON-serializable data.
        json.dumps(pdf.field_report)
        json.dumps(
            [
                {k: v for k, v in w.items() if k != "data"}
                for w in pdf.form_widgets
            ]
        )


def test_per_page_widgets() -> None:
    with open_scratch(build_pdf()) as pdf:
        page_1, page_2, page_3 = pdf.pages
        assert {w["object_ref"] for w in page_1.widgets} == {
            19,
            21,
            25,
            28,
            32,
        }
        assert {w["object_ref"] for w in page_2.widgets} == {20, 26}
        assert {w["object_ref"] for w in page_3.widgets} == {27, 29, 30, 31}
        # All widgets on every page retain the field-level logical value.
        for widget in page_2.widgets:
            if widget["field_ref"] == 14:
                assert widget["logical_value"] == "Yes"


def test_duplicate_full_path() -> None:
    with open_scratch(build_duplicate_path_pdf()) as pdf:
        fields = pdf.form_fields
        assert len(fields) == 2
        assert all(f["duplicate_name"] for f in fields)
        assert all(f["duplicate_path"] for f in fields)
        statuses = {row["status"] for row in pdf.field_report}
        assert "duplicate_path" in statuses
        assert "duplicate_name" in statuses


def test_pdf_without_form_returns_empty_collections() -> None:
    path = os.path.join(HERE, "pdfs/nics-background-checks-2015-11.pdf")
    with pdfplumber.open(path) as pdf:
        assert pdf.form_fields == []
        assert pdf.form_widgets == []
        assert pdf.field_report == []
        assert all(page.widgets == [] for page in pdf.pages)
        # Repeated access is cached.
        assert pdf.form is pdf.form


def test_widget_outside_acroform_is_reported_as_orphan() -> None:
    # annotations.pdf has no /AcroForm but does contain a widget annotation.
    path = os.path.join(HERE, "pdfs/annotations.pdf")
    with pdfplumber.open(path) as pdf:
        assert pdf.form_fields == []
        widgets = pdf.form_widgets
        assert len(widgets) == 1
        assert widgets[0]["status"] == "orphan_widget"
        report = pdf.field_report
        assert len(report) == 1
        assert report[0]["status"] == "orphan_widget"
        assert report[0]["page_number"] == widgets[0]["page_number"]
        assert report[0]["object_ref"] == widgets[0]["object_ref"]


def test_real_signature_pdf_merged_field_widget() -> None:
    path = os.path.join(HERE, "pdfs/federal-register-2020-17221.pdf")
    with pdfplumber.open(path) as pdf:
        fields = pdf.form_fields
        assert len(fields) == 1
        signature = fields[0]
        assert signature["field_type"] == "Sig"
        assert signature["path"] == "USGPOSignature"
        # /V points at a signature dictionary; keep the object reference
        # rather than trying to flatten the dictionary.
        assert signature["value"] == {"object_ref": 155}

        widgets = pdf.form_widgets
        assert len(widgets) == 1
        widget = widgets[0]
        assert widget["page_number"] == 1
        assert widget["field_ref"] == widget["object_ref"] == 144
        assert widget["status"] == "ok"
