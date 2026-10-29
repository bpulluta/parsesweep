"""Check schema validation and atomic manual curation commands."""

from __future__ import annotations

import csv
import json

import pytest
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from psweep.cli.main import cli
from psweep.discovery import publication
from psweep.discovery.publication import csv_content


def test_check_command_valid_payload(monkeypatch, tmp_path: Path) -> None:
    """Check command valid payload."""
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"foo": {"type": "string"}},
                "required": ["foo"],
            }
        ),
        encoding="utf-8",
    )

    extraction_path = tmp_path / "doc.json"
    extraction_path.write_text(
        json.dumps({"payload": {"foo": "bar"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "psweep.cli.commands_check.get_config",
        lambda: SimpleNamespace(default_schema=str(schema_path)),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["check", str(extraction_path)])
    assert result.exit_code == 0
    assert "Schema validation passed" in result.output


def test_check_command_invalid_json_exits_nonzero(
    monkeypatch, tmp_path: Path
) -> None:
    """Check command invalid json exits nonzero."""
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps({"type": "object", "properties": {}}),
        encoding="utf-8",
    )
    extraction_path = tmp_path / "bad.json"
    extraction_path.write_text("{invalid", encoding="utf-8")

    monkeypatch.setattr(
        "psweep.cli.commands_check.get_config",
        lambda: SimpleNamespace(default_schema=str(schema_path)),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["check", str(extraction_path)])
    assert result.exit_code == 1
    assert "Invalid JSON file" in result.output


def test_check_uses_recorded_schema_instead_of_default(monkeypatch, tmp_path):
    """Check uses recorded schema instead of default."""
    schema_path = tmp_path / "resolved.json"
    schema_path.write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["quote"],
                "properties": {"quote": {"type": "string", "pattern": r"\S"}},
            }
        )
    )
    extraction = tmp_path / "record.json"
    extraction.write_text(
        json.dumps(
            {
                "lineage": {"schema_id": str(schema_path)},
                "payload": {"quote": "Evidence"},
            }
        )
    )
    monkeypatch.setattr(
        "psweep.cli.commands_check.get_config",
        lambda: SimpleNamespace(default_schema="not-present.json"),
    )
    result = CliRunner().invoke(cli, ["check", str(extraction)])
    assert result.exit_code == 0, result.output


def test_check_explicit_schema_and_calendar_format(tmp_path):
    """Check explicit schema and calendar format."""
    schema_path = tmp_path / "resolved.json"
    schema_path.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"date": {"type": "string", "format": "date"}},
            }
        )
    )
    extraction = tmp_path / "record.json"
    extraction.write_text(json.dumps({"payload": {"date": "2024-02-30"}}))
    result = CliRunner().invoke(
        cli, ["check", str(extraction), "--schema", str(schema_path)]
    )
    assert result.exit_code == 1
    assert "Schema validation failed" in result.output


def test_check_missing_recorded_schema_does_not_silently_fallback(
    monkeypatch, tmp_path
):
    """Check missing recorded schema does not silently fallback."""
    default = tmp_path / "default.json"
    default.write_text(json.dumps({"type": "object"}))
    extraction = tmp_path / "record.json"
    extraction.write_text(
        json.dumps(
            {
                "lineage": {"schema_id": "absent-domain-schema.json"},
                "payload": {},
            }
        )
    )
    monkeypatch.setattr(
        "psweep.cli.commands_check.get_config",
        lambda: SimpleNamespace(default_schema=str(default)),
    )
    result = CliRunner().invoke(cli, ["check", str(extraction)])
    assert result.exit_code == 1
    assert "Schema not found" in result.output


def test_check_invalid_schema_reports_error(tmp_path):
    """Check invalid schema reports error."""
    schema = tmp_path / "invalid.json"
    schema.write_text(json.dumps({"type": "invalid"}))
    record = tmp_path / "record.json"
    record.write_text(json.dumps({"payload": {}}))
    result = CliRunner().invoke(
        cli, ["check", str(record), "--schema", str(schema)]
    )
    assert result.exit_code == 1
    assert "Cannot load schema" in result.output


def test_curate_command_with_run_dir_quiet(tmp_path: Path) -> None:
    """Curate command with run dir quiet."""
    run_dir = tmp_path / "domain/runs/run-001"
    docs_dir = run_dir / "documents"
    docs_dir.mkdir(parents=True)

    doc_path = docs_dir / "example.txt"
    doc_path.write_text("hello", encoding="utf-8")

    review_csv = run_dir / "review.csv"
    with review_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "path",
                "relative_path",
                "llm_selected",
                "human_decision",
                "human_notes",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "path": str(doc_path),
                "relative_path": "example.txt",
                "llm_selected": "true",
                "human_decision": "",
                "human_notes": "",
            }
        )

    record = {
        "status": "downloaded",
        "path": str(doc_path),
        "relative_path": "example.txt",
        "review_selected": True,
        "url": "https://agency.gov/example",
    }
    _write_manifest_and_index(run_dir, [record])
    runner = CliRunner()
    result = runner.invoke(cli, ["curate", "--run", str(run_dir), "--quiet"])
    assert result.exit_code == 0
    curated_dir = Path(result.output.strip())
    assert curated_dir.exists()
    assert (curated_dir / "example.txt").exists()
    assert (tmp_path / "domain/curated/example.txt").exists()


