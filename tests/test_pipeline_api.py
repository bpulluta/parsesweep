from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from psweep.pipeline import (
    build_run_stage_commands,
    clear_run_extract_output,
    compile_extractions,
    count_curated_documents,
    count_extracted_documents,
    extract_documents,
    read_checkpoint_entries,
    resolve_run_discovery_enabled,
    resolve_run_extraction_dir,
    resolve_run_validation,
    run_checkpoint_path,
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
    config_path.write_text(
        "domain: example\n"
        "discovery:\n"
        "  targets:\n"
        "    - label: demo\n"
        "      url: https://example.com\n",
        encoding="utf-8",
    )

    stage_cmds = build_run_stage_commands(
        config_path,
        base_cmd=["pixi", "run", "psweep"],
        skip_discover=False,
        skip_extract=False,
        fresh=True,
        target_limit=3,
        retention_documents="curated",
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
                "--target-limit",
                "3",
                "--retention-documents",
                "curated",
                "--fresh",
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
                "--fresh",
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
                "--fresh",
            ],
        ),
    ]


def test_build_run_stage_commands_wires_validation(tmp_path: Path) -> None:
    config_path = tmp_path / "example.yaml"
    config_path.write_text(
        "domain: example\n"
        "discovery:\n"
        "  targets:\n"
        "    - label: demo\n"
        "      url: https://example.com\n"
        "extraction:\n"
        "  schema: schemas/example.json\n"
        "  output_dir: extracted/example\n"
        "validation:\n"
        "  models: [primary, secondary]\n",
        encoding="utf-8",
    )

    resolved = resolve_run_validation(config_path)
    assert resolved is not None
    assert resolved["validation_dir"] == Path("extracted/example/validation")

    stage_cmds = build_run_stage_commands(
        config_path,
        base_cmd=["pixi", "run", "psweep"],
    )

    stage_names = [name for name, _ in stage_cmds]
    assert stage_names == ["discover", "extract", "validate", "compile"]

    validate_cmd = dict(stage_cmds)["validate"]
    assert validate_cmd[:4] == ["pixi", "run", "psweep", "validate"]
    assert validate_cmd[validate_cmd.index("--config") + 1] == str(config_path)


