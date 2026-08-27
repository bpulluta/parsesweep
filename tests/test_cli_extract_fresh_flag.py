from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from psweep.cli.main import cli

SCHEMA_PATH = Path("schemas/personal/geothermal_ordinance_schema.json")


def test_extract_help_exposes_fresh_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["extract", "--help"])
    assert result.exit_code == 0
    flat = " ".join(result.output.split())
    assert "--fresh" in flat
    assert "Re-extract documents that already have output JSON" in flat
    assert ".text_cache" in flat
    assert ".pages" in flat
    # Legacy toggle must be gone.
    assert "--reprocess" not in result.output
    assert "--skip-existing" not in result.output


def test_extract_default_skips_already_processed(tmp_path: Path) -> None:
    """No flag => skip files that already have output JSON (backward compat)."""
    assert SCHEMA_PATH.exists()
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "doc1.txt").write_text("hello world", encoding="utf-8")

    out_dir = tmp_path / "extracted"
    out_dir.mkdir()
    (out_dir / "doc1.json").write_text("{}", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "extract",
            str(docs),
            "--schema",
            str(SCHEMA_PATH),
            "--output",
            str(out_dir),
        ],
    )
    assert result.exit_code == 0
    assert "already processed" in result.output


def test_extract_fresh_reprocesses_existing(tmp_path: Path, monkeypatch) -> None:
    """--fresh => do not skip existing output; the doc is queued for extraction."""
    assert SCHEMA_PATH.exists()
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "doc1.txt").write_text("hello world", encoding="utf-8")

    out_dir = tmp_path / "extracted"
    out_dir.mkdir()
    (out_dir / "doc1.json").write_text("{}", encoding="utf-8")

    captured: dict = {}

    def _fake_extract_one(doc_path, **kwargs):
        captured["ran"] = True
        return {
            "success": True,
            "file": doc_path.name,
            "cost": 0.0,
            "extraction_time": 0.0,
        }

    monkeypatch.setattr(
        "psweep.cli.commands_extract._extract_one_document", _fake_extract_one
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    # Reset the cached global config so get_config() re-reads the environment
    # with the monkeypatched OPENAI_API_KEY rather than returning a stale
    # instance that was populated before the key was set.
    monkeypatch.setattr("psweep.utils.config._global_config", None)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "extract",
            str(docs),
            "--schema",
            str(SCHEMA_PATH),
            "--output",
            str(out_dir),
            "--fresh",
        ],
    )
    # With --fresh the already-present output must NOT short-circuit the run:
    # the extractor is invoked for the existing document.
    assert captured.get("ran") is True, result.output
