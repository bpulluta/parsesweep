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
    "what counts — do not impose outside assumptions about document type."
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
        max_chars: int = 8000,
    ) -> None:
        self._description = document_description
        self._model = model
        self._models = models
        self._default_model = default_model
        self._keep_top = max(1, int(keep_top))
        self._action = action if action in {"move", "flag"} else "move"
        self._max_chars = max_chars
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

    def _grade(
        self, file_path: str, target_context: str = ""
    ) -> dict[str, Any] | None:
        """Return the LLM grade for one file, or None if it can't be graded."""
        try:
            text = ContentSampler.extract_text(file_path)
        except Exception:  # noqa: BLE001 - unreadable file, skip grading
            return None
        if not text or len(text.strip()) < ContentSampler.MIN_EXTRACTION_LENGTH:
            return None

        sample = text[: self._max_chars]
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

            for record in records:
                path = str(record.get("path"))
                # Cost saver: reuse a prior grade when the file + description +
                # model are unchanged, so re-runs skip the per-file LLM call.
                grade = self._read_cached_grade(path)
                if grade is not None:
                    cached += 1
                    record["review_cached"] = True
                else:
                    grade = self._grade(path, target_context=target_context)
                    if grade is None:
                        continue
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
            for rank, record in enumerate(ranked):
                keep = rank < self._keep_top and bool(
                    record.get("review_is_primary")
                )
                record["review_selected"] = keep
                if keep:
                    selected += 1
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
