"""One schema-driven compiler adapts to unrelated domain shapes."""

import json
from unittest.mock import Mock

import pandas as pd
import pytest
import yaml

from psweep.compilation.data_compiler import DataCompiler
from psweep.pipeline import compile_extractions
from psweep.utils.schema_metadata import SchemaMetadata


@pytest.mark.parametrize("domain", ["manual", "regulation", "comments"])
def test_schema_controls_columns_types_and_yaml_presentation(tmp_path, domain):
    common = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
    }
    if domain == "manual":
        fields = {
            "model_number": {"type": "string"},
            "rated_power_kw": {"type": ["number", "null"]},
            "measurements": {"type": "array", "items": {"type": "number"}},
        }
        item = {
            "model_number": "GX-01",
            "rated_power_kw": 250.5,
            "measurements": [2.5, 3.5],
        }
        key = "model_number"
        rename = {"Rated Power Kw": "output_kw"}
        order = ["model_number", "output_kw"]
    elif domain == "regulation":
        fields = {
            "requirement": {"type": "string"},
            "limit": {"type": ["number", "null"]},
            "unit": {"type": "string"},
        }
        item = {"requirement": "Setback", "limit": 300, "unit": "feet"}
        key = "requirement"
        rename = {}
        order = ["requirement", "limit", "unit"]
    else:
        fields = {
            "speaker": {"type": "string"},
            "sentiment": {"type": "string"},
            "evidence_quote": {"type": "string"},
        }
        item = {
            "speaker": "Anonymous",
            "sentiment": "mixed",
            "evidence_quote": "There are benefits and drawbacks.",
        }
        key = "evidence_quote"
        rename = {}
        order = ["speaker", "sentiment", "evidence_quote"]
    schema = {
        **common,
        "properties": {
            "source": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                },
            },
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": fields},
            },
        },
        "$metadata": {
            "extraction": {
                "main_data_array": "items",
                "context_objects": ["source"],
                "identifier_fields": ["source.url"],
            },
            "identity": {
                "deduplication": {
                    "key_fields": [key],
                    "partition_fields": ["source.url"],
                }
            },
            "compilation": {"normalization": {"state_column": None}},
        },
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    directory = tmp_path / domain
    directory.mkdir()
    (directory / "record.json").write_text(
        json.dumps(
            {
                "payload": {
                    "source": {"url": "https://example.org/document"},
                    "items": [item],
                }
            }
        )
    )
    config = tmp_path / "run.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "domain": domain,
                "compilation": {
                    "output": {
                        "column_renames": rename,
                        "column_order": order,
                    }
                },
            }
        )
    )
    result = compile_extractions(
        directory,
        path,
        config_path=config,
        output_dir=tmp_path / "output",
        report_format="both",
    )
    csv = pd.read_csv(result.output_files[0])
    excel = pd.read_excel(result.output_files[1])
    assert len(csv) == len(excel) == 1
    assert csv.columns.tolist() == excel.columns.tolist()
    assert csv.columns.tolist()[: len(order)] == order
    assert "project" not in csv and "fips" not in csv and "data" not in csv
    if domain != "comments":
        assert "sentiment" not in csv
    if domain == "manual":
        assert "rated_power_kw" not in csv
        assert csv.iloc[0]["output_kw"] == 250.5
        assert pd.api.types.is_numeric_dtype(csv["output_kw"])
        assert csv.iloc[0]["measurements"] == "2.5, 3.5"
    if domain == "regulation":
        assert csv.iloc[0]["limit"] == 300
        assert csv.iloc[0]["unit"] == "feet"
    if domain == "comments":
        assert csv.iloc[0]["sentiment"] == "mixed"


