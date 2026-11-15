from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from callfacts import (
    ArchiveInputError,
    ArchiveResult,
    chunks_from_dicts,
    load_archive,
    parse_archive,
    parse_records,
)

FIXTURE_DIR = Path(__file__).parent / "callfacts_fixtures"


def make_chunk(
    block_no: int,
    stream_id: int,
    direction: str,
    *,
    end_headers: bool = True,
    end_stream: bool = False,
    headers: list[tuple[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "block_no": block_no,
        "stream_id": stream_id,
        "direction": direction,
        "end_headers": end_headers,
        "end_stream": end_stream,
        "headers": headers or [],
    }


def response(block_no: int, stream_id: int, status: str, **kwargs: object) -> dict[str, object]:
    return make_chunk(
        block_no,
        stream_id,
        "response",
        headers=[(":status", status)],
        **kwargs,  # type: ignore[arg-type]
    )


class TestInterleavedArchive:
    @pytest.fixture(scope="class")
    @classmethod
    def result(cls) -> ArchiveResult:
        return load_archive(FIXTURE_DIR / "interleaved_archive.json")

    def test_streams_keep_archive_order(self, result: ArchiveResult) -> None:
        assert [fact.stream_id for fact in result.facts] == [1, 3]

    def test_complete_stream_phases(self, result: ArchiveResult) -> None:
        fact = result.facts[0]
        assert fact.stream_id == 1
        assert fact.state == "complete"

        assert fact.request is not None
        assert fact.request.frame_no == 1
        assert fact.request.block_numbers == (1,)
        assert fact.request.headers == (
            (":method", "GET"),
            (":path", "/a"),
            (":scheme", "https"),
        )

        assert len(fact.informational) == 1
        early = fact.informational[0]
        assert early.phase == "informational"
        assert early.status == 103
        assert early.frame_no == 1
        assert early.block_numbers == (3, 5)
        assert early.headers == (
            (":status", "103"),
            ("link", "</style.css>; rel=preload"),
            ("x-early-hint", "1"),
        )

        assert fact.final is not None
        assert fact.final.phase == "final"
        assert fact.final.status == 200
        assert fact.final.frame_no == 2
        assert fact.final.block_numbers == (7, 8)
        assert fact.final.headers == (
            (":status", "200"),
            ("content-type", "application/json"),
            ("content-length", "12"),
        )
        assert fact.final.end_stream is False

        assert len(fact.trailers) == 1
        trailer = fact.trailers[0]
        assert trailer.phase == "trailer"
        assert trailer.status is None
        assert trailer.frame_no == 3
        assert trailer.block_numbers == (9,)
        assert trailer.headers == (("x-checksum", "deadbeef"),)
        assert trailer.end_stream is True

        assert fact.open_frames == ()

    def test_undetermined_stream_keeps_103_and_open_frame(self, result: ArchiveResult) -> None:
        fact = result.facts[1]
        assert fact.stream_id == 3
        assert fact.final is None
        assert fact.state == "undetermined"
        assert [item.status for item in fact.informational] == [103, 103]
        assert [item.block_numbers for item in fact.informational] == [(4,), (6,)]
        assert fact.trailers == ()
        assert len(fact.open_frames) == 1
        assert fact.open_frames[0].direction == "response"
        assert fact.open_frames[0].block_numbers == (10,)
        assert fact.open_frames[0].headers == (("x-pending", "never-terminated"),)

    def test_stats_exclude_undetermined_stream(self, result: ArchiveResult) -> None:
        stats = result.stats
        assert stats.total_streams == 2
        assert stats.finalized_streams == 1
        assert stats.undetermined_streams == 1
        assert stats.successful_streams == 1
        assert stats.failed_streams == 0
        assert stats.success_rate == 1.0

    def test_every_fact_traces_back_to_original_block_numbers(self, result: ArchiveResult) -> None:
        document = json.loads((FIXTURE_DIR / "interleaved_archive.json").read_text())
        original_blocks = {chunk["block_no"] for chunk in document["chunks"]}

        traced: list[int] = []
        for fact in result.facts:
            if fact.request is not None:
                traced.extend(fact.request.block_numbers)
            for phase_fact in (*fact.informational, *fact.trailers):
                traced.extend(phase_fact.block_numbers)
            if fact.final is not None:
                traced.extend(fact.final.block_numbers)
            for open_frame in fact.open_frames:
                traced.extend(open_frame.block_numbers)

        assert sorted(traced) == sorted(original_blocks)


class TestPhaseStateMachine:
    def test_103_alone_never_finalizes_stream(self) -> None:
        chunks = chunks_from_dicts([
            make_chunk(1, 1, "request"),
            response(2, 1, "103"),
            response(3, 1, "103"),
        ])
        result = parse_archive(chunks)
        fact = result.facts[0]
        assert fact.state == "undetermined"
        assert fact.final is None
        assert len(fact.informational) == 2
        assert result.stats.finalized_streams == 0
        assert result.stats.undetermined_streams == 1
        assert result.stats.success_rate == 0.0

    def test_success_rate_counts_failures_but_not_undetermined(self) -> None:
        records = [
            make_chunk(1, 1, "request"),
            response(2, 1, "200", end_stream=True),
            make_chunk(3, 3, "request"),
            response(4, 3, "500", end_stream=True),
            make_chunk(5, 5, "request"),
            response(6, 5, "103"),
        ]
        result = parse_records(records)
        assert result.stats.total_streams == 3
        assert result.stats.finalized_streams == 2
        assert result.stats.undetermined_streams == 1
        assert result.stats.successful_streams == 1
        assert result.stats.failed_streams == 1
        assert result.stats.success_rate == 0.5

    def test_each_phase_keeps_its_own_fields(self) -> None:
        records = [
            make_chunk(1, 1, "request", headers=[(":method", "POST")]),
            response(2, 1, "100"),
            make_chunk(
                3,
                1,
                "response",
                headers=[(":status", "202"), ("content-length", "0")],
            ),
            make_chunk(4, 1, "response", end_stream=True, headers=[("x-trace", "abc")]),
        ]
        fact = parse_records(records).facts[0]
        assert fact.request is not None
        assert fact.request.headers == ((":method", "POST"),)
        assert fact.informational[0].headers == ((":status", "100"),)
        assert fact.final is not None
        assert fact.final.headers == ((":status", "202"), ("content-length", "0"))
        assert fact.trailers[0].headers == (("x-trace", "abc"),)

    def test_trailer_without_status_follows_final(self) -> None:
        records = [
            make_chunk(1, 1, "request"),
            response(2, 1, "200"),
            make_chunk(3, 1, "response", headers=[("x-t", "1")]),
            make_chunk(4, 1, "response", end_stream=True, headers=[("x-t2", "2")]),
        ]
        fact = parse_records(records).facts[0]
        assert len(fact.trailers) == 2
        assert all(item.status is None for item in fact.trailers)

    def test_status_after_final_is_rejected(self) -> None:
        records = [
            make_chunk(1, 1, "request"),
            response(2, 1, "200"),
            response(3, 1, "204", end_stream=True),
        ]
        with pytest.raises(ArchiveInputError, match=":status after the final response"):
            parse_records(records)

    def test_first_response_without_status_is_rejected(self) -> None:
        records = [
            make_chunk(1, 1, "request"),
            make_chunk(2, 1, "response", headers=[("content-type", "text/plain")]),
        ]
        with pytest.raises(ArchiveInputError, match="missing :status"):
            parse_records(records)

    def test_informational_frame_with_end_stream_is_rejected(self) -> None:
        records = [
            make_chunk(1, 1, "request"),
            response(2, 1, "103", end_stream=True),
        ]
        with pytest.raises(ArchiveInputError, match="must not carry END_STREAM"):
            parse_records(records)


class TestReassembly:
    def test_interleaved_continuation_chunks_group_per_stream(self) -> None:
        records = [
            make_chunk(1, 1, "response", end_headers=False, headers=[(":status", "200")]),
            make_chunk(2, 3, "response", end_headers=False, headers=[(":status", "404")]),
            make_chunk(3, 1, "response", end_headers=True, headers=[("x-a", "1")]),
            make_chunk(4, 3, "response", end_headers=True, end_stream=True, headers=[("x-b", "2")]),
        ]
        result = parse_records(records)
        first, second = result.facts
        assert first.final is not None
        assert second.final is not None
        assert first.final.block_numbers == (1, 3)
        assert first.final.headers == ((":status", "200"), ("x-a", "1"))
        assert second.final.block_numbers == (2, 4)
        assert second.final.status == 404
        assert second.final.end_stream is True

    def test_unterminated_frame_is_retained_as_open(self) -> None:
        records = [
            make_chunk(1, 1, "request"),
            make_chunk(2, 1, "response", end_headers=False, headers=[(":status", "200")]),
            make_chunk(3, 1, "response", end_headers=False, headers=[("x-more", "yes")]),
        ]
        fact = parse_records(records).facts[0]
        assert fact.final is None
        assert fact.state == "undetermined"
        assert len(fact.open_frames) == 1
        assert fact.open_frames[0].block_numbers == (2, 3)
        assert fact.open_frames[0].headers == ((":status", "200"), ("x-more", "yes"))

    def test_frame_numbers_restart_per_direction(self) -> None:
        records = [
            make_chunk(1, 1, "request"),
            response(2, 1, "103"),
            response(3, 1, "200"),
        ]
        fact = parse_records(records).facts[0]
        assert fact.request is not None
        assert fact.request.frame_no == 1
        assert fact.informational[0].frame_no == 1
        assert fact.final is not None
        assert fact.final.frame_no == 2

    def test_name_value_mapping_headers_are_accepted(self) -> None:
        records = [
            {
                "block_no": 1,
                "stream_id": 1,
                "direction": "response",
                "end_headers": True,
                "end_stream": True,
                "headers": [{"name": ":status", "value": "204"}],
            }
        ]
        fact = parse_records(records).facts[0]
        assert fact.final is not None
        assert fact.final.status == 204
        assert fact.final.headers == ((":status", "204"),)

    def test_empty_archive(self) -> None:
        result = parse_archive(iter(()))
        assert result.facts == ()
        assert result.stats.total_streams == 0
        assert result.stats.success_rate == 0.0


class TestInputValidation:
    def test_duplicate_block_number_is_rejected(self) -> None:
        records = [
            response(1, 1, "200", end_stream=True),
            response(1, 3, "200", end_stream=True),
        ]
        with pytest.raises(ArchiveInputError, match="duplicate block number 1"):
            parse_records(records)

    def test_missing_field_is_rejected(self) -> None:
        record = {"block_no": 1, "stream_id": 1, "direction": "response"}
        with pytest.raises(ArchiveInputError, match="missing required field"):
            chunks_from_dicts([record])

    def test_bad_direction_is_rejected(self) -> None:
        with pytest.raises(ArchiveInputError, match="direction"):
            chunks_from_dicts([make_chunk(1, 1, "bogus")])

    def test_multiple_request_frames_are_rejected(self) -> None:
        records = [
            make_chunk(1, 1, "request"),
            make_chunk(2, 1, "request"),
        ]
        with pytest.raises(ArchiveInputError, match="more than one request frame"):
            parse_records(records)

    def test_non_integer_status_is_rejected(self) -> None:
        records = [
            make_chunk(1, 1, "request"),
            make_chunk(2, 1, "response", headers=[(":status", "ok")]),
        ]
        with pytest.raises(ArchiveInputError, match="non-integer :status"):
            parse_records(records)

    def test_frame_after_end_stream_is_rejected(self) -> None:
        records = [
            make_chunk(1, 1, "request", end_stream=True),
            make_chunk(2, 1, "request"),
        ]
        with pytest.raises(ArchiveInputError, match="after END_STREAM"):
            parse_records(records)

    @pytest.mark.parametrize(
        ("field_name", "bad_value"),
        [
            ("block_no", "1"),
            ("block_no", True),
            ("stream_id", 1.5),
            ("stream_id", False),
            ("end_headers", "yes"),
            ("end_stream", 0),
            ("headers", "not-a-sequence"),
        ],
    )
    def test_chunk_field_types_are_validated(self, field_name: str, bad_value: object) -> None:
        record = make_chunk(1, 1, "request")
        record[field_name] = bad_value
        with pytest.raises(ArchiveInputError):
            chunks_from_dicts([record])

    @pytest.mark.parametrize(
        "bad_headers",
        [
            [("only-one",)],
            ["bare-string"],
            [("name", 1)],
            [(1, "value")],
            [{"name": "n"}],
        ],
    )
    def test_header_entry_shapes_are_validated(self, bad_headers: object) -> None:
        record = make_chunk(1, 1, "response", headers=bad_headers)  # type: ignore[arg-type]
        with pytest.raises(ArchiveInputError):
            chunks_from_dicts([record])

    def test_multiple_status_fields_are_rejected(self) -> None:
        records = [
            make_chunk(1, 1, "request"),
            make_chunk(2, 1, "response", headers=[(":status", "200"), (":status", "201")]),
        ]
        with pytest.raises(ArchiveInputError, match="multiple :status"):
            parse_records(records)

    def test_out_of_range_status_is_rejected(self) -> None:
        records = [
            make_chunk(1, 1, "request"),
            make_chunk(2, 1, "response", headers=[(":status", "99")]),
        ]
        with pytest.raises(ArchiveInputError, match="out-of-range :status"):
            parse_records(records)


class TestLoadArchive:
    def test_accepts_open_file_object_and_bare_list(self) -> None:
        path = FIXTURE_DIR / "interleaved_archive.json"
        with path.open(encoding="utf-8") as handle:
            from_file = load_archive(handle)
        from_path = load_archive(path)
        assert from_file.facts == from_path.facts

    def test_accepts_bare_list_from_file_object(self) -> None:
        document = json.dumps([response(1, 1, "200", end_stream=True)])
        result = load_archive(io.StringIO(document))
        assert result.facts[0].final is not None
        assert result.facts[0].final.status == 200

    def test_accepts_blocks_key(self, tmp_path: Path) -> None:
        record = response(1, 1, "200", end_stream=True)
        archive = tmp_path / "blocks.json"
        archive.write_text(json.dumps({"blocks": [record]}))
        result = load_archive(archive)
        assert result.facts[0].final is not None
        assert result.facts[0].final.status == 200

    @pytest.mark.parametrize(
        "document",
        [
            "42",
            '{"other": []}',
            '"a string"',
        ],
    )
    def test_rejects_unexpected_document_shapes(self, document: str) -> None:
        with pytest.raises(ArchiveInputError):
            load_archive(io.StringIO(document))
