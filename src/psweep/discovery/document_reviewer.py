"""LLM-based post-download document review and in-place selection.

Grade each downloaded file against the configured document description.
The engine copies selections into ``curated/``; originals stay put.
Raw grades are cached by content and grading inputs in a domain-level
directory, so identical documents are never re-graded across runs.

Reuse the extraction LLM client and document text extraction. Explicit
per-record failures let the caller block unsafe publication. Recordings
are graded on a cached transcript only; review never pays to transcribe,
so a recording without one stays selected for extraction to transcribe.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import islice
from pathlib import Path
from threading import Lock
from typing import Any

from psweep.extraction.transcription import TranscriptionError, is_audio
from psweep.security import provider_error_category
from psweep.utils.file_io import atomic_json
from .targets import target_identity
from .urls import redact_url, redact_urls_in_text
from .validators import ContentSampler
import contextlib

_REVIEW_WORKERS = 4
_GRADE_ATTEMPTS = 2
_GRADE_FIELDS = ("is_primary", "relevance", "doc_kind", "reason")


class ReviewError(Exception):
    """A per-file failure without sensitive provider error text.

    LLM/provider failures are retryable; unreadable content is terminal.
    """

    def __init__(
        self, message: str, *, usage_unknown: bool = False, retryable: bool = True
    ) -> None:
        self.usage_unknown = usage_unknown
        self.retryable = retryable
        super().__init__(message)


class UnreadableDocumentError(ReviewError):
    """No reviewable text; recorded as a terminal ``skipped_unreadable``."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class _NoTranscriptError(Exception):
    """A recording without a cached transcript: kept, ungraded."""


# Field name constants — use these everywhere instead of string literals so a
# typo is a NameError at import time rather than a silent runtime no-op.
FIELD_REVIEW_SELECTED = "review_selected"
FIELD_REVIEW_IS_PRIMARY = "review_is_primary"
FIELD_REVIEW_RELEVANCE = "review_relevance"
FIELD_REVIEW_REDUNDANT = "review_redundant"
FIELD_REVIEW_DOC_KIND = "review_doc_kind"
FIELD_REVIEW_REASON = "review_reason"
FIELD_REVIEW_CACHED = "review_cached"
RECORDING_KEPT = "recording kept without review (transcribed at extraction)"

_REVIEW_SYSTEM = (
    "You grade one candidate document against the TARGET DOCUMENT description "
    "the caller provides, judging only from the excerpt and source URL shown. "
    "The description defines what counts — do not impose outside assumptions "
    "about document type. Return your assessment as a JSON object."
)

# Anchored levels instead of a free-form score: re-grades of the same
# document land on the same level, so selection stays stable.
_TOP_GRADE = 4
_PRIMARY_GRADE = 3
_REVIEW_PROMPT = (
    "TARGET DOCUMENT: {description}\n"
    "{target_line}{source_line}\n"
    "Grade the excerpt below with exactly one level:\n"
    "4 = it IS the target document for the search target and fully matches "
    "the description, including any stated preferences.\n"
    "3 = it IS the target document for the search target, but is partial or "
    "less preferred by the description.\n"
    "2 = it matches the described document type or subject, but the excerpt "
    "and source URL do not show that it belongs to the search target.\n"
    "1 = it concerns the subject or the search target but is not the "
    "described document.\n"
    "0 = unrelated.\n"
    "Decide first whether the document belongs to the search target as the "
    "description defines it (without a SEARCH TARGET line, the description "
    "alone defines it). When that evidence is missing or ambiguous, choose "
    "the lower level.\n"
    "Return JSON with: doc_kind (a short free-form label for what this "
    "document actually is), reason (one sentence citing the deciding "
    "evidence), and grade (integer 0-4).\n\n"
    "EXCERPT:\n{sample}"
)

_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc_kind": {"type": "string"},
        "reason": {"type": "string"},
        "grade": {"type": "integer", "minimum": 0, "maximum": _TOP_GRADE},
    },
    "required": ["doc_kind", "reason", "grade"],
}

_DEDUP_SYSTEM = (
    "You compare multiple documents discovered for the same search target and "
    "identify which are redundant — meaning they cover the same content as "
    "another document in the set but are an older, superseded, or less "
    "authoritative version of it. Judge redundancy against the TARGET "
    "DOCUMENT description the caller provides; do not impose outside "
    "assumptions about document type. Your goal: ensure the curated set has "
    "maximum unique content with no version redundancy. Documents covering "
    "genuinely "
    "different material (e.g., different sections, different topics, "
    "complementary content) are NOT redundant even if they come from the same "
    "source. Return your assessment as a JSON object."
)

_DEDUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "redundancy_groups": {
            "type": "array",
            "description": (
                "Each group represents a set of documents where one supersedes "
                "the others. Only populate when genuine version redundancy "
                "exists. Leave empty if all documents cover distinct content."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "keep_index": {
                        "type": "integer",
                        "description": (
                            "0-based index of the document to KEEP (most current/authoritative)."
                        ),
                    },
                    "redundant_indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "0-based indices of documents to EXCLUDE (superseded/redundant)."
                        ),
                    },
                    "reason": {"type": "string"},
                },
                "required": ["keep_index", "redundant_indices", "reason"],
            },
        }
    },
    "required": ["redundancy_groups"],
}


class DocumentReviewer:
    """Grade downloaded files with an LLM and promote the primary one(s)."""

    def __init__(
        self,
        *,
        document_description: str,
        model: str | None = None,
        models: dict[str, str] | None = None,
        default_model: str | None = None,
        keep_top: int = 1,
        max_chars: int = 12000,
        review_keywords: list[str] | None = None,
        deduplicate_redundant: bool = True,
        dedup_key_fields: list[str] | None = None,
        cache_dir: Path | None = None,
        identity_fields: list[str] | None = None,
    ) -> None:
        self._description = document_description
        self._identity_fields = identity_fields
        self._cache_dir = cache_dir
        self._model = model
        self._models = models
        self._default_model = default_model
        self._keep_top = max(1, int(keep_top))
        if type(max_chars) is not int or max_chars <= 0:
            message = "max_chars must be a positive integer"
            raise ValueError(message)
        self._max_chars = max_chars
        self._review_keywords = [k.lower() for k in (review_keywords or []) if k and k.strip()]
        self._deduplicate_redundant = bool(deduplicate_redundant)
        self._dedup_key_fields = [
            field for field in (dedup_key_fields or []) if field and field.strip()
        ]
        self._client: Any = None
        # Cost accounting
        self._total_cost: float = 0.0
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._llm_calls: int = 0
        self._usage_unknown_calls: int = 0
        self._usage_lock = Lock()

    def _ensure_client(self) -> Any:
        if self._client is None:
            # Route through the shared model-tiering resolver: honors the
            # ``models:`` block + the stage ``model`` (tier or literal), and
            # works for any provider (Azure/OpenAI/Anthropic/Gemini).
            from psweep.extraction.llm_factory import build_llm_client

            self._client = build_llm_client(
                self._model,
                models=self._models,
                default_model=self._default_model,
            )
        return self._client

    def _build_review_sample(self, text: str) -> str:
        """Build a bounded text sample for LLM review from any document.

        Strategy: head + keyword-targeted windows + distributed body + tail.
        When review_keywords are configured, the sampler finds sections
        containing those keywords and includes them — ensuring relevant
        content is sampled even in long documents where it appears deep.

        For documents shorter than max_chars, returns full text.
        """
        if len(text) <= self._max_chars:
            return text

        separator = "\n[...]\n"
        n_body_windows = 4
        total_separator_count = 3 + (n_body_windows - 1)
        separator_budget = len(separator) * total_separator_count

        usable = self._max_chars - separator_budget
        if usable // 5 <= 0:
            # No room for all sections: avoid empty or negative windows.
            return text[: self._max_chars]
        head_budget = usable // 5
        tail_budget = usable // 5
        keyword_budget = usable // 5
        body_budget = usable - head_budget - tail_budget - keyword_budget

        head = text[:head_budget]
        tail = text[-tail_budget:]

        # Keyword-targeted sampling: find sections containing domain keywords
        keyword_section = ""
        if self._review_keywords:
            text_lower = text.lower()
            keyword_windows: list[str] = []
            seen_positions: set[int] = set()
            window_size = max(
                1,
                keyword_budget // max(len(self._review_keywords), 2),
            )

            for keyword in self._review_keywords:
                pos = text_lower.find(keyword, head_budget)
                while pos != -1 and pos < len(text) - tail_budget:
                    # Skip if too close to a window we already captured
                    if not any(abs(pos - s) < window_size for s in seen_positions):
                        start = max(head_budget, pos - window_size // 4)
                        excerpt = text[start : start + window_size]
                        if excerpt.strip():
                            keyword_windows.append(excerpt)
                            seen_positions.add(pos)
                        if len(keyword_windows) * window_size >= keyword_budget:
                            break
                    pos = text_lower.find(keyword, pos + window_size)
                if len(keyword_windows) * window_size >= keyword_budget:
                    break

            if keyword_windows:
                keyword_section = separator.join(keyword_windows)[:keyword_budget]
            else:
                # No keywords found — give budget back to body
                body_budget += keyword_budget

        # Distributed body windows through the middle
        body_start = head_budget
        body_end = len(text) - tail_budget
        body_length = body_end - body_start

        if body_length <= body_budget:
            body_section = text[body_start:body_end]
        else:
            window_size = body_budget // n_body_windows
            step = (body_length - window_size) // max(n_body_windows - 1, 1)
            excerpts: list[str] = []
            for i in range(n_body_windows):
                offset = body_start + (i * step)
                excerpt = text[offset : offset + window_size]
                if excerpt.strip():
                    excerpts.append(excerpt)
            body_section = separator.join(excerpts)

        parts = [head]
        if keyword_section:
            parts.append(keyword_section)
        parts.extend((body_section, tail))
        combined = separator.join(parts)
        return combined[: self._max_chars]

    @staticmethod
    def _normalize_dedup_value(value: object) -> str:
        """Normalize a dedup field into a stable comparison token."""
        if value is None:
            return ""
        if isinstance(value, (dict, list, tuple, set)):
            value = json.dumps(value, sort_keys=True, separators=(",", ":"))
        text = str(value).casefold()
        return re.sub(r"[^a-z0-9]+", "", text)

    def _dedup_field_value(self, record: dict[str, Any], field: str) -> object:
        """Return the raw value for one configured dedup field."""
        if field == "path_basename":
            return Path(str(record.get("path") or "")).name
        if field == "path_stem":
            return Path(str(record.get("path") or "")).stem
        return record.get(field)

    def _deduplication_key(self, record: dict[str, Any]) -> str:
        """Build a stable dedup key from configured record fields."""
        parts = [
            self._normalize_dedup_value(self._dedup_field_value(record, field))
            for field in self._dedup_key_fields
        ]
        return "\x1f".join(parts)

    @staticmethod
    def _file_sha256(path: str) -> str | None:
        """Return SHA-256 hex digest for a file, or None if unreadable."""
        try:
            h = hashlib.sha256()
            with Path(path).open("rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    @staticmethod
    def _record_content_hash(record: dict[str, Any]) -> str | None:
        """Return the byte-identity SHA-256 for a curated record.

        The downloader already computes a SHA-256 of the streamed bytes and
        stores it as ``content_hash`` (engine ``_download_candidates``). Reuse
        it to avoid a second full-file read. It is only trustworthy when the
        on-disk bytes still match the download: browser escalation and code-host
        adapters both rewrite the file in place, so when ``browser_escalated``
        or ``code_host_adapter`` is set we re-hash the file. All paths use the
        identical SHA-256 algorithm, so the duplicates detected are unchanged.
        """
        stored = record.get("content_hash")
        if (
            isinstance(stored, str)
            and stored
            and not record.get("browser_escalated")
            and not record.get("code_host_adapter")
        ):
            return stored
        return DocumentReviewer._file_sha256(str(record.get("path", "")))

    @staticmethod
    def _find_byte_identical_duplicates(primaries: list[dict[str, Any]]) -> set[int]:
        """Return indices of byte-identical files, keeping the highest-ranked one.

        Two curated files that hash to the same SHA-256 are the same content
        (e.g. http vs https mirror, www vs non-www). Only one needs extraction.
        Files whose path is unreadable are skipped (never marked redundant).
        """
        hash_groups: dict[str, list[int]] = {}
        for idx, record in enumerate(primaries):
            digest = DocumentReviewer._record_content_hash(record)
            if digest is None:
                continue
            hash_groups.setdefault(digest, []).append(idx)

        redundant: set[int] = set()
        for indices in hash_groups.values():
            if len(indices) < 2:
                continue
            keep = max(
                indices,
                key=lambda i: (
                    float(primaries[i].get("review_relevance") or 0.0),
                    bool(primaries[i].get("review_is_primary")),
                    str(primaries[i].get("path") or ""),
                ),
            )
            redundant.update(i for i in indices if i != keep)
        return redundant

    def _find_structural_duplicates(self, primaries: list[dict[str, Any]]) -> set[int]:
        """Identify obvious duplicates from a configurable structural key."""
        groups: dict[str, list[int]] = {}
        for idx, record in enumerate(primaries):
            key = self._deduplication_key(record)
            if not key.strip("\x1f"):
                continue
            groups.setdefault(key, []).append(idx)

        redundant: set[int] = set()
        for indices in groups.values():
            if len(indices) < 2:
                continue
            keep = max(
                indices,
                key=lambda i: (
                    float(primaries[i].get("review_relevance") or 0.0),
                    bool(primaries[i].get("review_is_primary")),
                    str(primaries[i].get("path") or ""),
                ),
            )
            redundant.update(i for i in indices if i != keep)
        return redundant

    @staticmethod
    def _record_source_url(record: dict[str, Any]) -> str:
        """Actual download location: post-redirect ``final_url``, else ``url``.

        Returns "" when the record carries no URL; never invents one.
        """
        for field in ("final_url", "url"):
            value = str(record.get(field) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _valid_grade(grade: object) -> bool:
        """Require typed fields and a finite unit-interval score."""
        if not isinstance(grade, dict):
            return False
        relevance = grade.get("relevance")
        return (
            isinstance(grade.get("is_primary"), bool)
            and type(relevance) in {int, float}
            and math.isfinite(relevance)
            and 0 <= relevance <= 1
            and isinstance(grade.get("doc_kind"), str)
            and isinstance(grade.get("reason"), str)
        )

    @staticmethod
    def _grade_from_reply(reply: object) -> dict[str, Any] | None:
        """Map a valid anchored level to the stored grade, else None."""
        if not isinstance(reply, dict):
            return None
        level = reply.get("grade")
        if (
            type(level) is not int
            or not 0 <= level <= _TOP_GRADE
            or not isinstance(reply.get("doc_kind"), str)
            or not isinstance(reply.get("reason"), str)
        ):
            return None
        return {
            "is_primary": level >= _PRIMARY_GRADE,
            "relevance": level / _TOP_GRADE,
            "doc_kind": reply["doc_kind"],
            "reason": reply["reason"],
        }

    def _track_usage(self, result: object = None) -> None:
        """Accumulate returned numeric usage under the shared lock."""
        usage = result if isinstance(result, dict) else {}
        cost = usage.get("cost")
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        cost_known = type(cost) in {int, float} and math.isfinite(cost) and cost >= 0
        input_known = type(input_tokens) is int and input_tokens >= 0
        output_known = type(output_tokens) is int and output_tokens >= 0
        with self._usage_lock:
            self._llm_calls += 1
            self._usage_unknown_calls += not (cost_known and input_known and output_known)
            if cost_known:
                self._total_cost += cost
            if input_known:
                self._total_input_tokens += input_tokens
            if output_known:
                self._total_output_tokens += output_tokens

    @staticmethod
    def _mark_review_failure(
        record: dict[str, Any],
        error: ReviewError,
    ) -> None:
        """Flag a sanitized failure and deselect the affected file."""
        record[FIELD_REVIEW_SELECTED] = False
        record["review_failed"] = True
        record["review_error"] = str(error)
        record["review_retryable"] = error.retryable
        if error.usage_unknown:
            record["review_usage_unknown"] = True

    def _grade(
        self, file_path: str, target_context: str = "", source_url: str = ""
    ) -> dict[str, Any] | None:
        """Return a raw grade or raise a sanitized review failure."""
        try:
            # Full extraction reaches keyword matches beyond five pages.
            from psweep.extraction.document_utils import (
                extract_text_from_document,
            )

            text = extract_text_from_document(Path(file_path))
        except TranscriptionError as exc:
            raise _NoTranscriptError from exc
        except Exception:  # noqa: BLE001 - unreadable file, skip grading
            # Fall back to the sampler if full extraction fails
            try:
                text = ContentSampler.extract_text(file_path)
            except Exception as exc:
                message = "document text extraction failed"
                raise UnreadableDocumentError(message) from exc
        if not text or len(text.strip()) < ContentSampler.MIN_EXTRACTION_LENGTH:
            message = "insufficient extracted text for document review"
            raise UnreadableDocumentError(message)

        sample = self._build_review_sample(text)
        coverage = {
            "document_chars": len(text),
            "sampled_chars": len(sample),
            "truncated": len(text) > len(sample),
        }
        target_context = redact_urls_in_text(target_context)
        source_url = redact_url(source_url) if source_url else ""
        user_prompt = _REVIEW_PROMPT.format(
            description=self._description,
            target_line=(f"SEARCH TARGET: {target_context}\n" if target_context else ""),
            source_line=(
                f"SOURCE_URL: {source_url} (actual download location of this file)\n"
                if source_url
                else ""
            ),
            sample=sample,
        )
        for _ in range(_GRADE_ATTEMPTS):
            try:
                result = self._client.extract(
                    text=sample,
                    schema=_REVIEW_SCHEMA,
                    system_prompt=_REVIEW_SYSTEM,
                    user_prompt=user_prompt,
                )
            except Exception as exc:
                self._track_usage()
                message = (
                    f"LLM grade call failed: {provider_error_category(exc)} ({type(exc).__name__})"
                )
                raise ReviewError(message, usage_unknown=True) from exc
            # Retain returned usage even when the grade data is invalid.
            self._track_usage(result)
            grade = self._grade_from_reply(result.get("data") if isinstance(result, dict) else None)
            if grade is not None:
                return {**grade, "coverage": coverage}
        message = f"invalid LLM grade: expected integer level 0-{_TOP_GRADE} and strings"
        raise ReviewError(message)

    # Raw grade cache (selection is always recomputed).

    def _cache_key(self, file_path: str, source_url: str = "", target_context: str = "") -> str:
        """Identify raw grading inputs by content, excluding file location
        and selection controls.
        """
        from psweep.extraction.llm_factory import (
            resolve_llm_kwargs,
        )

        resolved = resolve_llm_kwargs(
            self._model,
            models=self._models,
            default_model=self._default_model,
        )
        resolved.pop("api_key", None)
        content_hash = self._file_sha256(file_path)
        if content_hash is None:
            message = "cannot read document content for grade cache identity"
            raise ReviewError(message, retryable=False)
        graded_inputs = json.dumps(
            {
                "content_sha256": content_hash,
                "description": self._description,
                "model": resolved,
                "source_url": redact_url(source_url),
                "target_context": redact_urls_in_text(target_context),
                "max_chars": self._max_chars,
                "keywords": self._review_keywords,
                "system": _REVIEW_SYSTEM,
                "prompt": _REVIEW_PROMPT,
                "schema": _REVIEW_SCHEMA,
                "grade_scale": [_PRIMARY_GRADE, _TOP_GRADE],
                "sampling": "head-keyword-body-tail-v2",
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(graded_inputs.encode()).hexdigest()

    def _read_cached_grade(self, cache_key: str) -> dict[str, Any] | None:
        """Return a valid stored grade for matching raw inputs."""
        if self._cache_dir is None:
            return None
        try:
            payload = json.loads(
                (self._cache_dir / f"{cache_key}.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or not self._valid_grade(payload.get("llm")):
            return None
        return {**payload["llm"], "coverage": payload.get("coverage", {})}

    def _write_cached_grade(self, cache_key: str, grade: dict[str, Any]) -> None:
        """Best-effort store of a fresh grade; a failed write only costs a regrade."""
        if self._cache_dir is None:
            return
        payload = {
            "llm": {field: grade[field] for field in _GRADE_FIELDS},
            "coverage": grade.get("coverage", {}),
        }
        with contextlib.suppress(OSError, TypeError, ValueError):
            atomic_json(self._cache_dir / f"{cache_key}.json", payload)

    def _find_redundant_docs(
        self, primaries: list[dict[str, Any]], target_context: str
    ) -> set[int]:
        """Identify redundant docs among confirmed primaries.

        Compare existing filename, URL, kind and assessment metadata.
        Return indices into ``primaries`` for documents to exclude.
        Provider failures flag survivors but preserve their raw grades.
        """
        if len(primaries) < 2:
            return set()

        # Build a numbered list of doc metadata for the LLM.
        lines: list[str] = [
            f"TARGET DOCUMENT: {self._description}\n",
            f"Target context: {redact_urls_in_text(target_context)}\n" if target_context else "",
            "Documents to compare (0-based index):\n",
        ]
        for i, rec in enumerate(primaries):
            path = str(rec.get("path") or "")
            filename = Path(path).name if path else f"doc_{i}"
            url = redact_url(self._record_source_url(rec)) or "unknown"
            doc_kind = str(rec.get("review_doc_kind") or "unknown")
            reason = str(rec.get("review_reason") or "")
            lines.append(
                f"[{i}] File: {filename}\n"
                f"    Source: {url}\n"
                f"    Kind: {doc_kind}\n"
                f"    Assessment: {reason}\n"
            )

        user_prompt = (
            "".join(lines) + "\nFor each group of documents that cover the same content "
            "where one supersedes another (older version, less authoritative "
            "source, or duplicate encoding of the same material), return a "
            "redundancy_groups entry specifying the keep_index (most "
            "current/authoritative) and the "
            "redundant_indices to exclude. "
            "Documents covering genuinely different content are NOT "
            "redundant — return empty redundancy_groups if all docs are "
            "complementary. "
            "Return your answer as a JSON object matching this schema:\n"
            + json.dumps(_DEDUP_SCHEMA, indent=2)
        )

        try:
            result = self._client.extract(
                text=user_prompt,
                schema=_DEDUP_SCHEMA,
                system_prompt=_DEDUP_SYSTEM,
                user_prompt=user_prompt,
            )
        except Exception as exc:  # noqa: BLE001 - no LLM configured
            self._track_usage()
            message = (
                f"LLM dedup call failed: {provider_error_category(exc)} ({type(exc).__name__})"
            )
            error = ReviewError(message, usage_unknown=True)
            for record in primaries:
                self._mark_review_failure(record, error)
            return set()

        self._track_usage(result)

        try:
            return self._redundant_indices(result, len(primaries))
        except ReviewError as exc:
            for record in primaries:
                self._mark_review_failure(record, exc)
            return set()

    @staticmethod
    def _redundant_indices(result: object, count: int) -> set[int]:
        """Validate a comparative response before using any indices."""
        data = result.get("data") if isinstance(result, dict) else None
        groups = data.get("redundancy_groups") if isinstance(data, dict) else None
        message = "invalid LLM dedup assessment"
        if not isinstance(groups, list):
            raise ReviewError(message)
        redundant: set[int] = set()
        for group in groups:
            if not isinstance(group, dict):
                raise ReviewError(message)
            keep = group.get("keep_index")
            to_drop = group.get("redundant_indices")
            if (
                type(keep) is not int
                or not 0 <= keep < count
                or not isinstance(to_drop, list)
                or not isinstance(group.get("reason"), str)
                or any(
                    type(idx) is not int or not 0 <= idx < count or idx == keep for idx in to_drop
                )
            ):
                raise ReviewError(message)
            redundant.update(to_drop)
        return redundant

    @classmethod
    def _require_grade(cls, grade: object) -> None:
        """Reject invalid or absent grades from all client adapters."""
        if not cls._valid_grade(grade):
            message = "invalid or unavailable document grade"
            raise ReviewError(message)

    def _grade_record(
        self,
        record: dict[str, Any],
        context: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
        """Isolate a per-file failure and validate before selection."""
        path = str(record.get("path"))
        source_url = self._record_source_url(record)
        record["review_target_context"] = context
        try:
            cache_key = self._cache_key(path, source_url, context)
            grade = self._read_cached_grade(cache_key)
            was_cached = grade is not None
            if grade is None:
                grade = self._grade(
                    path,
                    target_context=context,
                    source_url=source_url,
                )
                self._require_grade(grade)
                self._write_cached_grade(cache_key, grade)
        except _NoTranscriptError:
            record[FIELD_REVIEW_SELECTED] = True
            record[FIELD_REVIEW_REASON] = RECORDING_KEPT
            return record, None, False
        except UnreadableDocumentError as exc:
            record.update(status="skipped_unreadable", reason=str(exc))
            record[FIELD_REVIEW_SELECTED] = False
            return record, None, False
        except ReviewError as exc:
            self._mark_review_failure(record, exc)
            return record, None, False
        except Exception as exc:  # noqa: BLE001 - isolate record-level failures
            message = f"document grade failed ({type(exc).__name__})"
            self._mark_review_failure(record, ReviewError(message))
            return record, None, False
        return record, grade, was_cached

    def _iter_grades(
        self,
        records: list[dict[str, Any]],
        context: str,
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any] | None, bool]]:
        """Keep at most one four-worker window of futures pending."""
        remaining = iter(records)
        with ThreadPoolExecutor(max_workers=_REVIEW_WORKERS) as executor:
            pending = {
                executor.submit(self._grade_record, record, context)
                for record in islice(remaining, _REVIEW_WORKERS)
            }
            while pending:
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    yield future.result()
                pending.update(
                    executor.submit(self._grade_record, record, context)
                    for record in islice(remaining, len(completed))
                )

    def review(
        self,
        downloads: list[dict[str, Any]],
        notes: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Grade downloaded files per target and promote the top one(s).

        Annotates each record with ``review_*`` fields.
        Files stay in place; the engine materializes selected files separately.
        """
        gradable = [r for r in downloads if r.get("status") == "downloaded" and r.get("path")]
        if not gradable:
            return downloads, notes
        for record in gradable:
            for field in tuple(record):
                if field.startswith("review_"):
                    record.pop(field)
            record[FIELD_REVIEW_SELECTED] = False

        try:
            self._ensure_client()
        except Exception as exc:  # noqa: BLE001 - no LLM configured
            message = f"LLM unavailable ({type(exc).__name__})"
            for record in gradable:
                self._mark_review_failure(record, ReviewError(message))
            notes.append(f"Document review skipped ({message}).")
            return downloads, notes

        # Group by originating target so promotion is per-jurisdiction.
        groups: dict[str, list[dict[str, Any]]] = {}
        for record in gradable:
            # Selection/dedup are recomputed, not grade-cache inputs.
            record.pop(FIELD_REVIEW_REDUNDANT, None)
            record.pop("review_deduplication", None)
            key = str(record.get("target_id") or record.get("target_label") or "_")
            groups.setdefault(key, []).append(record)

        selected = 0
        graded = 0
        cached = 0
        for target_key, records in groups.items():
            # Only identity fields describe the target; passthrough columns
            # would add prompt noise and split the grade cache.
            identity = target_identity(
                records[0].get("target_metadata") or {}, self._identity_fields
            )
            target_context = (
                ", ".join(f"{key}={value}" for key, value in sorted(identity.items()) if value)
                or target_key
            )

            for record, grade, was_cached in self._iter_grades(records, target_context):
                if grade is None:
                    continue
                record[FIELD_REVIEW_CACHED] = was_cached
                record["review_grade"] = {field: grade[field] for field in _GRADE_FIELDS}
                record["review_coverage"] = grade.get("coverage", {})
                if was_cached:
                    cached += 1
                else:
                    graded += 1
                record[FIELD_REVIEW_IS_PRIMARY] = grade["is_primary"]
                record[FIELD_REVIEW_RELEVANCE] = grade["relevance"]
                record[FIELD_REVIEW_DOC_KIND] = grade["doc_kind"]
                record[FIELD_REVIEW_REASON] = grade["reason"]

            # Equal levels are common; break ties by provenance so
            # selection never depends on download order.
            ranked = sorted(
                (r for r in records if "review_relevance" in r),
                key=lambda r: (
                    not r.get(FIELD_REVIEW_IS_PRIMARY),
                    -float(r.get(FIELD_REVIEW_RELEVANCE) or 0.0),
                    self._record_source_url(r),
                    str(r.get("path") or ""),
                ),
            )

            # Comparative dedup: identify redundant docs among the confirmed
            # primaries BEFORE applying keep_top, so keep_top reflects the
            # desired number of genuinely distinct/complementary documents.
            # Each pass rebuilds `survivors` from the latest redundancy state so
            # disjoint keep decisions across passes cannot zero-out the set.
            if self._deduplicate_redundant:
                primaries = [r for r in ranked if bool(r.get(FIELD_REVIEW_IS_PRIMARY))]
                if len(primaries) >= 2:
                    # Pass 1: byte-identical files (mirrors, http/https variants).
                    byte_redundant = self._find_byte_identical_duplicates(primaries)
                    if byte_redundant:
                        notes.append(
                            f"Dedup ({target_key}): excluded "
                            f"{len(byte_redundant)} byte-identical duplicate(s)."
                        )
                        for i in byte_redundant:
                            primaries[i][FIELD_REVIEW_REDUNDANT] = True

                    # Rebuild survivors after pass 1 so pass 2 only sees
                    # non-redundant primaries (fixes: disjoint keep decisions).
                    survivors = [
                        r
                        for r in ranked
                        if bool(r.get(FIELD_REVIEW_IS_PRIMARY))
                        and not bool(r.get(FIELD_REVIEW_REDUNDANT))
                    ]

                    # Pass 2: structural key duplicates (configurable fields).
                    if len(survivors) >= 2 and self._dedup_key_fields:
                        structural_redundant = self._find_structural_duplicates(survivors)
                        if structural_redundant:
                            redundant_path_set = {
                                str(survivors[i].get("path")) for i in structural_redundant
                            }
                            notes.append(
                                f"Dedup ({target_key}): excluded "
                                f"{len(redundant_path_set)} structural duplicate(s)."
                            )
                            for r in ranked:
                                if str(r.get("path")) in redundant_path_set:
                                    r[FIELD_REVIEW_REDUNDANT] = True

                    # Rebuild survivors after pass 2 for the LLM pass.
                    survivors = [
                        r
                        for r in ranked
                        if bool(r.get(FIELD_REVIEW_IS_PRIMARY))
                        and not bool(r.get(FIELD_REVIEW_REDUNDANT))
                    ]
                    if len(survivors) >= 2:
                        redundant_path_set = {
                            str(survivors[i].get("path"))
                            for i in self._find_redundant_docs(survivors, target_context)
                        }
                        if redundant_path_set:
                            notes.append(
                                f"Dedup ({target_key}): excluded "
                                f"{len(redundant_path_set)} redundant doc(s)."
                            )
                            for r in ranked:
                                if str(r.get("path")) in redundant_path_set:
                                    r[FIELD_REVIEW_REDUNDANT] = True

                # Safety net: if every primary was marked redundant (disjoint
                # keep decisions across passes), un-mark the highest-ranked one
                # so keep_top always selects at least 1 primary per target.
                final_primaries = [r for r in ranked if bool(r.get(FIELD_REVIEW_IS_PRIMARY))]
                live_primaries = [
                    r for r in final_primaries if not bool(r.get(FIELD_REVIEW_REDUNDANT))
                ]
                if final_primaries and not live_primaries:
                    best = final_primaries[0]  # ranked is already sorted by relevance
                    best.pop(FIELD_REVIEW_REDUNDANT, None)
                    notes.append(
                        f"Dedup ({target_key}): safety-net un-marked 1 file "
                        f"— all primaries were marked redundant."
                    )

            non_redundant_rank = 0
            for record in ranked:
                is_redundant = bool(record.get(FIELD_REVIEW_REDUNDANT))
                if is_redundant or record.get("review_failed"):
                    record[FIELD_REVIEW_SELECTED] = False
                else:
                    keep = non_redundant_rank < self._keep_top and bool(
                        record.get(FIELD_REVIEW_IS_PRIMARY)
                    )
                    record[FIELD_REVIEW_SELECTED] = keep
                    if keep:
                        selected += 1
                    non_redundant_rank += 1

        notes.append(
            f"Document review: graded {graded} file(s) "
            f"({cached} reused from cache), "
            f"selected {selected} primary document(s) "
            f"(keep_top={self._keep_top})."
        )
        kept = sum(r.get(FIELD_REVIEW_REASON) == RECORDING_KEPT for r in gradable)
        if kept:
            notes.append(f"{kept} recording(s) kept without review (transcribed at extraction).")
        unreadable = sum(
            r.get("status") == "skipped_unreadable" and not is_audio(Path(r["path"]))
            for r in gradable
        )
        if unreadable:
            notes.append(
                f"{unreadable} document(s) had too little text to review; "
                "JS-rendered pages need discovery.crawl.browser_escalation."
            )
        if self._total_cost > 0:
            notes.append(
                f"Document review cost: ${self._total_cost:.4f} "
                f"({self._llm_calls} LLM call(s), "
                f"{self._total_input_tokens + self._total_output_tokens:,} tokens)."
            )
        return downloads, notes

    def get_costs(self) -> dict[str, Any]:
        """Return a synchronized snapshot of returned usage and cost."""
        with self._usage_lock:
            return {
                "total_cost_usd": round(self._total_cost, 6),
                "llm_calls": self._llm_calls,
                "total_input_tokens": self._total_input_tokens,
                "total_output_tokens": self._total_output_tokens,
                "usage_unknown_calls": self._usage_unknown_calls,
            }
