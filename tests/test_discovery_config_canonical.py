"""Canonical discovery config and explicit-file migration boundaries."""

import json
from pathlib import Path
import re

import pytest
import yaml

from psweep.config.runtime_config_loader import (
    RuntimeConfigError,
    _ACQ_SEARCH_KEY_MAP,
    _ACQ_SELECTION_KEY_MAP,
    _ACQUISITION_OBJECT_ALLOWED_KEYS,
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.discovery.request_inputs import (
    normalize_discover_inputs,
    validate_discover_plan,
)


_RETIRED_CASES = [
    (
        {"seeker": {"provider": "serpapi"}},
        "discovery.seeker",
        "discovery.search",
    ),
    ({"browser": True}, "discovery.browser", "discovery.browser_mode"),
    (
        {"search": {"max_results_per_query": 7}},
        "discovery.search.max_results_per_query",
        "discovery.search.max_results",
    ),
    (
        {"selection": {"primary_per_target": 3}},
        "discovery.selection.primary_per_target",
        "discovery.selection.max_per_target",
    ),
    (
        {"selection": {"relevance_require_any_terms": ["manual"]}},
        "discovery.selection.relevance_require_any_terms",
        "discovery.selection.require_any",
    ),
    (
        {"selection": {"relevance_exclude_any_terms": ["news"]}},
        "discovery.selection.relevance_exclude_any_terms",
        "discovery.selection.exclude",
    ),
]


@pytest.mark.parametrize("discovery,retired,replacement", _RETIRED_CASES)
def test_load_rejects_retired_discovery_keys_with_migration(
    tmp_path: Path,
    discovery: dict,
    retired: str,
    replacement: str,
):
    """Loading retired syntax reports its canonical replacement."""
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump({"discovery": discovery}), encoding="utf-8")
    with pytest.raises(RuntimeConfigError, match=re.escape(retired)) as exc:
        load_runtime_config_file(path)
    assert "retired" in str(exc.value)
    assert replacement in str(exc.value)


def test_retired_config_fails_before_target_source_io(tmp_path: Path):
    """Migration errors win over unreadable configured target sources."""
    path = tmp_path / "run.yaml"
    path.write_text(
        "discovery:\n  targets_csv: missing.csv\n  seeker: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(
        RuntimeConfigError,
        match=r"discovery\.seeker.*retired",
    ):
        load_runtime_config_file(path)


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("discovery,retired,replacement", _RETIRED_CASES)
def test_direct_resolve_rejects_retired_keys_regardless_of_strict(
    discovery: dict,
    retired: str,
    replacement: str,
    strict: bool,
):
    """Direct callers cannot bypass migration checks with non-strict mode."""
    with pytest.raises(RuntimeConfigError, match=re.escape(retired)) as exc:
        resolve_command_config(
            command="discover",
            cli_values={},
            config_data={"discovery": discovery},
            strict=strict,
        )
    assert replacement in str(exc.value)


@pytest.mark.parametrize(
    "discovery",
    [
        {"seeker": {"enabled": False}, "search": {"enabled": True}},
        {"browser": True, "browser_mode": False},
        {"search": {"max_results_per_query": 30, "max_results": 4}},
        {"selection": {"primary_per_target": 20, "max_per_target": 2}},
        {
            "selection": {
                "relevance_require_any_terms": ["old"],
                "require_any": ["new"],
            }
        },
        {
            "selection": {
                "relevance_exclude_any_terms": ["old"],
                "exclude": ["new"],
            }
        },
    ],
)
def test_retired_alias_conflicts_never_silently_override(discovery: dict):
    """Canonical syntax alongside an alias remains a migration error."""
    with pytest.raises(RuntimeConfigError, match="retired"):
        resolve_command_config(
            command="discover",
            cli_values={},
            config_data={"discovery": discovery},
            strict=False,
        )


def test_search_and_selection_allowed_keys_share_merge_maps():
    """Accepted mapped controls and merge controls have one source of truth."""
    search_keys = set(_ACQ_SEARCH_KEY_MAP) | {
        "provider",
        "enabled",
        "serpapi_params",
        "google_news",
    }
    assert _ACQUISITION_OBJECT_ALLOWED_KEYS["search"] == search_keys
    assert _ACQUISITION_OBJECT_ALLOWED_KEYS["selection"] == set(
        _ACQ_SELECTION_KEY_MAP
    )
    assert "seeker" not in _ACQUISITION_OBJECT_ALLOWED_KEYS


def test_canonical_search_family_resolves_and_enables_search(tmp_path: Path):
    """Named query families and canonical controls reach request inputs."""
    path = tmp_path / "run.yaml"
    path.write_text(
        "discovery:\n"
        "  query_families:\n    manuals: ['{manufacturer} manual pdf']\n"
        "  search:\n    use_query_family: manuals\n    max_results: 7\n"
        "  selection:\n    max_per_target: 3\n"
        "    require_any: [manual]\n    exclude: [news]\n"
        "    relevance_require_legal_marker_terms: [chapter]\n"
        "  browser_mode: true\n",
        encoding="utf-8",
    )
    resolved = resolve_command_config(
        command="discover",
        cli_values={},
        config_data=load_runtime_config_file(path),
        strict=True,
    )
    normalized = normalize_discover_inputs(resolved)
    validate_discover_plan(normalized)
    assert normalized["enable_serpapi"] is True
    assert normalized["use_query_family"] == "manuals"
    assert normalized["query_families"] == {
        "manuals": ["{manufacturer} manual pdf"],
    }
    assert normalized["seeker_max_results"] == 7
    assert normalized["selection_primary_per_target"] == 3
    assert normalized["selection_relevance_require_any_terms"] == ["manual"]
    assert normalized["selection_relevance_exclude_any_terms"] == ["news"]
    assert normalized["selection_relevance_require_legal_marker_terms"] == [
        "chapter",
    ]
    assert normalized["browser_mode"] is True
    assert resolved["_config_sources"]["use_query_family"] == (
        "config.discovery.search.use_query_family"
    )


