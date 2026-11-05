from pathlib import Path

import yaml
import pytest

from psweep.config.runtime_config_loader import (
    RuntimeConfigError,
    catalog_for_command,
    load_runtime_config_file,
    resolve_command_config,
)


def test_load_runtime_config_file_rejects_unknown_top_level(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text("unknown_key: true\n", encoding="utf-8")

    with pytest.raises(RuntimeConfigError, match="unknown key 'unknown_key'"):
        load_runtime_config_file(config_path)


def test_resolve_command_config_merges_cli_and_file_values(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: geothermal_ordinances
schema: config/geothermal_ordinances/schema.json
extraction:
  skip_existing: true
  max_context: 111111
  timeout_seconds: 480
""",
        encoding="utf-8",
    )
    config_data = load_runtime_config_file(config_path)

    resolved = resolve_command_config(
        command="extract",
        cli_values={"max_context": 222222},
        config_data=config_data,
    )

    assert resolved["path"] == "outputs/geothermal_ordinances/discovered/curated"
    assert resolved["schema"] == "config/geothermal_ordinances/schema.json"
    assert resolved["output"] == "outputs/geothermal_ordinances/extracted"
    assert resolved["_config_sources"]["output"] == "derived"
    assert resolved["max_context"] == 222222
    assert resolved["timeout_seconds"] == 480
    assert resolved["_config_sources"]["max_context"] == "cli"


def test_resolve_command_config_strict_unknown_section_key_raises(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
schema: config/geothermal_ordinances/schema.json
compilation:
  random_flag: true
""",
        encoding="utf-8",
    )
    config_data = load_runtime_config_file(config_path)

    with pytest.raises(RuntimeConfigError, match="unknown key 'compilation.random_flag'"):
        resolve_command_config(
            command="compile",
            cli_values={},
            config_data=config_data,
        )


def test_load_runtime_config_file_rejects_invalid_compilation_provenance_policy(
    tmp_path: Path,
):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
schema: config/geothermal_ordinances/schema.json
compilation:
  provenance_policy: maybe
""",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigError, match="compilation.provenance_policy"):
        load_runtime_config_file(config_path)


def test_resolve_command_config_maps_compilation_provenance_policy(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
schema: config/geothermal_ordinances/schema.json
compilation:
  provenance_policy: FAIL
""",
        encoding="utf-8",
    )
    config_data = load_runtime_config_file(config_path)

    resolved = resolve_command_config(
        command="compile",
        cli_values={},
        config_data=config_data,
    )

    assert resolved["provenance_policy"] == "fail"


def test_runtime_catalog_is_derived_from_the_config_schemas():
    from psweep.config import VARIABLE_CATALOG
    from psweep.config.discovery_config import DISCOVERY_SCHEMA
    from psweep.config.runtime_config_loader import (
        _ALLOWED_SECTION_FIELDS,
        _ALLOWED_TOP_LEVEL,
    )

    names = {
        section: [entry["name"] for entry in entries]
        for section, entries in VARIABLE_CATALOG.items()
    }
    assert names["discovery"] == list(DISCOVERY_SCHEMA)
    assert {"search", "link_prioritization", "retry_policy", "partition_by"} <= set(
        names["discovery"]
    )
    for section, allowed in _ALLOWED_SECTION_FIELDS.items():
        assert set(names[section]) == set(allowed), section
    assert set(names["global"]) == _ALLOWED_TOP_LEVEL - set(_ALLOWED_SECTION_FIELDS) - {
        "discovery"
    }
    assert all(len(set(entries)) == len(entries) for entries in names.values())


@pytest.mark.parametrize("command", ["discovery", "extraction", "compilation", "validation"])
def test_commands_resolve_by_command_name_only(command: str):
    with pytest.raises(RuntimeConfigError, match="Unsupported command"):
        catalog_for_command(command)


def test_catalog_for_command_contains_required_and_advanced_fields():
    entries = catalog_for_command("extract")
    names = {entry["name"] for entry in entries}
    levels = {entry["name"]: entry["level"] for entry in entries}

    assert "input_dir" not in names
    assert "max_concurrent" in names
    assert "schema" in names
    assert levels["schema"] == "optional"
    assert levels["max_context"] == "advanced"
    assert levels["timeout_seconds"] == "advanced"


@pytest.mark.parametrize("timeout_value", [0, -1, True, "600"])
def test_load_runtime_config_file_rejects_invalid_extraction_timeout(
    tmp_path: Path, timeout_value
):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        f"""
schema: config/geothermal_ordinances/schema.json
extraction:
  skip_existing: true
  timeout_seconds: {timeout_value!r}
""",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigError, match="extraction.timeout_seconds"):
        load_runtime_config_file(config_path)


