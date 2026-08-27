from __future__ import annotations

import csv
from pathlib import Path

from click.testing import CliRunner

from psweep.cli.main import cli

SCHEMA_PATH = Path("schemas/personal/geothermal_ordinance_schema.json")


def _stub_extract(monkeypatch):
    seen: list[str] = []

    def _fake_extract_one(doc_path, **kwargs):
        seen.append(Path(doc_path).name)
        return {
            "success": True,
            "file": Path(doc_path).name,
            "items": 0,
            "cost": 0.0,
            "time": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "output_path": "",
        }

    monkeypatch.setattr(
        "psweep.cli.commands_extract._extract_one_document", _fake_extract_one
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("psweep.utils.config._global_config", None)
    return seen


def test_limit_selects_next_unprocessed_documents(tmp_path: Path, monkeypatch) -> None:
    seen = _stub_extract(monkeypatch)
    docs = tmp_path / "documents"
    docs.mkdir()
    for i in range(1, 5):
        (docs / f"doc{i}.txt").write_text("hello", encoding="utf-8")

    out_dir = tmp_path / "extracted"
    out_dir.mkdir()
    (out_dir / "doc1.json").write_text("{}", encoding="utf-8")
    (out_dir / "doc2.json").write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "extract",
            str(docs),
            "--schema",
            str(SCHEMA_PATH),
            "--output",
            str(out_dir),
            "-n",
            "2",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == ["doc3.txt", "doc4.txt"]


def test_from_index_limit_selects_next_unprocessed(tmp_path: Path, monkeypatch) -> None:
    seen = _stub_extract(monkeypatch)
    discovered = tmp_path / "discovered" / "demo" / "curated" / "a"
    discovered.mkdir(parents=True)
    for i in range(1, 4):
        (discovered / f"doc{i}.txt").write_text("hello", encoding="utf-8")

    index_path = tmp_path / "download_index.csv"
    with index_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["status", "path", "domain", "relative_path"]
        )
        writer.writeheader()
        for i in range(1, 4):
            writer.writerow(
                {
                    "status": "downloaded",
                    "path": (discovered / f"doc{i}.txt").as_posix(),
                    "domain": "demo",
                    "relative_path": f"a/doc{i}.txt",
                }
            )

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "doc1.json").write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "extract",
            "--from-index",
            str(index_path),
            "--schema",
            str(SCHEMA_PATH),
            "--output",
            str(out_dir),
            "-n",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == ["doc2.txt"]


def test_from_index_preserves_partition_subdirs(tmp_path: Path, monkeypatch) -> None:
    seen = _stub_extract(monkeypatch)
    root = tmp_path / "discovered" / "demo" / "curated"
    for partition in ("p1", "p2"):
        part_dir = root / partition
        part_dir.mkdir(parents=True, exist_ok=True)
        (part_dir / "ordinance.txt").write_text(partition, encoding="utf-8")

    index_path = tmp_path / "download_index.csv"
    with index_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["status", "path", "domain", "relative_path"]
        )
        writer.writeheader()
        for partition in ("p1", "p2"):
            writer.writerow(
                {
                    "status": "downloaded",
                    "path": (root / partition / "ordinance.txt").as_posix(),
                    "domain": "demo",
                    "relative_path": f"{partition}/ordinance.txt",
                }
            )

    out_dir = tmp_path / "out"
    result = CliRunner().invoke(
        cli,
        [
            "extract",
            "--from-index",
            str(index_path),
            "--schema",
            str(SCHEMA_PATH),
            "--output",
            str(out_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(seen) == 2
    assert (out_dir / "p1").exists()
    assert (out_dir / "p2").exists()
