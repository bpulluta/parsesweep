"""Atomic manual curation command."""

from __future__ import annotations

import csv
import json

import pytest
from pathlib import Path

from click.testing import CliRunner

from psweep.cli.main import cli
from psweep.discovery import publication
from psweep.discovery.publication import csv_content


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


def test_curate_human_override_publishes_manifest_and_index(tmp_path):
    """Human decisions live in the manifest and indexes only."""
    run, document, index, review = _curation_fixture(tmp_path)
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


def test_curate_reject_removes_only_the_runs_document(tmp_path):
    """Other runs' documents in the same partition survive a rejection."""
    run, document, _, review = _curation_fixture(tmp_path)
    consolidated = tmp_path / "domain/curated"
    consolidated.mkdir(parents=True)
    (consolidated / "other.pdf").write_bytes(b"other target")
    (tmp_path / "domain/download_index.csv").write_text(
        "relative_path,url\nother.pdf,https://agency.gov/other.pdf\n"
    )
    review.write_text(
        csv_content(
            ["relative_path", "human_decision"],
            [{"relative_path": "report.pdf", "human_decision": "reject"}],
        )
    )
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code == 0, result.output
    assert (consolidated / "other.pdf").read_bytes() == b"other target"
    assert not (consolidated / "report.pdf").exists()
    with (tmp_path / "domain/download_index.csv").open() as handle:
        assert [r["relative_path"] for r in csv.DictReader(handle)] == ["other.pdf"]


def test_curate_reject_never_withdraws_another_owners_path(tmp_path):
    """A rejection only withdraws a path whose index owner is this run's."""
    run, _, _, review = _curation_fixture(tmp_path)
    consolidated = tmp_path / "domain/curated"
    consolidated.mkdir(parents=True)
    (consolidated / "report.pdf").write_bytes(b"other owner")
    (tmp_path / "domain/download_index.csv").write_text(
        "relative_path,url,target_id\nreport.pdf,https://other.gov/report.pdf,t2\n"
    )
    review.write_text(
        csv_content(
            ["relative_path", "human_decision"],
            [{"relative_path": "report.pdf", "human_decision": "reject"}],
        )
    )
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code == 0, result.output
    assert (consolidated / "report.pdf").read_bytes() == b"other owner"
    with (tmp_path / "domain/download_index.csv").open() as handle:
        (row,) = csv.DictReader(handle)
    assert row["url"] == "https://other.gov/report.pdf"


def test_curate_refuses_to_overwrite_another_owners_path(tmp_path):
    """Publishing onto a path owned by someone else fails closed."""
    run, _, _, _ = _curation_fixture(tmp_path)
    consolidated = tmp_path / "domain/curated"
    consolidated.mkdir(parents=True)
    (consolidated / "report.pdf").write_bytes(b"other owner")
    (tmp_path / "domain/download_index.csv").write_text(
        "relative_path,url,target_id\nreport.pdf,https://other.gov/report.pdf,t2\n"
    )
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code != 0
    assert (consolidated / "report.pdf").read_bytes() == b"other owner"


def test_curate_flat_paths_stay_inside_custom_run(tmp_path):
    """Curate flat paths stay inside custom run."""
    run, _, _, _ = _curation_fixture(tmp_path, flat=True)
    result = CliRunner().invoke(cli, ["curate", "--run", str(run), "--quiet"])
    assert result.exit_code == 0, result.output
    assert (run / "run-curated/report.pdf").exists()
    assert (run / "curated/report.pdf").exists()
    assert not (tmp_path / "curated").exists()
    assert not (tmp_path / "download_index.csv").exists()


@pytest.mark.parametrize("failure", ["missing", "link", "manifest"])
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


@pytest.mark.parametrize("review_only", [True, False])
def test_human_keep_releases_only_review_failure_blocks(tmp_path, review_only):
    """Human review cannot release search/download incompleteness."""
    run, _, _, review = _curation_fixture(tmp_path)
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["downloads"][0].update(
        review_failed=True, review_retryable=True, review_selected=False
    )
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