def test_load_runtime_config_file_accepts_ocr_corrections(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
schema: config/geothermal_ordinances/schema.json
extraction:
  skip_existing: true
  ocr_corrections:
    - {pattern: '\\bIbs\\b', replacement: 'lbs'}
    - {pattern: 'gaspowered', replacement: 'gas powered', ignore_case: true}
""",
        encoding="utf-8",
    )
    loaded = load_runtime_config_file(config_path)
    rules = loaded["extraction"]["ocr_corrections"]
    assert rules[0]["replacement"] == "lbs"
    assert rules[1]["ignore_case"] is True


@pytest.mark.parametrize(
    ("rules_block", "match"),
    [
        ("    - {pattern: '', replacement: 'x'}\n", "pattern must be a non-empty"),
        ("    - {pattern: 'a', bogus: 1}\n", "unknown key"),
        ("    - {pattern: '('}\n", "replacement must be a non-empty"),
        ("    - {pattern: '(', replacement: 'x'}\n", "not a valid regular expression"),
        ("    - 'not-an-object'\n", "must be an object"),
    ],
)
def test_load_runtime_config_file_rejects_invalid_ocr_corrections(
    tmp_path: Path, rules_block: str, match: str
):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        "schema: config/geothermal_ordinances/schema.json\n"
        "extraction:\n"
        "  skip_existing: true\n"
        "  ocr_corrections:\n" + rules_block,
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigError, match=match):
        load_runtime_config_file(config_path)


def test_load_runtime_config_file_accepts_valid_discovery_section(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
discovery:
  sources:
    - https://example.org/docs
  queries:
    - "{jurisdiction} {state} geothermal ordinance pdf"
  crawl:
    max_depth: 1
    max_pages: 50
  runtime:
    max_concurrent_downloads: 3
    min_request_interval_ms: 150
  policy:
    robots_mode: warn
    tos_mode: enforce
    acknowledged_tos_domains:
      - county.gov
""",
        encoding="utf-8",
    )

    loaded = load_runtime_config_file(config_path)
    assert loaded["discovery"]["crawl"]["max_depth"] == 1
    assert loaded["discovery"]["runtime"]["max_concurrent_downloads"] == 3
    assert loaded["discovery"]["policy"]["robots_mode"] == "warn"



@pytest.mark.parametrize(
    ("discovery_block", "match"),
    [
        (
            "  runtime:\n    max_concurrent_downloads: 0\n    min_request_interval_ms: -1\n",
            "discovery.runtime.max_concurrent_downloads",
        ),
        ("  policy:\n    robots_mode: maybe\n", "discovery.policy.robots_mode"),
        ("  crawl:\n    provider: crawlee\n", "discovery.crawl.provider"),
        ("  - not\n  - an\n  - object\n", "'discovery' must be an object"),
    ],
)
def test_load_runtime_config_file_rejects_invalid_discovery_config(
    tmp_path: Path, discovery_block: str, match: str
):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(f"discovery:\n{discovery_block}", encoding="utf-8")

    with pytest.raises(RuntimeConfigError, match=match):
        load_runtime_config_file(config_path)


@pytest.mark.parametrize("flag", [True, False])
def test_load_runtime_config_file_accepts_browser_escalation_bool(
    tmp_path: Path, flag: bool
):
    """The documented bool shorthand for browser_escalation loads (regression)."""
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        f"discovery:\n  crawl:\n    browser_escalation: {str(flag).lower()}\n",
        encoding="utf-8",
    )

    loaded = load_runtime_config_file(config_path)
    assert loaded["discovery"]["crawl"]["browser_escalation"] is flag


def test_load_runtime_config_file_accepts_browser_escalation_mapping(
    tmp_path: Path,
):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        "discovery:\n"
        "  crawl:\n"
        "    browser_escalation:\n"
        "      enabled: true\n"
        "      min_shell_chars: 8000\n"
        "      min_rendered_chars: 10000\n"
        "      settle_seconds: 20\n",
        encoding="utf-8",
    )

    loaded = load_runtime_config_file(config_path)
    assert loaded["discovery"]["crawl"]["browser_escalation"]["min_shell_chars"] == 8000


@pytest.mark.parametrize(
    ("escalation_block", "match"),
    [
        (
            "      enabled: true\n      bogus_key: 1\n",
            "unknown key",
        ),
        ("      min_shell_chars: -5\n", "min_shell_chars' must be an integer >= 0"),
        ("      min_shell_chars: x\n", "min_shell_chars' must be an integer >= 0"),
    ],
)
def test_load_runtime_config_file_rejects_invalid_browser_escalation(
    tmp_path: Path, escalation_block: str, match: str
):
    """Inner-key validation now runs at load time (single source of truth)."""
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        f"discovery:\n  crawl:\n    browser_escalation:\n{escalation_block}",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigError, match=match):
        load_runtime_config_file(config_path)


@pytest.mark.parametrize(
    "override_body",
    [
        "output_dir: processed/from-override\nmax_context: 222222\n",
        "extraction:\n  output_dir: processed/from-override\n",
    ],
)
def test_load_runtime_config_file_rejects_split_processing_override(
    tmp_path: Path, override_body: str,
):
    """Flat and wrapped overrides must be migrated into the run config."""
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        "schema: config/geothermal_ordinances/schema.json\n"
        "extraction:\n"
        "  skip_existing: true\n",
        encoding="utf-8",
    )
    (tmp_path / "extraction.yaml").write_text(override_body, encoding="utf-8")

    with pytest.raises(
        RuntimeConfigError, match="Implicit section override files",
    ) as exc:
        load_runtime_config_file(run_path)
    assert "extraction.yaml" in str(exc.value)
    assert "run.yaml" in str(exc.value)


