"""Output headers are schema titles, else field names; there is no rename map."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from psweep.compilation.data_compiler import DataCompiler
from psweep.config.runtime_config_loader import (
    RuntimeConfigError,
    load_runtime_config_file,
    resolve_command_config,
    resolve_compilation_metadata,
)
from psweep.exceptions import SchemaMetadataError
from psweep.utils.schema_metadata import SchemaMetadata

SOLARTRACE = Path(__file__).resolve().parents[1] / "config" / "solartrace" / "run.yaml"


def _schema(output=None, titles=("Name", "Name")):
    compilation = {"output": output} if output else {}
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "title": titles[0]},
                        "alias": {"type": "string", "title": titles[1]},
                        "value": {"type": "number"},
                    },
                },
            }
        },
        "$metadata": {"extraction": {"main_data_array": "items"}, "compilation": compilation},
    }


def _prepare(tmp_path, schema):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    compiler = DataCompiler(SchemaMetadata(path), verbose=False)
    frame = pd.DataFrame([{"name": "a", "alias": "b", "value": 1}])
    return compiler._prepare_output_dataframe(frame)


def test_solartrace_titles_become_headers():
    config = load_runtime_config_file(SOLARTRACE)
    resolved = resolve_command_config(command="compile", cli_values={}, config_data=config)
    titles = resolve_compilation_metadata(resolved).get_column_titles()
    assert titles["ahj"] == "AHJ Name"
    assert titles["solsmart_awardee"] == "SolSmart Awardee"


def test_title_else_field_name_verbatim(tmp_path):
    output = _prepare(tmp_path, _schema(titles=("Name", "Also Known As")))
    assert output.columns.tolist() == ["Name", "Also Known As", "value"]


def test_duplicate_headers_are_rejected(tmp_path):
    with pytest.raises(ValueError, match=r"[Dd]uplicate"):
        _prepare(tmp_path, _schema())


@pytest.mark.parametrize("key", ["column_order", "exclude_fields"])
def test_unknown_output_header_is_rejected(tmp_path, key):
    schema = _schema({key: ["name"]}, titles=("Name", "Alias"))
    with pytest.raises(ValueError, match="unknown output header"):
        _prepare(tmp_path, schema)


def test_column_order_and_exclude_use_headers(tmp_path):
    schema = _schema(
        {"column_order": ["value", "Alias"], "exclude_fields": ["Name"]}, ("Name", "Alias")
    )
    assert _prepare(tmp_path, schema).columns.tolist() == ["value", "Alias"]


def test_column_renames_is_unknown(tmp_path):
    (tmp_path / "s.json").write_text(
        '{"$metadata": {"extraction": {"main_data_array": "items"}}}', encoding="utf-8"
    )
    with pytest.raises((RuntimeConfigError, SchemaMetadataError), match="column_renames"):
        resolve_compilation_metadata(
            {
                "schema": str(tmp_path / "s.json"),
                "compilation_output": {"column_renames": {"a": "b"}},
            }
        )