def test_query_crawl_delay_shorthands_preserve_full_form_precedence():
    """Shorthands still work, with explicit full-form settings winning."""
    config = {
        "discovery": {
            "queries": ["manual"],
            "follow_links": True,
            "request_delay_ms": 200,
        }
    }
    resolved = resolve_command_config(
        command="discover",
        cli_values={},
        config_data=config,
        strict=True,
    )
    normalized = normalize_discover_inputs(resolved)
    assert normalized["query_families"] == {"_default": ["manual"]}
    assert normalized["use_query_family"] == "_default"
    assert normalized["enable_serpapi"] is True
    assert normalized["topology_mode"] == "distributed"
    assert normalized["min_request_interval_ms"] == 200

    config["discovery"].update(
        {
            "query_families": {"named": ["spec"]},
            "search": {"use_query_family": "named", "enabled": False},
            "topology": {"mode": "centralized"},
            "runtime": {"min_request_interval_ms": 300},
        }
    )
    resolved = resolve_command_config(
        command="discover",
        cli_values={},
        config_data=config,
        strict=True,
    )
    normalized = normalize_discover_inputs(resolved)
    assert normalized["query_families"] == {"named": ["spec"]}
    assert normalized["use_query_family"] == "named"
    assert normalized["enable_serpapi"] is False
    assert normalized["topology_mode"] == "centralized"
    assert normalized["min_request_interval_ms"] == 300


@pytest.mark.parametrize("run_suffix", [".yaml", ".yml", ".json"])
@pytest.mark.parametrize(
    "section",
    ["discovery", "extraction", "compilation", "validation"],
)
@pytest.mark.parametrize("section_suffix", [".yaml", ".yml", ".json"])
def test_run_rejects_adjacent_section_files_without_reading_them(
    tmp_path: Path,
    run_suffix: str,
    section: str,
    section_suffix: str,
):
    """Every supported adjacent section extension fails before file parsing."""
    path = tmp_path / f"run{run_suffix}"
    path.write_text(
        json.dumps({"discovery": {"query": "manual"}}),
        encoding="utf-8",
    )
    adjacent = tmp_path / f"{section}{section_suffix}"
    adjacent.write_text("invalid content: [", encoding="utf-8")
    with pytest.raises(
        RuntimeConfigError,
        match="Implicit section override files",
    ) as exc:
        load_runtime_config_file(path)
    assert adjacent.name in str(exc.value)
    assert "Move" in str(exc.value)
    assert "remove" in str(exc.value)


@pytest.mark.parametrize("stem", ["custom", "run_backup", "run_discovery"])
@pytest.mark.parametrize("suffix", [".yaml", ".yml", ".json"])
def test_non_run_stem_does_not_auto_load_adjacent_sections(
    tmp_path: Path,
    stem: str,
    suffix: str,
):
    """A non-run filename never loads any neighboring section config."""
    path = tmp_path / f"{stem}{suffix}"
    path.write_text(
        json.dumps({"discovery": {"query": "original"}}),
        encoding="utf-8",
    )
    (tmp_path / "discovery.yaml").write_text(
        "query: override\n",
        encoding="utf-8",
    )
    loaded = load_runtime_config_file(path)
    assert loaded["discovery"] == {"query": "original"}
    assert "extraction" not in loaded


@pytest.mark.parametrize("suffix", [".yaml", ".yml", ".json"])
@pytest.mark.parametrize(
    "section,body",
    [
        ("discovery", {"query": "manual", "search": {"max_results": 7}}),
        ("extraction", {"input_dir": "docs", "schema": "schema.json"}),
        ("compilation", {"input_dir": "extracted", "schema": "schema.json"}),
        ("validation", {"models": ["gpt-4o", "gpt-4o-mini"]}),
    ],
)
def test_explicit_section_config_is_supported(
    tmp_path: Path,
    section: str,
    body: dict,
    suffix: str,
):
    """Explicit section-wrapped files work independently of adjacent runs."""
    path = tmp_path / f"{section}{suffix}"
    path.write_text(json.dumps({section: body}), encoding="utf-8")
    (tmp_path / "run.yaml").write_text(
        "unknown_key: true\n",
        encoding="utf-8",
    )
    assert load_runtime_config_file(path)[section] == body


def test_adjacent_qaqc_file_retains_validation_rename_diagnostic(
    tmp_path: Path,
):
    """Retired QA/QC filenames retain actionable validation rename guidance."""
    path = tmp_path / "run.yaml"
    path.write_text("discovery:\n  query: manual\n", encoding="utf-8")
    (tmp_path / "qaqc.yml").write_text("models: [a, b]\n", encoding="utf-8")
    with pytest.raises(
        RuntimeConfigError,
        match="renamed to 'validation'",
    ) as exc:
        load_runtime_config_file(path)
    assert "validation:" in str(exc.value)
    assert "remove" in str(exc.value)
