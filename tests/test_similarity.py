from __future__ import annotations

import json
from pathlib import Path

import pytest

import pylast
from pylast import (
    NodeState,
    ReplayScript,
    ReplaySimilaritySource,
    SimilarityCandidate,
    SimilarityResponse,
)

GRAPH = {
    "A": ["B", "C"],
    "B": ["D", "E"],
    "C": ["D", "F"],
    "D": [],
    "E": ["G"],
    "F": ["G"],
    "X": ["B"],
}


def crawl(seeds=("A",), **kwargs):
    kwargs.setdefault("max_depth", 2)
    kwargs.setdefault("budget", 20)
    return pylast.crawl_similar_artists(
        seeds, source=ReplaySimilaritySource(GRAPH), **kwargs
    )


def test_direction_is_preserved_and_named_counts_are_per_target():
    result = crawl(seeds=("A", "X"))

    assert ("A", "B") in result.by_direction
    assert ("B", "A") not in result.by_direction
    assert set(result.named_counts) == {"B", "C", "D", "E", "F"}
    assert result.named_counts["B"] == 2
    assert result.named_counts["D"] == 2
    assert result.get_node("B").named_count == 2
    assert result.get_node("D").parents == ["B", "C"]


def test_corpus_keeps_one_record_per_directed_edge():
    result = crawl()

    assert len(result.corpus) == 6
    pairs = [(record["source"], record["target"]) for record in result.corpus]
    assert ("A", "B") in pairs
    assert ("B", "A") not in pairs
    d_records = [record for record in result.corpus if record["target"] == "D"]
    assert [record["source"] for record in d_records] == ["B", "C"]
    assert all(record["named_count"] == 2 for record in d_records)


def test_shared_neighbor_is_requested_once_but_records_all_parents():
    source = ReplaySimilaritySource(GRAPH)
    result = pylast.crawl_similar_artists(
        ("A", "X"), source=source, max_depth=3, budget=20
    )

    assert source.requested.count("B") == 1
    assert source.requested.count("D") == 1
    assert result.get_node("D").named_count == 2
    assert result.get_node("D").parents == ["B", "C"]
    assert result.by_direction[("B", "D")].seeds == ("A", "X")
    assert result.by_direction[("C", "D")].seeds == ("A",)


def test_shared_neighbor_records_every_seed_that_reaches_it():
    source = ReplaySimilaritySource(GRAPH)
    result = pylast.crawl_similar_artists(
        ("A", "X"), source=source, max_depth=3, budget=20
    )

    node = result.get_node("D")
    assert node.seeds == ["A", "X"]
    assert node.discovered_by == ("A", "X")
    assert node.seed_count == 2


def test_seed_reachable_by_a_shared_neighbor_keeps_all_seeds():
    graph = {
        "A": ["B"],
        "B": ["C"],
        "C": ["D"],
        "D": [],
        "X": ["C"],
    }
    result = pylast.crawl_similar_artists(
        ("A", "X"), source=ReplaySimilaritySource(graph), max_depth=3, budget=10
    )

    assert result.get_node("D").seeds == ["A", "X"]


def test_claim_order_is_auditable_and_breadth_first():
    source = ReplaySimilaritySource(GRAPH)
    result = pylast.crawl_similar_artists("A", source=source, max_depth=2, budget=20)

    assert result.claim_order == ("A", "B", "C")
    assert source.claim_order == result.claim_order
    assert [call.attempt for call in source.calls] == [1, 1, 1]
    assert result.claimed_artists == ("A", "B", "C")
    assert result.attempts_by_artist == {"A": 1, "B": 1, "C": 1}


def test_two_rounds_means_seeds_and_depth_one_are_requested():
    result = crawl(max_depth=2)

    assert result.claim_order == ("A", "B", "C")
    assert result.get_node("D").state is NodeState.BOUNDARY
    assert result.get_node("D").attempts == 0


def test_depth_zero_does_not_spend_budget():
    source = ReplaySimilaritySource(GRAPH)
    result = pylast.crawl_similar_artists("A", source=source, max_depth=0, budget=5)

    assert source.calls == []
    assert result.budget_used == 0
    assert result.get_node("A").state is NodeState.BOUNDARY


def test_failure_retries_within_remaining_budget():
    source = ReplaySimilaritySource(
        {
            "A": ["B"],
            "B": ReplayScript([RuntimeError("temporary"), ["C"]]),
            "C": [],
        }
    )
    result = pylast.crawl_similar_artists(
        "A", source=source, max_depth=2, budget=3, max_retries=1
    )

    assert [
        (record.artist, record.attempt, record.outcome) for record in result.request_log
    ] == [
        ("A", 1, "ok"),
        ("B", 1, "error"),
        ("B", 2, "ok"),
    ]
    assert result.budget_used == 3
    assert result.get_node("B").state is NodeState.COMPLETE
    assert result.failures == []
    assert result.claimed_artists == ("A", "B")
    assert result.attempts_by_artist == {"A": 1, "B": 2}


