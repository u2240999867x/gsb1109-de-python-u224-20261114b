import copy

import iso8583
import iso8583.specs
import pytest
from iso8583 import audit


@pytest.fixture
def spec():
    return copy.deepcopy(iso8583.specs.default_ascii)


def encode(spec, doc):
    raw, _ = iso8583.encode(doc, spec)
    return raw


def test_request_response_pair(spec):
    req = encode(spec, {"t": "0200", "11": "000123", "37": "REF000000001"})
    resp = encode(
        spec, {"t": "0210", "11": "000123", "37": "REF000000001", "39": "00"}
    )

    report = audit.reconcile([req, resp], spec)

    assert len(report.packets) == 2
    assert len(report.businesses) == 1
    business = report.businesses[0]
    assert business.status == "matched"
    assert business.requests == [0]
    assert business.responses == [1]
    assert business.tracking == {"11": "000123", "37": "REF000000001"}
    assert report.summary() == {
        "packets": 2,
        "packet_categories": {"request": 1, "response": 1},
        "repeats": 0,
        "businesses": 1,
        "business_status": {"matched": 1, "open": 0, "unmatched_response": 0},
    }


def test_request_and_notification_same_tracking_stay_separate(spec):
    # 0200 request and 0421 repeated reversal advice share STAN/RRN but
    # belong to different message functions; neither may be swallowed.
    req = encode(spec, {"t": "0200", "11": "000123", "37": "REF000000001"})
    advice_repeat = encode(
        spec, {"t": "0421", "11": "000123", "37": "REF000000001"}
    )

    report = audit.reconcile([req, advice_repeat], spec)

    assert len(report.packets) == 2
    assert len(report.businesses) == 2
    assert report.packets[0].category is audit.MessageCategory.REQUEST
    assert report.packets[1].category is audit.MessageCategory.NOTIFICATION
    assert report.packets[1].is_repeat
    assert report.businesses[0].requests == [0]
    assert report.businesses[1].notifications == [1]
    assert report.businesses[1].repeats == [1]


def test_repeat_request_attaches_to_original_business(spec):
    req = encode(spec, {"t": "0200", "11": "000123", "37": "REF000000001"})
    req_repeat = encode(spec, {"t": "0201", "11": "000123", "37": "REF000000001"})
    resp = encode(
        spec, {"t": "0210", "11": "000123", "37": "REF000000001", "39": "00"}
    )

    report = audit.reconcile([req, req_repeat, resp], spec)

    assert len(report.packets) == 3
    assert len(report.businesses) == 1
    business = report.businesses[0]
    assert business.status == "matched"
    assert business.requests == [0, 1]
    assert business.repeats == [1]
    assert business.responses == [2]
    assert report.summary()["repeats"] == 1


def test_unmatched_response_kept_with_provenance(spec):
    req = encode(spec, {"t": "0200", "11": "000123", "37": "REF000000001"})
    orphan_resp = encode(
        spec, {"t": "0210", "11": "000456", "37": "REF000000002", "39": "00"}
    )

    report = audit.reconcile([req, orphan_resp], spec)

    assert len(report.businesses) == 2
    orphan = report.businesses[1]
    assert orphan.status == "unmatched_response"
    assert orphan.responses == [1]
    assert orphan.packet_seqs == [1]
    assert orphan.tracking == {"11": "000456", "37": "REF000000002"}
    assert report.unmatched() == [report.businesses[0], orphan]
    assert report.summary()["business_status"] == {
        "matched": 0,
        "open": 1,
        "unmatched_response": 1,
    }


def test_tracking_number_not_matched_by_trailing_digits(spec):
    # STANs share trailing digits but differ in full value
    req_a = encode(spec, {"t": "0200", "11": "100123", "37": "REF000000001"})
    req_b = encode(spec, {"t": "0200", "11": "000123", "37": "REF000000001"})

    report = audit.reconcile([req_a, req_b], spec)

    assert len(report.businesses) == 2
    assert report.businesses[0].tracking["11"] == "100123"
    assert report.businesses[1].tracking["11"] == "000123"


def test_response_matches_earliest_open_business(spec):
    req_1 = encode(spec, {"t": "0200", "11": "000123", "37": "REF000000001"})
    req_2 = encode(spec, {"t": "0200", "11": "000123", "37": "REF000000001"})
    resp = encode(
        spec, {"t": "0210", "11": "000123", "37": "REF000000001", "39": "00"}
    )

    report = audit.reconcile([req_1, req_2, resp], spec)

    assert len(report.businesses) == 2
    assert report.businesses[0].status == "matched"
    assert report.businesses[0].responses == [2]
    assert report.businesses[1].status == "open"


def test_institution_duplicate_field_marks_repeat(spec):
    req = encode(spec, {"t": "0200", "11": "000123", "37": "REF000000001"})
    dup = encode(
        spec, {"t": "0200", "11": "000123", "37": "REF000000001", "48": "DUP"}
    )

    report = audit.reconcile(
        [req, dup], spec, duplicate_field="48", duplicate_values={"DUP"}
    )

    assert len(report.businesses) == 1
    assert report.packets[1].is_repeat
    assert report.businesses[0].repeats == [1]


def test_notification_response_pair(spec):
    advice = encode(spec, {"t": "0420", "11": "000123", "37": "REF000000001"})
    advice_resp = encode(
        spec, {"t": "0430", "11": "000123", "37": "REF000000001", "39": "00"}
    )

    report = audit.reconcile([advice, advice_resp], spec)

    assert len(report.businesses) == 1
    business = report.businesses[0]
    assert business.status == "matched"
    assert business.notifications == [0]
    assert business.responses == [1]


def test_decoded_dicts_accepted():
    report = audit.reconcile(
        [
            {"t": "0200", "11": "000123"},
            {"t": "0210", "11": "000123", "39": "00"},
        ]
    )

    assert len(report.packets) == 2
    assert report.businesses[0].status == "matched"


def test_invalid_mti_rejected():
    with pytest.raises(ValueError, match="Message MTI must be 4 digits"):
        audit.reconcile([{"t": "200", "11": "000123"}])


def test_invalid_message_type_rejected(spec):
    with pytest.raises(
        TypeError, match="Message must be bytes, bytearray, or dict, not list"
    ):
        audit.reconcile([[]], spec)


def test_raw_message_requires_spec():
    with pytest.raises(TypeError, match="spec is required"):
        audit.reconcile([b"0200"])
