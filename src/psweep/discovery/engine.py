"""Discovery orchestration over acquisition, review, and publication."""

from __future__ import annotations

import contextlib
import csv
import hashlib
import importlib.util
import json
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from psweep.extraction.document_utils import (
    TEXT_CACHE_DIRNAME, text_cache_paths,
)
from psweep.utils.error_taxonomy import (
    build_error_record,
    normalize_error_records,
    summarize_error_records,
)

from .candidate_selector import CandidateSelector
from .concurrency import bounded_results
from .connectors import DiggerInput, resolve_digger_connector
from .connectors.digger import NullDiggerConnector
from .constants import (
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    DOCUMENT_EXTENSIONS,
    DOWNLOADABLE_EXTENSIONS,
    MIME_TO_EXTENSION,
    ROUTE_REASON_CENTRALIZED,
    ROUTE_REASON_DISTRIBUTED,
    SEEKER_ONLY_SCORE_WEIGHTS,
)
from .link_prioritizer import LinkPrioritizer
from .models import (
    CandidateScore,
    DiscoveryCandidate,
    DiscoveryManifest,
    discovery_status,
)
from .policies import (
    DEFAULT_ROBOTS_POLICY_MODE,
    DEFAULT_TOS_POLICY_MODE,
    DiscoveryPolicyEvaluator,
)
from .network import NetworkPolicyError, check_url, send_get
from .document_reviewer import DocumentReviewer
from .publication import (
    PublicationPaths,
    artifact_sha256,
    atomic_write,
    discovery_publication,
    effective_discovery_fingerprint,
    resolve_publication_paths,
    materialize_curated,
    record_is_curated,
    promote_curated,
    merge_download_index,
    refresh_latest_pointer,
    load_checkpoint,
    save_checkpoint_entries,
    publish_manual_curation,
)
from .retry import compute_backoff, is_transient_error
from .urls import (
    compile_url_patterns,
    decode_discovery_url,
    matches_allowed_domain,
    matches_url_exclusion,
    normalize_url_text,
    url_extension,
    url_host,
)

# Single source of truth for the download-organization default. Referenced by
# ``DiscoveryRequest`` and the ``discover`` CLI option/fallback.
DEFAULT_PARTITION_MODE = "auto"


# Browser escalation defaults for JS-rendered HTML shells. Explicit
# discovery.browser_escalation thresholds take precedence.
DEFAULT_BROWSER_ESCALATION_SETTLE_SECONDS = 15.0
DEFAULT_BROWSER_ESCALATION_MIN_SHELL_CHARS = 8000
DEFAULT_BROWSER_ESCALATION_MIN_RENDERED_CHARS = 10000

# A browser render returning fewer than this many characters of HTML is treated
# as a failed render (blank page / error shell) and skipped.
MIN_RENDERED_HTML_CHARS = 500

# Budget for the post-review browser-crawl retry of JS-shell targets
# (_retry_js_shells_with_digger). Deliberately small: it fires only for the
# minority of targets that ended with 0 curated docs but a JS-shell download.
JS_SHELL_RETRY_MAX_DEPTH = 3
JS_SHELL_RETRY_MAX_PAGES = 15
JS_SHELL_RETRY_MAX_FILES = 3
JS_SHELL_RETRY_TIMEOUT_SECONDS = 60

# Candidate scores for artifacts found by the JS-shell browser-crawl retry.
# They come from a link-text-matched crawl, so only url + trust signals exist.
JS_SHELL_RETRY_URL_SIGNAL = 0.7
JS_SHELL_RETRY_TRUST_SIGNAL = 0.3


class _RetryBudget:
    """Shared cap on retry targets across the post-review retry stages.

    A single budget is threaded through both retry stages (code-hosting
    SerpApi retry + JS-shell browser-crawl retry) so the cap bounds the
    combined per-run total, not each stage independently.

    ``limit=None`` means unbounded — the historical default behavior — and
    :meth:`allow` then returns whatever is requested without decrementing.
    """

    __slots__ = ("_remaining",)

    def __init__(self, limit: int | None) -> None:
        self._remaining = None if limit is None else max(0, int(limit))

    @property
    def unbounded(self) -> bool:
        return self._remaining is None

    def allow(self, requested: int) -> int:
        """Grant up to ``requested`` targets, decrementing remaining budget."""
        if self._remaining is None:
            return requested
        granted = min(max(0, requested), self._remaining)
        self._remaining -= granted
        return granted


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
    partition_mode: str = DEFAULT_PARTITION_MODE
    # Optional {full-name: short-key} jurisdiction alias map. Supplements the
    # built-in US-state table (config wins on conflicts) so non-US jurisdiction
    # schemes work under partition_mode=jurisdiction. None -> US default only.
    jurisdiction_aliases: dict[str, str] | None = None
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
    # Opt-in cap on the TOTAL number of targets processed by the post-review
    # retry stages (code-hosting SerpApi retry + JS-shell browser-crawl retry)
    # within a single run. Sourced from
    # ``config.discovery.retry_policy.max_retry_targets`` when the loader wires
    # it; read defensively via ``getattr`` so an unset value (None) means
    # unbounded — i.e. identical to the historical behavior.
    retry_max_retry_targets: int | None = None
    max_concurrent_downloads: int = DEFAULT_MAX_CONCURRENT_DOWNLOADS
    min_request_interval_ms: int = 0
    request_headers: dict[str, str] | None = None
    robots_policy_mode: str = DEFAULT_ROBOTS_POLICY_MODE
    tos_policy_mode: str = DEFAULT_TOS_POLICY_MODE
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
    link_prioritization_shopping_keywords: list[str] | None = None
    # Per-target candidate selection
    selection_primary_per_target: int = 1
    selection_exclude_draft: bool = True
    selection_draft_patterns: list[str] | None = None
    selection_relevance_require_any_terms: list[str] | None = None
    selection_relevance_require_legal_marker_terms: list[str] | None = None
    selection_relevance_exclude_any_terms: list[str] | None = None
    selection_exclude_url_patterns: list[str] | None = None
    selection_exclude_text_patterns: list[str] | None = None
    selection_max_per_host_per_target: int = 0
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
    # JS-shell browser re-render policy (see _escalate_js_shells_to_browser):
    # {enabled, settle_seconds, min_shell_chars, min_rendered_chars}
    browser_escalation: dict[str, object] | None = None
    # Code-host content adapters (see _resolve_code_host_shells): fetch ordinance
    # text from JS code hosts (e.g. Municode) via their server API before the
    # browser fallback. {enabled}; disabled unless explicitly opted in.
    code_host_adapters: dict[str, object] | None = None
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
    # When True, ignore all prior work for this run: skip the checkpoint (do not
    # prune already-completed targets) and refresh the seeker cache (force live
    # fetches even when ``seeker_cache`` is enabled). Drives the ``discover
    # --fresh`` / ``run --fresh`` "start fresh" UX.
    reprocess: bool = False
    # Optional callback for human-friendly progress messages emitted by engine stages.
    progress_callback: Callable[[str], None] | None = None
    # Downloaded-document retention policy:
    # - all: keep all downloaded documents
    # - curated: keep only selected curated documents
    # - none: keep no downloaded documents
    retention_documents: str = "all"


@dataclass(slots=True)
class DiscoveryResult:
    """Run outcomes and published artifacts returned by discovery."""

    run_id: str
    manifest_path: Path
    documents_dir: Path
    dry_run: bool
    download_index_path: Path | None = None
    review_index_path: Path | None = None
    curated_dir: Path | None = None
    curated_count: int = 0
    status: str = "empty"
    errors: list[dict[str, object]] = field(default_factory=list)


