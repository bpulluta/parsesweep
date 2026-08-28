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

    with pytest.raises(RuntimeConfigError, match="Unknown top-level config keys"):
        load_runtime_config_file(config_path)


def test_load_runtime_config_file_rejects_renamed_qaqc_section(tmp_path: Path):
    """A pre-rename 'qaqc:' section fails with a clear migration message."""
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        "domain: x\nqaqc:\n  models: [a, b]\n", encoding="utf-8"
    )

    with pytest.raises(
        RuntimeConfigError,
        match="'qaqc' config section was renamed to 'validation'",
    ):
        load_runtime_config_file(config_path)


def test_resolve_command_config_merges_cli_and_file_values(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
extraction:
  input_dir: documents/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
  output_dir: processed/geothermal_ordinances
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
        strict=True,
    )

    assert resolved["path"] == "documents/geothermal_ordinances"
    assert resolved["schema"] == "schemas/personal/geothermal_ordinance_schema.json"
    assert resolved["output"] == "processed/geothermal_ordinances"
    assert resolved["max_context"] == 222222
    assert resolved["timeout_seconds"] == 480
    assert resolved["_config_sources"]["max_context"] == "cli"


def test_resolve_command_config_strict_unknown_section_key_raises(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
compilation:
  input_dir: processed/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
  random_flag: true
""",
        encoding="utf-8",
    )
    config_data = load_runtime_config_file(config_path)

    with pytest.raises(RuntimeConfigError, match="Unknown keys in 'compilation' section"):
        resolve_command_config(
            command="compile",
            cli_values={},
            config_data=config_data,
            strict=True,
        )


def test_load_runtime_config_file_rejects_invalid_compilation_provenance_policy(
    tmp_path: Path,
):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
compilation:
  input_dir: processed/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
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
compilation:
  input_dir: processed/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
  provenance_policy: FAIL
""",
        encoding="utf-8",
    )
    config_data = load_runtime_config_file(config_path)

    resolved = resolve_command_config(
        command="compile",
        cli_values={},
        config_data=config_data,
        strict=True,
    )

    assert resolved["provenance_policy"] == "fail"


def test_catalog_for_command_contains_required_and_advanced_fields():
    entries = catalog_for_command("extract")
    names = {entry["name"] for entry in entries}
    levels = {entry["name"]: entry["level"] for entry in entries}

    assert "input_dir" in names
    assert "schema" in names
    assert levels["input_dir"] == "required"
    assert levels["max_context"] == "advanced"
    assert levels["timeout_seconds"] == "advanced"


@pytest.mark.parametrize("timeout_value", [0, -1, True, "600"])
def test_load_runtime_config_file_rejects_invalid_extraction_timeout(
    tmp_path: Path, timeout_value
):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        f"""
extraction:
  input_dir: documents/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
  timeout_seconds: {timeout_value!r}
""",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigError, match="extraction.timeout_seconds"):
        load_runtime_config_file(config_path)


