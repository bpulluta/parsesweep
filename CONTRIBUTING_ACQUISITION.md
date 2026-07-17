# Contributing to Acquisition

This guide describes the current acquisition pipeline and how to extend it. It
is aligned to the tracked runtime in this repository — not to planned or retired
abstractions. Do not add legacy compatibility layers or alternate runtime paths
unless explicitly asked.

## Pipeline overview

Acquisition turns a set of *targets* into downloaded, curated documents:

```
targets → seeker → [routing/crawl] → selection → download → classify → review → manifest
```

1. **Target generation** — produce the list of targets (context dicts) to
   search for. Providers in `acquisition/targets/`: `csv`, `inline`,
   `dataset`, `cross_product` (e.g. counties × doc-types), resolved by
   `resolve_target_provider()`.
2. **Seeker** — one search per target (SerpApi) → candidate URLs.
   `SerpApiSeeker` in `connectors/serpapi_seeker.py`.
3. **Routing / crawl** *(optional, `follow_links: true`)* — crawl seed pages to
   discover document links. Diggers in `connectors/digger.py`:
   `NullDiggerConnector`, `HttpDiggerConnector`, and `SeleniumDiggerConnector`
   (real headless Chrome for bot-protected sites), via
   `resolve_digger_connector()`.
4. **Selection** — per-target, recall-first (`CandidateSelector`): draft skip,
   light exclude terms, recency, `max_per_target`. See "Design principles".
5. **Download** — `AcquisitionEngine._download_candidates()`. With
   `browser_mode: true`, files are fetched *through* Chrome (real TLS) to get
   past edge bot-managers (e.g. Akamai); otherwise via `requests`.
6. **Classify** *(optional)* — cheap keyword check (`document_classifier`,
   `ContentSampler`) flags files that don't look like the expected type.
7. **Review** *(optional, LLM)* — `document_reviewer.py` grades each file and
   promotes the primary one(s) into a `reviewed/` subfolder for review-free
   scale.
8. **Manifest** — run manifest + `download_index.csv` (with target attribution,
   partitioning, classification/review annotations).

## Design principles

- **Recall first, precision downstream.** Acquire broadly (good queries + light
  excludes) so the real document is never filtered out; let the classifier and
  LLM reviewer handle precision. Avoid hard allow-lists and long require-term
  lists — they reject the actual files.
- **Domain-neutral core.** No domain vocabulary (jurisdictions, manufacturers,
  host tables) hard-coded in engine/selector/prioritizer. Domain specifics live
  in `config/<domain>/run.yaml` (`queries`, `query_context_aliases`,
  `partition_by`, `link_prioritization.*`, `document_classifier`,
  `document_review`).
- **Per-target independence.** Each target is queried and selected on its own;
  there is no global cross-target cap.
- **Stateless, budgeted connectors.** Connectors are stateless, enforce crawl
  budgets, and never own ranking/policy/manifest logic (that stays in the
  engine). One failed discovery/download must not abort the batch.

## Extension seams

| Seam | File | Resolver |
|---|---|---|
| Target provider | `acquisition/targets/` | `resolve_target_provider()` |
| Seeker connector | `connectors/serpapi_seeker.py` | instantiated in `AcquisitionEngine._run_seeker()` (no standalone resolver yet — wire it there explicitly) |
| Digger connector | `connectors/digger.py` | `resolve_digger_connector()` |

Shared constants (extensions/MIME/statuses) live in `acquisition/constants.py`;
URL helpers in `acquisition/urls.py`. Reuse them rather than re-deriving.

### Contracts (`connectors/base.py`)

- `BaseSeekerConnector.discover(SeekerInput) -> list[dict]` with keys
  `url, source, title, snippet, reasons`. `SeekerInput`: `query`, `max_results`,
  `extra_params` (template context / query-family metadata travel here).
- `BaseDiggerConnector.discover(DiggerInput) -> list[DiggerArtifact]`.
  `DiggerInput`: `seed_urls, max_depth, max_pages, max_files, timeout_seconds,
  allowed_domains, include_url_patterns, include_link_text_patterns,
  extra_params`. `DiggerArtifact`: `url, source, mime_type?, extension?, status,
  metadata?`. Set `metadata["source_seed"]` so crawled children keep target
  attribution.
- `BaseTargetProvider.provide() -> list[dict]` + `supports_source(str)`.

### Adding a digger connector

Implement `BaseDiggerConnector`, then add it to `resolve_digger_connector()`
(preserve existing aliases; keep unsupported-provider failures explicit). For
extension-less document links (e.g. DNN `showpublisheddocument`), rely on
`include_url_patterns` rather than file-extension matching.

### Adding a seeker connector

Implement `BaseSeekerConnector`, export it in `connectors/__init__.py`, and wire
dispatch in `AcquisitionEngine._run_seeker()` (there is no hidden seeker
resolver). Keep it covered by tests.

## Configuration

Runtime config lives in `config/<domain>/run.yaml`; see
[config/TEMPLATE.yaml](config/TEMPLATE.yaml) for the annotated, recall-first
reference and [config/README.md](config/README.md) for the field tables. Add new
knobs through `config/runtime_config_loader.py` (+ its validation tests), never
by reading ad hoc values inside a connector.

## Test map

- `tests/test_target_providers.py` — target generation (csv/inline/dataset/cross_product)
- `tests/test_serpapi_seeker.py` — seeker behavior + query rendering
- `tests/test_digger_connector_abstraction.py` — digger contracts + resolver aliases
- `tests/test_digger_fixture_based.py` — realistic crawl fixtures
- `tests/test_browser_digger.py` — browser digger + browser-mode download wiring
- `tests/test_candidate_selector.py` — recall-first selection
- `tests/test_link_prioritizer.py` — heuristic ranking
- `tests/test_target_attribution.py` — attribution through selection/prioritizer/routing
- `tests/test_checkpoint.py`, `tests/test_domain_neutral.py`,
  `tests/test_document_classifier.py`, `tests/test_document_reviewer.py`
- `tests/test_acquisition_models.py`, `tests/test_acquisition_topology_fixtures.py` — engine + routing

Minimum coverage for a new connector: (1) contract/normalized-output test,
(2) provider-alias resolution, (3) error path, (4) one engine-level regression
if it changes orchestration.

```bash
pixi run pytest tests/test_serpapi_seeker.py tests/test_digger_connector_abstraction.py -q
pixi run pytest tests/test_target_providers.py tests/test_acquisition_topology_fixtures.py -q
```

## Common mistakes to avoid

- Hard-coding domain vocabulary or host tables in connector/engine code.
- Front-loading aggressive precision filters at acquire time (rejects real docs).
- Putting ranking/validation/manifest logic in a connector.
- Adding a seeker provider without wiring `AcquisitionEngine._run_seeker()`.
- Using non-`pixi` commands for validation in this repository.
