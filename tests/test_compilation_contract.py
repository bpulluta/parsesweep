"""Offline regressions for effective compilation preflight and export writes."""

import json
from unittest.mock import Mock

import pandas as pd
import pytest
import yaml
from click.testing import CliRunner

from psweep.cli.main import cli
from psweep.compilation.data_compiler import DataCompiler
from psweep.pipeline import compile_extractions
from psweep.utils.schema_metadata import SchemaMetadata


@pytest.fixture
def contract_schema(tmp_path):
    schema = {
        "type": "object",
        "properties": {
            "source": {"type": "object", "properties": {"url": {"type": "string"}}},
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "kind": {"type": "string"}, "summary": {"type": "string"},
                "quote": {"type": "string"}, "details": {"type": "array"},
            }}},
        },
        "$metadata": {
            "extraction": {"main_data_array": "items", "context_objects": ["source"],
                           "identifier_fields": ["source.url"]},
            "identity": {"deduplication": {"key_fields": ["quote"],
                                           "partition_fields": ["source.url"]}},
        },
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    return path


def _config(tmp_path, schema, **compilation):
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump({"domain": "synthetic", "compilation": {
        "schema": str(schema), "input_dir": str(tmp_path / "input"),
        "output_dir": str(tmp_path / "output"), **compilation,
    }}))
    return path


def test_invalid_schema_owned_output_fails_before_fresh_or_calls(
    tmp_path, contract_schema, monkeypatch
):
    schema = json.loads(contract_schema.read_text())
    schema["$metadata"]["compilation"] = {"output": {"default_format": "invalid"}}
    contract_schema.write_text(json.dumps(schema))
    (tmp_path / "input").mkdir()
    (tmp_path / "output").mkdir()
    sentinel = tmp_path / "output" / "keep.txt"
    sentinel.write_text("previous output")
    cfg = _config(tmp_path, contract_schema, synthesis={
        "enabled": True, "group_by": ["source.url"], "model": "fake",
    })
    build_client = Mock(side_effect=AssertionError("must not call LLM factory"))
    monkeypatch.setattr("psweep.extraction.llm_factory.build_llm_client", build_client)
    result = CliRunner().invoke(cli, ["compile", "--config", str(cfg), "--fresh"])
    assert result.exit_code != 0
    assert "default_format" in result.output
    assert sentinel.read_text() == "previous output"
    build_client.assert_not_called()
    validation = CliRunner().invoke(cli, ["compile", "--config", str(cfg), "--validate-config"])
    assert validation.exit_code != 0
    assert "default_format" in validation.output


def test_unknown_output_setting_fails_validation_and_library_before_scanning(tmp_path, contract_schema, monkeypatch):
    cfg = _config(tmp_path, contract_schema, output={"column_orrder": ["quote"]})
    scan = Mock(side_effect=AssertionError("must not scan inputs"))
    monkeypatch.setattr(DataCompiler, "compile_from_directory", scan)
    result = CliRunner().invoke(cli, ["compile", "--config", str(cfg), "--validate-config"])
    assert result.exit_code != 0
    assert "column_orrder" in result.output
    (tmp_path / "input").mkdir()
    with pytest.raises(ValueError, match="column_orrder"):
        compile_extractions(tmp_path / "input", contract_schema, config_path=cfg,
                            output_dir=tmp_path / "output")
    assert not (tmp_path / "output").exists()
    scan.assert_not_called()


def test_schema_format_and_runtime_format_precedence(contract_schema):
    schema = json.loads(contract_schema.read_text())
    schema["$metadata"]["compilation"] = {"output": {"default_format": "csv"}}
    contract_schema.write_text(json.dumps(schema))
    assert SchemaMetadata.for_compilation(contract_schema, {}).get_output_formats() == ["csv"]
    assert SchemaMetadata.for_compilation(contract_schema, {
        "compilation_output": {"default_format": "both"},
    }).get_output_formats() == ["csv", "excel"]
    del schema["$metadata"]["compilation"]
    contract_schema.write_text(json.dumps(schema))
    assert SchemaMetadata.for_compilation(contract_schema, {}).get_output_formats() == ["csv", "excel"]