def test_load_runtime_config_file_accepts_valid_discovery_section(tmp_path: Path):
        config_path = tmp_path / "run.yaml"
        config_path.write_text(
                """
discovery:
    seeds:
        - https://example.org/docs
    topology:
        mode: hybrid
    query_families:
        geothermal_generic:
            - "{jurisdiction} {state} geothermal ordinance pdf"
    runtime:
        max_pages: 50
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
        assert loaded["discovery"]["topology"]["mode"] == "hybrid"
        assert loaded["discovery"]["runtime"]["max_concurrent_downloads"] == 3
        assert loaded["discovery"]["runtime"]["min_request_interval_ms"] == 150
        assert loaded["discovery"]["policy"]["robots_mode"] == "warn"


@pytest.mark.parametrize(
    ("discovery_block", "match"),
    [
        (
            "  runtime:\n    max_concurrent_downloads: 0\n    min_request_interval_ms: -1\n",
            "discovery.runtime.max_concurrent_downloads",
        ),
        ("  policy:\n    robots_mode: maybe\n", "discovery.policy.robots_mode"),
        ("  topology:\n    mode: unknown_mode\n", "discovery.topology.mode"),
        ("  - not\n  - an\n  - object\n", "'discovery' section must be an object"),
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
        f"discovery:\n  browser_escalation: {str(flag).lower()}\n",
        encoding="utf-8",
    )

    loaded = load_runtime_config_file(config_path)
    assert loaded["discovery"]["browser_escalation"] is flag


def test_load_runtime_config_file_accepts_browser_escalation_mapping(
    tmp_path: Path,
):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        "discovery:\n"
        "  browser_escalation:\n"
        "    enabled: true\n"
        "    min_shell_chars: 8000\n"
        "    min_rendered_chars: 10000\n"
        "    settle_seconds: 20\n",
        encoding="utf-8",
    )

    loaded = load_runtime_config_file(config_path)
    assert loaded["discovery"]["browser_escalation"]["min_shell_chars"] == 8000


@pytest.mark.parametrize(
    ("escalation_block", "match"),
    [
        (
            "    enabled: true\n    bogus_key: 1\n",
            "unknown key",
        ),
        (
            "    min_shell_chars: -5\n",
            "min_shell_chars must be >= 0",
        ),
        (
            "    min_shell_chars: not-an-int\n",
            "min_shell_chars must be an integer",
        ),
    ],
)
def test_load_runtime_config_file_rejects_invalid_browser_escalation(
    tmp_path: Path, escalation_block: str, match: str
):
    """Inner-key validation now runs at load time (single source of truth)."""
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        f"discovery:\n  browser_escalation:\n{escalation_block}",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigError, match=match):
        load_runtime_config_file(config_path)


def test_load_runtime_config_file_applies_split_file_processing_override(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
domain: geothermal_ordinances
extraction:
  input_dir: documents/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
  output_dir: processed/from-run
""",
        encoding="utf-8",
    )

    extraction_path = tmp_path / "extraction.yaml"
    extraction_path.write_text(
        """
output_dir: processed/from-override
max_context: 222222
""",
        encoding="utf-8",
    )

    loaded = load_runtime_config_file(run_path)

    assert loaded["extraction"]["input_dir"] == "documents/geothermal_ordinances"
    assert loaded["extraction"]["schema"] == "schemas/personal/geothermal_ordinance_schema.json"
    assert loaded["extraction"]["output_dir"] == "processed/from-override"
    assert loaded["extraction"]["max_context"] == 222222


def test_load_runtime_config_file_section_override_supports_nested_section_shape(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
compilation:
  input_dir: processed/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
""",
        encoding="utf-8",
    )

    compilation_path = tmp_path / "compilation.yaml"
    compilation_path.write_text(
        """
compilation:
  output_dir: compiled/geothermal_ordinances
""",
        encoding="utf-8",
    )

    loaded = load_runtime_config_file(run_path)

    assert loaded["compilation"]["input_dir"] == "processed/geothermal_ordinances"
    assert loaded["compilation"]["schema"] == "schemas/personal/geothermal_ordinance_schema.json"
    assert loaded["compilation"]["output_dir"] == "compiled/geothermal_ordinances"


def test_load_runtime_config_file_rejects_multiple_split_files_for_same_section(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
extraction:
  input_dir: documents/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
""",
        encoding="utf-8",
    )

    (tmp_path / "extraction.yaml").write_text("output_dir: one\n", encoding="utf-8")
    (tmp_path / "extraction.json").write_text('{"output_dir": "two"}', encoding="utf-8")

    with pytest.raises(RuntimeConfigError, match="Multiple section override files found for 'extraction'"):
        load_runtime_config_file(run_path)


def test_resolve_command_config_supports_acquire_alias(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
domain: geothermal_ordinances
discovery:
  seeds:
    - https://example.org/a
  query: geothermal ordinance
  output:
    documents_dir: documents/geothermal_ordinances/acquired
    manifest_path: output/discovery/geothermal_ordinances/manifest.json
""",
        encoding="utf-8",
    )

    config_data = load_runtime_config_file(run_path)
    resolved = resolve_command_config(
        command="discover",
        cli_values={},
        config_data=config_data,
        strict=True,
    )

    assert resolved["seed_urls"] == ["https://example.org/a"]
    assert resolved["query"] == "geothermal ordinance"
    assert resolved["output_documents"] == "documents/geothermal_ordinances/acquired"
    assert resolved["output_manifest"] == "output/discovery/geothermal_ordinances/manifest.json"
    assert resolved["domain"] == "geothermal_ordinances"


def test_resolve_command_config_sets_serpapi_flag_from_search_provider(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
discovery:
  seeds:
    - https://example.org/a
  search:
    provider: serpapi
    enabled: true
""",
        encoding="utf-8",
    )

    config_data = load_runtime_config_file(run_path)
    resolved = resolve_command_config(
        command="discover",
        cli_values={},
        config_data=config_data,
        strict=True,
    )

    assert resolved["enable_serpapi"] is True


