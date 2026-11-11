"""PDF screening policy wiring and curation lifecycle."""

from __future__ import annotations

import csv
from io import StringIO

import pytest

from psweep.config.discovery_config import resolve_discovery_section
from psweep.config.errors import RuntimeConfigError
from psweep.discovery.engine import DiscoveryEngine, DiscoveryRequest
from psweep.discovery.publication import effective_discovery_fingerprint, record_is_curated


def _evidence(state="flagged", mode="hold"):
    return {
        "mode": mode,
        "state": state,
        "inspector_version": 1,
        "source_sha256": "fixture",
        "page_count": 1,
        "inspected_pages": 1,
        "evidence": [{"kind": "strikeout", "strength": "strong", "page": 1}],
        "evidence_count": 1,
        "limitations": [],
    }


def _record(path, **extra):
    return {
        "status": "downloaded",
        "path": str(path),
        "relative_path": path.name,
        "url": "https://example.org/source.pdf",
        **extra,
    }


@pytest.mark.parametrize("mode", ["off", "warn", "hold"])
def test_screening_config_modes(mode):
    fields = resolve_discovery_section({"document_classifier": {"pdf_screening": mode}}, None)
    assert fields["document_classifier"][0]["pdf_screening"] == mode


@pytest.mark.parametrize("mode", [True, False, "strict", {}, [], None])
def test_invalid_screening_config(mode):
    with pytest.raises(RuntimeConfigError, match="pdf_screening"):
        resolve_discovery_section({"document_classifier": {"pdf_screening": mode}}, None)


@pytest.mark.parametrize("state", ["flagged", "unknown"])
@pytest.mark.parametrize("review_ran", [True, False])
def test_hold_overrides_automatic_review_not_human(state, review_ran):
    record = {
        "status": "downloaded",
        "review_selected": True,
        "document_eligibility": _evidence(state),
    }
    assert not record_is_curated(record, review_ran=review_ran)
    record["human_decision"] = "keep"
    assert record_is_curated(record, review_ran=review_ran)
    record["human_decision"] = "reject"
    assert not record_is_curated(record, review_ran=review_ran)


@pytest.mark.parametrize("mode,state", [("warn", "flagged"), ("hold", "no_findings")])
def test_unheld_evidence_does_not_select_irrelevant_records(mode, state):
    record = {
        "status": "downloaded",
        "review_selected": False,
        "document_eligibility": _evidence(state, mode),
    }
    assert record_is_curated(record, review_ran=False)
    assert not record_is_curated(record, review_ran=True)


def test_screening_only_config_does_not_run_keyword_classifier(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"fixture")
    monkeypatch.setattr("psweep.extraction.pdf_inspection.inspect_pdf", lambda *a, **k: _evidence())
    monkeypatch.setattr(
        "psweep.discovery.validators.ContentSampler.validate_content",
        lambda *a, **k: pytest.fail("No keyword configuration"),
    )
    records, _ = DiscoveryEngine._run_post_download_classifier(
        [_record(path, source="configured")], {"pdf_screening": "hold"}, []
    )
    assert records[0]["status"] == "downloaded"
    assert records[0]["document_eligibility"]["state"] == "flagged"
    assert not record_is_curated(records[0], review_ran=False)


def test_off_and_non_pdf_do_not_inspect(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "psweep.extraction.pdf_inspection.inspect_pdf",
        lambda *a, **k: pytest.fail("Screening is disabled or not applicable"),
    )
    for name, mode in [("source.pdf", "off"), ("source.txt", "hold")]:
        path = tmp_path / name
        path.write_text("ordinary source")
        records, _ = DiscoveryEngine._run_post_download_classifier(
            [_record(path)], {"pdf_screening": mode}, []
        )
        assert "document_eligibility" not in records[0]


@pytest.mark.parametrize("mode", ["curated", "none"])
def test_hold_retains_original_and_is_in_review_ledger(tmp_path, mode):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"fixture")
    record = _record(path, document_eligibility=_evidence(), status="rejected_classifier")
    DiscoveryEngine._apply_document_retention(
        mode=mode, documents_dir=tmp_path, curated_dir=None, download_records=[record]
    )
    assert path.exists()
    ledger = DiscoveryEngine._write_review_index(
        download_records=[record], run_dir=tmp_path, render_only=True
    )
    (row,) = csv.DictReader(StringIO(ledger))
    assert row["eligibility_state"] == "flagged"
    assert row["curated_selected"] == "False"


