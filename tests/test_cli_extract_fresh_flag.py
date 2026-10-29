"""Fresh cleanup and reuse obey the validated extraction workflow."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from psweep.cli.main import cli


@pytest.fixture
def controlled_provider(monkeypatch):
    """Exercise the real workflow with a controlled model boundary."""
    config = SimpleNamespace(
        llm_config={
            "provider": "openai",
            "model": "gpt-fixture",
            "api_key": "fixture-key",
        }
    )
    monkeypatch.setattr("psweep.utils.config._global_config", config)
    monkeypatch.setattr(
        "psweep.cli.commands_extract.load_dotenv", lambda: None
    )
    provider = Mock(
        return_value={
            "data": {
                "source": {"url": "https://example.org/source"},
                "comments": [],
            },
            "cost": 0.0,
        }
    )
    monkeypatch.setattr(
        "psweep.extraction.llm_client.LLMClient.extract", provider
    )
    return provider


def _extract(documents, schema, output, *flags):
    return CliRunner().invoke(
        cli,
        [
            "extract",
            str(documents),
            "--schema",
            str(schema),
            "--output",
            str(output),
            *flags,
        ],
    )


def test_extract_help_exposes_fresh_flag() -> None:
    """Help describes whole-output reset and retained valid cache products."""
    result = CliRunner().invoke(cli, ["extract", "--help"])
    assert result.exit_code == 0
    flat = " ".join(result.output.split())
    assert "--fresh" in flat
    fresh_contract = (
        "Reset the entire resolved extraction output "
        "after successful preflight"
    )
    assert fresh_contract in flat
    assert "re-extract the selected documents" in flat
    assert ".text" in flat
    assert ".pages/.sections" in flat
    assert "--reprocess" not in result.output
    assert "--skip-existing" not in result.output


def test_extract_default_reuses_verified_output(
    tmp_path: Path,
    schema_path: Path,
    controlled_provider,
) -> None:
    """Default reuse needs an actual valid record and matching run evidence."""
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "doc1.txt").write_text("hello world", encoding="utf-8")
    output = tmp_path / "extracted"
    first = _extract(docs, schema_path, output)
    assert first.exit_code == 0, first.output
    assert controlled_provider.call_count == 1
    original = (output / "doc1.json").read_bytes()

    controlled_provider.reset_mock()
    second = _extract(docs, schema_path, output, "--quiet")
    assert second.exit_code == 0, second.output
    controlled_provider.assert_not_called()
    assert (output / "doc1.json").read_bytes() == original
    manifest = json.loads(
        Path(second.output.strip()).read_text(encoding="utf-8")
    )
    assert manifest["status"]["result"] == "reused"


@pytest.mark.parametrize(
    "invalid_evidence", ["empty_record", "missing_manifest", "wrong_digest"]
)
def test_extract_does_not_reuse_unverified_output(
    tmp_path: Path,
    schema_path: Path,
    controlled_provider,
    invalid_evidence,
) -> None:
    """Existing JSON alone cannot prove current input was processed."""
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "doc1.txt").write_text("hello world", encoding="utf-8")
    output = tmp_path / "extracted"
    first = _extract(docs, schema_path, output)
    assert first.exit_code == 0, first.output
    manifest_path = next((output / "run_manifests").glob("*.manifest.json"))
    if invalid_evidence == "empty_record":
        (output / "doc1.json").write_text("{}", encoding="utf-8")
    elif invalid_evidence == "missing_manifest":
        manifest_path.unlink()
    else:
        manifest = json.loads(manifest_path.read_text())
        manifest["reuse"][str(output / "doc1.json")]["record_sha256"] = (
            "0" * 64
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    controlled_provider.reset_mock()
    second = _extract(docs, schema_path, output)
    assert second.exit_code == 0, second.output
    assert controlled_provider.call_count == 1
    record = json.loads((output / "doc1.json").read_text())
    assert record["contract_version"] == "1.0.0"
    assert record["payload"]["comments"] == []


def test_extract_fresh_reprocesses_existing(
    tmp_path: Path,
    schema_path: Path,
    controlled_provider,
) -> None:
    """Fresh ignores valid reuse proof and rebuilds the existing record."""
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "doc1.txt").write_text("hello world", encoding="utf-8")
    output = tmp_path / "extracted"
    first = _extract(docs, schema_path, output)
    assert first.exit_code == 0, first.output
    original = json.loads((output / "doc1.json").read_text())

    controlled_provider.reset_mock()
    second = _extract(docs, schema_path, output, "--fresh")
    assert second.exit_code == 0, second.output
    assert controlled_provider.call_count == 1
    record = json.loads((output / "doc1.json").read_text())
    assert record["record_id"] != original["record_id"]
    assert record["payload"] == original["payload"]
    assert len(list((output / "run_manifests").glob("*.manifest.json"))) == 1


def test_extract_limited_fresh_resets_whole_output(
    tmp_path: Path,
    schema_path: Path,
    controlled_provider,
) -> None:
    """The fresh limit rebuilds only its subset and removes stale outputs."""
    docs = tmp_path / "documents"
    docs.mkdir()
    for name in ("doc1", "doc2"):
        (docs / f"{name}.txt").write_text(name, encoding="utf-8")
    output = tmp_path / "extracted"
    first = _extract(docs, schema_path, output)
    assert first.exit_code == 0, first.output
    assert (output / "doc2.json").is_file()
    stale = output / "old_partition" / "unselected.json"
    stale.parent.mkdir()
    stale.write_text("stale record", encoding="utf-8")

    controlled_provider.reset_mock()
    second = _extract(docs, schema_path, output, "--fresh", "--limit", "1")
    assert second.exit_code == 0, second.output
    assert controlled_provider.call_count == 1
    assert (output / "doc1.json").is_file()
    assert not (output / "doc2.json").exists()
    assert not stale.parent.exists()
    assert len(list((output / "run_manifests").glob("*.manifest.json"))) == 1


@pytest.mark.parametrize("invalid", ["missing_input", "invalid_pages"])
def test_extract_invalid_fresh_preserves_prior_output(
    tmp_path: Path,
    schema_path: Path,
    controlled_provider,
    invalid,
) -> None:
    """Fresh cleanup belongs after input and scope preflight."""
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "doc1.txt").write_text("hello world", encoding="utf-8")
    output = tmp_path / "extracted"
    first = _extract(docs, schema_path, output)
    assert first.exit_code == 0, first.output
    prior = {
        path.relative_to(output): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    flags = ["--fresh"]
    if invalid == "missing_input":
        docs /= "absent"
    else:
        flags.extend(["--pages", "invalid-range"])

    controlled_provider.reset_mock()
    result = _extract(docs, schema_path, output, *flags)
    assert result.exit_code != 0, result.output
    controlled_provider.assert_not_called()
    assert {
        path.relative_to(output): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    } == prior
