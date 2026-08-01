"""LLM-based post-download document review (curation).

The keyword classifier flags obvious mismatches cheaply. This optional step
goes further: it asks an LLM to grade each downloaded file against a plain
description of the document you actually want, then promotes the most relevant
file(s) into a ``reviewed/`` subfolder next to the originals.

The point is county-scale automation without human review: after a run over
thousands of jurisdictions, a person only opens the ``reviewed/`` folders —
presentations, drafts, superseded versions, and tangential reports stay out.

Reuses the extraction LLM client (``extraction/llm_client.py``) and the
``ContentSampler`` text extractor, so it adds no new dependencies. It is
best-effort: if the LLM is unavailable it leaves files untouched and says so.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .validators import ContentSampler

REVIEW_CACHE_DIRNAME = ".review"

_REVIEW_SYSTEM = (
    "You review candidate documents and decide which one best matches the "
    "TARGET DOCUMENT description the caller provides. Judge each document "
    "strictly against that description: a document is primary only when it IS "
    "the described document, not merely related to it. The description defines "
    "what counts — do not impose outside assumptions about document type. "
    "Return your assessment as a JSON object."
)

_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_primary": {"type": "boolean"},
        "relevance": {"type": "number"},
        "doc_kind": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["is_primary", "relevance", "doc_kind", "reason"],
}

_DEDUP_SYSTEM = (
    "You compare multiple documents discovered for the same jurisdiction and "
    "identify which are redundant — meaning they cover the same regulatory "
    "content as another document in the set but are an older, superseded, or "
    "less authoritative version of it. Your goal: ensure the curated set has "
    "maximum unique regulatory content with no version redundancy. "
    "Documents covering genuinely different regulatory provisions (e.g., "
    "different sections, different topics, complementary rules) are NOT "
    "redundant even if they are from the same jurisdiction. "
    "Return your assessment as a JSON object."
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
                        "description": "0-based index of the document to KEEP (most current/authoritative).",
                    },
                    "redundant_indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "0-based indices of documents to EXCLUDE (superseded/redundant).",
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
        action: str = "move",
        max_chars: int = 12000,
        review_keywords: list[str] | None = None,
        deduplicate_redundant: bool = True,
    ) -> None:
        self._description = document_description
        self._model = model
        self._models = models
        self._default_model = default_model
        self._keep_top = max(1, int(keep_top))
        self._action = action if action in {"move", "flag"} else "move"
        self._max_chars = max_chars
        self._review_keywords = [
            k.lower() for k in (review_keywords or []) if k and k.strip()
        ]
        self._deduplicate_redundant = bool(deduplicate_redundant)
        self._client: Any = None
        # Cost accounting
        self._total_cost: float = 0.0
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._llm_calls: int = 0

    def _ensure_client(self) -> Any:
        if self._client is None:
            # Route through the shared model-tiering resolver: honors the
            # ``models:`` block + the stage ``model`` (tier or literal), and
            # works for any provider (Azure/OpenAI/Anthropic/Gemini).
            from ..extraction.llm_factory import build_llm_client

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
            window_size = keyword_budget // max(len(self._review_keywords), 2)

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
        parts.append(body_section)
        parts.append(tail)
        combined = separator.join(parts)
        return combined[: self._max_chars]

    def _grade(
        self, file_path: str, target_context: str = ""
    ) -> dict[str, Any] | None:
        """Return the LLM grade for one file, or None if it can't be graded."""
        try:
            # Use full document extraction (with caching) instead of the
            # 5-page sample. This ensures the keyword-targeted sampling can
            # find relevant content deep in large multi-section documents.
            from pathlib import Path

            from ..extraction.document_utils import extract_text_from_document

            text = extract_text_from_document(Path(file_path))
        except Exception:  # noqa: BLE001 - unreadable file, skip grading
            # Fall back to the sampler if full extraction fails
            try:
                text = ContentSampler.extract_text(file_path)
            except Exception:
                return None
        if not text or len(text.strip()) < ContentSampler.MIN_EXTRACTION_LENGTH:
            return None

        sample = self._build_review_sample(text)
        target_line = (
            f"\nSEARCH TARGET CONTEXT: {target_context}\n"
            if target_context
            else ""
        )
        user_prompt = (
            f"TARGET DOCUMENT: {self._description}\n"
            f"{target_line}\n"
            "Grade the document excerpt below against the target description. "
            "Return JSON with: is_primary (bool — true only if this IS the "
            "target document type AND is specifically relevant to the search "
            "target context above), relevance (0.0-1.0), doc_kind (a short "
            "free-form label describing what this document actually is), and "
            "a one-sentence reason.\n\n"
            f"EXCERPT:\n{sample}"
        )
        try:
            result = self._client.extract(
                text=sample,
                schema=_REVIEW_SCHEMA,
                system_prompt=_REVIEW_SYSTEM,
                user_prompt=user_prompt,
            )
        except Exception:  # noqa: BLE001 - one bad file must not abort review
            return None
        # Track cost from the LLM response
        if isinstance(result, dict):
            self._total_cost += result.get("cost", 0.0) or 0.0
            self._total_input_tokens += result.get("input_tokens", 0) or 0
            self._total_output_tokens += result.get("output_tokens", 0) or 0
            self._llm_calls += 1
        return result.get("data") if isinstance(result, dict) else None

    # -- grade cache -------------------------------------------------------

    def _cache_key(self, file_path: str) -> str:
        """Cache key = file size+mtime + the description/model that graded it.

        A re-run reuses a stored grade only when the file is byte-identical
        (size+mtime) AND the target description/model are unchanged, so editing
        the ``document_description`` or switching models re-grades correctly.
        """
        try:
            st = Path(file_path).stat()
            stat_part = f"{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            stat_part = "0:0"
        digest = hashlib.sha256(
            f"{self._description}\x00{self._model or ''}".encode()
        ).hexdigest()[:16]
        return f"{stat_part}:{digest}"

    def _read_cached_grade(self, file_path: str) -> dict[str, Any] | None:
        """Return a stored LLM grade when the cache key still matches."""
        src = Path(file_path)
        sidecar = src.parent / REVIEW_CACHE_DIRNAME / f"{src.stem}.json"
        if not sidecar.exists():
            return None
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if payload.get("cache_key") != self._cache_key(file_path):
            return None
        llm = payload.get("llm")
        if not isinstance(llm, dict) or llm.get("relevance") is None:
            return None
        return llm

    def _find_redundant_docs(
        self, primaries: list[dict[str, Any]], target_context: str
    ) -> set[int]:
        """Comparative pass: identify redundant docs among confirmed primaries.

        Uses already-available metadata (filename, URL, doc_kind, review_reason)
        to detect version overlap without re-reading any document. Returns the
        0-based indices (into ``primaries``) that should be excluded as
        superseded/redundant.

        Designed to be called only when len(primaries) >= 2. Fails silently:
        any error returns an empty set so all primaries are kept.
        """
        if len(primaries) < 2:
            return set()

        # Build a numbered list of doc metadata for the LLM.
        lines: list[str] = [
            f"Jurisdiction context: {target_context}\n",
            "Documents to compare (0-based index):\n",
        ]
        for i, rec in enumerate(primaries):
            path = str(rec.get("path") or "")
            filename = Path(path).name if path else f"doc_{i}"
            url = str(rec.get("url") or rec.get("final_url") or "unknown")
            doc_kind = str(rec.get("review_doc_kind") or "unknown")
            reason = str(rec.get("review_reason") or "")
            lines.append(
                f"[{i}] File: {filename}\n"
                f"    Source: {url}\n"
                f"    Kind: {doc_kind}\n"
                f"    Assessment: {reason}\n"
            )

        user_prompt = (
            "".join(lines)
            + "\nFor each group of documents that cover the same regulatory content "
            "where one supersedes another (older version, less authoritative source, "
            "or duplicate encoding of the same ordinance), return a redundancy_groups "
            "entry specifying the keep_index (most current/authoritative) and the "
            "redundant_indices to exclude. "
            "Documents covering genuinely different regulatory content are NOT "
            "redundant — return empty redundancy_groups if all docs are complementary. "
            "Return your answer as a JSON object."
        )

        try:
            result = self._client.extract(
                text=user_prompt,
                schema=_DEDUP_SCHEMA,
                system_prompt=_DEDUP_SYSTEM,
                user_prompt=user_prompt,
            )
        except Exception:  # noqa: BLE001 - dedup is best-effort
            return set()

        if isinstance(result, dict):
            self._total_cost += result.get("cost", 0.0) or 0.0
            self._total_input_tokens += result.get("input_tokens", 0) or 0
            self._total_output_tokens += result.get("output_tokens", 0) or 0
            self._llm_calls += 1

        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict):
            return set()

        redundant: set[int] = set()
        n = len(primaries)
        for group in data.get("redundancy_groups") or []:
            if not isinstance(group, dict):
                continue
            keep = group.get("keep_index")
            to_drop = group.get("redundant_indices") or []
            # Validate all indices are in-bounds and keep is not in to_drop.
            if not isinstance(keep, int) or keep < 0 or keep >= n:
                continue
            valid_drop = {
                idx for idx in to_drop
                if isinstance(idx, int) and 0 <= idx < n and idx != keep
            }
            redundant |= valid_drop

        return redundant

    def review(
        self,
        downloads: list[dict[str, Any]],
        notes: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Grade downloaded files per target and promote the top one(s).

        Annotates each record with ``review_*`` fields. In ``move`` mode the
        promoted file(s) are relocated to a ``reviewed/`` subfolder and their
        ``path``/``relative_path`` updated; in ``flag`` mode files stay put.
        """
        gradable = [
            r
            for r in downloads
            if r.get("status") == "downloaded" and r.get("path")
        ]
        if not gradable:
            return downloads, notes

        try:
            self._ensure_client()
        except Exception as exc:  # noqa: BLE001 - no LLM configured
            notes.append(
                f"Document review skipped (LLM unavailable: {exc})."
            )
            return downloads, notes

        # Group by originating target so promotion is per-jurisdiction.
        groups: dict[str, list[dict[str, Any]]] = {}
        for record in gradable:
            meta = record.get("target_metadata") or {}
            key = str(meta.get("label") or record.get("target_label") or "_")
            groups.setdefault(key, []).append(record)

        selected = 0
        graded = 0
        cached = 0
        for target_key, records in groups.items():
            # Build target context string from first record's metadata.
            # Passes all user-defined target fields so the LLM can assess
            # whether the document is relevant for THIS specific target.
            _meta = (records[0].get("target_metadata") or {}) if records else {}
            _ctx_parts = [
                f"{k}={v}" for k, v in _meta.items()
                if v and k != "query"  # exclude the search query itself
            ]
            target_context = ", ".join(_ctx_parts) if _ctx_parts else target_key

            # Grade files concurrently (4 threads) for speed.
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _grade_record(record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
                path = str(record.get("path"))
                grade = self._read_cached_grade(path)
                was_cached = grade is not None
                if grade is None:
                    grade = self._grade(path, target_context=target_context)
                return record, grade, was_cached

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(_grade_record, r) for r in records]
                for future in as_completed(futures):
                    record, grade, was_cached = future.result()
                    if grade is None:
                        continue
                    if was_cached:
                        cached += 1
                        record["review_cached"] = True
                    else:
                        graded += 1
                    record["review_is_primary"] = bool(grade.get("is_primary"))
                    record["review_relevance"] = grade.get("relevance")
                    record["review_doc_kind"] = grade.get("doc_kind")
                    record["review_reason"] = grade.get("reason")

            ranked = sorted(
                (r for r in records if "review_relevance" in r),
                key=lambda r: (
                    bool(r.get("review_is_primary")),
                    float(r.get("review_relevance") or 0.0),
                ),
                reverse=True,
            )

            # Comparative dedup: identify redundant docs among the confirmed
            # primaries BEFORE applying keep_top, so keep_top reflects the
            # desired number of genuinely distinct/complementary documents.
            redundant_indices: set[int] = set()
            if self._deduplicate_redundant:
                primaries = [
                    r for r in ranked if bool(r.get("review_is_primary"))
                ]
                if len(primaries) >= 2:
                    redundant_path_set = {
                        str(primaries[i].get("path"))
                        for i in self._find_redundant_docs(primaries, target_context)
                    }
                    if redundant_path_set:
                        notes.append(
                            f"Dedup ({target_key}): excluded "
                            f"{len(redundant_path_set)} redundant doc(s)."
                        )
                    # Mark redundant records so keep_top skips them.
                    for r in ranked:
                        if str(r.get("path")) in redundant_path_set:
                            r["review_redundant"] = True

            non_redundant_rank = 0
            for record in ranked:
                is_redundant = bool(record.get("review_redundant"))
                if is_redundant:
                    record["review_selected"] = False
                else:
                    keep = non_redundant_rank < self._keep_top and bool(
                        record.get("review_is_primary")
                    )
                    record["review_selected"] = keep
                    if keep:
                        selected += 1
                    non_redundant_rank += 1
                # Files are NOT moved: everything stays in place under
                # documents/ and the engine materializes the selected set into
                # curated/. A per-file sidecar records the verdict so a human
                # can see the reasoning (and override) in context.
                self._write_sidecar(record)

        notes.append(
            f"Document review: graded {graded} file(s) "
            f"({cached} reused from cache), "
            f"selected {selected} primary document(s) "
            f"(keep_top={self._keep_top})."
        )
        if self._total_cost > 0:
            notes.append(
                f"Document review cost: ${self._total_cost:.4f} "
                f"({self._llm_calls} LLM call(s), "
                f"{self._total_input_tokens + self._total_output_tokens:,} tokens)."
            )
        return downloads, notes

    def get_costs(self) -> dict[str, Any]:
        """Return accumulated cost data from document review LLM calls."""
        return {
            "total_cost_usd": round(self._total_cost, 6),
            "llm_calls": self._llm_calls,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
        }

    def _write_sidecar(self, record: dict[str, Any]) -> None:
        """Write ``.review/<stem>.json`` next to a graded file (best-effort).

        Stores a ``cache_key`` so a later run can reuse the grade without a
        fresh LLM call, and preserves any existing ``human`` decision block.
        """
        if "review_relevance" not in record:
            return
        src = Path(str(record.get("path") or ""))
        if not src.name:
            return

        review_dir = src.parent / REVIEW_CACHE_DIRNAME
        sidecar = review_dir / f"{src.stem}.json"
        human_block: dict[str, Any] = {"decision": None, "notes": None}
        if sidecar.exists():
            try:
                prior = json.loads(sidecar.read_text(encoding="utf-8"))
                if isinstance(prior.get("human"), dict):
                    human_block = prior["human"]
            except (OSError, ValueError):
                pass

        payload = {
            "file": src.name,
            "cache_key": self._cache_key(str(src)),
            "llm": {
                "is_primary": record.get("review_is_primary"),
                "relevance": record.get("review_relevance"),
                "doc_kind": record.get("review_doc_kind"),
                "reason": record.get("review_reason"),
                "selected": record.get("review_selected"),
            },
            "human": human_block,
        }
        try:
            review_dir.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        except OSError:
            return
