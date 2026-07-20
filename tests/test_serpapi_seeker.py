"""Integration tests for SerpApi seeker connector with mocked payloads."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from streamline_extract.acquisition.connectors import SerpApiSeeker, SeekerInput


class TestSerpApiSeekerConnector:
    """Test SerpApi seeker with mocked SerpApi payloads."""

    @pytest.fixture(autouse=True)
    def _force_ssl_verify_true_for_mocked_client_tests(self, monkeypatch):
        """Keep existing mocked Client-based tests deterministic unless overridden."""
        monkeypatch.setenv("SERPAPI_SSL_VERIFY", "true")

    @pytest.fixture
    def mock_serpapi_response_geothermal(self) -> dict:
        """Mock SerpApi response for a geothermal ordinance query."""
        return {
            "organic_results": [
                {
                    "position": 1,
                    "title": "Chaffee County Code Chapter 16.16",
                    "link": "https://www.chaffeecounty.org/documents/53007.pdf",
                    "snippet": "Oil and Gas and Geothermal Resources ordinance.",
                },
                {
                    "position": 2,
                    "title": "Title 16 - Land Use Code",
                    "link": "https://www.chaffeecounty.org/land-use-code/chapter-16",
                    "snippet": "Chapter 16 establishes land use zoning and development standards...",
                },
                {
                    "position": 3,
                    "title": "Geothermal Resources Permit",
                    "link": "https://example.com/permit",
                    "snippet": "Apply for geothermal resource extraction permit.",
                },
            ]
        }

    @pytest.fixture
    def mock_serpapi_response_tariff(self) -> dict:
        """Mock SerpApi response for a tariff query."""
        return {
            "organic_results": [
                {
                    "position": 1,
                    "title": "Schedule A - Residential Rates",
                    "link": "https://www.utility.com/schedules/schedule-a.pdf",
                    "snippet": "Effective rates for residential customers.",
                },
                {
                    "position": 2,
                    "title": "Tariff Book 2026",
                    "link": "https://www.utility.com/tariffs/book-2026.pdf",
                    "snippet": "Complete tariff schedule effective January 2026.",
                },
            ]
        }

    @pytest.fixture
    def mock_serpapi_response_empty(self) -> dict:
        """Mock SerpApi response with no organic results."""
        return {"organic_results": []}

    def test_seeker_initialization_with_api_key(self):
        """Test seeker initializes with explicit API key."""
        with patch("serpapi.Client"):
            seeker = SerpApiSeeker(api_key="explicit-key")
            assert seeker.api_key == "explicit-key"

    def test_seeker_initialization_from_env(self):
        """Test seeker reads API key from environment."""
        with patch("serpapi.Client"):
            with patch.dict(os.environ, {"SERPAPI_API_KEY": "env-key"}):
                seeker = SerpApiSeeker()
                assert seeker.api_key == "env-key"

    def test_seeker_initialization_from_alt_env_key(self):
        """Test seeker reads API key from SERPAPI_KEY fallback."""
        with patch("serpapi.Client"):
            with patch.dict(os.environ, {"SERPAPI_KEY": "alt-env-key"}, clear=True):
                seeker = SerpApiSeeker()
                assert seeker.api_key == "alt-env-key"

    def test_seeker_initialization_fails_without_api_key(self):
        """Test seeker raises error when API key is missing."""
        with patch("serpapi.Client"):
            with patch.dict(os.environ, {}, clear=True):
                with pytest.raises(ValueError, match="SERPAPI_API_KEY or SERPAPI_KEY"):
                    SerpApiSeeker()

    def test_seeker_initialization_fails_without_serpapi_dependency(self):
        """Test seeker raises error when serpapi package is not installed."""
        # Mock import to fail
        def mock_import(name, *args, **kwargs):
            if name == "serpapi":
                raise ImportError("No module named 'serpapi'")
            return __import__(name, *args, **kwargs)

        with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}):
            with patch("builtins.__import__", side_effect=mock_import):
                with pytest.raises(RuntimeError, match="SerpApi seeker requires"):
                    SerpApiSeeker()

    def test_discover_geothermal_query(self, mock_serpapi_response_geothermal):
        """Test discover normalizes geothermal search results."""
        with patch("serpapi.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.search.return_value = mock_serpapi_response_geothermal
            mock_client_class.return_value = mock_client

            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(
                    query="Chaffee County Colorado geothermal ordinance",
                    max_results=10,
                )
                candidates = seeker.discover(seeker_input)

                assert len(candidates) == 3
                assert candidates[0]["url"] == "https://www.chaffeecounty.org/documents/53007.pdf"
                assert candidates[0]["source"] == "serpapi_google"
                assert candidates[0]["title"] == "Chaffee County Code Chapter 16.16"
                assert any("SerpApi result rank 1" in reason for reason in candidates[0]["reasons"])

    def test_discover_tariff_query(self, mock_serpapi_response_tariff):
        """Test discover works for tariff queries."""
        with patch("serpapi.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.search.return_value = mock_serpapi_response_tariff
            mock_client_class.return_value = mock_client

            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(
                    query="residential rate tariff pdf",
                    max_results=5,
                )
                candidates = seeker.discover(seeker_input)

                assert len(candidates) == 2
                assert all(c["source"] == "serpapi_google" for c in candidates)

    def test_discover_empty_results(self, mock_serpapi_response_empty):
        """Test discover returns empty list when no results found."""
        with patch("serpapi.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.search.return_value = mock_serpapi_response_empty
            mock_client_class.return_value = mock_client

            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(query="nonexistent query")
                candidates = seeker.discover(seeker_input)

                assert len(candidates) == 0

    def test_discover_uses_http_fallback_when_ssl_verify_disabled(
        self,
        mock_serpapi_response_geothermal,
    ):
        """When SSL verification is disabled, seeker uses direct HTTP with verify=False."""
        with patch("requests.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = mock_serpapi_response_geothermal
            mock_get.return_value = mock_response

            with patch.dict(
                os.environ,
                {
                    "SERPAPI_API_KEY": "test-api-key",
                    "SERPAPI_SSL_VERIFY": "false",
                },
                clear=True,
            ):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(
                    query="Chaffee County Colorado geothermal ordinance",
                    max_results=10,
                )
                candidates = seeker.discover(seeker_input)

                assert len(candidates) == 3
                assert candidates[0]["url"] == "https://www.chaffeecounty.org/documents/53007.pdf"
                call_kwargs = mock_get.call_args.kwargs
                assert call_kwargs["verify"] is False

    def test_discover_defaults_to_http_fallback_when_ssl_verify_unset(
        self,
        mock_serpapi_response_geothermal,
    ):
        """When SSL verify env vars are unset, default behavior uses verify=False."""
        with patch("requests.get") as mock_get:
            mock_response = MagicMock()
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = mock_serpapi_response_geothermal
            mock_get.return_value = mock_response

            with patch.dict(
                os.environ,
                {
                    "SERPAPI_API_KEY": "test-api-key",
                },
                clear=True,
            ):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(
                    query="Chaffee County Colorado geothermal ordinance",
                    max_results=10,
                )
                candidates = seeker.discover(seeker_input)

                assert len(candidates) == 3
                call_kwargs = mock_get.call_args.kwargs
                assert call_kwargs["verify"] is False

    def test_discover_filters_invalid_urls(self):
        """Test discover skips results with invalid URLs."""
        mock_response = {
            "organic_results": [
                {
                    "position": 1,
                    "title": "Valid result",
                    "link": "https://example.com/doc.pdf",
                    "snippet": "Valid",
                },
                {
                    "position": 2,
                    "title": "Missing URL",
                    "link": None,
                    "snippet": "Invalid",
                },
                {
                    "position": 3,
                    "title": "Invalid protocol",
                    "link": "ftp://invalid.com/doc.pdf",
                    "snippet": "Invalid protocol",
                },
            ]
        }
        with patch("serpapi.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.search.return_value = mock_response
            mock_client_class.return_value = mock_client

            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(query="test query")
                candidates = seeker.discover(seeker_input)

                assert len(candidates) == 1
                assert candidates[0]["url"] == "https://example.com/doc.pdf"

    def test_discover_empty_query_raises_error(self):
        """Test discover raises error for empty query."""
        with patch("serpapi.Client"):
            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(query="", max_results=10)

                with pytest.raises(ValueError, match="cannot be resolved"):
                    seeker.discover(seeker_input)

    def test_discover_whitespace_query_raises_error(self):
        """Test discover raises error for whitespace-only query."""
        with patch("serpapi.Client"):
            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(query="   ", max_results=10)

                with pytest.raises(ValueError, match="cannot be resolved"):
                    seeker.discover(seeker_input)

    def test_discover_api_failure_raises_error(self):
        """Test discover raises error when SerpApi call fails."""
        with patch("serpapi.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.search.side_effect = RuntimeError("API rate limited")
            mock_client_class.return_value = mock_client

            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(query="test query")
                with pytest.raises(RuntimeError, match="SerpApi search failed"):
                    seeker.discover(seeker_input)

    def test_discover_api_failure_redacts_api_key_from_error_message(self):
        """SerpApi failures should not leak api_key values in surfaced errors."""
        leaked_message = (
            "HTTPSConnectionPool(host='serpapi.com', port=443): Max retries exceeded "
            "with url: /search?q=test&api_key=test-api-key&output=json"
        )
        with patch("serpapi.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.search.side_effect = RuntimeError(leaked_message)
            mock_client_class.return_value = mock_client

            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(query="test query")

                with pytest.raises(RuntimeError) as exc_info:
                    seeker.discover(seeker_input)

                message = str(exc_info.value)
                assert "test-api-key" not in message
                assert "api_key=<redacted>" in message

    def test_discover_retries_transient_error_then_succeeds(self, mock_serpapi_response_geothermal):
        with patch("serpapi.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.search.side_effect = [
                RuntimeError("SSL handshake timeout"),
                mock_serpapi_response_geothermal,
            ]
            mock_client_class.return_value = mock_client

            with patch("time.sleep") as _sleep:
                with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                    seeker = SerpApiSeeker()
                    seeker_input = SeekerInput(query="test query")
                    candidates = seeker.discover(seeker_input)

        assert len(candidates) == 3
        assert mock_client.search.call_count == 2

    def test_discover_extra_params(self):
        """Test discover passes extra parameters to SerpApi."""
        with patch("serpapi.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.search.return_value = {"organic_results": []}
            mock_client_class.return_value = mock_client

            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(
                    query="test query",
                    extra_params={"gl": "us", "hl": "en"},
                )
                seeker.discover(seeker_input)

                # Verify search was called with extra params
                call_args = mock_client.search.call_args
                search_params = call_args[0][0]
                assert search_params["gl"] == "us"
                assert search_params["hl"] == "en"

    def test_search_params_construction(self):
        """Test SerpApi search parameter construction."""
        seeker_input = SeekerInput(
            query="my search query",
            max_results=20,
            extra_params={"location": "Denver, CO"},
        )
        params = SerpApiSeeker._build_search_params(seeker_input)

        assert params["q"] == "my search query"
        assert params["engine"] == "google"
        assert params["num"] == 20
        assert params["location"] == "Denver, CO"

    def test_query_template_optional_token_rendering(self):
        """Optional token placeholders should be removed when values are missing."""
        rendered = SerpApiSeeker._render_query_template(
            "{jurisdiction} {state} geothermal ordinance {known_doc_id?} filetype:pdf",
            {
                "jurisdiction": "Chaffee County",
                "state": "CO",
            },
        )

        assert rendered == "Chaffee County CO geothermal ordinance filetype:pdf"

    def test_query_template_required_token_missing_raises(self):
        """Required token placeholders should fail when values are missing."""
        with pytest.raises(ValueError, match="Missing required template token"):
            SerpApiSeeker._render_query_template(
                "{jurisdiction} {state} geothermal ordinance",
                {"jurisdiction": "Chaffee County"},
            )

    def test_discover_renders_template_context(self):
        """discover should render query templates using template_context input."""
        with patch("serpapi.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.search.return_value = {"organic_results": []}
            mock_client_class.return_value = mock_client

            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(
                    query="{jurisdiction} {state} geothermal ordinance {known_doc_id?} filetype:pdf",
                    extra_params={
                        "template_context": {
                            "jurisdiction": "Chaffee County",
                            "state": "CO",
                        },
                        "hl": "en",
                    },
                )
                seeker.discover(seeker_input)

                call_args = mock_client.search.call_args
                search_params = call_args[0][0]
                assert (
                    search_params["q"]
                    == "Chaffee County CO geothermal ordinance filetype:pdf"
                )
                assert search_params["hl"] == "en"
                assert "template_context" not in search_params

    def test_discover_uses_query_family_fallback_when_query_missing(self):
        """discover should use selected query family when direct query is empty."""
        with patch("serpapi.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.search.return_value = {"organic_results": []}
            mock_client_class.return_value = mock_client

            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(
                    query="",
                    extra_params={
                        "use_query_family": "geothermal_generic",
                        "query_families": {
                            "geothermal_generic": [
                                "{jurisdiction} {state} geothermal ordinance {known_doc_id} filetype:pdf",
                                "{jurisdiction} {state} geothermal ordinance filetype:pdf",
                            ]
                        },
                        "template_context": {
                            "jurisdiction": "Chaffee County",
                            "state": "CO",
                        },
                    },
                )
                seeker.discover(seeker_input)

                call_args = mock_client.search.call_args
                search_params = call_args[0][0]
                assert (
                    search_params["q"]
                    == "Chaffee County CO geothermal ordinance filetype:pdf"
                )

    def test_discover_empty_query_without_fallback_raises(self):
        """discover should fail if query and fallback templates are both missing."""
        with patch("serpapi.Client"):
            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(query="", extra_params={})

                with pytest.raises(ValueError, match="cannot be resolved"):
                    seeker.discover(seeker_input)

    def test_supports_provider(self):
        """Test provider support checking."""
        with patch("serpapi.Client"):
            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                seeker = SerpApiSeeker()
                assert seeker.supports_provider("serpapi")
                assert seeker.supports_provider("serpapi_google")
                assert seeker.supports_provider("SERPAPI")  # case-insensitive
                assert not seeker.supports_provider("crawlee")
                assert not seeker.supports_provider("direct_url")

    def test_url_validation(self):
        """Test URL validation logic."""
        assert SerpApiSeeker._is_valid_url("https://example.com/doc.pdf")
        assert SerpApiSeeker._is_valid_url("http://example.com/doc.pdf")
        assert SerpApiSeeker._is_valid_url("HTTPS://EXAMPLE.COM")  # case-insensitive

        assert not SerpApiSeeker._is_valid_url("")
        assert not SerpApiSeeker._is_valid_url(None)
        assert not SerpApiSeeker._is_valid_url("ftp://example.com")
        assert not SerpApiSeeker._is_valid_url("example.com")
        assert not SerpApiSeeker._is_valid_url(123)  # type validation

    def test_normalized_candidate_format(self):
        """Test normalized candidate has all expected fields."""
        mock_response = {
            "organic_results": [
                {
                    "position": 1,
                    "title": "Test Result",
                    "link": "https://example.com/doc.pdf",
                    "snippet": "Test snippet",
                }
            ]
        }
        with patch("serpapi.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.search.return_value = mock_response
            mock_client_class.return_value = mock_client

            with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-api-key"}):
                seeker = SerpApiSeeker()
                seeker_input = SeekerInput(query="test query")
                candidates = seeker.discover(seeker_input)

                assert len(candidates) > 0
                candidate = candidates[0]
                assert "url" in candidate
                assert "source" in candidate
                assert "title" in candidate
                assert "snippet" in candidate
                assert "reasons" in candidate
                assert isinstance(candidate["reasons"], list)


class TestSerpApiResultCache:
    _RESPONSE = {
        "organic_results": [
            {"link": "https://example.gov/a.pdf", "title": "A", "snippet": "x"},
            {"link": "https://example.gov/b.pdf", "title": "B", "snippet": "y"},
        ]
    }

    def test_second_discover_hits_cache_no_second_api_call(self, tmp_path):
        with patch("serpapi.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.search.return_value = self._RESPONSE
            mock_client_class.return_value = mock_client

            with patch.dict(
                os.environ,
                {"SERPAPI_API_KEY": "test-api-key", "SERPAPI_SSL_VERIFY": "true"},
            ):
                seeker = SerpApiSeeker(cache_dir=str(tmp_path / "cache"))
                si = SeekerInput(query="geothermal ordinance", max_results=10)

                first = seeker.discover(si)
                assert len(first) == 2
                assert mock_client.search.call_count == 1

                # A fresh seeker sharing the cache dir must NOT call the API.
                seeker2 = SerpApiSeeker(cache_dir=str(tmp_path / "cache"))
                mock_client.search.reset_mock()
                second = seeker2.discover(si)
                assert mock_client.search.call_count == 0
                assert [c["url"] for c in second] == [c["url"] for c in first]

    def test_no_cache_dir_means_no_persistence(self, tmp_path):
        with patch("serpapi.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.search.return_value = self._RESPONSE
            mock_client_class.return_value = mock_client
            with patch.dict(
                os.environ,
                {"SERPAPI_API_KEY": "test-api-key", "SERPAPI_SSL_VERIFY": "true"},
            ):
                seeker = SerpApiSeeker()  # no cache_dir
                si = SeekerInput(query="q", max_results=10)
                seeker.discover(si)
                seeker.discover(si)
                assert mock_client.search.call_count == 2