def test_failure_does_not_retry_when_budget_is_gone():
    source = ReplaySimilaritySource(
        {"A": ["B"], "B": RuntimeError("temporary"), "C": []}
    )
    result = pylast.crawl_similar_artists(
        "A", source=source, max_depth=2, budget=2, max_retries=5
    )

    assert [(record.artist, record.attempt) for record in result.request_log] == [
        ("A", 1),
        ("B", 1),
    ]
    assert result.budget_used == 2
    assert result.budget_exhausted is True
    assert result.get_node("B").state is NodeState.FAILED


def test_exhausted_retry_is_reported_as_failed():
    source = ReplaySimilaritySource({"A": ["B"], "B": RuntimeError("temporary")})
    result = pylast.crawl_similar_artists(
        "A", source=source, max_depth=2, budget=10, max_retries=1
    )

    assert result.get_node("B").attempts == 2
    assert result.get_node("B").state is NodeState.FAILED
    assert len(result.failures) == 1
    assert "temporary" in result.get_node("B").error


def test_budget_exhaustion_separates_unvisited_empty_and_truncated():
    responses = {
        "A": SimilarityResponse(["B", "C"], truncated=True),
        "B": [],
        "C": ["D", "E"],
        "D": [],
        "E": [],
    }
    result = pylast.crawl_similar_artists(
        "A",
        source=ReplaySimilaritySource(responses),
        max_depth=3,
        budget=2,
        limit=2,
    )

    assert result.report()["unvisited"] == ["C"]
    assert result.report()["no_neighbors"] == ["B"]
    assert result.report()["truncated"] == ["A"]
    assert result.get_node("A").state is NodeState.TRUNCATED
    assert result.get_node("B").state is NodeState.EMPTY
    assert result.get_node("C").state is NodeState.UNVISITED
    assert result.budget_exhausted is True
    assert result.remaining_budget == 0


def test_limit_marks_response_as_truncated_even_without_envelope():
    responses = {"A": ["B", "C"], "B": [], "C": []}
    result = pylast.crawl_similar_artists(
        "A", source=ReplaySimilaritySource(responses), max_depth=2, budget=5, limit=2
    )

    assert result.get_node("A").state is NodeState.TRUNCATED
    assert result.truncated == (result.get_node("A"),)


def test_short_response_is_complete():
    responses = {"A": ["B"], "B": []}
    result = pylast.crawl_similar_artists(
        "A", source=ReplaySimilaritySource(responses), max_depth=2, budget=5, limit=5
    )

    assert result.get_node("A").state is NodeState.COMPLETE
    assert result.complete == (result.get_node("A"),)


def test_zero_or_none_limit_disables_truncation_detection():
    responses = {"A": ["B"], "B": []}
    result = pylast.crawl_similar_artists(
        "A", source=ReplaySimilaritySource(responses), max_depth=2, budget=5, limit=0
    )

    assert result.get_node("A").state is NodeState.COMPLETE


def test_full_limit_detection_can_be_overridden_by_envelope():
    responses = {
        "A": SimilarityResponse(["B"], truncated=False),
        "B": [],
    }
    result = pylast.crawl_similar_artists(
        "A", source=ReplaySimilaritySource(responses), max_depth=2, budget=5, limit=1
    )

    assert result.get_node("A").state is NodeState.COMPLETE


def test_total_greater_than_returned_marks_truncated():
    responses = {
        "A": SimilarityResponse(["B"], total=10),
        "B": [],
    }
    result = pylast.crawl_similar_artists(
        "A", source=ReplaySimilaritySource(responses), max_depth=2, budget=5
    )

    assert result.get_node("A").state is NodeState.TRUNCATED


def test_cycle_does_not_requeue_visited_nodes():
    responses = {"A": ["B"], "B": ["A", "C"], "C": ["A"]}
    result = pylast.crawl_similar_artists(
        "A", source=ReplaySimilaritySource(responses), max_depth=5, budget=10
    )

    assert result.claim_order == ("A", "B", "C")
    assert result.get_node("A").named_count >= 1


def test_duplicate_candidates_from_same_source_count_once():
    responses = {
        "A": ["B", "b", SimilarityCandidate("B", 0.9)],
        "B": [],
    }
    result = pylast.crawl_similar_artists(
        "A", source=ReplaySimilaritySource(responses), max_depth=2, budget=5
    )

    assert result.named_counts["B"] == 1
    assert [edge.target for edge in result.edges] == ["B"]


