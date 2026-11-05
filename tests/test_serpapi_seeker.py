"""SerpApi contracts with narrow transport seams, not a global SDK mock."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from psweep.discovery import DiscoveryRequest
from psweep.discovery.connectors import SerpApiSeeker, SeekerInput
from psweep.discovery.connectors.serpapi_seeker import render_query_template
from psweep.discovery.publication import effective_discovery_fingerprint
from psweep.discovery.urls import canonical_url


@pytest.fixture
def search(monkeypatch):
    """Replace only the remote provider operation."""
    fake = Mock(return_value={"organic_results": []})
    monkeypatch.setattr(SerpApiSeeker, "_search_via_http", fake)
    monkeypatch.setenv("SERPAPI_API_KEY", "synthetic-search-key")
    monkeypatch.setattr(
        "psweep.discovery.connectors.serpapi_seeker.time.sleep", lambda _: None
    )
    return fake


def response(count=2):
    """Build deterministic organic results with complete provider fields."""
    return {
        "organic_results": [
            {
                "position": index + 1,
                "link": f"https://example.org/{index}.pdf",
                "title": f"Document {index}",
                "snippet": f"Evidence {index}",
            }
            for index in range(count)
        ]
    }


@pytest.mark.parametrize("source", ["explicit", "SERPAPI_API_KEY"])
def test_credentials(source, monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    if source == "explicit":
        seeker = SerpApiSeeker(api_key="synthetic-key")
    else:
        monkeypatch.setenv(source, "synthetic-key")
        seeker = SerpApiSeeker()
    assert seeker.api_key == "synthetic-key"


def test_missing_credentials_fail(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="SERPAPI_API_KEY environment variable"):
        SerpApiSeeker()


def test_no_legacy_sdk_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "serpapi", None)
    assert SerpApiSeeker(api_key="synthetic-key").ssl_verify


@pytest.mark.parametrize("count", [0, 2, 3])
def test_normalized_candidates(search, count):
    search.return_value = response(count)
    candidates = SerpApiSeeker().discover(
        SeekerInput(queries=["synthetic documents"])
    )
    assert len(candidates) == count
    for index, candidate in enumerate(candidates):
        assert candidate["url"] == f"https://example.org/{index}.pdf"
        assert candidate["source"] == "serpapi_google"
        assert candidate["title"] == f"Document {index}"
        assert candidate["snippet"] == f"Evidence {index}"
        assert any(
            f"SerpApi result rank {index + 1}" in reason
            for reason in candidate["reasons"]
        )


def test_confirmed_empty_search_preserves_other_queries(search):
    search.side_effect = [
        response(1),
        {
            "search_metadata": {"status": "Success"},
            "search_information": {"organic_results_state": "Fully empty"},
            "error": "Google hasn't returned any results for this query.",
        },
        response(2),
    ]
    seeker = SerpApiSeeker()
    candidates = seeker.discover(
        SeekerInput(queries=["first", "empty", "last"])
    )
    assert len(candidates) == 2
    assert search.call_count == 3
    assert [query["returned"] for query in seeker.query_results] == [1, 0, 2]


@pytest.mark.parametrize(
    "changes",
    [
        {"error": "Invalid API key"},
        {"search_metadata": {"status": "Error"}},
        {"search_metadata": None},
        {"search_information": {}},
        {"search_information": None},
        {"organic_results": [{"link": "https://example.org/doc.pdf"}]},
        {"organic_results": "malformed"},
    ],
)
def test_other_provider_errors_are_not_empty_successes(search, changes):
    search.return_value = {
        "search_metadata": {"status": "Success"},
        "search_information": {"organic_results_state": "Fully empty"},
        "error": "Google hasn't returned any results for this query.",
        **changes,
    }
    with pytest.raises(RuntimeError, match="SerpApi search failed"):
        SerpApiSeeker().discover(SeekerInput(queries=["q"]))


@pytest.mark.parametrize("query", ["", "   "])
def test_empty_query_fails_without_call(search, query):
    with pytest.raises(ValueError, match="could be rendered"):
        SerpApiSeeker().discover(SeekerInput(queries=[query]))
    search.assert_not_called()


def test_invalid_result_urls_are_excluded(search):
    search.return_value = response(1)
    search.return_value["organic_results"].extend(
        [
            {"link": None},
            {"link": "ftp://example.org/document"},
        ]
    )
    assert len(SerpApiSeeker().discover(SeekerInput(queries=["q"]))) == 1


def test_provider_failure_is_explicit_and_redacts_credentials(search):
    search.side_effect = RuntimeError(
        "request failed /search?q=example&api_key=synthetic-search-key&output=json"
    )
    with pytest.raises(RuntimeError, match="SerpApi search failed") as caught:
        SerpApiSeeker().discover(SeekerInput(queries=["q"]))
    assert "synthetic-search-key" not in str(caught.value)
    assert "api_key=<redacted>" in str(caught.value)


def test_transient_failure_retries(search):
    search.side_effect = [RuntimeError("connection timeout"), response()]
    assert len(SerpApiSeeker().discover(SeekerInput(queries=["q"]))) == 2
    assert search.call_count == 2


def test_extra_parameters_and_template_context(search):
    SerpApiSeeker().discover(
        SeekerInput(
            queries=["{jurisdiction} {state} ordinance {known_id?}"],
            context={"jurisdiction": "Example County", "state": "CO"},
            params={"gl": "us", "hl": "en"},
        )
    )
    params = search.call_args.args[0]
    assert params["q"] == "Example County CO ordinance"
    assert params["gl"] == "us"
    assert params["hl"] == "en"
    assert "template_context" not in params


def test_templates_missing_required_tokens_are_skipped(search):
    SerpApiSeeker().discover(
        SeekerInput(
            queries=["{jurisdiction} {missing}", "{jurisdiction} documents"],
            context={"jurisdiction": "Example County"},
        )
    )
    assert [c.args[0]["q"] for c in search.call_args_list] == ["Example County documents"]


def test_required_template_token_fails():
    with pytest.raises(ValueError, match="Missing required template token"):
        render_query_template(
            "{jurisdiction} {state}", {"jurisdiction": "Example"}
        )


def test_optional_template_token_is_removed():
    assert (
        render_query_template(
            "{jurisdiction} {state} {known_id?}",
            {"jurisdiction": "Example", "state": "CO"},
        )
        == "Example CO"
    )


def test_search_params():
    assert SerpApiSeeker._build_search_params(
        SeekerInput(queries=["q"], max_results=20, params={"location": "Denver, CO"}),
        "q",
    ) == {"q": "q", "engine": "google", "num": 20, "location": "Denver, CO"}


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("serpapi", True),
        ("serpapi_google", True),
        ("SERPAPI", True),
        ("crawlee", False),
        ("direct_url", False),
    ],
)
def test_supported_providers(provider, expected):
    assert (
        SerpApiSeeker(api_key="synthetic").supports_provider(provider)
        is expected
    )


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/doc.pdf", True),
        ("http://example.com/doc.pdf", True),
        ("HTTPS://EXAMPLE.COM", True),
        ("", False),
        (None, False),
        ("ftp://example.com", False),
        ("example.com", False),
        (123, False),
    ],
)
def test_url_validation(url, expected):
    assert SerpApiSeeker._is_valid_url(url) is expected


def test_verified_transport_is_the_default(monkeypatch):
    for key in ("SERPAPI_SSL_VERIFY", "PSWEEP_SSL_VERIFY"):
        monkeypatch.delenv(key, raising=False)
    calls = []

    def get(url, **options):
        calls.append((url, options))
        return SimpleNamespace(
            url=url,
            status_code=200,
            headers={},
            close=lambda: None,
            raise_for_status=lambda: None,
            json=response,
        )

    monkeypatch.setattr("psweep.discovery.network.request_get", get)
    assert (
        len(
            SerpApiSeeker(api_key="synthetic").discover(SeekerInput(queries=["q"]))
        )
        == 2
    )
    assert parse_qs(urlsplit(calls[0][0]).query)["api_key"] == ["synthetic"]
    assert calls[0][1]["verify"] is True
    assert calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("setting", ["false", "0", "off"])
def test_insecure_settings_are_rejected_in_protected_mode(monkeypatch, setting):
    monkeypatch.setenv("PSWEEP_SECURITY_MODE", "protected")
    monkeypatch.setenv("SERPAPI_SSL_VERIFY", setting)
    with pytest.raises(
        ValueError, match="Protected mode requires TLS verification"
    ):
        SerpApiSeeker(api_key="synthetic")


def test_cache_reuses_results_across_instances(search, tmp_path):
    search.return_value = response()
    cache = str(tmp_path / "cache")
    request = SeekerInput(queries=["q"])
    first = SerpApiSeeker(cache_dir=cache).discover(request)
    search.assert_called_once()
    search.reset_mock()
    second = SerpApiSeeker(cache_dir=cache).discover(request)
    assert second == first
    search.assert_not_called()


def test_no_cache_means_two_remote_calls(search):
    seeker = SerpApiSeeker()
    for _ in range(2):
        seeker.discover(SeekerInput(queries=["q"]))
    assert search.call_count == 2


def test_cache_refresh_fetches_and_updates_persistent_results(
    search, tmp_path
):
    cache = str(tmp_path / "cache")
    request = SeekerInput(queries=["q"])
    search.return_value = response(1)
    SerpApiSeeker(cache_dir=cache).discover(request)
    search.return_value = response(2)
    assert (
        len(
            SerpApiSeeker(cache_dir=cache, cache_refresh=True).discover(
                request
            )
        )
        == 2
    )
    search.reset_mock()
    assert len(SerpApiSeeker(cache_dir=cache).discover(request)) == 2
    search.assert_not_called()


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://a.gov/p?srsltid=AfmB", "https://a.gov/p"),
        ("https://a.gov/p?utm_source=x&id=7&gclid=1", "https://a.gov/p?id=7"),
        ("https://a.gov/p?FBCLID=1&Utm_Medium=m", "https://a.gov/p"),
        ("https://a.gov/bids.aspx?bidId=12", "https://a.gov/bids.aspx?bidId=12"),
        ("https://a.gov/l?5a55ec85_page=2&v=3", "https://a.gov/l?5a55ec85_page=2&v=3"),
        ("https://a.gov/d?fileID=9&q=a%20b+c", "https://a.gov/d?fileID=9&q=a%20b+c"),
        ("https://a.gov/d?fileID=9&srsltid=x#top", "https://a.gov/d?fileID=9#top"),
        ("https://a.gov/d?utm_id", "https://a.gov/d"),
        ("https://a.gov/p", "https://a.gov/p"),
    ],
)
def test_canonical_url_strips_only_tracking_parameters(url, expected):
    assert canonical_url(url) == expected


def test_tracking_variants_dedupe_across_queries(search):
    search.side_effect = [
        {"organic_results": [{"link": "https://a.gov/solar?srsltid=one"}]},
        {"organic_results": [
            {"link": "https://a.gov/solar?srsltid=two"},
            {"link": "https://a.gov/bids.aspx?bidId=4&utm_source=g"},
        ]},
    ]
    candidates = SerpApiSeeker().discover(SeekerInput(queries=["first", "second"]))
    assert [c["url"] for c in candidates] == [
        "https://a.gov/solar", "https://a.gov/bids.aspx?bidId=4",
    ]


def test_queries_interleave_by_rank_so_a_capped_selection_sees_each_query(search):
    def page(prefix, count):
        return {"organic_results": [
            {"link": f"https://{prefix}.gov/{rank}"} for rank in range(count)
        ]}

    search.side_effect = [page("a", 3), page("b", 2), page("c", 1)]
    candidates = SerpApiSeeker().discover(
        SeekerInput(queries=["first", "second", "third"])
    )
    assert [c["url"] for c in candidates] == [
        "https://a.gov/0", "https://b.gov/0", "https://c.gov/0",
        "https://a.gov/1", "https://b.gov/1",
        "https://a.gov/2",
    ]


@pytest.mark.parametrize(
    "params,organic,news",
    [
        (None, "serpapi_google", "serpapi_google_news"),
        ({"engine": "bing"}, "serpapi_bing", "serpapi_bing_news"),
        ({"engine": "google_news"}, "serpapi_google_news", "serpapi_google_news"),
    ],
)
def test_source_names_the_configured_engine(search, params, organic, news):
    search.return_value = {
        "organic_results": [{"link": "https://a.gov/organic"}],
        "news_results": [{"link": "https://a.gov/news", "date": "1 day ago"}],
    }
    candidates = SerpApiSeeker().discover(SeekerInput(queries=["q"], params=params))
    assert [c["source"] for c in candidates] == [organic, news]


@pytest.mark.parametrize(
    "change,invalidates",
    [
        ({"seeker_params": {"udm": "14"}}, True),
        ({"seeker_max_results": 3}, True),
        ({"seeker_cache": True}, False),
    ],
)
def test_search_settings_decide_resume(change, invalidates):
    """Completed targets re-run when search settings change, not on cache toggles."""
    base = {"domain": "d", "queries": ["{label}"], "targets": [{"label": "a"}]}
    before = effective_discovery_fingerprint(DiscoveryRequest(**base))
    after = effective_discovery_fingerprint(DiscoveryRequest(**base, **change))
    assert (before != after) is invalidates
