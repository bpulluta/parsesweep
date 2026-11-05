"""Compile derives roster mode exactly as extraction does."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from psweep.config import RuntimeConfigError
from psweep.config.runtime_config_loader import (
    load_runtime_config_file,
    resolve_command_config,
    resolve_compilation_metadata,
)
from psweep.compilation.data_compiler import DataCompiler
from psweep.extraction.roster import load_roster
from psweep.utils.schema_metadata import SchemaMetadata

from _helpers import EXAMPLE_CONFIG


def _compiler(config_path: Path) -> DataCompiler:
    resolved = resolve_command_config(
        command="compile",
        cli_values={},
        config_data=load_runtime_config_file(config_path),
    )
    return DataCompiler(resolve_compilation_metadata(resolved), inputs=resolved, verbose=False)


def _write_config(tmp_path: Path, key_fields: list[str]) -> Path:
    schema = {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "state": {"type": "string"},
                        "ahj": {"type": "string"},
                        "permit_id": {"type": "string"},
                    },
                },
            }
        },
        "$metadata": {
            "extraction": {"main_data_array": "rows"},
            "identity": {"deduplication": {"key_fields": key_fields}},
        },
    }
    (tmp_path / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (tmp_path / "targets.csv").write_text("state,ahj\nAZ,Pima County\n", encoding="utf-8")
    config = tmp_path / "run.yaml"
    config.write_text(
        f"schema: {tmp_path / 'schema.json'}\ndiscovery:\n  targets: targets.csv\n"
        "  partition_by: [state, ahj]\n",
        encoding="utf-8",
    )
    return config


def test_key_fields_equal_to_partition_by_activates_roster(tmp_path):
    assert _compiler(_write_config(tmp_path, ["ahj", "state"])).mode == "roster"


def test_disjoint_key_fields_compile_rows(tmp_path):
    assert _compiler(_write_config(tmp_path, ["permit_id"])).mode == "rows"


def test_partial_overlap_is_a_config_error(tmp_path):
    with pytest.raises(RuntimeConfigError, match="partially overlaps"):
        _compiler(_write_config(tmp_path, ["state", "permit_id"]))


def test_example_config_rosters_in_extract_and_compile():
    inputs = resolve_command_config(
        command="extract", cli_values={}, config_data=load_runtime_config_file(EXAMPLE_CONFIG)
    )
    roster = load_roster(inputs, SchemaMetadata(Path(inputs["schema"])))
    assert roster is not None and roster.fields == ("state", "name")
    assert _compiler(EXAMPLE_CONFIG).mode == "roster"
