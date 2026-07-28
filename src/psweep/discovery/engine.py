"""Scaffold discovery engine for initial CLI integration."""

from __future__ import annotations

import contextlib
import json
import csv
import hashlib
import importlib.util
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from dataclasses import dataclass, replace as dataclass_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from .connectors import DiggerInput, resolve_digger_connector
from .connectors.digger import NullDiggerConnector
from .candidate_selector import CandidateSelector
from .constants import (
    DEFAULT_QUERY_CONTEXT_ALIASES,
    DOWNLOADABLE_EXTENSIONS,
    MIME_TO_EXTENSION,
    ROUTE_REASON_CENTRALIZED,
    ROUTE_REASON_DISTRIBUTED,
    SEEKER_ONLY_SCORE_WEIGHTS,
)
from .link_prioritizer import LinkPrioritizer
from .models import DiscoveryCandidate, DiscoveryManifest, CandidateScore
from .retry import compute_backoff, is_transient_error
from .urls import normalize_url_text, url_host
from .policies import DiscoveryPolicyEvaluator
from psweep.utils.error_taxonomy import (
    build_error_record,
    normalize_error_records,
    summarize_error_records,
)


@dataclass(slots=True)
class DiscoveryRequest:
    """Normalized command inputs for a discovery run."""

    domain: str
    seed_urls: list[str]
    query: str | None
    enable_serpapi: bool
    output_documents: Path | None
    output_manifest: Path | None
    dry_run: bool
    state: str | None = None
    jurisdiction: str | None = None
    partition_mode: str = "auto"
    digger_provider: str = "seed_only"
    topology_mode: str | None = None
    hub_pages: list[str] | None = None
    allowed_domains: list[str] | None = None
    include_url_patterns: list[str] | None = None
    include_link_text_patterns: list[str] | None = None
    index_page_mode: dict[str, object] | None = None
    index_links: list[dict[str, object] | str] | None = None
    max_depth: int | None = None
    max_pages: int | None = None
    max_files: int | None = None
    timeout_seconds: int | None = None
    retry_max_attempts: int = 3
    retry_initial_backoff_seconds: float = 1.0
    retry_max_backoff_seconds: float = 8.0
    max_concurrent_downloads: int = 5
    min_request_interval_ms: int = 0
    request_headers: dict[str, str] | None = None
    robots_policy_mode: str = "ignore"
    tos_policy_mode: str = "ignore"
    acknowledged_tos_domains: list[str] | None = None
    targets: list[dict[str, object]] | None = None
    query_templates: list[str] | None = None
    query_families: dict[str, list[str]] | None = None
    use_query_family: str | None = None
    seeker_max_results: int = 10
    # Link prioritization
    link_prioritization_mode: str = "heuristic"
    link_top_k: int = 0  # 0 = rank only, no global cap (per-target controls recall)
    link_prioritization_keywords: list[str] | None = None
    link_prioritization_domain_scores: dict[str, float] | None = None
    power_range_kw: list[float] | None = None
    # Per-target candidate selection
    selection_primary_per_target: int = 1
    selection_exclude_draft: bool = True
    selection_draft_patterns: list[str] | None = None
    selection_relevance_require_any_terms: list[str] | None = None
    selection_relevance_require_legal_marker_terms: list[str] | None = None
    selection_relevance_exclude_any_terms: list[str] | None = None
    selection_relevance_allowed_domain_patterns: list[str] | None = None
    selection_require_supported_document: bool = True
    selection_target_identity_require_any_templates: list[str] | None = None
    selection_target_identity_require_all_templates: list[str] | None = None
    selection_target_identity_exclude_any_templates: list[str] | None = None
    # Post-download document classification (keyword) + LLM review (curation)
    document_classifier: dict[str, object] | None = None
    document_review: dict[str, object] | None = None
    # Query-template context aliases (coalesce first non-empty source field)
    query_context_aliases: dict[str, list[str]] | None = None
    # Output partitioning by target-metadata fields (generic, domain-neutral)
    partition_by: list[str] | None = None
    # Use a real browser to clear bot-manager challenges (Akamai/Cloudflare)
    # for downloads on protected sites.
    browser_mode: bool = False
    # Extra SerpApi query params forwarded verbatim to the seeker (e.g.
    # {"tbm": "nws"} for Google News, {"tbs": "qdr:y"} for recency). Enables
    # dated-news discovery for temporal domains.
    seeker_extra_params: dict[str, object] | None = None
    # Model-tiering map ({tier_name: model_name}) from the top-level run.yaml
    # ``models:`` block. Acquire-side LLM stages (document review) resolve their
    # ``model`` tier reference against this map via ``extraction.llm_factory``.
    models: dict[str, object] | None = None
    # Cache SerpApi results on disk so re-running acquire during tuning does not
    # re-pay for identical queries. TTL in minutes (0 = never expire).
    seeker_cache: bool = False
    seeker_cache_ttl_minutes: float = 0.0
    # Optional callback for human-friendly progress messages emitted by engine stages.
    progress_callback: Callable[[str], None] | None = None


@dataclass(slots=True)
class DiscoveryResult:
    """Minimal result payload for the scaffold engine."""

    run_id: str
    manifest_path: Path
    documents_dir: Path
    dry_run: bool
    download_index_path: Path | None = None
    review_index_path: Path | None = None
    curated_dir: Path | None = None
    curated_count: int = 0


class _RequestRateLimiter:
    """Thread-safe request pacing helper shared across network stages."""

    def __init__(self, min_interval_ms: int):
        self._min_interval_seconds = max(0.0, float(min_interval_ms) / 1000.0)
        self._last_request_monotonic = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self._min_interval_seconds <= 0:
            return

        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_monotonic
            remaining = self._min_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
                now = time.monotonic()
            self._last_request_monotonic = now


