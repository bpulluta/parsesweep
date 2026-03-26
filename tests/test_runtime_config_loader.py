from pathlib import Path

import pytest

from streamline_extract.config.runtime_config_loader import (
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


def test_resolve_command_config_merges_cli_and_file_values(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
processing:
  input_dir: documents/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
  output_dir: processed/geothermal_ordinances
  max_context: 111111
""",
        encoding="utf-8",
    )
    config_data = load_runtime_config_file(config_path)

    resolved = resolve_command_config(
        command="process",
        cli_values={"max_context": 222222},
        config_data=config_data,
        strict=True,
    )

    assert resolved["path"] == "documents/geothermal_ordinances"
    assert resolved["schema"] == "schemas/personal/geothermal_ordinance_schema.json"
    assert resolved["output"] == "processed/geothermal_ordinances"
    assert resolved["max_context"] == 222222
    assert resolved["_config_sources"]["max_context"] == "cli"


def test_resolve_command_config_strict_unknown_section_key_raises(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
consolidation:
  input_dir: processed/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
  random_flag: true
""",
        encoding="utf-8",
    )
    config_data = load_runtime_config_file(config_path)

    with pytest.raises(RuntimeConfigError, match="Unknown keys in 'consolidation' section"):
        resolve_command_config(
            command="consolidate",
            cli_values={},
            config_data=config_data,
            strict=True,
        )


def test_catalog_for_command_contains_required_and_advanced_fields():
    entries = catalog_for_command("process")
    names = {entry["name"] for entry in entries}
    levels = {entry["name"]: entry["level"] for entry in entries}

    assert "input_dir" in names
    assert "schema" in names
    assert levels["input_dir"] == "required"
    assert levels["max_context"] == "advanced"


def test_load_runtime_config_file_accepts_valid_acquisition_section(tmp_path: Path):
        config_path = tmp_path / "run.yaml"
        config_path.write_text(
                """
acquisition:
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
""",
                encoding="utf-8",
        )

        loaded = load_runtime_config_file(config_path)
        assert loaded["acquisition"]["topology"]["mode"] == "hybrid"
        assert loaded["acquisition"]["runtime"]["max_concurrent_downloads"] == 3
        assert loaded["acquisition"]["runtime"]["min_request_interval_ms"] == 150


def test_load_runtime_config_file_rejects_invalid_acquisition_runtime_controls(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
acquisition:
  runtime:
    max_concurrent_downloads: 0
    min_request_interval_ms: -1
""",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigError, match="acquisition.runtime.max_concurrent_downloads"):
        load_runtime_config_file(config_path)


def test_load_runtime_config_file_rejects_invalid_acquisition_topology_mode(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
acquisition:
  topology:
    mode: unknown_mode
""",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigError, match="acquisition.topology.mode"):
        load_runtime_config_file(config_path)


def test_load_runtime_config_file_rejects_non_object_acquisition_section(tmp_path: Path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
acquisition:
  - not
  - an
  - object
""",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigError, match="'acquisition' section must be an object"):
        load_runtime_config_file(config_path)


def test_load_runtime_config_file_applies_split_file_processing_override(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
domain: geothermal_ordinances
processing:
  input_dir: documents/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
  output_dir: processed/from-run
""",
        encoding="utf-8",
    )

    processing_path = tmp_path / "processing.yaml"
    processing_path.write_text(
        """
output_dir: processed/from-override
max_context: 222222
""",
        encoding="utf-8",
    )

    loaded = load_runtime_config_file(run_path)

    assert loaded["processing"]["input_dir"] == "documents/geothermal_ordinances"
    assert loaded["processing"]["schema"] == "schemas/personal/geothermal_ordinance_schema.json"
    assert loaded["processing"]["output_dir"] == "processed/from-override"
    assert loaded["processing"]["max_context"] == 222222


def test_load_runtime_config_file_section_override_supports_nested_section_shape(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
consolidation:
  input_dir: processed/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
""",
        encoding="utf-8",
    )

    consolidation_path = tmp_path / "consolidation.yaml"
    consolidation_path.write_text(
        """
consolidation:
  output_dir: consolidated/geothermal_ordinances
""",
        encoding="utf-8",
    )

    loaded = load_runtime_config_file(run_path)

    assert loaded["consolidation"]["input_dir"] == "processed/geothermal_ordinances"
    assert loaded["consolidation"]["schema"] == "schemas/personal/geothermal_ordinance_schema.json"
    assert loaded["consolidation"]["output_dir"] == "consolidated/geothermal_ordinances"


def test_load_runtime_config_file_rejects_multiple_split_files_for_same_section(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
processing:
  input_dir: documents/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
""",
        encoding="utf-8",
    )

    (tmp_path / "processing.yaml").write_text("output_dir: one\n", encoding="utf-8")
    (tmp_path / "processing.json").write_text('{"output_dir": "two"}', encoding="utf-8")

    with pytest.raises(RuntimeConfigError, match="Multiple section override files found for 'processing'"):
        load_runtime_config_file(run_path)


def test_resolve_command_config_supports_acquire_alias(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
domain: geothermal_ordinances
acquisition:
  seeds:
    - https://example.org/a
  query: geothermal ordinance
  output:
    documents_dir: documents/geothermal_ordinances/acquired
    manifest_path: output/acquisition/geothermal_ordinances/manifest.json
""",
        encoding="utf-8",
    )

    config_data = load_runtime_config_file(run_path)
    resolved = resolve_command_config(
        command="acquire",
        cli_values={},
        config_data=config_data,
        strict=True,
    )

    assert resolved["seed_urls"] == ["https://example.org/a"]
    assert resolved["query"] == "geothermal ordinance"
    assert resolved["output_documents"] == "documents/geothermal_ordinances/acquired"
    assert resolved["output_manifest"] == "output/acquisition/geothermal_ordinances/manifest.json"
    assert resolved["domain"] == "geothermal_ordinances"


def test_resolve_command_config_sets_serpapi_flag_from_search_provider(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
acquisition:
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
        command="acquire",
        cli_values={},
        config_data=config_data,
        strict=True,
    )

    assert resolved["enable_serpapi"] is True


def test_resolve_command_config_maps_acquisition_topology_and_runtime_fields(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
acquisition:
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
                command="acquire",
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
acquisition:
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
                command="acquire",
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


def test_resolve_command_config_maps_digger_connector_and_retry_policy(tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
                """
acquisition:
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
                command="acquire",
                cli_values={},
                config_data=config_data,
                strict=True,
        )

        assert resolved["digger_provider"] == "crawlee_playwright"
        assert resolved["retry_max_attempts"] == 5
        assert resolved["retry_initial_backoff_seconds"] == 2
        assert resolved["retry_max_backoff_seconds"] == 16


def test_resolve_command_config_maps_targets_and_query_family_controls(tmp_path: Path):
    run_path = tmp_path / "run.yaml"
    run_path.write_text(
        """
acquisition:
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
        command="acquire",
        cli_values={},
        config_data=config_data,
        strict=True,
    )

    assert isinstance(resolved["targets"], list)
    assert len(resolved["targets"]) == 2
    assert resolved["query_families"]["generator_similar_power"][0].startswith("{manufacturer}")
    assert resolved["use_query_family"] == "generator_similar_power"
    assert resolved["seeker_max_results"] == 7
