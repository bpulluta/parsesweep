"""Self-contained evidence contracts; no private pilot files are required."""

import json

import pytest
from jsonschema import Draft7Validator, FormatChecker

from psweep.config.runtime_config_loader import (
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.discovery.candidate_selector import CandidateSelector
from psweep.discovery.models import CandidateScore, DiscoveryCandidate


@pytest.fixture
def record():
    return {
        "source": {
            "url": "https://records.example.org/evidence.pdf",
            "record_kind": "submitted_statement",
        },
        "comments": [{
            "state": None,
            "county": None,
            "locality": None,
            "geographic_scope": "unspecified",
            "geography_quote": None,
            "project_name": "Example Project",
            "type": "statement",
            "date": None,
            "speaker": None,
            "sentiment": "not_stated",
            "summary": "The speaker asks about the project.",
            "evidence_quote": "When will this project begin?",
            "evidence_locator": "Page 1",
            "topics": [],
        }],
    }


def validator(schema_path):
    schema = json.loads(schema_path.read_text())
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema, format_checker=FormatChecker())


def test_evidence_contract_accepts_unknowns_and_empty_results(schema_path, record):
    check = validator(schema_path)
    check.validate(record)
    record["comments"] = []
    check.validate(record)
    del record["source"]["url"]
    assert list(check.iter_errors(record))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sentiment", "invented_value"),
        ("evidence_quote", ""),
        ("evidence_quote", " \t\n "),
        ("evidence_locator", ""),
        ("evidence_locator", " \t "),
        ("geographic_scope", "invented_scope"),
        ("date", "2024"),
        ("date", "2024-02-30"),
    ],
)
def test_evidence_contract_rejects_invalid_rows(schema_path, record, field, value):
    record["comments"][0][field] = value
    assert list(validator(schema_path).iter_errors(record))


@pytest.mark.parametrize(("scope", "field"), [("county", "county"), ("locality", "locality")])
def test_evidence_contract_requires_explicit_finer_geography(
    schema_path, record, scope, field
):
    record["comments"][0]["geographic_scope"] = scope
    check = validator(schema_path)
    assert list(check.iter_errors(record))
    record["comments"][0][field] = "Example Place"
    assert list(check.iter_errors(record))
    record["comments"][0]["geography_quote"] = "The project is in Example Place."
    check.validate(record)


def test_evidence_contract_does_not_claim_semantic_verification(schema_path, record):
    record["comments"][0].update(
        county="Not named in quotation",
        geographic_scope="county",
        geography_quote="The project is in Example Place.",
    )
    # Schema validation enforces structure, not the truth of the interpretation.
    validator(schema_path).validate(record)


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://records.example.org/evidence.pdf", True),
        ("https://other.example.org/evidence.pdf", False),
        ("https://records.example.org/private/file.pdf", False),
    ],
)
def test_source_policy_uses_synthetic_config(tmp_path, url, allowed):
    import yaml

    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump({
        "domain": "synthetic_evidence",
        "discovery": {"selection": {"exclude_url_patterns": [
            r"^https?://(?!records\.example\.org(?:/|$))",
            r"/private/",
        ]}},
    }))
    config = load_runtime_config_file(path)
    selector = CandidateSelector(
        exclude_draft=False,
        require_supported_document=False,
        exclude_url_patterns=config["discovery"]["selection"]["exclude_url_patterns"],
    )
    candidate = DiscoveryCandidate(url=url, source="test", score=CandidateScore())
    selected, _ = selector.select([[candidate]])
    assert bool(selected) is allowed


def test_synthetic_config_resolves_all_stages_strictly(tmp_path, schema_path):
    import yaml

    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump({
        "domain": "synthetic_evidence",
        "discovery": {"targets": [{"name": "Example Target"}]},
        "extraction": {"schema": str(schema_path), "input_dir": "input"},
        "compilation": {"schema": str(schema_path), "input_dir": "extracted"},
    }))
    config = load_runtime_config_file(path)
    for command in ("discover", "extract", "compile"):
        resolve_command_config(command=command, cli_values={}, config_data=config, strict=True)
