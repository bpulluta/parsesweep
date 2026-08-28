"""Tests for the extracted extraction provenance/lineage core module."""

from pathlib import Path

from psweep.extraction.provenance import (
    build_source_context_map,
    generate_run_id,
    prepend_source_context,
)


def test_generate_run_id_is_deterministic_and_input_sensitive():
    kwargs = dict(
        schema_path=Path("schemas/widgets.json"),
        provider="azure",
        model="gpt-x",
        enable_validation=False,
        doc_files=[Path("documents/b.pdf"), Path("documents/a.pdf")],
        artifact_id=None,
    )
    run_id_1 = generate_run_id(**kwargs)
    # Same inputs (document order permuted) -> same id, since docs are sorted.
    run_id_2 = generate_run_id(
        **{**kwargs, "doc_files": [Path("documents/a.pdf"), Path("documents/b.pdf")]}
    )
    assert run_id_1 == run_id_2
    assert run_id_1.startswith("run://")
    assert len(run_id_1) == len("run://") + 16

    # A changed input -> different id.
    run_id_diff = generate_run_id(**{**kwargs, "model": "gpt-y"})
    assert run_id_diff != run_id_1

    # Validation mode changes the seed too.
    run_id_val = generate_run_id(**{**kwargs, "enable_validation": True})
    assert run_id_val != run_id_1


def test_build_source_context_map_from_index(tmp_path):
    index_path = tmp_path / "download_index.csv"
    index_path.write_text(
        "path,final_url,target_label,source_jurisdiction,status\n"
        "docs/report.pdf,https://example.com/report.pdf,Example Site,new-york-city,downloaded\n",
        encoding="utf-8",
    )

    context_map = build_source_context_map(None, str(index_path))
    # Keyed by both the posix path and the bare filename.
    assert "docs/report.pdf" in context_map
    assert "report.pdf" in context_map
    entry = context_map["report.pdf"]
    assert entry["url"] == "https://example.com/report.pdf"
    assert entry["site_name"] == "Example Site"
    assert entry["jurisdiction"] == "New York City"


def test_build_source_context_map_parses_target_metadata(tmp_path):
    index_path = tmp_path / "download_index.csv"
    index_path.write_text(
        'path,url,target_metadata,status\n'
        'a.pdf,https://x/a.pdf,"{""company_name"": ""Acme"", ""city"": ""Reno"", ""state"": ""NV""}",downloaded\n',
        encoding="utf-8",
    )
    context_map = build_source_context_map(None, str(index_path))
    entry = context_map["a.pdf"]
    assert entry["company_name"] == "Acme"
    assert entry["city"] == "Reno"
    assert entry["state"] == "NV"


def test_build_source_context_map_missing_index_returns_empty():
    assert build_source_context_map(None, None) == {}


def test_prepend_source_context_adds_block_when_info_present():
    context_map = {
        "report.pdf": {
            "url": "https://example.com/report.pdf",
            "site_name": "Example Site",
            "company_name": "",
            "jurisdiction": "Reno",
            "city": "Reno",
            "state": "NV",
        }
    }
    out = prepend_source_context("BODY", Path("report.pdf"), context_map)
    assert out.startswith("=== CONTEXT")
    assert "SOURCE_URL: https://example.com/report.pdf" in out
    assert "QUERIED_SITE: Example Site" in out
    assert "QUERIED_JURISDICTION: Reno" in out
    assert "QUERIED_LOCATION: Reno, NV" in out
    assert out.rstrip().endswith("BODY")


def test_prepend_source_context_noop_when_no_match():
    assert prepend_source_context("BODY", Path("unknown.pdf"), {}) == "BODY"
