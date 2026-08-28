"""Tests for config-driven dedup severity tokens.

The name-heuristic token sets that rank conflict severity (a fallback behind
schema-derived per-field hints) can be extended via
``compilation.severity_tokens`` — declared in the schema ``$metadata`` or, more
commonly, supplied at runtime through ``run.yaml``'s ``compilation`` block (which
is merged onto the metadata). Configured tokens are unioned with the built-in
defaults; when unconfigured, behavior is unchanged.
"""

import json

import pytest

from psweep.compilation.deduplicator import Deduplicator
from psweep.exceptions import SchemaMetadataError
from psweep.utils.schema_metadata import (
    HIGH_SEVERITY_TOKENS,
    MEDIUM_SEVERITY_TOKENS,
    SchemaMetadata,
)


def _schema(compilation=None):
    meta = {
        "extraction": {
            "main_data_array": "items",
            "identifier_fields": ["id"],
        }
    }
    if compilation is not None:
        meta["compilation"] = compilation
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$metadata": meta,
        "type": "object",
        "properties": {"items": {"type": "array"}},
    }


def _write(tmp_path, payload):
    path = tmp_path / "s.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# "cadence" is not a substring of any built-in token, so it classifies as
# "low" until a domain names it a severity token.
NOVEL = "cadence"


class TestGetSeverityTokens:
    def test_defaults_when_unconfigured(self, tmp_path):
        meta = SchemaMetadata(_write(tmp_path, _schema()))
        high, medium = meta.get_severity_tokens()
        assert high == HIGH_SEVERITY_TOKENS
        assert medium == MEDIUM_SEVERITY_TOKENS

    def test_schema_declared_tokens_union_with_defaults(self, tmp_path):
        meta = SchemaMetadata(
            _write(
                tmp_path,
                _schema(
                    {"severity_tokens": {"high": [NOVEL], "medium": ["tier"]}}
                ),
            )
        )
        high, medium = meta.get_severity_tokens()
        assert NOVEL in high and HIGH_SEVERITY_TOKENS <= high
        assert "tier" in medium and MEDIUM_SEVERITY_TOKENS <= medium

    def test_runyaml_override_via_metadata_overrides(self, tmp_path):
        # This mirrors how run.yaml compilation.severity_tokens reaches the
        # compiler (resolved -> metadata_overrides -> SchemaMetadata).
        meta = SchemaMetadata(
            _write(tmp_path, _schema()),
            metadata_overrides={
                "compilation": {"severity_tokens": {"high": [NOVEL]}}
            },
        )
        high, _ = meta.get_severity_tokens()
        assert NOVEL in high and HIGH_SEVERITY_TOKENS <= high

    def test_tokens_lowercased(self, tmp_path):
        meta = SchemaMetadata(
            _write(
                tmp_path, _schema({"severity_tokens": {"high": [NOVEL.upper()]}})
            )
        )
        high, _ = meta.get_severity_tokens()
        assert NOVEL in high


class TestDeduplicatorUsesConfiguredTokens:
    def test_default_unmatched_column_is_low(self, tmp_path):
        dedup = Deduplicator(SchemaMetadata(_write(tmp_path, _schema())))
        assert dedup._classify_conflict_severity([NOVEL]) == "low"

    def test_configured_high_token_classifies_high(self, tmp_path):
        dedup = Deduplicator(
            SchemaMetadata(
                _write(tmp_path, _schema({"severity_tokens": {"high": [NOVEL]}}))
            )
        )
        assert dedup._classify_conflict_severity([NOVEL]) == "high"

    def test_configured_medium_token_classifies_medium(self, tmp_path):
        dedup = Deduplicator(
            SchemaMetadata(
                _write(
                    tmp_path, _schema({"severity_tokens": {"medium": [NOVEL]}})
                )
            )
        )
        assert dedup._classify_conflict_severity([NOVEL]) == "medium"

    def test_builtin_defaults_still_apply_alongside_config(self, tmp_path):
        # "rate" is a built-in high token; config supplements, never replaces.
        dedup = Deduplicator(
            SchemaMetadata(
                _write(tmp_path, _schema({"severity_tokens": {"high": [NOVEL]}}))
            )
        )
        assert dedup._classify_conflict_severity(["rate"]) == "high"


class TestSeverityTokensValidation:
    def test_non_dict_raises(self, tmp_path):
        with pytest.raises(SchemaMetadataError, match="severity_tokens"):
            SchemaMetadata(
                _write(tmp_path, _schema({"severity_tokens": ["high"]}))
            )

    def test_unknown_key_raises(self, tmp_path):
        with pytest.raises(SchemaMetadataError, match="severity_tokens"):
            SchemaMetadata(
                _write(
                    tmp_path, _schema({"severity_tokens": {"critical": ["x"]}})
                )
            )

    def test_non_str_list_raises(self, tmp_path):
        with pytest.raises(SchemaMetadataError, match="severity_tokens"):
            SchemaMetadata(
                _write(tmp_path, _schema({"severity_tokens": {"high": [1, 2]}}))
            )
