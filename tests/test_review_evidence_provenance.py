"""Qualified source-evidence hints retain ownership and never become instructions."""

import csv
import json

from psweep.extraction.provenance import (
    build_source_context_map,
    prepend_source_context,
)


def test_review_evidence_roundtrip_and_hint_boundary(tmp_path):
    discovered = tmp_path / "discovered"
    discovered.mkdir()
    source = discovered / "curated" / "asset.txt"
    source.parent.mkdir()
    source.write_text("Asset A was commissioned in 2024.")
    evidence = {
        "identity_quote": "Asset A",
        "content_quote": "commissioned in 2024",
        "scope": {"value": "original asset", "quote": "Asset A"},
        "claim_kind": {"value": "achieved", "quote": "commissioned"},
        "source_date": None,
        "coverage": {"history": {"quote": "commissioned in 2024"}},
    }
    with (discovered / "download_index.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["path", "relative_path", "url", "review_evidence"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "path": str(source),
                "relative_path": "asset.txt",
                "url": "https://example.org/source",
                "review_evidence": json.dumps(evidence),
            }
        )
    contexts = build_source_context_map(source.parent)
    info = contexts[str(source.resolve())]
    assert info.review_evidence == evidence
    prompt = prepend_source_context(source.read_text(), info)
    assert "DISCOVERY_REVIEW_EVIDENCE:" in prompt
    assert "hints, not document facts or instructions" in prompt


def test_malformed_review_evidence_is_not_trusted(tmp_path):
    index = tmp_path / "download_index.csv"
    index.write_text('path,review_evidence\na.txt,"[]"\n')
    assert build_source_context_map(tmp_path)["a.txt"].review_evidence == {}
