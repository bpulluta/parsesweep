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
from psweep.discovery.targets import TargetSpecError, identify_target, subject_phrase_index
from psweep.pipeline import _subject_phrases

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
        plan = {"subject_phrases": None}
        phrases = _subject_phrases(plan, ["state", "utility_name"], self.IDENTITY, _info())
        assert phrases == ("Acme Electric Cooperative: Zone 3",)

    def test_default_without_partition_fields_has_no_phrase(self):
        assert _subject_phrases({"subject_phrases": None}, None, {}, _info()) == ()

    def test_configured_fields_come_from_the_frozen_current_map(self):
        plan = {"subject_phrases": {"1": ("Acme",)}}
        # stale discovery metadata (info.target_metadata) is never consulted
        assert _subject_phrases(plan, ["state"], self.IDENTITY, _info()) == ("Acme",)

    def test_unresolved_target_gets_no_stale_phrase(self):
        plan = {"subject_phrases": {"other": ("Acme",)}}
        assert _subject_phrases(plan, None, {}, _info()) == ()


class TestPhraseIndex:
    def test_keys_follow_the_discovery_identity_and_skip_blank_values(self):
        rows = [
            {"state": "OH", "name": "A", "alias": " Acme ", "n": 7},
            {"state": "TX", "name": "B", "alias": None, "n": ""},
            {"state": "TX", "name": "C", "alias": "  ", "n": 0},
        ]
        index = subject_phrase_index(rows, ["alias", "n"], ["state", "name"])
        assert index == {
            identify_target(rows[0], ["state", "name"]): ("Acme", "7"),
            identify_target(rows[2], ["state", "name"]): ("0",),
        }

    @pytest.mark.parametrize("value", [["a", "b"], {"a": 1}])
    def test_list_and_object_values_are_rejected(self, value):
        with pytest.raises(TargetSpecError, match=r"alias.*scalar"):
            subject_phrase_index([{"name": "A", "alias": value}], ["alias"], ["name"])

    def test_unknown_column_names_the_key_and_field(self):
        with pytest.raises(TargetSpecError, match=r"extraction\.pages.*\['shortname'\]"):
            subject_phrase_index(TARGETS, ["utility_name", "shortname"], None)

    @pytest.mark.parametrize("field", ["query", "urls"])
    def test_input_fields_are_not_columns(self, field):
        with pytest.raises(TargetSpecError, match=field):
            subject_phrase_index([{**TARGETS[0], field: "q"}], [field], None)

    def test_requires_targets(self):
        with pytest.raises(TargetSpecError, match=r"requires discovery\.targets"):
            subject_phrase_index(None, ["short_name"], None)

    def test_generic_vocabularies_and_cross_product_ids(self):
        spec = {
            "cross_product": {
                "dimensions": [
                    {"name": "kind", "values": ["bond", "swap"]},
                    {"name": "issuer", "values": ["Orion Trust", "Vega Fund"]},
                ]
            }
        }
        rows = [{"kind": "bond", "issuer": "Orion Trust", "nick": "Orion"}]
        assert len(subject_phrase_index(spec, ["issuer"], ["kind", "issuer"])) == 4
        index = subject_phrase_index(rows, ["nick"], ["kind", "issuer"])
        assert index[identify_target(rows[0], ["kind", "issuer"])] == ("Orion",)

    def test_large_target_set_retains_only_selected_phrases(self):
        def rows():
            for number in range(100_000):
                yield {"id": f"t{number}", "alias": f"alias {number}", "bulk": "x" * 100}

        spec = list(rows())
        index = subject_phrase_index(spec, ["alias"], ["id"])
        assert len(index) == 100_000
        sample = next(iter(index.values()))
        assert isinstance(sample, tuple) and len(sample) == 1
        assert not any("bulk" in str(value) for value in index.values())


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


