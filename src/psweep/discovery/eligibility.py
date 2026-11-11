"""Domain-configured PDF screening decisions, separate from acquisition status."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


def eligibility_held(record: Mapping[str, object]) -> bool:
    """Whether automated curation must wait for an explicit human decision."""
    if str(record.get("human_decision") or "").strip().lower() in {"keep", "reject"}:
        return False
    evidence = record.get("document_eligibility")
    return (
        isinstance(evidence, Mapping)
        and evidence.get("mode") == "hold"
        and evidence.get("state") != "no_findings"
    )


def screening_identity(patterns: list[str]) -> str:
    """Bind detector semantics and configured label patterns, independently of action."""
    import hashlib
    import json
    from psweep.extraction.pdf_inspection import INSPECTOR_VERSION

    return hashlib.sha256(json.dumps([INSPECTOR_VERSION, patterns]).encode()).hexdigest()


def screen_pdf(record: dict[str, object], mode: str, patterns: list[str]) -> None:
    """Recompute evidence from current bytes; disabling screening clears old evidence."""
    record.pop("document_eligibility", None)
    path = Path(str(record.get("path") or ""))
    if mode == "off" or path.suffix.lower() != ".pdf":
        return
    from psweep.extraction.pdf_inspection import inspect_pdf

    record["document_eligibility"] = {
        **inspect_pdf(path, draft_patterns=patterns),
        "mode": mode,
        "identity": screening_identity(patterns),
    }


def eligibility_columns(record: Mapping[str, object]) -> dict[str, object]:
    """Flatten bounded inspection summaries for the existing human ledger/index."""
    evidence = record.get("document_eligibility")
    if not isinstance(evidence, Mapping):
        return dict.fromkeys(
            (
                "eligibility_state",
                "eligibility_mode",
                "eligibility_reason",
                "eligibility_identity",
                "eligibility_sha256",
            ),
            "",
        )
    reasons = [
        f"{item.get('kind', 'signal')}" + (f" (page {item['page']})" if item.get("page") else "")
        for item in evidence.get("evidence", [])
    ]
    reasons.extend(str(item) for item in evidence.get("limitations", []))
    return {
        "eligibility_state": evidence.get("state"),
        "eligibility_mode": evidence.get("mode"),
        "eligibility_reason": "; ".join(dict.fromkeys(reasons))[:2000],
        "eligibility_identity": evidence.get("identity", ""),
        "eligibility_sha256": evidence.get("source_sha256", ""),
    }
