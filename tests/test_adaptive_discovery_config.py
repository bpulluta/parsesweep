"""Adaptive discovery policy validation precedes requests or output mutation."""

import json

import pytest

from psweep.config.runtime_config_loader import load_runtime_config_file, resolve_command_config
from psweep.discovery.request_inputs import discovery_request_fields
from psweep.discovery.engine import DiscoveryRequest


def _config(tmp_path, discovery):
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            {
                "domain": "fixture",
                "models": {"cheap": "gpt-fixture", "strong": "gpt-strong"},
                "discovery": discovery,
            }
        )
    )
    return path


def test_new_fields_propagate_to_request(tmp_path):
    path = _config(
        tmp_path,
        {
            "targets": [{"name": "A", "address": "1 Main"}],
            "partition_by": ["name"],
            "queries": ["{name}"],
            "search": {"fallback_queries": ["{name} {address?} permit"]},
            "document_review": {
                "document_description": "facts for {name} at {address?}",
                "context_fields": ["address"],
                "model": "cheap",
                "escalate_model": "strong",
            },
            "runtime": {"max_review_calls": 8, "max_search_calls": 20},
        },
    )
    cfg = load_runtime_config_file(path)
    inputs = resolve_command_config(command="discover", cli_values={}, config_data=cfg)
    request = DiscoveryRequest(**discovery_request_fields(inputs))
    assert request.fallback_queries == ["{name} {address?} permit"]
    assert request.max_review_calls == 8 and request.max_search_calls == 20
    assert request.document_review["escalate_model"] == "strong"


@pytest.mark.parametrize(
    "discovery",
    [
        {"search": {"fallback_queries": ["x"]}},
        {"targets": [{"name": "A"}], "document_review": {"document_description": "{missing}"}},
        {
            "targets": [{"name": "A"}],
            "document_review": {"document_description": "facts", "context_fields": ["absent"]},
        },
        {
            "targets": [{"name": "A"}],
            "document_review": {
                "document_description": "facts",
                "context_fields": ["name", "name"],
            },
        },
        {"runtime": {"max_review_calls": True}},
        {"runtime": {"max_search_calls": 0}},
        {"targets": [{"name": "A"}], "document_review": {"document_description": "{name.bad}"}},
        {"document_review": {"coverage": {"history": "facts"}}},
        {
            "targets": [{"name": "A"}],
            "document_review": {
                "document_description": "facts",
                "coverage": {"bad.label": "facts"},
            },
        },
        {
            "targets": [{"name": "A"}],
            "document_review": {
                "document_description": "facts",
                "coverage": {"history": "{missing}"},
            },
        },
    ],
)
def test_invalid_adaptive_policy_fails_preflight(tmp_path, discovery):
    with pytest.raises(ValueError):
        load_runtime_config_file(_config(tmp_path, discovery))


def test_optional_context_blank_allowed(tmp_path):
    cfg = load_runtime_config_file(
        _config(
            tmp_path,
            {
                "targets": [{"name": "A", "address": None}],
                "document_review": {
                    "document_description": "facts {name} {address?}",
                    "context_fields": ["address"],
                },
            },
        )
    )
    assert cfg["discovery"]["document_review"]["context_fields"] == ["address"]