def test_active_screening_fingerprint_includes_inspector_version(monkeypatch):
    request = DiscoveryRequest(domain="example", document_classifier={"pdf_screening": "hold"})
    old = effective_discovery_fingerprint(request)
    monkeypatch.setattr("psweep.extraction.pdf_inspection.INSPECTOR_VERSION", 987654)
    assert effective_discovery_fingerprint(request) != old
    request.document_classifier = {"pdf_screening": "off"}
    old = effective_discovery_fingerprint(request)
    monkeypatch.setattr("psweep.extraction.pdf_inspection.INSPECTOR_VERSION", 987655)
    assert effective_discovery_fingerprint(request) == old


def _prior_index(tmp_path):
    from psweep.discovery.publication import artifact_sha256

    root = tmp_path / "corpus"
    prior = root / "curated/shared/source.pdf"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"prior original bytes")
    index = root / "download_index.csv"
    index.write_text(
        "relative_path,url,target_id\nshared/source.pdf,https://example.org/source.pdf,t1\n"
    )
    return root, prior, artifact_sha256(prior)


def test_identical_held_prior_is_withdrawn_even_in_blocked_partition(tmp_path):
    from psweep.discovery.publication import _merged_index

    root, prior, digest = _prior_index(tmp_path)
    content, removed = _merged_index(
        run_index=tmp_path / "incoming.csv",
        domain_index=root / "download_index.csv",
        promoted_paths=set(),
        replaced_partitions=set(),
        held_hashes={("t1", "https://example.org/source.pdf"): digest},
        pdf_screening_patterns=[],
        is_blocked=lambda _: True,
        run_content="relative_path,url,target_id\n",
    )
    assert removed == {"shared/source.pdf"}
    assert not list(csv.DictReader(StringIO(content)))
    assert prior.exists()  # Merge stages removals; it does not mutate live files.


def test_unchecked_prior_blocks_publication_without_losing_original(tmp_path):
    from psweep.discovery.publication import PublicationError, _merged_index

    root, prior, _ = _prior_index(tmp_path)
    with pytest.raises(PublicationError, match="unchecked prior document"):
        _merged_index(
            run_index=tmp_path / "incoming.csv",
            domain_index=root / "download_index.csv",
            promoted_paths=set(),
            replaced_partitions=set(),
            held_hashes={("t1", "https://example.org/source.pdf"): "different-bytes"},
            pdf_screening_patterns=[],
            is_blocked=lambda _: True,
            run_content="relative_path,url,target_id\n",
        )
    assert prior.read_bytes() == b"prior original bytes"


def test_no_review_screening_does_not_consume_top_n(tmp_path, monkeypatch):
    from psweep.discovery.document_reviewer import DocumentReviewer

    marked = _record(tmp_path / "marked.pdf", document_eligibility=_evidence())
    clean = _record(tmp_path / "clean.pdf")
    seen = []

    def review(self, records, notes):
        seen.extend(records)
        for r in records:
            r["review_selected"] = True
        return records, notes

    monkeypatch.setattr(DocumentReviewer, "review", review)
    records, _, _ = DiscoveryEngine._run_document_review(
        [marked, clean], {"document_description": "records", "keep_top": 1}, [], tmp_path
    )
    assert seen == [clean]
    assert not record_is_curated(records[0], review_ran=True)
    assert record_is_curated(records[1], review_ran=True)


@pytest.mark.parametrize("exists", [True, False])
def test_empty_fresh_root_publication_remains_an_extraction_input(tmp_path, exists):
    from pathlib import Path
    from psweep.discovery.publication import publish_manual_curation, resolve_publication_paths

    root = tmp_path / "corpus"
    run = root / "runs/current"
    documents = run / "documents"
    documents.mkdir(parents=True)
    if exists:
        (root / "curated").mkdir()
        (root / "curated/old.pdf").write_bytes(b"old")
    paths = resolve_publication_paths(documents, run / "manifest.json", "example", domain_dir=root)
    publish_manual_curation(
        paths=paths,
        download_records=[],
        run_index=paths.run_index,
        run_index_content="relative_path,url\n",
        metadata_updates={},
        attempted_partitions={Path()},
    )
    assert (root / "curated").is_dir()
    assert not list((root / "curated").iterdir())