class DiscoveryEngine:
    """Initial discovery engine that emits deterministic scaffold artifacts."""

    def __init__(self) -> None:
        self._policy_evaluator = DiscoveryPolicyEvaluator()

    @staticmethod
    def _emit_progress(request: DiscoveryRequest, message: str) -> None:
        callback = getattr(request, "progress_callback", None)
        if callback is None:
            return
        with contextlib.suppress(Exception):
            callback(message)

    @staticmethod
    def _target_label(target: dict[str, object] | None, fallback_index: int) -> str:
        if not isinstance(target, dict):
            return f"target-{fallback_index}"
        for key in (
            "label",
            "site_name",
            "jurisdiction",
            "county",
            "company_name",
            "query",
        ):
            value = target.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return f"target-{fallback_index}"

    _DEFAULT_REQUEST_HEADERS = {
        "User-Agent": "ParseSweep/2.0 (+discovery)"
    }
    # File-type sets / MIME map are centralized in constants.py.
    _SUPPORTED_EXTENSIONS = DOWNLOADABLE_EXTENSIONS
    _MIME_EXTENSION_MAP = MIME_TO_EXTENSION
    _STATE_ALIASES = {
        "alabama": "al",
        "alaska": "ak",
        "arizona": "az",
        "arkansas": "ar",
        "california": "ca",
        "colorado": "co",
        "connecticut": "ct",
        "delaware": "de",
        "florida": "fl",
        "georgia": "ga",
        "hawaii": "hi",
        "idaho": "id",
        "illinois": "il",
        "indiana": "in",
        "iowa": "ia",
        "kansas": "ks",
        "kentucky": "ky",
        "louisiana": "la",
        "maine": "me",
        "maryland": "md",
        "massachusetts": "ma",
        "michigan": "mi",
        "minnesota": "mn",
        "mississippi": "ms",
        "missouri": "mo",
        "montana": "mt",
        "nebraska": "ne",
        "nevada": "nv",
        "new hampshire": "nh",
        "new jersey": "nj",
        "new mexico": "nm",
        "new york": "ny",
        "north carolina": "nc",
        "north dakota": "nd",
        "ohio": "oh",
        "oklahoma": "ok",
        "oregon": "or",
        "pennsylvania": "pa",
        "rhode island": "ri",
        "south carolina": "sc",
        "south dakota": "sd",
        "tennessee": "tn",
        "texas": "tx",
        "utah": "ut",
        "vermont": "vt",
        "virginia": "va",
        "washington": "wa",
        "west virginia": "wv",
        "wisconsin": "wi",
        "wyoming": "wy",
    }

    @staticmethod
    def _slugify_domain(domain: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")
        return slug or "default"

    def _build_run_id(
        self, request: DiscoveryRequest, started_at: datetime
    ) -> str:
        timestamp = started_at.strftime("%Y%m%dT%H%M%SZ")
        fingerprint_basis = "|".join(
            [
                request.domain,
                request.query or "",
                "1" if request.dry_run else "0",
                *sorted(request.seed_urls),
            ]
        )
        fingerprint = hashlib.sha256(
            fingerprint_basis.encode("utf-8")
        ).hexdigest()[:8]
        return f"acq-{self._slugify_domain(request.domain)}-{timestamp}-{fingerprint}"

    def _resolve_output_paths(
        self,
        request: DiscoveryRequest,
        run_id: str,
    ) -> tuple[Path, Path]:
        # Everything for a run lives under one self-contained directory:
        #   discovered/<domain>/runs/<run_id>/
        #     manifest.json, download_index.csv, review.csv
        #     documents/   (all downloads)
        #     curated/     (final selected docs)
        run_dir = (
            Path("discovered") / request.domain / "runs" / run_id
        )

        if request.output_documents is not None:
            documents_dir = request.output_documents
        else:
            documents_dir = run_dir / "documents"

        if request.output_manifest is not None:
            manifest_path = request.output_manifest
        else:
            manifest_path = run_dir / "manifest.json"
        return documents_dir, manifest_path

    @staticmethod
    def _scaffold_candidates(
        seed_urls: list[str],
    ) -> list[DiscoveryCandidate]:
        candidates: list[DiscoveryCandidate] = []
        for seed_url in seed_urls:
            score = CandidateScore(
                url_signal=0.35,
                anchor_signal=0.0,
                content_signal=0.0,
                trust_signal=0.55,
            )
            candidates.append(
                DiscoveryCandidate(
                    url=seed_url,
                    source="seed_url",
                    score=score,
                    reasons=["Seed URL staged for seeker/digger expansion"],
                )
            )
        return candidates

    @staticmethod
    def _attribute_seeds_to_targets(
        seed_candidates: list[DiscoveryCandidate],
        targets: list[dict[str, object]],
    ) -> None:
        """Match seed URLs to targets and assign target_metadata.

        Fully generic: scores every field value in each target against the URL.
        Works with any targets.csv schema — jurisdiction, state, county, city,
        municipality, district, label, or any custom field. No hardcoded field
        names.

        Scoring: for each target, every word from any field value that appears
        in the URL contributes its character length to the score. Longer/more
        specific matches win. Minimum score threshold prevents false positives.
        """
        if not targets or not seed_candidates:
            return

        for candidate in seed_candidates:
            if candidate.target_metadata:
                continue  # Already attributed (e.g., from seeker)

            url_lower = candidate.url.lower()
            best_target: dict[str, object] | None = None
            best_score = 0

            for target in targets:
                score = 0
                # Score ALL string fields in the target against the URL
                for _field, val in target.items():
                    if not isinstance(val, str) or not val.strip():
                        continue
                    # Split multi-word values and check each part
                    # e.g., "Drumore Township" → ["drumore", "township"]
                    for part in val.lower().split():
                        if len(part) > 2 and part in url_lower:
                            score += len(part)

                if score > best_score:
                    best_score = score
                    best_target = target

            # Minimum threshold: at least one meaningful match (> 4 chars total)
            if best_target and best_score >= 4:
                candidate.target_metadata = dict(best_target)

    @staticmethod
    def _normalize_seed_urls(
        seed_urls: list[str],
    ) -> tuple[list[str], list[dict[str, object]]]:
        valid_seed_urls: list[str] = []
        error_records: list[dict[str, object]] = []

        for raw_seed in seed_urls:
            try:
                if not isinstance(raw_seed, str):
                    raise ValueError("Seed URL must be a string")
                seed = raw_seed.strip()
                if not seed:
                    raise ValueError("Seed URL must not be empty")
                if not re.match(r"^https?://", seed, flags=re.IGNORECASE):
                    raise ValueError(
                        f"Seed URL must start with http:// or https://: {seed}"
                    )
                valid_seed_urls.append(seed)
            except Exception as exc:
                error_records.append(
                    build_error_record(
                        exc,
                        stage="discovery.seed_validation",
                        document_path=str(raw_seed),
                    )
                )

        # Preserve first-seen order while removing duplicates.
        deduped: list[str] = []
        seen: set[str] = set()
        for seed in valid_seed_urls:
            if seed in seen:
                continue
            deduped.append(seed)
            seen.add(seed)

        return deduped, normalize_error_records(error_records)

    @staticmethod
    def _has_discovery_inputs(request: DiscoveryRequest) -> bool:
        """Return whether request includes query-driven discovery inputs."""
        return bool(
            request.query
            or request.targets
            or request.query_templates
            or request.query_families
            or request.use_query_family
        )

    @staticmethod
    def _append_discovery_gap_diagnostics(
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
        normalized_seed_urls: list[str],
        errors: list[dict[str, object]],
        notes: list[str],
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Emit explicit diagnostics when configured discovery yields no candidates."""
        if not DiscoveryEngine._has_discovery_inputs(request):
            return errors, notes
        if candidates or normalized_seed_urls:
            return errors, notes

        updated_errors = normalize_error_records(
            [
                *errors,
                build_error_record(
                    RuntimeError(
                        "No discovery candidates were produced from query/target inputs. "
                        "Enable a seeker provider (for example SerpApi with a valid key) "
                        "or provide seed URLs."
                    ),
                    stage="discovery.search",
                    provider=(
                        "serpapi" if request.enable_serpapi else "seed_only"
                    ),
                ),
            ]
        )
        updated_notes = [
            *notes,
            "Discovery produced zero candidates from query/target inputs; "
            "configure seeker credentials or add seed URLs.",
        ]
        return updated_errors, updated_notes

    @staticmethod
    def _resolve_serpapi_state(
        request: DiscoveryRequest,
    ) -> tuple[dict[str, object], list[dict[str, object]], list[str]]:
        if not request.enable_serpapi:
            return (
                {
                    "provider": "seed_only",
                    "enabled": False,
                    "available": False,
                },
                [],
                [],
            )

        notes: list[str] = []
        errors: list[dict[str, object]] = []
        is_available = importlib.util.find_spec("serpapi") is not None

        if not is_available:
            errors.append(
                build_error_record(
                    RuntimeError(
                        "SerpApi feature flag enabled but optional dependency is not installed"
                    ),
                    stage="discovery.seeker_init",
                    provider="serpapi",
                )
            )
        elif not (os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY")):
            errors.append(
                build_error_record(
                    ValueError(
                        "SerpApi feature flag enabled but SERPAPI_API_KEY or SERPAPI_KEY is not set"
                    ),
                    stage="discovery.seeker_init",
                    provider="serpapi",
                )
            )
        else:
            notes.append("SerpApi feature flag enabled; seeker connected.")

        return (
            {
                "provider": "serpapi",
                "enabled": True,
                "available": is_available,
            },
            normalize_error_records(errors),
            notes,
        )

    @staticmethod
    def _build_selector(request: DiscoveryRequest) -> CandidateSelector:
        """Construct a CandidateSelector from request selection settings.

        Shared by seeker-stage selection and post-routing re-selection so the
        two never drift apart.
        """
        return CandidateSelector(
            exclude_draft=request.selection_exclude_draft,
            draft_patterns=request.selection_draft_patterns or None,
            relevance_require_any_terms=request.selection_relevance_require_any_terms
            or None,
            relevance_require_legal_marker_terms=request.selection_relevance_require_legal_marker_terms
            or None,
            relevance_exclude_any_terms=request.selection_relevance_exclude_any_terms
            or None,
            require_supported_document=bool(
                request.selection_require_supported_document
            ),
            target_identity_require_any_templates=request.selection_target_identity_require_any_templates
            or None,
            target_identity_require_all_templates=request.selection_target_identity_require_all_templates
            or None,
            target_identity_exclude_any_templates=request.selection_target_identity_exclude_any_templates
            or None,
        )

    @staticmethod
    def _run_seeker(
        request: DiscoveryRequest,
        seeker_state: dict[str, object],
        current_notes: list[str],
        current_errors: list[dict[str, object]],
    ) -> tuple[
        list[DiscoveryCandidate],
        list[str],
        list[dict[str, object]],
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        """Call SerpApi seeker and return discovered candidates."""
        if not seeker_state.get("enabled") or not seeker_state.get(
            "available"
        ):
            return [], current_notes, current_errors, [], []

        notes = list(current_notes)
        errors = list(current_errors)

        try:
            from .connectors.serpapi_seeker import SerpApiSeeker

            seeker_cache_dir = None
            if getattr(request, "seeker_cache", False):
                seeker_cache_dir = str(
                    Path("discovered")
                    / request.domain
                    / ".serpapi_cache"
                )
            seeker = SerpApiSeeker(
                retry_max_attempts=request.retry_max_attempts,
                retry_initial_backoff_seconds=request.retry_initial_backoff_seconds,
                retry_max_backoff_seconds=request.retry_max_backoff_seconds,
                min_request_interval_seconds=max(
                    0.0, float(request.min_request_interval_ms) / 1000.0
                ),
                cache_dir=seeker_cache_dir,
                cache_ttl_seconds=max(
                    0.0,
                    float(
                        getattr(request, "seeker_cache_ttl_minutes", 0.0) or 0.0
                    )
                    * 60.0,
                ),
            )
            seeker_inputs = DiscoveryEngine._build_seeker_inputs(request)
            # Collect results per-target so selection can be applied independently
            raw_candidates_by_target: list[list[dict[str, object]]] = []
            total_inputs = len(seeker_inputs)
            for idx, seeker_input in enumerate(seeker_inputs, start=1):
                target = (
                    request.targets[idx - 1]
                    if request.targets and idx - 1 < len(request.targets)
                    else None
                )
                target_label = DiscoveryEngine._target_label(target, idx)
                query_preview = (seeker_input.query or "").strip() or "(resolved from templates)"
                DiscoveryEngine._emit_progress(
                    request,
                    f"seeker {idx}/{total_inputs}: searching {target_label} | query={query_preview}",
                )
                discovered = seeker.discover(seeker_input)
                raw_candidates_by_target.append(discovered)
                DiscoveryEngine._emit_progress(
                    request,
                    f"seeker {idx}/{total_inputs}: {target_label} -> {len(discovered)} candidate(s)",
                )
        except Exception as exc:
            errors.append(
                build_error_record(
                    exc,
                    stage="discovery.seeker_discover",
                    provider="serpapi",
                )
            )
            return [], notes, normalize_error_records(errors), [], []

        def _to_candidate(
            raw: dict[str, object],
        ) -> DiscoveryCandidate | None:
            url = raw.get("url")
            if not url:
                return None
            return DiscoveryCandidate(
                url=url,
                source=raw.get("source", "serpapi_google"),
                score=CandidateScore(
                    url_signal=0.5,
                    anchor_signal=0.1,
                    content_signal=0.0,
                    trust_signal=0.4,
                    weights=dict(SEEKER_ONLY_SCORE_WEIGHTS),
                ),
                reasons=list(raw.get("reasons") or []),
                title=raw.get("title") or None,
                snippet=raw.get("snippet") or None,
            )

        candidates_by_target: list[list[DiscoveryCandidate]] = [
            [c for raw in raws if (c := _to_candidate(raw)) is not None]
            for raws in raw_candidates_by_target
        ]
        total_discovered = sum(len(g) for g in candidates_by_target)
        notes.append(
            f"SerpApi seeker returned {total_discovered} candidate(s) across "
            f"{len(candidates_by_target)} target(s)."
        )

        # ---------------------------------------------------------------
        # Per-target selection (draft filter + recency)
        # ---------------------------------------------------------------
        primary_per_target = max(
            1, int(request.selection_primary_per_target or 1)
        )
        selector = DiscoveryEngine._build_selector(request)
        candidates, selection_notes, target_selection_metrics = (
            selector.select(
                candidates_by_target,
                primary_per_target=primary_per_target,
                target_contexts=request.targets or None,
                include_metrics=True,
            )
        )
        notes.extend(selection_notes)
        notes.append(
            f"Per-target selection (primary_per_target={primary_per_target}) "
            f"reduced {total_discovered} candidate(s) to {len(candidates)}."
        )

        # ---------------------------------------------------------------
        # Global heuristic prioritization (ranking + optional top-K cap)
        # ---------------------------------------------------------------
        prioritizer_lineage: list[dict[str, object]] = []
        mode = (request.link_prioritization_mode or "heuristic").lower()
        if mode != "off" and candidates:
            power_range: tuple[float, float] | None = None
            if request.power_range_kw and len(request.power_range_kw) >= 2:
                power_range = (
                    float(request.power_range_kw[0]),
                    float(request.power_range_kw[1]),
                )
            # Rank for ordering, but only apply a global cap when the config
            # explicitly sets link_top_k. Otherwise per-target selection
            # (max_per_target) is the sole recall control — a hidden global cap
            # was silently dropping valid documents.
            top_k_setting = int(request.link_top_k or 0)
            effective_top_k: int | None = (
                max(1, top_k_setting) if top_k_setting > 0 else None
            )
            if effective_top_k and len(candidates) <= effective_top_k:
                effective_top_k = None  # no additional cap needed
            # allowed-domain patterns are a SOFT trust boost (ranking), never a
            # hard filter — preferred domains rank higher without excluding others.
            domain_scores = dict(
                request.link_prioritization_domain_scores or {}
            )
            for pattern in (
                request.selection_relevance_allowed_domain_patterns or []
            ):
                domain_scores.setdefault(str(pattern), 0.15)
            prioritizer = LinkPrioritizer(
                keywords=request.link_prioritization_keywords,
                domain_authority_overrides=domain_scores or None,
                top_k=effective_top_k,
                power_range_kw=power_range,
            )
            ranked_candidates, prioritizer_lineage = prioritizer.prioritize(
                candidates
            )
            notes.append(
                f"Link prioritizer ({mode}) ranked {len(candidates)} candidate(s) "
                f"\u2192 top-{len(ranked_candidates)} surfaced."
            )
            candidates = ranked_candidates

        return (
            candidates,
            notes,
            normalize_error_records(errors),
            prioritizer_lineage,
            target_selection_metrics,
        )

    @staticmethod
    def _target_template_context(
        target: dict[str, object],
        aliases: dict[str, list[str]] | None = None,
    ) -> dict[str, str]:
        context: dict[str, str] = {}
        for key, value in target.items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                text = str(value).strip()
                if text:
                    context[str(key)] = text
                continue
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, (str, int, float, bool)):
                    text = str(first).strip()
                    if text:
                        context[str(key)] = text
                        singular_key = str(key)
                        if (
                            singular_key.endswith("s")
                            and len(singular_key) > 1
                        ):
                            context[singular_key[:-1]] = text

        # Coalesce aliases: an alias resolves to the first non-empty source
        # field. Config-driven; uses DEFAULT_QUERY_CONTEXT_ALIASES when not
        # overridden.
        effective_aliases = (
            aliases
            if aliases is not None
            else DEFAULT_QUERY_CONTEXT_ALIASES
        )
        for alias, sources in effective_aliases.items():
            if context.get(alias):
                continue
            for source_field in sources:
                resolved = context.get(source_field)
                if resolved:
                    context[alias] = resolved
                    break

        return context

    @staticmethod
    def _build_seeker_inputs(request: DiscoveryRequest):
        from .connectors.base import SeekerInput

        base_extra: dict[str, object] = {}
        if request.query_templates:
            base_extra["query_templates"] = request.query_templates
        if request.query_families:
            base_extra["query_families"] = request.query_families
        if request.use_query_family:
            base_extra["use_query_family"] = request.use_query_family
        if request.seeker_extra_params:
            # Verbatim SerpApi params (e.g. tbm=nws for Google News). Nested
            # under a reserved key so the seeker merges them into the request
            # without confusing them with engine control keys.
            base_extra["serpapi_params"] = dict(request.seeker_extra_params)

        targets = request.targets or []
        inputs: list[SeekerInput] = []

        if targets:
            for target in targets:
                if not isinstance(target, dict):
                    continue
                target_query = str(
                    target.get("query") or request.query or ""
                ).strip()
                extra_params = dict(base_extra)
                extra_params["template_context"] = (
                    DiscoveryEngine._target_template_context(
                        target, request.query_context_aliases
                    )
                )
                inputs.append(
                    SeekerInput(
                        query=target_query,
                        max_results=max(1, int(request.seeker_max_results)),
                        extra_params=extra_params,
                    )
                )

        if not inputs:
            template_context = {}
            if request.state:
                template_context["state"] = request.state
            if request.jurisdiction:
                template_context["jurisdiction"] = request.jurisdiction

            extra_params = dict(base_extra)
            if template_context:
                extra_params["template_context"] = template_context
            inputs.append(
                SeekerInput(
                    query=request.query or "",
                    max_results=max(1, int(request.seeker_max_results)),
                    extra_params=extra_params,
                )
            )

        return inputs

    _is_transient_network_error = staticmethod(is_transient_error)

    @staticmethod
    def _retry_backoff_for_attempt(
        *,
        attempt: int,
        initial_backoff_seconds: float,
        max_backoff_seconds: float,
    ) -> float:
        return compute_backoff(
            attempt,
            initial_backoff_seconds=initial_backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
        )

    @staticmethod
    def _resolve_retry_policy(
        request: DiscoveryRequest,
    ) -> dict[str, float | int]:
        return {
            "max_attempts": max(1, int(request.retry_max_attempts)),
            "initial_backoff_seconds": max(
                0.0, float(request.retry_initial_backoff_seconds)
            ),
            "max_backoff_seconds": max(
                0.0, float(request.retry_max_backoff_seconds)
            ),
        }

    @classmethod
    def _resolve_request_headers(
        cls, request: DiscoveryRequest
    ) -> dict[str, str]:
        headers = dict(cls._DEFAULT_REQUEST_HEADERS)
        for key, value in (request.request_headers or {}).items():
            normalized_key = str(key or "").strip()
            normalized_value = str(value or "").strip()
            if normalized_key and normalized_value:
                headers[normalized_key] = normalized_value
        return headers

    @staticmethod
    def _policy_status_from_blocking_code(blocking_code: str | None) -> str:
        if blocking_code == "robots_disallowed":
            return "skipped_robots_disallowed"
        if blocking_code == "robots_unavailable":
            return "skipped_robots_unavailable"
        if blocking_code == "tos_unacknowledged":
            return "skipped_tos_unacknowledged"
        return "skipped_policy"

    @staticmethod
    def _policy_note_templates() -> dict[str, str]:
        return {
            "robots_disallowed": "robots.txt disallow rules",
            "robots_unavailable": "unevaluable robots.txt rules",
            "tos_unacknowledged": "missing ToS acknowledgement",
        }

    def _evaluate_request_policy(
        self,
        *,
        url: str,
        request: DiscoveryRequest,
        ssl_verify: bool,
    ):
        return self._policy_evaluator.evaluate(
            url=url,
            ssl_verify=ssl_verify,
            request_headers=self._resolve_request_headers(request),
            robots_policy_mode=request.robots_policy_mode,
            tos_policy_mode=request.tos_policy_mode,
            acknowledged_tos_domains=request.acknowledged_tos_domains,
        )

    def _build_policy_notes(
        self,
        *,
        stage_name: str,
        blocked_counts: Counter[str],
        warning_counts: Counter[str],
        request: DiscoveryRequest,
    ) -> list[str]:
        notes: list[str] = []
        labels = self._policy_note_templates()

        for code, count in sorted(blocked_counts.items()):
            if count <= 0:
                continue
            notes.append(
                f"{stage_name} policy enforcement skipped {count} URL(s) due to {labels.get(code, code)}."
            )

        for code, count in sorted(warning_counts.items()):
            if count <= 0:
                continue
            mode = (
                request.robots_policy_mode
                if code.startswith("robots_")
                else request.tos_policy_mode
            )
            notes.append(
                f"{stage_name} policy warnings: {count} URL(s) proceeded despite {labels.get(code, code)} because mode={mode}."
            )

        return notes

    def _filter_urls_for_policy(
        self,
        *,
        urls: list[str],
        request: DiscoveryRequest,
        ssl_verify: bool,
        stage_name: str,
    ) -> tuple[list[str], list[str]]:
        allowed_urls: list[str] = []
        blocked_counts: Counter[str] = Counter()
        warning_counts: Counter[str] = Counter()

        for url in urls:
            policy_result = self._evaluate_request_policy(
                url=url,
                request=request,
                ssl_verify=ssl_verify,
            )
            if policy_result.allowed:
                allowed_urls.append(url)
            if policy_result.blocking_code:
                blocked_counts[policy_result.blocking_code] += 1
            for code in policy_result.warning_codes:
                warning_counts[code] += 1

        return allowed_urls, self._build_policy_notes(
            stage_name=stage_name,
            blocked_counts=blocked_counts,
            warning_counts=warning_counts,
            request=request,
        )

    def _download_with_retry(
        self,
        *,
        url: str,
        ssl_verify: bool,
        retry_policy: dict[str, float | int],
        request_headers: dict[str, str],
        rate_limiter: _RequestRateLimiter | None = None,
    ):
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError(
                "requests dependency is required for discovery downloads"
            ) from exc

        max_attempts = int(retry_policy["max_attempts"])
        initial_backoff_seconds = float(
            retry_policy["initial_backoff_seconds"]
        )
        max_backoff_seconds = float(retry_policy["max_backoff_seconds"])

        last_exc: BaseException | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                if rate_limiter is not None:
                    rate_limiter.wait()
                response = requests.get(
                    url,
                    allow_redirects=True,
                    timeout=60,
                    stream=True,
                    verify=ssl_verify,
                    headers=request_headers,
                )
                response.raise_for_status()
                return response, attempt
            except Exception as exc:
                last_exc = exc
                if (
                    attempt >= max_attempts
                    or not self._is_transient_network_error(exc)
                ):
                    raise
                delay = self._retry_backoff_for_attempt(
                    attempt=attempt + 1,
                    initial_backoff_seconds=initial_backoff_seconds,
                    max_backoff_seconds=max_backoff_seconds,
                )
                if delay > 0:
                    time.sleep(delay)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Unexpected empty retry cycle for download request")

    def _download_single_candidate(
        self,
        *,
        idx: int,
        candidate: DiscoveryCandidate,
        request: DiscoveryRequest,
        documents_dir: Path,
        ssl_verify: bool,
        retry_policy: dict[str, float | int],
        rate_limiter: _RequestRateLimiter | None,
        browser: object | None = None,
    ) -> tuple[
        int, dict[str, object], dict[str, object] | None, bool, list[str]
    ]:
        url = candidate.url
        if not isinstance(url, str) or not re.match(
            r"^https?://", url, flags=re.IGNORECASE
        ):
            return (
                idx,
                {"url": url, "status": "skipped_invalid_url"},
                None,
                False,
                [],
            )

        policy_result = self._evaluate_request_policy(
            url=url,
            request=request,
            ssl_verify=ssl_verify,
        )
        if not policy_result.allowed:
            return (
                idx,
                {
                    "url": url,
                    "status": self._policy_status_from_blocking_code(
                        policy_result.blocking_code
                    ),
                    "policy_blocking_code": policy_result.blocking_code,
                    "policy_reason": "; ".join(policy_result.messages),
                },
                None,
                False,
                list(policy_result.warning_codes),
            )

        try:
            if browser is not None:
                # Fetch through the real browser (bypasses Akamai TLS checks).
                if rate_limiter is not None:
                    rate_limiter.wait()
                content, mime_type = browser.download_bytes(url)
                final_url = url
                attempt_count = 1

                def _chunks(_c: bytes = content) -> object:
                    return [_c]
            else:
                response, attempt_count = self._download_with_retry(
                    url=url,
                    ssl_verify=ssl_verify,
                    retry_policy=retry_policy,
                    request_headers=self._resolve_request_headers(request),
                    rate_limiter=rate_limiter,
                )
                final_url = response.url or url
                mime_type = (response.headers or {}).get("Content-Type")

                def _chunks(_r: object = response) -> object:
                    return _r.iter_content(chunk_size=65536)

            extension = self._infer_extension_from_url_or_mime(
                final_url, mime_type
            )
            if extension is None:
                return (
                    idx,
                    {
                        "url": url,
                        "final_url": final_url,
                        "status": "skipped_unsupported_type",
                        "mime_type": mime_type,
                        "policy_warning_codes": list(
                            policy_result.warning_codes
                        ),
                    },
                    None,
                    False,
                    list(policy_result.warning_codes),
                )

            passed_final_url_guard, reject_code = (
                self._passes_final_url_selection_guard(
                    final_url=final_url,
                    request=request,
                )
            )
            if not passed_final_url_guard:
                return (
                    idx,
                    {
                        "url": url,
                        "final_url": final_url,
                        "status": "skipped_selection_filter",
                        "reason": reject_code,
                        "mime_type": mime_type,
                        "policy_warning_codes": list(
                            policy_result.warning_codes
                        ),
                    },
                    None,
                    False,
                    list(policy_result.warning_codes),
                )

            partition_mode, partition_meta, partition_dir = (
                self._resolve_partition_dir(
                    documents_dir=documents_dir,
                    url=final_url,
                    request=request,
                    target_metadata=candidate.target_metadata,
                )
            )
            partition_dir.mkdir(parents=True, exist_ok=True)
            filename = self._safe_filename(final_url, extension, idx)
            target_path = partition_dir / filename
            suffix = 1

            while True:
                try:
                    bytes_written = 0
                    with target_path.open("xb") as handle:
                        for chunk in _chunks():
                            if not chunk:
                                continue
                            handle.write(chunk)
                            bytes_written += len(chunk)
                    break
                except FileExistsError:
                    suffix += 1
                    target_path = (
                        partition_dir
                        / f"{Path(filename).stem}-{suffix}{extension}"
                    )

            return (
                idx,
                {
                    "url": url,
                    "final_url": final_url,
                    "partition_mode": partition_mode,
                    "status": "downloaded",
                    "mime_type": mime_type,
                    "path": target_path.as_posix(),
                    "relative_path": target_path.relative_to(
                        documents_dir
                    ).as_posix(),
                    "bytes": bytes_written,
                    "attempt_count": attempt_count,
                    "policy_warning_codes": list(policy_result.warning_codes),
                    "target_label": (
                        candidate.target_metadata or {}
                    ).get("label"),
                    "target_metadata": candidate.target_metadata,
                    **partition_meta,
                },
                None,
                True,
                list(policy_result.warning_codes),
            )
        except Exception as exc:
            error = build_error_record(
                exc,
                stage="discovery.download",
                document_path=url,
                provider="http",
            )
            return (
                idx,
                {
                    "url": url,
                    "status": "failed",
                    "error": str(exc),
                    "policy_warning_codes": list(policy_result.warning_codes),
                },
                error,
                False,
                list(policy_result.warning_codes),
            )

    @staticmethod
    def _downloads_ssl_verify() -> bool:
        raw_value = (
            os.getenv("DISCOVERY_SSL_VERIFY")
            or os.getenv("PSWEEP_SSL_VERIFY")
            or "false"
        )
        return str(raw_value).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

    @staticmethod
    def _infer_extension_from_url_or_mime(
        url: str, mime_type: str | None
    ) -> str | None:
        parsed = urlparse(url)
        suffix = Path(parsed.path).suffix.lower()
        if suffix in DiscoveryEngine._SUPPORTED_EXTENSIONS:
            return suffix

        normalized_mime = (mime_type or "").split(";", 1)[0].strip().lower()
        ext = DiscoveryEngine._MIME_EXTENSION_MAP.get(normalized_mime)
        if ext in DiscoveryEngine._SUPPORTED_EXTENSIONS:
            return ext
        return None

    @staticmethod
    def _safe_filename(url: str, ext: str, index: int) -> str:
        parsed = urlparse(url)
        base = Path(unquote(parsed.path)).name
        if not base:
            base = f"candidate-{index:03d}{ext}"

        base_no_query = base.split("?", 1)[0]
        stem = Path(base_no_query).stem or f"candidate-{index:03d}"
        sanitized_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-_")
        if not sanitized_stem:
            sanitized_stem = f"candidate-{index:03d}"
        return f"{sanitized_stem}{ext}"

    _normalize_url_text = staticmethod(normalize_url_text)
    _url_host = staticmethod(url_host)

    @classmethod
    def _passes_final_url_selection_guard(
        cls,
        *,
        final_url: str,
        request: DiscoveryRequest,
    ) -> tuple[bool, str | None]:
        """Post-redirect junk guard: reject only on high-confidence excludes.

        Recall-first: the pre-download selector already applied any configured
        relevance rules against the candidate's full context. Re-applying
        require/allowed-domain gates to the bare final-URL string caused valid
        documents to be dropped (their filename lacks the keywords). Precision
        now lives downstream in the document classifier, so this guard only
        rejects a final URL that positively matches an *exclude* term.
        """
        text = cls._normalize_url_text(final_url)
        exclude_terms = list(
            request.selection_relevance_exclude_any_terms or []
        )
        if any(str(term).lower() in text for term in exclude_terms):
            return False, "final_url_matches_excluded_term"
        return True, None

    @staticmethod
    def _host_partition_dir(documents_dir: Path, url: str) -> tuple[str, Path]:
        """Return host partition key and target directory for downloaded files."""
        parsed = urlparse(url)
        host = (parsed.hostname or "unknown-host").lower()
        sanitized_host = re.sub(r"[^A-Za-z0-9.-]+", "-", host).strip(".-")
        if not sanitized_host:
            sanitized_host = "unknown-host"
        partition_dir = documents_dir / "by_host" / sanitized_host
        return sanitized_host, partition_dir

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")

    @classmethod
    def _normalize_state_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            return None
        if len(normalized) == 2 and normalized.isalpha():
            return normalized
        return cls._STATE_ALIASES.get(normalized) or cls._slug(normalized)

    @classmethod
    def _infer_jurisdiction_from_query(
        cls, query: str | None
    ) -> tuple[str | None, str | None]:
        if not query:
            return None, None

        query_clean = re.sub(r"\s+", " ", query).strip()
        if not query_clean:
            return None, None

        jurisdiction_match = re.search(
            r"([A-Za-z][A-Za-z\s'\-]+\b(?:County|City|Parish|Borough|Municipality|Town|Village))",
            query_clean,
            flags=re.IGNORECASE,
        )
        jurisdiction = (
            jurisdiction_match.group(1).strip() if jurisdiction_match else None
        )

        lower_query = query_clean.lower()
        state_key: str | None = None
        for state_name, abbrev in cls._STATE_ALIASES.items():
            if re.search(rf"\b{re.escape(state_name)}\b", lower_query):
                state_key = abbrev
                break
        if state_key is None:
            abbrev_match = re.search(r"\b([A-Za-z]{2})\b", query_clean)
            if abbrev_match:
                candidate = abbrev_match.group(1).lower()
                if candidate in set(cls._STATE_ALIASES.values()):
                    state_key = candidate

        return jurisdiction, state_key

    @classmethod
    def _jurisdiction_partition_dir(
        cls,
        documents_dir: Path,
        *,
        state: str | None,
        jurisdiction: str | None,
    ) -> tuple[str, str, Path]:
        state_key = cls._normalize_state_key(state) or "unknown-state"
        jurisdiction_key = (
            cls._slug(jurisdiction or "unknown-jurisdiction")
            or "unknown-jurisdiction"
        )
        partition_dir = (
            documents_dir / "by_jurisdiction" / state_key / jurisdiction_key
        )
        return state_key, jurisdiction_key, partition_dir

    @classmethod
    def _generic_partition_dir(
        cls,
        documents_dir: Path,
        *,
        fields: list[str],
        target_metadata: dict[str, object] | None,
    ) -> tuple[dict[str, str], Path]:
        """Partition by arbitrary target-metadata fields (domain-neutral).

        Produces ``by_<field1>/<value1>/<value2>/...`` and emits a
        ``source_<field>`` metadata entry per field. Missing values fall back
        to ``unknown-<field>``.
        """
        meta = target_metadata or {}
        partition_dir = documents_dir / ("by_" + "_".join(fields))
        source_meta: dict[str, str] = {}
        for field_name in fields:
            raw = meta.get(field_name)
            value_key = cls._slug(str(raw)) if raw else f"unknown-{field_name}"
            value_key = value_key or f"unknown-{field_name}"
            source_meta[f"source_{field_name}"] = value_key
            partition_dir = partition_dir / value_key
        return source_meta, partition_dir

    @classmethod
    def _resolve_partition_dir(
        cls,
        *,
        documents_dir: Path,
        url: str,
        request: DiscoveryRequest,
        target_metadata: dict[str, object] | None = None,
    ) -> tuple[str, dict[str, str], Path]:
        # Generic, config-driven partitioning takes precedence when set.
        if request.partition_by:
            source_meta, partition_dir = cls._generic_partition_dir(
                documents_dir,
                fields=list(request.partition_by),
                target_metadata=target_metadata,
            )
            return ("fields", source_meta, partition_dir)

        requested_mode = (request.partition_mode or "auto").strip().lower()
        mode = (
            requested_mode
            if requested_mode in {"auto", "jurisdiction", "host"}
            else "auto"
        )

        inferred_jurisdiction, inferred_state = (
            cls._infer_jurisdiction_from_query(request.query)
        )
        jurisdiction = request.jurisdiction or inferred_jurisdiction
        state = request.state or inferred_state

        if mode == "jurisdiction":
            state_key, jurisdiction_key, partition_dir = (
                cls._jurisdiction_partition_dir(
                    documents_dir,
                    state=state,
                    jurisdiction=jurisdiction,
                )
            )
            return (
                "jurisdiction",
                {
                    "source_state": state_key,
                    "source_jurisdiction": jurisdiction_key,
                },
                partition_dir,
            )

        if mode == "host":
            host_key, partition_dir = cls._host_partition_dir(
                documents_dir, url
            )
            return ("host", {"source_host": host_key}, partition_dir)

        # auto mode: use jurisdiction partition when both hints exist; fallback to host.
        if jurisdiction and state:
            state_key, jurisdiction_key, partition_dir = (
                cls._jurisdiction_partition_dir(
                    documents_dir,
                    state=state,
                    jurisdiction=jurisdiction,
                )
            )
            return (
                "jurisdiction",
                {
                    "source_state": state_key,
                    "source_jurisdiction": jurisdiction_key,
                },
                partition_dir,
            )

        host_key, partition_dir = cls._host_partition_dir(documents_dir, url)
        return ("host", {"source_host": host_key}, partition_dir)

    def _download_candidates(
        self,
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
        documents_dir: Path,
        max_downloads: int = 10,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
        """Download supported candidate files for non-dry discovery runs."""
        try:
            import requests  # noqa: F401
        except ImportError as exc:
            error = build_error_record(
                RuntimeError(
                    "requests dependency is required for discovery downloads"
                ),
                stage="discovery.download",
                provider="http",
            )
            return [], [error], [f"Download stage unavailable: {exc}"]

        ssl_verify = self._downloads_ssl_verify()
        retry_policy = self._resolve_retry_policy(request)
        downloads: list[dict[str, object]] = []
        errors: list[dict[str, object]] = []
        downloaded_count = 0
        blocked_counts: Counter[str] = Counter()
        warning_counts: Counter[str] = Counter()
        max_concurrent_downloads = max(
            1, int(request.max_concurrent_downloads)
        )
        rate_limiter = _RequestRateLimiter(request.min_request_interval_ms)
        staged_candidates = list(candidates[:max_downloads])
        self._emit_progress(
            request,
            f"download: staging {len(staged_candidates)} candidate(s) for fetch",
        )

        download_notes_extra: list[str] = []
        CompletedT = tuple[
            int,
            dict[str, object],
            dict[str, object] | None,
            bool,
            list[str],
        ]
        completed: list[CompletedT] = []

        # Browser mode: bot-protected sites (Akamai) block requests even with
        # transplanted cookies (TLS fingerprinting), so files are fetched
        # THROUGH a real browser. A single Chrome session is shared and
        # downloads run serially (the driver is not thread-safe).
        browser = None
        if request.browser_mode:
            browser, note = self._open_browser_for_download()
            download_notes_extra.append(note)

        try:
            if browser is not None:
                for idx, candidate in enumerate(staged_candidates, start=1):
                    completed.append(
                        self._download_single_candidate(
                            idx=idx,
                            candidate=candidate,
                            request=request,
                            documents_dir=documents_dir,
                            ssl_verify=ssl_verify,
                            retry_policy=retry_policy,
                            rate_limiter=rate_limiter,
                            browser=browser,
                        )
                    )
            else:
                futures = []
                with ThreadPoolExecutor(
                    max_workers=max_concurrent_downloads
                ) as executor:
                    for idx, candidate in enumerate(
                        staged_candidates, start=1
                    ):
                        futures.append(
                            executor.submit(
                                self._download_single_candidate,
                                idx=idx,
                                candidate=candidate,
                                request=request,
                                documents_dir=documents_dir,
                                ssl_verify=ssl_verify,
                                retry_policy=retry_policy,
                                rate_limiter=rate_limiter,
                            )
                        )
                    for future in as_completed(futures):
                        completed.append(future.result())
        finally:
            if browser is not None:
                browser.close()

        for (
            _,
            download_record,
            maybe_error,
            was_downloaded,
            policy_warning_codes,
        ) in sorted(completed, key=lambda item: item[0]):
            downloads.append(download_record)
            status = str(download_record.get("status") or "unknown")
            url_preview = str(download_record.get("url") or "")
            if len(url_preview) > 80:
                url_preview = f"{url_preview[:77]}..."
            self._emit_progress(
                request,
                f"download: {status} | {url_preview}",
            )
            if maybe_error is not None:
                errors.append(maybe_error)
            if was_downloaded:
                downloaded_count += 1
            blocking_code = download_record.get("policy_blocking_code")
            if isinstance(blocking_code, str) and blocking_code:
                blocked_counts[blocking_code] += 1
            for code in policy_warning_codes:
                warning_counts[code] += 1

        notes = [
            f"Download stage completed: {downloaded_count} file(s) saved from {len(candidates)} candidate(s)."
        ]
        notes.extend(download_notes_extra)
        if not ssl_verify:
            notes.append(
                "Download SSL verification disabled via DISCOVERY_SSL_VERIFY/PSWEEP_SSL_VERIFY."
            )
        notes.append(
            "Download retry policy: "
            f"attempts={retry_policy['max_attempts']}, "
            f"initial_backoff={retry_policy['initial_backoff_seconds']}s, "
            f"max_backoff={retry_policy['max_backoff_seconds']}s."
        )
        notes.append(
            "Download throughput controls: "
            f"max_concurrent_downloads={max_concurrent_downloads}, "
            f"min_request_interval_ms={max(0, int(request.min_request_interval_ms))}."
        )
        notes.extend(
            self._build_policy_notes(
                stage_name="Download",
                blocked_counts=blocked_counts,
                warning_counts=warning_counts,
                request=request,
            )
        )
        return downloads, normalize_error_records(errors), notes

    @staticmethod
    def _apply_post_routing_selection_filters(
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
    ) -> tuple[list[DiscoveryCandidate], list[str]]:
        """Re-apply selection eligibility after routing introduces new URLs.

        Routing connectors can discover URLs that were not present in seeker output.
        This stage ensures routed URLs still obey draft/relevance/domain/document
        constraints before download.
        """
        if not candidates:
            return candidates, []

        selector = DiscoveryEngine._build_selector(request)
        filtered, filter_notes = selector.select(
            [candidates],
            primary_per_target=max(1, len(candidates)),
        )
        notes: list[str] = [
            f"Post-routing selection eligibility reduced {len(candidates)} candidate(s) to {len(filtered)}."
        ]
        notes.extend([f"Post-routing {n}" for n in filter_notes])
        return filtered, notes

    @staticmethod
    def _filter_candidates_for_download(
        candidates: list[DiscoveryCandidate],
    ) -> tuple[list[DiscoveryCandidate], list[str]]:
        """Keep downloads focused on viable discovered candidates.

        Simplification rule:
        - Always honor explicit seed URLs provided by the user.
        - For discovered candidates, skip low-confidence rejected items by default.
        """
        if not candidates:
            return [], []

        discovered = [c for c in candidates if c.source != "seed_url"]
        has_viable_discovered = any(
            c.score.acceptance_class() != "rejected" for c in discovered
        )

        filtered: list[DiscoveryCandidate] = []
        rejected_skipped = 0
        for candidate in candidates:
            if candidate.source == "seed_url":
                filtered.append(candidate)
                continue

            if (
                has_viable_discovered
                and candidate.score.acceptance_class() == "rejected"
            ):
                rejected_skipped += 1
                continue
            filtered.append(candidate)

        notes: list[str] = []
        if rejected_skipped:
            notes.append(
                "Download simplification skipped "
                f"{rejected_skipped} rejected discovered candidate(s); "
                "accepted/review discovered candidates were available."
            )
        elif discovered and not has_viable_discovered:
            notes.append(
                "All discovered candidates scored as rejected; preserving them "
                "to avoid recall loss in strict domains."
            )
        if not filtered and candidates:
            notes.append(
                "No candidates met the default download quality bar; "
                "run ended with discovery-only outputs."
            )
        return filtered, notes

    # ------------------------------------------------------------------
    # Observability helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_candidate_summary(
        candidates: list[DiscoveryCandidate],
    ) -> dict[str, object]:
        """Count candidates by acceptance class and source for the manifest."""
        by_class: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for candidate in candidates:
            cls = candidate.score.acceptance_class()
            by_class[cls] = by_class.get(cls, 0) + 1
            src = candidate.source or "unknown"
            by_source[src] = by_source.get(src, 0) + 1
        return {
            "total": len(candidates),
            "by_acceptance_class": by_class,
            "by_source": by_source,
        }

    @staticmethod
    def _build_download_summary(
        download_records: list[dict[str, object]],
    ) -> dict[str, object]:
        """Aggregate download outcomes and byte counts from download records."""
        counts: dict[str, int] = {}
        total_bytes = 0
        for record in download_records:
            status = str(record.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
            if status == "downloaded":
                total_bytes += int(record.get("bytes") or 0)
        return {
            "total": len(download_records),
            "downloaded": counts.get("downloaded", 0),
            "skipped": sum(
                v for k, v in counts.items() if k.startswith("skipped")
            ),
            "failed": counts.get("failed", 0),
            "total_bytes": total_bytes,
            "by_status": counts,
        }

    @staticmethod
    def _candidate_has_reason(
        candidate: DiscoveryCandidate, reason: str
    ) -> bool:
        return any(note == reason for note in (candidate.reasons or []))

    @staticmethod
    def _qualifying_index_fixture_urls(
        request: DiscoveryRequest,
    ) -> list[str]:
        qualifying_urls: list[str] = []
        seen_urls: set[str] = set()

        for raw_link in request.index_links or []:
            if isinstance(raw_link, str):
                url = raw_link
                link_text = ""
            elif isinstance(raw_link, dict):
                url = str(raw_link.get("url") or "")
                link_text = str(raw_link.get("text") or "")
            else:
                continue

            if not url.lower().startswith(("http://", "https://")):
                continue
            if not NullDiggerConnector._matches_include_patterns(
                url=url,
                link_text=link_text,
                include_url_patterns=request.include_url_patterns,
                include_link_text_patterns=request.include_link_text_patterns,
            ):
                continue
            if not NullDiggerConnector._matches_allowed_domain(
                url, request.allowed_domains
            ):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            qualifying_urls.append(url)

        return qualifying_urls

    @staticmethod
    def _build_unknown_target_metrics(
        *,
        request: DiscoveryRequest,
        seeker_state: dict[str, object],
        target_selection_metrics: list[dict[str, object]],
    ) -> dict[str, object]:
        if not seeker_state.get("enabled"):
            return {
                "applicable": False,
                "reason": "seeker_disabled",
            }

        target_count = len(target_selection_metrics)
        if target_count == 0:
            return {
                "applicable": False,
                "reason": "no_target_queries",
            }

        targets_with_staged_candidates = sum(
            1
            for metric in target_selection_metrics
            if int(metric.get("selected_candidates") or 0) > 0
        )
        success_rate = (
            targets_with_staged_candidates / target_count
            if target_count
            else None
        )
        threshold = 0.80

        return {
            "applicable": True,
            "measurement_mode": "target_matrix"
            if request.targets
            else "single_query",
            "target_count": target_count,
            "targets_with_staged_candidates": targets_with_staged_candidates,
            "success_rate": round(success_rate, 4)
            if success_rate is not None
            else None,
            "coverage_threshold": threshold,
            "meets_coverage_threshold": bool(
                success_rate is not None and success_rate >= threshold
            ),
            "targets": target_selection_metrics,
        }

    def _build_centralized_page_sweep_metrics(
        self,
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
    ) -> dict[str, object]:
        if request.topology_mode not in {"centralized", "hybrid"}:
            return {
                "applicable": False,
                "reason": "topology_not_centralized",
            }

        qualifying_urls = self._qualifying_index_fixture_urls(request)
        centralized_urls = {
            candidate.url
            for candidate in candidates
            if self._candidate_has_reason(
                candidate, ROUTE_REASON_CENTRALIZED
            )
        }

        recovered_link_count = sum(
            1 for url in qualifying_urls if url in centralized_urls
        )
        recovery_rate = (
            recovered_link_count / len(qualifying_urls)
            if qualifying_urls
            else None
        )
        threshold = 0.95

        return {
            "applicable": True,
            "measurement_mode": "seeded_index_links"
            if request.index_links
            else "live_hub_pages",
            "hub_page_count": len(request.hub_pages or []),
            "qualifying_link_count": len(qualifying_urls),
            "recovered_link_count": recovered_link_count,
            "recovery_rate": round(recovery_rate, 4)
            if recovery_rate is not None
            else None,
            "coverage_threshold": threshold,
            "meets_coverage_threshold": bool(
                recovery_rate is not None and recovery_rate >= threshold
            ),
        }

    def _build_hybrid_mixed_source_metrics(
        self,
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
    ) -> dict[str, object]:
        if request.topology_mode != "hybrid":
            return {
                "applicable": False,
                "reason": "topology_not_hybrid",
            }

        centralized_count = sum(
            1
            for candidate in candidates
            if self._candidate_has_reason(
                candidate, ROUTE_REASON_CENTRALIZED
            )
        )
        distributed_count = sum(
            1
            for candidate in candidates
            if self._candidate_has_reason(
                candidate, ROUTE_REASON_DISTRIBUTED
            )
        )
        both_paths_resolved = centralized_count > 0 and distributed_count > 0

        return {
            "applicable": True,
            "centralized_candidate_count": centralized_count,
            "distributed_candidate_count": distributed_count,
            "both_paths_resolved": both_paths_resolved,
            "coverage_threshold": "both_paths_required",
            "meets_coverage_threshold": both_paths_resolved,
        }

    def _build_acceptance_metrics(
        self,
        *,
        request: DiscoveryRequest,
        seeker_state: dict[str, object],
        target_selection_metrics: list[dict[str, object]],
        candidates: list[DiscoveryCandidate],
    ) -> dict[str, object]:
        return {
            "unknown_target_discovery": self._build_unknown_target_metrics(
                request=request,
                seeker_state=seeker_state,
                target_selection_metrics=target_selection_metrics,
            ),
            "centralized_page_sweep": self._build_centralized_page_sweep_metrics(
                request=request,
                candidates=candidates,
            ),
            "hybrid_mixed_source_resolution": self._build_hybrid_mixed_source_metrics(
                request=request,
                candidates=candidates,
            ),
        }

    @staticmethod
    def _open_browser_for_download() -> tuple[object | None, str]:
        """Open a shared headless-browser session for downloads, if possible.

        Returns ``(session_or_None, note)``. On any failure (Selenium missing,
        Chrome won't start) returns ``(None, reason)`` and the caller falls
        back to the normal requests downloader.
        """
        try:
            from .browser import BrowserSession, BrowserUnavailableError
        except ImportError:
            return None, "Browser mode requested but Selenium is not installed."
        try:
            session = BrowserSession()
            session._start()  # noqa: SLF001 - own the lifecycle here
        except BrowserUnavailableError as exc:
            return None, f"Browser mode unavailable ({exc}); used direct HTTP."
        return session, "Browser mode: downloading via headless Chrome."

    @staticmethod
    def _inherit_target_metadata(
        artifact: object,
        candidate_by_url: dict[str, DiscoveryCandidate],
    ) -> dict[str, object] | None:
        """Attribute a crawled artifact to the seed target that produced it.

        The digger stamps ``source_seed`` on discovered artifacts; the seed
        URL maps back to the originating candidate (and its target metadata).
        """
        metadata = getattr(artifact, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        seed_url = metadata.get("source_seed")
        if not seed_url:
            return None
        seed_candidate = candidate_by_url.get(str(seed_url))
        if seed_candidate is None:
            return None
        return seed_candidate.target_metadata

    @staticmethod
    def _copy_candidate_with_reason(
        candidate: DiscoveryCandidate,
        reason: str,
    ) -> DiscoveryCandidate:
        reasons = list(candidate.reasons)
        if reason not in reasons:
            reasons.append(reason)
        return DiscoveryCandidate(
            url=candidate.url,
            source=candidate.source,
            score=candidate.score,
            reasons=reasons,
            status=candidate.status,
            mime_type=candidate.mime_type,
            extension=candidate.extension,
            canonical_url=candidate.canonical_url,
            content_hash=candidate.content_hash,
            target_metadata=candidate.target_metadata,
        )

    @staticmethod
    def _build_constraints(request: DiscoveryRequest) -> dict[str, object]:
        constraints: dict[str, object] = {
            "partition_mode": request.partition_mode,
        }

        if request.topology_mode:
            constraints["topology_mode"] = request.topology_mode
        if request.digger_provider:
            constraints["digger_provider"] = request.digger_provider
        if request.allowed_domains:
            constraints["allowed_domains"] = request.allowed_domains
        if request.hub_pages:
            constraints["hub_pages"] = request.hub_pages
        if request.include_url_patterns:
            constraints["include_url_patterns"] = request.include_url_patterns
        if request.include_link_text_patterns:
            constraints["include_link_text_patterns"] = (
                request.include_link_text_patterns
            )
        if request.index_page_mode:
            constraints["index_page_mode"] = request.index_page_mode
        if request.index_links is not None:
            constraints["index_link_count"] = len(request.index_links)

        for field_name in (
            "max_depth",
            "max_pages",
            "max_files",
            "timeout_seconds",
        ):
            value = getattr(request, field_name)
            if value is not None:
                constraints[field_name] = value

        constraints["max_concurrent_downloads"] = max(
            1, int(request.max_concurrent_downloads)
        )
        constraints["min_request_interval_ms"] = max(
            0, int(request.min_request_interval_ms)
        )

        constraints["retry_policy"] = {
            "max_attempts": max(1, int(request.retry_max_attempts)),
            "initial_backoff_seconds": max(
                0.0, float(request.retry_initial_backoff_seconds)
            ),
            "max_backoff_seconds": max(
                0.0, float(request.retry_max_backoff_seconds)
            ),
        }
        constraints["policy"] = {
            "robots_policy_mode": request.robots_policy_mode,
            "tos_policy_mode": request.tos_policy_mode,
            "acknowledged_tos_domains": list(
                request.acknowledged_tos_domains or []
            ),
        }
        if request.targets:
            constraints["target_count"] = len(request.targets)
        if request.use_query_family:
            constraints["use_query_family"] = request.use_query_family
        if request.query_templates:
            constraints["query_template_count"] = len(request.query_templates)
        if request.query_families:
            constraints["query_family_count"] = len(request.query_families)
        constraints["link_prioritization"] = {
            "mode": request.link_prioritization_mode,
            "top_k": request.link_top_k,
            "keywords": list(request.link_prioritization_keywords or []),
            "power_range_kw": list(request.power_range_kw)
            if request.power_range_kw
            else None,
        }

        return constraints

    def _route_distributed_candidates(
        self,
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
    ) -> tuple[
        list[DiscoveryCandidate],
        list[dict[str, object]],
        list[str],
        dict[str, object],
    ]:
        notes: list[str] = []
        errors: list[dict[str, object]] = []
        routing_state: dict[str, object] = {
            "mode": "distributed",
            "applied": False,
            "connector": request.digger_provider,
        }

        if not candidates:
            notes.append(
                "Distributed routing skipped because no candidates were available."
            )
            return candidates, errors, notes, routing_state

        # Only route crawlable HTML pages (non-document URLs) from domains
        # configured for deep crawling. PDFs and other direct-download files
        # don't benefit from link-following. This prevents wasting the crawl
        # budget on sites that serve content directly (no navigation needed).
        from .connectors.digger import HttpDiggerConnector

        # Configurable: domains that use deep navigation requiring crawling.
        # Falls back to link_prioritization domain_scores keys (which are
        # code-hosting sites by convention).
        crawl_domains = getattr(request, "crawl_domains", None) or list(
            (request.link_prioritization_domain_scores or {}).keys()
        )
        crawlable_urls: list[str] = []
        for candidate in candidates:
            url = candidate.url
            if HttpDiggerConnector._is_document_url(url):
                continue  # PDFs skip routing — they'll be downloaded directly
            if not crawl_domains:
                crawlable_urls.append(url)
                continue
            host = (urlparse(url).hostname or "").lower()
            if any(domain in host for domain in crawl_domains):
                crawlable_urls.append(url)

        if not crawlable_urls:
            notes.append(
                "Distributed routing skipped: no crawlable HTML pages from allowed domains."
            )
            # Filter by allowed_domains to maintain domain boundary.
            if request.allowed_domains:
                from .connectors.digger import NullDiggerConnector
                candidates = [
                    c for c in candidates
                    if NullDiggerConnector._matches_allowed_domain(
                        c.url, request.allowed_domains
                    )
                ]
            return candidates, errors, notes, routing_state

        notes.append(
            f"Distributed routing: {len(crawlable_urls)} crawlable page(s) "
            f"from {len(candidates)} candidate(s) sent to digger."
        )

        ssl_verify = self._downloads_ssl_verify()
        filtered_seed_urls, policy_notes = self._filter_urls_for_policy(
            urls=crawlable_urls,
            request=request,
            ssl_verify=ssl_verify,
            stage_name="Routing",
        )
        notes.extend(policy_notes)
        if not filtered_seed_urls:
            notes.append(
                "Distributed routing skipped because policy controls filtered all routing URLs."
            )
            return candidates, errors, notes, routing_state

        try:
            connector = resolve_digger_connector(request.digger_provider)
            artifacts = connector.discover(
                DiggerInput(
                    seed_urls=filtered_seed_urls,
                    max_depth=request.max_depth or 2,
                    max_pages=request.max_pages or 50,
                    max_files=request.max_files or 20,
                    timeout_seconds=request.timeout_seconds or 30,
                    allowed_domains=request.allowed_domains,
                    include_url_patterns=request.include_url_patterns,
                    include_link_text_patterns=request.include_link_text_patterns,
                    extra_params={
                        "ssl_verify": ssl_verify,
                        "request_headers": self._resolve_request_headers(
                            request
                        ),
                        "retry": self._resolve_retry_policy(request),
                    },
                )
            )
        except Exception as exc:
            errors.append(
                build_error_record(
                    exc,
                    stage="discovery.routing",
                    provider=request.digger_provider,
                )
            )
            notes.append(
                "Distributed routing failed; falling back to unrouted candidates."
            )
            return (
                candidates,
                normalize_error_records(errors),
                notes,
                routing_state,
            )

        discovery_modes = sorted(
            {
                str(artifact.metadata.get("discovery_mode") or "unknown")
                for artifact in artifacts
                if isinstance(artifact.metadata, dict)
            }
        )
        routing_state.update(
            {
                "applied": True,
                "artifact_count": len(artifacts),
                "discovery_modes": discovery_modes,
            }
        )

        if not artifacts:
            notes.append(
                "Distributed routing produced no digger artifacts; falling back to unrouted candidates."
            )
            return (
                candidates,
                normalize_error_records(errors),
                notes,
                routing_state,
            )

        candidate_by_url: dict[str, DiscoveryCandidate] = {}
        for candidate in candidates:
            candidate_by_url.setdefault(candidate.url, candidate)

        routed_candidates: list[DiscoveryCandidate] = []
        seen_urls: set[str] = set()
        route_reason = ROUTE_REASON_DISTRIBUTED
        for artifact in artifacts:
            if artifact.url in seen_urls:
                continue
            seen_urls.add(artifact.url)
            existing = candidate_by_url.get(artifact.url)
            if existing is not None:
                routed_candidates.append(
                    self._copy_candidate_with_reason(existing, route_reason)
                )
                continue

            # Attribute crawled children back to the seed target that led
            # the digger to them (target provenance for crawl domains).
            inherited_metadata = self._inherit_target_metadata(
                artifact, candidate_by_url
            )
            routed_candidates.append(
                DiscoveryCandidate(
                    url=artifact.url,
                    source=artifact.source,
                    score=CandidateScore(url_signal=0.35, trust_signal=0.55),
                    reasons=[route_reason],
                    mime_type=artifact.mime_type,
                    extension=artifact.extension,
                    target_metadata=inherited_metadata,
                )
            )

        notes.append(
            f"Distributed routing staged {len(routed_candidates)} candidate(s) through digger."
        )

        # Merge digger artifacts with original non-crawled candidates (PDFs
        # and other direct-download files that bypassed the digger).
        for candidate in candidates:
            if candidate.url not in seen_urls:
                routed_candidates.append(candidate)
                seen_urls.add(candidate.url)

        return (
            routed_candidates,
            normalize_error_records(errors),
            notes,
            routing_state,
        )

    def _route_centralized_candidates(
        self,
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
    ) -> tuple[
        list[DiscoveryCandidate],
        list[dict[str, object]],
        list[str],
        dict[str, object],
    ]:
        notes: list[str] = []
        errors: list[dict[str, object]] = []
        routing_state: dict[str, object] = {
            "mode": "centralized",
            "applied": False,
            "connector": request.digger_provider,
        }

        hub_pages = list(request.hub_pages or [])
        seed_urls = hub_pages or [candidate.url for candidate in candidates]
        if not seed_urls:
            notes.append(
                "Centralized routing skipped because no hub pages or candidates were available."
            )
            return candidates, errors, notes, routing_state

        ssl_verify = self._downloads_ssl_verify()
        filtered_seed_urls, policy_notes = self._filter_urls_for_policy(
            urls=seed_urls,
            request=request,
            ssl_verify=ssl_verify,
            stage_name="Routing",
        )
        notes.extend(policy_notes)
        if not filtered_seed_urls:
            notes.append(
                "Centralized routing skipped because policy controls filtered all hub pages or routing URLs."
            )
            return candidates, errors, notes, routing_state

        index_page_mode = {
            "enabled": True,
            "collect_all_matching_links": True,
        }
        if isinstance(request.index_page_mode, dict):
            index_page_mode.update(request.index_page_mode)

        extra_params: dict[str, object] = {
            "index_page_mode": index_page_mode,
        }
        if request.index_links is not None:
            extra_params["index_links"] = request.index_links

        try:
            connector = resolve_digger_connector(request.digger_provider)
            artifacts = connector.discover(
                DiggerInput(
                    seed_urls=filtered_seed_urls,
                    max_depth=request.max_depth or 2,
                    max_pages=request.max_pages or 50,
                    max_files=request.max_files or 20,
                    timeout_seconds=request.timeout_seconds or 30,
                    allowed_domains=request.allowed_domains,
                    include_url_patterns=request.include_url_patterns,
                    include_link_text_patterns=request.include_link_text_patterns,
                    extra_params={
                        **extra_params,
                        "ssl_verify": ssl_verify,
                        "request_headers": self._resolve_request_headers(
                            request
                        ),
                        "retry": self._resolve_retry_policy(request),
                    },
                )
            )
        except Exception as exc:
            errors.append(
                build_error_record(
                    exc,
                    stage="discovery.routing",
                    provider=request.digger_provider,
                )
            )
            notes.append(
                "Centralized routing failed; falling back to unrouted candidates."
            )
            return (
                candidates,
                normalize_error_records(errors),
                notes,
                routing_state,
            )

        discovery_modes = sorted(
            {
                str(artifact.metadata.get("discovery_mode") or "unknown")
                for artifact in artifacts
                if isinstance(artifact.metadata, dict)
            }
        )
        routing_state.update(
            {
                "applied": True,
                "artifact_count": len(artifacts),
                "discovery_modes": discovery_modes,
                "hub_page_count": len(hub_pages),
                "index_link_count": len(request.index_links or []),
            }
        )

        if not artifacts:
            notes.append(
                "Centralized routing produced no sweep artifacts; falling back to unrouted candidates."
            )
            return (
                candidates,
                normalize_error_records(errors),
                notes,
                routing_state,
            )

        candidate_by_url: dict[str, DiscoveryCandidate] = {}
        for candidate in candidates:
            candidate_by_url.setdefault(candidate.url, candidate)

        routed_candidates: list[DiscoveryCandidate] = []
        seen_urls: set[str] = set()
        route_reason = ROUTE_REASON_CENTRALIZED
        for artifact in artifacts:
            if artifact.url in seen_urls:
                continue
            seen_urls.add(artifact.url)
            existing = candidate_by_url.get(artifact.url)
            if existing is not None:
                routed_candidates.append(
                    self._copy_candidate_with_reason(existing, route_reason)
                )
                continue

            inherited_metadata = self._inherit_target_metadata(
                artifact, candidate_by_url
            )
            routed_candidates.append(
                DiscoveryCandidate(
                    url=artifact.url,
                    source=artifact.source,
                    score=CandidateScore(url_signal=0.35, trust_signal=0.55),
                    reasons=[route_reason],
                    mime_type=artifact.mime_type,
                    extension=artifact.extension,
                    target_metadata=inherited_metadata,
                )
            )

        notes.append(
            f"Centralized routing staged {len(routed_candidates)} candidate(s) through hub sweep."
        )
        return (
            routed_candidates,
            normalize_error_records(errors),
            notes,
            routing_state,
        )

    @staticmethod
    def _dedupe_candidates_by_url(
        candidates: list[DiscoveryCandidate],
    ) -> list[DiscoveryCandidate]:
        deduped: list[DiscoveryCandidate] = []
        seen_urls: set[str] = set()
        for candidate in candidates:
            if candidate.url in seen_urls:
                continue
            seen_urls.add(candidate.url)
            deduped.append(candidate)
        return deduped

    def _route_hybrid_candidates(
        self,
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
    ) -> tuple[
        list[DiscoveryCandidate],
        list[dict[str, object]],
        list[str],
        dict[str, object],
    ]:
        notes: list[str] = []
        errors: list[dict[str, object]] = []
        routing_state: dict[str, object] = {
            "mode": "hybrid",
            "applied": True,
            "stages": [],
        }

        (
            centralized_candidates,
            centralized_errors,
            centralized_notes,
            centralized_state,
        ) = self._route_centralized_candidates(
            request=request,
            candidates=candidates,
        )
        errors.extend(centralized_errors)
        notes.extend(centralized_notes)
        routing_state["stages"].append({"centralized": centralized_state})

        centralized_routed_candidates = centralized_candidates
        if int(centralized_state.get("artifact_count") or 0) == 0:
            centralized_routed_candidates = []

        centralized_urls = {
            candidate.url for candidate in centralized_routed_candidates
        }
        fallback_candidates = [
            candidate
            for candidate in candidates
            if candidate.url not in centralized_urls
        ]

        (
            distributed_candidates,
            distributed_errors,
            distributed_notes,
            distributed_state,
        ) = self._route_distributed_candidates(
            request=request,
            candidates=fallback_candidates,
        )
        errors.extend(distributed_errors)
        notes.extend(distributed_notes)
        routing_state["stages"].append({"distributed": distributed_state})

        # Use the distributed results directly. When routing was skipped
        # (e.g., all candidates are PDFs), distributed_candidates contains
        # the original candidates filtered by allowed_domains.
        distributed_routed_candidates = distributed_candidates

        routing_state["centralized_candidate_count"] = len(
            centralized_routed_candidates
        )
        routing_state["distributed_candidate_count"] = len(
            distributed_routed_candidates
        )

        combined_candidates = self._dedupe_candidates_by_url(
            [*centralized_routed_candidates, *distributed_routed_candidates]
        )
        routing_state["final_candidate_count"] = len(combined_candidates)

        if combined_candidates:
            notes.append(
                f"Hybrid routing produced {len(combined_candidates)} candidate(s) after centralized-plus-distributed sequencing."
            )
            return (
                combined_candidates,
                normalize_error_records(errors),
                notes,
                routing_state,
            )

        notes.append(
            "Hybrid routing produced no routed candidates; falling back to unrouted candidates."
        )
        return (
            candidates,
            normalize_error_records(errors),
            notes,
            routing_state,
        )

    @staticmethod
    def _write_download_index(
        *,
        request: DiscoveryRequest,
        run_id: str,
        manifest_path: Path,
        download_records: list[dict[str, object]],
    ) -> Path:
        """Write run-level download index CSV for downstream automation."""
        index_path = manifest_path.parent / "download_index.csv"
        fieldnames = [
            "run_id",
            "domain",
            "target_label",
            "partition_mode",
            "source_state",
            "source_jurisdiction",
            "source_host",
            "status",
            "classification_passed",
            "url",
            "final_url",
            "mime_type",
            "bytes",
            "relative_path",
            "path",
            "review_selected",
            "review_is_primary",
            "review_relevance",
            "review_doc_kind",
            "error",
            "target_metadata",
        ]

        with index_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for record in download_records:
                raw_meta = record.get("target_metadata")
                writer.writerow(
                    {
                        "run_id": run_id,
                        "domain": request.domain,
                        "target_label": record.get("target_label"),
                        "partition_mode": record.get("partition_mode"),
                        "source_state": record.get("source_state"),
                        "source_jurisdiction": record.get(
                            "source_jurisdiction"
                        ),
                        "source_host": record.get("source_host"),
                        "status": record.get("status"),
                        "classification_passed": record.get(
                            "classification_passed"
                        ),
                        "url": record.get("url"),
                        "final_url": record.get("final_url"),
                        "mime_type": record.get("mime_type"),
                        "bytes": record.get("bytes"),
                        "relative_path": record.get("relative_path"),
                        "path": record.get("path"),
                        "review_selected": record.get("review_selected"),
                        "review_is_primary": record.get("review_is_primary"),
                        "review_relevance": record.get("review_relevance"),
                        "review_doc_kind": record.get("review_doc_kind"),
                        "error": record.get("error"),
                        "target_metadata": (
                            json.dumps(raw_meta) if raw_meta else None
                        ),
                    }
                )
        return index_path

    @staticmethod
    def _write_review_index(
        *,
        request: DiscoveryRequest,
        download_records: list[dict[str, object]],
        run_dir: Path,
    ) -> Path:
        """Write the human-editable review ledger (``review.csv``).

        One row per downloaded file with the LLM's verdict and two blank
        columns — ``human_decision`` (``keep``/``reject``) and ``human_notes`` —
        for a person to override the automated curation. ``curate`` re-reads this
        file to rebuild ``curated/``. Rows are ordered so each target's most
        relevant candidates sort to the top for quick scanning.
        """
        review_path = run_dir / "review.csv"
        fieldnames = [
            "target",
            "partition",
            "state",
            "jurisdiction",
            "doc_kind",
            "relevance",
            "llm_is_primary",
            "llm_selected",
            "human_decision",
            "human_notes",
            "llm_reason",
            "relative_path",
            "path",
        ]
        rows = [
            r
            for r in download_records
            if r.get("status") == "downloaded" and r.get("path")
        ]
        rows.sort(
            key=lambda r: (
                str(r.get("target_label") or ""),
                -float(r.get("review_relevance") or 0.0),
            )
        )
        with review_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for record in rows:
                rel = record.get("relative_path") or ""
                partition = (
                    str(rel).rsplit("/", 1)[0] if "/" in str(rel) else ""
                )
                writer.writerow(
                    {
                        "target": record.get("target_label"),
                        "partition": partition,
                        "state": record.get("source_state"),
                        "jurisdiction": record.get("source_jurisdiction"),
                        "doc_kind": record.get("review_doc_kind"),
                        "relevance": record.get("review_relevance"),
                        "llm_is_primary": record.get("review_is_primary"),
                        "llm_selected": record.get("review_selected"),
                        "human_decision": "",
                        "human_notes": "",
                        "llm_reason": record.get("review_reason"),
                        "relative_path": rel,
                        "path": record.get("path"),
                    }
                )
        return review_path

    @staticmethod
    def _materialize_curated(
        *,
        documents_dir: Path,
        download_records: list[dict[str, object]],
    ) -> tuple[Path, int]:
        """Populate ``curated/`` with the final-selected documents.

        Mirrors each selected file's partition layout under a sibling
        ``curated/`` directory using hardlinks (falling back to copies across
        filesystems), so downstream ``extract`` can consume only the curated set
        without duplicating storage. Returns ``(curated_dir, count)``.

        Selection rule: when LLM review ran (any record carries
        ``review_selected``), curate only the selected primaries. When no review
        was configured, curate every successfully-downloaded file — so domains
        without ``document_review`` still get a populated curated set.
        """
        import shutil

        curated_dir = documents_dir.parent / "curated"
        # Rebuild from scratch so the curated set always reflects the current
        # decisions (no stale files from a previous run/curate pass).
        if curated_dir.exists():
            shutil.rmtree(curated_dir, ignore_errors=True)

        review_ran = any(
            "review_selected" in record for record in download_records
        )

        def _is_curated(record: dict[str, object]) -> bool:
            if review_ran:
                return bool(record.get("review_selected"))
            return record.get("status") == "downloaded"

        def _link(src_file: Path, dest_file: Path) -> None:
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            if dest_file.exists():
                dest_file.unlink()
            try:
                dest_file.hardlink_to(src_file)
            except (OSError, AttributeError):
                shutil.copy2(src_file, dest_file)

        count = 0
        for record in download_records:
            if not _is_curated(record):
                continue
            src = Path(str(record.get("path") or ""))
            rel = record.get("relative_path")
            if not src.exists() or not rel:
                continue
            dest = curated_dir / str(rel)
            try:
                _link(src, dest)
                # Carry over the cached extracted/OCR text so the extraction
                # stage reading from curated/ reuses it instead of re-OCRing.
                for suffix in (".txt", ".meta.json"):
                    cache_src = src.parent / ".text" / f"{src.stem}{suffix}"
                    if cache_src.exists():
                        _link(cache_src, dest.parent / ".text" / cache_src.name)
                count += 1
            except OSError:
                continue
        return curated_dir, count

    @staticmethod
    def _promote_to_consolidated_curated(
        *,
        run_curated_dir: Path,
        domain_dir: Path,
    ) -> None:
        """Copy curated documents to the domain-level consolidated directory.

        The consolidated directory (``discovered/<domain>/curated/``) accumulates
        curated documents across all discovery runs. The partition structure
        (``by_state_jurisdiction/<state>/<jurisdiction>/``) ensures newer
        curations for the same target overwrite older ones without conflicts.

        This allows extraction to read from ONE stable directory regardless of
        how many discovery runs contributed documents.
        """
        import shutil

        consolidated = domain_dir / "curated"
        consolidated.mkdir(parents=True, exist_ok=True)

        for src_file in run_curated_dir.rglob("*"):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(run_curated_dir)
            dest = consolidated / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                if dest.exists():
                    dest.unlink()
                dest.hardlink_to(src_file)
            except (OSError, AttributeError):
                shutil.copy2(src_file, dest)

        # Merge the run's download_index.csv into a domain-level index so
        # extraction can resolve source URLs for provenance/citation.
        run_dir = run_curated_dir.parent
        run_index = run_dir / "download_index.csv"
        if run_index.is_file():
            DiscoveryEngine._merge_download_index(
                run_index=run_index,
                domain_index=domain_dir / "download_index.csv",
            )

    @staticmethod
    def _merge_download_index(
        *,
        run_index: Path,
        domain_index: Path,
    ) -> None:
        """Merge a run's download_index.csv into the domain-level index.

        Keyed by relative_path — newer entries overwrite older ones for the
        same file. This ensures extraction's source-context lookup finds
        URLs for ALL curated documents regardless of which run produced them.
        """
        import csv

        existing: dict[str, dict[str, str]] = {}
        fieldnames: list[str] = []

        if domain_index.is_file():
            with domain_index.open(encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                fieldnames = list(reader.fieldnames or [])
                for row in reader:
                    key = row.get("relative_path") or row.get("path", "")
                    if key:
                        existing[key] = dict(row)

        with run_index.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if not fieldnames:
                fieldnames = list(reader.fieldnames or [])
            for row in reader:
                key = row.get("relative_path") or row.get("path", "")
                if key:
                    existing[key] = dict(row)

        with domain_index.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in existing.values():
                writer.writerow(row)

    @staticmethod
    def _refresh_latest_pointer(run_dir: Path) -> None:
        """Point ``<domain>/latest`` at this run (symlink, txt fallback)."""
        import os

        domain_dir = run_dir.parent.parent  # .../<domain>/runs/<id> -> <domain>
        link_path = domain_dir / "latest"
        target = Path("runs") / run_dir.name  # relative for portability
        try:
            tmp = domain_dir / ".latest.tmp"
            if tmp.exists() or tmp.is_symlink():
                tmp.unlink()
            os.symlink(target, tmp, target_is_directory=True)
            os.replace(tmp, link_path)
        except OSError:
            # Filesystems without symlink support: leave a text pointer instead.
            try:
                (domain_dir / "latest.txt").write_text(
                    run_dir.name + "\n", encoding="utf-8"
                )
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Checkpointing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _target_checkpoint_key(target: dict[str, object]) -> str:
        """Stable key for a target row used in checkpoint tracking."""
        label = target.get("label")
        if label:
            return str(label)
        return hashlib.md5(
            json.dumps(target, sort_keys=True, default=str).encode()
        ).hexdigest()[:12]

    @staticmethod
    def _checkpoint_path(manifest_path: Path) -> Path:
        """Path to the domain-level checkpoint file."""
        return manifest_path.parent.parent.parent / "checkpoint.json"

    @staticmethod
    def _load_checkpoint(path: Path) -> dict[str, dict[str, object]]:
        """Load completed target keys from checkpoint file."""
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("entries", {}) or {}
        except (json.JSONDecodeError, OSError, ValueError):
            return {}

    @staticmethod
    def _save_checkpoint_entries(
        path: Path,
        new_entries: dict[str, dict[str, object]],
    ) -> None:
        """Merge new_entries into the checkpoint file atomically."""
        existing: dict[str, object] = {}
        if path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError, ValueError):
                existing = json.loads(
                    path.read_text(encoding="utf-8")
                ).get("entries", {}) or {}
        existing.update(new_entries)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "entries": existing}, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Post-download document classification
    # ------------------------------------------------------------------

    @staticmethod
    def _run_post_download_classifier(
        downloads: list[dict[str, object]],
        classifier_cfg: dict[str, object],
        notes: list[str],
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Run keyword-based content classification on downloaded files.

        Reads the first pages of each downloaded file and checks for
        required_keywords.  Records that fail and have action='filter'
        get their status changed to 'rejected_classifier'.
        """
        from .validators import ContentSampler

        required = list(classifier_cfg.get("required_keywords") or [])
        nice_to_have = list(
            classifier_cfg.get("nice_to_have_keywords") or []
        )
        min_matches = int(classifier_cfg.get("min_required_matches", 1))
        action = str(classifier_cfg.get("action", "warn"))
        passed = failed = errors = 0
        for record in downloads:
            if record.get("status") != "downloaded":
                continue
            file_path = record.get("path")
            if not file_path:
                continue
            try:
                result = ContentSampler.validate_content(
                    str(file_path), required, nice_to_have, min_matches
                )
                record["classification_passed"] = result.success
                record["classification_score"] = result.confidence_score
                if not result.success:
                    failed += 1
                    if action == "filter":
                        record["status"] = "rejected_classifier"
                else:
                    passed += 1
            except Exception as exc:
                record["classification_error"] = str(exc)
                errors += 1
        notes.append(
            f"Post-download classifier: {passed} passed, {failed} failed"
            + (f", {errors} error(s)" if errors else "")
            + f" (action={action})."
        )
        return downloads, notes

    def _escalate_js_shells_to_browser(
        self,
        downloads: list[dict[str, object]],
        notes: list[str],
        request: DiscoveryRequest,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Re-fetch HTML files detected as JS-rendered shells via browser.

        After initial download, some HTML files from SPA sites (municode.com,
        ecode360.com, etc.) contain only a JavaScript bootstrap with no
        server-rendered content. This method:

        1. Identifies HTML files with insufficient extracted text.
        2. Launches a headless browser to render the page.
        3. Saves the rendered HTML (with actual content) over the shell.
        4. Re-validates the replacement file.

        Configurable via ``discovery.browser_escalation`` in run.yaml:
            settle_seconds: Time to wait for SPA to render (default: 10)
            min_shell_chars: Below this = JS shell detected (default: 200)
            min_rendered_chars: Rendered text must exceed this (default: 1000)
            enabled: true/false (default: true)

        This is a general-purpose escalation that works for any domain without
        site-specific configuration.
        """
        from pathlib import Path

        from .validators import ContentSampler

        # Load configurable thresholds from discovery config
        escalation_cfg = getattr(request, "browser_escalation", None) or {}
        if isinstance(escalation_cfg, bool):
            escalation_cfg = {"enabled": escalation_cfg}
        if not escalation_cfg.get("enabled", True):
            return downloads, notes

        settle_seconds = float(escalation_cfg.get("settle_seconds", 10))
        min_shell_chars = int(escalation_cfg.get("min_shell_chars", 200))
        min_rendered_chars = int(escalation_cfg.get("min_rendered_chars", 1000))

        html_extensions = {".html", ".htm"}
        candidates_for_escalation: list[dict[str, object]] = []

        for record in downloads:
            if record.get("status") != "downloaded":
                continue
            file_path = record.get("path")
            if not file_path:
                continue
            path = Path(str(file_path))
            if path.suffix.lower() not in html_extensions:
                continue

            # Check if the file has insufficient content (JS shell signature)
            try:
                text = ContentSampler.extract_text(str(path))
                if len(text.strip()) < min_shell_chars:
                    candidates_for_escalation.append(record)
            except Exception:
                continue

        if not candidates_for_escalation:
            return downloads, notes

        # Attempt browser rendering for shell files
        browser = None
        escalated = 0
        try:
            browser, browser_note = self._open_browser_for_download()
            if browser is None:
                notes.append(
                    f"Browser escalation: {len(candidates_for_escalation)} "
                    f"JS-rendered HTML file(s) detected but browser unavailable "
                    f"({browser_note}). Content may be incomplete."
                )
                return downloads, notes

            for record in candidates_for_escalation:
                file_path = Path(str(record["path"]))
                url = str(record.get("url") or record.get("final_url") or "")
                if not url:
                    continue

                try:
                    rendered_html = browser.fetch_html(
                        url, settle_seconds=settle_seconds
                    )
                    if not rendered_html or len(rendered_html.strip()) < 500:
                        continue

                    # Save rendered HTML over the shell
                    file_path.write_text(rendered_html, encoding="utf-8")

                    # Verify the rendered version has meaningful content
                    # (not just navigation/TOC from a lazy-loading SPA).
                    text = ContentSampler.extract_text(str(file_path))
                    if len(text.strip()) >= min_rendered_chars:
                        escalated += 1
                        record["browser_escalated"] = True

                        # Cache extracted text as .text/ file (same pattern as
                        # OCR'd PDFs). Users can inspect the .txt instead of
                        # opening raw HTML, and the extraction pipeline reuses
                        # it without re-parsing.
                        from ..extraction.document_utils import write_text_cache

                        write_text_cache(
                            file_path, text, method="browser_render"
                        )
                    else:
                        # Page rendered but insufficient content (likely a TOC).
                        # Try following keyword-matching links 1 level deeper.
                        classifier_cfg = getattr(request, "document_classifier", None) or {}
                        keywords = classifier_cfg.get("nice_to_have_keywords") or []
                        if keywords and rendered_html:
                            deeper_url = self._find_keyword_link_in_rendered(
                                rendered_html, url, keywords
                            )
                            if deeper_url:
                                try:
                                    deeper_html = browser.fetch_html(
                                        deeper_url, settle_seconds=settle_seconds
                                    )
                                    if deeper_html:
                                        deeper_text = ContentSampler.extract_text_from_string(deeper_html)
                                        if len(deeper_text.strip()) >= min_rendered_chars:
                                            # Save the deeper page as a NEW file
                                            deeper_path = file_path.parent / f"{file_path.stem}-deep{file_path.suffix}"
                                            deeper_path.write_text(deeper_html, encoding="utf-8")
                                            write_text_cache(deeper_path, deeper_text, method="browser_render")
                                            # Add as a new download record
                                            new_record = dict(record)
                                            new_record["path"] = str(deeper_path)
                                            new_record["relative_path"] = str(
                                                Path(record.get("relative_path", "")).parent / deeper_path.name
                                            )
                                            new_record["url"] = deeper_url
                                            new_record["browser_escalated"] = True
                                            new_record["status"] = "downloaded"
                                            downloads.append(new_record)
                                            escalated += 1
                                except Exception:
                                    pass
                        record["browser_escalated"] = False
                except Exception:
                    record["browser_escalated"] = False
                    continue
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

        if escalated:
            notes.append(
                f"Browser escalation: re-rendered {escalated} of "
                f"{len(candidates_for_escalation)} JS-shell HTML file(s) "
                f"via headless browser."
            )
        elif candidates_for_escalation:
            notes.append(
                f"Browser escalation: {len(candidates_for_escalation)} "
                f"JS-rendered HTML file(s) detected; browser rendering "
                f"did not yield additional content."
            )

        return downloads, notes

    def _retry_js_shells_with_digger(
        self,
        *,
        request: DiscoveryRequest,
        download_records: list[dict[str, object]],
        notes: list[str],
        documents_dir: Path,
        review_cfg: dict[str, object],
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Browser-crawl retry for failed targets with JS-shell downloads.

        After review, some targets have 0 curated docs but DO have downloaded
        HTML files that were identified as JS shells (municode, etc.). For these,
        launch the HTTP digger (with browser fallback) on the code-hosting URL
        to crawl 2-3 levels deep and find keyword-matching section pages.

        This is the hybrid approach: fast SerpApi for most targets, targeted
        browser crawl only for the ~15% that need it.
        """
        from pathlib import Path as _Path
        from collections import defaultdict

        classifier_cfg = getattr(request, "document_classifier", None) or {}
        keywords = classifier_cfg.get("nice_to_have_keywords") or []
        if not keywords:
            return download_records, notes

        domain_scores = request.link_prioritization_domain_scores or {}
        code_hosting_domains = set(domain_scores.keys())
        if not code_hosting_domains:
            return download_records, notes

        # Find targets with 0 curated AND JS-shell downloads from code-hosting domains
        by_target: dict[str, list[dict[str, object]]] = defaultdict(list)
        for rec in download_records:
            meta = rec.get("target_metadata") or {}
            key = str(meta.get("label") or "")
            if key:
                by_target[key].append(rec)

        shell_urls: list[tuple[str, str, dict[str, object]]] = []
        for target_key, records in by_target.items():
            if any(r.get("review_selected") for r in records):
                continue  # target already has curated docs

            for rec in records:
                if not rec.get("browser_escalated") and rec.get("status") == "downloaded":
                    # Check if this is a JS shell from a code-hosting domain
                    url = str(rec.get("url") or "")
                    from urllib.parse import urlparse
                    host = urlparse(url).hostname or ""
                    if any(d in host for d in code_hosting_domains):
                        file_path = rec.get("path")
                        if file_path and _Path(str(file_path)).suffix.lower() in {".html", ".htm"}:
                            shell_urls.append((target_key, url, rec.get("target_metadata") or {}))
                            break

        if not shell_urls:
            return download_records, notes

        # Use the HTTP digger with browser to crawl from each shell URL
        from .connectors.digger import HttpDiggerConnector
        from .connectors.base import DiggerInput

        digger = HttpDiggerConnector()
        crawled = 0
        for target_key, url, target_meta in shell_urls:
            di = DiggerInput(
                seed_urls=[url],
                max_depth=3,
                max_pages=15,
                max_files=3,
                timeout_seconds=60,
                include_link_text_patterns=keywords,
                extra_params={"ssl_verify": self._downloads_ssl_verify()},
            )
            try:
                artifacts = digger.discover(di)
            except Exception:
                continue

            if not artifacts:
                continue

            # Download and review the found artifacts
            from .models import DiscoveryCandidate, CandidateScore
            artifact_candidates = [
                DiscoveryCandidate(
                    url=a.url,
                    source="browser_crawl_retry",
                    score=CandidateScore(
                        url_signal=0.7, anchor_signal=0.0,
                        content_signal=0.0, trust_signal=0.3,
                    ),
                    target_metadata=target_meta,
                )
                for a in artifacts
            ]

            retry_downloads, _, _ = self._download_candidates(
                request=request,
                candidates=artifact_candidates,
                documents_dir=documents_dir,
                max_downloads=len(artifact_candidates),
            )

            if retry_downloads and review_cfg:
                retry_downloads, _, _ = self._run_document_review(
                    retry_downloads, review_cfg, [],
                    models=getattr(request, "models", None),
                    classifier_keywords=keywords,
                )

            new_curated = sum(1 for r in retry_downloads if r.get("review_selected"))
            download_records.extend(retry_downloads)
            if new_curated:
                crawled += 1

        if crawled or shell_urls:
            notes.append(
                f"Browser crawl retry: crawled {len(shell_urls)} JS-shell URL(s), "
                f"{crawled} target(s) found new curated doc(s)."
            )
        return download_records, notes

    @staticmethod
    def _find_keyword_link_in_rendered(
        html: str, base_url: str, keywords: list[str]
    ) -> str | None:
        """Find the first link in rendered HTML whose text matches any keyword.

        Used after browser escalation renders a TOC page — identifies the
        deepest relevant section link to follow (e.g., "Oil and Gas" chapter
        on a municode TOC page).
        """
        import re
        from html.parser import HTMLParser
        from urllib.parse import urljoin

        class _LinkFinder(HTMLParser):
            def __init__(self):
                super().__init__()
                self.links: list[tuple[str, str]] = []
                self._href: str | None = None
                self._text_parts: list[str] = []

            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    for name, val in attrs:
                        if name == "href" and val:
                            self._href = val
                            self._text_parts = []

            def handle_data(self, data):
                if self._href is not None:
                    self._text_parts.append(data)

            def handle_endtag(self, tag):
                if tag == "a" and self._href:
                    text = " ".join(self._text_parts).strip()
                    if text:
                        self.links.append((self._href, text))
                    self._href = None
                    self._text_parts = []

        parser = _LinkFinder()
        try:
            parser.feed(html)
        except Exception:
            return None

        keywords_lower = [k.lower() for k in keywords]
        for href, text in parser.links:
            text_lower = text.lower()
            if any(kw in text_lower for kw in keywords_lower):
                return urljoin(base_url, href)
        return None

    def _retry_failed_targets_on_code_hosting(
        self,
        *,
        request: DiscoveryRequest,
        download_records: list[dict[str, object]],
        notes: list[str],
        documents_dir: Path,
        review_cfg: dict[str, object],
        classifier_keywords: list[str] | None = None,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Targeted retry for targets that got 0 curated docs from code-hosting sites.

        When a target has candidates from known code-hosting domains (municode,
        ecode360, amlegal, etc.) but none scored high enough for curation, this
        fires one additional SerpApi query scoped to the domain path already
        discovered, combined with domain keywords. This finds deeper sections
        (like specific chapters) that the initial broad query missed.

        Only fires for failed targets — typically 1-2 extra queries per run.
        """
        # Identify code-hosting domains from link_prioritization.domain_scores
        domain_scores = request.link_prioritization_domain_scores or {}
        code_hosting_domains = set(domain_scores.keys())
        if not code_hosting_domains:
            return download_records, notes

        keywords = classifier_keywords or []
        if not keywords:
            return download_records, notes

        # Group records by target, find targets with 0 curated
        from urllib.parse import urlparse
        from collections import defaultdict

        by_target: dict[str, list[dict[str, object]]] = defaultdict(list)
        for rec in download_records:
            meta = rec.get("target_metadata") or {}
            key = str(meta.get("label") or "")
            if key:
                by_target[key].append(rec)

        failed_targets: list[tuple[str, str, dict[str, object]]] = []
        for target_key, records in by_target.items():
            has_curated = any(r.get("review_selected") for r in records)
            if has_curated:
                continue

            # Find code-hosting URLs for this target
            code_hosting_found = False
            for rec in records:
                url = str(rec.get("url") or "")
                parsed = urlparse(url)
                host = parsed.hostname or ""
                matched_domain = None
                for domain in code_hosting_domains:
                    if domain in host:
                        matched_domain = domain
                        break
                if matched_domain:
                    # Extract domain + path prefix for site-scoped retry
                    path_parts = parsed.path.strip("/").split("/")
                    # Keep first 2-3 path segments as the jurisdiction scope
                    scope = "/".join(path_parts[:3]) if len(path_parts) >= 3 else "/".join(path_parts[:2])
                    site_prefix = f"{host}/{scope}" if scope else host
                    meta = rec.get("target_metadata") or {}
                    failed_targets.append((target_key, site_prefix, meta))
                    break

        if not failed_targets:
            return download_records, notes

        # Fire targeted retry queries
        retry_notes: list[str] = []
        keyword_or = " OR ".join(f'"{k}"' for k in keywords[:3])

        from .connectors.serpapi_seeker import SerpApiSeeker
        from .connectors.base import SeekerInput

        seeker = SerpApiSeeker(
            cache_dir=str(documents_dir.parent.parent.parent / ".serpapi_cache"),
            cache_ttl_seconds=86400 * 7,
        )

        retry_candidates = []
        for target_key, site_prefix, target_meta in failed_targets:
            query = f"site:{site_prefix} {keyword_or}"
            try:
                results = seeker.discover(SeekerInput(query=query, max_results=5))
            except Exception as exc:
                notes.append(f"Code-hosting retry: query failed for '{target_key}': {exc}")
                continue

            if not results:
                continue

            for result in results[:2]:
                url = result.get("link") or result.get("url")
                if url:
                    retry_candidates.append({
                        "url": url,
                        "target_metadata": target_meta,
                        "source": "code_hosting_retry",
                    })

            retry_notes.append(
                f"Code-hosting retry for '{target_key}': queried site:{site_prefix}, "
                f"found {len(results)} result(s)."
            )

        if not retry_candidates:
            notes.extend(retry_notes)
            return download_records, notes

        # Download retry candidates
        from .models import DiscoveryCandidate, CandidateScore

        candidates_for_download = [
            DiscoveryCandidate(
                url=c["url"],
                source="code_hosting_retry",
                score=CandidateScore(
                    url_signal=0.6, anchor_signal=0.0,
                    content_signal=0.0, trust_signal=0.4,
                ),
                target_metadata=c.get("target_metadata"),
            )
            for c in retry_candidates
        ]

        retry_downloads, retry_errors, retry_dl_notes = self._download_candidates(
            request=request,
            candidates=candidates_for_download,
            documents_dir=documents_dir,
            max_downloads=len(candidates_for_download),
        )

        # Review retry downloads
        if retry_downloads and review_cfg:
            retry_downloads, retry_review_notes, _ = self._run_document_review(
                retry_downloads,
                review_cfg,
                [],
                models=getattr(request, "models", None),
                classifier_keywords=classifier_keywords,
            )
            retry_notes.extend(retry_review_notes)

        # Merge into main records
        download_records.extend(retry_downloads)
        curated_retry = sum(1 for r in retry_downloads if r.get("review_selected"))
        retry_notes.append(
            f"Code-hosting retry: {len(retry_downloads)} downloaded, "
            f"{curated_retry} curated from retry."
        )
        notes.extend(retry_notes)
        return download_records, notes

    @staticmethod
    def _run_document_review(
        downloads: list[dict[str, object]],
        review_cfg: dict[str, object],
        notes: list[str],
        models: dict[str, object] | None = None,
        classifier_keywords: list[str] | None = None,
    ) -> tuple[list[dict[str, object]], list[str], dict[str, object]]:
        """LLM-grade downloaded files and promote the primary one(s).

        Best-effort curation: annotates records with ``review_*`` fields and,
        in ``move`` mode, relocates the top file(s) per target into a
        ``reviewed/`` subfolder. Never fails the run if the LLM is unavailable.

        Returns (downloads, notes, costs_dict).
        """
        description = str(review_cfg.get("document_description") or "").strip()
        if not description:
            notes.append(
                "Document review skipped (no document_description configured)."
            )
            return downloads, notes, {}

        from .document_reviewer import DocumentReviewer

        reviewer = DocumentReviewer(
            document_description=description,
            model=(str(review_cfg["model"]) if review_cfg.get("model") else None),
            models={str(k): str(v) for k, v in (models or {}).items()} or None,
            keep_top=int(review_cfg.get("keep_top", 1) or 1),
            action=str(review_cfg.get("action", "move")),
            max_chars=int(review_cfg.get("max_chars", 12000) or 12000),
            review_keywords=review_cfg.get("keywords") or classifier_keywords,
        )
        try:
            downloads, notes = reviewer.review(downloads, notes)
            return downloads, notes, reviewer.get_costs()
        except Exception as exc:  # noqa: BLE001 - review is best-effort
            notes.append(f"Document review error (skipped): {exc}")
            return downloads, notes, reviewer.get_costs()

    def run(self, request: DiscoveryRequest) -> DiscoveryResult:
        started_at = datetime.now(timezone.utc)
        run_id = self._build_run_id(request, started_at)
        documents_dir, manifest_path = self._resolve_output_paths(
            request, run_id
        )
        normalized_seed_urls, seed_errors = self._normalize_seed_urls(
            request.seed_urls
        )
        seeker_state, seeker_errors, seeker_notes = (
            self._resolve_serpapi_state(request)
        )
        all_errors = normalize_error_records([*seed_errors, *seeker_errors])
        self._emit_progress(
            request,
            f"run: discovery started (run_id={run_id})",
        )

        # Checkpoint: skip targets that completed in a previous run.
        checkpoint_path = self._checkpoint_path(manifest_path)
        completed_checkpoint_keys = self._load_checkpoint(checkpoint_path)
        if completed_checkpoint_keys and request.targets:
            original_target_count = len(request.targets)
            request = dataclass_replace(
                request,
                targets=[
                    t
                    for t in request.targets
                    if self._target_checkpoint_key(t)
                    not in completed_checkpoint_keys
                ],
            )
            skipped_count = original_target_count - len(request.targets)
            if skipped_count:
                seeker_notes.append(
                    f"Checkpoint: skipped {skipped_count} already-completed "
                    f"target(s) (of {original_target_count} total)."
                )

        # Run seeker discovery when SerpApi is enabled and healthy (no init errors).
        prioritizer_lineage: list[dict[str, object]] = []
        target_selection_metrics: list[dict[str, object]] = []
        seeker_raw_count = 0
        if seeker_errors:
            seeker_candidates: list[DiscoveryCandidate] = []
        else:
            self._emit_progress(request, "run: seeker stage started")
            (
                seeker_candidates,
                seeker_notes,
                all_errors,
                prioritizer_lineage,
                target_selection_metrics,
            ) = self._run_seeker(
                request, seeker_state, seeker_notes, all_errors
            )
            self._emit_progress(
                request,
                f"run: seeker stage completed with {len(seeker_candidates)} candidate(s)",
            )
            seeker_raw_count = (
                len(prioritizer_lineage)
                if prioritizer_lineage
                else len(seeker_candidates)
            )

        # Merge seeker candidates with seed candidates. Seeds are always
        # included (they represent known-good URLs from config) while seeker
        # candidates come from search. This ensures both discovery paths
        # contribute to the download pool regardless of search results.
        seed_candidates = self._scaffold_candidates(normalized_seed_urls)

        # Attribute seeds to targets: match each seed URL to the best target
        # based on jurisdiction/state appearing in the URL. This ensures seeds
        # get proper partition paths (tx/keller instead of unknown-state) and
        # count toward per-target keep_top in document review.
        if seed_candidates and request.targets:
            self._attribute_seeds_to_targets(seed_candidates, request.targets)

        if seeker_candidates and seed_candidates:
            # Deduplicate: seeds that were also found by seeker are not doubled.
            seeker_urls = {c.url for c in seeker_candidates}
            unique_seeds = [
                c for c in seed_candidates if c.url not in seeker_urls
            ]
            candidates = seeker_candidates + unique_seeds
            if unique_seeds:
                seeker_notes.append(
                    f"Merged {len(unique_seeds)} seed URL(s) with "
                    f"{len(seeker_candidates)} seeker candidate(s)."
                )
        elif seeker_candidates:
            candidates = seeker_candidates
        else:
            candidates = seed_candidates

        all_errors, seeker_notes = self._append_discovery_gap_diagnostics(
            request=request,
            candidates=candidates,
            normalized_seed_urls=normalized_seed_urls,
            errors=all_errors,
            notes=seeker_notes,
        )

        candidates_after_seeker = len(candidates)

        routing_notes: list[str] = []
        routing_state: dict[str, object] = {
            "mode": request.topology_mode or "default",
            "applied": False,
        }
        if request.topology_mode == "distributed":
            candidates, routing_errors, routing_notes, routing_state = (
                self._route_distributed_candidates(
                    request=request,
                    candidates=candidates,
                )
            )
            all_errors = normalize_error_records(
                [*all_errors, *routing_errors]
            )
        elif request.topology_mode == "centralized":
            candidates, routing_errors, routing_notes, routing_state = (
                self._route_centralized_candidates(
                    request=request,
                    candidates=candidates,
                )
            )
            all_errors = normalize_error_records(
                [*all_errors, *routing_errors]
            )
        elif request.topology_mode == "hybrid":
            candidates, routing_errors, routing_notes, routing_state = (
                self._route_hybrid_candidates(
                    request=request,
                    candidates=candidates,
                )
            )
            all_errors = normalize_error_records(
                [*all_errors, *routing_errors]
            )

        # Only re-check eligibility when a crawl actually introduced new URLs.
        # For seeker-only runs this stage is redundant and would flatten the
        # per-target selections into one group, so skip it unless routing ran.
        routing_applied = bool(routing_state.get("applied"))
        if candidates and seeker_candidates and routing_applied:
            candidates, post_routing_filter_notes = (
                self._apply_post_routing_selection_filters(
                    request=request,
                    candidates=candidates,
                )
            )
            routing_notes.extend(post_routing_filter_notes)

        candidates_after_routing = len(candidates)

        documents_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        download_records: list[dict[str, object]] = []
        download_index_path: Path | None = None
        review_index_path: Path | None = None
        curated_dir: Path | None = None
        curated_count = 0
        download_notes: list[str] = []
        review_costs: dict[str, object] = {}
        if not request.dry_run and candidates:
            self._emit_progress(request, "run: download stage started")
            candidates_for_download, gating_notes = (
                self._filter_candidates_for_download(candidates)
            )
            download_notes.extend(gating_notes)
            download_errors: list[dict[str, object]] = []
            if candidates_for_download:
                # Per-target selection (selection.max_per_target) already bounds
                # recall; do not additionally truncate the global download set to
                # the scaffold default of 10. Honor runtime.max_files when set,
                # otherwise download everything selection kept.
                download_cap = request.max_files or len(candidates_for_download)
                download_records, download_errors, stage_download_notes = (
                    self._download_candidates(
                        request=request,
                        candidates=candidates_for_download,
                        documents_dir=documents_dir,
                        max_downloads=download_cap,
                    )
                )
                download_notes.extend(stage_download_notes)

                # Post-download classification (optional, per-domain config).
                classifier_cfg = getattr(
                    request, "document_classifier", None
                )
                if classifier_cfg and download_records:
                    download_records, download_notes = (
                        self._run_post_download_classifier(
                            download_records, classifier_cfg, download_notes
                        )
                    )

                # Browser escalation: re-fetch HTML files that are JS-rendered
                # shells (SPA frameworks like Angular/React) with no usable
                # server-side content. Replaces the shell with rendered text.
                download_records, download_notes = (
                    self._escalate_js_shells_to_browser(
                        download_records, download_notes, request
                    )
                )

                # LLM document review/curation (optional, per-domain config).
                review_cfg = getattr(request, "document_review", None)
                if review_cfg and download_records:
                    classifier_cfg = getattr(request, "document_classifier", None) or {}
                    download_records, download_notes, review_costs = (
                        self._run_document_review(
                            download_records,
                            review_cfg,
                            download_notes,
                            models=getattr(request, "models", None),
                            classifier_keywords=classifier_cfg.get("nice_to_have_keywords"),
                        )
                    )

                    # Retry on code-hosting sites for targets that got 0 curated.
                    if request.link_prioritization_domain_scores:
                        classifier_cfg = getattr(request, "document_classifier", None) or {}
                        download_records, download_notes = (
                            self._retry_failed_targets_on_code_hosting(
                                request=request,
                                download_records=download_records,
                                notes=download_notes,
                                documents_dir=documents_dir,
                                review_cfg=review_cfg,
                                classifier_keywords=classifier_cfg.get("nice_to_have_keywords"),
                            )
                        )

                    # Browser crawl retry: for targets that still have 0 curated
                    # AND have JS-shell escalated pages, use the digger to crawl
                    # deeper from those rendered pages.
                    download_records, download_notes = (
                        self._retry_js_shells_with_digger(
                            request=request,
                            download_records=download_records,
                            notes=download_notes,
                            documents_dir=documents_dir,
                            review_cfg=review_cfg,
                        )
                    )

                # Checkpoint: persist completed targets for resume capability.
                new_checkpoint_entries: dict[
                    str, dict[str, object]
                ] = {}
                for _rec in download_records:
                    _meta = _rec.get("target_metadata")
                    if _meta and _rec.get("status") != "failed":
                        _key = self._target_checkpoint_key(_meta)
                        _prev = new_checkpoint_entries.get(_key, {})
                        _prev_count = int(_prev.get("download_count", 0))
                        new_checkpoint_entries[_key] = {
                            "completed_at": started_at.isoformat(),
                            "run_id": run_id,
                            "download_count": _prev_count + (
                                1
                                if _rec.get("status") == "downloaded"
                                else 0
                            ),
                        }
                if new_checkpoint_entries:
                    self._save_checkpoint_entries(
                        checkpoint_path, new_checkpoint_entries
                    )
                    download_notes.append(
                        f"Checkpoint: saved {len(new_checkpoint_entries)}"
                        f" target(s) to {checkpoint_path.as_posix()}"
                    )

            all_errors = normalize_error_records(
                [*all_errors, *download_errors]
            )
            download_index_path = self._write_download_index(
                request=request,
                run_id=run_id,
                manifest_path=manifest_path,
                download_records=download_records,
            )
            # Materialize the curated set and the human-editable review ledger,
            # so a run yields one self-contained folder: documents/ (everything),
            # curated/ (final picks), review.csv (adjust + re-curate).
            if download_records:
                curated_dir, curated_count = self._materialize_curated(
                    documents_dir=documents_dir,
                    download_records=download_records,
                )
                # Promote curated docs to the domain-level consolidated directory.
                # This accumulates across runs so extraction always sees the
                # complete set regardless of which run produced each document.
                if curated_count > 0:
                    self._promote_to_consolidated_curated(
                        run_curated_dir=curated_dir,
                        domain_dir=manifest_path.parent.parent.parent,
                    )
                review_index_path = self._write_review_index(
                    request=request,
                    download_records=download_records,
                    run_dir=manifest_path.parent,
                )
                download_notes.append(
                    f"Curated {curated_count} document(s) → "
                    f"{curated_dir.as_posix()}"
                )
                download_notes.append(
                    f"Review ledger written: {review_index_path.as_posix()}"
                )
            download_notes.append(
                f"Download index written: {download_index_path.as_posix()}"
            )

        base_status = "scaffold_dry_run" if request.dry_run else "scaffold"
        status = f"{base_status}_with_errors" if all_errors else base_status
        self._emit_progress(
            request,
            f"run: discovery finished with status={status}",
        )

        completed_at = datetime.now(timezone.utc)
        elapsed_seconds = round((completed_at - started_at).total_seconds(), 3)

        manifest = DiscoveryManifest(
            run_id=run_id,
            status=status,
            started_at=started_at.isoformat(),
            input={
                "domain": request.domain,
                "seed_urls": normalized_seed_urls,
                "query": request.query,
                "enable_serpapi": request.enable_serpapi,
                "dry_run": request.dry_run,
            },
            constraints=self._build_constraints(request),
            timing={
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "elapsed_seconds": elapsed_seconds,
            },
            stage_summaries={
                "seeker": {
                    "provider": seeker_state.get("provider", "seed_only"),
                    "enabled": bool(seeker_state.get("enabled")),
                    "queries_executed": len(request.targets or []),
                    "candidates_discovered": seeker_raw_count,
                    "candidates_after_prioritization": candidates_after_seeker,
                    "link_prioritization_mode": request.link_prioritization_mode,
                    "link_top_k": request.link_top_k,
                },
                "routing": {
                    "mode": request.topology_mode or "default",
                    "applied": bool(routing_state.get("applied")),
                    "candidates_in": candidates_after_seeker,
                    "candidates_out": candidates_after_routing,
                },
                "downloads": self._build_download_summary(download_records),
                "document_review": {
                    "costs": review_costs,
                } if review_costs else {},
                "acceptance_metrics": self._build_acceptance_metrics(
                    request=request,
                    seeker_state=seeker_state,
                    target_selection_metrics=target_selection_metrics,
                    candidates=candidates,
                ),
            },
            candidate_summary=self._build_candidate_summary(candidates),
            lineage={
                "run_id": run_id,
                "documents_dir": documents_dir.as_posix(),
                "manifest_path": manifest_path.as_posix(),
                "seeker": seeker_state,
                "routing": routing_state,
                "link_prioritization": {
                    "mode": request.link_prioritization_mode,
                    "top_k": request.link_top_k,
                    "candidates": prioritizer_lineage,
                }
                if prioritizer_lineage
                else {
                    "mode": request.link_prioritization_mode,
                    "top_k": request.link_top_k,
                    "candidates": [],
                },
                "download_index_csv": (
                    download_index_path.as_posix()
                    if download_index_path is not None
                    else None
                ),
                "review_index_csv": (
                    review_index_path.as_posix()
                    if review_index_path is not None
                    else None
                ),
                "curated_dir": (
                    curated_dir.as_posix()
                    if curated_dir is not None
                    else None
                ),
                "curated_count": curated_count,
            },
            candidates=candidates,
            downloads=download_records,
            errors=all_errors,
            error_summary=summarize_error_records(all_errors),
            notes=[
                *seeker_notes,
                *routing_notes,
                *download_notes,
            ],
        )
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        # Point <domain>/latest at this run so downstream steps and humans can
        # address "the current run" without knowing the timestamped run_id. Skip
        # runs that produced no downloads (e.g. everything checkpoint-skipped),
        # so `latest` keeps pointing at the last run that actually has documents.
        if (
            not request.dry_run
            and request.output_manifest is None
            and download_records
        ):
            self._refresh_latest_pointer(manifest_path.parent)

        return DiscoveryResult(
            run_id=run_id,
            manifest_path=manifest_path,
            documents_dir=documents_dir,
            dry_run=request.dry_run,
            download_index_path=download_index_path,
            review_index_path=review_index_path,
            curated_dir=curated_dir,
            curated_count=curated_count,
        )