def test_schema_uri_partitions_preserve_source_path_case(tmp_path):
    schema = {
        "type": "object",
        "properties": {
            "source": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "format": "uri"},
                },
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "statement": {"type": "string"},
                    },
                },
            },
        },
        "$metadata": {
            "extraction": {
                "main_data_array": "items",
                "context_objects": ["source"],
                "identifier_fields": ["source.url"],
            },
            "identity": {
                "deduplication": {
                    "key_fields": ["statement"],
                    "partition_fields": ["source.url"],
                }
            },
        },
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    directory = tmp_path / "input"
    directory.mkdir()
    for index, name in enumerate(("A", "a")):
        (directory / f"record-{index}.json").write_text(
            json.dumps(
                {
                    "payload": {
                        "source": {"url": f"https://example.org/{name}"},
                        "items": [{"statement": "Identical evidence"}],
                    }
                }
            )
        )
    compiler = DataCompiler(SchemaMetadata(path), verbose=False)
    frame, _ = compiler.compile_from_directory(directory)
    assert len(frame) == 2


def test_nested_rows_preserve_parents_without_children(tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {
                        "main_data_array": "items",
                        "identifier_fields": ["name"],
                    },
                }
            }
        )
    )
    directory = tmp_path / "input"
    directory.mkdir()
    (directory / "record.json").write_text(
        json.dumps(
            {
                "payload": {
                    "items": [
                        {"name": "Assembly", "parts": [{"serial": "P-1"}]},
                        {"name": "Standalone", "parts": []},
                        {"name": "Unspecified"},
                    ]
                }
            }
        )
    )
    compiler = DataCompiler(SchemaMetadata(schema), verbose=False)
    frame, _ = compiler.compile_from_directory(directory)
    assert len(frame) == 3
    assert set(frame["Name"]) == {"Assembly", "Standalone", "Unspecified"}
    assert frame.loc[frame["Name"] == "Assembly", "Serial"].iloc[0] == "P-1"
    assert frame.loc[frame["Name"] == "Standalone", "Serial"].isna().all()


@pytest.mark.parametrize(
    "fuzzy", [[], ["variant"], [{"field": "variant", "normalize": "words"}]]
)
def test_fully_populated_keys_do_not_require_pairwise_matching(
    tmp_path, monkeypatch, fuzzy
):
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {
                        "main_data_array": "items",
                        "identifier_fields": ["name"],
                    },
                    "identity": {
                        "deduplication": {
                            "key_fields": ["name", "variant"]
                            if fuzzy
                            else ["name"],
                            "fuzzy_key_fields": fuzzy,
                        }
                    },
                }
            }
        )
    )
    compiler = DataCompiler(SchemaMetadata(schema), verbose=False)
    frame = pd.DataFrame(
        {
            "Name": ["Shared"] * 2000,
            "Variant": [f"Mode {index % 100}" for index in range(2000)],
            "Evidence": [""] * 1999 + ["More complete"],
        }
    )
    normalize = Mock(wraps=compiler.deduplicator._normalize_fuzzy_value)
    monkeypatch.setattr(
        compiler.deduplicator, "_normalize_fuzzy_value", normalize
    )
    result = compiler.deduplicator.deduplicate(frame)
    assert len(result) == (100 if fuzzy else 1)
    assert "More complete" in result["Evidence"].tolist()
    assert normalize.call_count == (len(frame) if fuzzy else 0)


def test_wildcard_fuzzy_matching_preserves_greedy_source_order(tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {
                        "main_data_array": "items",
                        "identifier_fields": ["name"],
                    },
                    "identity": {
                        "deduplication": {
                            "key_fields": ["name", "variant"],
                            "fuzzy_key_fields": ["variant"],
                        }
                    },
                }
            }
        )
    )
    compiler = DataCompiler(SchemaMetadata(schema), verbose=False)
    frame = pd.DataFrame(
        {
            "Name": ["Shared"] * 3,
            "Variant": ["A", "", "B"],
            "Evidence": ["first", "", "last"],
        }
    )
    result = compiler.deduplicator.deduplicate(frame)
    assert result["Variant"].tolist() == ["A", "B"]
    assert (
        compiler.deduplicator.preview_deduplication(frame)[
            "duplicates_removed"
        ]
        == 1
    )


