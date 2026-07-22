"""Typed acquisition models for candidates, scoring, and manifests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CandidateScore:
    """Score signals and acceptance classification for a candidate URL."""

    url_signal: float = 0.0
    anchor_signal: float = 0.0
    content_signal: float = 0.0
    trust_signal: float = 0.0
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "url_signal": 0.35,
            "anchor_signal": 0.25,
            "content_signal": 0.25,
            "trust_signal": 0.15,
        }
    )

    def weighted_total(self) -> float:
        weighted = (
            self.url_signal * self.weights.get("url_signal", 0.0)
            + self.anchor_signal * self.weights.get("anchor_signal", 0.0)
            + self.content_signal * self.weights.get("content_signal", 0.0)
            + self.trust_signal * self.weights.get("trust_signal", 0.0)
        )
        return max(0.0, min(1.0, weighted))

    def acceptance_class(self) -> str:
        total = self.weighted_total()
        if total >= 0.75:
            return "accepted"
        if total >= 0.45:
            return "needs_review"
        return "rejected"

    def to_dict(self) -> dict[str, Any]:
        total = self.weighted_total()
        return {
            "url_signal": self.url_signal,
            "anchor_signal": self.anchor_signal,
            "content_signal": self.content_signal,
            "trust_signal": self.trust_signal,
            "weights": self.weights,
            "total": total,
            "acceptance_class": self.acceptance_class(),
        }


@dataclass(slots=True)
class AcquisitionCandidate:
    """Normalized candidate URL and validation/scoring details."""

    url: str
    source: str
    score: CandidateScore
    reasons: list[str] = field(default_factory=list)
    status: str | None = None
    mime_type: str | None = None
    extension: str | None = None
    canonical_url: str | None = None
    content_hash: str | None = None
    target_metadata: dict[str, Any] | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        effective_status = self.status or self.score.acceptance_class()
        return {
            "url": self.url,
            "source": self.source,
            "status": effective_status,
            "score": self.score.to_dict(),
            "reasons": self.reasons,
            "mime_type": self.mime_type,
            "extension": self.extension,
            "canonical_url": self.canonical_url,
            "content_hash": self.content_hash,
        }


@dataclass(slots=True)
class AcquisitionManifest:
    """Run-level manifest model for acquisition outputs and provenance."""

    run_id: str
    status: str
    started_at: str
    input: dict[str, Any]
    constraints: dict[str, Any] = field(default_factory=dict)
    lineage: dict[str, Any] = field(default_factory=dict)
    candidates: list[AcquisitionCandidate] = field(default_factory=list)
    downloads: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    error_summary: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    manifest_version: str = "1.0.0"
    # Observability fields
    timing: dict[str, Any] = field(default_factory=dict)
    stage_summaries: dict[str, Any] = field(default_factory=dict)
    candidate_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "run_id": self.run_id,
            "status": self.status,
            "started_at": self.started_at,
            "timing": self.timing,
            "input": self.input,
            "constraints": self.constraints,
            "stage_summaries": self.stage_summaries,
            "candidate_summary": self.candidate_summary,
            "lineage": self.lineage,
            "candidates": [
                candidate.to_dict() for candidate in self.candidates
            ],
            "downloads": self.downloads,
            "errors": self.errors,
            "error_summary": self.error_summary,
            "notes": self.notes,
        }