def test_build_run_stage_commands_propagates_fresh_to_validation(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "example.yaml"
    config_path.write_text(
        "domain: example\n"
        "extraction:\n"
        "  schema: schemas/example.json\n"
        "validation:\n"
        "  models: [primary, secondary]\n",
        encoding="utf-8",
    )

    stage_cmds = build_run_stage_commands(
        config_path,
        base_cmd=["pixi", "run", "psweep"],
        fresh=True,
    )
    validate_cmd = dict(stage_cmds)["validate"]
    assert "--fresh" in validate_cmd


def test_build_run_stage_commands_applies_target_limit_only_to_discover(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "example.yaml"
    config_path.write_text(
        "domain: example\n"
        "discovery:\n"
        "  targets:\n"
        "    - label: demo\n"
        "      query: test\n",
        encoding="utf-8",
    )

    stage_cmds = build_run_stage_commands(
        config_path,
        base_cmd=["pixi", "run", "psweep"],
        target_limit=2,
    )
    cmd_map = dict(stage_cmds)
    assert "--target-limit" in cmd_map["discover"]
    assert cmd_map["discover"][cmd_map["discover"].index("--target-limit") + 1] == "2"
    assert "--target-limit" not in cmd_map["extract"]
    assert "--target-limit" not in cmd_map["compile"]


def test_build_run_stage_commands_applies_retention_only_to_discover(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "example.yaml"
    config_path.write_text(
        "domain: example\n"
        "discovery:\n"
        "  targets:\n"
        "    - label: demo\n"
        "      query: test\n",
        encoding="utf-8",
    )

    stage_cmds = build_run_stage_commands(
        config_path,
        base_cmd=["pixi", "run", "psweep"],
        retention_documents="none",
    )
    cmd_map = dict(stage_cmds)
    assert "--retention-documents" in cmd_map["discover"]
    assert (
        cmd_map["discover"][cmd_map["discover"].index("--retention-documents") + 1]
        == "none"
    )
    assert "--retention-documents" not in cmd_map["extract"]
    assert "--retention-documents" not in cmd_map["compile"]


def test_build_run_stage_commands_uses_config_extract_input_without_target_limit(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "example.yaml"
    config_path.write_text(
        "domain: example\n"
        "discovery:\n"
        "  targets:\n"
        "    - label: demo\n"
        "      query: test\n",
        encoding="utf-8",
    )

    stage_cmds = build_run_stage_commands(
        config_path,
        base_cmd=["pixi", "run", "psweep"],
    )
    extract_cmd = dict(stage_cmds)["extract"]
    assert extract_cmd[:5] == ["pixi", "run", "psweep", "extract", "--config"]


def test_build_run_stage_commands_wires_extract_input_path_when_provided(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "example.yaml"
    config_path.write_text(
        "domain: example\n"
        "discovery:\n"
        "  targets:\n"
        "    - label: demo\n"
        "      query: test\n",
        encoding="utf-8",
    )

    stage_cmds = build_run_stage_commands(
        config_path,
        base_cmd=["pixi", "run", "psweep"],
        extract_input_path="discovered/example/latest/curated",
    )
    extract_cmd = dict(stage_cmds)["extract"]
    assert extract_cmd[:6] == [
        "pixi",
        "run",
        "psweep",
        "extract",
        "discovered/example/latest/curated",
        "--config",
    ]


def test_build_run_stage_commands_no_validation_when_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "example.yaml"
    config_path.write_text(
        "domain: example\nextraction:\n  schema: s.json\n", encoding="utf-8"
    )

    assert resolve_run_validation(config_path) is None
    stage_names = [
        name
        for name, _ in build_run_stage_commands(
            config_path, base_cmd=["pixi", "run", "psweep"]
        )
    ]
    assert stage_names == ["extract", "compile"]


def test_resolve_run_discovery_enabled_reads_split_override(tmp_path: Path) -> None:
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        "domain: example\n"
        "extraction:\n"
        "  schema: schemas/example.json\n",
        encoding="utf-8",
    )
    (tmp_path / "discovery.yaml").write_text(
        "seeds:\n"
        "  - https://example.com/docs\n",
        encoding="utf-8",
    )

    assert resolve_run_discovery_enabled(config_path) is True


def test_build_run_stage_commands_skips_discover_without_discovery_config(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "example.yaml"
    config_path.write_text(
        "domain: example\n"
        "extraction:\n"
        "  schema: s.json\n",
        encoding="utf-8",
    )

    assert resolve_run_discovery_enabled(config_path) is False
    stage_names = [
        name
        for name, _ in build_run_stage_commands(
            config_path, base_cmd=["pixi", "run", "psweep"]
        )
    ]
    assert stage_names == ["extract", "compile"]


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


# ── run-command on-disk helpers (extracted from app.py run) ──────────────────


def test_run_checkpoint_path_uses_domain_convention() -> None:
    assert run_checkpoint_path("acme") == Path(
        "discovered/acme/checkpoint.json"
    )


def test_read_checkpoint_entries_missing_file(tmp_path: Path) -> None:
    assert read_checkpoint_entries(tmp_path / "nope.json") == []


def test_read_checkpoint_entries_returns_keys(tmp_path: Path) -> None:
    cp = tmp_path / "checkpoint.json"
    cp.write_text(
        json.dumps({"entries": {"target-a": {}, "target-b": {}}}),
        encoding="utf-8",
    )
    assert sorted(read_checkpoint_entries(cp)) == ["target-a", "target-b"]


def test_read_checkpoint_entries_malformed_json(tmp_path: Path) -> None:
    cp = tmp_path / "checkpoint.json"
    cp.write_text("{not valid json", encoding="utf-8")
    assert read_checkpoint_entries(cp) == []


def test_read_checkpoint_entries_no_entries_key(tmp_path: Path) -> None:
    cp = tmp_path / "checkpoint.json"
    cp.write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert read_checkpoint_entries(cp) == []


def test_resolve_run_extraction_dir_uses_config_value() -> None:
    cfg = {"extraction": {"output_dir": "custom/out"}}
    assert resolve_run_extraction_dir(cfg, "acme") == Path("custom/out")


def test_resolve_run_extraction_dir_defaults_to_domain() -> None:
    assert resolve_run_extraction_dir({}, "acme") == Path("extracted/acme")


def test_clear_run_extract_output_removes_existing(tmp_path: Path) -> None:
    d = tmp_path / "extracted" / "acme"
    d.mkdir(parents=True)
    (d / "doc.json").write_text("{}", encoding="utf-8")
    assert clear_run_extract_output(d) is True
    assert not d.exists()


def test_clear_run_extract_output_missing_dir(tmp_path: Path) -> None:
    assert clear_run_extract_output(tmp_path / "nope") is False


def test_count_curated_documents_skips_sidecars(tmp_path: Path) -> None:
    d = tmp_path / "curated"
    d.mkdir()
    (d / "a.pdf").write_text("x", encoding="utf-8")
    (d / "a.pdf.text").write_text("x", encoding="utf-8")
    (d / "meta.json").write_text("{}", encoding="utf-8")
    assert count_curated_documents(d) == 1


def test_count_extracted_documents_excludes_manifests(tmp_path: Path) -> None:
    d = tmp_path / "extracted"
    (d / "run_manifests").mkdir(parents=True)
    (d / "a.json").write_text("{}", encoding="utf-8")
    (d / "b.json").write_text("{}", encoding="utf-8")
    (d / "run_manifests" / "m.json").write_text("{}", encoding="utf-8")
    assert count_extracted_documents(d) == 2
