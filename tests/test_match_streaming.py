"""Verify bounded streaming recognition preserves complete ranking semantics."""

import random
from collections import defaultdict

import pytest

from fingerprint_server.database import logic
from fingerprint_server.database.logic import QueryTooBroadError, rank_matches


def test_streaming_matches_original_ranking_for_shuffled_repeated_evidence() -> None:
    """Keep distinct votes, ties, repeated hashes, and duplicate rows deterministic."""
    rng = random.Random(42)
    query = [(f"{index % 5:020x}", index * 3) for index in range(40)]

    def metadata(reference):
        return '{"title":"' + reference + '","artists":["Artist"],"duration":120}'

    for _ in range(30):
        rows = [
            {
                "reference_id": f"reference-{rng.randrange(6)}",
                "recording_key": None,
                "delta": rng.randrange(-2, 4),
                "idx": rng.randrange(len(query)),
            }
            for _ in range(300)
        ]
        rows.extend(rows[:10])
        groups = defaultdict(set)
        for row in sorted(rows, key=lambda row: (row["idx"], row["delta"])):
            groups[(row["reference_id"], row["delta"])].add(row["idx"])
        best = {}
        for (reference, delta), indices in groups.items():
            if reference not in best or len(indices) > len(best[reference][1]):
                best[reference] = (delta, indices)
        expected = sorted(best.items(), key=lambda entry: (-len(entry[1][1]), entry[0]))[:2]
        rng.shuffle(rows)
        actual = rank_matches(query, iter(rows), metadata)
        assert [
            (item["reference_id"], item["matched_landmarks"], item["offset_seconds"])
            for item in actual
        ] == [
            (reference, len(indices), delta * 2048 / 44100)
            for reference, (delta, indices) in expected
        ]


def test_streaming_candidate_budget_fails_without_partial_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop at the budget and never rank or read metadata for a partial search."""
    monkeypatch.setattr(logic, "MAX_MATCH_CANDIDATES", 4)
    consumed = []

    def rows():
        for index in range(100):
            consumed.append(index)
            yield {
                "reference_id": "reference",
                "recording_key": None,
                "delta": index,
                "idx": 0,
            }

    def metadata(_reference):
        pytest.fail("A partial search must not read metadata")

    with pytest.raises(QueryTooBroadError):
        rank_matches([("0" * 20, 0)], rows(), metadata)
    assert consumed == list(range(5))


def test_streaming_keeps_evidence_beyond_former_limit() -> None:
    """Include a competing recording even when its rows follow the old 100k cap."""
    query = [(f"{index:020x}", index) for index in range(50)]

    def rows():
        for delta in range(100_000):
            yield {"reference_id": "noise", "recording_key": "noise", "delta": delta, "idx": 0}
        for reference in ("first", "second"):
            for index in range(50):
                yield {
                    "reference_id": reference,
                    "recording_key": reference,
                    "delta": 20,
                    "idx": index,
                }

    result = rank_matches(query, rows(), lambda reference: '{"title":"' + reference + '"}')
    assert [item["reference_id"] for item in result] == ["first", "second"]
    assert [item["matched_landmarks"] for item in result] == [50, 50]
