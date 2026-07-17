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

from pathlib import Path
from typing import Any

from .validators import ContentSampler

_REVIEW_SYSTEM = (
    "You review candidate documents and decide which is the authoritative "
    "PRIMARY document a researcher wants. Be strict: presentations, drafts, "
    "proposed/superseded versions, news items, meeting minutes, application "
    "packages, and tangential reports or studies are NOT primary."
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
        keep_top: int = 1,
        action: str = "move",
        max_chars: int = 8000,
    ) -> None:
        self._description = document_description
        self._model = model
        self._keep_top = max(1, int(keep_top))
        self._action = action if action in {"move", "flag"} else "move"
        self._max_chars = max_chars
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            import os

            from ..extraction.llm_client import LLMClient

            model = self._model
            kwargs: dict[str, Any] = {}
            # Mirror the extraction pipeline's provider resolution: default to
            # the configured Azure deployment when Azure env is present.
            azure_key = os.getenv("AZURE_OPENAI_API_KEY")
            azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
            if not model and azure_key and azure_endpoint:
                model = os.getenv("AZURE_OPENAI_MODEL") or "gpt-4o-mini"
                # api_key must be passed explicitly: LLMClient only wires up
                # AZURE_API_BASE/version when an api_key is supplied, otherwise
                # litellm sees api_base=None and fails.
                kwargs = {
                    "api_key": azure_key,
                    "provider": "azure",
                    "azure_endpoint": azure_endpoint,
                    "azure_api_version": os.getenv(
                        "AZURE_OPENAI_API_VERSION"
                    ),
                }
            else:
                kwargs = {"api_key": os.getenv("OPENAI_API_KEY")}
            self._client = LLMClient(model=model or "gpt-4o-mini", **kwargs)
        return self._client

    def _grade(self, file_path: str) -> dict[str, Any] | None:
        """Return the LLM grade for one file, or None if it can't be graded."""
        try:
            text = ContentSampler.extract_text(file_path)
        except Exception:  # noqa: BLE001 - unreadable file, skip grading
            return None
        if not text or len(text.strip()) < ContentSampler.MIN_EXTRACTION_LENGTH:
            return None

        sample = text[: self._max_chars]
        user_prompt = (
            f"TARGET DOCUMENT: {self._description}\n\n"
            "Grade the document excerpt below. Return JSON with: "
            "is_primary (bool), relevance (0.0-1.0), doc_kind (short label "
            "e.g. ordinance, code, draft, presentation, news, report), and a "
            "one-sentence reason.\n\n"
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
        return result.get("data") if isinstance(result, dict) else None

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
        for records in groups.values():
            for record in records:
                grade = self._grade(str(record.get("path")))
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
            f"Document review: graded {graded} file(s), "
            f"selected {selected} primary document(s) "
            f"(keep_top={self._keep_top})."
        )
        return downloads, notes

    @staticmethod
    def _write_sidecar(record: dict[str, Any]) -> None:
        """Write ``.review/<stem>.json`` next to a graded file (best-effort)."""
        if "review_relevance" not in record:
            return
        src = Path(str(record.get("path") or ""))
        if not src.name:
            return
        import json

        review_dir = src.parent / ".review"
        payload = {
            "file": src.name,
            "llm": {
                "is_primary": record.get("review_is_primary"),
                "relevance": record.get("review_relevance"),
                "doc_kind": record.get("review_doc_kind"),
                "reason": record.get("review_reason"),
                "selected": record.get("review_selected"),
            },
            "human": {"decision": None, "notes": None},
        }
        try:
            review_dir.mkdir(parents=True, exist_ok=True)
            (review_dir / f"{src.stem}.json").write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        except OSError:
            return