def test_fuzzy_index_matches_reference_for_missing_and_populated_keys(
    tmp_path,
):
    from itertools import product

    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {
                        "main_data_array": "items",
                        "identifier_fields": ["name"],
                    },
                    "identity": {
                        "deduplication": {
                            "key_fields": ["name", "variant", "mode"],
                            "fuzzy_key_fields": ["variant", "mode"],
                        }
                    },
                }
            }
        )
    )
    dedup = DataCompiler(SchemaMetadata(schema), verbose=False).deduplicator
    combinations = list(product(("", "A", "B"), repeat=2))
    for offset in range(len(combinations)):
        ordered = combinations[offset:] + combinations[:offset]
        frame = pd.DataFrame(ordered + ordered, columns=["Variant", "Mode"])
        frame["Name"] = "Common"
        expected = []
        remaining = list(frame.index)
        while remaining:
            anchor, *others = remaining
            matched = [anchor]
            unmatched = []
            for index in others:
                pairs = zip(
                    frame.loc[anchor, ["Variant", "Mode"]],
                    frame.loc[index, ["Variant", "Mode"]],
                )
                if all(
                    not left or not right or left.lower() == right.lower()
                    for left, right in pairs
                ):
                    matched.append(index)
                else:
                    unmatched.append(index)
            expected.append(matched)
            remaining = unmatched
        actual = list(
            dedup._match_key_group(
                frame, list(frame.index), ["Variant", "Mode"], {}
            )
        )
        assert actual == expected


@pytest.mark.parametrize(
    "items", [None, "invalid", {"0": {}}, [{"name": "valid"}, "invalid"]]
)
def test_malformed_main_array_never_becomes_empty_success(tmp_path, items):
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {
                        "main_data_array": "items",
                        "identifier_fields": ["name"],
                    },
                }
            }
        )
    )
    directory = tmp_path / "input"
    directory.mkdir()
    (directory / "record.json").write_text(
        json.dumps({"payload": {"items": items}})
    )
    with pytest.raises(ValueError, match="record.json.*items"):
        DataCompiler(
            SchemaMetadata(schema), verbose=False
        ).compile_from_directory(directory)


@pytest.mark.parametrize("missing", ["key_fields", "partition_fields"])
def test_incomplete_identity_never_deduplicates_on_partial_keys(
    tmp_path, missing
):
    schema = tmp_path / "schema.json"
    dedup = {"key_fields": ["name"], "partition_fields": []}
    dedup[missing].append("missing_identity")
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {
                        "main_data_array": "items",
                        "identifier_fields": ["name"],
                    },
                    "identity": {"deduplication": dedup},
                }
            }
        )
    )
    compiler = DataCompiler(SchemaMetadata(schema), verbose=False)
    frame = pd.DataFrame({"Name": ["Shared", "Shared"], "Value": [1, 2]})
    with pytest.raises(ValueError, match="missing_identity"):
        compiler.deduplicator.deduplicate(frame.copy())
    with pytest.raises(ValueError, match="missing_identity"):
        compiler.deduplicator.preview_deduplication(frame)


def test_declared_optional_identity_absent_in_all_records_remains_null(tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "variant": {"type": ["string", "null"]},
        }}},
    }, "$metadata": {
        "extraction": {"main_data_array": "items", "identifier_fields": ["name"]},
        "identity": {"deduplication": {"key_fields": ["name", "variant"]}},
    }}))
    directory = tmp_path / "input"
    directory.mkdir()
    (directory / "record.json").write_text(json.dumps({"payload": {
        "items": [{"name": "First"}, {"name": "Second"}],
    }}))
    compiler = DataCompiler(SchemaMetadata(schema), verbose=False)
    frame, _ = compiler.compile_from_directory(directory)
    assert len(frame) == 2
    assert frame["Variant"].isna().all()


def test_unknown_fixed_profile_is_rejected_not_silently_ignored(tmp_path):
    from psweep.config.runtime_config_loader import (
        RuntimeConfigError,
        resolve_compilation_metadata,
    )

    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {
                        "main_data_array": "items",
                        "identifier_fields": ["id"],
                    }
                }
            }
        )
    )
    with pytest.raises(RuntimeConfigError, match="unknown key"):
        resolve_compilation_metadata(
            {
                "schema": str(schema),
                "compilation_output": {
                    "fact_table": {"fields": {"sentiment": "sentiment"}},
                },
            }
        )
