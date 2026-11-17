"""Offline automatic fallback fixtures: no seeded/manual URLs or provider access."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
import requests

from discovery_helpers import FakeResponse, MeteredReviewClient
from psweep.discovery import DiscoveryEngine, DiscoveryRequest
from psweep.discovery.adaptive_search import (
    covered_labels,
    distinct_accepted_count,
    eligible_targets,
)
from psweep.discovery.connectors.serpapi_seeker import SerpApiSeeker
from psweep.discovery.document_reviewer import DocumentReviewer
from psweep.discovery.targets import identify_target
from psweep.extraction import document_utils, llm_factory


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(SerpApiSeeker, "_ensure_prepared", lambda _self: None)
    monkeypatch.setattr(
        DiscoveryEngine,
        "_resolve_serpapi_state",
        staticmethod(
            lambda _request: ({"provider": "serpapi", "enabled": True, "available": True}, [], [])
        ),
    )
    monkeypatch.setattr(
        llm_factory,
        "resolve_llm_kwargs",
        lambda model, **_kwargs: {"model": model or "fixture", "provider": "fixture"},
    )
    quote = "Fixture identity and required evidence history status."
    monkeypatch.setattr(
        document_utils,
        "extract_text_from_document",
        lambda path: Path(path).read_text(encoding="utf-8") + "\n" + (quote + " ") * 100,
    )
    responses = {}
    queries = []
    fetched = []
    grades = Counter()
    decisions = {}
    failures = set()

    def search(params):
        queries.append(params["q"])
        result = responses[params["q"]]
        if isinstance(result, Exception):
            raise result
        return {"organic_results": [{"link": url} for url in result]}

    def download(url, **_kwargs):
        fetched.append(url)
        if url in failures:
            raise requests.ConnectionError("fixture download failure")
        return FakeResponse(url=url, chunks=[b"%PDF-1.4\n" + url.encode()])

    class Client(MeteredReviewClient):
        def respond(self, *, user_prompt, **_kwargs):
            url = next(url for url in decisions if url in user_prompt)
            grades[url] += 1
            decision = decisions[url]
            level = decision["grade"] if isinstance(decision, dict) else decision
            return {
                "data": {
                    "grade": level,
                    "doc_kind": "fixture evidence",
                    "reason": "fixture",
                    "evidence": {
                        "identity_quote": quote,
                        "content_quote": quote,
                        "scope": {"value": "Fixture document", "quote": quote},
                        "claim_kind": {"value": "direct", "quote": quote},
                        "source_date": None,
                        "coverage": {
                            label: {"quote": quote}
                            for label in (
                                decision.get("coverage", []) if isinstance(decision, dict) else []
                            )
                        },
                    },
                },
                "cost": 0.01,
                "input_tokens": 10,
                "output_tokens": 5,
            }

    monkeypatch.setattr(SerpApiSeeker, "_build_search_callable", lambda _self: search)
    monkeypatch.setattr("psweep.discovery.network.request_get", download)
    monkeypatch.setattr(DocumentReviewer, "_ensure_client", lambda _self: Client())
    return responses, queries, fetched, grades, decisions, failures


def request(**changes):
    values = {
        "domain": "fixture",
        "targets": [{"label": "Target"}],
        "partition_by": ["label"],
        "queries": ["initial {label}"],
        "fallback_queries": ["fallback {label}"],
        "selection_primary_per_target": 2,
        "robots_policy_mode": "ignore",
        "tos_policy_mode": "ignore",
        "retry_max_attempts": 1,
        "document_review": {
            "document_description": "Evidence for {label}",
            "keep_top": 2,
            "deduplicate_redundant": False,
        },
    }
    values.update(changes)
    return DiscoveryRequest(**values)


def payload(result):
    return json.loads(result.manifest_path.read_text())


def selected(result):
    return [
        record["url"] for record in payload(result)["downloads"] if record.get("review_selected")
    ]


@pytest.mark.parametrize(
    "label,description",
    [
        ("Compressor", "Equipment specifications for {label}"),
        ("County", "Issued permits for {label}"),
        ("Phase", "Infrastructure phase history for {label}"),
    ],
)
def test_zero_initial_documents_automatically_fall_back_and_complete(harness, label, description):
    responses, queries, fetched, grades, decisions, _failures = harness
    responses.update(
        {f"initial {label}": [], f"fallback {label}": ["https://sources.test/evidence.pdf"]}
    )
    decisions["https://sources.test/evidence.pdf"] = 4
    req = request(
        targets=[{"label": label}],
        document_review={
            "document_description": description,
            "keep_top": 1,
            "deduplicate_redundant": False,
        },
    )
    result = DiscoveryEngine().run(req)
    assert queries == [f"initial {label}", f"fallback {label}"]
    assert len(selected(result)) == 1
    assert payload(result)["stage_summaries"]["fallback"]["eligible_targets"] == 1
    assert payload(result)["stage_summaries"]["seeker"]["queries_executed"] == 2
    assert payload(result)["stage_summaries"]["document_review"]["costs"]["llm_calls"] == 1
    assert DiscoveryEngine().run(req).status == "reused"
    assert len(queries) == 2 and len(fetched) == sum(grades.values()) == 1


def test_qualified_initial_evidence_skips_fallback(harness):
    responses, queries, _fetched, _grades, decisions, _failures = harness
    urls = ["https://sources.test/a.pdf", "https://sources.test/b.pdf"]
    responses["initial Target"] = urls
    decisions.update(dict.fromkeys(urls, 4))
    result = DiscoveryEngine().run(request())
    assert queries == ["initial Target"]
    assert selected(result) == urls
    assert payload(result)["stage_summaries"]["fallback"]["eligible_targets"] == 0


def test_repeated_url_is_excluded_before_selection_and_combined_top_n(harness):
    responses, queries, fetched, grades, decisions, _failures = harness
    a, b, c = (f"https://sources.test/{name}.pdf" for name in "abc")
    responses.update({"initial Target": [a], "fallback Target": [a, b, c]})
    decisions.update({a: 3, b: 4, c: 4})
    result = DiscoveryEngine().run(request(selection_primary_per_target=1))
    assert fetched == [a, b]
    assert set(selected(result)) == {a, b}
    assert grades == {a: 1, b: 1}
    costs = payload(result)["stage_summaries"]["document_review"]["costs"]
    assert costs["llm_calls"] == 2 and costs["total_cost_usd"] == pytest.approx(0.02)
    assert queries == ["initial Target", "fallback Target"]


def test_pending_download_failure_does_not_trigger_more_paid_search(harness):
    responses, queries, _fetched, _grades, decisions, failures = harness
    url = "https://sources.test/a.pdf"
    responses["initial Target"] = [url]
    decisions[url] = 4
    failures.add(url)
    result = DiscoveryEngine().run(request())
    assert queries == ["initial Target"]
    assert payload(result)["stage_summaries"]["fallback"]["eligible_targets"] == 0
    assert result.status == "failed"


def test_search_budget_resume_reuses_initial_and_only_searches_pending_fallback(harness):
    responses, queries, fetched, grades, decisions, _failures = harness
    url = "https://sources.test/a.pdf"
    responses.update({"initial Target": [], "fallback Target": [url]})
    decisions[url] = 4
    req = request(max_search_calls=1)
    first = DiscoveryEngine().run(req)
    assert queries == ["initial Target"]
    assert payload(first)["stage_summaries"]["attempt_budget"]["search_calls"] == 1
    assert payload(first)["errors"]
    result = DiscoveryEngine().run(req)
    assert queries == ["initial Target", "fallback Target"]
    assert selected(result) == [url] and fetched == [url] and grades[url] == 1
    DiscoveryEngine().run(req)
    assert len(queries) == 2


def test_review_budget_denial_blocks_fallback_and_resumes_cached_source(harness):
    responses, queries, fetched, grades, decisions, _failures = harness
    a, b = (f"https://sources.test/{name}.pdf" for name in "ab")
    responses["initial Target"] = [a, b]
    decisions.update({a: 4, b: 4})
    req = request(max_review_calls=1)
    first = DiscoveryEngine().run(req)
    assert queries == ["initial Target"]
    assert sum(grades.values()) == 1
    assert any(record.get("review_failed") for record in payload(first)["downloads"])
    second = DiscoveryEngine().run(req)
    assert len(selected(second)) == 2
    assert len(fetched) == 2 and sum(grades.values()) == 2 and len(queries) == 1


def test_duplicate_queries_never_repeat_initial_request(harness):
    responses, queries, _fetched, _grades, _decisions, _failures = harness
    responses["initial Target"] = []
    result = DiscoveryEngine().run(request(fallback_queries=["initial {label}", "initial {label}"]))
    assert queries == ["initial Target"]
    assert payload(result)["stage_summaries"]["fallback"]["queries_executed"] == 0


def test_thousand_target_empty_fallback_is_bounded_and_budget_resumable(harness):
    responses, queries, fetched, grades, _decisions, _failures = harness
    targets = [{"label": f"Target-{index}"} for index in range(1000)]
    for target in targets:
        responses[f"initial {target['label']}"] = []
        responses[f"fallback {target['label']}"] = []
    req = request(targets=targets, max_search_calls=1007)
    first = DiscoveryEngine().run(req)
    assert len(queries) == 1007 and not fetched and not grades
    summary = payload(first)["stage_summaries"]
    assert summary["attempt_budget"]["search_calls"] == 1007
    assert summary["fallback"]["eligible_targets"] == 1000
    assert summary["fallback"]["pending_targets"] == 993
    second = DiscoveryEngine().run(req)
    assert len(queries) == 2000
    assert len(set(queries)) == 2000
    assert payload(second)["stage_summaries"]["attempt_budget"]["search_calls"] == 993
    assert DiscoveryEngine().run(req).status == "empty"
    assert len(queries) == 2000


def test_distinct_count_deduplicates_content_and_final_url():
    records = [
        {
            "status": "downloaded",
            "source": "serpapi_google",
            "review_selected": True,
            "url": f"https://source.test/{i}",
            "final_url": final,
            "content_hash": content,
        }
        for i, (final, content) in enumerate(
            [
                ("https://source.test/a", "same"),
                ("https://source.test/b", "same"),
                ("https://source.test/a", "new"),
            ]
        )
    ]
    assert distinct_accepted_count(records) == 1


def test_final_top_n_and_host_cap_apply_across_both_stages(harness):
    responses, queries, _fetched, grades, decisions, _failures = harness
    a, b, c = "https://same.test/a.pdf", "https://same.test/b.pdf", "https://other.test/c.pdf"
    responses.update({"initial Target": [a], "fallback Target": [b, c]})
    decisions.update({a: 3, b: 4, c: 3})
    result = DiscoveryEngine().run(request(selection_max_per_host_per_target=1))
    assert set(selected(result)) == {b, c}
    assert grades == {a: 1, b: 1, c: 1}
    assert queries == ["initial Target", "fallback Target"]


def test_global_keep_top_prefers_new_evidence_without_dropping_complementary_initial(harness):
    responses, _queries, _fetched, grades, decisions, _failures = harness
    a, b, c = (f"https://sources.test/{name}.pdf" for name in "abc")
    responses.update({"initial Target": [a], "fallback Target": [b, c]})
    decisions.update({a: 3, b: 4, c: 4})
    result = DiscoveryEngine().run(request())
    assert set(selected(result)) == {b, c}
    assert grades == {a: 1, b: 1, c: 1}


def test_remaining_file_cap_persists_stage_then_resumes_only_unfinished_candidates(harness):
    responses, queries, fetched, grades, decisions, _failures = harness
    a, b, c = (f"https://sources.test/{name}.pdf" for name in "abc")
    responses.update({"initial Target": [a], "fallback Target": [b, c]})
    decisions.update({a: 4, b: 4, c: 4})
    req = request(max_files=2)
    first = DiscoveryEngine().run(req)
    assert fetched == [a, b] and len(selected(first)) == 2
    assert payload(first)["lineage"]["blocked_partitions"]
    second = DiscoveryEngine().run(req)
    assert fetched == [a, b, c] and len(selected(second)) == 2
    assert queries == ["initial Target", "fallback Target"]
    assert grades == {a: 1, b: 1, c: 1}
    assert DiscoveryEngine().run(req).status == "reused"


def test_fallback_provider_failure_preserves_prior_curation_and_reuses_good_source(
    harness, tmp_path
):
    responses, queries, fetched, grades, decisions, _failures = harness
    a, b = (f"https://sources.test/{name}.pdf" for name in "ab")
    responses.update(
        {
            "initial Target": [a],
            "fallback Target": requests.ConnectionError("fixture provider unavailable"),
        }
    )
    decisions.update({a: 4, b: 4})
    prior = tmp_path / "outputs/fixture/discovered/curated/by_label/target/prior.pdf"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"previous useful evidence")
    req = request()
    first = DiscoveryEngine().run(req)
    assert prior.read_bytes() == b"previous useful evidence"
    assert len(selected(first)) == 1 and fetched == [a]
    responses["fallback Target"] = [b]
    second = DiscoveryEngine().run(req)
    assert fetched == [a, b] and grades == {a: 1, b: 1}
    assert queries == ["initial Target", "fallback Target", "fallback Target"]
    assert len(selected(second)) == 2


def test_adaptive_dry_run_sends_no_search_or_review(harness):
    _responses, queries, fetched, grades, _decisions, _failures = harness
    result = DiscoveryEngine().run(request(dry_run=True))
    assert not queries and not fetched and not grades
    assert payload(result)["stage_summaries"]["attempt_budget"]["search_calls"] == 0


def test_completed_adaptive_resume_requires_no_search_credentials(harness, monkeypatch):
    responses, queries, _fetched, _grades, decisions, _failures = harness
    url = "https://sources.test/evidence.pdf"
    responses.update({"initial Target": [], "fallback Target": [url]})
    decisions[url] = 4
    req = request(
        document_review={
            "document_description": "Evidence",
            "keep_top": 1,
            "deduplicate_redundant": False,
        }
    )
    DiscoveryEngine().run(req)
    monkeypatch.setattr(
        DiscoveryEngine,
        "_resolve_serpapi_state",
        staticmethod(
            lambda _request: (
                {"provider": "serpapi", "enabled": True, "available": True},
                [
                    {
                        "error_type": "missing_configuration",
                        "message": "no credentials",
                        "retryable": False,
                    }
                ],
                [],
            )
        ),
    )
    result = DiscoveryEngine().run(req)
    assert result.status == "reused" and result.errors == []
    assert queries == ["initial Target", "fallback Target"]


def test_byte_mirrors_do_not_satisfy_coverage_or_get_graded_twice(harness, monkeypatch):
    responses, queries, _fetched, grades, decisions, _failures = harness
    a, mirror, b = (f"https://sources.test/{name}.pdf" for name in ("a", "mirror", "b"))
    responses.update({"initial Target": [a, mirror], "fallback Target": [b]})
    decisions.update({a: 4, mirror: 4, b: 4})

    def download(url, **_kwargs):
        content = a if url == mirror else url
        return FakeResponse(url=url, chunks=[b"%PDF-1.4\n" + content.encode()])

    monkeypatch.setattr("psweep.discovery.network.request_get", download)
    result = DiscoveryEngine().run(request())
    assert set(selected(result)) == {a, b}
    assert grades == {a: 1, b: 1}
    assert queries == ["initial Target", "fallback Target"]


def test_changed_rendered_review_scope_invalidates_completed_target(harness):
    responses, queries, _fetched, grades, decisions, _failures = harness
    url = "https://sources.test/a.pdf"
    responses["initial Target"] = [url]
    decisions[url] = 4
    req = request(
        targets=[{"label": "Target", "address": "First"}],
        document_review={
            "document_description": "Evidence at {address}",
            "keep_top": 1,
            "deduplicate_redundant": False,
        },
    )
    DiscoveryEngine().run(req)
    req.targets[0]["address"] = "Second"
    DiscoveryEngine().run(req)
    assert grades[url] == 2
    assert queries == ["initial Target"]


@pytest.mark.parametrize(
    "policy,message",
    [
        ({"document_description": "Evidence for {missing}"}, "Missing required template token"),
        ({"document_description": "Evidence", "context_fields": ["typo"]}, "context_fields"),
        (
            {"document_description": "Evidence", "context_fields": ["label", "label"]},
            "context_fields",
        ),
    ],
)
def test_direct_engine_bad_review_template_fails_before_calls_or_publication(
    harness, tmp_path, policy, message
):
    _responses, queries, fetched, grades, _decisions, _failures = harness
    req = request(document_review=policy)
    with pytest.raises(ValueError, match=message):
        DiscoveryEngine().run(req)
    assert not queries and not fetched and not grades
    assert not (tmp_path / "outputs").exists()


def test_pending_ambiguous_review_never_schedules_fallback():
    target = {"label": "Target"}
    key = identify_target(target, ["label"])
    records = [
        {"target_id": key, "status": "downloaded", "review_failed": True, "review_retryable": True}
    ]
    assert (
        eligible_targets(
            [target], records, keep_top=2, partition_by=["label"], incomplete_keys=set()
        )
        == []
    )


@pytest.mark.parametrize("coverage", [None, {}, {"history": "Source history"}])
def test_missing_roles_trigger_fallback_without_changing_source_goal(coverage):
    target = {"label": "Target"}
    key = identify_target(target, ["label"])
    records = [
        {
            "target_id": key,
            "status": "downloaded",
            "review_selected": True,
            "review_is_primary": True,
            "url": "https://source.test/a.pdf",
            "review_evidence": {"coverage": {"status": {"quote": "Explicit status"}}},
        }
    ]
    eligible = eligible_targets(
        [target],
        records,
        keep_top=1,
        partition_by=["label"],
        incomplete_keys=set(),
        coverage=coverage,
    )
    assert eligible == ([target] if coverage else [])
    assert eligible_targets(
        [target],
        records,
        keep_top=2,
        partition_by=["label"],
        incomplete_keys=set(),
        coverage=coverage,
    ) == [target]
    assert (
        eligible_targets(
            [target],
            records,
            keep_top=1,
            partition_by=["label"],
            incomplete_keys={key},
            coverage=coverage,
        )
        == []
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "failed"},
        {"review_selected": False},
        {"review_is_primary": False},
        {"review_failed": True},
        {"review_evidence": {"coverage": {"history": {"quote": " "}}}},
        {"review_evidence": {"coverage": {"history": "invented"}}},
    ],
)
def test_coverage_never_uses_failed_secondary_or_unquoted_records(bad):
    record = {
        "status": "downloaded",
        "review_selected": True,
        "review_is_primary": True,
        "review_evidence": {"coverage": {"history": {"quote": "Source history"}}},
    }
    assert covered_labels([record]) == {"history"}
    record.update(bad)
    assert covered_labels([record]) == set()


def test_fallback_budget_visits_first_query_for_every_owner_before_second(harness):
    responses, queries, _fetched, _grades, _decisions, _failures = harness
    targets = [{"label": label} for label in "ABC"]
    for label in "ABC":
        responses[f"initial {label}"] = []
        responses[f"first {label}"] = []
        responses[f"second {label}"] = []
    req = request(
        targets=targets,
        fallback_queries=["first {label}", "second {label}"],  # noqa: RUF027
        max_search_calls=6,
    )
    first = DiscoveryEngine().run(req)
    assert queries == ["initial A", "initial B", "initial C", "first A", "first B", "first C"]
    assert payload(first)["stage_summaries"]["fallback"]["pending_targets"] == 3
    second = DiscoveryEngine().run(req)
    assert queries[-3:] == ["second A", "second B", "second C"]
    assert len(queries) == len(set(queries)) == 9
    assert payload(second)["stage_summaries"]["fallback"]["pending_targets"] == 0


def test_exhausted_missing_coverage_is_completed_not_budget_pending(harness, monkeypatch):
    responses, queries, fetched, _grades, decisions, _failures = harness
    url = "https://sources.test/a.pdf"
    responses.update({"initial Target": [url], "fallback Target": []})
    decisions[url] = 4
    req = request(
        document_review={
            "document_description": "Evidence",
            "keep_top": 1,
            "deduplicate_redundant": False,
            "coverage": {"history": "Explicit history"},
        }
    )
    result = DiscoveryEngine().run(req)
    assert selected(result) == [url]
    fallback = payload(result)["stage_summaries"]["fallback"]
    assert fallback["pending_targets"] == 0 and fallback["low_coverage_targets"] == 1
    assert fallback["targets"][0]["missing_coverage"] == ["history"]
    assert fallback["targets"][0]["source_status"] == "low_coverage"
    assert DiscoveryEngine().run(req).status == "reused"
    assert queries == ["initial Target", "fallback Target"] and fetched == [url]


def test_missing_role_fallback_keeps_complementary_initial_evidence_on_resume(harness):
    responses, queries, fetched, grades, decisions, _failures = harness
    a, b = "https://sources.test/a.pdf", "https://sources.test/b.pdf"
    responses.update({"initial Target": [a], "fallback Target": [b]})
    decisions.update(
        {a: {"grade": 4, "coverage": ["history"]}, b: {"grade": 4, "coverage": ["status"]}}
    )
    req = request(
        max_search_calls=1,
        document_review={
            "document_description": "Evidence",
            "keep_top": 2,
            "deduplicate_redundant": False,
            "coverage": {"history": "Historical source", "status": "Explicit status"},
        },
    )
    first = DiscoveryEngine().run(req)
    assert selected(first) == [a]
    fallback = payload(first)["stage_summaries"]["fallback"]
    assert fallback["pending_targets"] == 1 and fallback["low_coverage_targets"] == 0
    assert fallback["targets"][0]["source_status"] == "pending"
    assert fallback["targets"][0]["pending_reasons"] == ["search_budget"]
    result = DiscoveryEngine().run(req)
    assert set(selected(result)) == {a, b}
    assert payload(result)["stage_summaries"]["fallback"]["targets"][0]["missing_coverage"] == []
    assert fetched == [a, b] and grades == {a: 1, b: 1}
    assert queries == ["initial Target", "fallback Target"]
    assert DiscoveryEngine().run(req).status == "reused"


@pytest.mark.parametrize(
    "coverage",
    [
        [],
        {"": "role"},
        {"role": ""},
        {"role": 123},
        {"role": "Evidence for {missing}"},
    ],
)
def test_direct_coverage_bad_config_fails_before_work(harness, tmp_path, coverage):
    _responses, queries, fetched, grades, _decisions, _failures = harness
    with pytest.raises(ValueError):
        DiscoveryEngine().run(
            request(
                document_review={
                    "document_description": "Evidence",
                    "coverage": coverage,
                }
            )
        )
    assert not queries and not fetched and not grades
    assert not (tmp_path / "outputs").exists()


def test_rendered_coverage_scope_changes_input_digest():
    req = request(
        document_review={
            "document_description": "Evidence",
            "coverage": {
                "role": "Evidence at {address}",
            },
        }
    )
    first = DiscoveryEngine._input_digest(req, {"label": "Target", "address": "First"})
    second = DiscoveryEngine._input_digest(req, {"label": "Target", "address": "Second"})
    assert first != second


def test_finite_file_budget_covers_owners_and_resume_only_fetches_their_pending_ranks(harness):
    responses, queries, fetched, grades, decisions, _failures = harness
    targets = [{"label": label} for label in "AB"]
    urls = {
        label: [f"https://sources.test/{label}-{rank}.pdf" for rank in range(3)] for label in "AB"
    }
    for label, group in urls.items():
        responses[f"initial {label}"] = group
        responses[f"fallback {label}"] = []
        decisions.update(dict.fromkeys(group, 4))
    req = request(
        targets=targets, selection_primary_per_target=3, max_files=2, max_concurrent_downloads=1
    )
    first = DiscoveryEngine().run(req)
    assert fetched == [urls["A"][0], urls["B"][0]]
    assert set(selected(first)) == set(fetched)
    second = DiscoveryEngine().run(req)
    assert fetched == [urls["A"][0], urls["B"][0], urls["A"][1], urls["B"][1]]
    assert len(selected(second)) == 4
    DiscoveryEngine().run(req)
    assert fetched[-2:] == [urls["A"][2], urls["B"][2]]
    assert len(set(fetched)) == 6 and sum(grades.values()) == 6
    assert queries == ["initial A", "initial B"]
    assert DiscoveryEngine().run(req).status == "reused"


def test_initial_query_failure_does_not_block_successful_owner_completion(harness):
    responses, queries, fetched, grades, decisions, _failures = harness
    url = "https://sources.test/a.pdf"
    responses.update({"initial A": [url], "initial B": []})
    decisions[url] = 4
    req = request(
        targets=[{"label": label} for label in "AB"],
        max_search_calls=1,
        document_review={
            "document_description": "Evidence",
            "keep_top": 1,
            "deduplicate_redundant": False,
        },
    )
    first = DiscoveryEngine().run(req)
    assert selected(first) == [url] and queries == ["initial A"]
    checkpoint = json.loads((first.manifest_path.parents[2] / "checkpoint.json").read_text())
    entries = checkpoint["entries"]
    assert entries[identify_target(req.targets[0], req.partition_by)].get("incomplete") is not True
    DiscoveryEngine().run(req)
    assert queries == ["initial A", "initial B"] and fetched == [url] and grades[url] == 1


def test_slot_query_selection_preserves_interleaved_rank_and_duplicate_priority(harness):
    responses, _queries, _fetched, _grades, _decisions, _failures = harness
    urls = [f"https://source.test/{name}.pdf" for name in "abcdef"]
    responses.update({"first Target": urls[:4], "second Target": [urls[3], *urls[4:]]})
    req = request(
        queries=["first {label}", "second {label}"],
        link_prioritization_mode="off",
        selection_primary_per_target=3,
    )
    engine = DiscoveryEngine()
    state = {"enabled": True, "available": True}
    full, _, errors, _, metrics, calls = engine._run_seeker(req, state, [], [])
    assert not errors and calls == 2
    slot, _, errors, _, slot_metrics, calls = engine._run_seeker(
        req, state, [], [], query_slot=True
    )
    assert not errors and calls == 0
    assert [item.url for item in full] == [urls[0], urls[3], urls[1]]
    assert [item.url for item in slot] == [item.url for item in full]
    assert slot_metrics[0]["input_candidates"] == metrics[0]["input_candidates"] == 6


def test_configured_fetch_remains_free_for_following_fallback_stage(harness):
    responses, queries, fetched, grades, decisions, _failures = harness
    configured = "https://sources.test/configured.pdf"
    discovered = "https://sources.test/found.pdf"
    responses.update({"initial Target": [], "fallback Target": [discovered]})
    decisions[discovered] = 4
    req = request(
        targets=[{"label": "Target", "urls": [configured]}],
        max_files=1,
        document_review={
            "document_description": "Evidence",
            "keep_top": 1,
            "deduplicate_redundant": False,
        },
    )
    result = DiscoveryEngine().run(req)
    assert fetched == [configured, discovered]
    assert set(selected(result)) == {configured, discovered}
    assert queries == ["initial Target", "fallback Target"] and grades == {discovered: 1}
    assert payload(result)["stage_summaries"]["fallback"]["pending_targets"] == 0
