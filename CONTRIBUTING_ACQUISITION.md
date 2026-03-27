# Contributing to Acquisition (Seeker & Digger)

This guide explains how to extend the web acquisition pipeline by implementing new seeker and digger connectors.

## Overview: Acquisition Pipeline Architecture

StreamlineExtract's acquisition stage discovers candidate documents from the web in two sequential phases:

```
[Seeker Stage]              [Digger Stage]              [Validation & Filtering]
Query → Search API      →   Crawl/Index Pages      →   MIME/Keyword/Content
(e.g., SerpApi)             (e.g., HTTP crawler)        Checks → Scored Candidates
```

**Phase 1: Seeker** (Query Discovery)
- Accepts a search query and optional constraints
- Returns candidate URLs with metadata (title, snippet, source)
- Providers: SerpApi (production), Search API, custom search services

**Phase 2: Digger** (Link Discovery)
- Accepts seed page URLs and crawl budget parameters
- Returns discovered document links and artifacts
- Providers: HTTP crawler (production), Crawlee/Playwright (scheduled), custom crawlers

**Phase 3: Scoring & Filtering** (Downstream)
- Validates candidates (MIME type, keywords, content sampling)
- Ranks by heuristic signals (file type, domain authority, keyword match, power class)
- Returns top-K candidates for download

---

## Connector Architecture

### Base Interfaces

All connectors inherit from abstract base classes in [src/streamline_extract/acquisition/connectors/base.py](src/streamline_extract/acquisition/connectors/base.py).

#### BaseSeekerConnector

