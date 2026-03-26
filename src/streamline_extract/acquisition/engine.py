"""Scaffold acquisition engine for initial CLI integration."""

from __future__ import annotations

import json
import csv
import hashlib
import importlib.util
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from .connectors import DiggerInput, resolve_digger_connector
from .models import AcquisitionCandidate, AcquisitionManifest, CandidateScore
from streamline_extract.utils.error_taxonomy import (
    build_error_record,
    normalize_error_records,
    summarize_error_records,
)


@dataclass(slots=True)
class AcquisitionRequest:
    """Normalized command inputs for an acquisition run."""

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
    max_concurrent_downloads: int = 2
    min_request_interval_ms: int = 0
    targets: list[dict[str, object]] | None = None
    query_templates: list[str] | None = None
    query_families: dict[str, list[str]] | None = None
    use_query_family: str | None = None
    seeker_max_results: int = 10


@dataclass(slots=True)
class AcquisitionResult:
    """Minimal result payload for the scaffold engine."""

    run_id: str
    manifest_path: Path
    documents_dir: Path
    dry_run: bool
    download_index_path: Path | None = None


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


class AcquisitionEngine:
    """Initial acquisition engine that emits deterministic scaffold artifacts."""

    _SUPPORTED_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".xlsx", ".csv"}
    _MIME_EXTENSION_MAP = {
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "text/plain": ".txt",
        "text/csv": ".csv",
        "application/csv": ".csv",
        "application/vnd.ms-excel": ".xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }
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

    def _build_run_id(self, request: AcquisitionRequest, started_at: datetime) -> str:
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
        request: AcquisitionRequest,
        run_id: str,
    ) -> tuple[Path, Path]:
        if request.output_documents is not None:
            documents_dir = request.output_documents
        else:
            documents_dir = Path("documents") / request.domain / "acquired" / "runs" / run_id

        if request.output_manifest is not None:
            manifest_path = request.output_manifest
        else:
            manifest_path = (
                Path("output")
                / "acquisition"
                / request.domain
                / "runs"
                / run_id
                / "manifest.json"
            )
        return documents_dir, manifest_path

    @staticmethod
    def _scaffold_candidates(seed_urls: list[str]) -> list[AcquisitionCandidate]:
        candidates: list[AcquisitionCandidate] = []
        for seed_url in seed_urls:
            score = CandidateScore(
                url_signal=0.35,
                anchor_signal=0.0,
                content_signal=0.0,
                trust_signal=0.55,
            )
            candidates.append(
                AcquisitionCandidate(
                    url=seed_url,
                    source="seed_url",
                    score=score,
                    reasons=["Seed URL staged for seeker/digger expansion"],
                )
            )
        return candidates

    @staticmethod
    def _normalize_seed_urls(seed_urls: list[str]) -> tuple[list[str], list[dict[str, object]]]:
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
                    raise ValueError(f"Seed URL must start with http:// or https://: {seed}")
                valid_seed_urls.append(seed)
            except Exception as exc:
                error_records.append(
                    build_error_record(
                        exc,
                        stage="acquisition.seed_validation",
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
    def _resolve_serpapi_state(
        request: AcquisitionRequest,
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
                    stage="acquisition.seeker_init",
                    provider="serpapi",
                )
            )
        elif not (os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY")):
            errors.append(
                build_error_record(
                    ValueError(
                        "SerpApi feature flag enabled but SERPAPI_API_KEY or SERPAPI_KEY is not set"
                    ),
                    stage="acquisition.seeker_init",
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
    def _run_seeker(
        request: AcquisitionRequest,
        seeker_state: dict[str, object],
        current_notes: list[str],
        current_errors: list[dict[str, object]],
    ) -> tuple[list[AcquisitionCandidate], list[str], list[dict[str, object]]]:
        """Call SerpApi seeker and return discovered candidates."""
        if not seeker_state.get("enabled") or not seeker_state.get("available"):
            return [], current_notes, current_errors

        notes = list(current_notes)
        errors = list(current_errors)

        try:
            from .connectors.serpapi_seeker import SerpApiSeeker
            from .connectors.base import SeekerInput

            seeker = SerpApiSeeker(
                retry_max_attempts=request.retry_max_attempts,
                retry_initial_backoff_seconds=request.retry_initial_backoff_seconds,
                retry_max_backoff_seconds=request.retry_max_backoff_seconds,
                min_request_interval_seconds=max(0.0, float(request.min_request_interval_ms) / 1000.0),
            )
            seeker_inputs = AcquisitionEngine._build_seeker_inputs(request)
            raw_candidates: list[dict[str, object]] = []
            for seeker_input in seeker_inputs:
                raw_candidates.extend(seeker.discover(seeker_input))
        except Exception as exc:
            errors.append(
                build_error_record(
                    exc,
                    stage="acquisition.seeker_discover",
                    provider="serpapi",
                )
            )
            return [], notes, normalize_error_records(errors)

        candidates: list[AcquisitionCandidate] = []
        for raw in raw_candidates:
            url = raw.get("url")
            if not url:
                continue
            score = CandidateScore(
                url_signal=0.5,
                anchor_signal=0.1,
                content_signal=0.0,
                trust_signal=0.4,
            )
            candidates.append(
                AcquisitionCandidate(
                    url=url,
                    source=raw.get("source", "serpapi_google"),
                    score=score,
                    reasons=list(raw.get("reasons") or []),
                )
            )

        notes.append(f"SerpApi seeker returned {len(candidates)} candidate(s).")
        return candidates, notes, normalize_error_records(errors)

    @staticmethod
    def _target_template_context(target: dict[str, object]) -> dict[str, str]:
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
                        if singular_key.endswith("s") and len(singular_key) > 1:
                            context[singular_key[:-1]] = text

        if "utility_or_jurisdiction" not in context:
            if context.get("jurisdiction"):
                context["utility_or_jurisdiction"] = context["jurisdiction"]
            elif context.get("manufacturer"):
                context["utility_or_jurisdiction"] = context["manufacturer"]

        return context

    @staticmethod
    def _build_seeker_inputs(request: AcquisitionRequest):
        from .connectors.base import SeekerInput

        base_extra: dict[str, object] = {}
        if request.query_templates:
            base_extra["query_templates"] = request.query_templates
        if request.query_families:
            base_extra["query_families"] = request.query_families
        if request.use_query_family:
            base_extra["use_query_family"] = request.use_query_family

        targets = request.targets or []
        inputs: list[SeekerInput] = []

        if targets:
            for target in targets:
                if not isinstance(target, dict):
                    continue
                target_query = str(target.get("query") or request.query or "").strip()
                extra_params = dict(base_extra)
                extra_params["template_context"] = AcquisitionEngine._target_template_context(target)
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

    @staticmethod
    def _is_transient_network_error(exc: BaseException) -> bool:
        try:
            import requests

            if isinstance(
                exc,
                (
                    requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.SSLError,
                ),
            ):
                return True
        except Exception:
            pass

        lowered = str(exc).lower()
        markers = (
            "timeout",
            "temporarily unavailable",
            "try again",
            "connection reset",
            "connection aborted",
            "connection refused",
            "name resolution",
            "dns",
            "ssl",
            "tls",
            "429",
            "503",
            "504",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _retry_backoff_for_attempt(
        *,
        attempt: int,
        initial_backoff_seconds: float,
        max_backoff_seconds: float,
    ) -> float:
        if attempt <= 1:
            return 0.0
        wait = initial_backoff_seconds * (2 ** (attempt - 2))
        if max_backoff_seconds <= 0:
            return max(0.0, wait)
        return min(wait, max_backoff_seconds)

    @staticmethod
    def _resolve_retry_policy(request: AcquisitionRequest) -> dict[str, float | int]:
        return {
            "max_attempts": max(1, int(request.retry_max_attempts)),
            "initial_backoff_seconds": max(0.0, float(request.retry_initial_backoff_seconds)),
            "max_backoff_seconds": max(0.0, float(request.retry_max_backoff_seconds)),
        }

    def _download_with_retry(
        self,
        *,
        url: str,
        ssl_verify: bool,
        retry_policy: dict[str, float | int],
        rate_limiter: _RequestRateLimiter | None = None,
    ):
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("requests dependency is required for acquisition downloads") from exc

        max_attempts = int(retry_policy["max_attempts"])
        initial_backoff_seconds = float(retry_policy["initial_backoff_seconds"])
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
                )
                response.raise_for_status()
                return response, attempt
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts or not self._is_transient_network_error(exc):
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
        candidate: AcquisitionCandidate,
        request: AcquisitionRequest,
        documents_dir: Path,
        ssl_verify: bool,
        retry_policy: dict[str, float | int],
        rate_limiter: _RequestRateLimiter | None,
    ) -> tuple[int, dict[str, object], dict[str, object] | None, bool]:
        url = candidate.url
        if not isinstance(url, str) or not re.match(r"^https?://", url, flags=re.IGNORECASE):
            return idx, {"url": url, "status": "skipped_invalid_url"}, None, False

        try:
            response, attempt_count = self._download_with_retry(
                url=url,
                ssl_verify=ssl_verify,
                retry_policy=retry_policy,
                rate_limiter=rate_limiter,
            )

            final_url = response.url or url
            mime_type = (response.headers or {}).get("Content-Type")
            extension = self._infer_extension_from_url_or_mime(final_url, mime_type)
            if extension is None:
                return idx, {
                    "url": url,
                    "final_url": final_url,
                    "status": "skipped_unsupported_type",
                    "mime_type": mime_type,
                }, None, False

            partition_mode, partition_meta, partition_dir = self._resolve_partition_dir(
                documents_dir=documents_dir,
                url=final_url,
                request=request,
            )
            partition_dir.mkdir(parents=True, exist_ok=True)
            filename = self._safe_filename(final_url, extension, idx)
            target_path = partition_dir / filename
            suffix = 1

            while True:
                try:
                    bytes_written = 0
                    with target_path.open("xb") as handle:
                        for chunk in response.iter_content(chunk_size=65536):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            bytes_written += len(chunk)
                    break
                except FileExistsError:
                    suffix += 1
                    target_path = partition_dir / f"{Path(filename).stem}-{suffix}{extension}"

            return idx, {
                "url": url,
                "final_url": final_url,
                "partition_mode": partition_mode,
                "status": "downloaded",
                "mime_type": mime_type,
                "path": target_path.as_posix(),
                "relative_path": target_path.relative_to(documents_dir).as_posix(),
                "bytes": bytes_written,
                "attempt_count": attempt_count,
                **partition_meta,
            }, None, True
        except Exception as exc:
            error = build_error_record(
                exc,
                stage="acquisition.download",
                document_path=url,
                provider="http",
            )
            return idx, {
                "url": url,
                "status": "failed",
                "error": str(exc),
            }, error, False

    @staticmethod
    def _downloads_ssl_verify() -> bool:
        raw_value = os.getenv("ACQUISITION_SSL_VERIFY") or os.getenv("STREAMLINE_EXTRACT_SSL_VERIFY") or "true"
        return str(raw_value).strip().lower() not in {"0", "false", "no", "off"}

    @staticmethod
    def _infer_extension_from_url_or_mime(url: str, mime_type: str | None) -> str | None:
        parsed = urlparse(url)
        suffix = Path(parsed.path).suffix.lower()
        if suffix in AcquisitionEngine._SUPPORTED_EXTENSIONS:
            return suffix

        normalized_mime = (mime_type or "").split(";", 1)[0].strip().lower()
        ext = AcquisitionEngine._MIME_EXTENSION_MAP.get(normalized_mime)
        if ext in AcquisitionEngine._SUPPORTED_EXTENSIONS:
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
    def _infer_jurisdiction_from_query(cls, query: str | None) -> tuple[str | None, str | None]:
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
        jurisdiction = jurisdiction_match.group(1).strip() if jurisdiction_match else None

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
        jurisdiction_key = cls._slug(jurisdiction or "unknown-jurisdiction") or "unknown-jurisdiction"
        partition_dir = documents_dir / "by_jurisdiction" / state_key / jurisdiction_key
        return state_key, jurisdiction_key, partition_dir

    @classmethod
    def _resolve_partition_dir(
        cls,
        *,
        documents_dir: Path,
        url: str,
        request: AcquisitionRequest,
    ) -> tuple[str, dict[str, str], Path]:
        requested_mode = (request.partition_mode or "auto").strip().lower()
        mode = requested_mode if requested_mode in {"auto", "jurisdiction", "host"} else "auto"

        inferred_jurisdiction, inferred_state = cls._infer_jurisdiction_from_query(request.query)
        jurisdiction = request.jurisdiction or inferred_jurisdiction
        state = request.state or inferred_state

        if mode == "jurisdiction":
            state_key, jurisdiction_key, partition_dir = cls._jurisdiction_partition_dir(
                documents_dir,
                state=state,
                jurisdiction=jurisdiction,
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
            host_key, partition_dir = cls._host_partition_dir(documents_dir, url)
            return ("host", {"source_host": host_key}, partition_dir)

        # auto mode: use jurisdiction partition when both hints exist; fallback to host.
        if jurisdiction and state:
            state_key, jurisdiction_key, partition_dir = cls._jurisdiction_partition_dir(
                documents_dir,
                state=state,
                jurisdiction=jurisdiction,
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
        request: AcquisitionRequest,
        candidates: list[AcquisitionCandidate],
        documents_dir: Path,
        max_downloads: int = 10,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
        """Download supported candidate files for non-dry acquisition runs."""
        try:
            import requests
        except ImportError as exc:
            error = build_error_record(
                RuntimeError("requests dependency is required for acquisition downloads"),
                stage="acquisition.download",
                provider="http",
            )
            return [], [error], [f"Download stage unavailable: {exc}"]

        ssl_verify = self._downloads_ssl_verify()
        retry_policy = self._resolve_retry_policy(request)
        downloads: list[dict[str, object]] = []
        errors: list[dict[str, object]] = []
        downloaded_count = 0
        max_concurrent_downloads = max(1, int(request.max_concurrent_downloads))
        rate_limiter = _RequestRateLimiter(request.min_request_interval_ms)
        staged_candidates = list(candidates[:max_downloads])

        futures = []
        with ThreadPoolExecutor(max_workers=max_concurrent_downloads) as executor:
            for idx, candidate in enumerate(staged_candidates, start=1):
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

            completed: list[tuple[int, dict[str, object], dict[str, object] | None, bool]] = []
            for future in as_completed(futures):
                completed.append(future.result())

        for _, download_record, maybe_error, was_downloaded in sorted(completed, key=lambda item: item[0]):
            downloads.append(download_record)
            if maybe_error is not None:
                errors.append(maybe_error)
            if was_downloaded:
                downloaded_count += 1

        notes = [
            f"Download stage completed: {downloaded_count} file(s) saved from {len(candidates)} candidate(s)."
        ]
        if not ssl_verify:
            notes.append("Download SSL verification disabled via ACQUISITION_SSL_VERIFY/STREAMLINE_EXTRACT_SSL_VERIFY.")
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
        return downloads, normalize_error_records(errors), notes

    @staticmethod
    def _copy_candidate_with_reason(
        candidate: AcquisitionCandidate,
        reason: str,
    ) -> AcquisitionCandidate:
        reasons = list(candidate.reasons)
        if reason not in reasons:
            reasons.append(reason)
        return AcquisitionCandidate(
            url=candidate.url,
            source=candidate.source,
            score=candidate.score,
            reasons=reasons,
            status=candidate.status,
            mime_type=candidate.mime_type,
            extension=candidate.extension,
            canonical_url=candidate.canonical_url,
            content_hash=candidate.content_hash,
        )

    @staticmethod
    def _build_constraints(request: AcquisitionRequest) -> dict[str, object]:
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
            constraints["include_link_text_patterns"] = request.include_link_text_patterns
        if request.index_page_mode:
            constraints["index_page_mode"] = request.index_page_mode
        if request.index_links is not None:
            constraints["index_link_count"] = len(request.index_links)

        for field_name in ("max_depth", "max_pages", "max_files", "timeout_seconds"):
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
        if request.targets:
            constraints["target_count"] = len(request.targets)
        if request.use_query_family:
            constraints["use_query_family"] = request.use_query_family
        if request.query_templates:
            constraints["query_template_count"] = len(request.query_templates)
        if request.query_families:
            constraints["query_family_count"] = len(request.query_families)

        return constraints

    def _route_distributed_candidates(
        self,
        *,
        request: AcquisitionRequest,
        candidates: list[AcquisitionCandidate],
    ) -> tuple[list[AcquisitionCandidate], list[dict[str, object]], list[str], dict[str, object]]:
        notes: list[str] = []
        errors: list[dict[str, object]] = []
        routing_state: dict[str, object] = {
            "mode": "distributed",
            "applied": False,
            "connector": request.digger_provider,
        }

        if not candidates:
            notes.append("Distributed routing skipped because no candidates were available.")
            return candidates, errors, notes, routing_state

        try:
            connector = resolve_digger_connector(request.digger_provider)
            artifacts = connector.discover(
                DiggerInput(
                    seed_urls=[candidate.url for candidate in candidates],
                    max_depth=request.max_depth or 2,
                    max_pages=request.max_pages or 50,
                    max_files=request.max_files or 20,
                    timeout_seconds=request.timeout_seconds or 30,
                    allowed_domains=request.allowed_domains,
                    include_url_patterns=request.include_url_patterns,
                    include_link_text_patterns=request.include_link_text_patterns,
                    extra_params={
                        "ssl_verify": self._downloads_ssl_verify(),
                        "retry": self._resolve_retry_policy(request),
                    },
                )
            )
        except Exception as exc:
            errors.append(
                build_error_record(
                    exc,
                    stage="acquisition.routing",
                    provider=request.digger_provider,
                )
            )
            notes.append("Distributed routing failed; falling back to unrouted candidates.")
            return candidates, normalize_error_records(errors), notes, routing_state

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
            notes.append("Distributed routing produced no digger artifacts; falling back to unrouted candidates.")
            return candidates, normalize_error_records(errors), notes, routing_state

        candidate_by_url: dict[str, AcquisitionCandidate] = {}
        for candidate in candidates:
            candidate_by_url.setdefault(candidate.url, candidate)

        routed_candidates: list[AcquisitionCandidate] = []
        seen_urls: set[str] = set()
        route_reason = "Routed via distributed digger path"
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

            routed_candidates.append(
                AcquisitionCandidate(
                    url=artifact.url,
                    source=artifact.source,
                    score=CandidateScore(url_signal=0.35, trust_signal=0.55),
                    reasons=[route_reason],
                    mime_type=artifact.mime_type,
                    extension=artifact.extension,
                )
            )

        notes.append(
            f"Distributed routing staged {len(routed_candidates)} candidate(s) through digger."
        )
        return routed_candidates, normalize_error_records(errors), notes, routing_state

    def _route_centralized_candidates(
        self,
        *,
        request: AcquisitionRequest,
        candidates: list[AcquisitionCandidate],
    ) -> tuple[list[AcquisitionCandidate], list[dict[str, object]], list[str], dict[str, object]]:
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
            notes.append("Centralized routing skipped because no hub pages or candidates were available.")
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
                    seed_urls=seed_urls,
                    max_depth=request.max_depth or 2,
                    max_pages=request.max_pages or 50,
                    max_files=request.max_files or 20,
                    timeout_seconds=request.timeout_seconds or 30,
                    allowed_domains=request.allowed_domains,
                    include_url_patterns=request.include_url_patterns,
                    include_link_text_patterns=request.include_link_text_patterns,
                    extra_params={
                        **extra_params,
                        "ssl_verify": self._downloads_ssl_verify(),
                        "retry": self._resolve_retry_policy(request),
                    },
                )
            )
        except Exception as exc:
            errors.append(
                build_error_record(
                    exc,
                    stage="acquisition.routing",
                    provider=request.digger_provider,
                )
            )
            notes.append("Centralized routing failed; falling back to unrouted candidates.")
            return candidates, normalize_error_records(errors), notes, routing_state

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
            notes.append("Centralized routing produced no sweep artifacts; falling back to unrouted candidates.")
            return candidates, normalize_error_records(errors), notes, routing_state

        candidate_by_url: dict[str, AcquisitionCandidate] = {}
        for candidate in candidates:
            candidate_by_url.setdefault(candidate.url, candidate)

        routed_candidates: list[AcquisitionCandidate] = []
        seen_urls: set[str] = set()
        route_reason = "Routed via centralized hub sweep"
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

            routed_candidates.append(
                AcquisitionCandidate(
                    url=artifact.url,
                    source=artifact.source,
                    score=CandidateScore(url_signal=0.35, trust_signal=0.55),
                    reasons=[route_reason],
                    mime_type=artifact.mime_type,
                    extension=artifact.extension,
                )
            )

        notes.append(
            f"Centralized routing staged {len(routed_candidates)} candidate(s) through hub sweep."
        )
        return routed_candidates, normalize_error_records(errors), notes, routing_state

    @staticmethod
    def _dedupe_candidates_by_url(
        candidates: list[AcquisitionCandidate],
    ) -> list[AcquisitionCandidate]:
        deduped: list[AcquisitionCandidate] = []
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
        request: AcquisitionRequest,
        candidates: list[AcquisitionCandidate],
    ) -> tuple[list[AcquisitionCandidate], list[dict[str, object]], list[str], dict[str, object]]:
        notes: list[str] = []
        errors: list[dict[str, object]] = []
        routing_state: dict[str, object] = {
            "mode": "hybrid",
            "applied": True,
            "stages": [],
        }

        centralized_candidates, centralized_errors, centralized_notes, centralized_state = (
            self._route_centralized_candidates(
                request=request,
                candidates=candidates,
            )
        )
        errors.extend(centralized_errors)
        notes.extend(centralized_notes)
        routing_state["stages"].append({"centralized": centralized_state})

        centralized_routed_candidates = centralized_candidates
        if int(centralized_state.get("artifact_count") or 0) == 0:
            centralized_routed_candidates = []

        centralized_urls = {candidate.url for candidate in centralized_routed_candidates}
        fallback_candidates = [
            candidate for candidate in candidates
            if candidate.url not in centralized_urls
        ]

        distributed_candidates, distributed_errors, distributed_notes, distributed_state = (
            self._route_distributed_candidates(
                request=request,
                candidates=fallback_candidates,
            )
        )
        errors.extend(distributed_errors)
        notes.extend(distributed_notes)
        routing_state["stages"].append({"distributed": distributed_state})

        distributed_routed_candidates = distributed_candidates
        if int(distributed_state.get("artifact_count") or 0) == 0:
            distributed_routed_candidates = []

        routing_state["centralized_candidate_count"] = len(centralized_routed_candidates)
        routing_state["distributed_candidate_count"] = len(distributed_routed_candidates)

        combined_candidates = self._dedupe_candidates_by_url(
            [*centralized_routed_candidates, *distributed_routed_candidates]
        )
        routing_state["final_candidate_count"] = len(combined_candidates)

        if combined_candidates:
            notes.append(
                f"Hybrid routing produced {len(combined_candidates)} candidate(s) after centralized-plus-distributed sequencing."
            )
            return combined_candidates, normalize_error_records(errors), notes, routing_state

        notes.append("Hybrid routing produced no routed candidates; falling back to unrouted candidates.")
        return candidates, normalize_error_records(errors), notes, routing_state

    @staticmethod
    def _write_download_index(
        *,
        request: AcquisitionRequest,
        run_id: str,
        manifest_path: Path,
        download_records: list[dict[str, object]],
    ) -> Path:
        """Write run-level download index CSV for downstream automation."""
        index_path = manifest_path.parent / "download_index.csv"
        fieldnames = [
            "run_id",
            "domain",
            "partition_mode",
            "source_state",
            "source_jurisdiction",
            "source_host",
            "status",
            "url",
            "final_url",
            "mime_type",
            "bytes",
            "relative_path",
            "path",
            "error",
        ]

        with index_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for record in download_records:
                writer.writerow(
                    {
                        "run_id": run_id,
                        "domain": request.domain,
                        "partition_mode": record.get("partition_mode"),
                        "source_state": record.get("source_state"),
                        "source_jurisdiction": record.get("source_jurisdiction"),
                        "source_host": record.get("source_host"),
                        "status": record.get("status"),
                        "url": record.get("url"),
                        "final_url": record.get("final_url"),
                        "mime_type": record.get("mime_type"),
                        "bytes": record.get("bytes"),
                        "relative_path": record.get("relative_path"),
                        "path": record.get("path"),
                        "error": record.get("error"),
                    }
                )
        return index_path

    def run(self, request: AcquisitionRequest) -> AcquisitionResult:
        started_at = datetime.now(timezone.utc)
        run_id = self._build_run_id(request, started_at)
        documents_dir, manifest_path = self._resolve_output_paths(request, run_id)
        normalized_seed_urls, seed_errors = self._normalize_seed_urls(request.seed_urls)
        seeker_state, seeker_errors, seeker_notes = self._resolve_serpapi_state(request)
        all_errors = normalize_error_records([*seed_errors, *seeker_errors])

        # Run seeker discovery when SerpApi is enabled and healthy (no init errors).
        if seeker_errors:
            seeker_candidates: list[AcquisitionCandidate] = []
        else:
            seeker_candidates, seeker_notes, all_errors = self._run_seeker(
                request, seeker_state, seeker_notes, all_errors
            )

        # Prefer seeker candidates; fall back to scaffold seeds if seeker yields nothing.
        if seeker_candidates:
            candidates = seeker_candidates
        else:
            candidates = self._scaffold_candidates(normalized_seed_urls)

        routing_notes: list[str] = []
        routing_state: dict[str, object] = {
            "mode": request.topology_mode or "default",
            "applied": False,
        }
        if request.topology_mode == "distributed":
            candidates, routing_errors, routing_notes, routing_state = self._route_distributed_candidates(
                request=request,
                candidates=candidates,
            )
            all_errors = normalize_error_records([*all_errors, *routing_errors])
        elif request.topology_mode == "centralized":
            candidates, routing_errors, routing_notes, routing_state = self._route_centralized_candidates(
                request=request,
                candidates=candidates,
            )
            all_errors = normalize_error_records([*all_errors, *routing_errors])
        elif request.topology_mode == "hybrid":
            candidates, routing_errors, routing_notes, routing_state = self._route_hybrid_candidates(
                request=request,
                candidates=candidates,
            )
            all_errors = normalize_error_records([*all_errors, *routing_errors])

        documents_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        download_records: list[dict[str, object]] = []
        download_index_path: Path | None = None
        download_notes: list[str] = []
        if not request.dry_run and candidates:
            download_records, download_errors, download_notes = self._download_candidates(
                request=request,
                candidates=candidates,
                documents_dir=documents_dir,
            )
            all_errors = normalize_error_records([*all_errors, *download_errors])
            download_index_path = self._write_download_index(
                request=request,
                run_id=run_id,
                manifest_path=manifest_path,
                download_records=download_records,
            )
            download_notes.append(
                f"Download index written: {download_index_path.as_posix()}"
            )

        base_status = "scaffold_dry_run" if request.dry_run else "scaffold"
        status = f"{base_status}_with_errors" if all_errors else base_status

        manifest = AcquisitionManifest(
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
            lineage={
                "run_id": run_id,
                "documents_dir": documents_dir.as_posix(),
                "manifest_path": manifest_path.as_posix(),
                "seeker": seeker_state,
                "routing": routing_state,
                "download_index_csv": (
                    download_index_path.as_posix()
                    if download_index_path is not None
                    else None
                ),
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

        return AcquisitionResult(
            run_id=run_id,
            manifest_path=manifest_path,
            documents_dir=documents_dir,
            dry_run=request.dry_run,
            download_index_path=download_index_path,
        )