def _write_manifest_and_index(run: Path, records: list[dict], *, flat=False):
    index = run / ("run-download_index.csv" if flat else "download_index.csv")
    review = run / "ledger.csv"
    manifest = {
        "run_id": "fixture",
        "input": {"domain": "fixture"},
        "downloads": records,
        "lineage": {
            "documents_dir": str(run / "documents"),
            "download_index_csv": str(index),
            "review_index_csv": str(review),
            "curated_dir": str(run / ("run-curated" if flat else "curated")),
        },
    }
    (run / "manifest.json").write_text(json.dumps(manifest))
    fields = ["relative_path", "path", "status", "url", "review_selected"]
    index.write_text(csv_content(fields, records))
    if (run / "review.csv").exists():
        (run / "review.csv").rename(review)
    return manifest, index, review


def _curation_fixture(tmp_path, *, reviewed=True, flat=False):
    run = tmp_path / ("flat" if flat else "domain/runs/r1")
    documents = run / "documents"
    documents.mkdir(parents=True)
    document = documents / "report.pdf"
    document.write_bytes(b"report")
    record = {
        "status": "downloaded",
        "path": str(document),
        "relative_path": "report.pdf",
        "url": "https://agency.gov/report.pdf",
    }
    if reviewed:
        record["review_selected"] = True
    _manifest, index, review = _write_manifest_and_index(
        run, [record], flat=flat
    )
    review.write_text(
        csv_content(
            [
                "relative_path",
                "path",
                "llm_selected",
                "human_decision",
                "human_notes",
            ],
            [
                {
                    "relative_path": "report.pdf",
                    "path": str(document),
                    "llm_selected": "true" if reviewed else "",
                    "human_decision": "",
                    "human_notes": "",
                }
            ],
        )
    )
    return run, document, index, review


def test_curate_no_review_blank_ledger_keeps_downloaded_default(tmp_path):
    """Curate no review blank ledger keeps downloaded default."""
    run, _, index, _ = _curation_fixture(tmp_path, reviewed=False)
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code == 0, result.output
    assert (run / "curated/report.pdf").exists()
    assert (tmp_path / "domain/curated/report.pdf").exists()
    with index.open() as handle:
        row = next(csv.DictReader(handle))
    assert row["curated_selected"] == "True"
    assert not row["review_selected"]


def test_curate_blank_ledger_uses_manifest_selection_not_edited_llm_column(
    tmp_path,
):
    """Curate blank ledger uses manifest selection not edited llm
    column.
    """
    run, _, _, review = _curation_fixture(tmp_path)
    review.write_text(
        csv_content(
            ["relative_path", "human_decision", "llm_selected"],
            [
                {
                    "relative_path": "report.pdf",
                    "human_decision": "",
                    "llm_selected": "false",
                }
            ],
        )
    )
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code == 0, result.output
    assert (run / "curated/report.pdf").exists()


def test_curate_human_keep_overrides_underlying_rejection(tmp_path):
    """Curate human keep overrides underlying rejection."""
    run, document, _, review = _curation_fixture(tmp_path)
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["downloads"][0]["review_selected"] = False
    (run / "manifest.json").write_text(json.dumps(manifest))
    review.write_text(
        csv_content(
            ["relative_path", "human_decision"],
            [{"relative_path": "report.pdf", "human_decision": "keep"}],
        )
    )
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code == 0, result.output
    assert (run / "curated/report.pdf").read_bytes() == document.read_bytes()
    assert (tmp_path / "domain/curated/report.pdf").exists()
    assert (
        json.loads((run / "manifest.json").read_text())["downloads"][0][
            "review_selected"
        ]
        is False
    )


def test_curate_human_override_publishes_manifest_index_and_sidecar_identity(
    tmp_path,
):
    """Curate human override publishes manifest index and sidecar
    identity.
    """
    run, document, index, review = _curation_fixture(tmp_path)
    sidecar = document.parent / ".review" / "report.pdf.json"
    sidecar.parent.mkdir()
    identity = {
        "file": "report.pdf",
        "source_url": "https://agency.gov/report.pdf",
        "cache_key": "immutable",
        "llm": {"is_primary": True},
        "selection": {"selected": True},
        "coverage": {"mode": "full"},
        "human": {"decision": None},
    }
    sidecar.write_text(json.dumps(identity))
    wrong_stem = sidecar.parent / "report.json"
    wrong_stem.write_text('{"untouched":true}')
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code == 0, result.output
    review.write_text(
        csv_content(
            ["relative_path", "path", "human_decision", "human_notes"],
            [
                {
                    "relative_path": "report.pdf",
                    "path": str(document),
                    "human_decision": "reject",
                    "human_notes": "not current",
                }
            ],
        )
    )
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code == 0, result.output
    assert not (run / "curated/report.pdf").exists()
    assert not (tmp_path / "domain/curated/report.pdf").exists()
    updated = json.loads(sidecar.read_text())
    assert {
        key: value for key, value in updated.items() if key != "human"
    } == {key: value for key, value in identity.items() if key != "human"}
    assert updated["human"] == {"decision": "reject", "notes": "not current"}
    assert wrong_stem.read_text() == '{"untouched":true}'
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["lineage"]["curated_count"] == 0
    assert manifest["downloads"][0]["review_selected"] is True
    assert manifest["downloads"][0]["human_decision"] == "reject"
    with index.open() as handle:
        row = next(csv.DictReader(handle))
    assert (
        row["review_selected"] == "True" and row["human_decision"] == "reject"
    )
    assert row["curated_selected"] == "False"
    with (tmp_path / "domain/download_index.csv").open() as handle:
        assert list(csv.DictReader(handle)) == []