def test_export_prepares_once_and_checks_excel_limit_before_either_write(
    tmp_path, contract_schema, monkeypatch
):
    compiler = DataCompiler(SchemaMetadata(contract_schema), verbose=False)
    prepare = Mock(return_value=pd.DataFrame({"quote": ["Evidence"]}))
    monkeypatch.setattr(compiler, "_prepare_output_dataframe", prepare)
    validate = Mock(wraps=compiler.validate_export)
    monkeypatch.setattr(compiler, "validate_export", validate)
    paths, frame = compiler.export(pd.DataFrame({"Quote": ["Evidence"]}), tmp_path, "facts", ["csv", "excel"])
    assert len(paths) == 2
    assert frame.columns.tolist() == ["quote"]
    prepare.assert_called_once()
    validate.assert_called_once()
    monkeypatch.setattr("psweep.compilation.data_compiler.EXCEL_MAX_DATA_ROWS", 0)
    csv_write = Mock()
    excel_write = Mock()
    monkeypatch.setattr(compiler.csv_exporter, "save", csv_write)
    monkeypatch.setattr(compiler.excel_formatter, "save", excel_write)
    with pytest.raises(ValueError, match="CSV"):
        compiler.export(frame, tmp_path / "too-large", "facts", ["csv", "excel"])
    assert not (tmp_path / "too-large").exists()
    csv_write.assert_not_called()
    excel_write.assert_not_called()


def test_compiler_reads_each_input_once_and_skips_internal_sidecars(
    tmp_path, contract_schema, monkeypatch
):
    import psweep.compilation.data_compiler as module

    directory = tmp_path / "records"
    directory.mkdir()
    (directory / "record.json").write_text(json.dumps({"payload": {
        "source": {"url": "https://example.org/a"},
        "items": [{"quote": "Evidence", "kind": "observation", "summary": "Summary"}],
    }}))
    sidecars = directory / "validation"
    sidecars.mkdir()
    (sidecars / "record.json").write_text("must not read")
    original = module.json.load
    loads = Mock(side_effect=original)
    monkeypatch.setattr(module.json, "load", loads)
    compiler = DataCompiler(SchemaMetadata(contract_schema), verbose=False)
    frame, _ = compiler.compile_from_directory(directory)
    assert len(frame) == 1
    assert loads.call_count == 1


def test_wrapper_volatility_does_not_hide_nested_evidence_conflicts(tmp_path, contract_schema):
    directory = tmp_path / "records"
    directory.mkdir()
    record = {"payload": {"source": {"url": "https://example.org/a"},
                         "items": [{"quote": "Evidence", "details": {"amount": 1}}]},
              "lineage": {"extracted_at": "2024-01-01T00:00:00Z"}}
    second = json.loads(json.dumps(record))
    second["lineage"]["extracted_at"] = "2024-01-02T00:00:00Z"
    (directory / "first.json").write_text(json.dumps(record))
    (directory / "second.json").write_text(json.dumps(second))
    compiler = DataCompiler(SchemaMetadata(contract_schema), verbose=False)
    frame, _ = compiler.compile_from_directory(directory, apply_deduplication=False)
    assert compiler.deduplicator.preview_deduplication(frame)["suspicious_groups_count"] == 0
    second["payload"]["items"][0]["details"]["amount"] = 2
    (directory / "second.json").write_text(json.dumps(second))
    frame, _ = compiler.compile_from_directory(directory, apply_deduplication=False)
    assert compiler.deduplicator.preview_deduplication(frame)["suspicious_groups_count"] == 1


def test_excel_cell_overflow_fails_before_fresh_or_any_writer(tmp_path, contract_schema, monkeypatch):
    compiler = DataCompiler(SchemaMetadata(contract_schema), verbose=False)
    frame = pd.DataFrame({"data": ["x" * 32768]})
    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("previous export")
    monkeypatch.setattr(compiler, "_prepare_output_dataframe", lambda df: df)
    csv_write = Mock()
    monkeypatch.setattr(compiler, "save_csv", csv_write)
    with pytest.raises(ValueError, match="32,767|32767"):
        compiler.export(frame, output, "facts", ["csv", "excel"], fresh=True)
    csv_write.assert_not_called()
    assert sentinel.read_text() == "previous export"


def test_cli_dry_run_checks_excel_evidence_limit(tmp_path, contract_schema):
    directory = tmp_path / "input"
    directory.mkdir()
    (directory / "record.json").write_text(json.dumps({"payload": {
        "source": {"url": "https://example.org/a"},
        "items": [{"quote": "x" * 32768, "kind": "observation", "summary": "Summary"}],
    }}))
    cfg = _config(tmp_path, contract_schema)
    result = CliRunner().invoke(cli, ["compile", "--config", str(cfg), "--dry-run"])
    assert result.exit_code != 0
    assert "Excel cells" in result.output


def _compiler(contract_schema):
    return DataCompiler(SchemaMetadata(contract_schema), verbose=False)


def _no_staging_leak(parent):
    return not list(parent.glob(".psweep-export-*"))


