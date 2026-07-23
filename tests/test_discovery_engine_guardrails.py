"""Regression tests for discovery engine onboarding guardrails."""

from __future__ import annotations

import json
from pathlib import Path

from psweep.discovery.engine import DiscoveryEngine, DiscoveryRequest


def _read_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_query_discovery_without_candidates_emits_explicit_error(tmp_path):
    """Query-based runs without seeds should fail loudly when discovery cannot produce candidates."""
    documents_dir = tmp_path / "documents"
    manifest_path = tmp_path / "manifest.json"

    request = DiscoveryRequest(
        domain="guardrail_test",
        seed_urls=[],
        query="xcel residential tariff pdf",
        enable_serpapi=False,
        output_documents=documents_dir,
        output_manifest=manifest_path,
        dry_run=True,
    )

    result = DiscoveryEngine().run(request)
    manifest = _read_manifest(result.manifest_path)

    assert manifest["status"] == "scaffold_dry_run_with_errors"
    assert manifest["error_summary"]["total_errors"] >= 1
    assert any(
        record.get("stage") == "discovery.search"
        for record in manifest.get("errors", [])
    )
    assert any(
        "Discovery produced zero candidates" in note
        for note in manifest.get("notes", [])
    )
