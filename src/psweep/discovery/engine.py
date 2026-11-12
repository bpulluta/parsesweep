"""Discovery orchestration over acquisition, review, and publication."""

from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import os
import re
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import datetime, UTC
from io import StringIO
from itertools import chain
from pathlib import Path
from collections.abc import Callable
from urllib.parse import unquote, urlparse

from psweep.extraction.transcription import is_audio
from psweep.extraction.document_utils import (
    TEXT_CACHE_DIRNAME,
    text_cache_paths,
)
from psweep.utils.error_taxonomy import (
    build_error_record,
    normalize_error_records,
    summarize_error_records,
)

from .adaptive_search import (
    attempted_urls,
    covered_labels,
    distinct_accepted_count,
    eligible_targets,
    source_unique_records,
)
from .attempt_budget import AttemptBudget, AttemptBudgetExceeded
from .candidate_selector import CandidateSelector
from .concurrency import bounded_results
from psweep.utils.file_io import atomic_write
from psweep.workspace import Workspace
from .connectors import DiggerInput, resolve_digger_connector
from .targets import TARGET_INPUT_FIELDS, identify_target, target_identity
from .review_policy import iter_review_scopes, render_review_scope
from .constants import (
    DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    DOCUMENT_EXTENSIONS,
    DOWNLOADABLE_EXTENSIONS,
    MIME_TO_EXTENSION,
    CONFIGURED_SOURCE,
    ROUTE_REASON_CRAWLED,
    SEEKER_ONLY_SCORE_WEIGHTS,
    USER_AGENT,
)
from .link_prioritizer import (
    LinkPrioritizer,
    domain_score_patterns,
    host_matches_domain_pattern,
)
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
    PolicyCheckResult,
)
from .network import NetworkPolicyError, check_url, send_get
from .transport import ResponseTooLargeError, max_file_bytes
from .document_reviewer import DocumentReviewer
from .eligibility import eligibility_columns, eligibility_held, screen_pdf
from .publication import (
    PublicationPaths,
    artifact_sha256,
    discovery_publication,
    assign_document_names,
    effective_discovery_fingerprint,
    inherit_text_caches,
    resolve_publication_paths,
    materialize_curated,
    owner_key,
    record_is_curated,
    promote_curated,
    merge_download_index,
    refresh_latest_pointer,
    load_checkpoint,
    load_prior_outcomes,
    save_checkpoint_entries,
    publish_manual_curation,
)
from psweep.utils.retry import (
    browser_may_recover,
    compute_backoff,
    is_retryable,
    is_transient_error,
)
from .urls import (
    compile_url_patterns,
    decode_discovery_url,
    matches_allowed_domain,
    matches_url_exclusion,
    normalize_url_text,
    url_extension,
    url_host,
)
import operator

# Download partition and index label for shared (non-target) documents.
SHARED_PARTITION = "_shared"

# Download method of a verified prior outcome, reused without a network request.
REUSED_DOWNLOAD_METHOD = "reused"

# Record fields kept as the final outcome of a child of an incomplete owner.
PRIOR_OUTCOME_FIELDS = (
    "status",
    "final_url",
    "mime_type",
    "reason",
    "error",
    "policy_blocking_code",
    "policy_reason",
)

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


