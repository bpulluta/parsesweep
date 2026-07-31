from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from psweep.pipeline import (
    build_run_stage_commands,
    compile_extractions,
    extract_documents,
)


def test_extract_documents_uses_max_context_chars(
    monkeypatch, tmp_path: Path
) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "sample.txt").write_text("sample document", encoding="utf-8")

    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {
                        "main_data_array": "items",
                        "identifier_fields": ["name"],
                    },
                    "identity": {
                        "deduplication": {
                            "key_fields": ["name"],
                            "ignore_fields": [],
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    class DummyExtractor:
        def __init__(
            self,
            *,
            model: str,
            provider: str,
            max_context_chars: int,
            schema_metadata=None,
            api_key=None,
        ) -> None:
            captured["model"] = model
            captured["provider"] = provider
            captured["max_context_chars"] = max_context_chars

        def extract(self, text: str, schema: dict) -> SimpleNamespace:
            captured["text"] = text
            captured["schema"] = schema
            return SimpleNamespace(data={"items": [{"name": "a"}]}, cost=1.25)

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("psweep.extraction.DocumentExtractor", DummyExtractor)
    monkeypatch.setattr(
        "psweep.extraction.document_utils.extract_text_from_document",
        lambda doc, page_range=None: "extracted text",
    )

    result = extract_documents(
        docs_dir,
        schema_path,
        output_dir=tmp_path / "out",
        max_context=123,
        provider="openai",
        skip_existing=False,
    )

    assert captured["max_context_chars"] == 123
    assert captured["provider"] == "openai"
    assert result.total == 1
    assert result.successful == 1
    assert result.failed == 0
    output_record = json.loads((tmp_path / "out" / "sample.json").read_text())
    assert output_record["payload"] == {"items": [{"name": "a"}]}
    assert output_record["lineage"]["provider"] == "openai"

    compiled = compile_extractions(
        tmp_path / "out",
        schema_path,
        output_dir=tmp_path / "compiled",
        report_format="csv",
    )
    assert compiled.total_rows == 1
    assert compiled.output_files[0].exists()


def test_compile_extractions_writes_requested_outputs(tmp_path: Path) -> None:
    extraction_dir = tmp_path / "extracted"
    extraction_dir.mkdir()
    (extraction_dir / "sample.json").write_text(
        json.dumps(
            {
                "payload": {
                    "items": [
                        {
                            "name": "a",
                            "type": "x",
                        }
                    ]
                },
                "lineage": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "run_id": "run://123",
                    "artifact_id": "artifact://123",
                },
                "quality": {"errors": []},
            }
        ),
        encoding="utf-8",
    )

    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {
                        "main_data_array": "items",
                        "identifier_fields": ["name"],
                    },
                    "identity": {
                        "deduplication": {
                            "key_fields": ["name"],
                            "ignore_fields": [],
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    result = compile_extractions(
        extraction_dir,
        schema_path,
        output_dir=tmp_path / "compiled",
        report_format="csv",
    )

    assert result.total_rows == 1
    assert result.deduplicated is True
    assert result.output_files == [tmp_path / "compiled" / "extracted.csv"]
    assert result.output_files[0].exists()


def test_build_run_stage_commands_preserves_flags(tmp_path: Path) -> None:
    config_path = tmp_path / "run.yaml"
    config_path.write_text("domain: example\n", encoding="utf-8")

    stage_cmds = build_run_stage_commands(
        config_path,
        base_cmd=["pixi", "run", "psweep"],
        skip_discover=False,
        skip_extract=False,
        reprocess=True,
        extra_flags=("-q",),
    )

    assert stage_cmds == [
        (
            "discover",
            [
                "pixi",
                "run",
                "psweep",
                "discover",
                "--config",
                str(config_path),
                "-q",
            ],
        ),
        (
            "extract",
            [
                "pixi",
                "run",
                "psweep",
                "extract",
                "--config",
                str(config_path),
                "-q",
                "--reprocess",
            ],
        ),
        (
            "compile",
            [
                "pixi",
                "run",
                "psweep",
                "compile",
                "--config",
                str(config_path),
                "-q",
            ],
        ),
    ]


def test_extract_documents_uses_page_range_csv(tmp_path: Path, monkeypatch) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "sample.txt").write_text("sample document", encoding="utf-8")

    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {
                        "main_data_array": "items",
                        "identifier_fields": ["name"],
                    },
                    "identity": {
                        "deduplication": {
                            "key_fields": ["name"],
                            "ignore_fields": [],
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    pages_csv = tmp_path / "pages.csv"
    pages_csv.write_text("file_path,start_page,end_page\nsample.txt,5,9\n", encoding="utf-8")

    seen_page_ranges: list[tuple[str, tuple[int, int] | None]] = []

    class DummyExtractor:
        def __init__(
            self,
            *,
            model: str,
            provider: str,
            max_context_chars: int,
            schema_metadata=None,
            api_key=None,
        ) -> None:
            pass

        def extract(self, text: str, schema: dict) -> SimpleNamespace:
            return SimpleNamespace(data={"items": [{"name": "a"}]}, cost=0.0)

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("psweep.extraction.DocumentExtractor", DummyExtractor)
    monkeypatch.setattr(
        "psweep.extraction.document_utils.extract_text_from_document",
        lambda doc, page_range=None: seen_page_ranges.append((doc.name, page_range))
        or "extracted text",
    )

    extract_documents(
        docs_dir,
        schema_path,
        output_dir=tmp_path / "out",
        max_context=123,
        provider="openai",
        skip_existing=False,
        pages_csv=pages_csv,
    )

    assert seen_page_ranges == [("sample.txt", (5, 9))]