```python
class BaseSeekerConnector(ABC):
    """Abstract interface for search discovery (seeker stage)."""
    
    @abstractmethod
    def discover(self, seeker_input: SeekerInput) -> list[dict[str, Any]]:
        """
        Discover candidate URLs from a query.
        
        Args:
            seeker_input: Query, max results, and optional provider-specific parameters
        
        Returns:
            List of normalized candidates:
            {
                "url": str,
                "source": str,
                "title": str | None,
                "snippet": str | None,
                "reasons": list[str],
            }
        
        Raises:
            RuntimeError: If provider unavailable or API key missing
            ValueError: If query invalid or constraints unsatisfiable
        """
        pass
    
    @abstractmethod
    def supports_provider(self, provider: str) -> bool:
        # Contributing to Acquisition

        This guide covers the current extension surface for web acquisition. It is intentionally aligned to the tracked runtime in this repository, not to planned or retired connector abstractions.

        ## Current Runtime Shape

        The acquisition pipeline currently has four concrete extension seams:

        1. Seeker connector contract in `src/streamline_extract/acquisition/connectors/base.py`
        2. Digger connector contract and provider resolver in `src/streamline_extract/acquisition/connectors/digger.py`
        3. Engine orchestration in `src/streamline_extract/acquisition/engine.py`
        4. Focused regression suites under `tests/`

        Today, the tracked runtime behaves like this:

        - Seeker stage: `SerpApiSeeker` is the active provider in `AcquisitionEngine._run_seeker()`
        - Digger stage: `resolve_digger_connector()` dispatches to `NullDiggerConnector` or `HttpDiggerConnector`
        - Ranking stage: `LinkPrioritizer` ranks seeker output before download
        - Validation stage: downstream candidate selection, MIME checks, content checks, and policy gates remain engine-managed

        Do not add legacy compatibility layers or alternate runtime paths unless the user explicitly asks for them.

        ## Core Contracts

        The base connector interfaces live in `src/streamline_extract/acquisition/connectors/base.py`.

        ### Seeker contract

        `BaseSeekerConnector.discover()` accepts `SeekerInput` and returns a list of normalized dictionaries with at least:

        ```python
        {
            "url": str,
            "source": str,
            "title": str | None,
            "snippet": str | None,
            "reasons": list[str],
        }
        ```

        `SeekerInput` currently contains:

        - `query`
        - `max_results`
        - `extra_params`

        `extra_params` is where template context, query-family metadata, and provider-specific settings travel.

        ### Digger contract

        `BaseDiggerConnector.discover()` accepts `DiggerInput` and returns `list[DiggerArtifact]`.

        `DiggerInput` currently contains:

        - `seed_urls`
        - `max_depth`
        - `max_pages`
        - `max_files`
        - `timeout_seconds`
        - `allowed_domains`
        - `include_url_patterns`
        - `include_link_text_patterns`
        - `extra_params`

        `DiggerArtifact` must provide:

        - `url`
        - `source`
        - optional `mime_type`
        - optional `extension`
        - `status`
        - optional `metadata`

        ## Existing Providers

        Tracked providers today:

        - `SerpApiSeeker` in `src/streamline_extract/acquisition/connectors/serpapi_seeker.py`
        - `NullDiggerConnector` in `src/streamline_extract/acquisition/connectors/digger.py`
        - `HttpDiggerConnector` in `src/streamline_extract/acquisition/connectors/digger.py`

        Provider aliases already supported by the digger resolver are covered by tests. Preserve those aliases when extending the resolver.

        ## Adding a New Seeker Connector

        Create the connector module under `src/streamline_extract/acquisition/connectors/` and implement `BaseSeekerConnector`.

        Minimal skeleton:

        ```python
        from __future__ import annotations

        from typing import Any

        from .base import BaseSeekerConnector, SeekerInput


        class CustomSeeker(BaseSeekerConnector):
            def discover(self, seeker_input: SeekerInput) -> list[dict[str, Any]]:
                if not seeker_input.query.strip():
                    raise ValueError("Query cannot be empty")

                return [
                    {
                        "url": "https://example.com/document.pdf",
                        "source": "custom_search",
                        "title": "Example",
                        "snippet": "Example snippet",
                        "reasons": ["custom_search_result"],
                    }
                ]

            def supports_provider(self, provider: str) -> bool:
                return provider.lower() in {"custom", "custom_search"}
        ```

        After that, update `src/streamline_extract/acquisition/connectors/__init__.py` exports.

        Important: seeker provider dispatch is not yet handled by a standalone resolver function. The current engine path instantiates `SerpApiSeeker` directly inside `AcquisitionEngine._run_seeker()`. If you add a new seeker provider, you must update that dispatch path deliberately rather than assuming a hidden resolver already exists.

        Keep that change explicit and covered by tests.

        ## Adding a New Digger Connector

        Implement `BaseDiggerConnector`, then wire it into `resolve_digger_connector()` in `src/streamline_extract/acquisition/connectors/digger.py`.

        Minimal skeleton:

        ```python
        from __future__ import annotations

        from .base import BaseDiggerConnector, DiggerArtifact, DiggerInput


        class CustomDiggerConnector(BaseDiggerConnector):
            def discover(self, digger_input: DiggerInput) -> list[DiggerArtifact]:
                if not digger_input.seed_urls:
                    return []

                return [
                    DiggerArtifact(
                        url="https://example.com/document.pdf",
                        source="custom_digger",
                        status="link_discovered",
                        metadata={"seed_count": len(digger_input.seed_urls)},
                    )
                ]

            def supports_provider(self, provider: str) -> bool:
                return provider.lower() in {"custom", "custom_digger"}
        ```

        When extending `resolve_digger_connector()`:

        - preserve existing aliases
        - keep unsupported-provider failures explicit
        - add focused tests for the new alias set

        ## Engine Integration Rules

        The engine is responsible for more than connector dispatch. New connectors must fit these existing behaviors:

        - `AcquisitionEngine._build_seeker_inputs()` handles target-matrix expansion and template context
        - `CandidateSelector` reduces seeker output before ranking
        - `LinkPrioritizer` can cap ranked seeker output via `link_top_k`
        - routing, policy enforcement, retries, and manifest lineage stay engine-owned

        That means connector additions should stay narrow. Do not move ranking, policy, or manifest responsibilities into connectors.

        ## Test Map

        Use the existing test files by concern:

        - `tests/test_serpapi_seeker.py`: seeker-provider behavior and query rendering
        - `tests/test_digger_connector_abstraction.py`: digger provider contracts and resolver behavior
        - `tests/test_digger_fixture_based.py`: realistic crawl fixtures
        - `tests/test_acquisition_models.py`: engine orchestration, manifest output, prioritization, downloads, retries
        - `tests/test_acquisition_topology_fixtures.py`: distributed, centralized, and hybrid routing flows

        For a new connector, the minimum expected coverage is:

        1. contract test for normalized output
        2. provider-alias resolution test
        3. error-path test for invalid input or unavailable dependency
        4. one engine-level regression if the new provider changes orchestration behavior

        ## Focused Test Commands

        Use `pixi` commands only.

        ```bash
        pixi run pytest tests/test_serpapi_seeker.py tests/test_digger_connector_abstraction.py -q
        pixi run pytest tests/test_acquisition_models.py tests/test_acquisition_topology_fixtures.py -q
        pixi run pytest tests/test_digger_fixture_based.py -q
        ```

        For a seeker-only change, start with the seeker and engine files that actually exercise ranking and manifest output. For a digger-only change, start with resolver and fixture coverage.

        ## Contributor Checklist

        Before considering an acquisition connector change complete, verify all of the following:

        - connector output matches the base contract exactly
        - provider naming and aliases are explicit and tested
        - budget and timeout handling stay bounded
        - domain and pattern filters still work as configured
        - manifest lineage remains intact
        - focused `pixi run pytest` coverage passes for the touched seam

        ## Configuration Notes

        Most provider behavior is driven from runtime config under `config/<domain>/run.yaml`, especially:

        - `acquisition.seeker.*`
        - `acquisition.runtime.*`
        - `acquisition.topology.*`
        - `acquisition.digger.*`

        If a connector needs a new runtime knob, add it through the runtime config loader and its validation tests instead of reading ad hoc values inside the connector.

        ## Common Mistakes To Avoid

        - adding a new seeker connector without updating `AcquisitionEngine._run_seeker()`
        - putting ranking or validation logic into the connector instead of the engine
        - introducing domain-specific hardcoded host tables in connector code
        - skipping engine-level tests when provider dispatch behavior changes
        - using non-`pixi` commands for validation in this repository
  --seeker-provider custom_search \
  --digger-provider custom_crawl
```