def test_resolve_command_config_maps_discovery_topology_and_runtime_fields(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
discovery:
    seeds:
        - https://county.gov/hub
    topology:
        mode: distributed
    digger:
        allowed_domains:
            - county.gov
        discovery_rules:
            include_url_patterns:
                - \\.pdf$
            include_link_text_patterns:
                - ordinance
    runtime:
        max_depth: 4
        max_pages: 25
        max_files: 8
        timeout_seconds: 12
        max_concurrent_downloads: 4
        min_request_interval_ms: 120
""",
                encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        resolved = resolve_command_config(
                command="discover",
                cli_values={},
                config_data=config_data,
                strict=True,
        )

        assert resolved["topology_mode"] == "distributed"
        assert resolved["allowed_domains"] == ["county.gov"]
        assert resolved["include_url_patterns"] == [r"\.pdf$"]
        assert resolved["include_link_text_patterns"] == ["ordinance"]
        assert resolved["max_depth"] == 4
        assert resolved["max_pages"] == 25
        assert resolved["max_files"] == 8
        assert resolved["timeout_seconds"] == 12
        assert resolved["max_concurrent_downloads"] == 4
        assert resolved["min_request_interval_ms"] == 120


def test_resolve_command_config_maps_centralized_hub_sweep_fields(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
discovery:
    hub_pages:
        - https://docs.county.gov/index.html
    topology:
        mode: centralized
    routing:
        index_links:
            - url: https://docs.county.gov/geothermal-ordinance.pdf
              text: Geothermal Ordinance
    digger:
        index_page_mode:
            enabled: true
            collect_all_matching_links: true
        discovery_rules:
            include_url_patterns:
                - \\.pdf$
""",
                encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        resolved = resolve_command_config(
                command="discover",
                cli_values={},
                config_data=config_data,
                strict=True,
        )

        assert resolved["topology_mode"] == "centralized"
        assert resolved["hub_pages"] == ["https://docs.county.gov/index.html"]
        assert resolved["index_page_mode"] == {
                "enabled": True,
                "collect_all_matching_links": True,
        }
        assert resolved["index_links"] == [
                {
                        "url": "https://docs.county.gov/geothermal-ordinance.pdf",
                        "text": "Geothermal Ordinance",
                }
        ]


def test_resolve_command_config_maps_request_headers(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
discovery:
    request_headers:
            User-Agent: "ParseSweep/2.0 (custom contact: example@example.com)"
            Accept-Language: "en-US,en;q=0.9"
""",
                encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        resolved = resolve_command_config(
                command="discover",
                cli_values={},
                config_data=config_data,
                strict=True,
        )

        assert resolved["request_headers"] == {
                "User-Agent": "ParseSweep/2.0 (custom contact: example@example.com)",
                "Accept-Language": "en-US,en;q=0.9",
        }


def test_resolve_command_config_maps_digger_connector_and_retry_policy(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
discovery:
    seeds:
        - https://example.org/hub
    digger:
        connector: crawlee_playwright
    retry_policy:
        max_attempts: 5
        initial_backoff_seconds: 2
        max_backoff_seconds: 16
""",
                encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        resolved = resolve_command_config(
                command="discover",
                cli_values={},
                config_data=config_data,
                strict=True,
        )

        assert resolved["digger_provider"] == "crawlee_playwright"
        assert resolved["retry_max_attempts"] == 5
        assert resolved["retry_initial_backoff_seconds"] == 2


def test_resolve_command_config_maps_digger_provider_alias(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
discovery:
    seeds:
        - https://example.org/hub
    digger:
        provider: http
""",
                encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        resolved = resolve_command_config(
                command="discover",
                cli_values={},
                config_data=config_data,
                strict=True,
        )

        assert resolved["digger_provider"] == "http"
        assert resolved["_config_sources"]["digger_provider"] == "config.discovery.digger.provider"


def test_resolve_command_config_maps_link_prioritization_block(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
discovery:
    seeds:
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
                strict=True,
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
    seeds:
        - https://example.org/hub
    selection:
        relevance_require_any_terms:
            - ordinance
            - municipal code
        relevance_require_legal_marker_terms:
            - title
            - chapter
        relevance_exclude_any_terms:
            - state brief
            - specific plan
        relevance_allowed_domain_patterns:
            - county.gov
            - ecode360.com
        require_supported_document: true
""",
                encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        resolved = resolve_command_config(
                command="discover",
                cli_values={},
                config_data=config_data,
                strict=True,
        )

        assert resolved["selection_relevance_require_any_terms"] == ["ordinance", "municipal code"]
        assert resolved["selection_relevance_require_legal_marker_terms"] == ["title", "chapter"]
        assert resolved["selection_relevance_exclude_any_terms"] == ["state brief", "specific plan"]
        assert resolved["selection_relevance_allowed_domain_patterns"] == ["county.gov", "ecode360.com"]
        assert resolved["selection_require_supported_document"] is True


def test_resolve_command_config_maps_split_selection_exclusions(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
discovery:
  seeds:
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
                strict=True,
        )

        assert resolved["selection_max_per_host_per_target"] == 2
        assert resolved["selection_exclude_url_patterns"] == ["archive"]
        assert resolved["selection_exclude_text_patterns"] == ["preliminary"]


def test_resolve_command_config_maps_generic_selection_templates(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
discovery:
    seeds:
        - https://example.org/hub
    selection:
        primary_per_target: 3
        exclude_draft: false
        draft_patterns:
            - draft
            - proposed
        target_identity_require_any_templates:
            - "{jurisdiction}"
            - "{manufacturer}"
        target_identity_require_all_templates:
            - "{state}"
        target_identity_exclude_any_templates:
            - "sample"
""",
                encoding="utf-8",
        )

        config_data = load_runtime_config_file(run_path)
        resolved = resolve_command_config(
                command="discover",
                cli_values={},
                config_data=config_data,
                strict=True,
        )

        assert resolved["selection_primary_per_target"] == 3
        assert resolved["selection_exclude_draft"] is False
        assert resolved["selection_draft_patterns"] == ["draft", "proposed"]
        assert resolved["selection_target_identity_require_any_templates"] == ["{jurisdiction}", "{manufacturer}"]
        assert resolved["selection_target_identity_require_all_templates"] == ["{state}"]
        assert resolved["selection_target_identity_exclude_any_templates"] == ["sample"]


def test_discovery_selection_primary_per_target_must_cover_review_keep_top(
        tmp_path: Path,
):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
            """
discovery:
  seeds:
        - https://example.org/hub
  selection:
        primary_per_target: 2
  document_review:
        keep_top: 3
""",
            encoding="utf-8",
        )

        with pytest.raises(
            RuntimeConfigError,
            match="selection.primary_per_target' must be >=",
        ):
            load_runtime_config_file(run_path)


def test_discovery_selection_and_review_accept_equal_limits(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
            """
discovery:
  seeds:
        - https://example.org/hub
  selection:
        primary_per_target: 3
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
            strict=True,
        )
        assert resolved["selection_primary_per_target"] == 3
        assert resolved["document_review"]["keep_top"] == 3


def test_resolve_command_config_maps_discovery_policy_fields(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
discovery:
    seeds:
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
                strict=True,
        )

        assert resolved["robots_policy_mode"] == "enforce"
        assert resolved["tos_policy_mode"] == "warn"
        assert resolved["acknowledged_tos_domains"] == ["example.org", "county.gov"]


def test_resolve_command_config_maps_targets_and_query_family_controls(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
discovery:
  targets:
    - manufacturer: Generac
      power_class_kw: 200-300
    - manufacturer: John Deere
      power_class_kw: 200-300
  query_families:
    generator_similar_power:
      - "{manufacturer} {power_class_kw} kW generator spec pdf"
  seeker:
    provider: serpapi
    use_query_family: generator_similar_power
    max_results: 7
""",
        encoding="utf-8",
    )

    config_data = load_runtime_config_file(run_path)
    resolved = resolve_command_config(
        command="discover",
        cli_values={},
        config_data=config_data,
        strict=True,
    )

    assert isinstance(resolved["targets"], list)
    assert len(resolved["targets"]) == 2
    assert resolved["query_families"]["generator_similar_power"][0].startswith("{manufacturer}")
    assert resolved["use_query_family"] == "generator_similar_power"
    assert resolved["seeker_max_results"] == 7


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
extraction:
  input_dir: documents/demo
  schema: schemas/personal/x.json
compilation:
  input_dir: processed/demo
  schema: schemas/personal/x.json
""",
        encoding="utf-8",
    )
    data = load_runtime_config_file(config_path)
    assert data["models"] == {"fast": "gpt-4o-mini", "accurate": "gpt-5"}
    for command in ("discover", "extract", "compile"):
        merged = resolve_command_config(
            command=command, cli_values={}, config_data=data, strict=True
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
        "extraction:\n  schema: s.json\n  input_dir: d\n  " + snippet,
    )
    with pytest.raises(RuntimeConfigError, match=match):
        load_runtime_config_file(cfg)


def test_processing_validator_accepts_valid_block(tmp_path: Path):
    cfg = _write(
        tmp_path,
        "extraction:\n  schema: s.json\n  input_dir: d\n  max_context: 600000\n"
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
        "extraction:\n  schema: s.json\n  input_dir: d\n" + pages_block,
    )
    loaded = load_runtime_config_file(cfg)
    ext = loaded["extraction"]

    if expected_csv is None:
        assert "pages_csv" not in ext
    else:
        assert ext["pages_csv"] == expected_csv

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
        "extraction:\n  schema: s.json\n  input_dir: d\n"
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
        "extraction:\n  schema: s.json\n  input_dir: d\n"
        "  pages:\n    auto_locate:\n"
        "      section_description: the rate tables\n"
        "      save_discovered: yesplease\n",
    )
    with pytest.raises(
        RuntimeConfigError, match="save_discovered must be a boolean"
    ):
        load_runtime_config_file(cfg)


def test_old_flat_pages_keys_rejected(tmp_path: Path):
    """Old pages_csv / page_targeting keys are no longer accepted."""
    cfg = _write(
        tmp_path,
        "extraction:\n  schema: s.json\n  input_dir: d\n"
        "  pages_csv: old.csv\n"
        "  page_targeting:\n    enabled: true\n"
        "    section_description: old style\n",
    )
    with pytest.raises(RuntimeConfigError, match="no longer supported"):
        load_runtime_config_file(cfg)


def test_model_context_windows_validates_and_passes_through(tmp_path: Path):
    cfg = _write(
        tmp_path,
        "model_context_windows:\n  my-deploy: 300000\n"
        "extraction:\n  schema: s.json\n  input_dir: d\n",
    )
    config_data = load_runtime_config_file(cfg)
    resolved = resolve_command_config(
        command="extract", cli_values={}, config_data=config_data, strict=True
    )
    assert resolved["model_context_windows"] == {"my-deploy": 300000}


def test_model_context_windows_rejects_non_positive(tmp_path: Path):
    cfg = _write(
        tmp_path,
        "model_context_windows:\n  my-deploy: 0\n",
    )
    with pytest.raises(RuntimeConfigError, match="positive integer"):
        load_runtime_config_file(cfg)


# ── compilation.synthesis validation ──────────────────────────────
# The synthesis block was previously an unchecked passthrough, so a mistyped
# key (e.g. group_bye) silently produced empty output. These lock the guard.

_VALID_SYNTHESIS = (
    "compilation:\n"
    "  input_dir: d\n"
    "  schema: s.json\n"
    "  synthesis:\n"
    "    enabled: true\n"
    "    min_sources_for_llm: 2\n"
    "    group_by: [entity.name]\n"
    "    identity_fields: [entity.city]\n"
    "    relevance_flag: applicability.on_topic\n"
    "    citation_field: provenance.url\n"
    "    reconcile_fields:\n"
    "      - {field: start, evidence: ev_start}\n"
    "      - {field: end}\n"
    "    ordering_constraint: [start, end]\n"
    "    narrative_fields: [status]\n"
)


def test_synthesis_block_accepts_valid_config(tmp_path: Path):
    config_data = load_runtime_config_file(_write(tmp_path, _VALID_SYNTHESIS))
    resolved = resolve_command_config(
        command="compile",
        cli_values={},
        config_data=config_data,
        strict=True,
    )
    assert resolved["synthesis"]["group_by"] == ["entity.name"]


def test_synthesis_accepts_branching_ordering_and_exclusive_pairs(tmp_path: Path):
    body = (
        "compilation:\n  input_dir: d\n  schema: s.json\n"
        "  synthesis:\n    group_by: [x]\n"
        "    reconcile_fields:\n"
        "      - {field: a}\n"
        "      - {field: b}\n"
        "      - {field: c}\n"
        "    ordering_constraints: [[a, b], [b, c]]\n"
        "    ordering_exclusive_pairs: [[b, c]]\n"
        "    ordering_infer_precision_from_pins: false\n"
    )
    config_data = load_runtime_config_file(_write(tmp_path, body))
    resolved = resolve_command_config(
        command="compile",
        cli_values={},
        config_data=config_data,
        strict=True,
    )
    assert resolved["synthesis"]["ordering_constraints"] == [["a", "b"], ["b", "c"]]


def test_synthesis_accepts_item_group_by(tmp_path: Path):
    """item_group_by is an optional list of item-level paths that splits each
    entity into one row per sub-entity."""
    body = (
        "compilation:\n  input_dir: d\n  schema: s.json\n"
        "  synthesis:\n    enabled: true\n    group_by: [entity.name]\n"
        "    item_group_by: [phase_label]\n"
    )
    config_data = load_runtime_config_file(_write(tmp_path, body))
    resolved = resolve_command_config(
        command="compile",
        cli_values={},
        config_data=config_data,
        strict=True,
    )
    assert resolved["synthesis"]["item_group_by"] == ["phase_label"]


@pytest.mark.parametrize(
    ("case_id", "body", "match"),
    [
        (
            "item_group_by_must_be_string_list",
            "compilation:\n  input_dir: d\n  schema: s.json\n"
            "  synthesis:\n    group_by: [x]\n    item_group_by: [\"\"]\n",
            "item_group_by must be a list of non-empty strings",
        ),
        (
            "item_group_by_may_not_shadow_group_by_column",
            "compilation:\n  input_dir: d\n  schema: s.json\n"
            "  synthesis:\n    group_by: [entity.name]\n"
            "    item_group_by: [phase.name]\n",
            "collide with group_by output column\\(s\\): name",
        ),
        (
            "unknown_key",
            "compilation:\n  input_dir: d\n  schema: s.json\n"
            "  synthesis:\n    enabled: true\n    group_by: [x]\n    group_bye: [x]\n",
            "Unknown keys in 'compilation.synthesis'",
        ),
        (
            "enabled_requires_group_by",
            "compilation:\n  input_dir: d\n  schema: s.json\n"
            "  synthesis:\n    enabled: true\n",
            "group_by is required",
        ),
        (
            "reconcile_fields_require_field_key",
            "compilation:\n  input_dir: d\n  schema: s.json\n"
            "  synthesis:\n    group_by: [x]\n"
            "    reconcile_fields:\n      - {evidence: ev}\n",
            "non-empty string 'field'",
        ),
        (
            "ordering_must_reference_reconciled_fields",
            "compilation:\n  input_dir: d\n  schema: s.json\n"
            "  synthesis:\n    group_by: [x]\n"
            "    reconcile_fields:\n      - {field: start}\n"
            "    ordering_constraint: [start, missing]\n",
            "not in reconcile_fields: missing",
        ),
        (
            "min_sources_must_be_non_negative_int",
            "compilation:\n  input_dir: d\n  schema: s.json\n"
            "  synthesis:\n    group_by: [x]\n    min_sources_for_llm: -1\n",
            "min_sources_for_llm",
        ),
        (
            "invalid_ordering_exclusive_pairs_shape",
            "compilation:\n  input_dir: d\n  schema: s.json\n"
            "  synthesis:\n    group_by: [x]\n"
            "    reconcile_fields:\n      - {field: a}\n      - {field: b}\n"
            "    ordering_exclusive_pairs: [[a, b, c]]\n",
            "ordering_exclusive_pairs must be a list of 2-item string lists",
        ),
        (
            "unknown_fields_in_ordering_constraints",
            "compilation:\n  input_dir: d\n  schema: s.json\n"
            "  synthesis:\n    group_by: [x]\n"
            "    reconcile_fields:\n      - {field: start}\n"
            "    ordering_constraints: [[start, missing]]\n",
            "ordering_constraints references field\\(s\\) not in reconcile_fields: missing",
        ),
    ],
)
def test_synthesis_block_rejects_invalid_config(
    tmp_path: Path, case_id: str, body: str, match: str
):
    """compilation.synthesis validation rejects malformed blocks loudly.

    Previously the block was an unchecked passthrough, so typos silently
    produced empty output. Each case locks one guard.
    """
    with pytest.raises(RuntimeConfigError, match=match):
        load_runtime_config_file(_write(tmp_path, body))


def _resolve_discovery(body: str) -> dict:
    from psweep.config.runtime_config_loader import resolve_command_config

    return resolve_command_config(
        command="discover", cli_values={}, config_data=yaml.safe_load(body)
    )


@pytest.mark.parametrize(
    ("block", "match"),
    [
        (
            "  seeker:\n    provider: serpapi\n    max_result: 7\n",
            "Unknown keys in 'discovery.seeker': max_result",
        ),
        (
            "  search:\n    provider: serpapi\n    maxresults: 5\n",
            "Unknown keys in 'discovery.search': maxresults",
        ),
        (
            "  retry_policy:\n    max_attempt: 3\n",
            "Unknown keys in 'discovery.retry_policy': max_attempt",
        ),
        (
            "  document_review:\n    keep_topp: 1\n",
            "Unknown keys in 'discovery.document_review': keep_topp",
        ),
    ],
)
def test_load_runtime_config_file_rejects_unknown_subblock_key(
    tmp_path: Path, block: str, match: str
):
    """A typo inside a consumed discovery sub-block fails loudly (not no-op)."""
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        "discovery:\n  seeds:\n    - https://example.org/a\n" + block,
        encoding="utf-8",
    )
    with pytest.raises(RuntimeConfigError, match=match):
        load_runtime_config_file(config_path)


def test_load_runtime_config_file_accepts_known_subblock_keys(tmp_path: Path):
    """serpapi_params stays an unrestricted passthrough (shallow check only)."""
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        "discovery:\n"
        "  seeds:\n    - https://example.org/a\n"
        "  search:\n"
        "    provider: serpapi\n"
        "    max_results: 5\n"
        "    serpapi_params:\n"
        "      tbm: nws\n"
        "      any_verbatim_key: ok\n",
        encoding="utf-8",
    )
    loaded = load_runtime_config_file(config_path)
    assert loaded["discovery"]["search"]["max_results"] == 5


def test_browser_escalation_resolves_to_discovery_inputs():
    """discovery.browser_escalation reaches the engine request payload."""
    resolved = _resolve_discovery(
        "discovery:\n  query: q\n  browser_escalation:\n"
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
            "discovery:\n  query: q\n  browser_escalation:\n    min_shel_chars: 1500\n",
            "unknown key",
        ),
        (
            "discovery:\n  query: q\n  browser_escalation:\n    min_shell_chars: abc\n",
            "must be an integer",
        ),
        (
            "discovery:\n  query: q\n  browser_escalation:\n    settle_seconds: -1\n",
            "must be >= 0",
        ),
        (
            "discovery:\n  query: q\n  browser_escalation: [1]\n",
            "must be a mapping or bool",
        ),
    ],
)
def test_browser_escalation_rejects_invalid_config(body: str, match: str):
    """Typos/invalid values fail loudly instead of silently no-op'ing."""
    with pytest.raises(RuntimeConfigError, match=match):
        _resolve_discovery(body)
