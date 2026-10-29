"""Seedless discovery budgets, format eligibility, and useful defaults."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from psweep.config import (
    RuntimeConfigError,
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.discovery import (
    CandidateScore,
    DiscoveryCandidate,
    DiscoveryEngine,
    DiscoveryRequest,
)
from psweep.discovery.connectors.base import SeekerInput
from psweep.discovery.connectors.serpapi_seeker import SerpApiSeeker
from psweep.discovery.candidate_selector import CandidateSelector
from psweep.discovery.link_prioritizer import LinkPrioritizer
from psweep.discovery.request_inputs import normalize_discover_inputs
from discovery_helpers import make_candidate, FakeResponse
import yaml


def _write_config(tmp_path, discovery):
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump({"discovery": discovery}), encoding="utf-8")
    return path


def _seeker(monkeypatch, response, **kwargs):
    monkeypatch.setattr(SerpApiSeeker, "_ensure_prepared", lambda _self: None)
    search = Mock(return_value=response)
    monkeypatch.setattr(
        SerpApiSeeker, "_build_search_callable", lambda _self: search
    )
    return SerpApiSeeker(
        api_key="fixture-key", retry_max_attempts=1, **kwargs
    ), search


def _response(count=8):
    return {
        "organic_results": [
            {"link": f"https://example.org/{i}.pdf", "title": f"Document {i}"}
            for i in range(count)
        ]
    }


@pytest.mark.parametrize("cached", [False, True])
def test_search_result_cap_applies_to_live_and_cache(
    monkeypatch, tmp_path, cached
):
    """Oversized provider pages cannot exceed the local per-query cap."""
    seeker, search = _seeker(monkeypatch, _response(), cache_dir=str(tmp_path))
    inputs = SeekerInput(query="documents", max_results=3)
    if cached:
        seeker._cache_put(seeker._build_search_params(inputs), _response())
    results = seeker.discover(inputs)
    assert [r["url"] for r in results] == [
        f"https://example.org/{i}.pdf" for i in range(3)
    ]
    assert search.call_count == (0 if cached else 1)


def test_query_caps_are_independent_and_duplicates_do_not_use_budget(
    monkeypatch,
):
    """Cap each query after deduplication, not the union of all queries."""
    seeker, search = _seeker(monkeypatch, {})
    search.side_effect = [
        {
            "organic_results": [
                {"link": "https://example.org/a.pdf"},
                {"link": "https://example.org/a.pdf"},
                {"link": "https://example.org/b.pdf"},
                {"link": "https://example.org/ignored.pdf"},
            ]
        },
        {
            "organic_results": [
                {"link": "https://example.org/c.pdf"},
                {"link": "https://example.org/d.pdf"},
                {"link": "https://example.org/ignored2.pdf"},
            ]
        },
    ]
    results = seeker.discover(
        SeekerInput(
            query="",
            max_results=2,
            extra_params={"query_templates": ["first", "second"]},
        )
    )
    assert [r["url"] for r in results] == [
        f"https://example.org/{c}.pdf" for c in "abcd"
    ]
    assert search.call_count == 2


def test_news_results_obey_the_same_cap(monkeypatch):
    """News and organic responses share one local result budget."""
    response = {
        "news_results": [
            {"link": f"https://example.org/news/{i}"} for i in range(8)
        ]
    }
    seeker, _ = _seeker(monkeypatch, response)
    assert len(seeker.discover(SeekerInput(query="news", max_results=3))) == 3


@pytest.mark.parametrize("key", ["q", "num", "api_key"])
def test_reserved_search_params_rejected_in_loader_and_connector(
    tmp_path, key
):
    """Provider passthrough cannot override resolved query, limit, or key."""
    config = _write_config(
        tmp_path,
        {
            "queries": ["documents"],
            "search": {
                "serpapi_params": {key: "override"},
            },
        },
    )
    with pytest.raises(RuntimeConfigError, match="reserved"):
        load_runtime_config_file(config)
    with pytest.raises(ValueError, match="reserved"):
        SerpApiSeeker._build_search_params(
            SeekerInput(
                query="documents",
                extra_params={"serpapi_params": {key: "override"}},
            )
        )


@pytest.mark.parametrize(
    "response",
    [
        {"error": "search failed fixture-key"},
        [],
        {"organic_results": "bad"},
    ],
)
def test_error_responses_are_not_cached(monkeypatch, tmp_path, response):
    """HTTP success does not make a provider error a valid cache result."""
    seeker, _ = _seeker(monkeypatch, response, cache_dir=str(tmp_path))
    with pytest.raises(RuntimeError) as caught:
        seeker.discover(SeekerInput(query="documents", max_results=3))
    assert "fixture-key" not in str(caught.value)
    assert not list(tmp_path.glob("*.json"))


def test_bad_cached_response_fails_as_search_error(monkeypatch, tmp_path):
    """Malformed cache payloads do not silently become empty searches."""
    seeker, search = _seeker(monkeypatch, _response(), cache_dir=str(tmp_path))
    inputs = SeekerInput(query="documents", max_results=3)
    seeker._cache_path(seeker._build_search_params(inputs)).write_text("[]")
    with pytest.raises(RuntimeError):
        seeker.discover(inputs)
    search.assert_not_called()


@pytest.mark.parametrize(
    "discovery",
    [
        {"queries": ["documents"]},
        {"query": "documents"},
        {"query_templates": ["documents"]},
        {"targets": [{"query": "documents"}]},
    ],
)
def test_search_intent_needs_no_enablement_knob(discovery):
    """Query-driven configurations enable the default search provider."""
    resolved = resolve_command_config(
        command="discover",
        cli_values={},
        config_data={"discovery": discovery},
        strict=True,
    )
    assert normalize_discover_inputs(resolved)["enable_serpapi"] is True


@pytest.mark.parametrize(
    "discovery",
    [
        {"queries": ["documents"], "enable_serpapi": False},
        {"queries": ["documents"], "search": {"enabled": False}},
        {
            "queries": ["documents"],
            "search": {"provider": "serpapi", "enabled": False},
        },
        {"seeds": ["https://example.org/doc.txt"]},
        {"queries": []},
    ],
)
def test_explicit_disable_and_seed_only_do_not_start_search(discovery):
    """Auto-enablement respects opt-outs and seeds-only use."""
    resolved = resolve_command_config(
        command="discover",
        cli_values={},
        config_data={"discovery": discovery},
        strict=True,
    )
    assert normalize_discover_inputs(resolved)["enable_serpapi"] is False


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/document.html",
        "https://example.org/article",
        "https://example.org/guide.htm",
        "https://example.org/download?id=1",
    ],
)
def test_supported_web_documents_do_not_need_filter_disabled(url):
    """HTML and extensionless web documents are provisionally eligible."""
    candidate = make_candidate(url)
    selected, _ = CandidateSelector(exclude_draft=False).select(
        [[candidate]], 1
    )
    assert selected == [candidate]


@pytest.mark.parametrize(
    "url", ["https://example.org/a.exe", "https://example.org/a.png"]
)
def test_unsupported_files_stay_ineligible(url):
    """Web-document support does not accept arbitrary binary formats."""
    selected, _ = CandidateSelector(exclude_draft=False).select(
        [[make_candidate(url)]], 1
    )
    assert selected == []


def test_candidate_metadata_survives_ranking_and_routing():
    """Changes to scores/reasons retain search evidence and target context."""
    candidate = DiscoveryCandidate(
        url="https://example.org/doc.pdf",
        source="serpapi_google",
        score=CandidateScore(),
        title="Document title",
        snippet="Source evidence",
        target_metadata={"label": "target"},
    )
    ranked, _ = LinkPrioritizer(top_k=None).prioritize([candidate])
    copied = DiscoveryEngine._copy_candidate_with_reason(ranked[0], "routed")
    assert copied.title == candidate.title
    assert copied.snippet == candidate.snippet
    assert copied.target_metadata == candidate.target_metadata
    assert copied.to_dict()["title"] == candidate.title


def _request(tmp_path, **kwargs):
    return DiscoveryRequest(
        domain="fixture",
        seed_urls=[],
        query="documents",
        enable_serpapi=True,
        output_documents=tmp_path / "documents",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        **kwargs,
    )


def test_source_scope_precedes_per_target_selection(monkeypatch, tmp_path):
    """Off-domain first results cannot crowd out a permitted later result."""
    response = {
        "organic_results": [
            {"link": "https://outside.org/first.pdf"},
            {"link": "https://example.org/good.pdf"},
            {"link": "https://example.org/article.html"},
        ]
    }
    _seeker(monkeypatch, response)
    request = _request(
        tmp_path,
        allowed_domains=["example.org"],
        include_url_patterns=[r"\.pdf$"],
    )
    candidates, _, errors, _, metrics, _ = DiscoveryEngine._run_seeker(
        request,
        {"enabled": True, "available": True},
        [],
        [],
    )
    assert not errors
    assert [c.url for c in candidates] == ["https://example.org/good.pdf"]
    assert metrics[0]["scope_rejected"] == 2


def test_redirects_cannot_escape_requested_format(tmp_path):
    """Explicit PDF scope remains effective at the final download URL."""
    request = _request(tmp_path, include_url_patterns=[r"\.pdf$"])
    allowed, reason = DiscoveryEngine._passes_final_url_selection_guard(
        final_url="https://example.org/article.html",
        request=request,
    )
    assert allowed is False
    assert reason == "final_url_outside_include_patterns"


def test_automatic_recovery_is_opt_in(monkeypatch, tmp_path):
    """Minimal runs do not initialize browsers or source adapters."""
    request = _request(tmp_path)
    record = {"status": "downloaded", "path": str(tmp_path / "shell.html")}
    with patch(
        "psweep.discovery.engine.DiscoveryEngine._open_browser_for_download"
    ) as browser:
        assert DiscoveryEngine()._escalate_js_shells_to_browser(
            [record], [], request
        )[0] == [record]
        browser.assert_not_called()
    with patch(
        "psweep.discovery.code_host_adapters.resolve_adapter"
    ) as adapter:
        assert DiscoveryEngine()._resolve_code_host_shells(
            [record], [], request
        )[0] == [record]
        adapter.assert_not_called()


def test_pdf_named_html_is_not_a_strict_pdf(monkeypatch, tmp_path):
    """A PDF suffix cannot disguise an HTML response under strict scope."""
    url = "https://example.org/document.pdf"
    response = FakeResponse(
        url=url,
        content_type="text/html",
        chunks=[b"<html>not PDF</html>"],
    )
    monkeypatch.setattr("requests.get", lambda *_args, **_kwargs: response)
    request = _request(
        tmp_path,
        include_url_patterns=[r"\.pdf$"],
        robots_policy_mode="ignore",
        tos_policy_mode="ignore",
    )
    _, record, error, saved, _ = DiscoveryEngine()._download_single_candidate(
        idx=0,
        candidate=make_candidate(url),
        request=request,
        documents_dir=tmp_path / "documents",
        ssl_verify=False,
        retry_policy={
            "max_attempts": 1,
            "initial_backoff_seconds": 0,
            "max_backoff_seconds": 0,
        },
        rate_limiter=None,
    )
    assert error is None and saved is False
    assert record["reason"] == "content_type_outside_include_patterns"
    assert not (tmp_path / "documents").exists()


@pytest.mark.parametrize(
    "content_type, expected",
    [
        ("text/html", ".html"),
        ("application/pdf", ".pdf"),
        ("application/zip", None),
    ],
)
def test_extensionless_download_is_checked_by_actual_mime(
    monkeypatch,
    tmp_path,
    content_type,
    expected,
):
    """Ambiguous URLs use the existing GET, not another HEAD/probe call."""
    url = "https://example.org/download?id=1"
    get = Mock(return_value=FakeResponse(url=url, content_type=content_type))
    monkeypatch.setattr("requests.get", get)
    request = _request(
        tmp_path, robots_policy_mode="ignore", tos_policy_mode="ignore"
    )
    _, record, error, saved, _ = DiscoveryEngine()._download_single_candidate(
        idx=0,
        candidate=make_candidate(url),
        request=request,
        documents_dir=tmp_path / "documents",
        ssl_verify=False,
        retry_policy={
            "max_attempts": 1,
            "initial_backoff_seconds": 0,
            "max_backoff_seconds": 0,
        },
        rate_limiter=None,
    )
    assert error is None
    assert get.call_count == 1
    assert saved is (expected is not None)
    if expected:
        assert record["path"].endswith(expected)
    else:
        assert record["status"] == "skipped_unsupported_type"


def test_filtered_search_reports_rules_not_missing_credentials(tmp_path):
    """Successful searches filtered to zero give actionable scope feedback."""
    _, notes = DiscoveryEngine._append_discovery_gap_diagnostics(
        request=_request(tmp_path),
        candidates=[],
        normalized_seed_urls=[],
        errors=[],
        notes=[],
        discovered_count=3,
    )
    assert "passed configured source/format/selection rules" in notes[-1]
    assert "valid key" not in notes[-1]


def test_minimal_template_is_executable_without_advanced_settings():
    """The shipped starter needs queries, not a budget/recovery checklist."""
    config = load_runtime_config_file(Path("config/TEMPLATE.yaml"))
    assert set(config["discovery"]) == {"queries"}
    resolved = resolve_command_config(
        command="discover",
        cli_values={},
        config_data=config,
        strict=True,
    )
    assert normalize_discover_inputs(resolved)["enable_serpapi"] is True


@pytest.mark.parametrize(
    "url, pattern",
    [
        ("https://example.org/article.php", r"/article\.php$"),
        ("https://example.org/guide.htm", r"\.htm$"),
    ],
)
def test_html_source_path_rules_are_not_content_type_rules(
    monkeypatch,
    tmp_path,
    url,
    pattern,
):
    """Arbitrary source-path scope is applied to real URLs only."""
    monkeypatch.setattr(
        "requests.get",
        Mock(
            return_value=FakeResponse(
                url=url,
                content_type="text/html",
                chunks=[b"<html>article</html>"],
            )
        ),
    )
    request = _request(
        tmp_path,
        include_url_patterns=[pattern],
        robots_policy_mode="ignore",
        tos_policy_mode="ignore",
    )
    _, record, error, saved, _ = DiscoveryEngine()._download_single_candidate(
        idx=0,
        candidate=make_candidate(url),
        request=request,
        documents_dir=tmp_path / "documents",
        ssl_verify=False,
        retry_policy={
            "max_attempts": 1,
            "initial_backoff_seconds": 0,
            "max_backoff_seconds": 0,
        },
        rate_limiter=None,
    )
    assert error is None and saved is True
    assert record["path"].endswith(".html")


def test_excluded_target_queries_do_not_enable_search():
    """Target limits apply before inferring search work."""
    normalized = normalize_discover_inputs(
        {
            "seed_urls": ["https://example.org/document.txt"],
            "targets": [
                {"label": "first"},
                {"label": "unused", "query": "unused query"},
            ],
            "target_limit": 1,
        }
    )
    assert normalized["targets"] == [{"label": "first"}]
    assert normalized["enable_serpapi"] is False


def test_browser_retry_carries_explicit_opt_in(monkeypatch, tmp_path):
    """An explicitly enabled retry crawler receives its browser policy."""
    shell = tmp_path / "shell.html"
    shell.write_text("<html>shell</html>")
    request = _request(
        tmp_path,
        browser_escalation={"enabled": True},
        document_classifier={"nice_to_have_keywords": ["documents"]},
        link_prioritization_domain_scores={"example.org": 0.2},
    )
    discover = Mock(return_value=[])
    monkeypatch.setattr(
        "psweep.discovery.connectors.digger.HttpDiggerConnector.discover",
        discover,
    )
    records = [
        {
            "status": "downloaded",
            "path": str(shell),
            "url": "https://example.org/code",
            "target_metadata": {"label": "target"},
        }
    ]
    DiscoveryEngine()._retry_js_shells_with_digger(
        request=request,
        download_records=records,
        notes=[],
        documents_dir=tmp_path / "documents",
        review_cfg={},
    )
    assert (
        discover.call_args.args[0].extra_params["browser_escalation"] is True
    )