def test_load_runtime_config_file_rejects_all_split_files_for_same_section(
    tmp_path: Path,
):
    """Every conflicting override is listed in the migration error."""
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        "schema: s.json\nextraction:\n  skip_existing: true\n", encoding="utf-8",
    )
    (tmp_path / "extraction.yaml").write_text(
        "output_dir: one\n", encoding="utf-8",
    )
    (tmp_path / "extraction.json").write_text(
        '{"output_dir": "two"}', encoding="utf-8",
    )

    with pytest.raises(
        RuntimeConfigError, match="Implicit section override files",
    ) as exc:
        load_runtime_config_file(run_path)
    assert "extraction.yaml" in str(exc.value)
    assert "extraction.json" in str(exc.value)


def test_resolve_command_config_maps_crawl_and_runtime_fields(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
domain: demo
discovery:
  sources:
    - https://county.gov/hub
  crawl:
    provider: selenium
    max_depth: 2
    max_pages: 25
    allowed_domains:
      - county.gov
    include_url_patterns:
      - \\.pdf$
    include_link_text_patterns:
      - ordinance
    request_headers:
      User-Agent: "ParseSweep/2.0 (contact: example@example.com)"
  runtime:
    max_files: 8
    timeout_seconds: 12
    max_concurrent_downloads: 4
    min_request_interval_ms: 120
  retry_policy:
    max_attempts: 5
    initial_backoff_seconds: 2
""",
        encoding="utf-8",
    )

    resolved = resolve_command_config(
        command="discover", cli_values={}, config_data=load_runtime_config_file(run_path),
    )

    assert resolved["sources"] == ["https://county.gov/hub"]
    assert resolved["crawl_provider"] == "selenium"
    assert resolved["_config_sources"]["crawl_provider"] == "config.discovery.crawl.provider"
    assert resolved["allowed_domains"] == ["county.gov"]
    assert resolved["include_url_patterns"] == [r"\.pdf$"]
    assert resolved["include_link_text_patterns"] == ["ordinance"]
    assert resolved["request_headers"] == {
        "User-Agent": "ParseSweep/2.0 (contact: example@example.com)"
    }
    assert (resolved["max_depth"], resolved["max_pages"], resolved["max_files"]) == (2, 25, 8)
    assert resolved["timeout_seconds"] == 12
    assert resolved["max_concurrent_downloads"] == 4
    assert resolved["min_request_interval_ms"] == 120
    assert resolved["retry_max_attempts"] == 5
    assert resolved["retry_initial_backoff_seconds"] == 2



def test_resolve_command_config_maps_link_prioritization_block(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
discovery:
    sources:
        - https://example.org/hub
    link_prioritization:
        mode: heuristic
        top_k: 12
        keywords:
            - tariff
            - rate schedule
        domain_scores:
            utility.com: 0.95
""",
                encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        resolved = resolve_command_config(
                command="discover",
                cli_values={},
                config_data=config_data,
        )

        assert resolved["link_prioritization_mode"] == "heuristic"
        assert resolved["link_top_k"] == 12
        assert resolved["link_prioritization_keywords"] == ["tariff", "rate schedule"]
        assert resolved["link_prioritization_domain_scores"] == {"utility.com": 0.95}


def test_resolve_command_config_maps_selection_relevance_terms(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
discovery:
    sources:
        - https://example.org/hub
    selection:
        require_any:
            - ordinance
            - municipal code
        exclude:
            - state brief
            - specific plan
        require_supported_document: true
""",
                encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        resolved = resolve_command_config(
                command="discover",
                cli_values={},
                config_data=config_data,
        )

        assert resolved["selection_relevance_require_any_terms"] == ["ordinance", "municipal code"]
        assert resolved["selection_relevance_exclude_any_terms"] == ["state brief", "specific plan"]
        assert resolved["selection_require_supported_document"] is True


def test_resolve_command_config_maps_split_selection_exclusions(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
discovery:
  sources:
        - https://example.org/a
  selection:
        max_per_host_per_target: 2
        exclude_url_patterns:
          - archive
        exclude_text_patterns:
          - preliminary
""",
                encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        resolved = resolve_command_config(
                command="discover",
                cli_values={},
                config_data=config_data,
        )

        assert resolved["selection_max_per_host_per_target"] == 2
        assert resolved["selection_exclude_url_patterns"] == ["archive"]
        assert resolved["selection_exclude_text_patterns"] == ["preliminary"]


def test_resolve_command_config_maps_generic_selection_templates(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
discovery:
    sources:
        - https://example.org/hub
    selection:
        max_per_target: 3
        exclude_draft: false
        draft_patterns:
            - draft
            - proposed
        target_require_any:
            - "{jurisdiction}"
            - "{manufacturer}"
        target_require_all:
            - "{state}"
        target_exclude:
            - "sample"
""",
                encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        resolved = resolve_command_config(
                command="discover",
                cli_values={},
                config_data=config_data,
        )

        assert resolved["selection_primary_per_target"] == 3
        assert resolved["selection_exclude_draft"] is False
        assert resolved["selection_draft_patterns"] == ["draft", "proposed"]
        assert resolved["selection_target_require_any"] == ["{jurisdiction}", "{manufacturer}"]
        assert resolved["selection_target_require_all"] == ["{state}"]
        assert resolved["selection_target_exclude"] == ["sample"]


def test_discovery_selection_max_per_target_must_cover_review_keep_top(
    tmp_path: Path,
):
    """Acquisition must provide enough candidates for configured review."""
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
discovery:
  sources:
        - https://example.org/hub
  selection:
        max_per_target: 2
  document_review:
        keep_top: 3
""",
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeConfigError,
        match=r"selection\.max_per_target' must be >=",
    ):
        load_runtime_config_file(run_path)


def test_discovery_selection_and_review_accept_equal_limits(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
            """
discovery:
  sources:
        - https://example.org/hub
  selection:
        max_per_target: 3
  document_review:
        keep_top: 3
""",
            encoding="utf-8",
        )

        data = load_runtime_config_file(run_path)
        resolved = resolve_command_config(
            command="discover",
            cli_values={},
            config_data=data,
        )
        assert resolved["selection_primary_per_target"] == 3
        assert resolved["document_review"]["keep_top"] == 3


def test_resolve_command_config_maps_discovery_policy_fields(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
discovery:
    sources:
        - https://example.org/hub
    policy:
        robots_mode: enforce
        tos_mode: warn
        acknowledged_tos_domains:
            - example.org
            - county.gov
""",
                encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        resolved = resolve_command_config(
                command="discover",
                cli_values={},
                config_data=config_data,
        )

        assert resolved["robots_policy_mode"] == "enforce"
        assert resolved["tos_policy_mode"] == "warn"
        assert resolved["acknowledged_tos_domains"] == ["example.org", "county.gov"]


def test_resolve_command_config_maps_targets_queries_and_search(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
discovery:
  targets:
    - manufacturer: Generac
      power_class_kw: 200-300
    - manufacturer: John Deere
      power_class_kw: 200-300
  queries:
    - "{manufacturer} {power_class_kw} kW generator spec pdf"
  search:
    max_results: 7
    params:
      tbm: nws
""",
        encoding="utf-8",
    )

    resolved = resolve_command_config(
        command="discover", cli_values={}, config_data=load_runtime_config_file(run_path),
    )

    assert len(resolved["targets"]) == 2
    assert resolved["queries"][0].startswith("{manufacturer}")
    assert resolved["seeker_max_results"] == 7
    assert resolved["seeker_params"] == {"tbm": "nws"}



def test_models_block_flows_to_every_command(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: demo
models:
  fast: gpt-4o-mini
  accurate: gpt-5
discovery:
  queries: ["a"]
schema: config/x/schema.json
extraction:
  skip_existing: true
compilation:
  dry_run: false
""",
        encoding="utf-8",
    )
    data = load_runtime_config_file(config_path)
    assert data["models"] == {"fast": "gpt-4o-mini", "accurate": "gpt-5"}
    for command in ("discover", "extract", "compile"):
        merged = resolve_command_config(
            command=command, cli_values={}, config_data=data
        )
        assert merged["models"] == {"fast": "gpt-4o-mini", "accurate": "gpt-5"}


def test_models_block_rejects_non_mapping(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text("models:\n  - gpt-4o-mini\n", encoding="utf-8")
    with pytest.raises(RuntimeConfigError, match="'models' must be a mapping"):
        load_runtime_config_file(config_path)


def test_models_block_rejects_empty_model_name(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text("models:\n  fast: ''\n", encoding="utf-8")
    with pytest.raises(RuntimeConfigError, match="non-empty"):
        load_runtime_config_file(config_path)


# ── Processing deep validator + model_context_windows (process audit) ─────────


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "run.yaml"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.mark.parametrize(
    ("snippet", "match"),
    [
        ("max_context: not-an-int\n", "max_context"),
        ("provider: bogus\n", "provider"),
        ("pages:\n    auto_locate:\n      trigger_chars: -5\n", "trigger_chars"),
    ],
)
def test_processing_validator_rejects_invalid_fields(
    tmp_path: Path, snippet: str, match: str
):
    cfg = _write(
        tmp_path,
        "schema: s.json\nextraction:\n  skip_existing: true\n  " + snippet,
    )
    with pytest.raises(RuntimeConfigError, match=match):
        load_runtime_config_file(cfg)


def test_processing_validator_accepts_valid_block(tmp_path: Path):
    cfg = _write(
        tmp_path,
        "schema: s.json\nextraction:\n  skip_existing: true\n  max_context: 600000\n"
        "  provider: azure\n"
        "  pages:\n    auto_locate:\n"
        "      section_description: the rate tables\n"
        "      trigger_chars: 200000\n"
        "      max_selected_pages: 30\n",
    )
    loaded = load_runtime_config_file(cfg)
    assert loaded["extraction"]["max_context"] == 600000


@pytest.mark.parametrize(
    ("pages_block", "expected_csv", "expected_section_description"),
    [
        (
            "  pages:\n    csv: ranges.csv\n    auto_locate:\n      section_description: rate tables\n      trigger_chars: 200000\n",
            "ranges.csv",
            "rate tables",
        ),
        (
            "  pages:\n    auto_locate:\n      section_description: the charges section\n",
            None,
            "the charges section",
        ),
        ("  pages:\n    csv: my_ranges.csv\n", "my_ranges.csv", None),
    ],
)
def test_pages_block_variants(
    tmp_path: Path,
    pages_block: str,
    expected_csv: str | None,
    expected_section_description: str | None,
):
    cfg = _write(
        tmp_path,
        "schema: s.json\nextraction:\n  skip_existing: true\n" + pages_block,
    )
    loaded = load_runtime_config_file(cfg)
    ext = loaded["extraction"]

    if expected_csv is None:
        assert "pages_csv" not in ext
    else:
        assert ext["pages_csv"] == (tmp_path / expected_csv).as_posix()

    if expected_section_description is None:
        assert "page_targeting" not in ext
    else:
        assert ext["page_targeting"]["enabled"] is True
        assert (
            ext["page_targeting"]["section_description"]
            == expected_section_description
        )


def test_pages_auto_locate_accepts_save_discovered(tmp_path: Path):
    """save_discovered is consumed by extract; a bool must be accepted."""
    cfg = _write(
        tmp_path,
        "schema: s.json\nextraction:\n  skip_existing: true\n"
        "  pages:\n    auto_locate:\n"
        "      section_description: the rate tables\n"
        "      save_discovered: true\n",
    )
    loaded = load_runtime_config_file(cfg)
    assert loaded["extraction"]["page_targeting"]["save_discovered"] is True


def test_pages_auto_locate_rejects_non_bool_save_discovered(tmp_path: Path):
    """A mistyped (non-bool) save_discovered fails loudly instead of no-op'ing."""
    cfg = _write(
        tmp_path,
        "schema: s.json\nextraction:\n  skip_existing: true\n"
        "  pages:\n    auto_locate:\n"
        "      section_description: the rate tables\n"
        "      save_discovered: yesplease\n",
    )
    with pytest.raises(
        RuntimeConfigError, match="save_discovered must be a boolean"
    ):
        load_runtime_config_file(cfg)


@pytest.mark.parametrize(
    ("pages_block", "match"),
    [
        ("  pages:\n    csvv: r.csv\n", "unknown key 'extraction.pages.csvv' — did you mean 'csv'"),
        (
            "  pages:\n    auto_locate:\n      section_description: x\n      trigger_char: 5\n",
            "unknown key 'extraction.pages.auto_locate.trigger_char' — did you mean 'trigger_chars'",
        ),
    ],
)
def test_pages_block_rejects_unknown_sub_keys(tmp_path: Path, pages_block: str, match: str):
    cfg = _write(tmp_path, "schema: s.json\nextraction:\n" + pages_block)
    with pytest.raises(RuntimeConfigError, match=match):
        load_runtime_config_file(cfg)


def test_pages_csv_resolves_relative_to_the_config_directory(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / "config" / "demo"
    config_dir.mkdir(parents=True)
    cfg = _write(config_dir, "schema: s.json\nextraction:\n  pages:\n    csv: ranges.csv\n")
    monkeypatch.chdir(tmp_path)

    loaded = load_runtime_config_file(cfg)
    resolved = resolve_command_config(command="extract", cli_values={}, config_data=loaded)

    assert loaded["extraction"]["pages_csv"] == (config_dir / "ranges.csv").as_posix()
    assert resolved["pages_csv"] == (config_dir / "ranges.csv").as_posix()


def test_old_flat_pages_keys_rejected_beside_the_pages_block(tmp_path: Path):
    cfg = _write(
        tmp_path,
        "schema: s.json\nextraction:\n  pages:\n    csv: r.csv\n  pages_csv: old.csv\n",
    )
    with pytest.raises(RuntimeConfigError, match="unknown key 'extraction.pages_csv' — did you mean 'pages'"):
        load_runtime_config_file(cfg)


@pytest.mark.parametrize("value", ["10-35", "[1, 2]", "true"])
def test_pages_must_be_a_mapping(tmp_path: Path, value: str):
    cfg = _write(tmp_path, f"schema: s.json\nextraction:\n  pages: {value}\n")
    with pytest.raises(RuntimeConfigError, match="extraction.pages must be a mapping"):
        load_runtime_config_file(cfg)


def test_old_flat_pages_keys_rejected(tmp_path: Path):
    """Internal pages_csv / page_targeting keys fail like any unknown key."""
    cfg = _write(
        tmp_path,
        "schema: s.json\nextraction:\n  skip_existing: true\n"
        "  pages_csv: old.csv\n"
        "  page_targeting:\n    enabled: true\n"
        "    section_description: old style\n",
    )
    with pytest.raises(RuntimeConfigError, match="unknown key 'extraction.pages_csv'"):
        load_runtime_config_file(cfg)


def test_model_context_windows_validates_and_passes_through(tmp_path: Path):
    cfg = _write(
        tmp_path,
        "model_context_windows:\n  my-deploy: 300000\n"
        "schema: s.json\nextraction:\n  skip_existing: true\n",
    )
    config_data = load_runtime_config_file(cfg)
    resolved = resolve_command_config(
        command="extract", cli_values={}, config_data=config_data
    )
    assert resolved["model_context_windows"] == {"my-deploy": 300000}


def test_model_context_windows_rejects_non_positive(tmp_path: Path):
    cfg = _write(
        tmp_path,
        "model_context_windows:\n  my-deploy: 0\n",
    )
    with pytest.raises(RuntimeConfigError, match="positive integer"):
        load_runtime_config_file(cfg)


def test_compilation_synthesis_block_is_rejected(tmp_path: Path):
    from psweep.config.runtime_config_loader import resolve_command_config

    cfg = load_runtime_config_file(
        _write(tmp_path, "schema: s.json\ncompilation:\n  synthesis: {enabled: true}\n")
    )
    with pytest.raises(RuntimeConfigError, match="synthesis"):
        resolve_command_config(command="compile", cli_values={}, config_data=cfg)


def test_compile_inputs_carry_discovery_roster(tmp_path: Path):
    from psweep.config.runtime_config_loader import resolve_command_config

    (tmp_path / "targets.csv").write_text("state,ahj\nAZ,Pima County\n", encoding="utf-8")
    cfg = load_runtime_config_file(
        _write(
            tmp_path,
            "schema: s.json\ndiscovery:\n  targets: targets.csv\n"
            "  partition_by: [state, ahj]\n",
        )
    )
    resolved = resolve_command_config(command="compile", cli_values={}, config_data=cfg)
    assert resolved["partition_by"] == ["state", "ahj"]
    assert resolved["targets"]


def _resolve_discovery(body: str) -> dict:
    from psweep.config.runtime_config_loader import resolve_command_config

    return resolve_command_config(
        command="discover", cli_values={}, config_data=yaml.safe_load(body)
    )


@pytest.mark.parametrize(
    ("block", "match"),
    [
        (
            "  search:\n    max_result: 7\n",
            "unknown key 'discovery.search.max_result' — did you mean 'max_results'",
        ),
        (
            "  retry_policy:\n    max_attempt: 3\n",
            "unknown key 'discovery.retry_policy.max_attempt'",
        ),
        (
            "  document_review:\n    keep_topp: 1\n",
            "unknown key 'discovery.document_review.keep_topp'",
        ),
    ],
)
def test_load_runtime_config_file_rejects_unknown_subblock_key(
    tmp_path: Path, block: str, match: str
):
    """A typo inside a consumed discovery sub-block fails loudly (not no-op)."""
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        "discovery:\n  sources:\n    - https://example.org/a\n" + block,
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigError, match=match):
        load_runtime_config_file(config_path)


def test_load_runtime_config_file_accepts_known_subblock_keys(tmp_path: Path):
    """search.params stays an unrestricted passthrough (shallow check only)."""
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        "discovery:\n"
        "  sources:\n    - https://example.org/a\n"
        "  search:\n"
        "    max_results: 5\n"
        "    params:\n"
        "      tbm: nws\n"
        "      any_verbatim_key: ok\n",
        encoding="utf-8",
    )
    loaded = load_runtime_config_file(config_path)
    assert loaded["discovery"]["search"]["max_results"] == 5


def test_browser_escalation_resolves_to_discovery_inputs():
    """discovery.browser_escalation reaches the engine request payload."""
    resolved = _resolve_discovery(
        "discovery:\n  queries: [q]\n  crawl:\n   browser_escalation:\n"
        "    min_shell_chars: 1500\n    settle_seconds: 20\n"
    )
    assert resolved["browser_escalation"] == {
        "min_shell_chars": 1500,
        "settle_seconds": 20.0,
    }


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (
            "discovery:\n  queries: [q]\n  crawl:\n   browser_escalation:\n    min_shel_chars: 1500\n",
            "unknown key",
        ),
        (
            "discovery:\n  queries: [q]\n  crawl:\n   browser_escalation:\n    min_shell_chars: abc\n",
            "must be an integer >= 0",
        ),
        (
            "discovery:\n  queries: [q]\n  crawl:\n   browser_escalation:\n    settle_seconds: -1\n",
            "must be a finite number >= 0",
        ),
        (
            "discovery:\n  queries: [q]\n  crawl:\n   browser_escalation: [1]\n",
            "must be an object",
        ),
    ],
)
def test_browser_escalation_rejects_invalid_config(body: str, match: str):
    """Typos/invalid values fail loudly instead of silently no-op'ing."""
    with pytest.raises(RuntimeConfigError, match=match):
        _resolve_discovery(body)