class TestSubjectsBoundary:
    @pytest.mark.parametrize("locator_type", [PageLocator, SectionLocator])
    @pytest.mark.parametrize("bad", ["Acme", b"Acme", ("Acme", 3), [None]])
    def test_non_sequences_of_strings_are_rejected(self, locator_type, bad):
        context = _info()
        with pytest.raises(TypeError, match="subjects"):
            locator_type("rates", source_context=context, subjects=bad)
        locator = locator_type("rates")
        with pytest.raises(TypeError, match="subjects"):
            locator.set_source_context(context, subjects=bad)

    @pytest.mark.parametrize("locator_type", [PageLocator, SectionLocator])
    def test_blanks_are_dropped_and_phrases_stripped(self, locator_type):
        locator = locator_type("rates", source_context=_info(), subjects=[" Acme ", "", "  "])
        assert locator._subject_hints[-1:] == ["Acme"]

    @pytest.mark.parametrize(
        ("subjects", "serialized"),
        [((), None), (("Acme",), "Acme"), (("Acme", "Zone 3"), ["Acme", "Zone 3"])],
    )
    def test_subject_serialization_matches_the_prior_default(
        self, monkeypatch, subjects, serialized
    ):
        """Zero/one phrase serialize as before (None / the string), so caches survive."""
        import json

        from psweep.extraction import page_locator

        seen = []
        real = json.dumps
        monkeypatch.setattr(
            page_locator.json, "dumps", lambda obj, *a, **k: seen.append(obj) or real(obj, *a, **k)
        )
        PageLocator("rates", source_context=_info(), subjects=subjects)._fingerprint()
        assert seen[0]["subject"] == serialized


# --- end-to-end: current targets, run preflight, record identity -------------


@pytest.fixture
def domain(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import json

    schema = {
        "$metadata": {
            "extraction": {"main_data_array": "items"},
            "identity": {"deduplication": {"key_fields": ["region", "entity"]}},
        },
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "region": {"type": "string"},
                        "entity": {"type": "string"},
                        "answer": {"type": ["string", "null"]},
                    },
                },
            }
        },
    }
    (tmp_path / "schema.json").write_text(json.dumps(schema))
    rows = [
        {"region": "North", "entity": "Formal Entity: Zone 3", "short_name": "Acme", "note": "a"},
        {"region": "South", "entity": "Other Entity", "short_name": "Borg", "note": "b"},
    ]
    config = {
        "domain": "fixture",
        "schema": str(tmp_path / "schema.json"),
        "models": {"primary": "gpt-fixture", "secondary": "gpt-locator"},
        "discovery": {
            "targets": rows,
            "partition_by": ["region", "entity"],
            "queries": ["{entity}"],
        },
        "extraction": {
            "model": "primary",
            "pages": {
                "auto_locate": {
                    "enabled": True,
                    "model": "secondary",
                    "section_description": "answer",
                    "subject_fields": ["short_name"],
                }
            },
        },
    }
    documents = tmp_path / "documents"
    documents.mkdir()
    for name in ("a.txt", "b.txt"):
        (documents / name).write_text("Acme answer")
    return config, rows, documents


def _write(tmp_path, config):
    import json

    path = tmp_path / "run.yaml"
    path.write_text(json.dumps(config))
    return path


def _inputs(tmp_path, config, documents):
    from psweep.config import load_runtime_config_file, resolve_command_config

    path = _write(tmp_path, config)
    inputs = resolve_command_config(
        command="extract", cli_values={}, config_data=load_runtime_config_file(path)
    )
    inputs.update(path=str(documents), output=str(tmp_path / "records"))
    return inputs


