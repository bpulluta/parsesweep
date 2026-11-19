"""``pages.auto_locate.subject_fields``: extra identity phrases for page targeting."""

from __future__ import annotations

import pytest

from psweep.config.runtime_config_loader import (
    RuntimeConfigError,
    _validate_page_targeting_block,
)
from psweep.extraction.page_locator import PageLocator
from psweep.extraction.provenance import SourceContext
from psweep.extraction.section_locator import SectionLocator
from psweep.pipeline import _check_subject_fields, _subject_phrases

# Synthetic roster: partition_by is [state, utility_name]; short_name is a passthrough column.
TARGETS = [
    {"state": "OH", "utility_name": "Acme Electric Cooperative: Zone 3", "short_name": "Acme"},
    {"state": "TX", "utility_name": "Lone Star Power: North", "short_name": "Lone Star"},
]
PAGES = [
    "County minutes. Public comment on roads.",
    "The board heard Acme tariff rates for residential customers.",
    "Adjourned.",
]


def _info(label="Acme Electric Cooperative: Zone 3", metadata=None):
    return SourceContext(None, "target", "1", label, metadata or TARGETS[0])


class TestSubjectPhrases:
    IDENTITY = {"state": "OH", "utility_name": "Acme Electric Cooperative: Zone 3"}

    def test_default_is_last_partition_field(self):
        phrases = _subject_phrases(None, ["state", "utility_name"], self.IDENTITY, _info())
        assert phrases == ("Acme Electric Cooperative: Zone 3",)

    def test_default_without_partition_fields_has_no_phrase(self):
        assert _subject_phrases(None, None, {}, _info()) == ()

    def test_configured_fields_read_the_full_target_row(self):
        phrases = _subject_phrases(
            ["utility_name", "short_name"], ["state", "utility_name"], self.IDENTITY, _info()
        )
        assert phrases == ("Acme Electric Cooperative: Zone 3", "Acme")

    def test_blank_or_missing_values_are_skipped(self):
        info = _info(metadata={"utility_name": "A", "short_name": None})
        assert _subject_phrases(["short_name", "other", "utility_name"], None, {}, info) == ("A",)


class TestAnchoring:
    def test_alias_column_anchors_a_pdf_the_label_cannot(self):
        context = _info()
        held = PageLocator("rates", source_context=context)
        assert held._subject_indices(PAGES) == []
        anchored = PageLocator("rates", source_context=context, subjects=("Acme",))
        assert anchored._subject_indices(PAGES) == [1]

    def test_set_source_context_rebinds_subjects(self):
        locator = PageLocator("rates")
        locator.set_source_context(_info(), subjects=("Acme",))
        assert "Acme" in locator._subject_hints
        locator.set_source_context(_info())
        assert "Acme" not in locator._subject_hints

    def test_section_locator_receives_the_same_hints(self):
        locator = SectionLocator("rates", source_context=_info(), subjects=("Acme", "Zone 3"))
        assert locator._subject_hints[-2:] == ["Acme", "Zone 3"]


class TestCacheIdentity:
    def test_fingerprint_changes_with_subjects(self):
        context = _info()
        base = PageLocator("rates", source_context=context)._fingerprint()
        one = PageLocator("rates", source_context=context, subjects=("Acme",))._fingerprint()
        two = PageLocator(
            "rates", source_context=context, subjects=("Acme", "Zone 3")
        )._fingerprint()
        assert len({base, one, two}) == 3

    def test_single_subject_fingerprint_matches_across_construction_paths(self):
        context = _info()
        built = PageLocator("rates", source_context=context, subjects=("Acme",))
        rebound = PageLocator("rates")
        rebound.set_source_context(context, subjects=("Acme",))
        assert built._fingerprint() == rebound._fingerprint()


class TestValidation:
    @pytest.mark.parametrize(
        "value",
        ["short_name", [], [""], ["  "], [1], ["short_name", "short_name"], {"a": 1}],
    )
    def test_malformed_value_is_rejected(self, value):
        with pytest.raises(RuntimeConfigError, match="subject_fields"):
            _validate_page_targeting_block({"subject_fields": value})

    def test_valid_value_and_absence_pass(self):
        _validate_page_targeting_block({"subject_fields": ["utility_name", "short_name"]})
        _validate_page_targeting_block({})

    def test_unknown_column_names_the_field(self):
        with pytest.raises(ValueError, match=r"\['shortname'\]"):
            _check_subject_fields({"targets": TARGETS}, ["utility_name", "shortname"])

    def test_input_columns_are_not_target_columns(self):
        targets = [{**TARGETS[0], "query": "q"}]
        with pytest.raises(ValueError, match="query"):
            _check_subject_fields({"targets": targets}, ["query"])

    def test_requires_targets(self):
        with pytest.raises(ValueError, match=r"requires discovery\.targets"):
            _check_subject_fields({}, ["short_name"])

    def test_existing_columns_and_absence_pass(self):
        _check_subject_fields({"targets": TARGETS}, ["utility_name", "short_name"])
        _check_subject_fields({}, None)


def test_subject_fields_change_the_extraction_fingerprint_only_when_set(tmp_path):
    from psweep.pipeline import _extraction_fingerprints

    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    doc = tmp_path / "a.txt"
    doc.write_text("x")

    def fingerprint(targeting):
        inputs = {"page_targeting": targeting} if targeting is not None else {}
        return _extraction_fingerprints(
            inputs,
            {"model": "m", "provider": "p"},
            None,
            schema,
            [doc],
            {doc: "d"},
            {},
            {},
            None,
            {},
            {doc: None},
            {},
        )[doc]

    base = {"enabled": True, "section_description": "rates"}
    absent = fingerprint(base)
    assert fingerprint(dict(base)) == absent
    assert fingerprint({**base, "subject_fields": ["short_name"]}) != absent
    assert fingerprint({**base, "subject_fields": ["short_name", "alias"]}) != fingerprint(
        {**base, "subject_fields": ["short_name"]}
    )