### Example 3: Full Acquisition Workflow with Testing

```bash
# 1. Implement connector (inherit from base)
# 2. Register in __init__.py
# 3. Wire in engine.py
# 4. Create 5-10 unit tests
# 5. Run full test suite
pixi run pytest tests/test_acquisition_models.py -v

# 6. Test with real documents
pixi run streamline-extract acquire documents/test/ \
  --config config/test_domain/run.yaml \
  --output output/test_acquisition

# 7. Review acquired candidates
ls -la output/test_acquisition/
```

---

## Key Design Principles

1. **Statelessness**: Connectors are stateless; no persistent connection objects
2. **Normalization**: All results map to standard contract (SeekerInput/DiggerInput, normalized dicts/artifacts)
3. **Budgets**: Digger connectors strictly enforce crawl budgets (max_depth, max_pages, max_files, timeout)
4. **Error Resilience**: Failed individual discoveries don't stop the batch; continue with next seed
5. **Transparency**: Include metadata (source, discovery_method, elapsed_seconds) for audit trails

---

## Troubleshooting

**"Unsupported provider" error:**
- Ensure connector is registered in `__init__.py`
- Check `supports_provider()` method includes lowercase variant
- Run `pytest` to validate registration

**"Timeout or budget exceeded":**
- Increase `timeout_seconds` or `max_pages` in config
- Reduce `max_depth` if crawl is too deep
- Check network connectivity and target site performance

**"API key not found":**
- Set environment variable in `.env` or shell
- Verify key is correctly loaded in `__init__` method
- Run `echo $YOUR_API_KEY` to debug

**Tests failing:**
- Run with `-v` flag to see assertion details
- Check if test uses mocks or live API (prefer mocks)
- Ensure normalized output structure matches contract

---

## Next Steps

- Review existing [SerpApiSeeker](src/streamline_extract/acquisition/connectors/serpapi_seeker.py) for production patterns
- Check [test examples](tests/test_digger_connector_abstraction.py) for testing best practices
- Open an issue if extending with new domain or provider
- Submit PR with new connector (include tests, docs, and examples)