def test_fresh_writer_failure_keeps_prior_output_and_leaves_no_staging(
    tmp_path, contract_schema, monkeypatch
):
    compiler = _compiler(contract_schema)
    output = tmp_path / "output"
    output.mkdir()
    (output / "facts.csv").write_text("OLD CSV BYTES")
    (output / "keep.txt").write_text("unrelated")
    link = output / "link.txt"
    link.symlink_to(output / "keep.txt")
    monkeypatch.setattr(
        compiler, "save_excel",
        Mock(side_effect=OSError("excel writer blew up")),
    )
    with pytest.raises(ValueError):
        compiler.export(
            pd.DataFrame({"quote": ["x"]}), output, "facts",
            ["csv", "excel"], fresh=True,
        )
    assert (output / "facts.csv").read_text() == "OLD CSV BYTES"
    assert (output / "keep.txt").read_text() == "unrelated"
    assert link.is_symlink()
    assert _no_staging_leak(tmp_path)


def test_nonfresh_writer_failure_keeps_requested_and_unrelated_files(
    tmp_path, contract_schema, monkeypatch
):
    # The destructive case: the first writer (CSV) succeeds and the second
    # (Excel) fails. A direct write would have already clobbered the old CSV;
    # staging must leave every real output byte-for-byte intact.
    compiler = _compiler(contract_schema)
    output = tmp_path / "output"
    output.mkdir()
    (output / "facts.csv").write_text("OLD CSV BYTES")
    (output / "facts.xlsx").write_bytes(b"OLD XLSX BYTES")
    (output / "keep.txt").write_text("unrelated")
    monkeypatch.setattr(
        compiler, "save_excel",
        Mock(side_effect=OSError("excel writer blew up")),
    )
    with pytest.raises(ValueError):
        compiler.export(
            pd.DataFrame({"quote": ["x"]}), output, "facts",
            ["csv", "excel"], fresh=False,
        )
    assert (output / "facts.csv").read_text() == "OLD CSV BYTES"
    assert (output / "facts.xlsx").read_bytes() == b"OLD XLSX BYTES"
    assert (output / "keep.txt").read_text() == "unrelated"
    assert _no_staging_leak(tmp_path)


