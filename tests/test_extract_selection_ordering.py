"""Extraction limits apply to pending work after validated reuse."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from psweep.cli.main import cli


@pytest.fixture
def controlled_extraction(monkeypatch):
    """Record document work at the mocked model boundary."""
    seen: list[str] = []
    config = SimpleNamespace(
        llm_config={
            "provider": "openai",
            "model": "gpt-fixture",
            "api_key": "fixture-key",
        }
    )
    monkeypatch.setattr("psweep.utils.config._global_config", config)
    monkeypatch.setattr("psweep.cli.commands_extract.load_dotenv", lambda: None)

    def extract(self, text, schema, **kwargs):
        # Source context may precede the body for indexed documents.
        seen.append(text.splitlines()[-1])
        return {
            "data": {
                "source": {"url": "https://example.org/source"},
                "comments": [],
            },
            "cost": 0.0,
        }

    monkeypatch.setattr("psweep.extraction.llm_client.LLMClient.extract", extract)
    return seen


def _invoke(schema_path, output, *inputs):
    return CliRunner().invoke(
        cli,
        [
            "extract",
            *inputs,
            "--schema",
            str(schema_path),
            "--output",
            str(output),
        ],
    )


def test_limit_selects_next_unprocessed_documents(
    tmp_path: Path,
    schema_path: Path,
    controlled_extraction,
) -> None:
    """A limited directory run advances past verified prior records."""
    docs = tmp_path / "documents"
    docs.mkdir()
    for i in range(1, 5):
        (docs / f"doc{i}.txt").write_text(f"doc{i}.txt", encoding="utf-8")
    output = tmp_path / "extracted"

    # Generate valid records and manifest evidence through the same workflow.
    first = _invoke(schema_path, output, str(docs), "-n", "2")
    assert first.exit_code == 0, first.output
    assert sorted(controlled_extraction) == ["doc1.txt", "doc2.txt"]
    prior = [(output / f"doc{i}.json").read_bytes() for i in (1, 2)]
    controlled_extraction.clear()

    second = _invoke(schema_path, output, str(docs), "-n", "2")
    assert second.exit_code == 0, second.output
    assert sorted(controlled_extraction) == ["doc3.txt", "doc4.txt"]
    assert [(output / f"doc{i}.json").read_bytes() for i in (1, 2)] == prior
    assert all((output / f"doc{i}.json").is_file() for i in range(1, 5))


def test_curated_limit_selects_next_unprocessed(
    tmp_path: Path,
    schema_path: Path,
    controlled_extraction,
) -> None:
    """A partitioned limited run advances past verified prior records."""
    discovered = tmp_path / "discovered" / "demo" / "curated" / "a"
    discovered.mkdir(parents=True)
    documents = [discovered / f"doc{i}.txt" for i in range(1, 4)]
    for document in documents:
        document.write_text(document.name, encoding="utf-8")
    output = tmp_path / "out"

    first = _invoke(schema_path, output, str(discovered.parent), "-n", "1")
    assert first.exit_code == 0, first.output
    assert sorted(controlled_extraction) == ["doc1.txt"]
    prior = (output / "a/doc1.json").read_bytes()
    controlled_extraction.clear()

    second = _invoke(schema_path, output, str(discovered.parent), "-n", "1")
    assert second.exit_code == 0, second.output
    assert sorted(controlled_extraction) == ["doc2.txt"]
    assert (output / "a/doc1.json").read_bytes() == prior
    assert (output / "a/doc2.json").is_file()
    assert not (output / "a/doc3.json").exists()


def test_curated_preserves_partition_subdirs(
    tmp_path: Path,
    schema_path: Path,
    controlled_extraction,
) -> None:
    """Equal basenames in separate partitions retain independent records."""
    root = tmp_path / "discovered" / "demo" / "curated"
    documents = []
    for partition in ("p1", "p2"):
        part_dir = root / partition
        part_dir.mkdir(parents=True, exist_ok=True)
        document = part_dir / "ordinance.txt"
        document.write_text(partition, encoding="utf-8")
        documents.append(document)
    output = tmp_path / "out"

    result = _invoke(schema_path, output, str(root))
    assert result.exit_code == 0, result.output
    assert sorted(controlled_extraction) == ["p1", "p2"]
    records = [
        json.loads((output / partition / "ordinance.json").read_text())
        for partition in ("p1", "p2")
    ]
    assert [record["document"]["source_path"] for record in records] == [
        str(document) for document in documents
    ]
    assert records[0]["record_id"] != records[1]["record_id"]
    assert not (output / "ordinance.json").exists()


def test_invalid_existing_json_is_pending_before_limit(
    tmp_path: Path,
    schema_path: Path,
    controlled_extraction,
) -> None:
    """An unverified first record must consume the pending-work limit."""
    docs = tmp_path / "documents"
    docs.mkdir()
    for name in ("doc1", "doc2"):
        (docs / f"{name}.txt").write_text(f"{name}.txt", encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    (output / "doc1.json").write_text("{}", encoding="utf-8")

    result = _invoke(schema_path, output, str(docs), "--limit", "1")
    assert result.exit_code == 0, result.output
    assert sorted(controlled_extraction) == ["doc1.txt"]
    assert json.loads((output / "doc1.json").read_text())["contract_version"] == "1.1.0"
    assert not (output / "doc2.json").exists()
