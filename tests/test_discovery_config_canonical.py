"""Canonical discovery config: strict keys, resolution, and explicit-file boundaries."""

import json
from pathlib import Path
import re

import pytest
import yaml

from psweep.config.discovery_config import resolve_discovery_section
from psweep.config.runtime_config_loader import (
    RuntimeConfigError,
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.discovery.request_inputs import discovery_request_fields


@pytest.mark.parametrize(
    "discovery,message",
    [
        ({"seeds": ["https://a.gov"]}, "unknown key 'discovery.seeds'"),
        ({"targets_csv": "t.csv"}, "unknown key 'discovery.targets_csv' — did you mean 'targets'?"),
        ({"querys": ["x"]}, "did you mean 'queries'?"),
        ({"crawl": {"max_dept": 1}}, "unknown key 'discovery.crawl.max_dept' — did you mean 'max_depth'?"),
        ({"follow_links": True}, "unknown key 'discovery.follow_links'"),
        ({"search": {"enabled": True}}, "unknown key 'discovery.search.enabled'"),
        ({"selection": {"primary_per_target": 3}}, "discovery.selection.primary_per_target"),
        ({"document_review": {"action": "move"}}, "discovery.document_review.action"),
    ],
)
def test_unknown_keys_fail_with_path_and_suggestion(tmp_path: Path, discovery, message):
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump({"discovery": discovery}), encoding="utf-8")
    with pytest.raises(RuntimeConfigError) as exc:
        load_runtime_config_file(path)
    assert str(exc.value).startswith(f"{path}: ")
    assert message in str(exc.value)


@pytest.mark.parametrize(
    "discovery,match",
    [
        ({"sources": ["ftp://a.gov/x"]}, r"discovery\.sources"),
        ({"crawl": {"max_depth": "1"}}, r"discovery\.crawl\.max_depth"),
        ({"crawl": {"browser_downloads": "false"}}, r"discovery\.crawl\.browser_downloads"),
        ({"crawl": {"include_url_patterns": ["("]}}, r"discovery\.crawl\.include_url_patterns"),
        ({"search": {"params": {"api_key": "x"}}}, r"api_key"),
    ],
)
def test_invalid_values_fail(discovery, match):
    with pytest.raises(RuntimeConfigError, match=match):
        resolve_discovery_section(discovery, None)


def test_canonical_config_resolves_request_fields(tmp_path: Path):
    path = tmp_path / "run.yaml"
    path.write_text(
        "discovery:\n"
        "  sources: [https://county.gov/index]\n"
        "  queries: ['{name} manual pdf']\n"
        "  targets: [{name: Pump, urls: [https://pump.example/a.pdf]}]\n"
        "  search: {max_results: 7}\n"
        "  selection: {max_per_target: 3, require_any: [manual], exclude: [news]}\n"
        "  crawl: {provider: selenium, max_depth: 1, browser_downloads: true}\n"
        "  runtime: {max_files: 4}\n",
        encoding="utf-8",
    )
    resolved = resolve_command_config(
        command="discover",
        cli_values={"domain": "manuals"},
        config_data=load_runtime_config_file(path),
    )
    fields = discovery_request_fields(resolved)
    assert fields["sources"] == ["https://county.gov/index"]
    assert fields["queries"] == ["{name} manual pdf"]
    assert fields["targets"] == [{"name": "Pump", "urls": ["https://pump.example/a.pdf"]}]
    assert fields["seeker_max_results"] == 7
    assert fields["selection_primary_per_target"] == 3
    assert fields["selection_relevance_require_any_terms"] == ["manual"]
    assert fields["crawl_provider"] == "selenium"
    assert fields["max_depth"] == 1
    assert fields["browser_downloads"] is True
    assert fields["max_files"] == 4
    assert resolved["_config_sources"]["max_depth"] == "config.discovery.crawl.max_depth"


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
        json.dumps({"discovery": {"queries": ["manual"]}}),
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
        json.dumps({"discovery": {"queries": ["original"]}}),
        encoding="utf-8",
    )
    (tmp_path / "discovery.yaml").write_text(
        "queries: [override]\n",
        encoding="utf-8",
    )
    loaded = load_runtime_config_file(path)
    assert loaded["discovery"] == {"queries": ["original"]}
    assert "extraction" not in loaded


@pytest.mark.parametrize("suffix", [".yaml", ".yml", ".json"])
@pytest.mark.parametrize(
    "section,body",
    [
        ("discovery", {"queries": ["manual"], "search": {"max_results": 7}}),
        ("extraction", {"skip_existing": True, "max_context": 1000}),
        ("compilation", {"dry_run": True}),
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
    path.write_text("discovery:\n  queries: [manual]\n", encoding="utf-8")
    (tmp_path / "qaqc.yml").write_text("models: [a, b]\n", encoding="utf-8")
    with pytest.raises(
        RuntimeConfigError,
        match="renamed to 'validation'",
    ) as exc:
        load_runtime_config_file(path)
    assert "validation:" in str(exc.value)
    assert "remove" in str(exc.value)