def test_code_host_adapters_resolves_to_discovery_inputs():
    """discovery.code_host_adapters reaches the engine request payload."""
    resolved = _resolve_discovery(
        "discovery:\n  queries: [q]\n  crawl:\n   code_host_adapters:\n    enabled: false\n"
    )
    assert resolved["code_host_adapters"] == {"enabled": False}


def test_code_host_adapters_bool_shorthand_resolves():
    """The bool shorthand toggles the whole adapter layer."""
    resolved = _resolve_discovery(
        "discovery:\n  queries: [q]\n  crawl:\n   code_host_adapters: false\n"
    )
    assert resolved["code_host_adapters"] == {"enabled": False}


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (
            "discovery:\n  queries: [q]\n  crawl:\n   code_host_adapters:\n"
            "    enabled: true\n    bogus_key: 1\n",
            "unknown key",
        ),
        (
            "discovery:\n  queries: [q]\n  crawl:\n   code_host_adapters: [1]\n",
            "must be an object",
        ),
    ],
)
def test_code_host_adapters_rejects_invalid_config(body: str, match: str):
    """Typos/invalid shapes fail loudly instead of silently no-op'ing."""
    with pytest.raises(RuntimeConfigError, match=match):
        _resolve_discovery(body)


def test_load_runtime_config_file_rejects_invalid_code_host_adapters(
    tmp_path: Path,
):
    """Inner-key validation runs at load time too (single source of truth)."""
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        "discovery:\n  crawl:\n    code_host_adapters:\n      enabled: true\n      typo: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigError, match="unknown key"):
        load_runtime_config_file(config_path)


