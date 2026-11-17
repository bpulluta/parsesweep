"""Offline scheduling invariants and bounded nonempty synthetic searches."""

from __future__ import annotations

import time
import tracemalloc
from collections import Counter

import pytest

from psweep.discovery import DiscoveryEngine, DiscoveryRequest
from psweep.discovery.attempt_budget import AttemptBudget
from psweep.discovery.candidate_selector import CandidateSelector
from psweep.discovery.connectors.base import SeekerInput
from psweep.discovery.constants import CONFIGURED_SOURCE
from psweep.discovery.models import CandidateScore, DiscoveryCandidate


def candidate(owner, rank, *, source="serpapi_google"):
    return DiscoveryCandidate(
        url=f"https://source.test/{owner}-{rank}.pdf",
        source=source,
        target_id=owner,
        score=CandidateScore(),
    )


def test_file_cap_visits_owners_round_robin_without_reordering_their_ranks():
    groups = [[candidate(owner, rank) for rank in range(3)] for owner in "ABC"]
    ordinary = [item for group in groups for item in group]
    configured = candidate("shared", 0, source=CONFIGURED_SOURCE)
    reused = {DiscoveryEngine._reuse_key(groups[0][0]): {"status": "downloaded"}}
    staged, cut = DiscoveryEngine._stage_downloads([*ordinary, configured], 4, reused)
    assert staged == [
        configured,
        groups[0][0],
        groups[0][1],
        groups[1][0],
        groups[2][0],
        groups[0][2],
    ]
    assert cut == [*groups[1][1:], *groups[2][1:]]
    staged, cut = DiscoveryEngine._stage_downloads([*ordinary, configured], None, reused)
    assert staged == [configured, *ordinary] and not cut
    staged, cut = DiscoveryEngine._stage_downloads([*ordinary, configured], 0, reused)
    assert staged == [configured, groups[0][0]] and cut == ordinary[1:]


def test_shared_source_owners_are_fair_not_one_anonymous_pool():
    groups = [[candidate(None, rank) for rank in range(2)] for _ in range(2)]
    for idx, group in enumerate(groups):
        for item in group:
            item.source_url = f"https://owner.test/{idx}"
    staged, _cut = DiscoveryEngine._stage_downloads(
        [item for group in groups for item in group], 2, {}
    )
    assert staged == [groups[0][0], groups[1][0]]


def test_query_slots_keep_initial_target_order_and_skip_exhausted_owners():
    inputs = [SeekerInput(queries=["A1", "A2"]), SeekerInput(queries=["B1"])]
    assert list(DiscoveryEngine._seeker_jobs(inputs, query_slot=False)) == list(enumerate(inputs))
    jobs = list(DiscoveryEngine._seeker_jobs(inputs, query_slot=True))
    assert [(idx, item.queries) for idx, item in jobs] == [(0, ["A1"]), (1, ["B1"]), (0, ["A2"])]


@pytest.mark.parametrize("size", [20, 1000, 10000])
def test_nonempty_synthetic_work_is_bounded_and_resume_reuses_success(monkeypatch, size):
    from psweep.discovery.connectors import serpapi_seeker

    targets = [{"label": f"T{idx}"} for idx in range(size)]
    req = DiscoveryRequest(
        domain="synthetic",
        targets=targets,
        partition_by=["label"],
        queries=["first {label}", "second {label}"],
        selection_primary_per_target=3,
        link_prioritization_mode="off",
        max_search_calls=size + 7,
    )
    successful = {}
    calls = Counter()
    in_flight = peak_jobs = 0

    class Seeker:
        def __init__(self, *, before_request, **_kwargs):
            self.before_request = before_request
            self.queries_run = 0
            self.query_results = []

        def discover(self, item):
            nonlocal in_flight, peak_jobs
            query = item.queries[0]
            if query not in successful:
                self.before_request()
                self.queries_run += 1
                calls[query] += 1
                in_flight += 1
                peak_jobs = max(peak_jobs, in_flight)
                successful[query] = [
                    {
                        "url": f"https://synthetic.test/{query.replace(' ', '-')}-{rank}.pdf",
                        "source": "serpapi_google",
                        "snippet": "Representative evidence " * 20,
                    }
                    for rank in range(8)
                ]
                in_flight -= 1
            return successful[query]

    monkeypatch.setattr(serpapi_seeker, "SerpApiSeeker", Seeker)
    select = CandidateSelector.select
    largest_pool = 0

    def bounded_select(self, groups, **kwargs):
        nonlocal largest_pool
        largest_pool = max(largest_pool, *(len(group) for group in groups))
        assert len(groups) == 1 and len(groups[0]) <= 11
        return select(self, groups, **kwargs)

    monkeypatch.setattr(CandidateSelector, "select", bounded_select)
    engine = DiscoveryEngine()
    engine._attempt_budget = AttemptBudget(max_search_calls=req.max_search_calls)
    state = {"enabled": True, "available": True}
    tracemalloc.start()
    start = time.monotonic()
    first, _, errors, _, metrics, physical = engine._run_seeker(req, state, [], [], query_slot=True)
    elapsed = time.monotonic() - start
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert physical == size + 7 and sum(calls.values()) == size + 7
    assert errors and len(first) == size * 3
    assert sum(metric.get("search_complete") is False for metric in metrics) == size - 7
    assert all(metric.get("pending_reason") == "budget" for metric in metrics[7:])
    assert list(calls)[:size] == [f"first T{idx}" for idx in range(size)]
    engine._attempt_budget = AttemptBudget(max_search_calls=req.max_search_calls)
    second, _, errors, _, metrics, physical = engine._run_seeker(
        req, state, [], [], query_slot=True
    )
    assert physical == size - 7 and not errors
    assert len(second) == size * 3 and len(metrics) == size
    assert all(count == 1 for count in calls.values()) and len(calls) == size * 2
    assert peak_jobs == 1 and largest_pool == 11
    print(
        f"synthetic={size} elapsed={elapsed:.3f}s peak={peak_bytes / 1024 / 1024:.1f}MiB "
        f"physical={size * 2} reused={size + 7} in_flight={peak_jobs} selected={len(second)}"
    )