class _PartitionIndex:
    """Index partition overlap by ancestors instead of scanning all targets."""

    def __init__(self, partitions=()):
        self.paths: set[Path] = set()
        self.ancestors: set[Path] = set()
        for partition in partitions:
            self.add(partition)

    def add(self, partition: Path) -> None:
        self.paths.add(partition)
        self.ancestors.update(partition.parents)

    def __bool__(self) -> bool:
        return bool(self.paths)

    def __iter__(self):
        return iter(self.paths)

    def overlaps(self, partition: Path) -> bool:
        return partition in self.ancestors or any(
            ancestor in self.paths for ancestor in (partition, *partition.parents)
        )


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
    """Coordinate discovery and publish verified artifacts."""

    def __init__(self) -> None:
        self._policy_evaluator = DiscoveryPolicyEvaluator()
        self._rate_limiter = None
        self._retry_errors = []
        self._retry_costs = Counter()
        self._retry_search_calls = 0

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
                label = value.strip()
                state = target.get("state")
                if key in {"jurisdiction", "county"} and isinstance(state, str) and state.strip():
                    state = state.strip()
                    if not label.casefold().endswith(f", {state}".casefold()):
                        return f"{label}, {state}"
                return label
        return f"target-{fallback_index}"

    _DEFAULT_REQUEST_HEADERS = {
        "User-Agent": "ParseSweep/2.0 (+discovery)"
    }
    _ALLOWED_RETENTION_MODES = {"all", "curated", "none"}
    # File-type sets / MIME map are centralized in constants.py.
    _SUPPORTED_EXTENSIONS = DOWNLOADABLE_EXTENSIONS
    _MIME_EXTENSION_MAP = MIME_TO_EXTENSION

    @staticmethod
    def _slugify_domain(domain: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")
        return slug or "default"

    def _build_run_id(
        self, request: DiscoveryRequest, started_at: datetime
    ) -> str:
        timestamp = started_at.strftime("%Y%m%dT%H%M%S%fZ")
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
    def _seed_candidates(
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

            parsed = urlparse(decode_discovery_url(candidate.url))
            url_lower = unquote(parsed.path + "?" + parsed.query).lower()
            best_target: dict[str, object] | None = None
            best_score = 0
            tied = False

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
                    tied = False
                elif score == best_score and score > 0:
                    tied = True

            # A shared agency/host does not establish a unique target identity.
            if best_target and best_score >= 4 and not tied:
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
        discovered_count: int = 0,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Distinguish failed, empty, and filtered searches."""
        if not DiscoveryEngine._has_discovery_inputs(request):
            return errors, notes
        if candidates or normalized_seed_urls:
            return errors, notes

        if discovered_count:
            message = (
                f"Search returned {discovered_count} candidate(s), but none "
                "passed configured source/format/selection rules. "
                "Inspect selection metrics and adjust the query or rules; "
                "Changing credentials will not resolve these exclusions."
            )
        elif errors:
            return errors, [
                *notes,
                "No candidates were produced; inspect search errors.",
            ]
        elif request.enable_serpapi:
            message = (
                "Search returned no candidates. Adjust query or target inputs."
            )
        else:
            message = (
                "Search is disabled and no seed candidates are available. "
                "Enable search or supply seeds."
            )
        return errors, [*notes, message]

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
    def _resolve_seeker_cache_dir(request: DiscoveryRequest) -> str | None:
        """Return the canonical SerpApi cache directory for this request."""
        if not getattr(request, "seeker_cache", False):
            return None
        return str(Path("discovered") / request.domain / ".serpapi_cache")

    @staticmethod
    def _build_selector(request: DiscoveryRequest) -> CandidateSelector:
        """Construct a CandidateSelector from request selection settings.

        Shared by seeker-stage selection and post-routing re-selection so the
        two never drift apart.
        """
        return CandidateSelector(
            exclude_draft=request.selection_exclude_draft,
            draft_patterns=request.selection_draft_patterns,
            relevance_require_any_terms=request.selection_relevance_require_any_terms
            or None,
            relevance_require_legal_marker_terms=request.selection_relevance_require_legal_marker_terms
            or None,
            relevance_exclude_any_terms=request.selection_relevance_exclude_any_terms
            or None,
            exclude_url_patterns=request.selection_exclude_url_patterns or None,
            exclude_text_patterns=request.selection_exclude_text_patterns or None,
            require_supported_document=bool(
                request.selection_require_supported_document
            ),
            max_per_host_per_target=max(
                0, int(request.selection_max_per_host_per_target or 0)
            ),
            target_identity_require_any_templates=request.selection_target_identity_require_any_templates
            or None,
            target_identity_require_all_templates=request.selection_target_identity_require_all_templates
            or None,
            target_identity_exclude_any_templates=request.selection_target_identity_exclude_any_templates
            or None,
        )

    @staticmethod
    def _filter_seeker_scope(request, groups, query_results, notes):
        """Filter source scope before per-target selection."""
        scoped = []
        counts = []
        for group in groups:
            eligible = [
                candidate
                for candidate in group
                if DiscoveryEngine._passes_final_url_selection_guard(
                    final_url=candidate.url,
                    request=request,
                )[0]
            ]
            scoped.append(eligible)
            counts.append((len(group), len(group) - len(eligible)))
        notes.extend(
            f"Search '{summary['query']}': returned {summary['returned']}, "
            f"retained {summary['retained']} (limit {summary['limit']}, "
            f"cache={'hit' if summary['cache_hit'] else 'miss'})."
            for summary in query_results
        )
        return scoped, counts

    @staticmethod
    def _annotate_search_metrics(metrics, counts, groups, incomplete_targets):
        """Retain candidate counts and source-scope rejections."""
        for metric in metrics:
            idx = metric["target_index"]
            metric["input_candidates"], metric["scope_rejected"] = counts[idx]
            if metric["scope_rejected"] and not groups[idx]:
                metric["outcome"] = "excluded_source_scope"
            if idx in incomplete_targets:
                metric["search_complete"] = False

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
        int,
    ]:
        """Call SerpApi seeker and return discovered candidates.

        Returns a 6-tuple: (candidates, notes, errors, prioritizer_lineage,
        target_selection_metrics, queries_run).  ``queries_run`` is the number
        of live search attempts, including retries/timeouts (cache hits excluded;
        actual provider billing for failures may differ).
        """
        if not seeker_state.get("enabled") or not seeker_state.get(
            "available"
        ):
            return [], current_notes, current_errors, [], [], 0

        notes = list(current_notes)
        errors = list(current_errors)
        seeker = None
        incomplete_targets: set[int] = set()

        try:
            from .connectors.serpapi_seeker import (
                SerpApiSearchTimeout,
                SerpApiSeeker,
            )

            seeker_cache_dir = DiscoveryEngine._resolve_seeker_cache_dir(request)
            seeker = SerpApiSeeker(
                retry_max_attempts=request.retry_max_attempts,
                retry_initial_backoff_seconds=request.retry_initial_backoff_seconds,
                retry_max_backoff_seconds=request.retry_max_backoff_seconds,
                min_request_interval_seconds=max(
                    0.0, float(request.min_request_interval_ms) / 1000.0
                ),
                cache_dir=seeker_cache_dir,
                timeout_seconds=request.timeout_seconds or 45,
                cache_ttl_seconds=max(
                    0.0,
                    float(
                        getattr(request, "seeker_cache_ttl_minutes", 0.0) or 0.0
                    )
                    * 60.0,
                ),
                cache_refresh=bool(
                    getattr(request, "reprocess", False)
                    and not getattr(request, "dry_run", False)
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
                try:
                    discovered = seeker.discover(seeker_input)
                except SerpApiSearchTimeout as exc:
                    discovered = exc.candidates
                    incomplete_targets.add(idx - 1)
                    for message in exc.query_errors:
                        errors.append(build_error_record(
                            TimeoutError(f"{target_label}: {message}"),
                            stage="discovery.seeker_discover",
                            provider="serpapi",
                        ))
                    notes.append(
                        f"Partial search for {target_label}: "
                        f"{len(exc.query_errors)} query timeout(s); retained "
                        f"{len(discovered)} candidate(s)."
                    )
                except Exception as exc:
                    # Preserve already-completed targets and keep this target retryable.
                    discovered = []
                    incomplete_targets.add(idx - 1)
                    errors.append(build_error_record(
                        exc, stage="discovery.seeker_discover", provider="serpapi",
                    ))
                    notes.append(f"Search failed for {target_label}; target remains retryable.")
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
            return (
                [], notes, normalize_error_records(errors), [], [],
                seeker.queries_run if seeker is not None else 0,
            )

        def _to_candidate(
            raw: dict[str, object],
        ) -> DiscoveryCandidate | None:
            url = decode_discovery_url(str(raw.get("url") or ""))
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

        candidates_by_target, scope_counts = (
            DiscoveryEngine._filter_seeker_scope(
                request, candidates_by_target,
                getattr(seeker, "query_results", []), notes,
            )
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
        DiscoveryEngine._annotate_search_metrics(
            target_selection_metrics, scope_counts,
            candidates_by_target, incomplete_targets,
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
                shopping_path_keywords=(
                    request.link_prioritization_shopping_keywords
                ),
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
            seeker.queries_run,
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

        # Explicit aliases resolve to the first nonempty target field.
        effective_aliases = aliases or {}
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
            timeout_seconds=request.timeout_seconds or 15,
            rate_limiter=self._rate_limiter,
        )

    def _network_gate(
        self,
        request: DiscoveryRequest,
        ssl_verify: bool,
        *,
        document: bool = False,
    ) -> Callable[[str], object]:
        """Share policy and source checks across acquisition paths."""

        def gate(url: str) -> object:
            if not matches_allowed_domain(url, request.allowed_domains):
                return False
            if matches_url_exclusion(
                url,
                compile_url_patterns(request.selection_exclude_url_patterns),
            ):
                return False
            if (
                document
                and not self._passes_final_url_selection_guard(
                    final_url=url, request=request
                )[0]
            ):
                return False
            return self._evaluate_request_policy(
                url=url, request=request, ssl_verify=ssl_verify
            )

        return gate

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
        request: DiscoveryRequest | None = None,
    ):
        gate = (
            self._network_gate(request, ssl_verify, document=True)
            if request
            else None
        )
        return send_get(
            url,
            headers=request_headers,
            timeout=(request.timeout_seconds or 60) if request else 60,
            verify=ssl_verify,
            stream=True,
            gate=gate,
            response_url_gate=gate,
            limiter=rate_limiter,
            retry=(
                int(retry_policy["max_attempts"]),
                float(retry_policy["initial_backoff_seconds"]),
                float(retry_policy["max_backoff_seconds"]),
            ),
        )

    @staticmethod
    def _should_retry_download_with_browser(exc: BaseException) -> bool:
        """Return True when a direct fetch should be retried via browser."""
        try:
            import requests
        except ImportError:
            return False

        if not isinstance(exc, requests.exceptions.HTTPError):
            return False

        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code in {401, 403, 429, 451}:
            return True

        lowered = str(exc).lower()
        return any(
            marker in lowered
            for marker in ("forbidden", "unauthorized", "access denied")
        )

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
        url = decode_discovery_url(url)
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

        allowed, reason = self._passes_final_url_selection_guard(
            final_url=url, request=request
        )
        if not allowed:
            return (
                idx,
                {
                    "url": url,
                    "status": "skipped_selection_filter",
                    "reason": reason,
                },
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

        response = None
        target_path = None
        own_target = False
        try:
            if browser is not None:
                self._browser_policy_gate(request, url)
                if rate_limiter is not None:
                    rate_limiter.wait()
                content, mime_type, final_url = browser.download_bytes(url)
                attempt_count = 1
                download_method = "browser"

                def _chunks(_c: bytes = content) -> object:
                    return [_c]
            else:
                download_method = "requests"
                try:
                    response, attempt_count = self._download_with_retry(
                        url=url,
                        ssl_verify=ssl_verify,
                        retry_policy=retry_policy,
                        request_headers=self._resolve_request_headers(request),
                        rate_limiter=rate_limiter,
                        request=request,
                    )
                    final_url = response.url or url
                    mime_type = (response.headers or {}).get("Content-Type")

                    def _chunks(_r: object = response) -> object:
                        return _r.iter_content(chunk_size=65536)
                except Exception as exc:
                    if not self._browser_recovery_enabled(
                        request
                    ) or not self._should_retry_download_with_browser(exc):
                        raise
                    self._browser_policy_gate(request, url)

                    browser_session = browser
                    browser_owned = False
                    if browser_session is None:
                        browser_session, _browser_note = (
                            self._open_browser_for_download()
                        )
                        browser_owned = browser_session is not None
                    if browser_session is None:
                        raise

                    try:
                        if rate_limiter is not None:
                            rate_limiter.wait()
                        content, mime_type, final_url = (
                            browser_session.download_bytes(url)
                        )
                        attempt_count = 1
                        download_method = "browser_fallback"

                        def _chunks(_c: bytes = content) -> object:
                            return [_c]
                    finally:
                        if browser_owned:
                            with contextlib.suppress(Exception):
                                browser_session.close()

            if not final_url or not url_host(final_url):
                raise ValueError("Download returned no verified final URL")
            final_url = decode_discovery_url(final_url)
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

            # Strict file scope rejects MIME mismatches.
            # Web/script path rules keep their source-boundary meaning.
            if (
                request.include_url_patterns
                and url_extension(final_url) in DOCUMENT_EXTENSIONS
                and extension != url_extension(final_url)
            ):
                return (
                    idx,
                    {
                        "url": url,
                        "final_url": final_url,
                        "status": "skipped_selection_filter",
                        "reason": "content_type_outside_include_patterns",
                        "mime_type": mime_type,
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
                    hasher = hashlib.sha256()
                    with target_path.open("xb") as handle:
                        own_target = True
                        for chunk in _chunks():
                            if not chunk:
                                continue
                            handle.write(chunk)
                            hasher.update(chunk)
                            bytes_written += len(chunk)
                    content_hash = hasher.hexdigest()
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
                    "content_hash": content_hash,
                    "attempt_count": attempt_count,
                    "download_method": download_method,
                    "policy_warning_codes": list(policy_result.warning_codes),
                    "target_label": (
                        self._target_label(candidate.target_metadata, idx)
                        if candidate.target_metadata
                        else None
                    ),
                    "target_metadata": candidate.target_metadata,
                    **partition_meta,
                },
                None,
                True,
                list(policy_result.warning_codes),
            )
        except NetworkPolicyError as exc:
            return (
                idx,
                {
                    "url": url,
                    "final_url": exc.url,
                    "status": "skipped_selection_filter",
                    "reason": str(exc),
                },
                None,
                False,
                list(policy_result.warning_codes),
            )
        except Exception as exc:
            if own_target and target_path is not None:
                target_path.unlink(missing_ok=True)
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
        finally:
            if response is not None and callable(
                getattr(response, "close", None)
            ):
                response.close()

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
        normalized_mime = (mime_type or "").split(";", 1)[0].strip().lower()
        ext = DiscoveryEngine._MIME_EXTENSION_MAP.get(normalized_mime)
        if ext in DiscoveryEngine._SUPPORTED_EXTENSIONS:
            return ext
        suffix = url_extension(url)
        if normalized_mime.startswith(("image/", "video/", "audio/")):
            return None
        if suffix in DiscoveryEngine._SUPPORTED_EXTENSIONS:
            return suffix
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
        """Enforce deterministic source policy, not bare-URL relevance heuristics."""
        if not matches_allowed_domain(final_url, request.allowed_domains):
            return False, "final_url_outside_allowed_domains"
        include_patterns = compile_url_patterns(request.include_url_patterns)
        if include_patterns and not any(
            pattern.search(value) for pattern in include_patterns
            for value in (final_url, unquote(final_url))
        ):
            return False, "final_url_outside_include_patterns"
        patterns = compile_url_patterns(request.selection_exclude_url_patterns)
        if matches_url_exclusion(final_url, patterns):
            return False, "final_url_matches_excluded_pattern"
        text = cls._normalize_url_text(unquote(decode_discovery_url(final_url)))
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
    def _effective_state_aliases(
        cls, extra: dict[str, str] | None
    ) -> dict[str, str]:
        """Return only explicitly configured geographic aliases."""
        return {
            str(k).strip().lower(): str(v).strip().lower()
            for k, v in (extra or {}).items()
        }

    @classmethod
    def _normalize_state_key(
        cls, value: str | None, aliases: dict[str, str] | None = None
    ) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            return None
        alias_map = aliases or {}
        if len(normalized) == 2 and normalized.isalpha():
            return normalized
        return alias_map.get(normalized) or cls._slug(normalized)

    @classmethod
    def _jurisdiction_partition_dir(
        cls,
        documents_dir: Path,
        *,
        state: str | None,
        jurisdiction: str | None,
        aliases: dict[str, str] | None = None,
    ) -> tuple[str, str, Path]:
        state_key = cls._normalize_state_key(state, aliases) or "unknown-state"
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
        # Partitioning layers, most general first:
        #   1. partition_by  — the domain-agnostic, scalable path: partition by
        #      ANY target-metadata field(s) (county, region, year, manufacturer,
        #      ...). This is the mechanism to use for arbitrary schemes and it
        #      takes precedence over everything below.
        #   2. jurisdiction  — an OPTIONAL geographic convenience that adds
        #      query-based inference of a jurisdiction + short-key (aliases from
        #      discovery.jurisdiction_aliases, defaulting to the US-state table).
        #   3. host          — fall back to partitioning by URL host.
        # Only (1) is required for universality; (2)/(3) are conveniences.
        if request.partition_by:
            source_meta, partition_dir = cls._generic_partition_dir(
                documents_dir,
                fields=list(request.partition_by),
                target_metadata=target_metadata,
            )
            return ("fields", source_meta, partition_dir)

        requested_mode = (
            request.partition_mode or DEFAULT_PARTITION_MODE
        ).strip().lower()
        mode = (
            requested_mode
            if requested_mode in {"auto", "jurisdiction", "host"}
            else DEFAULT_PARTITION_MODE
        )

        alias_map = cls._effective_state_aliases(request.jurisdiction_aliases)
        context = target_metadata or {}
        jurisdiction = request.jurisdiction or context.get("jurisdiction")
        state = request.state or context.get("state")

        if mode == "jurisdiction":
            state_key, jurisdiction_key, partition_dir = (
                cls._jurisdiction_partition_dir(
                    documents_dir,
                    state=state,
                    jurisdiction=jurisdiction,
                    aliases=alias_map,
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
                    aliases=alias_map,
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
        rate_limiter = self._rate_limiter or _RequestRateLimiter(
            request.min_request_interval_ms
        )
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
                def fetch(item):
                    idx, candidate = item
                    return self._download_single_candidate(
                        idx=idx, candidate=candidate, request=request,
                        documents_dir=documents_dir, ssl_verify=ssl_verify,
                        retry_policy=retry_policy, rate_limiter=rate_limiter,
                    )

                completed.extend(bounded_results(
                    fetch, enumerate(staged_candidates, start=1),
                    workers=max_concurrent_downloads,
                ))
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
            candidate = staged_candidates[_ - 1]
            download_record.setdefault(
                "target_metadata", candidate.target_metadata,
            )
            download_record.setdefault(
                "target_label",
                self._target_label(candidate.target_metadata, _)
                if candidate.target_metadata else None,
            )
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
        browser_fallback_count = sum(
            1
            for record in downloads
            if str(record.get("download_method")) == "browser_fallback"
        )
        if browser_fallback_count:
            notes.append(
                "Download fallback retried "
                f"{browser_fallback_count} blocked URL(s) via headless browser."
            )
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
        groups: dict[str, list[DiscoveryCandidate]] = {}
        contexts: dict[str, dict] = {}
        for candidate in candidates:
            context = candidate.target_metadata or {}
            key = json.dumps(context, sort_keys=True, default=str)
            groups.setdefault(key, []).append(candidate)
            contexts[key] = context
        filtered, filter_notes = selector.select(
            list(groups.values()),
            primary_per_target=max(len(group) for group in groups.values()),
            target_contexts=list(contexts.values()),
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
    def _browser_recovery_enabled(request: DiscoveryRequest) -> bool:
        """Require explicit browser opt-in for automatic recovery."""
        cfg = request.browser_escalation
        return request.browser_mode or cfg is True or (
            isinstance(cfg, dict) and cfg.get("enabled") is True
        )

    @staticmethod
    def _copy_candidate_with_reason(
        candidate: DiscoveryCandidate,
        reason: str,
    ) -> DiscoveryCandidate:
        reasons = list(candidate.reasons)
        if reason not in reasons:
            reasons.append(reason)
        return dataclass_replace(candidate, reasons=reasons)

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
            "shopping_path_keywords": list(
                request.link_prioritization_shopping_keywords or []
            ),
        }

        return constraints

    # ------------------------------------------------------------------
    # Shared routing helpers (used by distributed + centralized routing).
    # These factor out the parts that were previously duplicated verbatim
    # across ``_route_distributed_candidates`` and
    # ``_route_centralized_candidates``. Behavior is unchanged: per-mode
    # differences (seed selection, extra digger params, routing-state
    # extras, note strings, and the distributed non-crawled merge-back)
    # stay in the two orchestrator methods below.
    # ------------------------------------------------------------------

    def _discover_routing_artifacts(
        self,
        *,
        request: DiscoveryRequest,
        filtered_seed_urls: list[str],
        ssl_verify: bool,
        extra_params: dict[str, object] | None = None,
    ) -> tuple[list[object] | None, list[dict[str, object]]]:
        """Run the digger ``discover`` call shared by both routing modes.

        Returns ``(artifacts, errors)``. ``artifacts is None`` signals the
        connector raised (caller should fall back to unrouted candidates);
        the error record is included in ``errors``. ``extra_params`` carries
        mode-specific digger params (empty for distributed, the index-page
        options for centralized) merged ahead of the shared ssl/header/retry
        params.
        """
        errors: list[dict[str, object]] = []
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
                        **(extra_params or {}),
                        "ssl_verify": ssl_verify,
                        "request_headers": self._resolve_request_headers(
                            request
                        ),
                        "retry": self._resolve_retry_policy(request),
                        "browser_escalation": self._browser_recovery_enabled(
                            request
                        ),
                        "request_gate": self._network_gate(
                            request, ssl_verify
                        ),
                        "response_url_gate": self._network_gate(
                            request, ssl_verify
                        ),
                        "rate_limiter": self._rate_limiter,
                        "browser_policy_strict": request.robots_policy_mode
                        == "enforce"
                        or request.tos_policy_mode == "enforce",
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
            return None, errors
        return artifacts, errors

    @staticmethod
    def _digger_discovery_modes(artifacts: list[object]) -> list[str]:
        """Sorted set of ``discovery_mode`` values across digger artifacts."""
        return sorted(
            {
                str(artifact.metadata.get("discovery_mode") or "unknown")
                for artifact in artifacts
                if isinstance(artifact.metadata, dict)
            }
        )

    def _build_routed_candidates_from_artifacts(
        self,
        *,
        candidates: list[DiscoveryCandidate],
        artifacts: list[object],
        route_reason: str,
    ) -> tuple[list[DiscoveryCandidate], set[str]]:
        """Merge digger artifacts back onto candidates for a routing mode.

        Existing candidates are re-tagged with ``route_reason``; artifacts
        with no matching candidate become new crawl-child candidates that
        inherit target provenance. Returns ``(routed_candidates, seen_urls)``
        so the distributed path can append its non-crawled candidates after.
        """
        candidate_by_url: dict[str, list[DiscoveryCandidate]] = {}
        for candidate in candidates:
            candidate_by_url.setdefault(candidate.url, []).append(candidate)

        routed_candidates: list[DiscoveryCandidate] = []
        seen_urls: set[str] = set()
        for artifact in artifacts:
            seen_urls.add(artifact.url)
            existing = candidate_by_url.get(artifact.url)
            if existing:
                routed_candidates.extend(
                    self._copy_candidate_with_reason(candidate, route_reason)
                    for candidate in existing
                )
                continue
            metadata = artifact.metadata or {}
            origins = candidate_by_url.get(
                str(metadata.get("source_seed")), []
            )
            contexts = [
                candidate.target_metadata for candidate in origins
            ] or [None]
            routed_candidates.extend(
                DiscoveryCandidate(
                    url=artifact.url,
                    source=artifact.source,
                    score=CandidateScore(url_signal=0.35, trust_signal=0.55),
                    reasons=[route_reason],
                    mime_type=artifact.mime_type,
                    extension=artifact.extension,
                    target_metadata=context,
                    snippet=str(metadata.get("link_text") or "") or None,
                )
                for context in contexts
            )
        return self._dedupe_candidates_by_url(routed_candidates), seen_urls

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
        crawlable_urls = list(
            dict.fromkeys(
                candidate.url
                for candidate in candidates
                if url_extension(candidate.url) not in DOWNLOADABLE_EXTENSIONS
                and matches_allowed_domain(
                    candidate.url, request.allowed_domains
                )
            )
        )

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

        artifacts, discover_errors = self._discover_routing_artifacts(
            request=request,
            filtered_seed_urls=filtered_seed_urls,
            ssl_verify=ssl_verify,
        )
        errors.extend(discover_errors)
        if artifacts is None:
            notes.append(
                "Distributed routing failed; falling back to unrouted candidates."
            )
            return (
                candidates,
                normalize_error_records(errors),
                notes,
                routing_state,
            )

        routing_state.update(
            {
                "applied": True,
                "artifact_count": len(artifacts),
                "discovery_modes": self._digger_discovery_modes(artifacts),
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

        routed_candidates, seen_urls = (
            self._build_routed_candidates_from_artifacts(
                candidates=candidates,
                artifacts=artifacts,
                route_reason=ROUTE_REASON_DISTRIBUTED,
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

        artifacts, discover_errors = self._discover_routing_artifacts(
            request=request,
            filtered_seed_urls=filtered_seed_urls,
            ssl_verify=ssl_verify,
            extra_params=extra_params,
        )
        errors.extend(discover_errors)
        if artifacts is None:
            notes.append(
                "Centralized routing failed; falling back to unrouted candidates."
            )
            return (
                candidates,
                normalize_error_records(errors),
                notes,
                routing_state,
            )

        routing_state.update(
            {
                "applied": True,
                "artifact_count": len(artifacts),
                "discovery_modes": self._digger_discovery_modes(artifacts),
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

        routed_candidates, _seen_urls = (
            self._build_routed_candidates_from_artifacts(
                candidates=candidates,
                artifacts=artifacts,
                route_reason=ROUTE_REASON_CENTRALIZED,
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
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            association = (
                candidate.url,
                json.dumps(
                    candidate.target_metadata, sort_keys=True, default=str
                ),
            )
            if association in seen:
                continue
            seen.add(association)
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
        index_path: Path | None = None,
        render_only: bool = False,
    ) -> Path | str:
        """Write provenance at the resolved run publication location."""
        index_path = index_path or manifest_path.parent / "download_index.csv"
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

        handle = StringIO(newline="")
        with contextlib.nullcontext(handle):
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
        if render_only:
            return handle.getvalue()
        atomic_write(index_path, handle.getvalue())
        return index_path

    @staticmethod
    def _write_review_index(
        *,
        request: DiscoveryRequest,
        download_records: list[dict[str, object]],
        run_dir: Path,
        render_only: bool = False,
    ) -> Path | str:
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

        handle = StringIO(newline="")
        with contextlib.nullcontext(handle):
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
                        "llm_selected": record.get(
                            "review_selected",
                            record.get("status") == "downloaded",
                        ),
                        "human_decision": "",
                        "human_notes": "",
                        "llm_reason": record.get("review_reason"),
                        "relative_path": rel,
                        "path": record.get("path"),
                    }
                )
        if render_only:
            return handle.getvalue()
        atomic_write(review_path, handle.getvalue())
        return review_path

    @staticmethod
    def _materialize_curated(
        *,
        documents_dir: Path,
        download_records: list[dict[str, object]],
        curated_dir: Path | None = None,
    ) -> tuple[Path, int, int]:
        """Publish documents, preserving prior curation on failure."""

        return materialize_curated(
            documents_dir=documents_dir,
            download_records=download_records,
            curated_dir=curated_dir,
        )

    @staticmethod
    def _record_is_curated(
        record: dict[str, object],
        *,
        review_ran: bool,
        review_failed: bool,
    ) -> bool:
        return not review_failed and record_is_curated(
            record, review_ran=review_ran
        )

    @classmethod
    def _apply_document_retention(
        cls,
        *,
        mode: str,
        documents_dir: Path,
        curated_dir: Path | None,
        download_records: list[dict[str, object]],
    ) -> tuple[int, int]:
        import shutil

        resolved_mode = str(mode or "all").strip().lower()
        if resolved_mode not in cls._ALLOWED_RETENTION_MODES:
            resolved_mode = "all"
        if resolved_mode == "all":
            return 0, 0

        files_removed = 0
        bytes_removed = 0

        def _remove_file(path: Path) -> None:
            nonlocal files_removed, bytes_removed
            try:
                if path.exists() and path.is_file():
                    try:
                        bytes_removed += int(path.stat().st_size)
                    except OSError:
                        pass
                    path.unlink()
                    files_removed += 1
            except OSError:
                return

        if resolved_mode == "none":
            if documents_dir.exists():
                for file_path in sorted(
                    (p for p in documents_dir.rglob("*") if p.is_file()),
                    reverse=True,
                ):
                    _remove_file(file_path)
                shutil.rmtree(documents_dir, ignore_errors=True)
            if curated_dir and curated_dir.exists():
                for file_path in sorted(
                    (p for p in curated_dir.rglob("*") if p.is_file()),
                    reverse=True,
                ):
                    _remove_file(file_path)
                shutil.rmtree(curated_dir, ignore_errors=True)
            return files_removed, bytes_removed

        review_ran = any(
            "review_selected" in record for record in download_records
        )
        kept_rel_paths: set[str] = set()
        for record in download_records:
            if cls._record_is_curated(
                record,
                review_ran=review_ran,
                review_failed=bool(record.get("review_failed")),
            ):
                rel = record.get("relative_path")
                if rel:
                    kept_rel_paths.add(str(rel))

        if documents_dir.exists():
            for record in download_records:
                rel = record.get("relative_path")
                if not rel:
                    continue
                rel_text = str(rel)
                if rel_text in kept_rel_paths:
                    continue
                file_path = documents_dir / rel_text
                _remove_file(file_path)
                if file_path.parent.exists():
                    cache_dir = file_path.parent / TEXT_CACHE_DIRNAME
                    if cache_dir.exists():
                        for cache_path in text_cache_paths(file_path):
                            _remove_file(cache_path)
            # Prune empty directories
            for dir_path in sorted(
                (p for p in documents_dir.rglob("*") if p.is_dir()),
                reverse=True,
            ):
                with contextlib.suppress(OSError):
                    dir_path.rmdir()
        return files_removed, bytes_removed

    @staticmethod
    def _partition_is_blocked(
        partition: Path, blocked: _PartitionIndex
    ) -> bool:
        return blocked.overlaps(partition)

    @staticmethod
    def _promote_to_consolidated_curated(
        *,
        run_curated_dir: Path,
        domain_dir: Path,
        attempted_partitions: set[Path] | None = None,
        blocked_partitions: set[Path] | _PartitionIndex | None = None,
        run_index: Path | None = None,
    ) -> None:
        """Publish complete partitions and provenance under a lock."""

        promote_curated(
            run_curated_dir=run_curated_dir,
            domain_dir=domain_dir,
            attempted_partitions=attempted_partitions,
            blocked_partitions=blocked_partitions,
            run_index=run_index,
        )

    @staticmethod
    def _merge_download_index(
        *,
        run_index: Path,
        domain_index: Path,
        promoted_paths: set[str],
        replaced_partitions: set[Path],
    ) -> None:
        """Atomically synchronize provenance for promoted files."""

        merge_download_index(
            run_index=run_index,
            domain_index=domain_index,
            promoted_paths=promoted_paths,
            replaced_partitions=replaced_partitions,
        )

    @staticmethod
    def _refresh_latest_pointer(
        run_dir: Path, *, domain_dir: Path | None = None
    ) -> None:
        """Atomically update latest, with a text fallback."""

        refresh_latest_pointer(run_dir, domain_dir=domain_dir)

    # ------------------------------------------------------------------
    # Checkpointing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _target_checkpoint_key(target: dict[str, object]) -> str:
        """Stable key for a target row used in checkpoint tracking."""
        label = target.get("label")
        if label and len(target) == 1:
            return str(label)
        return hashlib.md5(
            json.dumps(target, sort_keys=True, default=str).encode()
        ).hexdigest()[:12]

    @staticmethod
    def _checkpoint_path(manifest_path: Path) -> Path:
        """Path to the domain-level checkpoint file."""
        run_dir = manifest_path.parent
        domain_dir = (
            run_dir.parent.parent if run_dir.parent.name == "runs" else run_dir
        )
        return domain_dir / "checkpoint.json"

    @staticmethod
    def _load_checkpoint(
        path: Path,
        *,
        fingerprint: str | None = None,
        validate_artifacts: bool = True,
    ) -> dict[str, dict[str, object]]:
        """Load matching v2 completions with verified evidence."""

        return load_checkpoint(
            path,
            fingerprint=fingerprint,
            validate_artifacts=validate_artifacts,
        )

    @staticmethod
    def _save_checkpoint_entries(
        path: Path,
        new_entries: dict[str, dict[str, object]],
        *,
        fingerprint: str | None = None,
    ) -> None:
        """Merge v2 completions atomically, preserving corrupt state."""

        save_checkpoint_entries(path, new_entries, fingerprint=fingerprint)

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
        nice_to_have = list(classifier_cfg.get("nice_to_have_keywords") or [])
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

    def _code_host_get(
        self, request: DiscoveryRequest, ssl_verify: bool
    ) -> Callable[[str, dict], tuple[int, str]]:
        """Build an adapter getter returning (status, text).

        Adapter API endpoints use policy checks, not document selection.
        Headers and TLS verification remain shared with acquisition.
        """
        base_headers = self._resolve_request_headers(request)

        def gate(endpoint: str) -> object:
            return self._evaluate_request_policy(
                url=endpoint, request=request, ssl_verify=ssl_verify
            )

        def _get(url: str, headers: dict) -> tuple[int, str]:
            merged = dict(base_headers)
            merged.update(headers or {})
            response, _ = send_get(
                url,
                headers=merged,
                timeout=request.timeout_seconds or 30,
                verify=ssl_verify,
                gate=gate,
                limiter=self._rate_limiter,
                retry=(
                    request.retry_max_attempts,
                    request.retry_initial_backoff_seconds,
                    request.retry_max_backoff_seconds,
                ),
            )
            try:
                return response.status_code, response.text
            finally:
                response.close()

        return _get

    def _resolve_code_host_shells(
        self,
        downloads: list[dict[str, object]],
        notes: list[str],
        request: DiscoveryRequest,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Recover JS-shell content from known code hosts via their server API.

        For each downloaded HTML file that is a JS shell (little server-rendered
        text) whose URL matches a bundled code-host adapter (e.g. Municode),
        fetch the real ordinance text through the host's API, write it over the
        shell, and cache it — no browser required. Runs before browser
        escalation; anything unresolved is left untouched for that fallback.

        Enable with ``discovery.code_host_adapters.enabled: true``.
        Reuses the ``browser_escalation`` thresholds as the single definition of
        a "shell" so the two stages agree on what needs recovery.
        """
        import html
        from pathlib import Path

        from .code_host_adapters import resolve_adapter
        from .validators import ContentSampler

        cfg = getattr(request, "code_host_adapters", None)
        if isinstance(cfg, bool):
            cfg = {"enabled": cfg}
        cfg = cfg or {}
        if not cfg.get("enabled", False):
            return downloads, notes

        escalation_cfg = getattr(request, "browser_escalation", None) or {}
        if isinstance(escalation_cfg, bool):
            escalation_cfg = {}
        min_shell_chars = int(
            escalation_cfg.get(
                "min_shell_chars", DEFAULT_BROWSER_ESCALATION_MIN_SHELL_CHARS
            )
        )
        min_rendered_chars = int(
            escalation_cfg.get(
                "min_rendered_chars",
                DEFAULT_BROWSER_ESCALATION_MIN_RENDERED_CHARS,
            )
        )

        html_extensions = {".html", ".htm"}
        ssl_verify = self._downloads_ssl_verify()
        getter: Callable[[str, dict], tuple[int, str]] | None = None
        resolved = 0

        for record in downloads:
            if record.get("status") != "downloaded":
                continue
            file_path = record.get("path")
            if not file_path:
                continue
            path = Path(str(file_path))
            if path.suffix.lower() not in html_extensions:
                continue
            url = str(record.get("url") or record.get("final_url") or "")
            if not url:
                continue
            adapter = resolve_adapter(url)
            if adapter is None:
                continue

            # Shell detection: the same predicate as browser escalation.
            try:
                text = ContentSampler.extract_text(str(path))
            except Exception:  # noqa: BLE001 - unreadable file, skip
                continue
            if len(text.strip()) >= min_shell_chars:
                continue  # already has server content; not a shell

            # Honor robots/ToS per URL (the evaluator caches the robots parser
            # per host, so this stays cheap while keeping path granularity).
            try:
                policy = self._evaluate_request_policy(
                    url=url, request=request, ssl_verify=ssl_verify
                )
                allowed = bool(getattr(policy, "allowed", True))
            except (OSError, RuntimeError, ValueError):
                # Fail closed on policy I/O or invalid URL evaluation.
                allowed = False
            if not allowed:
                continue

            if getter is None:
                getter = self._code_host_get(request, ssl_verify)
            try:
                content = adapter.fetch_text(url, getter)
            except Exception:  # noqa: BLE001 - one bad fetch must not abort
                content = None
            if not content or len(content.strip()) < min_rendered_chars:
                continue

            # Write the recovered text back as valid, self-consistent HTML so
            # downstream .html extraction round-trips it — escaping keeps any
            # literal "<"/">" in ordinance text (e.g. "lots < 5,000 sq ft").
            # The clean text is also cached below for the fast path.
            wrapped = (
                "<!doctype html><html><body><pre>"
                + html.escape(content)
                + "</pre></body></html>"
            )
            try:
                self._replace_recovered_content(path, wrapped, record)
            except OSError:
                continue
            # Mark so downstream dedup re-hashes (bytes changed on disk) and so
            # the review index shows how the content was recovered.
            record["code_host_adapter"] = adapter.name
            from ..extraction.document_utils import write_text_cache

            try:
                write_text_cache(path, content, method="code_host_api")
            except Exception:  # noqa: BLE001 - text cache is best-effort
                pass
            resolved += 1

        if resolved:
            notes.append(
                f"Code-host adapters: recovered {resolved} JS-shell "
                "document(s) via server API (no browser)."
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
            settle_seconds: Time to wait for SPA to render (default: 15)
            min_shell_chars: Below this = JS shell detected (default: 8000)
            min_rendered_chars: Rendered text must exceed this (default: 10000)
            enabled: true/false (default: false)

        This is a general-purpose escalation that works for any domain without
        site-specific configuration.
        """
        from pathlib import Path

        from .validators import ContentSampler

        # Load configurable thresholds from discovery config
        escalation_cfg = getattr(request, "browser_escalation", None) or {}
        if isinstance(escalation_cfg, bool):
            escalation_cfg = {"enabled": escalation_cfg}
        if not escalation_cfg.get("enabled", False):
            return downloads, notes

        settle_seconds = float(
            escalation_cfg.get(
                "settle_seconds", DEFAULT_BROWSER_ESCALATION_SETTLE_SECONDS
            )
        )
        min_shell_chars = int(
            escalation_cfg.get(
                "min_shell_chars", DEFAULT_BROWSER_ESCALATION_MIN_SHELL_CHARS
            )
        )
        min_rendered_chars = int(
            escalation_cfg.get(
                "min_rendered_chars",
                DEFAULT_BROWSER_ESCALATION_MIN_RENDERED_CHARS,
            )
        )

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
                    allowed, reason = self._passes_final_url_selection_guard(
                        final_url=url, request=request
                    )
                    if not allowed:
                        record.update(
                            status="skipped_selection_filter", reason=reason
                        )
                        continue
                    self._browser_policy_gate(request, url)
                    rendered_html = browser.fetch_html(
                        url, settle_seconds=settle_seconds
                    )
                    observed_url = browser.current_url
                    allowed, reason = self._passes_final_url_selection_guard(
                        final_url=observed_url, request=request
                    )
                    if not observed_url or not allowed:
                        record.update(
                            status="skipped_selection_filter",
                            reason=reason or "unverified_browser_origin",
                        )
                        continue
                    record["final_url"] = observed_url
                    if (
                        not rendered_html
                        or len(rendered_html.strip()) < MIN_RENDERED_HTML_CHARS
                    ):
                        continue

                    # Validate content before replacing known bytes.
                    text = ContentSampler.extract_text_from_string(
                        rendered_html
                    )
                    if len(text.strip()) >= min_rendered_chars:
                        self._replace_recovered_content(
                            file_path, rendered_html, record
                        )
                        escalated += 1
                        record["browser_escalated"] = True

                        # Cache extracted text as .text/ file (same pattern as
                        # OCR'd PDFs). Users can inspect the .txt instead of
                        # opening raw HTML, and the extraction pipeline reuses
                        # it without re-parsing.
                        from ..extraction.document_utils import (
                            write_text_cache,
                        )

                        write_text_cache(
                            file_path, text, method="browser_render"
                        )
                    else:
                        # Page rendered but insufficient content (likely a TOC).
                        # Try following keyword-matching links 1 level deeper.
                        classifier_cfg = (
                            getattr(request, "document_classifier", None) or {}
                        )
                        keywords = (
                            classifier_cfg.get("nice_to_have_keywords") or []
                        )
                        if keywords and rendered_html:
                            deeper_url = self._find_keyword_link_in_rendered(
                                rendered_html, url, keywords
                            )
                            if (
                                deeper_url
                                and self._passes_final_url_selection_guard(
                                    final_url=deeper_url, request=request
                                )[0]
                            ):
                                try:
                                    self._browser_policy_gate(
                                        request, deeper_url
                                    )
                                    deeper_html = browser.fetch_html(
                                        deeper_url,
                                        settle_seconds=settle_seconds,
                                    )
                                    deeper_final_url = browser.current_url
                                    if (
                                        deeper_html
                                        and deeper_final_url
                                        and (
                                            self
                                            ._passes_final_url_selection_guard(
                                                final_url=deeper_final_url,
                                                request=request,
                                            )[0]
                                        )
                                    ):
                                        deeper_text = (
                                            ContentSampler
                                            .extract_text_from_string(
                                                deeper_html
                                            )
                                        )
                                        if (
                                            len(deeper_text.strip())
                                            >= min_rendered_chars
                                        ):
                                            # Save a new validated file.
                                            deeper_path = file_path.with_stem(
                                                f"{file_path.stem}-deep"
                                            )
                                            new_record = dict(record)
                                            self._replace_recovered_content(
                                                deeper_path,
                                                deeper_html,
                                                new_record,
                                            )
                                            write_text_cache(
                                                deeper_path,
                                                deeper_text,
                                                method="browser_render",
                                            )
                                            new_record["path"] = str(
                                                deeper_path
                                            )
                                            new_record["relative_path"] = str(
                                                Path(
                                                    record.get(
                                                        "relative_path", ""
                                                    )
                                                ).parent
                                                / deeper_path.name
                                            )
                                            new_record["url"] = deeper_url
                                            new_record["final_url"] = (
                                                deeper_final_url
                                            )
                                            new_record["browser_escalated"] = (
                                                True
                                            )
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

    # ------------------------------------------------------------------
    # Shared post-review retry helpers (used by both retry stages).
    # ------------------------------------------------------------------

    @staticmethod
    def _group_records_by_target_label(
        download_records: list[dict[str, object]],
    ) -> dict[str, list[dict[str, object]]]:
        """Group download records by their target label.

        Records with no usable target label are dropped (matching the prior
        per-stage grouping). Shared by both post-review retry stages.
        """
        from collections import defaultdict

        by_target: dict[str, list[dict[str, object]]] = defaultdict(list)
        for rec in download_records:
            meta = rec.get("target_metadata") or {}
            key = (
                DiscoveryEngine._target_checkpoint_key(meta)
                if meta else str(rec.get("target_label") or "")
            )
            if key:
                by_target[key].append(rec)
        return by_target

    def _download_and_review_retry_candidates(
        self,
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
        documents_dir: Path,
        review_cfg: dict[str, object],
        classifier_keywords: list[str] | None,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Download a retry candidate batch and (optionally) review it.

        This is the shared crawl/download/review core for both post-review
        retry stages. Review runs only when there are downloads AND a
        ``review_cfg`` is configured, matching the prior per-stage logic.
        Returns ``(retry_downloads, review_notes)``; callers decide whether
        to surface ``review_notes`` (code-hosting keeps them, JS-shell drops
        them, as before).
        """
        cap = getattr(self, "_remaining_downloads", None)
        retry_downloads, errors, notes = self._download_candidates(
            request=request,
            candidates=candidates,
            documents_dir=documents_dir,
            max_downloads=(
                min(len(candidates), cap)
                if cap is not None
                else len(candidates)
            ),
        )
        if cap is not None:
            self._remaining_downloads = max(0, cap - len(retry_downloads))
        self._retry_errors.extend(errors)
        retry_downloads, notes, costs = self._process_downloads(
            request, retry_downloads, notes
        )
        for key, value in costs.items():
            if isinstance(value, (int, float)):
                self._retry_costs[key] += value
        return retry_downloads, notes

    def _run_post_review_retries(
        self,
        *,
        request: DiscoveryRequest,
        download_records: list[dict[str, object]],
        notes: list[str],
        documents_dir: Path,
        review_cfg: dict[str, object],
        classifier_keywords: list[str] | None,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Run both post-review retry stages under one shared target budget.

        Preserves the historical sequencing exactly: the code-hosting SerpApi
        retry runs first (only when ``link_prioritization_domain_scores`` is
        configured), then the JS-shell browser-crawl retry. A single
        :class:`_RetryBudget` is shared across both so the opt-in cap
        (``retry_max_retry_targets``) bounds the combined per-run total. When
        the cap is unset (None) the budget is unbounded and behavior is
        identical to before this change.
        """
        max_retry_targets = getattr(request, "retry_max_retry_targets", None)
        budget = _RetryBudget(max_retry_targets)

        if request.link_prioritization_domain_scores:
            download_records, notes = (
                self._retry_failed_targets_on_code_hosting(
                    request=request,
                    download_records=download_records,
                    notes=notes,
                    documents_dir=documents_dir,
                    review_cfg=review_cfg,
                    classifier_keywords=classifier_keywords,
                    budget=budget,
                )
            )

        download_records, notes = self._retry_js_shells_with_digger(
            request=request,
            download_records=download_records,
            notes=notes,
            documents_dir=documents_dir,
            review_cfg=review_cfg,
            budget=budget,
        )
        return download_records, notes

    def _retry_js_shells_with_digger(
        self,
        *,
        request: DiscoveryRequest,
        download_records: list[dict[str, object]],
        notes: list[str],
        documents_dir: Path,
        review_cfg: dict[str, object],
        budget: "_RetryBudget | None" = None,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Browser-crawl retry for failed targets with JS-shell downloads.

        After review, some targets have 0 curated docs but DO have downloaded
        HTML files that were identified as JS shells (municode, etc.). For these,
        launch the HTTP digger (with browser fallback) on the code-hosting URL
        to crawl 2-3 levels deep and find keyword-matching section pages.

        This is the hybrid approach: fast SerpApi for most targets, targeted
        browser crawl only for the ~15% that need it.

        ``budget`` (optional) caps how many JS-shell targets this stage may
        crawl; when ``None`` (the default) the stage is unbounded, matching
        the historical behavior.
        """
        from pathlib import Path as _Path

        if not self._browser_recovery_enabled(request):
            return download_records, notes
        classifier_cfg = getattr(request, "document_classifier", None) or {}
        keywords = classifier_cfg.get("nice_to_have_keywords") or []
        if not keywords:
            return download_records, notes

        domain_scores = request.link_prioritization_domain_scores or {}
        code_hosting_domains = set(domain_scores.keys())
        if not code_hosting_domains:
            return download_records, notes

        # Find targets with 0 curated AND JS-shell downloads from code-hosting domains
        by_target = self._group_records_by_target_label(download_records)

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

        # Opt-in cap: bound how many JS-shell targets this stage crawls. When
        # no budget is passed (or it is unbounded) this is a no-op.
        if budget is not None:
            allowed = budget.allow(len(shell_urls))
            if allowed < len(shell_urls):
                notes.append(
                    f"Browser crawl retry: retry-target budget limited crawls "
                    f"to {allowed} of {len(shell_urls)} JS-shell target(s)."
                )
                shell_urls = shell_urls[:allowed]
            if not shell_urls:
                return download_records, notes

        # Use the HTTP digger with browser to crawl from each shell URL. A
        # single connector instance is reused across every shell target so the
        # browser session (when the digger escalates) is shared, not rebuilt.
        from .connectors.base import DiggerInput
        from .connectors.digger import HttpDiggerConnector

        digger = HttpDiggerConnector()
        crawled = 0
        for target_key, url, target_meta in shell_urls:
            di = DiggerInput(
                seed_urls=[url],
                max_depth=JS_SHELL_RETRY_MAX_DEPTH,
                max_pages=JS_SHELL_RETRY_MAX_PAGES,
                max_files=JS_SHELL_RETRY_MAX_FILES,
                timeout_seconds=JS_SHELL_RETRY_TIMEOUT_SECONDS,
                include_link_text_patterns=keywords,
                extra_params={
                    "ssl_verify": self._downloads_ssl_verify(),
                    "browser_escalation": self._browser_recovery_enabled(
                        request
                    ),
                    "request_gate": self._network_gate(
                        request, self._downloads_ssl_verify()
                    ),
                    "response_url_gate": self._network_gate(
                        request, self._downloads_ssl_verify()
                    ),
                    "rate_limiter": self._rate_limiter,
                    "browser_policy_strict": (
                        request.robots_policy_mode == "enforce"
                        or request.tos_policy_mode == "enforce"
                    ),
                },
            )
            try:
                artifacts = digger.discover(di)
            except (OSError, RuntimeError, ValueError) as exc:
                # Isolate network, browser, and crawl-input failures.
                self._retry_errors.append(
                    build_error_record(
                        exc, stage="discovery.retry_crawl", provider="http"
                    )
                )
                continue

            if not artifacts:
                continue

            # Download and review the found artifacts
            from .models import CandidateScore, DiscoveryCandidate
            artifact_candidates = [
                DiscoveryCandidate(
                    url=a.url,
                    source="browser_crawl_retry",
                    score=CandidateScore(
                        url_signal=JS_SHELL_RETRY_URL_SIGNAL, anchor_signal=0.0,
                        content_signal=0.0, trust_signal=JS_SHELL_RETRY_TRUST_SIGNAL,
                    ),
                    target_metadata=target_meta,
                )
                for a in artifacts
            ]

            # JS-shell path discards review notes (matches prior behavior).
            retry_downloads, retry_notes = (
                self._download_and_review_retry_candidates(
                    request=request,
                    candidates=artifact_candidates,
                    documents_dir=documents_dir,
                    review_cfg=review_cfg,
                    classifier_keywords=keywords,
                )
            )

            notes.extend(retry_notes)
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
        budget: "_RetryBudget | None" = None,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Targeted retry for targets that got 0 curated docs from code-hosting sites.

        When a target has candidates from known code-hosting domains (municode,
        ecode360, amlegal, etc.) but none scored high enough for curation, this
        fires one additional SerpApi query scoped to the domain path already
        discovered, combined with domain keywords. This finds deeper sections
        (like specific chapters) that the initial broad query missed.

        Only fires for failed targets — typically 1-2 extra queries per run.

        ``budget`` (optional) caps how many failed targets this stage retries;
        when ``None`` (the default) the stage is unbounded, matching the
        historical behavior.
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

        by_target = self._group_records_by_target_label(download_records)

        failed_targets: list[tuple[str, str, dict[str, object]]] = []
        for target_key, records in by_target.items():
            has_curated = any(r.get("review_selected") for r in records)
            if has_curated:
                continue

            # Find code-hosting URLs for this target
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

        # Opt-in cap: bound how many failed targets this stage retries. When
        # no budget is passed (or it is unbounded) this is a no-op.
        if budget is not None:
            allowed = budget.allow(len(failed_targets))
            if allowed < len(failed_targets):
                notes.append(
                    f"Code-hosting retry: retry-target budget limited retries "
                    f"to {allowed} of {len(failed_targets)} failed target(s)."
                )
                failed_targets = failed_targets[:allowed]
            if not failed_targets:
                return download_records, notes

        # Fire targeted retry queries
        retry_notes: list[str] = []
        keyword_or = " OR ".join(f'"{k}"' for k in keywords[:3])

        from .connectors.base import SeekerInput
        from .connectors.serpapi_seeker import SerpApiSeeker

        seeker = SerpApiSeeker(
            retry_max_attempts=request.retry_max_attempts,
            retry_initial_backoff_seconds=request.retry_initial_backoff_seconds,
            retry_max_backoff_seconds=request.retry_max_backoff_seconds,
            min_request_interval_seconds=max(
                0.0, float(request.min_request_interval_ms) / 1000.0
            ),
            cache_dir=DiscoveryEngine._resolve_seeker_cache_dir(request),
            timeout_seconds=request.timeout_seconds or 45,
            cache_ttl_seconds=max(
                0.0,
                float(
                    getattr(request, "seeker_cache_ttl_minutes", 0.0) or 0.0
                ),
            )
            * 60.0,
            cache_refresh=bool(
                getattr(request, "reprocess", False)
                and not getattr(request, "dry_run", False)
            ),
        )

        retry_candidates = []
        for target_key, site_prefix, target_meta in failed_targets:
            query = f"site:{site_prefix} {keyword_or}"
            try:
                before = seeker.queries_run
                results = seeker.discover(
                    SeekerInput(query=query, max_results=5)
                )
            except Exception as exc:
                self._retry_errors.append(
                    build_error_record(
                        exc,
                        stage="discovery.retry_search",
                        provider="serpapi",
                    )
                )
                notes.append(
                    f"Code-hosting retry: query failed for '{target_key}'."
                )
                continue
            finally:
                self._retry_search_calls += seeker.queries_run - before

            if not results:
                continue

            for result in results[:2]:
                url = result.get("link") or result.get("url")
                if url:
                    retry_candidates.append(
                        {
                            "url": url,
                            "target_metadata": target_meta,
                            "source": "code_hosting_retry",
                        }
                    )

            retry_notes.append(
                f"Code-hosting retry for '{target_key}': queried site:{site_prefix}, "
                f"found {len(results)} result(s)."
            )

        if not retry_candidates:
            notes.extend(retry_notes)
            return download_records, notes

        # Download retry candidates
        from .models import CandidateScore, DiscoveryCandidate

        candidates_for_download = [
            DiscoveryCandidate(
                url=c["url"],
                source="code_hosting_retry",
                score=CandidateScore(
                    url_signal=0.6,
                    anchor_signal=0.0,
                    content_signal=0.0,
                    trust_signal=0.4,
                ),
                target_metadata=c.get("target_metadata"),
            )
            for c in retry_candidates
        ]

        # Download + review the retry batch via the shared retry core. The
        # code-hosting path keeps the review notes (unlike the JS-shell path).
        retry_downloads, retry_review_notes = (
            self._download_and_review_retry_candidates(
                request=request,
                candidates=candidates_for_download,
                documents_dir=documents_dir,
                review_cfg=review_cfg,
                classifier_keywords=classifier_keywords,
            )
        )
        retry_notes.extend(retry_review_notes)

        # Merge into main records
        download_records.extend(retry_downloads)
        curated_retry = sum(
            1 for r in retry_downloads if r.get("review_selected")
        )
        retry_notes.append(
            f"Code-hosting retry: {len(retry_downloads)} downloaded, "
            f"{curated_retry} curated from retry."
        )
        notes.extend(retry_notes)
        return download_records, notes

    @staticmethod
    def _replace_recovered_content(
        path: Path, text: str, record: dict[str, object]
    ) -> None:
        """Publish recovery bytes and refresh their identity."""
        raw = text.encode("utf-8")
        atomic_write(path, raw)
        record.update(
            bytes=len(raw),
            content_hash=hashlib.sha256(raw).hexdigest(),
            mime_type="text/html",
        )

    def _browser_policy_gate(
        self, request: DiscoveryRequest, url: str
    ) -> None:
        """Gate navigation, not uncontrolled browser subresources."""
        if (
            request.robots_policy_mode == "enforce"
            or request.tos_policy_mode == "enforce"
        ):
            msg = (
                "Strict policy enforcement cannot cover browser "
                "subresources; use HTTP acquisition"
            )
            raise RuntimeError(msg)
        check_url(
            url, self._network_gate(request, self._downloads_ssl_verify())
        )
        if self._rate_limiter:
            self._rate_limiter.wait()

    def _process_downloads(
        self,
        request: DiscoveryRequest,
        records: list[dict[str, object]],
        notes: list[str],
    ) -> tuple[list[dict[str, object]], list[str], dict[str, object]]:
        """Apply recovery, classification, and review to each batch."""
        records, notes = self._resolve_code_host_shells(
            records, notes, request
        )
        records, notes = self._escalate_js_shells_to_browser(
            records, notes, request
        )
        classifier = request.document_classifier or {}
        if classifier:
            records, notes = self._run_post_download_classifier(
                records, classifier, notes
            )
        costs: dict[str, object] = {}
        if request.document_review:
            records, notes, costs = self._run_document_review(
                records,
                request.document_review,
                notes,
                models=request.models,
                classifier_keywords=classifier.get("nice_to_have_keywords"),
            )
        return records, notes, costs

    @staticmethod
    def _run_document_review(
        downloads: list[dict[str, object]],
        review_cfg: dict[str, object],
        notes: list[str],
        models: dict[str, object] | None = None,
        classifier_keywords: list[str] | None = None,
    ) -> tuple[list[dict[str, object]], list[str], dict[str, object]]:
        """LLM-grade downloaded files and promote the primary one(s).

        Annotate records and move selected files into ``reviewed/``
        when configured. Review errors remain isolated
        from acquisition and prevent incomplete curation from replacing
        previously published documents.

        Returns (downloads, notes, costs_dict).
        """
        description = str(review_cfg.get("document_description") or "").strip()
        if not description:
            notes.append(
                "Document review skipped (no document_description configured)."
            )
            return downloads, notes, {}

        reviewer = DocumentReviewer(
            document_description=description,
            model=(
                str(review_cfg["model"]) if review_cfg.get("model") else None
            ),
            models={str(k): str(v) for k, v in (models or {}).items()} or None,
            keep_top=int(review_cfg.get("keep_top", 1) or 1),
            max_chars=int(review_cfg.get("max_chars", 12000) or 12000),
            review_keywords=review_cfg.get("keywords") or classifier_keywords,
            deduplicate_redundant=bool(
                review_cfg.get("deduplicate_redundant", True)
            ),
            dedup_key_fields=[
                str(field)
                for field in (
                    (review_cfg.get("deduplication") or {}).get("key_fields")
                    or []
                )
                if str(field).strip()
            ]
            or None,
        )
        try:
            downloads, notes = reviewer.review(downloads, notes)
            return downloads, notes, reviewer.get_costs()
        except Exception as exc:  # noqa: BLE001 - review is best-effort
            # Failed review protects prior consolidated partitions.
            for _rec in downloads:
                _rec["review_failed"] = True
            notes.append(f"Document review error (skipped): {exc}")
            return downloads, notes, reviewer.get_costs()

    def _completion_partition(
        self,
        request: DiscoveryRequest,
        documents_dir: Path,
        *,
        url: str = "",
        target_metadata: dict[str, object] | None = None,
    ) -> Path:
        """Resolve relative completion and promotion partitions."""
        _, _, partition = self._resolve_partition_dir(
            documents_dir=documents_dir,
            url=url,
            request=request,
            target_metadata=target_metadata,
        )
        return partition.relative_to(documents_dir)

    def _initial_completion_blocks(
        self,
        *,
        request: DiscoveryRequest,
        documents_dir: Path,
        original_targets: list[dict[str, object]],
        completed_entries: dict[str, dict[str, object]],
        selection_metrics: list[dict[str, object]],
        seeker_failed: bool,
    ) -> tuple[set[str], _PartitionIndex]:
        """Protect reused targets and unfinished searches."""
        incomplete_keys = {
            self._target_checkpoint_key(
                request.targets[int(metric["target_index"])]
            )
            for metric in selection_metrics
            if metric.get("search_complete") is False and request.targets
        }
        if seeker_failed:
            incomplete_keys.update(
                self._target_checkpoint_key(target)
                for target in original_targets
            )
        blocked = _PartitionIndex()
        target_partitioned = bool(
            request.partition_by
            or request.partition_mode == "jurisdiction"
            or (request.state and request.jurisdiction)
        )
        for target in original_targets:
            key = self._target_checkpoint_key(target)
            if key not in incomplete_keys and key not in completed_entries:
                continue
            blocked.add(
                self._completion_partition(
                    request, documents_dir, target_metadata=target
                )
                if target_partitioned
                else Path()
            )
        if not original_targets and any(
            metric.get("search_complete") is False
            for metric in selection_metrics
        ):
            blocked.add(Path())
        return incomplete_keys, blocked

    def _block_capped_candidates(
        self,
        *,
        request: DiscoveryRequest,
        documents_dir: Path,
        candidates: list[DiscoveryCandidate],
        download_cap: int,
        incomplete_keys: set[str],
        blocked: _PartitionIndex,
    ) -> None:
        """Protect targets and partitions truncated by downloads."""
        for candidate in candidates[download_cap:]:
            if candidate.target_metadata:
                incomplete_keys.add(
                    self._target_checkpoint_key(candidate.target_metadata)
                )
            blocked.add(
                self._completion_partition(
                    request,
                    documents_dir,
                    url=candidate.url,
                    target_metadata=candidate.target_metadata,
                )
            )

    def _download_completions(
        self,
        *,
        request: DiscoveryRequest,
        documents_dir: Path,
        download_records: list[dict[str, object]],
        incomplete_keys: set[str],
        blocked: _PartitionIndex,
        run_id: str,
        started_at: datetime,
    ) -> dict[str, dict[str, object]]:
        """Count successes after blocking all failed partitions.

        Entries stay in memory until publication provides evidence.
        Unattributed failures also protect their partitions.
        """
        failed_keys: set[str] = set()
        for record in download_records:
            if not (
                record.get("status") == "failed" or record.get("review_failed")
            ):
                continue
            metadata = record.get("target_metadata")
            if metadata:
                failed_keys.add(self._target_checkpoint_key(metadata))
            blocked.add(
                self._completion_partition(
                    request,
                    documents_dir,
                    url=str(record.get("url") or ""),
                    target_metadata=metadata,
                )
            )

        entries: dict[str, dict[str, object]] = {}
        for record in download_records:
            metadata = record.get("target_metadata")
            if not metadata or record.get("status") != "downloaded":
                continue
            key = self._target_checkpoint_key(metadata)
            if key in failed_keys or key in incomplete_keys:
                continue
            if blocked and self._partition_is_blocked(
                self._completion_partition(
                    request,
                    documents_dir,
                    url=str(record.get("url") or ""),
                    target_metadata=metadata,
                ),
                blocked,
            ):
                continue
            if key not in entries:
                entries[key] = {
                    "completed_at": started_at.isoformat(),
                    "run_id": run_id,
                    "download_count": 0,
                }
            entries[key]["download_count"] += 1
        return entries

    def _empty_target_completions(
        self,
        *,
        request: DiscoveryRequest,
        documents_dir: Path,
        selection_metrics: list[dict[str, object]],
        download_records: list[dict[str, object]],
        incomplete_keys: set[str],
        entries: dict[str, dict[str, object]],
        run_id: str,
        started_at: datetime,
    ) -> set[Path]:
        """Complete exhausted empty searches without attempted seeds."""
        if request.dry_run or not request.targets:
            return set()
        attempted_keys = {
            self._target_checkpoint_key(record["target_metadata"])
            for record in download_records
            if record.get("target_metadata")
        }
        empty_parts: set[Path] = set()
        for metric in selection_metrics:
            if (
                metric.get("search_complete") is False
                or metric.get("selected_candidates")
            ):
                continue
            target = request.targets[int(metric["target_index"])]
            key = self._target_checkpoint_key(target)
            if key in incomplete_keys or key in attempted_keys:
                continue
            entries[key] = {
                "run_id": run_id,
                "completed_at": started_at.isoformat(),
                "outcome": "empty",
                "download_count": 0,
            }
            if (
                request.partition_by
                or request.partition_mode == "jurisdiction"
            ):
                empty_parts.add(
                    self._completion_partition(
                        request, documents_dir, target_metadata=target
                    )
                )
        return empty_parts

    @staticmethod
    def _preview_completion_result(
        *,
        request: DiscoveryRequest,
        paths: PublicationPaths,
        run_id: str,
        candidates: list[DiscoveryCandidate],
        download_records: list[dict[str, object]],
        errors: list[dict[str, object]],
        empty_parts: set[Path],
        completed_entries: dict[str, dict[str, object]],
        all_reused: bool,
    ) -> DiscoveryResult:
        """Preview result metadata without writing final artifacts."""
        result = DiscoveryResult(
            run_id=run_id,
            manifest_path=paths.manifest_path,
            documents_dir=paths.documents_dir,
            dry_run=request.dry_run,
            errors=errors,
        )
        if not request.dry_run:
            if not all_reused or candidates:
                result.download_index_path = paths.run_index
            if download_records:
                result.curated_dir = paths.run_curated_dir
                result.review_index_path = (
                    paths.manifest_path.parent / "review.csv"
                )
                reviewed = any(
                    record.get("review_selected") is not None
                    for record in download_records
                )
                result.curated_count = sum(
                    record_is_curated(record, review_ran=reviewed)
                    for record in download_records
                )
            elif empty_parts and request.reprocess:
                result.curated_dir = paths.run_curated_dir
        result.status = discovery_status(
            dry_run=request.dry_run,
            errors=errors,
            usable=(
                bool(candidates)
                if request.dry_run
                else result.curated_count > 0
            ),
        )
        if not request.dry_run and all_reused and not errors:
            curated_paths = {
                path
                for entry in completed_entries.values()
                for path in entry.get("curated_paths", {})
            }
            if curated_paths:
                result.status = "reused"
                result.curated_dir = paths.domain_dir / "curated"
                result.curated_count = len(curated_paths)
                result.download_index_path = (
                    paths.domain_dir / "download_index.csv"
                )
        return result

    def _build_completion_manifest(
        self,
        *,
        request: DiscoveryRequest,
        paths: PublicationPaths,
        result: DiscoveryResult,
        started_at: datetime,
        normalized_seed_urls: list[str],
        candidates: list[DiscoveryCandidate],
        download_records: list[dict[str, object]],
        stage_summaries: dict[str, object],
        lineage: dict[str, object],
        notes: list[str],
    ) -> DiscoveryManifest:
        """Combine result metadata and stage evidence."""
        completed_at = datetime.now(timezone.utc)
        return DiscoveryManifest(
            run_id=result.run_id,
            status=result.status,
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
                "elapsed_seconds": round(
                    (completed_at - started_at).total_seconds(), 3
                ),
            },
            stage_summaries=stage_summaries,
            candidate_summary=self._build_candidate_summary(candidates),
            lineage={
                "run_id": result.run_id,
                "documents_dir": paths.documents_dir.as_posix(),
                "manifest_path": paths.manifest_path.as_posix(),
                "domain_dir": paths.domain_dir.as_posix(),
                **lineage,
                "download_index_csv": (
                    result.download_index_path.as_posix()
                    if result.download_index_path is not None
                    else None
                ),
                "review_index_csv": (
                    result.review_index_path.as_posix()
                    if result.review_index_path is not None
                    else None
                ),
                "curated_dir": (
                    result.curated_dir.as_posix()
                    if result.curated_dir is not None
                    else None
                ),
                "curated_count": result.curated_count,
            },
            candidates=candidates,
            downloads=download_records,
            errors=result.errors,
            error_summary=summarize_error_records(result.errors),
            notes=notes,
        )

    def _publish_completion(
        self,
        *,
        request: DiscoveryRequest,
        paths: PublicationPaths,
        result: DiscoveryResult,
        manifest: DiscoveryManifest,
        completed_entries: dict[str, dict[str, object]],
        attempted_parts: set[Path],
        blocked: _PartitionIndex,
        all_reused: bool,
    ) -> Path:
        """Render metadata before publishing it with selected curation.

        Reuse returns a new snapshot, preserving the editable run.
        Immutable snapshots also provide checkpoint manifest evidence.
        """
        payload = manifest.to_dict()
        if result.status == "reused":
            payload["lineage"]["reused_manifests"] = list(
                {
                    path
                    for entry in completed_entries.values()
                    for path in entry.get("artifacts", {})
                    if path.endswith(".json")
                }
            )
        snapshot_path = (
            paths.manifest_path.parent / "outcomes" / f"{result.run_id}.json"
        )
        manifest_content = json.dumps(payload, indent=2, sort_keys=True)
        if all_reused:
            atomic_write(snapshot_path, manifest_content)
            result.manifest_path = snapshot_path
            return snapshot_path
        metadata_updates = {
            paths.manifest_path: manifest_content,
            snapshot_path: manifest_content,
        }
        if not request.dry_run:
            index_content = self._write_download_index(
                request=request,
                run_id=result.run_id,
                manifest_path=paths.manifest_path,
                download_records=manifest.downloads,
                index_path=paths.run_index,
                render_only=True,
            )
            metadata_updates[paths.run_index] = index_content
            metadata_updates[snapshot_path.with_suffix(".index.csv")] = (
                index_content
            )
            if result.review_index_path:
                metadata_updates[result.review_index_path] = (
                    self._write_review_index(
                        request=request,
                        download_records=manifest.downloads,
                        run_dir=paths.manifest_path.parent,
                        render_only=True,
                    )
                )
            if result.curated_dir is not None:
                publish_manual_curation(
                    paths=paths,
                    download_records=manifest.downloads,
                    run_index=paths.run_index,
                    run_index_content=index_content,
                    metadata_updates=metadata_updates,
                    attempted_partitions=attempted_parts,
                    blocked_partitions=blocked,
                )
                return snapshot_path
        # Metadata-only outcomes do not replace curation.
        for path, content in metadata_updates.items():
            atomic_write(path, content)
        return snapshot_path

    def _save_completion_evidence(
        self,
        *,
        paths: PublicationPaths,
        result: DiscoveryResult,
        download_records: list[dict[str, object]],
        entries: dict[str, dict[str, object]],
        snapshot_path: Path,
        checkpoint_path: Path,
        fingerprint: str,
    ) -> None:
        """Save completions with published artifact hashes."""
        if result.dry_run or not entries or not result.download_index_path:
            return
        evidence = {
            str(snapshot_path): artifact_sha256(snapshot_path),
            str(snapshot_path.with_suffix(".index.csv")): artifact_sha256(
                snapshot_path.with_suffix(".index.csv")
            ),
        }
        curated_paths: dict[str, dict[str, str]] = {}
        for record in download_records:
            metadata = record.get("target_metadata")
            relative_path = record.get("relative_path")
            if not metadata or not relative_path:
                continue
            key = self._target_checkpoint_key(metadata)
            if key not in entries:
                continue
            path = paths.domain_dir / "curated" / str(relative_path)
            if path.is_file():
                curated_paths.setdefault(key, {})[str(path)] = artifact_sha256(
                    path
                )
        for key, entry in entries.items():
            entry["artifacts"] = evidence
            entry["curated_paths"] = curated_paths.get(key, {})
        self._save_checkpoint_entries(
            checkpoint_path, entries, fingerprint=fingerprint
        )

    def _finalize_completion(
        self,
        *,
        request: DiscoveryRequest,
        paths: PublicationPaths,
        result: DiscoveryResult,
        download_records: list[dict[str, object]],
        entries: dict[str, dict[str, object]],
        snapshot_path: Path,
        checkpoint_path: Path,
        fingerprint: str,
        all_reused: bool,
    ) -> None:
        """Finalize retention, checkpoint evidence, and latest."""
        if request.dry_run:
            return
        if not all_reused and request.retention_documents != "all":
            self._apply_document_retention(
                mode=request.retention_documents,
                documents_dir=paths.documents_dir,
                curated_dir=result.curated_dir,
                download_records=download_records,
            )
        self._save_completion_evidence(
            paths=paths,
            result=result,
            download_records=download_records,
            entries=entries,
            snapshot_path=snapshot_path,
            checkpoint_path=checkpoint_path,
            fingerprint=fingerprint,
        )
        # Reuse and empty runs keep the prior latest-downloads pointer.
        if request.output_manifest is None and download_records:
            self._refresh_latest_pointer(
                paths.manifest_path.parent, domain_dir=paths.domain_dir
            )

    def run(self, request: DiscoveryRequest) -> DiscoveryResult:
        """Execute a full discovery run for the given request.

        Parameters
        ----------
        request : DiscoveryRequest
            Fully resolved discovery configuration and seed inputs.

        Returns
        -------
        DiscoveryResult
            Manifest path, downloaded documents, and acquisition errors.
        """
        started_at = datetime.now(timezone.utc)
        self._rate_limiter = _RequestRateLimiter(
            request.min_request_interval_ms
        )
        self._retry_errors = []
        self._retry_costs = Counter()
        self._retry_search_calls = 0
        self._remaining_downloads = request.max_files
        run_id = self._build_run_id(request, started_at)
        documents_dir, manifest_path = self._resolve_output_paths(
            request, run_id
        )
        paths = resolve_publication_paths(
            documents_dir, manifest_path, request.domain
        )
        documents_dir, manifest_path = paths.documents_dir, paths.manifest_path
        fingerprint = effective_discovery_fingerprint(request)
        if not request.dry_run:
            with discovery_publication(paths.domain_dir):
                pass
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

        # --fresh clears evidence instead of reusing completions.
        original_targets = list(request.targets or [])
        checkpoint_path = paths.domain_dir / "checkpoint.json"
        if request.reprocess:
            if not request.dry_run:
                with contextlib.suppress(OSError):
                    checkpoint_path.unlink(missing_ok=True)
            completed_checkpoint_keys: dict[str, dict[str, object]] = {}
        else:
            completed_checkpoint_keys = self._load_checkpoint(
                checkpoint_path, fingerprint=fingerprint
            )
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
                    f"target(s) (of {original_target_count} total). "
                    f"Use --fresh to include them."
                )

        # Search only active targets with a healthy provider.
        prioritizer_lineage: list[dict[str, object]] = []
        target_selection_metrics: list[dict[str, object]] = []
        seeker_raw_count = 0
        seeker_queries_run = 0
        if seeker_errors or (original_targets and not request.targets):
            seeker_candidates: list[DiscoveryCandidate] = []
        else:
            self._emit_progress(request, "run: seeker stage started")
            (
                seeker_candidates,
                seeker_notes,
                all_errors,
                prioritizer_lineage,
                target_selection_metrics,
                seeker_queries_run,
            ) = self._run_seeker(
                request, seeker_state, seeker_notes, all_errors
            )
            self._emit_progress(
                request,
                f"run: seeker stage completed with "
                f"{len(seeker_candidates)} candidate(s)",
            )
            seeker_raw_count = sum(
                int(metric.get("input_candidates", 0))
                for metric in target_selection_metrics
            )

        # Merge eligible seeds after resume filtering.
        seed_candidates = self._seed_candidates(normalized_seed_urls)

        # Attribute seeds for partitioning, review, and resume.
        if seed_candidates and original_targets:
            self._attribute_seeds_to_targets(seed_candidates, original_targets)
            seed_candidates = [
                candidate
                for candidate in seed_candidates
                if not candidate.target_metadata
                or self._target_checkpoint_key(candidate.target_metadata)
                not in completed_checkpoint_keys
            ]
        seed_candidates = [
            candidate
            for candidate in seed_candidates
            if self._passes_final_url_selection_guard(
                final_url=candidate.url, request=request
            )[0]
        ]

        if seeker_candidates and seed_candidates:
            # A seed already discovered by search is downloaded once.
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

        candidates = [
            candidate
            for candidate in candidates
            if self._passes_final_url_selection_guard(
                final_url=candidate.url, request=request
            )[0]
        ]
        all_errors, seeker_notes = self._append_discovery_gap_diagnostics(
            request=request,
            candidates=candidates,
            normalized_seed_urls=normalized_seed_urls,
            errors=all_errors,
            notes=seeker_notes,
            discovered_count=seeker_raw_count,
        )

        candidates_after_seeker = len(candidates)

        routing_notes: list[str] = []
        routing_state: dict[str, object] = {
            "mode": request.topology_mode or "default",
            "applied": False,
        }
        route = {
            "distributed": self._route_distributed_candidates,
            "centralized": self._route_centralized_candidates,
            "hybrid": self._route_hybrid_candidates,
        }.get(request.topology_mode)
        if route is not None:
            candidates, routing_errors, routing_notes, routing_state = route(
                request=request, candidates=candidates
            )
            all_errors = normalize_error_records(
                [*all_errors, *routing_errors]
            )

        # Refilter crawls without flattening search-only selections.
        routing_applied = bool(routing_state.get("applied"))
        if candidates and routing_applied:
            candidates, post_routing_filter_notes = (
                self._apply_post_routing_selection_filters(
                    request=request,
                    candidates=candidates,
                )
            )
            routing_notes.extend(post_routing_filter_notes)

        candidates = [
            candidate
            for candidate in candidates
            if self._passes_final_url_selection_guard(
                final_url=candidate.url, request=request
            )[0]
        ]
        candidates_after_routing = len(candidates)

        documents_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        download_records: list[dict[str, object]] = []
        incomplete_keys, blocked_parts = self._initial_completion_blocks(
            request=request,
            documents_dir=documents_dir,
            original_targets=original_targets,
            completed_entries=completed_checkpoint_keys,
            selection_metrics=target_selection_metrics,
            seeker_failed=bool(seeker_errors),
        )
        download_notes: list[str] = []
        review_costs: dict[str, object] = {}
        new_checkpoint_entries: dict[str, dict[str, object]] = {}
        attempted_parts = set()
        review_only_blocks = set()
        if not request.dry_run and candidates:
            self._emit_progress(request, "run: download stage started")
            candidates_for_download, gating_notes = (
                self._filter_candidates_for_download(candidates)
            )
            download_notes.extend(gating_notes)
            download_errors: list[dict[str, object]] = []
            if candidates_for_download:
                download_cap = request.max_files or len(
                    candidates_for_download
                )
                self._block_capped_candidates(
                    request=request,
                    documents_dir=documents_dir,
                    candidates=candidates_for_download,
                    download_cap=download_cap,
                    incomplete_keys=incomplete_keys,
                    blocked=blocked_parts,
                )
                download_records, download_errors, stage_download_notes = (
                    self._download_candidates(
                        request=request,
                        candidates=candidates_for_download,
                        documents_dir=documents_dir,
                        max_downloads=download_cap,
                    )
                )
                download_notes.extend(stage_download_notes)
                if self._remaining_downloads is not None:
                    self._remaining_downloads = max(
                        0, self._remaining_downloads - len(download_records)
                    )

                download_records, download_notes, review_costs = (
                    self._process_downloads(
                        request,
                        download_records,
                        download_notes,
                    )
                )
                review_cfg = request.document_review
                if review_cfg and download_records:
                    # Recovery retries share the opt-in target budget.
                    classifier_cfg = (
                        getattr(request, "document_classifier", None) or {}
                    )
                    download_records, download_notes = (
                        self._run_post_review_retries(
                            request=request,
                            download_records=download_records,
                            notes=download_notes,
                            documents_dir=documents_dir,
                            review_cfg=review_cfg,
                            classifier_keywords=classifier_cfg.get(
                                "nice_to_have_keywords"
                            ),
                        )
                    )

                hard_blocks = set(blocked_parts)
                for record in download_records:
                    if record.get("status") == "failed":
                        hard_blocks.add(
                            self._completion_partition(
                                request,
                                documents_dir,
                                url=str(record.get("url") or ""),
                                target_metadata=record.get("target_metadata"),
                            )
                        )
                review_only_blocks = {
                    self._completion_partition(
                        request,
                        documents_dir,
                        url=str(record.get("url") or ""),
                        target_metadata=record.get("target_metadata"),
                    )
                    for record in download_records
                    if record.get("review_failed")
                    and self._completion_partition(
                        request,
                        documents_dir,
                        url=str(record.get("url") or ""),
                        target_metadata=record.get("target_metadata"),
                    )
                    not in hard_blocks
                }
                new_checkpoint_entries = self._download_completions(
                    request=request,
                    documents_dir=documents_dir,
                    download_records=download_records,
                    incomplete_keys=incomplete_keys,
                    blocked=blocked_parts,
                    run_id=run_id,
                    started_at=started_at,
                )

            all_errors = normalize_error_records(
                [*all_errors, *download_errors, *self._retry_errors]
            )
            for key, value in self._retry_costs.items():
                review_costs[key] = review_costs.get(key, 0) + value
            seeker_queries_run += self._retry_search_calls
            if download_records:
                if request.reprocess:
                    attempted_parts.update(
                        Path(str(record["relative_path"])).parent
                        for record in download_records
                        if record.get("relative_path")
                    )
                if blocked_parts:
                    download_notes.append(
                        "Incomplete partitions retained run-local; "
                        "consolidated curation and provenance "
                        "were not replaced."
                    )

        empty_parts = self._empty_target_completions(
            request=request,
            documents_dir=documents_dir,
            selection_metrics=target_selection_metrics,
            download_records=download_records,
            incomplete_keys=incomplete_keys,
            entries=new_checkpoint_entries,
            run_id=run_id,
            started_at=started_at,
        )
        if request.reprocess:
            attempted_parts.update(empty_parts)
        for record in download_records:
            if record.get("review_failed"):
                all_errors.append(
                    build_error_record(
                        RuntimeError(
                            str(
                                record.get("review_error")
                                or "Document review failed"
                            )
                        ),
                        stage="discovery.review",
                        document_path=str(
                            record.get("path") or record.get("url")
                        ),
                    )
                )
        all_errors = normalize_error_records(all_errors)
        all_reused = bool(
            original_targets
            and not request.targets
            and completed_checkpoint_keys
            and not candidates
            and not download_records
        )
        result = self._preview_completion_result(
            request=request,
            paths=paths,
            run_id=run_id,
            candidates=candidates,
            download_records=download_records,
            errors=all_errors,
            empty_parts=empty_parts,
            completed_entries=completed_checkpoint_keys,
            all_reused=all_reused,
        )
        self._emit_progress(
            request,
            f"run: discovery finished with status={result.status}",
        )

        manifest = self._build_completion_manifest(
            request=request,
            paths=paths,
            result=result,
            started_at=started_at,
            normalized_seed_urls=normalized_seed_urls,
            candidates=candidates,
            download_records=download_records,
            stage_summaries={
                "seeker": {
                    "provider": seeker_state.get("provider", "seed_only"),
                    "enabled": bool(seeker_state.get("enabled")),
                    "queries_executed": seeker_queries_run,
                    "candidates_discovered": seeker_raw_count,
                    "candidates_after_prioritization": candidates_after_seeker,
                    "link_prioritization_mode": (
                        request.link_prioritization_mode
                    ),
                    "link_top_k": request.link_top_k,
                },
                "routing": {
                    "mode": request.topology_mode or "default",
                    "applied": bool(routing_state.get("applied")),
                    "candidates_in": candidates_after_seeker,
                    "candidates_out": candidates_after_routing,
                },
                "downloads": self._build_download_summary(download_records),
                "document_review": (
                    {"costs": review_costs} if review_costs else {}
                ),
                "acceptance_metrics": self._build_acceptance_metrics(
                    request=request,
                    seeker_state=seeker_state,
                    target_selection_metrics=target_selection_metrics,
                    candidates=candidates,
                ),
            },
            lineage={
                "blocked_partitions": [str(part) for part in blocked_parts],
                "review_only_blocked_partitions": [
                    str(part) for part in review_only_blocks
                ],
                "request_fingerprint": fingerprint,
                "seeker": seeker_state,
                "routing": routing_state,
                "link_prioritization": {
                    "mode": request.link_prioritization_mode,
                    "top_k": request.link_top_k,
                    "candidates": prioritizer_lineage,
                },
            },
            notes=[*seeker_notes, *routing_notes, *download_notes],
        )
        snapshot_path = self._publish_completion(
            request=request,
            paths=paths,
            result=result,
            manifest=manifest,
            completed_entries=completed_checkpoint_keys,
            attempted_parts=attempted_parts,
            blocked=blocked_parts,
            all_reused=all_reused,
        )
        self._finalize_completion(
            request=request,
            paths=paths,
            result=result,
            download_records=download_records,
            entries=new_checkpoint_entries,
            snapshot_path=snapshot_path,
            checkpoint_path=checkpoint_path,
            fingerprint=fingerprint,
            all_reused=all_reused,
        )
        return result