def test_link_prioritization_shopping_keywords_resolve(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
discovery:
  sources:
    - https://example.com/hub
  link_prioritization:
    keywords:
      - spec
    shopping_path_keywords:
      - wishlist
      - compare-products
""",
        encoding="utf-8",
    )
    config_data = load_runtime_config_file(run_path)
    resolved = resolve_command_config(
        command="discover",
        cli_values={},
        config_data=config_data,
    )
    assert resolved["link_prioritization_shopping_keywords"] == [
        "wishlist",
        "compare-products",
    ]


def test_link_prioritization_shopping_keywords_must_be_str_list(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
discovery:
  sources:
    - https://example.com/hub
  link_prioritization:
    shopping_path_keywords:
      - 123
""",
        encoding="utf-8",
    )
    with pytest.raises(
        RuntimeConfigError, match="shopping_path_keywords"
    ):
        load_runtime_config_file(run_path)


def test_link_prioritization_rejects_unknown_inner_key(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
discovery:
  sources:
    - https://example.com/hub
  link_prioritization:
    power_range_kw: [200, 300]
""",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigError, match="link_prioritization"):
        load_runtime_config_file(run_path)


def test_reasoning_models_passthrough_and_resolve(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
reasoning_models:
  - halo-reason
  - my-thinker
schema: config/x/schema.json
extraction:
  skip_existing: true
""",
        encoding="utf-8",
    )
    config_data = load_runtime_config_file(run_path)
    resolved = resolve_command_config(
        command="extract",
        cli_values={},
        config_data=config_data,
    )
    assert resolved["reasoning_models"] == ["halo-reason", "my-thinker"]