@dataclass(slots=True)
class DiscoveryRequest:
    """Normalized command inputs for a discovery run."""

    domain: str
    dry_run: bool = False
    # Shared configured URLs (``discovery.sources``); always downloaded/kept.
    sources: list[str] = field(default_factory=list)
    # Search query templates; a target row ``query`` overrides them per target.
    queries: list[str] = field(default_factory=list)
    # One bounded, review-qualified recovery stage for weak targets.
    fallback_queries: list[str] | None = None
    max_review_calls: int | None = None
    max_search_calls: int | None = None
    targets: list[dict[str, object]] | None = None
    crawl_provider: str = "http"
    # 0 = never follow links; N = fetch HTML pages up to N-1 links deep.
    max_depth: int = 0
    max_pages: int = 50
    allowed_domains: list[str] | None = None
    include_url_patterns: list[str] | None = None
    include_link_text_patterns: list[str] | None = None
    # Download/crawl artifact cap; None = unbounded. Configured URLs never count.
    max_files: int | None = None
    timeout_seconds: int | None = None
    retry_max_attempts: int = 3
    retry_initial_backoff_seconds: float = 1.0
    retry_max_backoff_seconds: float = 8.0
    max_concurrent_downloads: int = DEFAULT_MAX_CONCURRENT_DOWNLOADS
    min_request_interval_ms: int = 0
    request_headers: dict[str, str] | None = None
    robots_policy_mode: str = DEFAULT_ROBOTS_POLICY_MODE
    tos_policy_mode: str = DEFAULT_TOS_POLICY_MODE
    acknowledged_tos_domains: list[str] | None = None
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
    selection_relevance_exclude_any_terms: list[str] | None = None
    selection_exclude_url_patterns: list[str] | None = None
    selection_exclude_text_patterns: list[str] | None = None
    selection_max_per_host_per_target: int = 0
    selection_require_supported_document: bool = True
    selection_target_require_any: list[str] | None = None
    selection_target_require_all: list[str] | None = None
    selection_target_exclude: list[str] | None = None
    # Post-download document classification (keyword) + LLM review (curation)
    document_classifier: dict[str, object] | None = None
    document_review: dict[str, object] | None = None
    # Output partitioning by target-metadata fields (generic, domain-neutral)
    partition_by: list[str] | None = None
    # Use a real browser to clear bot-manager challenges (Akamai/Cloudflare)
    # for downloads on protected sites.
    browser_downloads: bool = False
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
    seeker_params: dict[str, object] | None = None
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
    fresh: bool = False
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
        self.causes: set[str] = set()
        for partition in partitions:
            self.add(partition)

    def add(self, partition: Path, cause: str | None = None) -> None:
        self.paths.add(partition)
        self.ancestors.update(partition.parents)
        if cause:
            self.causes.add(cause)

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
        self._attempt_budget = AttemptBudget()
        self._reviewer = None
        self._fallback_pending: set[str] = set()
        self._acquisition_pending: dict[str, set[str]] = defaultdict(set)

    @staticmethod
    def _emit_progress(request: DiscoveryRequest, message: str) -> None:
        callback = getattr(request, "progress_callback", None)
        if callback is None:
            return
        with contextlib.suppress(Exception):
            callback(message)

    @staticmethod
    def _target_label(
        target: dict[str, object] | None,
        fallback_index: int,
        fields: list[str] | None = None,
    ) -> str:
        """``label`` column, else the target's identity values."""
        if not isinstance(target, dict):
            return f"target-{fallback_index}"
        label = str(target.get("label") or "").strip()
        if not label:
            label = ", ".join(
                str(value).strip()
                for value in target_identity(target, fields).values()
                if str(value or "").strip()
            )
        return label or str(target.get("query") or f"target-{fallback_index}")

    _ALLOWED_RETENTION_MODES = {"all", "curated", "none"}
    # File-type sets / MIME map are centralized in constants.py.
    _SUPPORTED_EXTENSIONS = DOWNLOADABLE_EXTENSIONS
    _MIME_EXTENSION_MAP = MIME_TO_EXTENSION

    @staticmethod
    def _slugify_domain(domain: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")
        return slug or "default"

    def _build_run_id(self, request: DiscoveryRequest, started_at: datetime) -> str:
        timestamp = started_at.strftime("%Y%m%dT%H%M%S%fZ")
        return f"acq-{self._slugify_domain(request.domain)}-{timestamp}"

    @staticmethod
    def _resolve_output_paths(
        request: DiscoveryRequest,
        run_id: str,
    ) -> tuple[Path, Path]:
        # One self-contained directory per run: manifest.json,
        # download_index.csv, review.csv, documents/, curated/.
        run_dir = Workspace(request.domain).discovered / "runs" / run_id
        return run_dir / "documents", run_dir / "manifest.json"

    @staticmethod
    def _configured_candidates(request: DiscoveryRequest) -> list[DiscoveryCandidate]:
        """Stage shared ``sources`` and target ``urls`` as configured candidates."""
        score = CandidateScore(
            url_signal=0.35,
            anchor_signal=0.0,
            content_signal=0.0,
            trust_signal=0.55,
        )
        candidates: list[DiscoveryCandidate] = []
        seen: set[tuple[str, str | None]] = set()
        targets = ((None, url) for url in request.sources)
        rows = (
            (target, str(url))
            for target in request.targets or ()
            for url in target.get("urls") or ()
        )
        for target, url in chain(targets, rows):
            target_id = identify_target(target, request.partition_by) if target else None
            if (url, target_id) in seen:
                continue
            seen.add((url, target_id))
            candidates.append(
                DiscoveryCandidate(
                    url=url,
                    source=CONFIGURED_SOURCE,
                    score=score,
                    reasons=["Configured URL"],
                    target_metadata=DiscoveryEngine._target_metadata(target),
                    source_scope="target" if target else "shared",
                    source_url=url,
                    target_id=target_id,
                )
            )
        return candidates

    @staticmethod
    def _target_metadata(target: dict[str, object] | None) -> dict[str, object] | None:
        """Target fields exposed downstream (``urls``/``query`` are inputs, not metadata)."""
        if not target:
            return None
        return {k: v for k, v in target.items() if k not in TARGET_INPUT_FIELDS}

    @staticmethod
    def _search_enabled(request: DiscoveryRequest) -> bool:
        """Search runs iff ``queries`` is set or any target row has a ``query``."""
        return bool(request.queries) or any(target.get("query") for target in request.targets or ())

    @staticmethod
    def _append_discovery_gap_diagnostics(
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
        errors: list[dict[str, object]],
        notes: list[str],
        discovered_count: int = 0,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Distinguish failed, empty, and filtered searches."""
        if not DiscoveryEngine._search_enabled(request) or candidates:
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
        else:
            message = "Search returned no candidates. Adjust query or target inputs."
        return errors, [*notes, message]

    @staticmethod
    def _resolve_serpapi_state(
        request: DiscoveryRequest,
    ) -> tuple[dict[str, object], list[dict[str, object]], list[str]]:
        if not DiscoveryEngine._search_enabled(request):
            return {"provider": "serpapi", "enabled": False, "available": False}, [], []

        notes: list[str] = []
        errors: list[dict[str, object]] = []
        if not os.getenv("SERPAPI_API_KEY"):
            errors.append(
                build_error_record(
                    ValueError("Search queries are configured but SERPAPI_API_KEY is not set"),
                    stage="discovery.seeker_init",
                    provider="serpapi",
                )
            )
        else:
            notes.append("Search enabled; SerpApi seeker connected.")

        return (
            {
                "provider": "serpapi",
                "enabled": True,
                "available": True,
            },
            normalize_error_records(errors),
            notes,
        )

    @staticmethod
    def _resolve_seeker_cache_dir(request: DiscoveryRequest) -> str | None:
        """Return the canonical SerpApi cache directory for this request."""
        if not (request.seeker_cache or request.fallback_queries):
            return None
        return str(Workspace(request.domain).serpapi_cache)

    @staticmethod
    def _build_selector(request: DiscoveryRequest) -> CandidateSelector:
        """Construct a CandidateSelector from request selection settings.

        Shared by seeker-stage selection and post-routing re-selection so the
        two never drift apart.
        """
        return CandidateSelector(
            exclude_draft=request.selection_exclude_draft,
            draft_patterns=request.selection_draft_patterns,
            relevance_require_any_terms=request.selection_relevance_require_any_terms or None,
            relevance_exclude_any_terms=request.selection_relevance_exclude_any_terms or None,
            exclude_url_patterns=request.selection_exclude_url_patterns or None,
            exclude_text_patterns=request.selection_exclude_text_patterns or None,
            require_supported_document=bool(request.selection_require_supported_document),
            max_per_host_per_target=max(0, int(request.selection_max_per_host_per_target or 0)),
            target_require_any=request.selection_target_require_any or None,
            target_require_all=request.selection_target_require_all or None,
            target_exclude=request.selection_target_exclude or None,
        )

    @staticmethod
    def _seeker_jobs(inputs, *, query_slot: bool):
        """Keep initial target order; fallback visits every owner by query slot."""
        if not query_slot:
            yield from enumerate(inputs)
            return
        for slot in range(max((len(item.queries) for item in inputs), default=0)):
            for idx, item in enumerate(inputs):
                if slot < len(item.queries):
                    yield idx, dataclass_replace(item, queries=[item.queries[slot]], context=None)

    @staticmethod
    def _seeker_candidate(raw, target, request):
        url = decode_discovery_url(str(raw.get("url") or ""))
        if not url:
            return None
        return DiscoveryCandidate(
            url=url,
            source=str(raw["source"]),
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
            target_metadata=DiscoveryEngine._target_metadata(target),
            source_scope="target" if target else "shared",
            target_id=identify_target(target, request.partition_by) if target else None,
        )

    def _run_seeker(
        self,
        request: DiscoveryRequest,
        seeker_state: dict[str, object],
        current_notes: list[str],
        current_errors: list[dict[str, object]],
        *,
        excluded_urls: dict[str, set[str]] | None = None,
        excluded_queries: dict[str, set[str]] | None = None,
        query_slot: bool = False,
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
        if request.dry_run and request.fallback_queries:
            return (
                [],
                [*current_notes, "Adaptive dry run: search and review requests skipped."],
                current_errors,
                [],
                [],
                0,
            )
        if not seeker_state.get("enabled") or not seeker_state.get("available"):
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
                    float(getattr(request, "seeker_cache_ttl_minutes", 0.0) or 0.0) * 60.0,
                ),
                cache_refresh=request.fresh and not request.dry_run,
                before_request=self._attempt_budget.reserve_search,
            )
            from .connectors.serpapi_seeker import resolve_search_queries

            seeker_targets = request.targets or [None]
            seeker_inputs = []
            for target, original_input in zip(
                seeker_targets, self._build_seeker_inputs(request), strict=True
            ):
                item = original_input
                key = identify_target(target, request.partition_by) if target else ""
                if query_slot or excluded_queries is not None:
                    item = dataclass_replace(
                        item,
                        queries=[
                            query
                            for query in resolve_search_queries(item)
                            if query not in (excluded_queries or {}).get(key, set())
                        ],
                        context=None,
                    )
                seeker_inputs.append(item)
            primary_per_target = max(1, int(request.selection_primary_per_target or 1))
            selector = self._build_selector(request)
            total_inputs = len(seeker_inputs)
            selected_by_target = [[] for _ in seeker_inputs]
            target_selection_metrics = [
                {
                    "target_index": idx,
                    "input_candidates": 0,
                    "scope_rejected": 0,
                    "selected_candidates": 0,
                    "outcome": "no_candidates",
                }
                for idx in range(total_inputs)
            ]
            selection_notes_by_target = [[] for _ in seeker_inputs]
            # Only bounded selections survive each job. URL/rank bookkeeping is
            # transient; raw provider rows never overlap full-corpus candidates.
            seen = [{} for _ in seeker_inputs]
            slots = [0 for _ in seeker_inputs]
            total_discovered = 0
            for idx, seeker_input in self._seeker_jobs(seeker_inputs, query_slot=query_slot):
                if not seeker_input.queries:
                    continue
                target = seeker_targets[idx]
                target_label = self._target_label(target, idx + 1, request.partition_by)
                self._emit_progress(
                    request,
                    f"seeker {idx + 1}/{total_inputs}: searching {target_label} "
                    f"| queries={len(seeker_input.queries)}",
                )
                try:
                    discovered = seeker.discover(seeker_input)
                except SerpApiSearchTimeout as exc:
                    discovered = exc.candidates
                    incomplete_targets.add(idx)
                    target_selection_metrics[idx]["pending_reason"] = "provider"
                    errors.extend(
                        build_error_record(
                            TimeoutError(f"{target_label}: {message}"),
                            stage="discovery.seeker_discover",
                            provider="serpapi",
                        )
                        for message in exc.query_errors
                    )
                    notes.append(
                        f"Partial search for {target_label}: "
                        f"{len(exc.query_errors)} query timeout(s); retained "
                        f"{len(discovered)} candidate(s)."
                    )
                except Exception as exc:  # noqa: BLE001 - provider isolation
                    discovered = []
                    incomplete_targets.add(idx)
                    target_selection_metrics[idx]["pending_reason"] = (
                        "budget" if isinstance(exc, AttemptBudgetExceeded) else "provider"
                    )
                    errors.append(
                        build_error_record(
                            exc, stage="discovery.seeker_discover", provider="serpapi"
                        )
                    )
                    notes.append(f"Search failed for {target_label}; target remains retryable.")
                group = list(selected_by_target[idx])
                metric = target_selection_metrics[idx]
                for rank, raw in enumerate(discovered):
                    candidate = self._seeker_candidate(raw, target, request)
                    if candidate is None:
                        continue
                    rank_key = (rank, slots[idx])
                    previous_rank = seen[idx].get(candidate.url)
                    if previous_rank is not None:
                        if (
                            query_slot
                            and rank_key < previous_rank
                            and candidate.url
                            not in (excluded_urls or {}).get(candidate.target_id or "", set())
                            and self._passes_final_url_selection_guard(
                                final_url=candidate.url, request=request
                            )[0]
                        ):
                            seen[idx][candidate.url] = rank_key
                            group = [item for item in group if item.url != candidate.url]
                            group.append(candidate)
                        continue
                    seen[idx][candidate.url] = rank_key
                    total_discovered += 1
                    if candidate.url in (excluded_urls or {}).get(candidate.target_id or "", set()):
                        continue
                    metric["input_candidates"] += 1
                    if not self._passes_final_url_selection_guard(
                        final_url=candidate.url, request=request
                    )[0]:
                        metric["scope_rejected"] += 1
                        continue
                    group.append(candidate)
                if query_slot:
                    group.sort(key=lambda candidate: seen[idx][candidate.url])
                selected, selection_notes, metrics = selector.select(
                    [group],
                    primary_per_target=primary_per_target,
                    target_contexts=[self._target_metadata(target)] if target else None,
                    include_metrics=True,
                    labels=[target_label],
                )
                selected_by_target[idx] = selected
                selection_notes_by_target[idx] = selection_notes
                metric["selected_candidates"] = len(selected)
                metric["outcome"] = metrics[0]["outcome"]
                if (
                    metric["scope_rejected"]
                    and metric["input_candidates"] == metric["scope_rejected"]
                ):
                    metric["outcome"] = "excluded_source_scope"
                slots[idx] += 1
                if not query_slot or slots[idx] == len(seeker_inputs[idx].queries):
                    seen[idx].clear()
                del discovered, group
                self._emit_progress(
                    request,
                    f"seeker {idx + 1}/{total_inputs}: {target_label} "
                    f"-> {len(selected)} selected candidate(s)",
                )
            for idx in incomplete_targets:
                target_selection_metrics[idx]["search_complete"] = False
        except Exception as exc:  # noqa: BLE001 - provider isolation
            errors.append(
                build_error_record(exc, stage="discovery.seeker_discover", provider="serpapi")
            )
            return (
                [],
                notes,
                normalize_error_records(errors),
                [],
                [],
                seeker.queries_run if seeker else 0,
            )

        candidates = list(chain.from_iterable(selected_by_target))
        notes.extend(
            f"Search '{item['query']}': returned {item['returned']}, "
            f"retained {item['retained']} (limit {item['limit']}, "
            f"cache={'hit' if item['cache_hit'] else 'miss'})."
            for item in getattr(seeker, "query_results", [])
        )
        notes.append(
            f"SerpApi seeker returned {total_discovered} candidate(s) "
            f"across {total_inputs} target(s)."
        )
        notes.extend(chain.from_iterable(selection_notes_by_target))
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
            effective_top_k: int | None = max(1, top_k_setting) if top_k_setting > 0 else None
            if effective_top_k and len(candidates) <= effective_top_k:
                effective_top_k = None  # no additional cap needed
            prioritizer = LinkPrioritizer(
                keywords=request.link_prioritization_keywords,
                domain_authority_overrides=(request.link_prioritization_domain_scores or None),
                top_k=effective_top_k,
                shopping_path_keywords=(request.link_prioritization_shopping_keywords),
            )
            ranked_candidates, prioritizer_lineage = prioritizer.prioritize(candidates)
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
    def _target_template_context(target: dict[str, object]) -> dict[str, str]:
        """Scalar target fields usable as ``{field}`` query-template tokens."""
        return {
            str(key): str(value).strip()
            for key, value in target.items()
            if key not in TARGET_INPUT_FIELDS
            and isinstance(value, (str, int, float, bool))
            and str(value).strip()
        }

    @staticmethod
    def _seeker_input(
        request: DiscoveryRequest,
        target: dict[str, object] | None,
    ):
        """One search input; a target row ``query`` overrides ``queries``."""
        from .connectors.base import SeekerInput

        row_query = str((target or {}).get("query") or "").strip()
        return SeekerInput(
            queries=[row_query] if row_query else list(request.queries),
            max_results=max(1, int(request.seeker_max_results)),
            context=DiscoveryEngine._target_template_context(target or {}),
            params=request.seeker_params,
        )

    @staticmethod
    def _build_seeker_inputs(request: DiscoveryRequest):
        """One seeker input per target (or one shared input without targets)."""
        return [
            DiscoveryEngine._seeker_input(request, target) for target in request.targets or [None]
        ]

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
            "initial_backoff_seconds": max(0.0, float(request.retry_initial_backoff_seconds)),
            "max_backoff_seconds": max(0.0, float(request.retry_max_backoff_seconds)),
        }

    @classmethod
    def _resolve_request_headers(
        cls,
        request: DiscoveryRequest,
        url: str | None = None,
        adapter_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        from requests.structures import CaseInsensitiveDict
        from .code_host_adapters import resolve_adapter

        headers = CaseInsensitiveDict({"User-Agent": USER_AGENT})
        cfg = request.code_host_adapters
        enabled = cfg if isinstance(cfg, bool) else (cfg or {}).get("enabled", False)
        adapter = resolve_adapter(url) if enabled and url else None
        if adapter:
            headers.update(adapter.request_headers())
        headers.update(adapter_headers or {})
        for key, value in (request.request_headers or {}).items():
            normalized_key = str(key or "").strip()
            normalized_value = str(value or "").strip()
            if normalized_key and normalized_value:
                headers[normalized_key] = normalized_value
        return dict(headers)

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
            request_headers=self._resolve_request_headers(request, url),
            robots_policy_mode=request.robots_policy_mode,
            tos_policy_mode=request.tos_policy_mode,
            acknowledged_tos_domains=request.acknowledged_tos_domains,
            timeout_seconds=request.timeout_seconds or 15,
            rate_limiter=self._rate_limiter,
            source_gate=lambda target: matches_allowed_domain(target, request.allowed_domains)
            and not matches_url_exclusion(
                target, compile_url_patterns(request.selection_exclude_url_patterns)
            ),
        )

    def _network_gate(
        self,
        request: DiscoveryRequest,
        ssl_verify: bool,
        *,
        document: bool = False,
        configured: bool = False,
    ) -> Callable[[str], object]:
        """Share policy and source checks; configured URLs get policy checks only."""

        def gate(url: str) -> object:
            if configured:
                return self._evaluate_request_policy(
                    url=url, request=request, ssl_verify=ssl_verify
                )
            if not matches_allowed_domain(url, request.allowed_domains):
                return False
            if matches_url_exclusion(
                url,
                compile_url_patterns(request.selection_exclude_url_patterns),
            ):
                return False
            if (
                document
                and not self._passes_final_url_selection_guard(final_url=url, request=request)[0]
            ):
                return False
            return self._evaluate_request_policy(url=url, request=request, ssl_verify=ssl_verify)

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
                f"{stage_name} policy enforcement skipped "
                f"{count} URL(s) due to {labels.get(code, code)}."
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
                f"{stage_name} policy warnings: {count} URL(s) proceeded "
                f"despite {labels.get(code, code)} because mode={mode}."
            )

        return notes

    def _filter_urls_for_policy(
        self,
        *,
        urls: list[str],
        request: DiscoveryRequest,
        ssl_verify: bool,
        stage_name: str,
    ) -> tuple[list[str], list[str], set[str]]:
        """Return allowed URLs, policy notes, and URLs blocked only by a
        transient policy evaluation failure (retry on a later run).
        """
        allowed_urls: list[str] = []
        retryable_urls: set[str] = set()
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
            elif policy_result.retryable:
                retryable_urls.add(url)
            if policy_result.blocking_code:
                blocked_counts[policy_result.blocking_code] += 1
            for code in policy_result.warning_codes:
                warning_counts[code] += 1

        return (
            allowed_urls,
            self._build_policy_notes(
                stage_name=stage_name,
                blocked_counts=blocked_counts,
                warning_counts=warning_counts,
                request=request,
            ),
            retryable_urls,
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
        configured: bool = False,
    ):
        gate = (
            self._network_gate(request, ssl_verify, document=True, configured=configured)
            if request
            else None
        )
        return send_get(
            url,
            headers=request_headers,
            timeout=(request.timeout_seconds or 60) if request else 60,
            verify=ssl_verify,
            stream=True,
            max_bytes=max_file_bytes(),
            gate=gate,
            response_url_gate=gate,
            limiter=rate_limiter,
            retry=(
                int(retry_policy["max_attempts"]),
                float(retry_policy["initial_backoff_seconds"]),
                float(retry_policy["max_backoff_seconds"]),
            ),
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
        prior: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object], dict[str, object] | None, bool, list[str]]:
        """Fetch one candidate, or reuse its ``prior`` final outcome from an
        earlier run without a network request (see ``_reusable_outcomes``).
        """
        url = candidate.url
        url = decode_discovery_url(url)
        if not isinstance(url, str) or not re.match(r"^https?://", url, flags=re.IGNORECASE):
            return (
                idx,
                {"url": url, "status": "skipped_invalid_url"},
                None,
                False,
                [],
            )
        if prior is not None and prior.get("status") != "downloaded":
            return (
                idx,
                {**prior, "url": url, "download_method": REUSED_DOWNLOAD_METHOD},
                None,
                False,
                [],
            )

        configured = candidate.source == CONFIGURED_SOURCE
        allowed, reason = (
            (True, None)
            if configured
            else self._passes_final_url_selection_guard(final_url=url, request=request)
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

        policy_result = (
            PolicyCheckResult()
            if prior is not None
            else self._evaluate_request_policy(
                url=url,
                request=request,
                ssl_verify=ssl_verify,
            )
        )
        if not policy_result.allowed and not policy_result.retryable:
            return (
                idx,
                {
                    "url": url,
                    "status": self._policy_status_from_blocking_code(policy_result.blocking_code),
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
            if not policy_result.allowed:
                raise NetworkPolicyError(url, "; ".join(policy_result.messages), policy_result)
            if prior is not None:
                final_url, mime_type = str(prior["final_url"]), prior.get("mime_type")
                attempt_count, download_method = 0, REUSED_DOWNLOAD_METHOD
                prior_path = Path(str(prior["path"]))

                def _chunks(_p: Path = prior_path) -> object:
                    with _p.open("rb") as handle:
                        yield from iter(lambda: handle.read(65536), b"")
            elif browser is not None:
                self._browser_policy_gate(request, url, configured=configured)
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
                        request_headers=self._resolve_request_headers(request, url),
                        rate_limiter=rate_limiter,
                        request=request,
                        configured=configured,
                    )
                    final_url = response.url or url
                    mime_type = (response.headers or {}).get("Content-Type")

                    def _chunks(_r: object = response) -> object:
                        return _r.iter_content(chunk_size=65536)
                except Exception as exc:
                    if not self._browser_recovery_enabled(request) or not browser_may_recover(exc):
                        raise
                    self._browser_policy_gate(request, url, configured=configured)

                    browser_session = browser
                    browser_owned = False
                    if browser_session is None:
                        browser_session, _browser_note = self._open_browser_for_download(request)
                        browser_owned = browser_session is not None
                    if browser_session is None:
                        raise

                    try:
                        if rate_limiter is not None:
                            rate_limiter.wait()
                        content, mime_type, final_url = browser_session.download_bytes(url)
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
            extension = self._infer_extension_from_url_or_mime(final_url, mime_type)
            chunks = iter(_chunks())
            normalized_mime = (mime_type or "").split(";", 1)[0].strip().lower()
            if extension is None and normalized_mime in {"", "application/octet-stream"}:
                # Some ordinance APIs return PDF bytes without a filename or MIME.
                prefix = b""
                while len(prefix) < 5:
                    chunk = next(chunks, None)
                    if chunk is None:
                        break
                    prefix += chunk
                if prefix.startswith(b"%PDF-"):
                    extension = ".pdf"
                chunks = chain((prefix,), chunks)
            if extension is None:
                return (
                    idx,
                    {
                        "url": url,
                        "final_url": final_url,
                        "status": "skipped_unsupported_type",
                        "mime_type": mime_type,
                        "policy_warning_codes": list(policy_result.warning_codes),
                    },
                    None,
                    False,
                    list(policy_result.warning_codes),
                )

            # Strict file scope rejects MIME mismatches.
            # Web/script path rules keep their source-boundary meaning.
            if (
                not configured
                and request.include_url_patterns
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
                (True, None)
                if configured
                else self._passes_final_url_selection_guard(
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
                        "policy_warning_codes": list(policy_result.warning_codes),
                    },
                    None,
                    False,
                    list(policy_result.warning_codes),
                )

            # The requested name; publication assigns the final consolidated name.
            relative = self._partition(
                request, candidate.target_id, candidate.target_metadata
            ) / self._safe_filename(final_url, extension)
            partition_dir = documents_dir / relative.parent
            partition_dir.mkdir(parents=True, exist_ok=True)
            filename = relative.name
            target_path = partition_dir / filename
            suffix = 1

            while True:
                try:
                    if prior is not None:
                        self._place_prior(prior_path, target_path)
                        own_target = True
                        bytes_written = target_path.stat().st_size
                        content_hash = str(prior["sha256"])
                        break
                    bytes_written = 0
                    hasher = hashlib.sha256()
                    with target_path.open("xb") as handle:
                        own_target = True
                        for chunk in chunks:
                            if not chunk:
                                continue
                            handle.write(chunk)
                            hasher.update(chunk)
                            bytes_written += len(chunk)
                    content_hash = hasher.hexdigest()
                    break
                except FileExistsError:
                    suffix += 1
                    target_path = partition_dir / f"{Path(filename).stem}-{suffix}{extension}"

            return (
                idx,
                {
                    "url": url,
                    "final_url": final_url,
                    "status": "downloaded",
                    "mime_type": mime_type,
                    "path": target_path.as_posix(),
                    "relative_path": relative.as_posix(),
                    "bytes": bytes_written,
                    "content_hash": content_hash,
                    "attempt_count": attempt_count,
                    "download_method": download_method,
                    "policy_warning_codes": list(policy_result.warning_codes),
                },
                None,
                True,
                list(policy_result.warning_codes),
            )
        except Exception as exc:  # noqa: BLE001 - network isolation: classified into an error record
            if (
                isinstance(exc, NetworkPolicyError)
                and not isinstance(exc, ResponseTooLargeError)
                and not is_transient_error(exc)
            ):
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
                    "retryable": error["retryable"],
                    "policy_warning_codes": list(policy_result.warning_codes),
                },
                error,
                False,
                list(policy_result.warning_codes),
            )
        finally:
            if response is not None and callable(getattr(response, "close", None)):
                response.close()

    @staticmethod
    def _downloads_ssl_verify() -> bool:
        from psweep.security import resolve_tls_verification

        return resolve_tls_verification("DISCOVERY_SSL_VERIFY")

    @staticmethod
    def _infer_extension_from_url_or_mime(url: str, mime_type: str | None) -> str | None:
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
    def _safe_filename(url: str, ext: str) -> str:
        stem = Path(unquote(urlparse(url).path)).stem
        sanitized_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-_")
        return f"{sanitized_stem or 'document'}{ext}"

    @staticmethod
    def _place_prior(source: Path, target: Path) -> None:
        """Hardlink reused bytes, copying across filesystems; raise
        ``FileExistsError`` when ``target`` is taken.
        """
        try:
            os.link(source, target)
        except FileExistsError:
            raise
        except OSError:
            import shutil

            try:
                with source.open("rb") as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
            except FileExistsError:
                raise
            except BaseException:
                target.unlink(missing_ok=True)
                raise

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
            pattern.search(value)
            for pattern in include_patterns
            for value in (final_url, unquote(final_url))
        ):
            return False, "final_url_outside_include_patterns"
        patterns = compile_url_patterns(request.selection_exclude_url_patterns)
        if matches_url_exclusion(final_url, patterns):
            return False, "final_url_matches_excluded_pattern"
        text = cls._normalize_url_text(unquote(decode_discovery_url(final_url)))
        exclude_terms = list(request.selection_relevance_exclude_any_terms or [])
        if any(str(term).lower() in text for term in exclude_terms):
            return False, "final_url_matches_excluded_term"
        return True, None

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")

    @classmethod
    def _partition(
        cls,
        request: DiscoveryRequest,
        target_id: str | None,
        target_metadata: dict[str, object] | None,
    ) -> Path:
        """Relative document partition: ``_shared``, ``by_<fields>/...`` or ``_targets/<id>``."""
        if target_id is None:
            return Path(SHARED_PARTITION)
        if not request.partition_by:
            return Path("_targets", target_id)
        meta = target_metadata or {}
        return Path(
            "by_" + "_".join(request.partition_by),
            *(
                cls._slug(str(meta.get(name) or "")) or f"unknown-{name}"
                for name in request.partition_by
            ),
        )

    @staticmethod
    def _reuse_key(candidate: DiscoveryCandidate) -> tuple[str, str]:
        return (
            owner_key(candidate.target_id, candidate.source_url) or "",
            decode_discovery_url(candidate.url),
        )

    @classmethod
    def _reusable_outcomes(
        cls,
        candidates: list[DiscoveryCandidate],
        prior: dict[str, dict[str, dict[str, object]]],
    ) -> dict[tuple[str, str], dict[str, object]]:
        """Prior final outcomes of ``candidates``; a download only while its
        file still matches the saved SHA-256.
        """
        reusable: dict[tuple[str, str], dict[str, object]] = {}
        for candidate in candidates:
            key = cls._reuse_key(candidate)
            outcome = prior.get(key[0], {}).get(key[1])
            if outcome is None or key in reusable:
                continue
            if outcome.get("status") == "downloaded":
                try:
                    if artifact_sha256(Path(str(outcome["path"]))) != outcome["sha256"]:
                        continue
                except (OSError, ValueError):
                    continue
            reusable[key] = outcome
        return reusable

    @classmethod
    def _stage_downloads(
        cls,
        candidates: list[DiscoveryCandidate],
        max_downloads: int | None,
        reusable: dict[tuple[str, str], dict[str, object]],
    ) -> tuple[list[DiscoveryCandidate], list[DiscoveryCandidate]]:
        """Split candidates into staged and cut by ``max_downloads``, which
        only counts new fetches: configured URLs and reused outcomes are free.
        """
        staged = [c for c in candidates if c.source == CONFIGURED_SOURCE]
        ordinary = [c for c in candidates if c.source != CONFIGURED_SOURCE]
        if max_downloads is None:
            return staged + ordinary, []
        pending: dict[str, deque[DiscoveryCandidate]] = defaultdict(deque)
        for candidate in ordinary:
            key = cls._reuse_key(candidate)
            if key in reusable:
                staged.append(candidate)
            else:
                pending[key[0]].append(candidate)
        owners = deque(pending)
        fetches = 0
        while owners and fetches < max_downloads:
            owner = owners.popleft()
            staged.append(pending[owner].popleft())
            fetches += 1
            if pending[owner]:
                owners.append(owner)
        # Audit/checkpoint cuts keep input order, independently of fair staging.
        cut_ids = {id(candidate) for group in pending.values() for candidate in group}
        return staged, [candidate for candidate in ordinary if id(candidate) in cut_ids]

    def _download_candidates(
        self,
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
        documents_dir: Path,
        max_downloads: int | None = None,
        reusable: dict[tuple[str, str], dict[str, object]] | None = None,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
        """Download candidates; configured URLs and ``reusable`` prior outcomes
        are staged outside ``max_downloads``.
        """
        try:
            import requests  # noqa: F401 - availability probe
        except ImportError as exc:
            error = build_error_record(
                RuntimeError("requests dependency is required for discovery downloads"),
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
        max_concurrent_downloads = max(1, int(request.max_concurrent_downloads))
        rate_limiter = self._rate_limiter or _RequestRateLimiter(request.min_request_interval_ms)
        reusable = reusable or {}
        staged_candidates, cut = self._stage_downloads(candidates, max_downloads, reusable)
        priors = [reusable.get(self._reuse_key(c)) for c in staged_candidates]
        self._emit_progress(
            request,
            f"download: staging {len(staged_candidates)} candidate(s) for fetch",
        )

        download_notes_extra: list[str] = []
        if cut:
            download_notes_extra.append(
                f"The runtime.max_files budget left {len(cut)} candidate(s) undownloaded."
            )
        completed_t = tuple[
            int,
            dict[str, object],
            dict[str, object] | None,
            bool,
            list[str],
        ]
        completed: list[completed_t] = []

        # Browser mode: bot-protected sites (Akamai) block requests even with
        # transplanted cookies (TLS fingerprinting), so files are fetched
        # THROUGH a real browser. A single Chrome session is shared and
        # downloads run serially (the driver is not thread-safe).
        browser = None
        if request.browser_downloads and None in priors:
            browser, note = self._open_browser_for_download(request)
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
                            prior=priors[idx - 1],
                        )
                    )
            else:

                def fetch(item):
                    idx, candidate = item
                    return self._download_single_candidate(
                        idx=idx,
                        candidate=candidate,
                        request=request,
                        documents_dir=documents_dir,
                        ssl_verify=ssl_verify,
                        retry_policy=retry_policy,
                        rate_limiter=rate_limiter,
                        prior=priors[idx - 1],
                    )

                completed.extend(
                    bounded_results(
                        fetch,
                        enumerate(staged_candidates, start=1),
                        workers=max_concurrent_downloads,
                    )
                )
        finally:
            if browser is not None:
                browser.close()

        for (
            _,
            download_record,
            maybe_error,
            was_downloaded,
            policy_warning_codes,
        ) in sorted(completed, key=operator.itemgetter(0)):
            candidate = staged_candidates[_ - 1]
            download_record.update(
                target_label=(
                    self._target_label(candidate.target_metadata, _, request.partition_by)
                    if candidate.target_id
                    else SHARED_PARTITION
                ),
                target_metadata=candidate.target_metadata,
                source=candidate.source,
                source_scope=candidate.source_scope,
                source_url=candidate.source_url,
                target_id=candidate.target_id,
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
                errors.append({**maybe_error, "target_label": download_record["target_label"]})
            if was_downloaded:
                downloaded_count += 1
            blocking_code = download_record.get("policy_blocking_code")
            if isinstance(blocking_code, str) and blocking_code:
                blocked_counts[blocking_code] += 1
            for code in policy_warning_codes:
                warning_counts[code] += 1

        notes = [
            (
                f"Download stage completed: {downloaded_count} "
                f"file(s) saved from {len(candidates)} candidate(s)."
            )
        ]
        reused = [r for r in downloads if r.get("download_method") == REUSED_DOWNLOAD_METHOD]
        if reused:
            files = sum(r.get("status") == "downloaded" for r in reused)
            notes.append(
                f"Resume reused {files} prior download(s) and {len(reused) - files} other "
                "final outcome(s) without a network request; "
                f"{len(downloads) - len(reused)} candidate(s) fetched."
            )
        browser_fallback_count = sum(
            1 for record in downloads if str(record.get("download_method")) == "browser_fallback"
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
        notes.extend(
            (
                (
                    "Download retry policy: "
                    f"attempts={retry_policy['max_attempts']}, "
                    f"initial_backoff={retry_policy['initial_backoff_seconds']}s, "
                    f"max_backoff={retry_policy['max_backoff_seconds']}s."
                ),
                (
                    "Download throughput controls: "
                    f"max_concurrent_downloads={max_concurrent_downloads}, "
                    f"min_request_interval_ms={max(0, int(request.min_request_interval_ms))}."
                ),
            )
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
        configured = [c for c in candidates if c.source == CONFIGURED_SOURCE]
        candidates = [c for c in candidates if c.source != CONFIGURED_SOURCE]
        if not candidates:
            return configured, []

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
            (
                "Post-routing selection eligibility reduced "
                f"{len(candidates)} candidate(s) to {len(filtered)}."
            )
        ]
        notes.extend([f"Post-routing {n}" for n in filter_notes])
        return configured + filtered, notes

    @staticmethod
    def _filter_candidates_for_download(
        candidates: list[DiscoveryCandidate],
    ) -> tuple[list[DiscoveryCandidate], list[str]]:
        """Keep downloads focused on viable discovered candidates.

        Simplification rule:
        - Always honor explicitly configured URLs.
        - For discovered candidates, skip low-confidence rejected items by default.
        """
        if not candidates:
            return [], []

        discovered = [c for c in candidates if c.source != CONFIGURED_SOURCE]
        has_viable_discovered = any(c.score.acceptance_class() != "rejected" for c in discovered)

        filtered: list[DiscoveryCandidate] = []
        rejected_skipped = 0
        for candidate in candidates:
            if candidate.source == CONFIGURED_SOURCE:
                filtered.append(candidate)
                continue

            if has_viable_discovered and candidate.score.acceptance_class() == "rejected":
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
        """Aggregate this run's fetch outcomes and bytes; reused prior
        outcomes made no request and are only counted in ``reused``.
        """
        counts: dict[str, int] = {}
        total_bytes = 0
        reused = 0
        for record in download_records:
            if record.get("download_method") == REUSED_DOWNLOAD_METHOD:
                reused += 1
                continue
            status = str(record.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
            if status == "downloaded":
                total_bytes += int(record.get("bytes") or 0)
        return {
            "total": len(download_records) - reused,
            "downloaded": counts.get("downloaded", 0),
            "skipped": sum(v for k, v in counts.items() if k.startswith("skipped")),
            "failed": counts.get("failed", 0),
            "reused": reused,
            "total_bytes": total_bytes,
            "by_status": counts,
        }

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
        success_rate = targets_with_staged_candidates / target_count if target_count else None
        threshold = 0.80

        return {
            "applicable": True,
            "measurement_mode": "target_matrix" if request.targets else "single_query",
            "target_count": target_count,
            "targets_with_staged_candidates": targets_with_staged_candidates,
            "success_rate": round(success_rate, 4) if success_rate is not None else None,
            "coverage_threshold": threshold,
            "meets_coverage_threshold": bool(
                success_rate is not None and success_rate >= threshold
            ),
            "targets": target_selection_metrics,
        }

    def _build_acceptance_metrics(
        self,
        *,
        request: DiscoveryRequest,
        seeker_state: dict[str, object],
        target_selection_metrics: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "unknown_target_discovery": self._build_unknown_target_metrics(
                request=request,
                seeker_state=seeker_state,
                target_selection_metrics=target_selection_metrics,
            ),
        }

    @classmethod
    def _open_browser_for_download(cls, request: DiscoveryRequest) -> tuple[object | None, str]:
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
            session = BrowserSession(request_headers=cls._resolve_request_headers(request))
            session._start()  # noqa: SLF001 - own the lifecycle here
        except BrowserUnavailableError as exc:
            return None, f"Browser mode unavailable ({exc}); used direct HTTP."
        return session, "Browser mode: downloading via headless Chrome."

    @staticmethod
    def _browser_recovery_enabled(request: DiscoveryRequest) -> bool:
        """Require explicit browser opt-in for automatic recovery."""
        cfg = request.browser_escalation
        return (
            request.browser_downloads
            or cfg is True
            or (isinstance(cfg, dict) and cfg.get("enabled") is True)
        )

    @staticmethod
    def _build_constraints(request: DiscoveryRequest) -> dict[str, object]:
        constraints: dict[str, object] = {
            "crawl_provider": request.crawl_provider,
        }
        if request.partition_by:
            constraints["partition_by"] = list(request.partition_by)
        if request.allowed_domains:
            constraints["allowed_domains"] = request.allowed_domains
        if request.include_url_patterns:
            constraints["include_url_patterns"] = request.include_url_patterns
        if request.include_link_text_patterns:
            constraints["include_link_text_patterns"] = request.include_link_text_patterns

        for field_name in (
            "max_depth",
            "max_pages",
            "max_files",
            "timeout_seconds",
        ):
            value = getattr(request, field_name)
            if value is not None:
                constraints[field_name] = value

        constraints["max_concurrent_downloads"] = max(1, int(request.max_concurrent_downloads))
        constraints["min_request_interval_ms"] = max(0, int(request.min_request_interval_ms))

        constraints["retry_policy"] = {
            "max_attempts": max(1, int(request.retry_max_attempts)),
            "initial_backoff_seconds": max(0.0, float(request.retry_initial_backoff_seconds)),
            "max_backoff_seconds": max(0.0, float(request.retry_max_backoff_seconds)),
        }
        constraints["policy"] = {
            "robots_policy_mode": request.robots_policy_mode,
            "tos_policy_mode": request.tos_policy_mode,
            "acknowledged_tos_domains": list(request.acknowledged_tos_domains or []),
        }
        if request.targets:
            constraints["target_count"] = len(request.targets)
        if request.sources:
            constraints["source_count"] = len(request.sources)
        if request.queries:
            constraints["query_count"] = len(request.queries)
        constraints["link_prioritization"] = {
            "mode": request.link_prioritization_mode,
            "top_k": request.link_top_k,
            "keywords": list(request.link_prioritization_keywords or []),
            "shopping_path_keywords": list(request.link_prioritization_shopping_keywords or []),
        }

        return constraints

    def _crawl_candidates(
        self,
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
        prior: dict[str, dict[str, dict[str, object]]] | None = None,
    ) -> tuple[
        list[DiscoveryCandidate],
        list[dict[str, object]],
        list[str],
        dict[str, object],
        list[DiscoveryCandidate],
    ]:
        """Follow links from HTML candidates when ``crawl.max_depth > 0``.

        Every input candidate is kept; linked documents become new candidates
        attributed to their source page's target and subject to selection.
        Also returns the source candidates whose crawl is unfinished
        (retryable page failure or exhausted budget); ``state`` names the
        cause. Links already settled by ``prior`` outcomes of these owners do
        not count against ``runtime.max_files``.
        """
        state: dict[str, object] = {
            "applied": False,
            "provider": request.crawl_provider,
            "max_depth": request.max_depth,
        }
        if request.max_depth <= 0:
            return candidates, [], [], state, []

        pages = list(
            dict.fromkeys(
                candidate.url
                for candidate in candidates
                if url_extension(candidate.url) not in DOCUMENT_EXTENSIONS
            )
        )
        ssl_verify = self._downloads_ssl_verify()
        pages, notes, unevaluated = self._filter_urls_for_policy(
            urls=pages,
            request=request,
            ssl_verify=ssl_verify,
            stage_name="Crawl",
        )
        if unevaluated:
            state["unfinished_cause"] = "crawl policy check unavailable"
        if not pages:
            notes.append("Crawl skipped: no HTML source pages to follow.")
            return candidates, [], notes, state, [c for c in candidates if c.url in unevaluated]

        self._emit_progress(request, f"crawl: following links from {len(pages)} page(s)")
        owners = {owner_key(c.target_id, c.source_url) for c in candidates}
        known = sum(len((prior or {}).get(key, {})) for key in owners if key)
        file_budget = None if request.max_files is None else request.max_files + known
        try:
            result = resolve_digger_connector(request.crawl_provider).discover(
                DiggerInput(
                    sources=pages,
                    max_depth=request.max_depth,
                    max_pages=request.max_pages,
                    max_files=file_budget,
                    timeout_seconds=request.timeout_seconds or 30,
                    allowed_domains=request.allowed_domains,
                    include_url_patterns=request.include_url_patterns,
                    include_link_text_patterns=request.include_link_text_patterns,
                    extra_params={
                        "ssl_verify": ssl_verify,
                        "request_headers": self._resolve_request_headers(request),
                        "retry": self._resolve_retry_policy(request),
                        "browser_escalation": self._browser_recovery_enabled(request),
                        "request_gate": self._network_gate(request, ssl_verify),
                        "response_url_gate": self._network_gate(request, ssl_verify),
                        "rate_limiter": self._rate_limiter,
                        "browser_policy_strict": request.robots_policy_mode == "enforce"
                        or request.tos_policy_mode == "enforce",
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 - network isolation: recorded as an error record
            error = build_error_record(
                exc,
                stage="discovery.crawl",
                provider=request.crawl_provider,
            )
            notes.append("Crawl failed; continuing with uncrawled candidates.")
            retry_urls = (set(pages) if error["retryable"] else set()) | unevaluated
            unfinished = [c for c in candidates if c.url in retry_urls]
            if error["retryable"]:
                state["unfinished_cause"] = "retryable crawl failure"
            return candidates, normalize_error_records([error]), notes, state, unfinished

        origins: dict[str, list[DiscoveryCandidate]] = {}
        for candidate in candidates:
            origins.setdefault(candidate.url, []).append(candidate)
        seen = {(c.url, c.target_id) for c in candidates}
        crawled: list[DiscoveryCandidate] = []
        for artifact in result.artifacts:
            metadata = artifact.metadata or {}
            page = urlparse(artifact.url)._replace(query="", fragment="")
            for origin in origins.get(str(metadata.get("source_url")), ()):
                if (artifact.url, origin.target_id) in seen:
                    continue
                seen.add((artifact.url, origin.target_id))
                # Query variants of a configured page (its pagination) are
                # that configured page, so they skip selection and review.
                continuation = origin.source == CONFIGURED_SOURCE and (
                    page == urlparse(origin.url)._replace(query="", fragment="")
                )
                crawled.append(
                    DiscoveryCandidate(
                        url=artifact.url,
                        source=CONFIGURED_SOURCE if continuation else artifact.source,
                        score=CandidateScore(url_signal=0.35, trust_signal=0.55),
                        reasons=[ROUTE_REASON_CRAWLED],
                        mime_type=artifact.mime_type,
                        extension=artifact.extension,
                        target_metadata=origin.target_metadata,
                        snippet=str(metadata.get("link_text") or "") or None,
                        source_scope=origin.source_scope,
                        source_url=origin.source_url or origin.url,
                        target_id=origin.target_id,
                    )
                )
        state.update(
            applied=True,
            page_count=len(pages),
            artifact_count=len(result.artifacts),
            incomplete_sources=sorted(result.incomplete_sources),
        )
        notes.append(
            f"Crawl fetched {result.pages_fetched} page(s) from {len(pages)} source page(s) "
            f"and added {len(crawled)} linked candidate(s)."
        )
        if result.incomplete_sources:
            budgets = [
                f"{name}={shown}"
                for name, shown, limit, used in (
                    ("crawl.max_pages", request.max_pages, request.max_pages, result.pages_fetched),
                    ("runtime.max_files", request.max_files, file_budget, len(result.artifacts)),
                )
                if limit is not None and used >= limit
            ]
            cause = (
                f"budget reached ({', '.join(budgets)})" if budgets else "retryable page failure"
            )
            state["unfinished_cause"] = f"crawl {cause}"
            notes.append(
                f"Crawl unfinished for {len(result.incomplete_sources)} source page(s): "
                f"{cause}; they stay incomplete."
            )
        unfinished = [
            c for c in candidates if c.url in result.incomplete_sources or c.url in unevaluated
        ]
        return candidates + crawled, [], notes, state, unfinished

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
            "target_id",
            "source_scope",
            "source_url",
            "status",
            "classification_passed",
            "eligibility_state",
            "eligibility_mode",
            "eligibility_reason",
            "eligibility_identity",
            "eligibility_sha256",
            "curated_selected",
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
            "review_evidence",
            "error",
            "target_metadata",
        ]

        review_ran = any(r.get("review_selected") is not None for r in download_records)
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
                        "target_id": record.get("target_id"),
                        "source_scope": record.get("source_scope"),
                        "source_url": record.get("source_url"),
                        "status": record.get("status"),
                        "classification_passed": record.get("classification_passed"),
                        **eligibility_columns(record),
                        "curated_selected": record_is_curated(record, review_ran=review_ran),
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
                        "review_evidence": (
                            json.dumps(
                                record["review_evidence"], ensure_ascii=False, sort_keys=True
                            )
                            if isinstance(record.get("review_evidence"), dict)
                            else None
                        ),
                        "error": record.get("error"),
                        "target_metadata": (json.dumps(raw_meta) if raw_meta else None),
                    }
                )
        if render_only:
            return handle.getvalue()
        atomic_write(index_path, handle.getvalue())
        return index_path

    @staticmethod
    def _write_review_index(
        *,
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
            "source_scope",
            "doc_kind",
            "relevance",
            "llm_is_primary",
            "llm_selected",
            "eligibility_state",
            "eligibility_mode",
            "eligibility_reason",
            "eligibility_identity",
            "eligibility_sha256",
            "curated_selected",
            "human_decision",
            "human_notes",
            "llm_reason",
            "relative_path",
            "path",
        ]
        rows = [
            r
            for r in download_records
            if r.get("path") and (r.get("status") == "downloaded" or eligibility_held(r))
        ]
        review_ran = any(r.get("review_selected") is not None for r in download_records)
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
                partition = str(rel).rsplit("/", 1)[0] if "/" in str(rel) else ""
                writer.writerow(
                    {
                        "target": record.get("target_label"),
                        "partition": partition,
                        "source_scope": record.get("source_scope"),
                        "doc_kind": record.get("review_doc_kind"),
                        "relevance": record.get("review_relevance"),
                        "llm_is_primary": record.get("review_is_primary"),
                        "llm_selected": record.get(
                            "review_selected",
                            record.get("status") == "downloaded",
                        ),
                        **eligibility_columns(record),
                        "curated_selected": record_is_curated(record, review_ran=review_ran),
                        "human_decision": record.get("human_decision") or "",
                        "human_notes": record.get("human_notes") or "",
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
        return record_is_curated({**record, "review_failed": review_failed}, review_ran=review_ran)

    @classmethod
    def _apply_document_retention(
        cls,
        *,
        mode: str,
        documents_dir: Path,
        curated_dir: Path | None,
        download_records: list[dict[str, object]],
        keep: frozenset[Path] = frozenset(),
    ) -> tuple[int, int]:
        """Delete run-local files the mode does not retain.

        ``keep`` (with each file's text cache) is never deleted.
        """
        import shutil

        resolved_mode = str(mode or "all").strip().lower()
        if resolved_mode not in cls._ALLOWED_RETENTION_MODES:
            resolved_mode = "all"
        if resolved_mode == "all":
            return 0, 0

        files_removed = 0
        bytes_removed = 0
        held = {
            Path(str(r["path"])) for r in download_records if r.get("path") and eligibility_held(r)
        }
        protected = {
            path.resolve()
            for kept in (set(keep) | held)
            for path in (kept, *text_cache_paths(kept))
        }

        def _remove_file(path: Path) -> None:
            nonlocal files_removed, bytes_removed
            try:
                if path.exists() and path.is_file():
                    with contextlib.suppress(OSError):
                        bytes_removed += int(path.stat().st_size)
                    path.unlink()
                    files_removed += 1
            except OSError:
                return

        def _prune_empty_dirs() -> None:
            for dir_path in sorted(
                (p for p in documents_dir.rglob("*") if p.is_dir()),
                reverse=True,
            ):
                with contextlib.suppress(OSError):
                    dir_path.rmdir()

        if resolved_mode == "none":
            if documents_dir.exists():
                for file_path in sorted(
                    (p for p in documents_dir.rglob("*") if p.is_file()),
                    reverse=True,
                ):
                    if file_path.resolve() not in protected:
                        _remove_file(file_path)
                _prune_empty_dirs()
                with contextlib.suppress(OSError):
                    documents_dir.rmdir()
            if curated_dir and curated_dir.exists():
                for file_path in sorted(
                    (p for p in curated_dir.rglob("*") if p.is_file()),
                    reverse=True,
                ):
                    _remove_file(file_path)
                shutil.rmtree(curated_dir, ignore_errors=True)
            return files_removed, bytes_removed

        review_ran = any("review_selected" in record for record in download_records)
        if documents_dir.exists():
            for record in download_records:
                if not record.get("path") or cls._record_is_curated(
                    record,
                    review_ran=review_ran,
                    review_failed=bool(record.get("review_failed")),
                ):
                    continue
                file_path = Path(str(record["path"]))
                if file_path.resolve() in protected:
                    continue
                _remove_file(file_path)
                if file_path.parent.exists():
                    cache_dir = file_path.parent / TEXT_CACHE_DIRNAME
                    if cache_dir.exists():
                        for cache_path in text_cache_paths(file_path):
                            _remove_file(cache_path)
            _prune_empty_dirs()
        return files_removed, bytes_removed

    @staticmethod
    def _partition_is_blocked(partition: Path, blocked: _PartitionIndex) -> bool:
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
    def _refresh_latest_pointer(run_dir: Path, *, domain_dir: Path | None = None) -> None:
        """Atomically update latest, with a text fallback."""

        refresh_latest_pointer(run_dir, domain_dir=domain_dir)

    # ------------------------------------------------------------------
    # Checkpointing helpers
    # ------------------------------------------------------------------

    @classmethod
    def _record_completion_key(cls, record: dict[str, object]) -> str | None:
        return owner_key(record.get("target_id"), record.get("source_url"))

    @staticmethod
    def _input_digest(request: DiscoveryRequest, target: dict[str, object]) -> str:
        """Digest of what a target executes: its URLs, rendered queries and
        rendered selection terms. Passthrough fields leave it unchanged.
        """
        from .connectors.serpapi_seeker import resolve_search_queries

        context = CandidateSelector._normalize_template_context(  # noqa: SLF001 - package-internal helper
            DiscoveryEngine._target_metadata(target) or {}
        )
        review = request.document_review or {}
        description, coverage = render_review_scope(
            str(review.get("document_description") or ""), review.get("coverage"), target
        )
        payload = {
            "urls": target.get("urls") or [],
            "queries": resolve_search_queries(DiscoveryEngine._seeker_input(request, target)),
            "fallback_queries": resolve_search_queries(
                DiscoveryEngine._seeker_input(
                    dataclass_replace(request, queries=request.fallback_queries or []),
                    {key: value for key, value in target.items() if key != "query"},
                )
            )
            if request.fallback_queries
            else [],
            "review_context": {
                field: target.get(field)
                for field in (request.document_review or {}).get("context_fields") or []
            },
            "review_description": description,
            "review_coverage": coverage,
            "selection": [
                CandidateSelector._resolve_templates(templates or [], context)  # noqa: SLF001 - package-internal helper
                for templates in (
                    request.selection_target_require_any,
                    request.selection_target_require_all,
                    request.selection_target_exclude,
                )
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()[:16]

    @classmethod
    def _input_digests(cls, request: DiscoveryRequest) -> dict[str, str]:
        """Completion key → input digest for every target (sources have none)."""
        return {
            identify_target(target, request.partition_by): cls._input_digest(request, target)
            for target in request.targets or ()
        }

    @classmethod
    def _skip_completed_inputs(
        cls,
        request: DiscoveryRequest,
        completed: dict[str, dict[str, object]],
    ) -> tuple[DiscoveryRequest, list[str]]:
        """Drop completed targets and shared sources from a resumed run.

        A target is skipped only when its id and input digest both match.
        """
        digests = cls._input_digests(request)
        inputs = {
            "targets": (
                request.targets or [],
                lambda target: identify_target(target, request.partition_by),
            ),
            "sources": (request.sources, lambda url: owner_key(None, url)),
        }
        remaining: dict[str, list] = {}
        notes: list[str] = []
        for name, (items, key) in inputs.items():
            remaining[name] = [
                item
                for item in items
                if key(item) not in completed
                or completed[key(item)].get("input_digest") != digests.get(key(item))
            ]
            skipped = len(items) - len(remaining[name])
            if skipped:
                notes.append(
                    f"Checkpoint: skipped {skipped} already-completed {name[:-1]}(s) "
                    f"(of {len(items)} total). Use --fresh to include them."
                )
        if not notes:
            return request, notes
        return (
            dataclass_replace(
                request,
                targets=None if request.targets is None else remaining["targets"],
                sources=remaining["sources"],
            ),
            notes,
        )

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
        *,
        draft_patterns: list[str] | None = None,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Screen PDFs and classify relevance without conflating the decisions.

        Keyword filtering preserves configured-URL exemptions. Explicit PDF
        screening applies to every downloaded PDF and holds uncertain originals
        for human review rather than changing their acquisition status.
        """
        from .validators import ContentSampler

        required = list(classifier_cfg.get("required_keywords") or [])
        nice_to_have = list(classifier_cfg.get("nice_to_have_keywords") or [])
        min_matches = int(classifier_cfg.get("min_required_matches", 1))
        action = str(classifier_cfg.get("action", "warn"))
        screening = str(classifier_cfg.get("pdf_screening", "off"))
        keyword_configured = any(key != "pdf_screening" for key in classifier_cfg)
        passed = failed = errors = 0
        for record in downloads:
            if record.get("status") != "downloaded":
                continue
            file_path = record.get("path")
            if not file_path:
                continue
            screen_pdf(record, screening, draft_patterns or [])
            if not keyword_configured:
                continue
            try:
                result = ContentSampler.validate_content(
                    str(file_path), required, nice_to_have, min_matches
                )
                record["classification_passed"] = result.success
                record["classification_score"] = result.confidence_score
                if not result.success:
                    failed += 1
                    if action == "filter" and record.get("source") != CONFIGURED_SOURCE:
                        record["status"] = "rejected_classifier"
                else:
                    passed += 1
            except Exception as exc:  # noqa: BLE001 - classifier isolation: error kept on the record
                record["classification_error"] = str(exc)
                errors += 1
        notes.append(
            f"Post-download classifier: {passed} passed, {failed} failed"
            + (f", {errors} error(s)" if errors else "")
            + f" (action={action})."
        )
        if screening != "off":
            counts = Counter(
                record["document_eligibility"]["state"]
                for record in downloads
                if record.get("document_eligibility")
            )
            notes.append(
                f"PDF screening ({screening}): {dict(counts)}; "
                f"{sum(eligibility_held(record) for record in downloads)} held for review."
            )
        return downloads, notes

    def _code_host_get(
        self, request: DiscoveryRequest, ssl_verify: bool
    ) -> Callable[[str, dict], tuple[int, str]]:
        """Build an adapter getter returning (status, text).

        Adapter API endpoints use policy checks, not document selection.
        Headers and TLS verification remain shared with acquisition.
        """

        def gate(endpoint: str) -> object:
            return self._evaluate_request_policy(
                url=endpoint, request=request, ssl_verify=ssl_verify
            )

        def _get(url: str, headers: dict) -> tuple[int, str]:
            merged = self._resolve_request_headers(request, url, headers)
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
            escalation_cfg.get("min_shell_chars", DEFAULT_BROWSER_ESCALATION_MIN_SHELL_CHARS)
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
            except Exception:  # noqa: BLE001, S112 - unreadable file, skip
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
                "<!doctype html><html><body><pre>" + html.escape(content) + "</pre></body></html>"
            )
            try:
                self._replace_recovered_content(path, wrapped, record)
            except OSError:
                continue
            # Mark so downstream dedup re-hashes (bytes changed on disk) and so
            # the review index shows how the content was recovered.
            record["code_host_adapter"] = adapter.name
            from psweep.extraction.document_utils import write_text_cache

            # The text cache is best-effort.
            with contextlib.suppress(Exception):
                write_text_cache(path, content, method="code_host_api")
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
            escalation_cfg.get("settle_seconds", DEFAULT_BROWSER_ESCALATION_SETTLE_SECONDS)
        )
        min_shell_chars = int(
            escalation_cfg.get("min_shell_chars", DEFAULT_BROWSER_ESCALATION_MIN_SHELL_CHARS)
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
            except Exception:  # noqa: BLE001, S112 - unreadable file is not an escalation candidate
                continue

        if not candidates_for_escalation:
            return downloads, notes

        # Attempt browser rendering for shell files
        browser = None
        escalated = 0
        try:
            browser, browser_note = self._open_browser_for_download(request)
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
                        record.update(status="skipped_selection_filter", reason=reason)
                        continue
                    self._browser_policy_gate(request, url)
                    rendered_html = browser.fetch_html(url, settle_seconds=settle_seconds)
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
                    if not rendered_html or len(rendered_html.strip()) < MIN_RENDERED_HTML_CHARS:
                        continue

                    # Validate content before replacing known bytes.
                    text = ContentSampler.extract_text_from_string(rendered_html)
                    if len(text.strip()) >= min_rendered_chars:
                        self._replace_recovered_content(file_path, rendered_html, record)
                        escalated += 1
                        record["browser_escalated"] = True

                        # Cache extracted text as .text/ file (same pattern as
                        # OCR'd PDFs). Users can inspect the .txt instead of
                        # opening raw HTML, and the extraction pipeline reuses
                        # it without re-parsing.
                        from psweep.extraction.document_utils import write_text_cache

                        write_text_cache(file_path, text, method="browser_render")
                    else:
                        # Page rendered but insufficient content (likely a TOC).
                        # Try following keyword-matching links 1 level deeper.
                        classifier_cfg = getattr(request, "document_classifier", None) or {}
                        keywords = classifier_cfg.get("nice_to_have_keywords") or []
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
                                    self._browser_policy_gate(request, deeper_url)
                                    deeper_html = browser.fetch_html(
                                        deeper_url,
                                        settle_seconds=settle_seconds,
                                    )
                                    deeper_final_url = browser.current_url
                                    if (
                                        deeper_html
                                        and deeper_final_url
                                        and (
                                            self._passes_final_url_selection_guard(
                                                final_url=deeper_final_url,
                                                request=request,
                                            )[0]
                                        )
                                    ):
                                        deeper_text = ContentSampler.extract_text_from_string(
                                            deeper_html
                                        )
                                        if len(deeper_text.strip()) >= min_rendered_chars:
                                            # Save a new validated file.
                                            new_record = dict(record)
                                            new_record["url"] = deeper_url
                                            requested = Path(str(record.get("relative_path") or ""))
                                            relative = requested.with_stem(f"{requested.stem}-deep")
                                            deeper_path = file_path.with_stem(
                                                f"{file_path.stem}-deep"
                                            )
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
                                            new_record["path"] = str(deeper_path)
                                            new_record["relative_path"] = relative.as_posix()
                                            new_record["final_url"] = deeper_final_url
                                            new_record["browser_escalated"] = True
                                            new_record["status"] = "downloaded"
                                            downloads.append(new_record)
                                            escalated += 1
                                except Exception:  # noqa: BLE001, S110 - deeper browser link is best-effort
                                    pass
                        record["browser_escalated"] = False
                except Exception:  # noqa: BLE001 - browser isolation: escalation is best-effort
                    record["browser_escalated"] = False
                    continue
        finally:
            if browser is not None:
                # Browser close is best-effort cleanup.
                with contextlib.suppress(Exception):
                    browser.close()

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
    def _group_records_by_target(
        download_records: list[dict[str, object]],
    ) -> dict[str, list[dict[str, object]]]:
        """Group target-scoped download records by ``target_id`` (shared records are skipped)."""
        by_target: dict[str, list[dict[str, object]]] = {}
        for rec in download_records:
            if rec.get("target_id"):
                by_target.setdefault(str(rec["target_id"]), []).append(rec)
        return by_target

    def _download_and_review_retry_candidates(
        self,
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
        documents_dir: Path,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Download a retry candidate batch and process it like the main batch.

        This is the shared download core for both post-review retry stages.
        Downloads count against the remaining ``max_files`` budget, then go
        through ``_process_downloads`` (review included when the request
        configures it). Returns ``(retry_downloads, review_notes)``; callers decide whether
        to surface ``review_notes`` (code-hosting keeps them, JS-shell drops
        them, as before).
        """
        cap = getattr(self, "_remaining_downloads", None)
        retry_downloads, errors, notes = self._download_candidates(
            request=request,
            candidates=candidates,
            documents_dir=documents_dir,
            max_downloads=(min(len(candidates), cap) if cap is not None else len(candidates)),
        )
        if cap is not None:
            self._remaining_downloads = max(0, cap - len(retry_downloads))
        self._retry_errors.extend(errors)
        retry_downloads, notes, costs = self._process_downloads(request, retry_downloads, notes)
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
        classifier_keywords: list[str] | None,
    ) -> tuple[list[dict[str, object]], list[str]]:
        """Run the post-review retry stages.

        The code-hosting SerpApi retry runs first (only when
        ``link_prioritization.domain_scores`` names code hosts), then the
        JS-shell browser-crawl retry.
        """
        if self._code_host_domains(request) and not request.fallback_queries:
            download_records, notes = self._retry_failed_targets_on_code_hosting(
                request=request,
                download_records=download_records,
                notes=notes,
                documents_dir=documents_dir,
                classifier_keywords=classifier_keywords,
            )

        download_records, notes = self._retry_js_shells_with_digger(
            request=request,
            download_records=download_records,
            notes=notes,
            documents_dir=documents_dir,
        )
        return download_records, notes

    @staticmethod
    def _code_host_domains(request: DiscoveryRequest) -> tuple[str, ...]:
        """Return the code hosts that scope the post-review retries.

        Only ``domain_scores`` host entries with a positive score count;
        leading-dot suffix patterns (``.gov``) only adjust ranking.
        """
        return tuple(
            dict.fromkeys(
                pattern
                for pattern, score in domain_score_patterns(
                    request.link_prioritization_domain_scores
                )
                if score > 0 and not pattern.startswith(".")
            )
        )

    @staticmethod
    def _matching_code_host(url: str, code_hosts: tuple[str, ...]) -> str | None:
        """Return the code host serving ``url`` (exact host or subdomain)."""
        try:
            host = urlparse(url).hostname or ""
        except ValueError:
            return None
        return next((d for d in code_hosts if host_matches_domain_pattern(host, d)), None)

    def _retry_js_shells_with_digger(
        self,
        *,
        request: DiscoveryRequest,
        download_records: list[dict[str, object]],
        notes: list[str],
        documents_dir: Path,
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

        if not self._browser_recovery_enabled(request):
            return download_records, notes
        classifier_cfg = getattr(request, "document_classifier", None) or {}
        keywords = classifier_cfg.get("nice_to_have_keywords") or []
        if not keywords:
            return download_records, notes

        code_hosting_domains = self._code_host_domains(request)
        if not code_hosting_domains:
            return download_records, notes

        # Find targets with 0 curated AND JS-shell downloads from code-hosting domains
        by_target = self._group_records_by_target(download_records)

        shell_urls: list[tuple[str, str, dict[str, object]]] = []
        for target_key, records in by_target.items():
            if any(r.get("review_selected") for r in records):
                continue  # target already has curated docs

            for rec in records:
                if not rec.get("browser_escalated") and rec.get("status") in {
                    "downloaded",
                    "skipped_unreadable",
                }:
                    # Check if this is a JS shell from a code-hosting domain
                    url = str(rec.get("url") or "")
                    if self._matching_code_host(url, code_hosting_domains):
                        file_path = rec.get("path")
                        if file_path and _Path(str(file_path)).suffix.lower() in {".html", ".htm"}:
                            shell_urls.append((target_key, url, rec))
                            break

        if not shell_urls:
            return download_records, notes

        # Use the HTTP digger with browser to crawl from each shell URL. A
        # single connector instance is reused across every shell target so the
        # browser session (when the digger escalates) is shared, not rebuilt.
        from .connectors.base import DiggerInput
        from .connectors.digger import HttpDiggerConnector

        digger = HttpDiggerConnector()
        crawled = 0
        for _target_key, url, shell in shell_urls:
            di = DiggerInput(
                sources=[url],
                max_depth=JS_SHELL_RETRY_MAX_DEPTH,
                max_pages=JS_SHELL_RETRY_MAX_PAGES,
                max_files=JS_SHELL_RETRY_MAX_FILES,
                timeout_seconds=JS_SHELL_RETRY_TIMEOUT_SECONDS,
                include_link_text_patterns=keywords,
                extra_params={
                    "ssl_verify": self._downloads_ssl_verify(),
                    "browser_escalation": self._browser_recovery_enabled(request),
                    "request_gate": self._network_gate(request, self._downloads_ssl_verify()),
                    "response_url_gate": self._network_gate(request, self._downloads_ssl_verify()),
                    "rate_limiter": self._rate_limiter,
                    "browser_policy_strict": (
                        request.robots_policy_mode == "enforce"
                        or request.tos_policy_mode == "enforce"
                    ),
                },
            )
            try:
                artifacts = digger.discover(di).artifacts
            except (OSError, RuntimeError, ValueError) as exc:
                # Isolate network, browser, and crawl-input failures.
                self._retry_errors.append(
                    build_error_record(exc, stage="discovery.retry_crawl", provider="http")
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
                        url_signal=JS_SHELL_RETRY_URL_SIGNAL,
                        anchor_signal=0.0,
                        content_signal=0.0,
                        trust_signal=JS_SHELL_RETRY_TRUST_SIGNAL,
                    ),
                    target_metadata=shell.get("target_metadata"),
                    source_scope=shell.get("source_scope"),
                    source_url=shell.get("source_url") or url,
                    target_id=shell.get("target_id"),
                )
                for a in artifacts
            ]

            # JS-shell path discards review notes (matches prior behavior).
            retry_downloads, retry_notes = self._download_and_review_retry_candidates(
                request=request,
                candidates=artifact_candidates,
                documents_dir=documents_dir,
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
    def _find_keyword_link_in_rendered(html: str, base_url: str, keywords: list[str]) -> str | None:
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
        except Exception:  # noqa: BLE001 - malformed HTML means no link found
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
        code_hosting_domains = self._code_host_domains(request)
        if not code_hosting_domains:
            return download_records, notes

        keywords = classifier_keywords or []
        if not keywords:
            return download_records, notes

        # Group records by target, find targets with 0 curated
        by_target = self._group_records_by_target(download_records)

        failed_targets: list[tuple[str, str, dict[str, object]]] = []
        for target_key, records in by_target.items():
            has_curated = any(r.get("review_selected") for r in records)
            if has_curated:
                continue

            # Find code-hosting URLs for this target
            for rec in records:
                url = str(rec.get("url") or "")
                if self._matching_code_host(url, code_hosting_domains):
                    parsed = urlparse(url)
                    host = parsed.hostname or ""
                    # Extract domain + path prefix for site-scoped retry
                    path_parts = parsed.path.strip("/").split("/")
                    # Keep first 2-3 path segments as the jurisdiction scope
                    scope = (
                        "/".join(path_parts[:3])
                        if len(path_parts) >= 3
                        else "/".join(path_parts[:2])
                    )
                    site_prefix = f"{host}/{scope}" if scope else host
                    failed_targets.append((target_key, site_prefix, rec))
                    break

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
            min_request_interval_seconds=max(0.0, float(request.min_request_interval_ms) / 1000.0),
            cache_dir=DiscoveryEngine._resolve_seeker_cache_dir(request),
            timeout_seconds=request.timeout_seconds or 45,
            cache_ttl_seconds=max(
                0.0,
                float(getattr(request, "seeker_cache_ttl_minutes", 0.0) or 0.0),
            )
            * 60.0,
            cache_refresh=request.fresh and not request.dry_run,
            before_request=self._attempt_budget.reserve_search,
        )

        retry_candidates = []
        for target_key, site_prefix, origin in failed_targets:
            query = f"site:{site_prefix} {keyword_or}"
            try:
                before = seeker.queries_run
                results = seeker.discover(SeekerInput(queries=[query], max_results=5))
            except Exception as exc:  # noqa: BLE001 - provider isolation: recorded as a retry error
                if isinstance(exc, AttemptBudgetExceeded):
                    origin.update(
                        review_failed=True,
                        review_retryable=True,
                        review_error="Search attempt budget exhausted during code-host recovery",
                    )
                self._retry_errors.append(
                    build_error_record(
                        exc,
                        stage="discovery.retry_search",
                        provider="serpapi",
                    )
                )
                notes.append(f"Code-hosting retry: query failed for '{target_key}'.")
                continue
            finally:
                self._retry_search_calls += seeker.queries_run - before

            if not results:
                continue

            for result in results[:2]:
                url = result.get("link") or result.get("url")
                if url:
                    retry_candidates.append((url, origin))

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
                url=url,
                source="code_hosting_retry",
                score=CandidateScore(
                    url_signal=0.6,
                    anchor_signal=0.0,
                    content_signal=0.0,
                    trust_signal=0.4,
                ),
                target_metadata=origin.get("target_metadata"),
                source_scope=origin.get("source_scope"),
                target_id=origin.get("target_id"),
            )
            for url, origin in retry_candidates
        ]

        # Download + review the retry batch via the shared retry core. The
        # code-hosting path keeps the review notes (unlike the JS-shell path).
        retry_downloads, retry_review_notes = self._download_and_review_retry_candidates(
            request=request,
            candidates=candidates_for_download,
            documents_dir=documents_dir,
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
    def _replace_recovered_content(path: Path, text: str, record: dict[str, object]) -> None:
        """Publish recovery bytes and refresh their identity."""
        raw = text.encode("utf-8")
        atomic_write(path, raw)
        record.update(
            bytes=len(raw),
            content_hash=hashlib.sha256(raw).hexdigest(),
            mime_type="text/html",
        )

    def _browser_policy_gate(
        self,
        request: DiscoveryRequest,
        url: str,
        *,
        configured: bool = False,
    ) -> None:
        """Gate navigation, not uncontrolled browser subresources."""
        if request.robots_policy_mode == "enforce" or request.tos_policy_mode == "enforce":
            msg = (
                "Strict policy enforcement cannot cover browser subresources; use HTTP acquisition"
            )
            raise RuntimeError(msg)
        check_url(
            url,
            self._network_gate(request, self._downloads_ssl_verify(), configured=configured),
        )
        if self._rate_limiter:
            self._rate_limiter.wait()

    def _process_downloads(
        self,
        request: DiscoveryRequest,
        records: list[dict[str, object]],
        notes: list[str],
        *,
        review: bool = True,
    ) -> tuple[list[dict[str, object]], list[str], dict[str, object]]:
        """Apply recovery, classification, and review to each batch."""
        records, notes = self._resolve_code_host_shells(records, notes, request)
        records, notes = self._escalate_js_shells_to_browser(records, notes, request)
        classifier = request.document_classifier or {}
        if classifier:
            records, notes = self._run_post_download_classifier(
                records,
                classifier,
                notes,
                draft_patterns=(
                    request.selection_draft_patterns if request.selection_exclude_draft else None
                ),
            )
        costs: dict[str, object] = {}
        if request.document_review and review:
            if request.fallback_queries:
                records_for_review = source_unique_records(records)
            else:
                records_for_review = records
            self._seed_recording_transcripts(
                Workspace(request.domain).discovered / "curated", records
            )
            _reviewed, notes, costs = self._run_document_review(
                records_for_review,
                request.document_review,
                notes,
                cache_dir=Workspace(request.domain).review_cache,
                models=request.models,
                classifier_keywords=classifier.get("nice_to_have_keywords"),
                identity_fields=request.partition_by,
                attempt_budget=self._attempt_budget,
                max_per_host_per_target=(
                    request.selection_max_per_host_per_target if request.fallback_queries else 0
                ),
            )
        return records, notes, costs

    def _acquire_batch(
        self,
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
        documents_dir: Path,
        prior: dict[str, dict[str, dict[str, object]]],
        incomplete_keys: set[str],
        blocked: _PartitionIndex,
        notes: list[str],
        review: bool = True,
    ):
        """Acquire a batch with shared reuse, owner fairness, budgets and processing."""
        reusable = self._reusable_outcomes(candidates, prior)
        _, cut = self._stage_downloads(candidates, self._remaining_downloads, reusable)
        self._block_candidates(
            request=request,
            candidates=cut,
            incomplete_keys=incomplete_keys,
            blocked=blocked,
            cause=f"budget runtime.max_files={request.max_files}",
        )
        records, errors, download_notes = self._download_candidates(
            request=request,
            candidates=candidates,
            documents_dir=documents_dir,
            max_downloads=self._remaining_downloads,
            reusable=reusable,
        )
        notes.extend(download_notes)
        if self._remaining_downloads is not None:
            self._remaining_downloads = max(
                0,
                self._remaining_downloads
                - sum(
                    record.get("download_method") != REUSED_DOWNLOAD_METHOD
                    and record.get("source") != CONFIGURED_SOURCE
                    for record in records
                ),
            )
        records, notes, costs = self._process_downloads(request, records, notes, review=review)
        return records, errors, notes, costs

    def _run_adaptive_search(
        self,
        *,
        request: DiscoveryRequest,
        records: list[dict[str, object]],
        candidates: list[DiscoveryCandidate],
        documents_dir: Path,
        prior: dict[str, dict[str, dict[str, object]]],
        incomplete_keys: set[str],
        blocked: _PartitionIndex,
    ):
        """Run one extra acquisition stage, then select over all source evidence."""
        from .connectors.serpapi_seeker import resolve_search_queries

        summary = {
            "enabled": bool(request.fallback_queries),
            "eligible_targets": 0,
            "pending_targets": 0,
            "completed_targets": 0,
            "low_coverage_targets": 0,
            "queries_executed": 0,
            "candidates": 0,
            "downloads": 0,
            "accepted": 0,
        }
        if request.dry_run or not request.fallback_queries:
            return [], [], [], {}, [], summary
        eligible = eligible_targets(
            request.targets or [],
            records,
            keep_top=int(request.document_review.get("keep_top", 1)),
            partition_by=request.partition_by,
            incomplete_keys=incomplete_keys,
            coverage=request.document_review.get("coverage"),
        )
        # A successful prior download may fill coverage while an interrupted
        # fallback query is still pending; finish that stage rather than forget it.
        grouped = self._group_records_by_target(records)
        eligible_keys = {identify_target(t, request.partition_by) for t in eligible}
        eligible.extend(
            target
            for target in request.targets or []
            if (key := identify_target(target, request.partition_by)) in self._fallback_pending
            and key not in eligible_keys
            and key not in incomplete_keys
            and not any(is_retryable(record) for record in grouped.get(key, []))
        )
        summary["eligible_targets"] = len(eligible)
        if not eligible:
            return [], [], [], {}, [], summary
        keys = {identify_target(target, request.partition_by) for target in eligible}
        self._fallback_pending.update(keys)
        fallback_request = dataclass_replace(
            request,
            queries=list(request.fallback_queries),
            sources=[],
            targets=[{k: v for k, v in target.items() if k != "query"} for target in eligible],
            max_files=self._remaining_downloads,
        )
        exclusions = attempted_urls(records)
        for candidate in candidates:
            exclusions.setdefault(candidate.target_id or "", set()).add(candidate.url)
        initial_queries = {
            identify_target(target, request.partition_by): set(
                resolve_search_queries(self._seeker_input(request, target))
            )
            for target in eligible
        }
        found, notes, errors, _lineage, metrics, calls = self._run_seeker(
            fallback_request,
            {"provider": "serpapi", "enabled": True, "available": True},
            [],
            [],
            excluded_urls=exclusions,
            excluded_queries=initial_queries,
            query_slot=True,
        )
        summary["queries_executed"] = calls
        failed, search_blocks = self._initial_completion_blocks(
            request=fallback_request,
            selection_metrics=metrics,
            seeker_failed=bool(errors and not metrics),
        )
        incomplete_keys.update(failed)
        for partition in search_blocks:
            blocked.add(partition, "fallback search unfinished")
        found, crawl_errors, crawl_notes, state, unfinished = self._crawl_candidates(
            request=fallback_request,
            candidates=found,
            prior=prior,
        )
        errors.extend(crawl_errors)
        notes.extend(crawl_notes)
        found = [
            candidate
            for candidate in found
            if candidate.url not in exclusions.get(candidate.target_id or "", set())
        ]
        if state["applied"]:
            found, filtered_notes = self._apply_post_routing_selection_filters(
                request=fallback_request,
                candidates=found,
            )
            notes.extend(filtered_notes)
        found = [
            candidate
            for candidate in found
            if candidate.url not in exclusions.get(candidate.target_id or "", set())
        ]
        self._block_candidates(
            request=request,
            candidates=unfinished,
            incomplete_keys=incomplete_keys,
            blocked=blocked,
            cause="fallback crawl unfinished",
        )
        summary["candidates"] = len(found)
        batch, gating_notes = self._filter_candidates_for_download(found)
        notes.extend(gating_notes)
        downloaded, download_errors, notes, costs = self._acquire_batch(
            request=request,
            candidates=batch,
            documents_dir=documents_dir,
            prior=prior,
            incomplete_keys=incomplete_keys,
            blocked=blocked,
            notes=notes,
            review=False,
        )
        errors.extend(download_errors)
        records.extend(downloaded)
        if downloaded:
            unique = source_unique_records(
                [record for record in records if record.get("target_id") in keys]
            )
            _reviewed, notes, merged_costs = self._run_document_review(
                unique,
                request.document_review,
                notes,
                cache_dir=Workspace(request.domain).review_cache,
                models=request.models,
                classifier_keywords=(request.document_classifier or {}).get(
                    "nice_to_have_keywords"
                ),
                identity_fields=request.partition_by,
                attempt_budget=self._attempt_budget,
                max_per_host_per_target=request.selection_max_per_host_per_target,
            )
            for key, value in merged_costs.items():
                costs[key] = costs.get(key, 0) + value
        summary["downloads"] = len(downloaded)
        summary["accepted"] = sum(
            distinct_accepted_count(group)
            for key, group in self._group_records_by_target(records).items()
            if key in keys
        )
        grouped = self._group_records_by_target(records)
        self._fallback_pending.difference_update(
            key
            for key in keys
            if key not in incomplete_keys
            and not any(is_retryable(record) for record in grouped.get(key, []))
        )
        summary["pending_targets"] = len(keys & self._fallback_pending)
        summary["completed_targets"] = len(keys - self._fallback_pending)
        coverage = set(request.document_review.get("coverage") or {})
        summary["low_coverage_targets"] = sum(
            distinct_accepted_count(grouped.get(key, []))
            < int(request.document_review.get("keep_top", 1))
            or bool(coverage - covered_labels(grouped.get(key, [])))
            for key in keys - self._fallback_pending
        )
        notes.append(
            f"Fallback outcome: {summary['completed_targets']} exhausted target(s) complete "
            f"({summary['low_coverage_targets']} below source/coverage goal); "
            f"{summary['pending_targets']} interrupted target(s) remain retryable."
        )
        # Remap subset metrics to the original active roster for completion.
        indices = {
            identify_target(t, request.partition_by): idx
            for idx, t in enumerate(request.targets or [])
        }
        for metric in metrics:
            target = fallback_request.targets[int(metric["target_index"])]
            metric["target_index"] = indices[identify_target(target, request.partition_by)]
            metric["stage"] = "fallback"
        notes.append(
            f"Fallback: {len(eligible)} weak target(s), {calls} search attempt(s), "
            f"{len(downloaded)} download(s), {summary['accepted']} distinct accepted source(s)."
        )
        return found, notes, errors, costs, metrics, summary

    def _annotate_target_outcomes(self, request, records, metrics, incomplete_keys):
        """Compact operational counts; coverage is evidence, not an accuracy score."""
        if not request.document_review or not request.targets:
            return
        grouped = self._group_records_by_target(records)
        required = set(request.document_review.get("coverage") or {})
        goal = int(request.document_review.get("keep_top", 1))
        for metric in metrics:
            target = request.targets[int(metric["target_index"])]
            key = identify_target(target, request.partition_by)
            outcomes = grouped.get(key, [])
            missing = sorted(required - covered_labels(outcomes))
            accepted = distinct_accepted_count(outcomes)
            reasons = set(self._acquisition_pending.get(key, set()))
            if metric.get("search_complete") is False:
                reasons.add("search_" + str(metric.get("pending_reason") or "provider"))
            for record in outcomes:
                if record.get("review_failed"):
                    reasons.add(
                        "review_budget"
                        if record.get("review_budget_exhausted")
                        else "review_evidence"
                        if "evidence" in str(record.get("review_error") or "")
                        else "review_provider"
                    )
                elif is_retryable(record):
                    reasons.add("download_provider")
            pending = (
                key in incomplete_keys
                or key in self._fallback_pending
                or metric.get("search_complete") is False
                or any(is_retryable(record) for record in outcomes)
            )
            if pending and not reasons:
                reasons.add("acquisition_unfinished")
            low = accepted < goal or bool(missing)
            metric.update(
                downloaded=sum(record.get("status") == "downloaded" for record in outcomes),
                accepted_sources=accepted,
                review_failed=sum(bool(record.get("review_failed")) for record in outcomes),
                missing_coverage=missing,
                pending_reasons=sorted(reasons),
                source_status="pending" if pending else "low_coverage" if low else "qualified",
            )

    @staticmethod
    def _seed_recording_transcripts(curated_dir: Path, records: list[dict[str, object]]) -> None:
        """Seed run copies of recordings with their curated transcript caches.

        Review then grades them without transcribing again.
        """
        targets: dict[Path, list[Path]] = {}
        for record in records:
            path = Path(str(record.get("path") or ""))
            if record.get("status") == "downloaded" and is_audio(path) and path.is_file():
                partition = Path(str(record.get("relative_path") or path.name)).parent
                targets.setdefault(partition, []).append(path)
        for partition, paths in targets.items():
            published = curated_dir / partition
            if published.is_dir():
                donors = [p for p in published.iterdir() if p.is_file() and is_audio(p)]
                inherit_text_caches(paths, donors)

    def _run_document_review(
        self,
        downloads: list[dict[str, object]],
        review_cfg: dict[str, object],
        notes: list[str],
        cache_dir: Path,
        models: dict[str, object] | None = None,
        classifier_keywords: list[str] | None = None,
        identity_fields: list[str] | None = None,
        *,
        attempt_budget: AttemptBudget | None = None,
        max_per_host_per_target: int = 0,
    ) -> tuple[list[dict[str, object]], list[str], dict[str, object]]:
        """LLM-grade downloaded files and promote the primary one(s).

        Annotate records in place; grades are reused from ``cache_dir``.
        Review errors remain isolated
        from acquisition and prevent incomplete curation from replacing
        previously published documents.

        Returns (downloads, notes, costs_dict).
        """
        description = str(review_cfg.get("document_description") or "").strip()
        if not description:
            notes.append("Document review skipped (no document_description configured).")
            return downloads, notes, {}

        reviewer = self._reviewer or DocumentReviewer(
            document_description=description,
            model=(str(review_cfg["model"]) if review_cfg.get("model") else None),
            models={str(k): str(v) for k, v in (models or {}).items()} or None,
            keep_top=int(review_cfg.get("keep_top", 1) or 1),
            max_chars=int(review_cfg.get("max_chars", 12000) or 12000),
            review_keywords=review_cfg.get("keywords") or classifier_keywords,
            deduplicate_redundant=bool(review_cfg.get("deduplicate_redundant", True)),
            dedup_key_fields=[
                str(field)
                for field in ((review_cfg.get("deduplication") or {}).get("key_fields") or [])
                if str(field).strip()
            ]
            or None,
            cache_dir=cache_dir,
            identity_fields=identity_fields,
            context_fields=review_cfg.get("context_fields"),
            coverage=review_cfg.get("coverage"),
            escalate_model=review_cfg.get("escalate_model"),
            attempt_budget=attempt_budget,
            max_per_host_per_target=max_per_host_per_target,
        )
        self._reviewer = reviewer
        previous_costs = reviewer.get_costs()

        def cost_delta():
            return {
                key: value - previous_costs.get(key, 0)
                for key, value in reviewer.get_costs().items()
                if isinstance(value, (int, float))
            }

        # Held PDFs never consume grading/dedup/top-N capacity. Configured
        # URLs still bypass relevance review, but not explicitly enabled screening.
        reviewable = [
            r for r in downloads if r.get("source") != CONFIGURED_SOURCE and not eligibility_held(r)
        ]
        for record in downloads:
            if eligibility_held(record):
                record["review_selected"] = False
            elif record.get("source") == CONFIGURED_SOURCE:
                record["review_selected"] = record.get("status") == "downloaded"
        try:
            reviewable, notes = reviewer.review(reviewable, notes)
            return downloads, notes, cost_delta()
        except Exception as exc:  # noqa: BLE001 - review is best-effort
            # Failed review protects prior consolidated partitions.
            for failed_record in reviewable:
                failed_record["review_failed"] = True
                failed_record["review_retryable"] = True
            notes.append(f"Document review error (skipped): {exc}")
            return downloads, notes, cost_delta()

    def _initial_completion_blocks(
        self,
        *,
        request: DiscoveryRequest,
        selection_metrics: list[dict[str, object]],
        seeker_failed: bool,
    ) -> tuple[set[str], _PartitionIndex]:
        """Block partitions of active targets whose search is unfinished."""
        targets = request.targets or []
        incomplete_keys = {
            identify_target(targets[int(metric["target_index"])], request.partition_by)
            for metric in selection_metrics
            if metric.get("search_complete") is False and targets
        }
        if seeker_failed:
            incomplete_keys.update(identify_target(t, request.partition_by) for t in targets)
        blocked = _PartitionIndex()
        for target in targets:
            key = identify_target(target, request.partition_by)
            if key in incomplete_keys:
                blocked.add(
                    self._partition(request, key, self._target_metadata(target)),
                    "search unfinished",
                )
        if not targets and any(
            metric.get("search_complete") is False for metric in selection_metrics
        ):
            blocked.add(Path(), "search unfinished")
        return incomplete_keys, blocked

    def _block_candidates(
        self,
        *,
        request: DiscoveryRequest,
        candidates: list[DiscoveryCandidate],
        incomplete_keys: set[str],
        blocked: _PartitionIndex,
        cause: str,
    ) -> None:
        """Keep the owners of unfinished candidates incomplete and blocked."""
        for candidate in candidates:
            key = owner_key(candidate.target_id, candidate.source_url)
            if key:
                incomplete_keys.add(key)
                self._acquisition_pending[key].add(
                    "download_budget" if cause.startswith("budget") else "crawl_unfinished"
                )
            blocked.add(
                self._partition(request, candidate.target_id, candidate.target_metadata),
                cause,
            )

    def _download_completions(
        self,
        *,
        request: DiscoveryRequest,
        download_records: list[dict[str, object]],
        incomplete_keys: set[str],
        blocked: _PartitionIndex,
        run_id: str,
        started_at: datetime,
    ) -> dict[str, dict[str, object]]:
        """Complete targets and sources whose outcomes are all final.

        Every non-retryable outcome (download, terminal failure, skip,
        rejection) counts toward completion. Retryable failures block their
        partition and keep the key incomplete.
        Entries stay in memory until publication provides evidence.
        """
        failed_keys: set[str] = set()
        for record in download_records:
            if not is_retryable(record):
                continue
            key = self._record_completion_key(record)
            if key:
                failed_keys.add(key)
            blocked.add(
                self._partition(
                    request,
                    record.get("target_id"),
                    record.get("target_metadata"),
                ),
                "retryable review failure"
                if record.get("review_failed")
                else "retryable download failure",
            )

        entries: dict[str, dict[str, object]] = {}
        for record in download_records:
            key = self._record_completion_key(record)
            if not key:
                continue
            if key in failed_keys or key in incomplete_keys:
                continue
            if blocked and self._partition_is_blocked(
                self._partition(
                    request,
                    record.get("target_id"),
                    record.get("target_metadata"),
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
            entries[key]["download_count"] += record.get("status") == "downloaded"
        return entries

    def _empty_target_completions(
        self,
        *,
        request: DiscoveryRequest,
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
            record["target_id"] for record in download_records if record.get("target_id")
        }
        empty_parts: set[Path] = set()
        for metric in selection_metrics:
            if metric.get("search_complete") is False or metric.get("selected_candidates"):
                continue
            target = request.targets[int(metric["target_index"])]
            key = identify_target(target, request.partition_by)
            if key in incomplete_keys or key in attempted_keys:
                continue
            entries[key] = {
                "run_id": run_id,
                "completed_at": started_at.isoformat(),
                "outcome": "empty",
                "download_count": 0,
            }
            empty_parts.add(self._partition(request, key, self._target_metadata(target)))
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
                result.review_index_path = paths.manifest_path.parent / "review.csv"
                reviewed = any(
                    record.get("review_selected") is not None for record in download_records
                )
                result.curated_count = sum(
                    record_is_curated(record, review_ran=reviewed) for record in download_records
                )
            elif empty_parts or (
                (request.document_classifier or {}).get("pdf_screening") == "hold"
                and not all_reused
            ):
                result.curated_dir = paths.run_curated_dir
        result.status = discovery_status(
            dry_run=request.dry_run,
            errors=errors,
            usable=(bool(candidates) if request.dry_run else result.curated_count > 0),
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
                result.download_index_path = paths.domain_dir / "download_index.csv"
        return result

    def _build_completion_manifest(
        self,
        *,
        request: DiscoveryRequest,
        paths: PublicationPaths,
        result: DiscoveryResult,
        started_at: datetime,
        candidates: list[DiscoveryCandidate],
        download_records: list[dict[str, object]],
        stage_summaries: dict[str, object],
        lineage: dict[str, object],
        notes: list[str],
    ) -> DiscoveryManifest:
        """Combine result metadata and stage evidence."""
        completed_at = datetime.now(UTC)
        return DiscoveryManifest(
            run_id=result.run_id,
            status=result.status,
            started_at=started_at.isoformat(),
            input={
                "domain": request.domain,
                "sources": list(request.sources),
                "queries": list(request.queries),
                "dry_run": request.dry_run,
            },
            constraints=self._build_constraints(request),
            timing={
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "elapsed_seconds": round((completed_at - started_at).total_seconds(), 3),
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
                    result.curated_dir.as_posix() if result.curated_dir is not None else None
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
        new_entries: dict[str, dict[str, object]],
        attempted_parts: set[Path],
        blocked: _PartitionIndex,
        all_reused: bool,
    ) -> Path:
        """Name documents, render metadata, and publish it with selected
        curation under one corpus lock.

        Reuse returns a new snapshot, preserving the editable run.
        Immutable snapshots also provide checkpoint manifest evidence.
        """
        snapshot_path = paths.manifest_path.parent / "outcomes" / f"{result.run_id}.json"
        publishes = not request.dry_run and not all_reused
        with discovery_publication(paths.domain_dir) if publishes else contextlib.nullcontext():
            if publishes:
                # Blocked partitions are published additively, not replaced.
                assign_document_names(
                    manifest.downloads,
                    paths.domain_dir / "download_index.csv",
                    {part for part in attempted_parts if not blocked.overlaps(part)},
                )
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
            manifest_content = json.dumps(payload, indent=2, sort_keys=True)
            if all_reused:
                atomic_write(snapshot_path, manifest_content)
                result.manifest_path = snapshot_path
                return snapshot_path
            metadata_updates = {
                paths.manifest_path: manifest_content,
                snapshot_path: manifest_content,
            }
            if publishes:
                index_content = self._write_download_index(
                    request=request,
                    run_id=result.run_id,
                    manifest_path=paths.manifest_path,
                    download_records=manifest.downloads,
                    index_path=paths.run_index,
                    render_only=True,
                )
                metadata_updates[paths.run_index] = index_content
                metadata_updates[snapshot_path.with_suffix(".index.csv")] = index_content
                if result.review_index_path:
                    metadata_updates[result.review_index_path] = self._write_review_index(
                        download_records=manifest.downloads,
                        run_dir=paths.manifest_path.parent,
                        render_only=True,
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
                        completed=set(new_entries),
                        pdf_screening_patterns=(
                            (request.selection_draft_patterns or [])
                            if request.selection_exclude_draft
                            else []
                        )
                        if (request.document_classifier or {}).get("pdf_screening") == "hold"
                        else None,
                        _lock_held=True,
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
        prior_outcomes: dict[str, dict[str, dict[str, object]]],
    ) -> None:
        """Save completions with published artifact hashes, and the final
        child outcomes of incomplete targets/sources for the next run.
        """
        if result.dry_run:
            return
        saved = {
            key: {"incomplete": True, "run_id": result.run_id, "outcomes": outcomes}
            for key, outcomes in self._incomplete_outcomes(
                paths=paths,
                download_records=download_records,
                completed=entries,
                prior=prior_outcomes,
            ).items()
        }
        for key in self._fallback_pending:
            saved.setdefault(key, {"incomplete": True, "run_id": result.run_id, "outcomes": {}})
            saved[key]["fallback_pending"] = True
        if entries and result.download_index_path:
            evidence = {
                str(snapshot_path): artifact_sha256(snapshot_path),
                str(snapshot_path.with_suffix(".index.csv")): artifact_sha256(
                    snapshot_path.with_suffix(".index.csv")
                ),
            }
            curated_paths: dict[str, dict[str, str]] = {}
            for record in download_records:
                key = self._record_completion_key(record)
                relative_path = record.get("relative_path")
                if not key or not relative_path or key not in entries:
                    continue
                path = paths.domain_dir / "curated" / str(relative_path)
                if path.is_file():
                    curated_paths.setdefault(key, {})[str(path)] = artifact_sha256(path)
            for key, entry in entries.items():
                entry["artifacts"] = evidence
                entry["curated_paths"] = curated_paths.get(key, {})
            saved.update(entries)
        if saved:
            self._save_checkpoint_entries(checkpoint_path, saved, fingerprint=fingerprint)

    def _incomplete_outcomes(
        self,
        *,
        paths: PublicationPaths,
        download_records: list[dict[str, object]],
        completed: dict[str, dict[str, object]],
        prior: dict[str, dict[str, dict[str, object]]],
    ) -> dict[str, dict[str, dict[str, object]]]:
        """Final child outcomes of owners left incomplete, merged over
        ``prior``: downloads with file evidence, and other non-retryable
        outcomes. Retryable failures are fetched again.
        """
        outcomes: dict[str, dict[str, dict[str, object]]] = {}
        for record in download_records:
            key = self._record_completion_key(record)
            url = record.get("url")
            if not key or key in completed or not isinstance(url, str):
                continue
            if record.get("status") == "downloaded":
                path = Path(str(record.get("path") or ""))
                if not path.is_file() and record.get("relative_path"):
                    path = paths.domain_dir / "curated" / str(record["relative_path"])
                if not path.is_file() or not record.get("content_hash"):
                    continue
                outcome = {
                    "status": "downloaded",
                    "final_url": record.get("final_url") or url,
                    "mime_type": record.get("mime_type"),
                    "path": str(path.resolve()),
                    "sha256": record["content_hash"],
                }
            elif is_retryable(record):
                continue
            else:
                outcome = {
                    field: record[field]
                    for field in PRIOR_OUTCOME_FIELDS
                    if record.get(field) is not None
                }
            outcomes.setdefault(key, {})[url] = outcome
        return {key: {**prior.get(key, {}), **new} for key, new in outcomes.items()}

    @classmethod
    def _remove_superseded_run_files(
        cls,
        *,
        paths: PublicationPaths,
        download_records: list[dict[str, object]],
        prior: dict[str, dict[str, dict[str, object]]],
    ) -> None:
        """Delete an earlier run's copy of each file this run reused; the
        current run now holds it. Consolidated curated files are never
        touched.
        """
        runs_dir = (paths.domain_dir / "runs").resolve()
        current = paths.documents_dir.resolve()
        for record in download_records:
            if record.get("download_method") != REUSED_DOWNLOAD_METHOD:
                continue
            outcome = prior.get(cls._record_completion_key(record) or "", {}).get(
                str(record.get("url"))
            )
            if not outcome or not outcome.get("path"):
                continue
            old = Path(str(outcome["path"])).resolve()
            if runs_dir not in old.parents or current in old.parents:
                continue
            for path in (old, *text_cache_paths(old)):
                path.unlink(missing_ok=True)
            for parent in old.parents:
                if parent == runs_dir:
                    break
                try:
                    parent.rmdir()
                except OSError:
                    break

    def _finalize_completion(
        self,
        *,
        request: DiscoveryRequest,
        paths: PublicationPaths,
        result: DiscoveryResult,
        download_records: list[dict[str, object]],
        entries: dict[str, dict[str, object]],
        prior_outcomes: dict[str, dict[str, dict[str, object]]],
        snapshot_path: Path,
        checkpoint_path: Path,
        fingerprint: str,
        all_reused: bool,
    ) -> None:
        """Finalize retention, checkpoint evidence, and latest."""
        if request.dry_run:
            return
        if not all_reused and request.retention_documents != "all":
            # Incomplete owners resume from these files; keep them.
            unfinished = {
                Path(str(record["path"]))
                for record in download_records
                if record.get("path")
                and (key := self._record_completion_key(record))
                and key not in entries
            }
            self._apply_document_retention(
                mode=request.retention_documents,
                documents_dir=paths.documents_dir,
                curated_dir=result.curated_dir,
                download_records=download_records,
                keep=frozenset(unfinished),
            )
        self._save_completion_evidence(
            paths=paths,
            result=result,
            download_records=download_records,
            entries=entries,
            snapshot_path=snapshot_path,
            checkpoint_path=checkpoint_path,
            fingerprint=fingerprint,
            prior_outcomes=prior_outcomes,
        )
        if not all_reused and request.retention_documents != "all":
            # After the checkpoint points at this run's copies.
            self._remove_superseded_run_files(
                paths=paths, download_records=download_records, prior=prior_outcomes
            )
        # Reuse and empty runs keep the prior latest-downloads pointer.
        if download_records:
            self._refresh_latest_pointer(paths.manifest_path.parent, domain_dir=paths.domain_dir)

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
        if request.fallback_queries and not (
            request.document_review
            and str(request.document_review.get("document_description") or "").strip()
        ):
            raise ValueError("search.fallback_queries requires document_review")
        # Direct callers receive the same before-request template safety as
        # config/CLI preflight; target hints are not partition identity.
        if request.document_review or request.fallback_queries:
            review = request.document_review or {}
            for _ in iter_review_scopes(
                str(review.get("document_description") or ""),
                review.get("context_fields"),
                review.get("coverage"),
                request.targets or [{}],
                additional_templates=request.fallback_queries or [],
            ):
                pass
        self._attempt_budget = AttemptBudget(
            max_review_calls=request.max_review_calls, max_search_calls=request.max_search_calls
        )
        self._reviewer = None
        self._fallback_pending = set()
        self._acquisition_pending = defaultdict(set)
        started_at = datetime.now(UTC)
        self._rate_limiter = _RequestRateLimiter(request.min_request_interval_ms)
        self._retry_errors = []
        self._retry_costs = Counter()
        self._retry_search_calls = 0
        self._remaining_downloads = request.max_files
        run_id = self._build_run_id(request, started_at)
        documents_dir, manifest_path = self._resolve_output_paths(request, run_id)
        paths = resolve_publication_paths(documents_dir, manifest_path, request.domain)
        documents_dir, manifest_path = paths.documents_dir, paths.manifest_path
        fingerprint = effective_discovery_fingerprint(request)
        if not request.dry_run:
            with discovery_publication(paths.domain_dir):
                pass
        seeker_state, seeker_errors, seeker_notes = self._resolve_serpapi_state(request)
        all_errors = normalize_error_records(seeker_errors)
        self._emit_progress(
            request,
            f"run: discovery started (run_id={run_id})",
        )

        # --fresh clears evidence instead of reusing completions.
        original_targets = list(request.targets or [])
        checkpoint_path = paths.domain_dir / "checkpoint.json"
        if request.fresh:
            if not request.dry_run:
                with contextlib.suppress(OSError):
                    checkpoint_path.unlink(missing_ok=True)
            completed_checkpoint_keys: dict[str, dict[str, object]] = {}
            prior_outcomes: dict[str, dict[str, dict[str, object]]] = {}
        else:
            completed_checkpoint_keys = self._load_checkpoint(
                checkpoint_path, fingerprint=fingerprint
            )
        original_sources = list(request.sources)
        request, skip_notes = self._skip_completed_inputs(request, completed_checkpoint_keys)
        if not request.fresh:
            active_keys = set(self._input_digests(request)) | {
                owner_key(None, url) for url in request.sources
            }
            prior_outcomes = load_prior_outcomes(
                checkpoint_path, fingerprint=fingerprint, owner_keys=active_keys
            )
        if request.fallback_queries and (
            request.dry_run
            or not self._search_enabled(request)
            or (original_targets and not request.targets)
        ):
            seeker_errors = []
            all_errors = []
        seeker_notes.extend(skip_notes)
        if request.fallback_queries and not request.seeker_cache:
            seeker_notes.append(
                "Adaptive search stores successful query results in the search cache "
                "to resume unfinished stages without repeated paid requests."
            )
        if request.fallback_queries and checkpoint_path.is_file() and not request.fresh:
            with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
                checkpoint = json.loads(checkpoint_path.read_text())
                self._fallback_pending = {
                    key
                    for key, entry in checkpoint.get("entries", {}).items()
                    if entry.get("fingerprint") == fingerprint and entry.get("fallback_pending")
                }

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
            ) = self._run_seeker(request, seeker_state, seeker_notes, all_errors)
            seeker_raw_count = sum(
                int(metric.get("input_candidates", 0)) for metric in target_selection_metrics
            )
            self._emit_progress(
                request,
                f"run: seeker stage completed: {seeker_raw_count} candidate(s) "
                f"found across {len(target_selection_metrics)} target(s), "
                f"{len(seeker_candidates)} selected",
            )

        # Configured URLs of resumed targets were dropped with their rows.
        configured = self._configured_candidates(request)
        configured_keys = {(c.url, c.target_id) for c in configured}
        candidates = configured + [
            c for c in seeker_candidates if (c.url, c.target_id) not in configured_keys
        ]
        all_errors, seeker_notes = self._append_discovery_gap_diagnostics(
            request=request,
            candidates=candidates,
            errors=all_errors,
            notes=seeker_notes,
            discovered_count=seeker_raw_count,
        )
        if request.fallback_queries:
            # Reconstruct only outcomes belonging to this active target. All
            # download evidence is checked again by _reusable_outcomes; stale
            # bytes are not resurrected as qualified sources.
            existing = {(candidate.url, candidate.target_id) for candidate in candidates}
            for target in request.targets or []:
                key = identify_target(target, request.partition_by)
                for url in prior_outcomes.get(key, {}):
                    if (url, key) not in existing:
                        candidate = self._seeker_candidate(
                            {
                                "url": url,
                                "source": "serpapi_google",
                                "reasons": ["Prior incomplete source outcome"],
                            },
                            target,
                            request,
                        )
                        if (
                            candidate is not None
                            and self._passes_final_url_selection_guard(
                                final_url=candidate.url, request=request
                            )[0]
                        ):
                            candidates.append(candidate)
                            existing.add((url, key))
        candidates_after_seeker = len(candidates)

        candidates, crawl_errors, crawl_notes, crawl_state, crawl_unfinished = (
            self._crawl_candidates(request=request, candidates=candidates, prior=prior_outcomes)
        )
        all_errors = normalize_error_records([*all_errors, *crawl_errors])
        if crawl_state["applied"]:
            # Crawled links obey selection; configured URLs pass through.
            candidates, post_crawl_notes = self._apply_post_routing_selection_filters(
                request=request,
                candidates=candidates,
            )
            crawl_notes.extend(post_crawl_notes)
            candidates = [
                candidate
                for candidate in candidates
                if candidate.source == CONFIGURED_SOURCE
                or self._passes_final_url_selection_guard(final_url=candidate.url, request=request)[
                    0
                ]
            ]
        candidates_after_crawl = len(candidates)

        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        download_records: list[dict[str, object]] = []
        incomplete_keys, blocked_parts = self._initial_completion_blocks(
            request=request,
            selection_metrics=target_selection_metrics,
            seeker_failed=bool(seeker_errors and not target_selection_metrics),
        )
        self._block_candidates(
            request=request,
            candidates=crawl_unfinished,
            incomplete_keys=incomplete_keys,
            blocked=blocked_parts,
            cause=str(crawl_state.get("unfinished_cause") or "crawl unfinished"),
        )
        download_notes: list[str] = []
        review_costs: dict[str, object] = {}
        new_checkpoint_entries: dict[str, dict[str, object]] = {}
        attempted_parts = set()
        review_only_blocks = set()
        if not request.dry_run and candidates:
            self._emit_progress(request, "run: download stage started")
            candidates_for_download, gating_notes = self._filter_candidates_for_download(candidates)
            download_notes.extend(gating_notes)
            download_errors: list[dict[str, object]] = []
            if candidates_for_download:
                download_records, download_errors, download_notes, review_costs = (
                    self._acquire_batch(
                        request=request,
                        candidates=candidates_for_download,
                        documents_dir=documents_dir,
                        prior=prior_outcomes,
                        incomplete_keys=incomplete_keys,
                        blocked=blocked_parts,
                        notes=download_notes,
                    )
                )
                review_cfg = request.document_review
                if review_cfg and download_records and not request.fallback_queries:
                    classifier_cfg = getattr(request, "document_classifier", None) or {}
                    download_records, download_notes = self._run_post_review_retries(
                        request=request,
                        download_records=download_records,
                        notes=download_notes,
                        documents_dir=documents_dir,
                        classifier_keywords=classifier_cfg.get("nice_to_have_keywords"),
                    )

                def record_partition(record: dict[str, object]) -> Path:
                    return self._partition(
                        request,
                        record.get("target_id"),
                        record.get("target_metadata"),
                    )

                hard_blocks = set(blocked_parts) | {
                    record_partition(record)
                    for record in download_records
                    if record.get("status") == "failed" and is_retryable(record)
                }
                review_only_blocks = {
                    record_partition(record)
                    for record in download_records
                    if record.get("review_failed") and is_retryable(record)
                } - hard_blocks
                new_checkpoint_entries = self._download_completions(
                    request=request,
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
                if request.fresh:
                    attempted_parts.update(
                        Path(str(record["relative_path"])).parent
                        for record in download_records
                        if record.get("relative_path")
                    )
                if blocked_parts:
                    download_notes.append(
                        f"{len(blocked_parts.paths)} partition(s) still incomplete "
                        f"({'; '.join(sorted(blocked_parts.causes))}): documents "
                        "downloaded so far were published additively, nothing was "
                        "withdrawn, and the next run continues."
                    )

        (
            fallback_candidates,
            fallback_notes,
            fallback_errors,
            fallback_costs,
            fallback_metrics,
            fallback_summary,
        ) = self._run_adaptive_search(
            request=request,
            records=download_records,
            candidates=candidates,
            documents_dir=documents_dir,
            prior=prior_outcomes,
            incomplete_keys=incomplete_keys,
            blocked=blocked_parts,
        )
        candidates.extend(fallback_candidates)
        download_notes.extend(fallback_notes)
        all_errors = normalize_error_records([*all_errors, *fallback_errors])
        fallback_summary["targets"] = fallback_metrics
        seeker_queries_run += int(fallback_summary.get("queries_executed") or 0)
        metrics_by_target = {metric["target_index"]: metric for metric in target_selection_metrics}
        for metric in fallback_metrics:
            previous = metrics_by_target.get(metric["target_index"])
            if previous is None:
                target_selection_metrics.append(metric)
            else:
                for count in ("input_candidates", "selected_candidates", "scope_rejected"):
                    previous[count] = int(previous.get(count) or 0) + int(metric.get(count) or 0)
                if metric.get("search_complete") is False:
                    previous["search_complete"] = False
                    previous["pending_reason"] = metric.get("pending_reason", "provider")
                previous["fallback_outcome"] = metric.get("outcome")
        for key, value in fallback_costs.items():
            review_costs[key] = review_costs.get(key, 0) + value
        if request.fallback_queries and not request.dry_run:
            hard_blocks_before_completion = set(blocked_parts) - review_only_blocks
            new_checkpoint_entries = self._download_completions(
                request=request,
                download_records=download_records,
                incomplete_keys=incomplete_keys,
                blocked=blocked_parts,
                run_id=run_id,
                started_at=started_at,
            )
            hard_blocks = hard_blocks_before_completion | {
                self._partition(request, record.get("target_id"), record.get("target_metadata"))
                for record in download_records
                if record.get("status") == "failed" and is_retryable(record)
            }
            review_only_blocks = {
                self._partition(request, record.get("target_id"), record.get("target_metadata"))
                for record in download_records
                if record.get("review_failed") and is_retryable(record)
            } - hard_blocks
            if request.fresh:
                attempted_parts.update(
                    Path(str(record["relative_path"])).parent
                    for record in download_records
                    if record.get("relative_path")
                )
        self._annotate_target_outcomes(
            request, download_records, target_selection_metrics, incomplete_keys
        )
        if fallback_metrics:
            self._annotate_target_outcomes(
                request, download_records, fallback_metrics, incomplete_keys
            )
        empty_parts = self._empty_target_completions(
            request=request,
            selection_metrics=target_selection_metrics,
            download_records=download_records,
            incomplete_keys=incomplete_keys,
            entries=new_checkpoint_entries,
            run_id=run_id,
            started_at=started_at,
        )
        if request.fresh:
            attempted_parts.update(empty_parts)
        for key, digest in self._input_digests(request).items():
            if key in new_checkpoint_entries:
                new_checkpoint_entries[key]["input_digest"] = digest
        for record in download_records:
            if record.get("review_failed"):
                all_errors.append(
                    {
                        **build_error_record(
                            RuntimeError(
                                str(record.get("review_error") or "Document review failed")
                            ),
                            stage="discovery.review",
                            document_path=str(record.get("path") or record.get("url")),
                        ),
                        "retryable": is_retryable(record),
                        "target_label": record.get("target_label"),
                    }
                )
        all_errors = normalize_error_records(all_errors)
        all_reused = bool(
            (original_targets or original_sources)
            and not request.targets
            and not request.sources
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
            candidates=candidates,
            download_records=download_records,
            stage_summaries={
                "seeker": {
                    "provider": seeker_state["provider"],
                    "enabled": bool(seeker_state.get("enabled")),
                    "queries_executed": seeker_queries_run,
                    "candidates_discovered": seeker_raw_count,
                    "candidates_after_prioritization": candidates_after_seeker,
                    "link_prioritization_mode": (request.link_prioritization_mode),
                    "link_top_k": request.link_top_k,
                },
                "crawl": {
                    "provider": request.crawl_provider,
                    "max_depth": request.max_depth,
                    "applied": bool(crawl_state["applied"]),
                    "candidates_in": candidates_after_seeker,
                    "candidates_out": candidates_after_crawl,
                },
                "fallback": fallback_summary,
                "attempt_budget": self._attempt_budget.snapshot(),
                "downloads": self._build_download_summary(download_records),
                "document_review": (
                    {
                        "costs": review_costs,
                        "configured_url_bypass": sum(
                            record.get("source") == CONFIGURED_SOURCE for record in download_records
                        ),
                        "automatically_qualified": sum(
                            record.get("source") != CONFIGURED_SOURCE
                            and bool(record.get("review_selected"))
                            and bool(record.get("review_is_primary"))
                            and not record.get("review_failed")
                            for record in download_records
                        ),
                    }
                    if request.document_review
                    else {}
                ),
                "acceptance_metrics": self._build_acceptance_metrics(
                    request=request,
                    seeker_state=seeker_state,
                    target_selection_metrics=target_selection_metrics,
                ),
            },
            lineage={
                "blocked_partitions": [str(part) for part in blocked_parts],
                "replaced_partitions": [str(part) for part in attempted_parts],
                "review_only_blocked_partitions": [str(part) for part in review_only_blocks],
                "request_fingerprint": fingerprint,
                "seeker": seeker_state,
                "crawl": crawl_state,
                "link_prioritization": {
                    "mode": request.link_prioritization_mode,
                    "top_k": request.link_top_k,
                    "candidates": prioritizer_lineage,
                },
            },
            notes=[*seeker_notes, *crawl_notes, *download_notes],
        )
        snapshot_path = self._publish_completion(
            request=request,
            paths=paths,
            result=result,
            manifest=manifest,
            completed_entries=completed_checkpoint_keys,
            new_entries=new_checkpoint_entries,
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
            prior_outcomes=prior_outcomes,
            snapshot_path=snapshot_path,
            checkpoint_path=checkpoint_path,
            fingerprint=fingerprint,
            all_reused=all_reused,
        )
        return result
