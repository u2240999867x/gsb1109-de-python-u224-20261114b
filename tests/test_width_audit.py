"""Tests for fixed-width convention triage in width_audit."""

# local
import width_audit
from width_audit import CONVENTIONS, _demo_payload, analyze, segment


def test_factory_a_eliminates_narrow_convention_at_calibration_column():
    report = analyze(_demo_payload())["工厂A"]
    assert report["viable_conventions"] == ["ambiguous_wide(aw=2)"]
    assert len(report["eliminated"]) == 1
    eliminated = report["eliminated"][0]
    assert eliminated["convention"] == "ambiguous_narrow(aw=1)"
    assert eliminated["at"] == {"row": "A-CAL-1", "column": 6, "field": "货号"}


def test_factory_b_keeps_both_conventions_and_triages_rows():
    report = analyze(_demo_payload())["工厂B"]
    assert report["viable_conventions"] == list(CONVENTIONS)
    assert report["eliminated"] == []
    by_id = {row["id"]: row for row in report["rows"]}
    assert by_id["B-ROW-1"]["status"] == "ingest"
    assert by_id["B-ROW-1"]["differing_fields"] == []
    review_row = by_id["B-ROW-2"]
    assert review_row["status"] == "review"
    assert "货号" in review_row["differing_fields"]
    narrow = review_row["fields_by_convention"]["ambiguous_narrow(aw=1)"]["货号"]
    wide = review_row["fields_by_convention"]["ambiguous_wide(aw=2)"]["货号"]
    assert narrow != wide


def test_segment_splits_on_display_columns_not_character_offsets():
    boundaries = [0, 6, 12]
    line = "B·04XX" + "WIDGET" + "SPEC02"
    narrow = segment(line, boundaries, CONVENTIONS["ambiguous_narrow(aw=1)"])
    wide = segment(line, boundaries, CONVENTIONS["ambiguous_wide(aw=2)"])
    assert narrow == ["B·04XX", "WIDGET", "SPEC02"]
    assert wide[0] == "B·04X"
    assert narrow != wide


def test_pure_ascii_line_is_invariant_across_conventions():
    boundaries = [0, 6, 12]
    line = "BR0001" + "WIDGET" + "SPEC01"
    narrow = segment(line, boundaries, CONVENTIONS["ambiguous_narrow(aw=1)"])
    wide = segment(line, boundaries, CONVENTIONS["ambiguous_wide(aw=2)"])
    assert narrow == wide == ["BR0001", "WIDGET", "SPEC01"]
