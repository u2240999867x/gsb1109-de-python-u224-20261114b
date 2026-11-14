from __future__ import annotations

from xml.dom import minidom

import pytest

from pylast import chartdiff


def make_page_xml(page, per_page, rows, total_pages=None, total=None):
    attrs = f'page="{page}" perPage="{per_page}"'
    if total_pages is not None:
        attrs += f' totalPages="{total_pages}"'
    if total is not None:
        attrs += f' total="{total}"'
    tracks = "".join(
        f'<track rank="{rank}">'
        f"<name>{title}</name>"
        f"<playcount>100</playcount>"
        f"<listeners>{listeners}</listeners>"
        f"<artist><name>{artist}</name></artist>"
        f"</track>"
        for rank, artist, title, listeners in rows
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>'
        f'<lfm status="ok"><tracks {attrs}>{tracks}</tracks></lfm>'
    )


def parse_page(page, per_page, rows):
    doc = minidom.parseString(make_page_xml(page, per_page, rows))
    return chartdiff.parse_geo_tracks_page(doc)


def make_snapshot(label, limit, per_page, pages):
    entries = []
    infos = {}
    for page_no, rows in pages.items():
        page_entries, info = parse_page(page_no, per_page, rows)
        entries.extend(page_entries)
        infos[info.page] = info
    return chartdiff.ChartSnapshot(
        label=label, entries=entries, pages=infos, limit=limit, per_page=per_page
    )


def rows_for(page, per_page, offset=0):
    # ranks 1-based, continuous across pages
    start = (page - 1) * per_page + 1 + offset
    return [
        (rank, f"artist{rank}", f"title{rank}", 1000 - rank)
        for rank in range(start, start + per_page)
    ]


class TestParseGeoTracksPage:
    def test_rank_comes_from_service_not_element_order(self):
        rows = [(7, "a7", "t7", 10), (3, "a3", "t3", 20), (9, "a9", "t9", 5)]
        entries, info = parse_page(1, 3, rows)

        assert [entry.rank for entry in entries] == [7, 3, 9]
        assert entries[0].title == "t7"
        assert entries[1].title == "t3"
        assert info.page == 1
        assert info.per_page == 3

    def test_missing_rank_attribute_raises(self):
        xml = (
            '<?xml version="1.0" ?><lfm status="ok">'
            '<tracks page="1" perPage="1">'
            "<track><name>t</name><artist><name>a</name></artist></track>"
            "</tracks></lfm>"
        )
        doc = minidom.parseString(xml)
        with pytest.raises(ValueError, match="rank"):
            chartdiff.parse_geo_tracks_page(doc)

    def test_page_metadata_is_captured(self):
        _, info = parse_page(2, 50, rows_for(2, 50))
        assert info.page == 2
        assert info.per_page == 50

    def test_entries_remember_source_page(self):
        entries, _ = parse_page(3, 50, rows_for(3, 50))
        assert {entry.page for entry in entries} == {3}


class TestSnapshotCompleteness:
    def test_ties_are_detected_from_shared_service_rank(self):
        snapshot = make_snapshot(
            "s",
            3,
            3,
            {1: [(1, "a1", "t1", 10), (2, "a2", "t2", 8), (2, "a3", "t3", 8)]},
        )
        assert snapshot.get("a1", "t1").tied is False
        assert snapshot.get("a2", "t2").tied is True
        assert snapshot.get("a3", "t3").tied is True

    def test_complete_snapshot_has_no_missing_pages(self):
        snapshot = make_snapshot("s", 100, 50, {1: rows_for(1, 50), 2: rows_for(2, 50)})
        assert snapshot.missing_pages() == []
        assert snapshot.gaps() == []

    def test_missing_middle_page_is_traceable(self):
        snapshot = make_snapshot("s", 150, 50, {1: rows_for(1, 50), 3: rows_for(3, 50)})
        assert snapshot.missing_pages() == [2]
        (gap,) = snapshot.gaps()
        assert gap.missing_pages == (2,)
        assert gap.rank_min == 51
        assert gap.rank_max == 100

    def test_unseen_bound_is_deepest_rank_plus_one(self):
        snapshot = make_snapshot("s", 50, 50, {1: rows_for(1, 50)})
        assert snapshot.unseen_rank_bound() == 51

    def test_unseen_bound_is_none_when_pages_are_missing(self):
        snapshot = make_snapshot("s", 150, 50, {1: rows_for(1, 50), 3: rows_for(3, 50)})
        assert snapshot.unseen_rank_bound() is None

    def test_boundary_tie_softens_unseen_bound(self):
        rows = rows_for(1, 49) + [(50, "a50a", "t50a", 8), (50, "a50b", "t50b", 8)]
        snapshot = make_snapshot("s", 50, 50, {1: rows})
        # the tie at rank 50 may continue past the stored rows
        assert snapshot.unseen_rank_bound() == 50

    def test_serialization_roundtrip_preserves_provenance(self):
        snapshot = make_snapshot("s", 150, 50, {1: rows_for(1, 50), 3: rows_for(3, 50)})
        restored = chartdiff.ChartSnapshot.from_dict(snapshot.to_dict())

        assert restored.limit == 150
        assert restored.per_page == 50
        assert restored.missing_pages() == [2]
        assert restored.gaps() == snapshot.gaps()
        assert restored.unseen_rank_bound() is None
        assert restored.get("artist120", "title120").rank == 120
        assert restored.get("artist120", "title120").page == 3