def test_nonfresh_publication_failure_rolls_back_each_requested_file(
    tmp_path, contract_schema, monkeypatch
):
    import pathlib

    compiler = _compiler(contract_schema)
    output = tmp_path / "output"
    output.mkdir()
    (output / "facts.csv").write_text("OLD CSV BYTES")
    (output / "facts.xlsx").write_bytes(b"OLD XLSX BYTES")
    (output / "keep.txt").write_text("unrelated")

    real_replace = pathlib.Path.replace

    def failing_replace(self, target):
        # Fail only when promoting the staged xlsx from the stage files/ dir
        # into the live output (after the csv was already promoted). Scoped to
        # files/ so the rollback's own backup/ restores are not disturbed.
        if (
            self.parent.name == "files"
            and ".psweep-export-" in str(self.parent.parent)
            and self.suffix == ".xlsx"
            and pathlib.Path(target).name == "facts.xlsx"
        ):
            raise OSError("simulated promotion failure")
        return real_replace(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", failing_replace)
    with pytest.raises(ValueError):
        compiler.export(
            pd.DataFrame({"quote": ["x"]}), output, "facts",
            ["csv", "excel"], fresh=False,
        )
    assert (output / "facts.csv").read_text() == "OLD CSV BYTES"
    assert (output / "facts.xlsx").read_bytes() == b"OLD XLSX BYTES"
    assert (output / "keep.txt").read_text() == "unrelated"
    assert _no_staging_leak(tmp_path)


def test_rollback_restore_failure_retains_backup_and_reports_it(
    tmp_path, contract_schema, monkeypatch
):
    import pathlib

    compiler = _compiler(contract_schema)
    output = tmp_path / "output"
    output.mkdir()
    (output / "facts.csv").write_text("OLD CSV BYTES")
    (output / "facts.xlsx").write_bytes(b"OLD XLSX BYTES")

    real_replace = pathlib.Path.replace

    def failing_replace(self, target):
        in_stage = ".psweep-export-" in str(self.parent.parent)
        # Fail promoting the staged xlsx (after csv promoted), then fail the
        # rollback's attempt to restore the csv backup, forcing retention.
        if in_stage and self.parent.name == "files" and self.suffix == ".xlsx":
            raise OSError("simulated promotion failure")
        if in_stage and self.parent.name == "backup" and self.suffix == ".csv":
            raise OSError("simulated restore failure")
        return real_replace(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", failing_replace)
    with pytest.raises(ValueError, match="Recovery details"):
        compiler.export(
            pd.DataFrame({"quote": ["x"]}), output, "facts",
            ["csv", "excel"], fresh=False,
        )
    # Restore of the csv failed, so the prior bytes must still be recoverable
    # from the retained stage-owned backup rather than silently lost.
    leaked = list(tmp_path.glob(".psweep-export-*"))
    assert leaked, "stage root must be kept when a backup cannot be restored"
    backups = list(leaked[0].rglob("*.csv"))
    assert any(b.read_text() == "OLD CSV BYTES" for b in backups)


def test_failed_new_file_cleanup_does_not_abort_other_restores(
    tmp_path, contract_schema, monkeypatch
):
    from pathlib import Path

    compiler = _compiler(contract_schema)
    output = tmp_path / "output"
    output.mkdir()
    (output / "facts.xlsx").write_bytes(b"OLD XLSX")
    replace = Path.replace
    unlink = Path.unlink

    def fail_publish(path, target):
        if path.parent.name == "files" and path.suffix == ".xlsx":
            raise OSError("publish failure")
        return replace(path, target)

    def fail_unlink(path, *args, **kwargs):
        if path == output / "facts.csv":
            raise OSError("new file cleanup failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", fail_publish)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(ValueError, match="rollback incomplete"):
        compiler.export(pd.DataFrame({"quote": ["new"]}), output,
                        "facts", ["csv", "excel"])
    assert (output / "facts.xlsx").read_bytes() == b"OLD XLSX"
    assert (output / "facts.csv").exists()


def test_output_dir_named_psweep_old_sibling_is_not_disturbed(
    tmp_path, contract_schema
):
    # Backups are stage-owned, never a fixed sentinel beside the output, so a
    # user file/dir that happens to share such a name is never touched.
    compiler = _compiler(contract_schema)
    output = tmp_path / "output"
    output.mkdir()
    (output / "facts.csv").write_text("OLD CSV BYTES")
    sentinel_dir = tmp_path / "output.psweep-old"
    sentinel_dir.mkdir()
    (sentinel_dir / "user.txt").write_text("precious")
    compiler.export(
        pd.DataFrame({"quote": ["x"]}), output, "facts",
        ["csv"], fresh=True,
    )
    assert (sentinel_dir / "user.txt").read_text() == "precious"
    assert (output / "facts.csv").exists()
    assert _no_staging_leak(tmp_path)


def test_nonfresh_success_publishes_both_formats_and_keeps_unrelated(
    tmp_path, contract_schema
):
    compiler = _compiler(contract_schema)
    output = tmp_path / "output"
    output.mkdir()
    (output / "facts.csv").write_text("OLD CSV BYTES")
    (output / "keep.txt").write_text("unrelated")
    emitted, _ = compiler.export(
        pd.DataFrame({"quote": ["x"]}), output, "facts",
        ["csv", "excel"], fresh=False,
    )
    assert [p.name for p in emitted] == ["facts.csv", "facts.xlsx"]
    assert (output / "facts.csv").exists()
    assert (output / "facts.xlsx").exists()
    assert (output / "facts.csv").read_text() != "OLD CSV BYTES"
    assert (output / "keep.txt").read_text() == "unrelated"
    assert _no_staging_leak(tmp_path)


def test_fresh_success_replaces_whole_directory_and_leaves_no_staging(
    tmp_path, contract_schema
):
    compiler = _compiler(contract_schema)
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep.txt").write_text("unrelated")
    emitted, _ = compiler.export(
        pd.DataFrame({"quote": ["x"]}), output, "facts",
        ["csv", "excel"], fresh=True,
    )
    assert sorted(p.name for p in emitted) == ["facts.csv", "facts.xlsx"]
    assert (output / "facts.csv").exists()
    assert (output / "facts.xlsx").exists()
    assert not (output / "keep.txt").exists()
    assert _no_staging_leak(tmp_path)


def test_fresh_empty_compile_removes_stale_outputs(tmp_path, contract_schema):
    directory = tmp_path / "input"
    directory.mkdir()
    (directory / "record.json").write_text(json.dumps({"payload": {"source": {"url": "https://example.org/a"}, "items": []}}))
    output = tmp_path / "output"
    output.mkdir()
    (output / "stale.csv").write_text("old rows")
    cfg = _config(tmp_path, contract_schema)
    result = CliRunner().invoke(cli, ["compile", "--config", str(cfg), "--fresh"])
    assert result.exit_code == 0, result.output
    assert not (output / "stale.csv").exists()
