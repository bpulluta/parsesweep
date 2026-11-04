"""Build fingerprint: content-aware compile freshness."""

from __future__ import annotations

import copy
import json

import pytest

from psweep.compilation.data_compiler import DataCompiler
from psweep.utils.schema_metadata import SchemaMetadata

EVIDENCE = {
    "type": "object",
    "properties": {"value": {"type": ["string", "null"]}, "quote": {"type": ["string", "null"]}},
    "required": ["value", "quote"],
}
SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "state": {"type": "string", "title": "State"},
                    "ahj": {"type": "string", "title": "AHJ Name"},
                    "online": {**EVIDENCE, "title": "Online Permitting"},
                    "note": {"type": ["string", "null"]},
                },
            },
        }
    },
    "$metadata": {
        "extraction": {"main_data_array": "rows"},
        "identity": {"deduplication": {"key_fields": ["state", "ahj"]}},
    },
}
TARGETS = "state,ahj,geo_id\nAZ,Pima County,4019\nCO,Boulder County,8013\n"


def _record(directory, name, value="Yes"):
    record = {
        "document": {"source_url": f"https://{name}.gov", "source_scope": "target"},
        "payload": {
            "rows": [
                {
                    "state": "AZ",
                    "ahj": "Pima County",
                    "online": {"value": value, "quote": "q"},
                    "note": None,
                }
            ]
        },
    }
    (directory / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")


@pytest.fixture
def setup(tmp_path):
    records = tmp_path / "records"
    records.mkdir()
    for name in ("a", "b"):
        _record(records, name)
    (tmp_path / "targets.csv").write_text(TARGETS, encoding="utf-8")
    (tmp_path / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")

    def fingerprint(schema=None, overlay=None, roster=True):
        if schema is not None:
            (tmp_path / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
        metadata = SchemaMetadata(tmp_path / "schema.json", overlay)
        inputs = {"targets": str(tmp_path / "targets.csv"), "partition_by": ["state", "ahj"]}
        return DataCompiler(metadata, inputs if roster else {}, verbose=False).fingerprint(records)

    return tmp_path, records, fingerprint


def test_fingerprint_is_stable_across_runs_and_file_order(setup, tmp_path_factory):
    tmp_path, records, fingerprint = setup
    first = fingerprint()
    assert fingerprint() == first and len(first) == 64
    reordered = tmp_path_factory.mktemp("reordered")
    for name in ("b", "a"):
        _record(reordered, name)
    metadata = SchemaMetadata(tmp_path / "schema.json")
    inputs = {"targets": str(tmp_path / "targets.csv"), "partition_by": ["state", "ahj"]}
    assert DataCompiler(metadata, inputs, verbose=False).fingerprint(reordered) == first
    assert fingerprint(roster=False) != first


def test_fingerprint_tracks_records(setup):
    _, records, fingerprint = setup
    base = fingerprint()
    _record(records, "a", value="Yez")
    changed = fingerprint()
    assert changed != base
    _record(records, "a")
    assert fingerprint() == base
    _record(records, "c")
    assert fingerprint() not in {base, changed}
    (records / "c.json").unlink()
    (records / "b.json").unlink()
    assert fingerprint() not in {base, changed}
    (records / "run_manifests").mkdir()
    (records / "run_manifests" / "x.json").write_text("{}", encoding="utf-8")
    _record(records, "b")
    assert fingerprint() == base


def test_fingerprint_tracks_compile_contract(setup):
    tmp_path, _, fingerprint = setup
    base = fingerprint()
    assert fingerprint(overlay={"compilation": {"output": {"exclude_fields": ["note"]}}}) != base
    assert (
        fingerprint(overlay={"compilation": {"normalization": {"state_column": "state"}}}) != base
    )
    formats = {
        fingerprint(overlay={"compilation": {"output": {"default_format": f}}})
        for f in ("csv", "xlsx", "both")
    }
    assert formats == {base}

    (tmp_path / "targets.csv").write_text(TARGETS.replace("8013", "8014"), encoding="utf-8")
    assert fingerprint() != base
    (tmp_path / "targets.csv").write_text(TARGETS, encoding="utf-8")
    assert fingerprint() == base

    titled = copy.deepcopy(SCHEMA)
    titled["properties"]["rows"]["items"]["properties"]["note"]["title"] = "Note"
    assert fingerprint(schema=titled) != base
    assert fingerprint(schema=SCHEMA) == base
    (tmp_path / "schema.json").write_text(json.dumps(SCHEMA) + "\n", encoding="utf-8")
    assert fingerprint() != base


@pytest.mark.parametrize("surface", ["pipeline", "cli"])
def test_empty_extraction_publishes_a_fresh_header_only_table(setup, surface):
    from click.testing import CliRunner

    from psweep.cli.app import cli
    from psweep.pipeline import compile_extractions, compiled_table_fresh

    tmp_path, records, _ = setup
    schema, out = tmp_path / "schema.json", tmp_path / "compiled"

    def compile_and_check() -> list[str]:
        if surface == "pipeline":
            compile_extractions(records, schema, output_dir=out, report_format="csv")
        else:
            args = ["compile", str(records), "--schema", str(schema), "--output", str(out), "-q"]
            result = CliRunner().invoke(cli, args, catch_exceptions=False)
            assert result.exit_code == 0, result.output
        assert compiled_table_fresh(records, schema, config_path=None, output_dir=out)
        return (out / "records.csv").read_text(encoding="utf-8").splitlines()

    assert len(compile_and_check()) == 2
    for name in ("a", "b"):
        record = json.loads((records / f"{name}.json").read_text(encoding="utf-8"))
        record["payload"]["rows"] = []
        (records / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")
    assert not compiled_table_fresh(records, schema, config_path=None, output_dir=out)
    lines = compile_and_check()
    assert len(lines) == 1 and "AHJ Name" in lines[0]
