"""Shared test helpers.

Small, dependency-free utilities reused across multiple test modules. Kept
minimal on purpose — anything domain-specific belongs in the individual test
files or a fixture.
"""

from __future__ import annotations

import json
from pathlib import Path


def write_json(path: Path, content: dict) -> None:
    """Write ``content`` as indented JSON, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2), encoding="utf-8")


def synthetic_evidence_schema() -> dict:
    """Return a small evidence contract independent of private domain files."""
    nullable_text = {"type": ["string", "null"], "pattern": r"\S"}
    properties = {
        "state": dict(nullable_text),
        "county": dict(nullable_text),
        "locality": dict(nullable_text),
        "geographic_scope": {
            "type": "string", "enum": ["unspecified", "state", "county", "locality"]
        },
        "geography_quote": dict(nullable_text),
        "project_name": dict(nullable_text),
        "docket_id": dict(nullable_text),
        "type": {"type": "string", "pattern": r"\S"},
        "date": {"type": ["string", "null"], "format": "date"},
        "speaker": dict(nullable_text),
        "speaker_role": {"type": "string", "pattern": r"\S"},
        "sentiment": {
            "type": ["string", "null"],
            "enum": ["pro", "against", "mixed", "not_stated", None],
        },
        "summary": {"type": "string", "pattern": r"\S"},
        "evidence_quote": {"type": "string", "pattern": r"\S"},
        "evidence_locator": {"type": "string", "pattern": r"\S"},
        "topics": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "end_of_life_quote": dict(nullable_text),
    }
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$metadata": {
            "domain": "synthetic_evidence",
            "version": "1.0.0",
            "extraction": {
                "main_data_array": "comments",
                "context_objects": ["source"],
                "identifier_fields": ["source.url"],
                "document_type": "Evidence record",
            },
            "identity": {"deduplication": {
                "key_fields": [key for key in properties if key != "summary"],
                "partition_fields": ["source.url"],
            }},
        },
        "type": "object",
        "required": ["source", "comments"],
        "properties": {
            "source": {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string", "format": "uri", "pattern": r"^https?://"},
                    "title": dict(nullable_text),
                    "agency": dict(nullable_text),
                    "record_kind": {"type": "string", "pattern": r"\S"},
                },
            },
            "comments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": properties,
                    "required": ["type", "summary", "evidence_quote", "evidence_locator"],
                    "allOf": [
                        {
                            "if": {
                                "properties": {"geographic_scope": {"const": scope}},
                                "required": ["geographic_scope"],
                            },
                            "then": {
                                "required": [field, "geography_quote"],
                                "properties": {
                                    field: {"type": "string", "pattern": r"\S"},
                                    "geography_quote": {"type": "string", "pattern": r"\S"},
                                },
                            },
                        }
                        for scope, field in (("county", "county"), ("locality", "locality"))
                    ],
                },
            },
        },
    }