def test_curate_flat_paths_stay_inside_custom_run(tmp_path):
    """Curate flat paths stay inside custom run."""
    run, _, _, _ = _curation_fixture(tmp_path, flat=True)
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code == 0, result.output
    assert (run / "run-curated/report.pdf").exists()
    assert (run / "curated/report.pdf").exists()
    assert not (tmp_path / "curated").exists()
    assert not (tmp_path / "download_index.csv").exists()


@pytest.mark.parametrize("failure", ["missing", "link", "manifest", "sidecar"])
def test_curate_failure_exit_one_preserves_all_prior_publications(
    tmp_path, monkeypatch, failure
):
    """Curate failure exit one preserves all prior publications."""
    run, document, index, _ = _curation_fixture(tmp_path)
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code == 0, result.output
    manifest = run / "manifest.json"
    before_manifest, before_index = manifest.read_bytes(), index.read_bytes()
    domain_index = tmp_path / "domain/download_index.csv"
    before_domain_index = domain_index.read_bytes()
    if failure == "missing":
        document.unlink()
    elif failure == "link":
        monkeypatch.setattr(
            publication,
            "_link",
            lambda *_: (_ for _ in ()).throw(OSError("link failed")),
        )
    elif failure == "sidecar":
        sidecar = document.parent / ".review/report.pdf.json"
        sidecar.parent.mkdir()
        sidecar.write_text("{")
    else:
        replace = Path.replace

        def fail_manifest(source, dest):
            if source.name.startswith("new-") and Path(dest) == manifest:
                raise OSError("manifest publication failed")
            return replace(source, dest)

        monkeypatch.setattr(Path, "replace", fail_manifest)
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code == 1, result.output
    assert "Curation publication failed" in result.output
    assert (run / "curated/report.pdf").read_bytes() == b"report"
    assert (tmp_path / "domain/curated/report.pdf").read_bytes() == b"report"
    assert manifest.read_bytes() == before_manifest
    assert index.read_bytes() == before_index
    assert domain_index.read_bytes() == before_domain_index


def test_curate_missing_manifest_is_not_legacy_ledger_fallback(tmp_path):
    """Curate missing manifest is not legacy ledger fallback."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "review.csv").write_text(
        "relative_path,human_decision\\nreport.pdf,keep\\n"
    )
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code == 1
    assert not (run / "curated").exists()


def test_curate_config_locates_actual_custom_manifest_filename(
    tmp_path, monkeypatch
):
    """Curate config locates actual custom manifest filename."""
    run, _, _, _ = _curation_fixture(tmp_path, flat=True)
    custom = run / "actual-manifest.json"
    (run / "manifest.json").rename(custom)
    config = tmp_path / "run.yaml"
    config.write_text(
        f"domain: fixture\ndiscovery:\n  output_manifest: {custom}\n"
    )
    monkeypatch.setattr(
        "psweep.cli.commands_curate.resolve_command_config",
        lambda **_kwargs: {
            "domain": "fixture",
            "output_manifest": str(custom),
        },
    )
    result = CliRunner().invoke(
        cli, ["curate", "--config", str(config), "--quiet"]
    )
    assert result.exit_code == 0, result.output
    assert (run / "run-curated/report.pdf").exists()
    assert json.loads(custom.read_text())["lineage"]["curated_count"] == 1
    assert not (run / "manifest.json").exists()
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code == 1
    assert "use --config" in result.output


@pytest.mark.parametrize("review_only", [True, False])
def test_human_keep_releases_only_review_failure_blocks(tmp_path, review_only):
    """Human review cannot release search/download incompleteness."""
    run, _, _, review = _curation_fixture(tmp_path)
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["downloads"][0].update(review_failed=True, review_selected=False)
    manifest["lineage"]["blocked_partitions"] = ["."]
    manifest["lineage"]["review_only_blocked_partitions"] = (
        ["."] if review_only else []
    )
    manifest_path.write_text(json.dumps(manifest))
    review.write_text(
        csv_content(
            ["relative_path", "human_decision"],
            [{"relative_path": "report.pdf", "human_decision": "keep"}],
        )
    )
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code == 0, result.output
    assert (run / "curated/report.pdf").exists()
    assert (tmp_path / "domain/curated/report.pdf").exists() is review_only