def test_reasoning_models_must_be_str_list(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
reasoning_models:
  - 5
schema: config/x/schema.json
extraction:
  skip_existing: true
""",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigError, match="reasoning_models"):
        load_runtime_config_file(run_path)


# ── validation: section (strict, resolved at load time) ──

_EVIDENCE = {
    "type": "object",
    "required": ["value", "quote"],
    "properties": {"value": {"type": ["number", "null"]}, "quote": {"type": ["string", "null"]}},
}


def _validation_config(
    tmp_path: Path, validation, *, rows: bool = False, fmt: str = "csv", models=None
) -> Path:
    import json

    if rows:
        items = {f: {"type": ["string", "null"]} for f in ("feature", "value", "verbatim")}
        schema = {
            "$metadata": {
                "extraction": {"main_data_array": "rules", "context_objects": ["place"]},
                "identity": {"deduplication": {"key_fields": ["feature", "value"]}},
            },
            "properties": {
                "place": {"type": "object", "properties": {"state": {"type": "string"}}},
                "rules": {"type": "array", "items": {"type": "object", "properties": items}},
            },
        }
    else:
        schema = {
            "$metadata": {
                "extraction": {"main_data_array": "sites"},
                "identity": {"deduplication": {"key_fields": ["state", "site"]}},
            },
            "properties": {
                "sites": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "state": {"type": "string", "title": "State"},
                            "site": {"type": "string", "title": "Site"},
                            "x": {**_EVIDENCE, "title": "X"},
                        },
                    },
                }
            },
        }
    (tmp_path / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (tmp_path / "targets.csv").write_text("state,site,region\nCO,Alder,r1\n", encoding="utf-8")
    (tmp_path / "samples").mkdir(exist_ok=True)
    for name in ("s", "primary", "match", "x_quote", "bad name", "a", "site"):
        (tmp_path / "samples" / f"{name}.csv").write_text("State,Site\n", encoding="utf-8")
    run = {
        "domain": "probe",
        "models": models or {"primary": "m-p", "a": "m-a", "second": "m-b", "judge": "m-j"},
        "extraction": {"model": "primary"},
        "compilation": {"output": {"default_format": fmt}},
        "validation": validation,
    }
    if not rows:
        run["discovery"] = {
            "targets": "targets.csv",
            "queries": ["{site}"],
            "partition_by": ["state", "site"],
        }
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump(run), encoding="utf-8")
    return path



def _resolve_validate(path: Path) -> dict:
    """Load a config and resolve the validate stage (schema-dependent checks)."""
    return resolve_command_config(
        command="validate", cli_values={}, config_data=load_runtime_config_file(path)
    )

@pytest.mark.parametrize(
    ("validation", "message"),
    [
        ({}, "'validation' section must be a non-empty mapping"),
        ({"references": ["samples/s.csv"], "judg": "judge"}, "did you mean 'judge'"),
        ({"fields": ["x"]}, "validation needs models or references to compare"),
        ({"models": []}, "validation.models must be a non-empty list of names"),
        ({"models": ["a", "a"]}, "validation.models lists 'a' twice"),
        ({"models": ["a"], "min_accuracy": 0.9}, "min_accuracy needs validation.references"),
        ({"references": ["samples/s.csv"], "min_accuracy": 1.5}, r"must be in \(0, 1\]"),
        ({"references": ["samples/s.csv"], "min_accuracy": True}, r"must be in \(0, 1\]"),
        ({"models": ["secnd"]}, "'secnd' is not a models: alias — did you mean 'second'"),
        ({"models": ["a"], "judge": "jduge"}, "validation.judge: 'jduge' is not a models: alias"),
        ({"models": ["primary"]}, "extraction.model is always a contender"),
        ({"references": ["samples/none.csv"]}, "samples/none.csv is not a CSV file"),
        ({"references": ["targets.csv", "schema.json"]}, "schema.json is not a CSV file"),
        ({"models": ["a"], "key_fields": ["state"]}, "roster mode keys rows by roster identity"),
        ({"models": ["a"], "quote_field": "x"}, "roster mode reads quotes from the evidence"),
        ({"models": ["a"], "fields": ["site"]}, "'site' is a key field"),
        ({"models": ["a"], "fields": ["region"]}, "'region' is a targets passthrough column"),
        ({"models": ["a"], "fields": ["xx"]}, "'xx' is not a scorable field"),
    ],
)
def test_validation_section_rejects(tmp_path: Path, validation, message):
    with pytest.raises(RuntimeConfigError, match=message):
        _resolve_validate(_validation_config(tmp_path, validation))


def test_validation_models_must_differ(tmp_path: Path):
    models = {"primary": "m-p", "a": "m-a", "b": "m-a"}
    path = _validation_config(tmp_path, {"models": ["a", "b"]}, models=models)
    with pytest.raises(RuntimeConfigError, match="'a' and 'b' resolve to the same model"):
        load_runtime_config_file(path)


def test_validation_requires_csv_output(tmp_path: Path):
    path = _validation_config(tmp_path, {"models": ["a"]}, fmt="excel")
    load_runtime_config_file(path)  # schema checks wait for the validate stage
    with pytest.raises(RuntimeConfigError, match="validation reads the compiled CSV"):
        _resolve_validate(path)


@pytest.mark.parametrize(
    ("validation", "message"),
    [
        ({"models": ["a"]}, "rows mode needs validation.key_fields"),
        ({"models": ["a"], "key_fields": ["feture"]}, "did you mean 'feature'"),
        (
            {"models": ["a"], "key_fields": ["state", "feature"], "quote_field": "verbatm"},
            "'verbatm' is not an item field — did you mean 'verbatim'",
        ),
        (
            {"models": ["a"], "key_fields": ["state", "feature"], "quote_field": "feature"},
            "'feature' is also a key field",
        ),
        (
            {"models": ["a"], "key_fields": ["feature"], "quote_field": "verbatim"},
            "quote_field matching needs a scope",
        ),
        (
            {"models": ["a"], "key_fields": ["state", "feature"], "fields": ["state"]},
            "'state' is a key field",
        ),
    ],
)
def test_validation_rows_mode_rejects(tmp_path: Path, validation, message):
    with pytest.raises(RuntimeConfigError, match=message):
        _resolve_validate(_validation_config(tmp_path, validation, rows=True))


@pytest.mark.parametrize("command", ["validate", "extract"])
def test_schema_aware_validation_checks_run_for_stages_that_use_them(
    tmp_path: Path, command: str
):
    """``extract`` consumes validation.quote_field, so it gets the checks too."""
    cfg = load_runtime_config_file(
        _validation_config(
            tmp_path,
            {"models": ["a"], "key_fields": ["state", "feature"], "quote_field": "verbatm"},
            rows=True,
        )
    )
    with pytest.raises(RuntimeConfigError, match="'verbatm' is not an item field"):
        resolve_command_config(command=command, cli_values={}, config_data=cfg)


@pytest.mark.parametrize(
    "validation",
    [
        {"references": ["samples/primary.csv"]},
        {"references": ["samples/match.csv"]},
        {"references": ["samples/x_quote.csv"]},
        {"references": ["samples/bad name.csv"]},
        {"references": ["samples/site.csv"]},  # a key title, case-insensitively
        {"models": ["a"], "references": ["samples/a.csv"]},
    ],
)
def test_validation_labels_must_not_collide(tmp_path: Path, validation):
    with pytest.raises(RuntimeConfigError, match="collides with a cells.csv column"):
        _resolve_validate(_validation_config(tmp_path, validation))


def test_validation_section_resolves(tmp_path: Path):
    path = _validation_config(tmp_path, {"models": ["a"], "references": ["samples/s.csv"]})
    settings = resolve_command_config(
        command="validate", cli_values={}, config_data=load_runtime_config_file(path)
    )["validation"]
    assert settings == {
        "models": ["a"],
        "references": [(tmp_path / "samples/s.csv").as_posix()],
        "judge": None,
        "fields": [],
        "key_fields": [],
        "quote_field": None,
        "min_accuracy": None,
    }


@pytest.mark.parametrize(
    ("validation", "expected"),
    [
        ({"models": ["a"], "key_fields": ["state", "feature"], "quote_field": "verbatim"},
         "verbatim"),
        ({"models": ["a"], "key_fields": ["state", "feature"]}, None),
    ],
)
def test_rows_quote_field_reaches_extraction(tmp_path: Path, validation, expected):
    path = _validation_config(tmp_path, validation, rows=True)
    extract = resolve_command_config(
        command="extract", cli_values={}, config_data=load_runtime_config_file(path)
    )
    assert extract.get("quote_field") == expected
    if expected:
        assert extract["_config_sources"]["quote_field"] == "config.validation.quote_field"