class TestDiffSnapshots:
    def make_prev(self):
        return make_snapshot("prev top 50", 50, 50, {1: rows_for(1, 50)})

    def test_exact_change_uses_service_ranks(self):
        prev = self.make_prev()
        # current chart: same tracks, but the response lists them in an
        # order that does not match the service ranks
        curr_rows = [(5, "artist1", "title1", 900), (2, "artist2", "title2", 950)]
        curr_rows += rows_for(1, 50)[2:]
        curr_rows.sort(key=lambda row: -row[3])
        curr = make_snapshot("curr top 50", 50, 50, {1: curr_rows})

        diff = chartdiff.diff_snapshots(prev, curr)

        by_title = {change.title: change for change in diff.exact}
        assert by_title["title1"].prev_rank == 1
        assert by_title["title1"].curr_rank == 5
        assert by_title["title1"].delta == -4
        assert by_title["title2"].delta == 0
        assert diff.bounded == []
        assert diff.indeterminate == []

    def test_unseen_track_is_bounded_not_ranked_51(self):
        prev = self.make_prev()
        curr_rows = [(12, "newartist", "newsong", 999)] + [
            row for row in rows_for(1, 100) if row[1] != "artist12"
        ]
        curr = make_snapshot(
            "curr top 100", 100, 50, {1: curr_rows[:50], 2: curr_rows[50:]}
        )

        diff = chartdiff.diff_snapshots(prev, curr)

        assert all(change.prev_rank != 51 for change in diff.exact)
        assert all(change.title != "newsong" for change in diff.exact)
        (entered,) = [c for c in diff.bounded if c.title == "newsong"]
        assert entered.kind == "entered"
        assert entered.known_rank == 12
        assert entered.unknown_rank_min == 51
        assert entered.delta_min == 39  # climbed at least 51 - 12
        assert entered.delta_max is None

    def test_exited_track_is_bounded(self):
        prev = self.make_prev()
        curr_rows = [row for row in rows_for(1, 100) if row[1] != "artist40"]
        curr = make_snapshot(
            "curr top 100", 100, 50, {1: curr_rows[:50], 2: curr_rows[50:]}
        )

        diff = chartdiff.diff_snapshots(prev, curr)

        (exited,) = [c for c in diff.bounded if c.title == "title40"]
        assert exited.kind == "exited"
        assert exited.known_rank == 40
        assert exited.unknown_rank_min == 101
        assert exited.delta_max == -61  # fell at least 101 - 40
        assert exited.delta_min is None

    def test_missing_middle_page_keeps_surrounding_ranks_provable(self):
        prev = make_snapshot(
            "prev top 150",
            150,
            50,
            {
                1: rows_for(1, 50),
                2: rows_for(2, 50),
                3: rows_for(3, 50),
            },
        )
        # current chart lost page 2; ranks on pages 1 and 3 stay service-true
        curr = make_snapshot(
            "curr top 150",
            150,
            50,
            {
                1: rows_for(1, 50),
                3: rows_for(3, 50),
            },
        )

        diff = chartdiff.diff_snapshots(prev, curr)

        # ranks before and after the gap still produce exact rows
        exact_ranks = {change.curr_rank for change in diff.exact}
        assert 1 in exact_ranks
        assert 50 in exact_ranks
        assert 101 in exact_ranks
        assert 150 in exact_ranks
        after_gap = next(c for c in diff.exact if c.curr_rank == 120)
        assert after_gap.prev_rank == 120
        assert after_gap.delta == 0

        # tracks from prev's page 2 are absent from curr, and curr's gap
        # means nothing is provable for them
        assert diff.bounded == []
        assert len(diff.indeterminate) == 50
        assert {c.known_rank for c in diff.indeterminate} == set(range(51, 101))
        assert all(c.seen_in == "prev" for c in diff.indeterminate)
        assert all("page(s) 2" in c.reason for c in diff.indeterminate)

    def test_absence_with_gap_is_indeterminate_and_names_the_page(self):
        # previous snapshot lost page 2, so absence from it proves nothing
        prev = make_snapshot(
            "prev top 150", 150, 50, {1: rows_for(1, 50), 3: rows_for(3, 50)}
        )
        curr_rows = [
            (120, "newartist", "newsong", 500) if row[0] == 120 else row
            for row in rows_for(1, 150)
        ]
        curr = make_snapshot(
            "curr top 150",
            150,
            50,
            {1: curr_rows[:50], 2: curr_rows[50:100], 3: curr_rows[100:]},
        )

        diff = chartdiff.diff_snapshots(prev, curr)

        (change,) = [c for c in diff.indeterminate if c.title == "newsong"]
        assert change.seen_in == "curr"
        assert change.known_rank == 120
        assert "prev top 150" in change.reason
        assert "page(s) 2" in change.reason
        assert all(c.title != "newsong" for c in diff.exact)
        assert all(c.title != "newsong" for c in diff.bounded)

        # the displaced track is still provably gone from the current chart
        (exited,) = [c for c in diff.bounded if c.title == "title120"]
        assert exited.kind == "exited"
        assert exited.unknown_rank_min == 151

    def test_exit_with_gap_in_current_is_indeterminate(self):
        prev = self.make_prev()
        # page 2 lost; page 3 holds tracks never seen in prev
        page1_rows = [row for row in rows_for(1, 50) if row[0] != 30]
        curr = make_snapshot(
            "curr top 150", 150, 50, {1: page1_rows, 3: rows_for(3, 50)}
        )

        diff = chartdiff.diff_snapshots(prev, curr)

        (change,) = [c for c in diff.indeterminate if c.title == "title30"]
        assert change.seen_in == "prev"
        assert change.known_rank == 30
        assert "curr top 150" in change.reason
        assert "page(s) 2" in change.reason

        # ranks after the gap are still usable: page-3 tracks are provably
        # new relative to the complete previous top 50
        entered = [c for c in diff.bounded if c.kind == "entered"]
        assert {c.known_rank for c in entered} == set(range(101, 151))
        assert all(c.unknown_rank_min == 51 for c in entered)

    def test_tied_tracks_diff_by_service_rank(self):
        prev = make_snapshot(
            "prev",
            3,
            3,
            {1: [(1, "a1", "t1", 10), (2, "a2", "t2", 8), (2, "a3", "t3", 8)]},
        )
        curr = make_snapshot(
            "curr",
            3,
            3,
            {1: [(1, "a2", "t2", 12), (1, "a1", "t1", 12), (3, "a3", "t3", 9)]},
        )

        diff = chartdiff.diff_snapshots(prev, curr)

        by_title = {change.title: change for change in diff.exact}
        assert by_title["t1"].delta == 0  # 1 -> 1
        assert by_title["t2"].delta == 1  # 2 -> 1
        assert by_title["t3"].delta == -1  # 2 -> 3
        assert by_title["t2"].curr_tied is True
        assert by_title["t3"].prev_tied is True


class TestFetchGeoChartSnapshot:
    def test_fetches_one_request_per_page(self, monkeypatch):
        calls = []

        class FakeRequest:
            def __init__(self, network, method, params):
                self.params = dict(params)
                calls.append((method, dict(params)))

            def execute(self, cacheable=False):
                page = self.params["page"]
                return minidom.parseString(make_page_xml(page, 2, rows_for(page, 2)))

        monkeypatch.setattr(chartdiff, "_Request", FakeRequest)

        snapshot = chartdiff.fetch_geo_chart_snapshot(
            object(), "Croatia", limit=4, per_page=2
        )

        assert [call[1]["page"] for call in calls] == [1, 2]
        assert all(call[0] == "geo.getTopTracks" for call in calls)
        assert all(call[1]["country"] == "Croatia" for call in calls)
        assert [entry.rank for entry in snapshot.entries] == [1, 2, 3, 4]
        assert snapshot.missing_pages() == []
        assert snapshot.unseen_rank_bound() == 5