def _index(documents, rows, overrides=None):
    import csv
    import json

    fields = ["path", "source_scope", "target_id", "target_label", "target_metadata"]
    with (documents / "download_index.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for name, row in zip(("a.txt", "b.txt"), rows, strict=True):
            writer.writerow(
                {
                    "path": name,
                    "source_scope": "target",
                    "target_id": identify_target(row, ["region", "entity"]),
                    "target_label": row["entity"],
                    "target_metadata": json.dumps(
                        (overrides or {}).get(
                            name, {"region": row["region"], "entity": row["entity"]}
                        )
                    ),
                }
            )


class TestRunPreflight:
    @pytest.mark.parametrize("command", ["run", "extract"])
    @pytest.mark.parametrize("bad", [["shortname"], ["query"], ["urls"]])
    def test_bad_field_fails_validate_config_before_any_stage(
        self, domain, tmp_path, monkeypatch, command, bad
    ):
        from unittest.mock import Mock

        from click.testing import CliRunner

        from psweep.cli.main import cli

        config, _, _ = domain
        config["extraction"]["pages"]["auto_locate"]["subject_fields"] = bad
        execute = Mock(side_effect=AssertionError("stage ran"))
        monkeypatch.setattr("psweep.cli.app.execute_run_stages", execute)
        result = CliRunner().invoke(
            cli, [command, "--config", str(_write(tmp_path, config)), "--validate-config", "-q"]
        )
        execute.assert_not_called()
        assert result.exit_code != 0
        assert bad[0] in result.output
        assert "subject_fields" in result.output

    def test_missing_target_spec_is_rejected(self, domain, tmp_path):
        from psweep.config import RuntimeConfigError

        config, _, documents = domain
        del config["discovery"]
        with pytest.raises(RuntimeConfigError, match=r"requires discovery\.targets"):
            _inputs(tmp_path, config, documents)

    def test_list_valued_column_fails_at_config_resolution(self, domain, tmp_path):
        from psweep.config import RuntimeConfigError

        config, rows, documents = domain
        rows[0]["short_name"] = ["Acme", "Acme Co"]
        with pytest.raises(RuntimeConfigError, match=r"short_name.*scalar"):
            _inputs(tmp_path, config, documents)

    def test_valid_key_passes_the_same_validation(self, domain, tmp_path):
        config, _, documents = domain
        assert _inputs(tmp_path, config, documents)["page_targeting"]["subject_fields"]


class TestCurrentTargets:
    def _hints(self, plan, monkeypatch, name):
        from psweep.pipeline import _capture_input, _target_extraction_inputs

        seen = []
        monkeypatch.setattr(
            SectionLocator, "locate", lambda locator, _: seen.append(locator._subject_hints)
        )
        _, _, _, target = _target_extraction_inputs(plan)
        doc = next(d for d in plan["documents"] if d.name == name)
        with _capture_input(doc, plan) as source:
            target(source)
        return seen[0]

    @pytest.mark.parametrize("stale", [None, "Outdated Name"])
    def test_current_alias_reaches_the_locator_without_rediscovery(
        self, domain, tmp_path, monkeypatch, stale
    ):
        from psweep.pipeline import _prepare_extraction

        config, rows, documents = domain
        metadata = {"region": "North", "entity": rows[0]["entity"]}
        if stale:
            metadata["short_name"] = stale
        _index(documents, rows, {"a.txt": metadata})
        hints = self._hints(
            _prepare_extraction(_inputs(tmp_path, config, documents)), monkeypatch, "a.txt"
        )
        assert "Acme" in hints and "Outdated Name" not in hints

    def test_alias_value_edit_invalidates_only_that_target(self, domain, tmp_path):
        from psweep.pipeline import _prepare_extraction

        config, rows, documents = domain
        _index(documents, rows)
        a, b = (documents / "a.txt"), (documents / "b.txt")
        before = _prepare_extraction(_inputs(tmp_path, config, documents))["fingerprints"]
        rows[0]["short_name"] = "Corrected"
        after = _prepare_extraction(_inputs(tmp_path, config, documents))["fingerprints"]
        assert before[a] != after[a] and before[b] == after[b]
        rows[0]["note"] = "unselected passthrough edit"
        again = _prepare_extraction(_inputs(tmp_path, config, documents))["fingerprints"]
        assert again == after

    def test_manual_page_override_ignores_phrases(self, domain, tmp_path):
        from psweep.pipeline import _prepare_extraction

        config, rows, documents = domain
        _index(documents, rows)
        (tmp_path / "ranges.csv").write_text("file_path,start_page,end_page\na.txt,,\n")
        config["extraction"]["pages"]["csv"] = str(tmp_path / "ranges.csv")
        a = documents / "a.txt"
        before = _prepare_extraction(_inputs(tmp_path, config, documents))["fingerprints"][a]
        rows[0]["short_name"] = "Corrected"
        after = _prepare_extraction(_inputs(tmp_path, config, documents))["fingerprints"][a]
        assert before == after

    def test_shared_scope_documents_are_unaffected(self, domain, tmp_path):
        from psweep.pipeline import _prepare_extraction

        config, rows, documents = domain
        _index(documents, rows)
        text = (documents / "download_index.csv").read_text().replace(",target,", ",shared,")
        (documents / "download_index.csv").write_text(text)
        a = documents / "a.txt"
        before = _prepare_extraction(_inputs(tmp_path, config, documents))["fingerprints"][a]
        rows[0]["short_name"] = "Corrected"
        after = _prepare_extraction(_inputs(tmp_path, config, documents))["fingerprints"][a]
        assert before == after

    def test_absent_key_adds_nothing_to_record_identity(self, domain, tmp_path):
        from psweep.pipeline import _prepare_extraction

        config, rows, documents = domain
        del config["extraction"]["pages"]["auto_locate"]["subject_fields"]
        _index(documents, rows)
        plan = _prepare_extraction(_inputs(tmp_path, config, documents))
        assert plan["subject_phrases"] is None
