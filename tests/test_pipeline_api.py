from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import yaml

from click.testing import CliRunner

from psweep.cli.main import cli
from psweep.exceptions import PipelineError
from psweep.outcomes import EXIT_PARTIAL

import pytest
from psweep.config import RuntimeConfigError

from psweep.workspace import Workspace
from psweep.pipeline import (
    build_run_stage_commands,
    compile_extractions,
    compiled_table_fresh,
    count_curated_documents,
    count_extracted_documents,
    extract_documents,
    resolve_run_discovery_enabled,
    run_pipeline,
)

from _helpers import EXAMPLE_CONFIG


@pytest.fixture(autouse=True)
def controlled_configuration(monkeypatch):
    """Keep factory tests isolated from local .env and real credentials."""
    config = SimpleNamespace(
        llm_config={
            "provider": "openai",
            "model": "gpt-fixture",
            "api_key": "fixture-key",
        }
    )
    monkeypatch.setattr("psweep.utils.config._global_config", config)
    monkeypatch.setattr(
        "psweep.cli.commands_extract.load_dotenv",
        lambda: None,
    )
    return config.llm_config


def test_extract_documents_uses_max_context_chars(
    monkeypatch,
    tmp_path: Path,
    controlled_configuration,
) -> None:
    controlled_configuration["base_url"] = "https://proxy.example.invalid/v1"
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
            azure_endpoint=None,
            azure_api_version=None,
            base_url=None,
            context_windows=None,
            reasoning_models=None,
            timeout=None,
        ) -> None:
            captured["model"] = model
            captured["provider"] = provider
            captured["max_context_chars"] = max_context_chars
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            captured["schema_metadata"] = schema_metadata
            captured["context_windows"] = context_windows
            captured["reasoning_models"] = reasoning_models
            captured["timeout"] = timeout

        def extract(self, text: str, schema: dict) -> SimpleNamespace:
            captured["text"] = text
            captured["schema"] = schema
            return SimpleNamespace(data={"items": [{"name": "a"}]}, cost=1.25)

    monkeypatch.setattr("psweep.extraction.DocumentExtractor", DummyExtractor)
    monkeypatch.setattr(
        "psweep.extraction.document_utils.extract_text_from_document",
        lambda doc, page_range=None, **_: "extracted text",
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
    assert captured["model"] == "gpt-fixture"
    assert captured["api_key"] == "fixture-key"
    assert captured["base_url"] == "https://proxy.example.invalid/v1"
    assert captured["context_windows"] is None
    assert captured["reasoning_models"] is None
    assert captured["timeout"] is None
    assert captured["text"] == "extracted text"
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


def test_compile_without_identity_reports_no_deduplication(tmp_path: Path) -> None:
    directory = tmp_path / "records"
    directory.mkdir()
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {
                        "main_data_array": "items",
                    }
                }
            }
        )
    )
    (directory / "record.json").write_text(
        json.dumps(
            {
                "payload": {
                    "items": [{"name": "same"}, {"name": "same"}],
                }
            }
        )
    )
    result = compile_extractions(
        directory, schema, output_dir=tmp_path / "output", report_format="csv"
    )
    assert result.total_rows == 2
    assert result.deduplicated is False


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
        "      urls: [https://example.com]\n",
        encoding="utf-8",
    )

    stage_cmds = build_run_stage_commands(
        config_path,
        base_cmd=["pixi", "run", "psweep"],
        skip_discover=False,
        skip_extract=False,
        fresh=True,
        target_limit=3,
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


def test_build_run_stage_commands_applies_target_limit_only_to_discover(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "example.yaml"
    config_path.write_text(
        "domain: example\ndiscovery:\n  targets:\n    - label: demo\n      query: test\n",
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


def test_build_run_stage_commands_uses_config_extract_input_without_target_limit(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "example.yaml"
    config_path.write_text(
        "domain: example\ndiscovery:\n  targets:\n    - label: demo\n      query: test\n",
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
        "domain: example\ndiscovery:\n  targets:\n    - label: demo\n      query: test\n",
        encoding="utf-8",
    )

    stage_cmds = build_run_stage_commands(
        config_path,
        base_cmd=["pixi", "run", "psweep"],
        extract_input_path="outputs/example/discovered/latest/curated",
    )
    extract_cmd = dict(stage_cmds)["extract"]
    assert extract_cmd[:6] == [
        "pixi",
        "run",
        "psweep",
        "extract",
        "outputs/example/discovered/latest/curated",
        "--config",
    ]


def test_build_run_stage_commands_without_discovery(tmp_path: Path) -> None:
    config_path = tmp_path / "example.yaml"
    config_path.write_text("domain: example\nschema: s.json\n", encoding="utf-8")

    stage_names = [
        name
        for name, _ in build_run_stage_commands(config_path, base_cmd=["pixi", "run", "psweep"])
    ]
    assert stage_names == ["extract", "compile"]


def test_resolve_run_discovery_enabled_rejects_split_override(
    tmp_path: Path,
) -> None:
    """Pipeline helpers do not restore retired implicit config overrides."""
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        "domain: example\nschema: schemas/example.json\n",
        encoding="utf-8",
    )
    (tmp_path / "discovery.yaml").write_text(
        "seeds:\n  - https://example.com/docs\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigError, match="section override"):
        resolve_run_discovery_enabled(config_path)


def test_build_run_stage_commands_skips_discover_without_discovery_config(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "example.yaml"
    config_path.write_text(
        "domain: example\nschema: s.json\n",
        encoding="utf-8",
    )

    assert resolve_run_discovery_enabled(config_path) is False
    stage_names = [
        name
        for name, _ in build_run_stage_commands(config_path, base_cmd=["pixi", "run", "psweep"])
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
            azure_endpoint=None,
            azure_api_version=None,
            base_url=None,
            context_windows=None,
            reasoning_models=None,
            timeout=None,
        ) -> None:
            pass

        def extract(self, text: str, schema: dict) -> SimpleNamespace:
            return SimpleNamespace(data={"items": [{"name": "a"}]}, cost=0.0)

    monkeypatch.setattr("psweep.extraction.DocumentExtractor", DummyExtractor)
    monkeypatch.setattr(
        "psweep.extraction.document_utils.extract_text_from_document",
        lambda doc, page_range=None, **_: seen_page_ranges.append((doc.name, page_range))
        or "extracted text",
    )

    result = extract_documents(
        docs_dir,
        schema_path,
        output_dir=tmp_path / "out",
        max_context=123,
        provider="openai",
        skip_existing=False,
        pages_csv=pages_csv,
    )

    assert result.successful == 1 and result.failed == 0
    assert seen_page_ranges == [("sample.txt", (5, 9))]
    record = json.loads((tmp_path / "out/sample.json").read_text())
    coverage = record["processing_metrics"]["coverage"]
    assert coverage["selected_page_range"] == [5, 9]


# Run orchestrators delegate artifact reuse and cleanup to their stage owners.


def _run_config(tmp_path, schema_path, *, discovery=True, validation=False):
    """Callers chdir to tmp_path so derived outputs land there."""
    output = tmp_path / Workspace("acme").extracted
    config = {
        "domain": "acme",
        "schema": str(schema_path),
    }
    if discovery:
        config["discovery"] = {
            "queries": ["{label} documents"],
            "targets": [{"label": "alpha"}, {"label": "beta"}],
        }
    if validation:
        (tmp_path / "labels.csv").write_text("label\nalpha\n", encoding="utf-8")
        config["validation"] = {"references": ["labels.csv"]}
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path, output


@pytest.mark.parametrize(
    "checkpoint",
    [
        None,
        "{not valid json",
        json.dumps({"other": 1}),
        json.dumps({"entries": {"alpha": {}, "beta": {}}}),
        json.dumps({"entries": {"alpha": {"artifacts": {"missing": "0" * 64}}}}),
    ],
)
def test_run_ui_does_not_predict_reuse_from_raw_checkpoint(
    tmp_path: Path,
    schema_path: Path,
    monkeypatch,
    checkpoint,
) -> None:
    """Missing, malformed and stale checkpoint keys are not skip evidence."""
    monkeypatch.chdir(tmp_path)
    config, _ = _run_config(tmp_path, schema_path)
    if checkpoint is not None:
        path = tmp_path / Workspace("acme").discovered / "checkpoint.json"
        path.parent.mkdir(parents=True)
        path.write_text(checkpoint, encoding="utf-8")
    plans = []
    monkeypatch.setattr(
        "psweep.cli.run_view.RunView.config",
        lambda _self, rows, **_kwargs: plans.append(rows),
    )
    subprocess = Mock(return_value=SimpleNamespace(returncode=1))
    monkeypatch.setattr("psweep.pipeline.subprocess.run", subprocess)

    result = CliRunner().invoke(cli, ["run", "--config", str(config), "--yes"])
    assert result.exit_code == 1, result.output
    assert plans[0]["Targets"] == "2 configured (reuse verified by discovery)"
    assert "remaining" not in plans[0]["Targets"].lower()
    assert "skip" not in plans[0]["Targets"].lower()
    assert subprocess.call_count == 1
    command = subprocess.call_args.args[0]
    assert "discover" in command
    assert "--fresh" not in command


@pytest.mark.parametrize("entrypoint", ["cli", "api"])
@pytest.mark.parametrize("existing", [False, True])
def test_parent_fresh_run_leaves_cleanup_to_extract_stage(
    tmp_path: Path,
    schema_path: Path,
    monkeypatch,
    entrypoint,
    existing,
) -> None:
    """Only the extract child may mutate the extraction output directory."""
    monkeypatch.chdir(tmp_path)
    config, output = _run_config(tmp_path, schema_path, discovery=False)
    sentinel = output / "prior.json"
    if existing:
        output.mkdir(parents=True)
        sentinel.write_text("previous verified output", encoding="utf-8")
    seen = []

    def child(command, **kwargs):
        seen.append(command)
        if existing:
            assert sentinel.read_text() == "previous verified output"
        else:
            assert not output.exists()
        # Simulate the extract child's failed preflight; compile must not run.
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr("psweep.pipeline.subprocess.run", child)
    if entrypoint == "cli":
        result = CliRunner().invoke(
            cli,
            ["run", "--config", str(config), "--fresh", "--yes"],
        )
        assert result.exit_code == 1, result.output
        assert "Stage 'extract' failed" in result.output
    else:
        with pytest.raises(PipelineError, match="Stage 'extract' failed"):
            run_pipeline(config, fresh=True)
    assert len(seen) == 1
    assert "extract" in seen[0] and "--fresh" in seen[0]
    if existing:
        assert sentinel.read_text() == "previous verified output"
    else:
        assert not output.exists()


@pytest.mark.parametrize("entrypoint", ["cli", "api"])
def test_partial_stage_continues_downstream_and_reports_partial(
    tmp_path: Path,
    schema_path: Path,
    monkeypatch,
    entrypoint,
) -> None:
    """A partial discover still feeds extraction; a failed stage stops the run."""
    monkeypatch.chdir(tmp_path)
    config, _ = _run_config(tmp_path, schema_path)
    codes = {"discover": EXIT_PARTIAL, "extract": 0, "compile": 0, "validate": 0}
    seen = []

    def child(command, **_kwargs):
        stage = next(name for name in codes if name in command)
        seen.append(stage)
        return SimpleNamespace(returncode=codes[stage])

    monkeypatch.setattr("psweep.pipeline.subprocess.run", child)
    if entrypoint == "cli":
        result = CliRunner().invoke(cli, ["run", "--config", str(config), "--yes"])
        assert result.exit_code == EXIT_PARTIAL, result.output
        assert "partial (discover had errors" in result.output
    else:
        assert run_pipeline(config).partial_stages == ["discover"]
    assert seen[:2] == ["discover", "extract"] and "compile" in seen

    seen.clear()
    codes["discover"] = 1
    if entrypoint == "cli":
        result = CliRunner().invoke(cli, ["run", "--config", str(config), "--yes"])
        assert result.exit_code == 1, result.output
        assert "Stage 'discover' failed" in result.output
    else:
        with pytest.raises(PipelineError, match="Stage 'discover' failed"):
            run_pipeline(config)
    assert seen == ["discover"]


def test_count_curated_documents_skips_sidecars(tmp_path: Path) -> None:
    d = tmp_path / "curated"
    d.mkdir()
    (d / "a.pdf").write_text("x", encoding="utf-8")
    (d / "a.pdf.text").write_text("x", encoding="utf-8")
    (d / ".review").mkdir()
    (d / ".review" / "a.pdf.json").write_text("{}", encoding="utf-8")
    (d / ".discovery-publication.json").write_text("{}", encoding="utf-8")
    assert count_curated_documents(d) == 1


def test_count_extracted_documents_excludes_manifests(tmp_path: Path) -> None:
    d = tmp_path / "extracted"
    (d / "run_manifests").mkdir(parents=True)
    (d / "a.json").write_text("{}", encoding="utf-8")
    (d / "b.json").write_text("{}", encoding="utf-8")
    (d / "run_manifests" / "m.json").write_text("{}", encoding="utf-8")
    assert count_extracted_documents(d) == 2


@pytest.mark.parametrize(
    ("sections", "skip_extract", "stages"),
    [
        ({"discovery": False}, False, ["extract", "compile"]),
        ({"validation": True}, False, ["discover", "extract", "compile", "validate"]),
        ({"validation": True}, True, ["discover", "compile"]),
    ],
)
def test_run_stages_validate_after_compile_with_a_validation_section(
    tmp_path, schema_path, sections, skip_extract, stages
) -> None:
    config_path, _ = _run_config(tmp_path, schema_path, **sections)
    stage_cmds = build_run_stage_commands(
        config_path, base_cmd=["psweep"], skip_extract=skip_extract, fresh=True
    )
    assert [name for name, _ in stage_cmds] == stages
    if stages[-1] == "validate":
        assert stage_cmds[-1][1] == ["psweep", "validate", "--config", str(config_path), "--fresh"]


def test_example_config_runs_discover_extract_compile() -> None:
    stage_cmds = build_run_stage_commands(EXAMPLE_CONFIG, base_cmd=["psweep"])
    assert [name for name, _ in stage_cmds] == ["discover", "extract", "compile"]


def test_compiled_table_fresh_tracks_records_table_and_manifest(tmp_path: Path) -> None:
    extracted, output = tmp_path / "extracted", tmp_path / "compiled"
    extracted.mkdir()
    record = extracted / "record.json"
    record.write_text(json.dumps({"payload": {"items": [{"name": "a"}]}}), encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {"main_data_array": "items"},
                    "identity": {"deduplication": {"key_fields": ["name"]}},
                }
            }
        ),
        encoding="utf-8",
    )

    def fresh() -> bool:
        return compiled_table_fresh(extracted, schema, config_path=None, output_dir=output)

    def compile_() -> Path:
        result = compile_extractions(extracted, schema, output_dir=output, report_format="csv")
        return result.output_files[0]

    assert not fresh()
    table = compile_()
    assert fresh()
    table.write_text(table.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    assert not fresh()
    compile_()
    record.write_text(json.dumps({"payload": {"items": [{"name": "b"}]}}), encoding="utf-8")
    assert not fresh()
    compile_()
    assert fresh()
    manifest = max((output / "run_manifests").glob("*.manifest.json"))
    data = json.loads(manifest.read_text(encoding="utf-8"))
    del data["lineage"]["build_fingerprint"]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert not fresh()