def test_candidate_objects_and_similar_items_are_supported():
    class FakeSimilarItem:
        def __init__(self, name, match):
            self.item = type("FakeArtist", (), {"name": name})()
            self.match = match

    responses = {
        "A": [FakeSimilarItem("B", 0.5), {"name": "C", "match": "0.25"}],
        "B": [],
        "C": [],
    }
    result = pylast.crawl_similar_artists(
        "A", source=ReplaySimilaritySource(responses), max_depth=2, budget=5
    )

    assert result.by_direction[("A", "B")].match == 0.5
    assert result.by_direction[("A", "C")].match == 0.25


def test_replay_script_can_exhaust():
    source = ReplaySimilaritySource(
        {
            "A": ReplayScript(
                [RuntimeError("one"), RuntimeError("two")], repeat_last=False
            )
        }
    )
    result = pylast.crawl_similar_artists(
        "A", source=source, max_depth=2, budget=5, max_retries=1
    )

    assert result.get_node("A").attempts == 2
    assert result.get_node("A").state is NodeState.FAILED


def test_save_json_and_jsonl(tmp_path: Path):
    result = crawl()
    json_path = result.save(tmp_path / "crawl.json")
    jsonl_path = result.save(tmp_path / "crawl.jsonl")

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["report"]["budget_used"] == 3
    assert payload["named_counts"]["D"] == 2
    assert payload["nodes"]["D"]["parents"] == ["B", "C"]
    assert len(payload["edges"]) == 6

    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6
    assert json.loads(lines[0])["source"] == "A"


def test_nested_directed_adjacency():
    result = crawl()
    d_edge = result.directed["B"]["D"]

    assert d_edge.source == "B"
    assert d_edge.target == "D"
    assert d_edge.named_count == 2


def test_network_adapter_calls_get_similar_once_per_artist():
    class FakeArtist:
        def __init__(self, name, network):
            self.name = name
            self.network = network

        def get_similar(self, limit=None):
            self.network.calls.append((self.name, limit))
            return [
                pylast.SimilarItem(FakeArtist(name, self.network), 1.0)
                for name in GRAPH.get(self.name, [])
            ]

    class FakeNetwork:
        def __init__(self):
            self.calls = []

        def get_artist(self, name):
            return FakeArtist(name, self)

    network = FakeNetwork()
    result = pylast.crawl_similar_artists(
        "A", network=network, max_depth=2, budget=10, limit=3
    )

    assert network.calls == [("A", 3), ("B", 3), ("C", 3)]
    assert result.get_node("D").named_count == 2


def test_callable_source_receives_limit():
    seen = []

    def source(artist, limit=None):
        seen.append((artist, limit))
        return GRAPH.get(artist, [])

    pylast.crawl_similar_artists("A", source=source, max_depth=2, budget=10, limit=4)

    assert seen == [("A", 4), ("B", 4), ("C", 4)]


def test_mapping_source_is_accepted_directly():
    result = pylast.crawl_similar_artists("A", source=GRAPH, max_depth=2, budget=5)

    assert result.get_node("A").state is NodeState.COMPLETE


def test_duplicate_seeds_are_collapsed():
    source = ReplaySimilaritySource(GRAPH)
    result = pylast.crawl_similar_artists(
        ["A", "a", " A "], source=source, max_depth=2, budget=5
    )

    assert result.seeds == ("A",)
    assert source.requested.count("A") == 1


def test_without_budget_is_unlimited():
    source = ReplaySimilaritySource(GRAPH)
    result = pylast.crawl_similar_artists("A", source=source, max_depth=3, budget=None)

    assert result.budget is None
    assert result.remaining_budget is None
    assert result.budget_exhausted is False


@pytest.mark.parametrize("budget", [-1, 1.5, True])
def test_invalid_budget_is_rejected(budget):
    with pytest.raises((TypeError, ValueError)):
        pylast.crawl_similar_artists(
            "A", source=ReplaySimilaritySource(GRAPH), budget=budget
        )


def test_missing_source_is_rejected():
    with pytest.raises(ValueError, match="Pass one of"):
        pylast.crawl_similar_artists("A", budget=1)


def test_state_aliases():
    assert NodeState.NOT_VISITED is NodeState.UNVISITED
    assert NodeState.NO_NEIGHBORS is NodeState.EMPTY
    assert NodeState.NO_NEIGHBOURS is NodeState.EMPTY


def test_crawler_instance_is_reusable():
    crawler = pylast.SimilarityCrawler(
        source=ReplaySimilaritySource(GRAPH), max_depth=2, limit=None
    )
    first = crawler.crawl("A", budget=5)
    second = crawler.crawl("X", budget=5)

    assert first.seeds == ("A",)
    assert second.seeds == ("X",)
    assert first.budget_used == 3
