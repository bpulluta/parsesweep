"""Tests for the Phase 1 runtime artifact compiler slice."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from streamline_extract.core import (
    ArtifactCompilerError,
    build_runtime_readiness_report,
    compile_runtime_artifact,
    resolve_pack_ref_for_schema,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_file(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_compile_runtime_artifact_is_deterministic(tmp_path) -> None:
    packs_dir = tmp_path / "schemas/domain_packs"
    profiles_dir = tmp_path / "schemas/profiles"

    _write_file(
        packs_dir / "tariffs/pack.yaml",
        "name: tariffs\nversion: 1.0.0\nmodules:\n  - classifier\n",
    )
    _write_file(
        profiles_dir / "default.profile.json",
        '{"profile_id": "default", "llm": {"model": "gpt-5"}}',
    )

    fixed_time = datetime(2026, 3, 24, 12, 0, tzinfo=timezone.utc)

    artifact1 = compile_runtime_artifact(
        "tariffs",
        "default",
        repo_root=REPO_ROOT,
        domain_packs_dir=packs_dir,
        profiles_dir=profiles_dir,
        compiled_at=fixed_time,
    )
    artifact2 = compile_runtime_artifact(
        "tariffs",
        "default",
        repo_root=REPO_ROOT,
        domain_packs_dir=packs_dir,
        profiles_dir=profiles_dir,
        compiled_at=fixed_time,
    )

    assert artifact1["artifact_id"] == artifact2["artifact_id"]
    assert artifact1["artifact_id"].startswith("artifact://runtime/")


def test_compile_runtime_artifact_missing_pack_raises(tmp_path) -> None:
    packs_dir = tmp_path / "schemas/domain_packs"
    profiles_dir = tmp_path / "schemas/profiles"
    _write_file(profiles_dir / "default.profile.json", '{"profile_id": "default"}')

    with pytest.raises(ArtifactCompilerError, match="Could not resolve pack"):
        compile_runtime_artifact(
            "missing-pack",
            "default",
            repo_root=REPO_ROOT,
            domain_packs_dir=packs_dir,
            profiles_dir=profiles_dir,
        )


def test_compile_runtime_artifact_missing_profile_raises(tmp_path) -> None:
    packs_dir = tmp_path / "schemas/domain_packs"
    profiles_dir = tmp_path / "schemas/profiles"
    _write_file(packs_dir / "tariffs/pack.yaml", "name: tariffs\nversion: 1.0.0\n")

    with pytest.raises(ArtifactCompilerError, match="Could not resolve profile"):
        compile_runtime_artifact(
            "tariffs",
            "missing-profile",
            repo_root=REPO_ROOT,
            domain_packs_dir=packs_dir,
            profiles_dir=profiles_dir,
        )


def test_compile_runtime_artifact_emits_lineage_fields(tmp_path) -> None:
    packs_dir = tmp_path / "schemas/domain_packs"
    profiles_dir = tmp_path / "schemas/profiles"

    _write_file(
        packs_dir / "geothermal/pack.yaml",
        "name: geothermal\nversion: 2.1.0\n",
    )
    _write_file(
        profiles_dir / "prod.profile.json",
        '{"profile_id": "prod", "runtime": {"strict": true}}',
    )

    artifact = compile_runtime_artifact(
        "geothermal",
        "prod",
        repo_root=REPO_ROOT,
        domain_packs_dir=packs_dir,
        profiles_dir=profiles_dir,
    )

    assert artifact["lineage"]["artifact_id"] == artifact["artifact_id"]
    assert artifact["lineage"]["profile_id"] == "prod"
    assert artifact["lineage"]["pack_name"] == "geothermal"
    assert artifact["lineage"]["pack_version"] == "2.1.0"
    assert artifact["contract_versions"]["extraction_record"] == "1.0.0"
    assert artifact["contract_versions"]["modules_catalog"] == "1.0.0"


def test_compile_runtime_artifact_uses_repo_assets() -> None:
    artifact = compile_runtime_artifact(
        "tariffs",
        "default",
        repo_root=REPO_ROOT,
    )

    assert artifact["lineage"]["profile_id"] == "default"
    assert artifact["lineage"]["pack_name"] == "tariffs"
    assert artifact["source"]["pack_path"].endswith("schemas/domain_packs/tariffs/pack.yaml")
    assert artifact["source"]["profile_path"].endswith("schemas/profiles/default.profile.json")
    assert artifact["resolved"]["pack"]["schema_path"] == "schemas/personal/electricity_tariff_schema.json"
    assert artifact["resolved"]["pack"]["qaqc"]["default_lane"] == "quantitative"


def test_compile_runtime_artifact_uses_repo_profile_tiering() -> None:
    artifact = compile_runtime_artifact(
        "aq_permits",
        "prod",
        repo_root=REPO_ROOT,
    )

    assert artifact["lineage"]["profile_id"] == "prod"
    assert artifact["resolved"]["profile"]["environment"] == "prod"
    assert artifact["resolved"]["pack"]["schema_path"] == "schemas/personal/air_quality_permits_schema.json"


def test_compile_runtime_artifact_exposes_repo_qaqc_lanes() -> None:
    artifact = compile_runtime_artifact(
        "geothermal_ordinances",
        "default",
        repo_root=REPO_ROOT,
    )

    assert artifact["resolved"]["pack"]["schema_path"] == "schemas/personal/geothermal_ordinance_schema.json"
    assert artifact["resolved"]["pack"]["qaqc"]["default_lane"] == "quantitative"
    assert artifact["resolved"]["pack"]["qaqc"]["lanes"]["quantitative"]["enabled"] is True
    assert artifact["resolved"]["pack"]["qaqc"]["lanes"]["qualitative"]["enabled"] is False


def test_compile_runtime_artifact_exposes_repo_qaqc_lanes_for_aq_permits() -> None:
    artifact = compile_runtime_artifact(
        "aq_permits",
        "default",
        repo_root=REPO_ROOT,
    )

    assert artifact["resolved"]["pack"]["qaqc"]["default_lane"] == "quantitative"
    assert artifact["resolved"]["pack"]["qaqc"]["lanes"]["quantitative"]["enabled"] is True
    assert artifact["resolved"]["pack"]["qaqc"]["lanes"]["quantitative"]["record_matching"]["key_fields"] == [
        "referenceNumber",
        "make",
        "model",
    ]


def test_compile_runtime_artifact_exposes_tariff_qaqc_projection() -> None:
    artifact = compile_runtime_artifact(
        "tariffs",
        "default",
        repo_root=REPO_ROOT,
    )

    projection = artifact["resolved"]["pack"]["qaqc"]["lanes"]["quantitative"]["projection"]
    assert projection["type"] == "nested_array_items"
    assert projection["source_array"] == "rate_schedules"
    assert projection["nested_array"] == "charges"
    assert projection["parent_fields"] == ["rate_name", "is_rider", "sector"]


def test_resolve_pack_ref_for_schema_uses_primary_and_alias_paths() -> None:
    current = resolve_pack_ref_for_schema(
        REPO_ROOT / "schemas/geothermal_ordinance_schema_v3.json",
        repo_root=REPO_ROOT,
    )
    legacy = resolve_pack_ref_for_schema(
        REPO_ROOT / "schemas/geothermal_ordinance_schema.json",
        repo_root=REPO_ROOT,
    )
    production = resolve_pack_ref_for_schema(
        REPO_ROOT / "schemas/personal/geothermal_ordinance_schema.json",
        repo_root=REPO_ROOT,
    )

    assert current == "geothermal_ordinances"
    assert legacy == "geothermal_ordinances"
    assert production == "geothermal_ordinances"


def test_build_runtime_readiness_report_uses_schema_resolution() -> None:
    report = build_runtime_readiness_report(
        schema_path=REPO_ROOT / "schemas/personal/electricity_tariff_schema.json",
        profile_ref="prod",
        repo_root=REPO_ROOT,
    )

    assert report["status"] == "ready"
    assert report["resolved"]["pack_name"] == "tariffs"
    assert report["resolved"]["profile_name"] == "prod"
    assert report["resolved"]["default_qaqc_lane"] == "quantitative"
    assert report["resolved"]["schema_file_path"].endswith(
        "schemas/personal/electricity_tariff_schema.json"
    )
    assert report["resolved"]["main_data_array"] == "rate_schedules"
    assert report["resolved"]["identifier_fields"] == [
        "utility_info.utility_name",
        "utility_info.state",
    ]
    assert "value_semantics_classifier" in report["resolved"]["enabled_module_ids"]
    assert {check["name"] for check in report["checks"]} >= {
        "schema_path",
        "schema_file",
        "schema_metadata",
        "main_data_array",
        "identifier_fields",
        "context_object_paths",
        "identifier_field_paths",
        "consolidation_paths",
        "qaqc_lane_paths",
        "modules",
        "profile_runtime",
        "contract_versions",
    }


def test_build_runtime_readiness_report_requires_runtime_sections(tmp_path) -> None:
    packs_dir = tmp_path / "schemas/domain_packs"
    profiles_dir = tmp_path / "schemas/profiles"

    _write_file(
        packs_dir / "broken/pack.yaml",
        (
            "name: broken\n"
            "version: 1.0.0\n"
            "schema_path: schemas/personal/electricity_tariff_schema.json\n"
            "modules: []\n"
        ),
    )
    _write_file(
        profiles_dir / "default.profile.json",
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    with pytest.raises(ArtifactCompilerError, match="non-empty 'modules' list"):
        build_runtime_readiness_report(
            pack_ref="broken",
            profile_ref="default",
            repo_root=REPO_ROOT,
            domain_packs_dir=packs_dir,
            profiles_dir=profiles_dir,
        )


def test_build_runtime_readiness_report_requires_existing_schema_file(tmp_path) -> None:
    packs_dir = tmp_path / "schemas/domain_packs"
    profiles_dir = tmp_path / "schemas/profiles"

    _write_file(
        packs_dir / "broken/pack.yaml",
        (
            "name: broken\n"
            "version: 1.0.0\n"
            f"schema_path: {(tmp_path / 'schemas/personal/missing.json').as_posix()}\n"
            "modules:\n"
            "  - module_id: mapper\n"
            "    enabled: true\n"
        ),
    )
    _write_file(
        profiles_dir / "default.profile.json",
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    with pytest.raises(ArtifactCompilerError, match="must resolve to an existing schema file"):
        build_runtime_readiness_report(
            pack_ref="broken",
            profile_ref="default",
            repo_root=REPO_ROOT,
            domain_packs_dir=packs_dir,
            profiles_dir=profiles_dir,
        )


def test_build_runtime_readiness_report_requires_schema_metadata_contract(tmp_path) -> None:
    packs_dir = tmp_path / "schemas/domain_packs"
    profiles_dir = tmp_path / "schemas/profiles"
    schema_path = tmp_path / "schemas/personal/broken.json"

    _write_file(
        schema_path,
        '{"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}}}}',
    )
    _write_file(
        packs_dir / "broken/pack.yaml",
        (
            "name: broken\n"
            "version: 1.0.0\n"
            f"schema_path: {schema_path.as_posix()}\n"
            "modules:\n"
            "  - module_id: mapper\n"
            "    enabled: true\n"
        ),
    )
    _write_file(
        profiles_dir / "default.profile.json",
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    with pytest.raises(ArtifactCompilerError, match="runtime metadata validation"):
        build_runtime_readiness_report(
            pack_ref="broken",
            profile_ref="default",
            repo_root=REPO_ROOT,
            domain_packs_dir=packs_dir,
            profiles_dir=profiles_dir,
        )


def test_build_runtime_readiness_report_requires_valid_identifier_paths(tmp_path) -> None:
    packs_dir = tmp_path / "schemas/domain_packs"
    profiles_dir = tmp_path / "schemas/profiles"
    schema_path = tmp_path / "schemas/personal/broken.json"

    _write_file(
        schema_path,
        (
            '{'
            '"$metadata": {'
            '  "extraction": {'
            '    "main_data_array": "items",'
            '    "context_objects": ["metadata"],'
            '    "identifier_fields": ["metadata.missing"]'
            '  },'
            '  "consolidation": {"deduplication": {"key_fields": ["name"], "ignore_fields": []}}'
            '},'
            '"type": "object",'
            '"properties": {'
            '  "metadata": {"type": "object", "properties": {"id": {"type": "string"}}},'
            '  "items": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}}'
            '}'
            '}'
        ),
    )
    _write_file(
        packs_dir / "broken/pack.yaml",
        (
            "name: broken\n"
            "version: 1.0.0\n"
            f"schema_path: {schema_path.as_posix()}\n"
            "modules:\n"
            "  - module_id: mapper\n"
            "    enabled: true\n"
        ),
    )
    _write_file(
        profiles_dir / "default.profile.json",
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    with pytest.raises(ArtifactCompilerError, match="Schema path could not be resolved"):
        build_runtime_readiness_report(
            pack_ref="broken",
            profile_ref="default",
            repo_root=REPO_ROOT,
            domain_packs_dir=packs_dir,
            profiles_dir=profiles_dir,
        )


def test_build_runtime_readiness_report_requires_object_context_objects(tmp_path) -> None:
    packs_dir = tmp_path / "schemas/domain_packs"
    profiles_dir = tmp_path / "schemas/profiles"
    schema_path = tmp_path / "schemas/personal/broken.json"

    _write_file(
        schema_path,
        (
            '{'
            '"$metadata": {'
            '  "extraction": {'
            '    "main_data_array": "items",'
            '    "context_objects": ["scalar_context"],'
            '    "identifier_fields": ["scalar_context"]'
            '  },'
            '  "consolidation": {"deduplication": {"key_fields": ["name"], "ignore_fields": []}}'
            '},'
            '"type": "object",'
            '"properties": {'
            '  "scalar_context": {"type": "string"},'
            '  "items": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}}'
            '}'
            '}'
        ),
    )
    _write_file(
        packs_dir / "broken/pack.yaml",
        (
            "name: broken\n"
            "version: 1.0.0\n"
            f"schema_path: {schema_path.as_posix()}\n"
            "modules:\n"
            "  - module_id: mapper\n"
            "    enabled: true\n"
        ),
    )
    _write_file(
        profiles_dir / "default.profile.json",
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    with pytest.raises(ArtifactCompilerError, match="context object must resolve to an object node"):
        build_runtime_readiness_report(
            pack_ref="broken",
            profile_ref="default",
            repo_root=REPO_ROOT,
            domain_packs_dir=packs_dir,
            profiles_dir=profiles_dir,
        )


def test_build_runtime_readiness_report_requires_valid_qaqc_lane_fields(tmp_path) -> None:
    packs_dir = tmp_path / "schemas/domain_packs"
    profiles_dir = tmp_path / "schemas/profiles"
    schema_path = tmp_path / "schemas/personal/broken.json"

    _write_file(
        schema_path,
        (
            '{'
            '"$metadata": {'
            '  "extraction": {'
            '    "main_data_array": "items",'
            '    "context_objects": ["metadata"],'
            '    "identifier_fields": ["metadata.id"]'
            '  },'
            '  "consolidation": {"deduplication": {"key_fields": ["name"], "ignore_fields": []}}'
            '},'
            '"type": "object",'
            '"properties": {'
            '  "metadata": {"type": "object", "properties": {"id": {"type": "string"}}},'
            '  "items": {"type": "array", "items": {"type": "object", "properties": {'
            '    "name": {"type": "string"},'
            '    "value": {"type": "number"}'
            '  }}}'
            '}'
            '}'
        ),
    )
    _write_file(
        packs_dir / "broken/pack.yaml",
        (
            "name: broken\n"
            "version: 1.0.0\n"
            f"schema_path: {schema_path.as_posix()}\n"
            "qaqc:\n"
            "  default_lane: quantitative\n"
            "  lanes:\n"
            "    quantitative:\n"
            "      enabled: true\n"
            "      record_matching:\n"
            "        key_fields:\n"
            "          - missing_field\n"
            "      comparison:\n"
            "        primary_fields:\n"
            "          - value\n"
            "modules:\n"
            "  - module_id: mapper\n"
            "    enabled: true\n"
        ),
    )
    _write_file(
        profiles_dir / "default.profile.json",
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    with pytest.raises(ArtifactCompilerError, match="could not be resolved against the schema"):
        build_runtime_readiness_report(
            pack_ref="broken",
            profile_ref="default",
            repo_root=REPO_ROOT,
            domain_packs_dir=packs_dir,
            profiles_dir=profiles_dir,
        )


def test_build_runtime_readiness_report_requires_valid_qaqc_projection_fields(tmp_path) -> None:
    packs_dir = tmp_path / "schemas/domain_packs"
    profiles_dir = tmp_path / "schemas/profiles"
    schema_path = tmp_path / "schemas/personal/broken.json"

    _write_file(
        schema_path,
        (
            '{'
            '"$metadata": {'
            '  "extraction": {'
            '    "main_data_array": "rate_schedules",'
            '    "context_objects": ["metadata"],'
            '    "identifier_fields": ["metadata.id"]'
            '  },'
            '  "consolidation": {"deduplication": {"key_fields": ["rate_name"], "ignore_fields": []}}'
            '},'
            '"type": "object",'
            '"properties": {'
            '  "metadata": {"type": "object", "properties": {"id": {"type": "string"}}},'
            '  "rate_schedules": {"type": "array", "items": {"type": "object", "properties": {'
            '    "rate_name": {"type": "string"},'
            '    "charges": {"type": "array", "items": {"type": "object", "properties": {"rate": {"type": "number"}}}}'
            '  }}}'
            '}'
            '}'
        ),
    )
    _write_file(
        packs_dir / "broken/pack.yaml",
        (
            "name: broken\n"
            "version: 1.0.0\n"
            f"schema_path: {schema_path.as_posix()}\n"
            "qaqc:\n"
            "  default_lane: quantitative\n"
            "  lanes:\n"
            "    quantitative:\n"
            "      enabled: true\n"
            "      projection:\n"
            "        type: nested_array_items\n"
            "        source_array: rate_schedules\n"
            "        nested_array: charges\n"
            "        parent_fields:\n"
            "          - missing_parent\n"
            "      record_matching:\n"
            "        key_fields:\n"
            "          - rate_name\n"
            "      comparison:\n"
            "        primary_fields:\n"
            "          - rate\n"
            "modules:\n"
            "  - module_id: mapper\n"
            "    enabled: true\n"
        ),
    )
    _write_file(
        profiles_dir / "default.profile.json",
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    with pytest.raises(ArtifactCompilerError, match="Schema path could not be resolved"):
        build_runtime_readiness_report(
            pack_ref="broken",
            profile_ref="default",
            repo_root=REPO_ROOT,
            domain_packs_dir=packs_dir,
            profiles_dir=profiles_dir,
        )


def test_build_runtime_readiness_report_requires_valid_consolidation_key_fields(tmp_path) -> None:
    packs_dir = tmp_path / "schemas/domain_packs"
    profiles_dir = tmp_path / "schemas/profiles"
    schema_path = tmp_path / "schemas/personal/broken.json"

    _write_file(
        schema_path,
        (
            '{'
            '"$metadata": {'
            '  "extraction": {'
            '    "main_data_array": "items",'
            '    "context_objects": ["metadata"],'
            '    "identifier_fields": ["metadata.id"]'
            '  },'
            '  "consolidation": {"deduplication": {"key_fields": ["name"], "ignore_fields": []}}'
            '},'
            '"type": "object",'
            '"properties": {'
            '  "metadata": {"type": "object", "properties": {"id": {"type": "string"}}},'
            '  "items": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}}'
            '}'
            '}'
        ),
    )
    _write_file(
        packs_dir / "broken/pack.yaml",
        (
            "name: broken\n"
            "version: 1.0.0\n"
            f"schema_path: {schema_path.as_posix()}\n"
            "consolidation:\n"
            "  deduplication:\n"
            "    key_fields:\n"
            "      - missing_field\n"
            "modules:\n"
            "  - module_id: mapper\n"
            "    enabled: true\n"
        ),
    )
    _write_file(
        profiles_dir / "default.profile.json",
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    with pytest.raises(ArtifactCompilerError, match="Consolidation deduplication field could not be resolved"):
        build_runtime_readiness_report(
            pack_ref="broken",
            profile_ref="default",
            repo_root=REPO_ROOT,
            domain_packs_dir=packs_dir,
            profiles_dir=profiles_dir,
        )


def test_build_runtime_readiness_report_requires_valid_consolidation_output_fields(tmp_path) -> None:
    packs_dir = tmp_path / "schemas/domain_packs"
    profiles_dir = tmp_path / "schemas/profiles"
    schema_path = tmp_path / "schemas/personal/broken.json"

    _write_file(
        schema_path,
        (
            '{'
            '"$metadata": {'
            '  "extraction": {'
            '    "main_data_array": "items",'
            '    "context_objects": ["metadata"],'
            '    "identifier_fields": ["metadata.id"]'
            '  },'
            '  "consolidation": {"deduplication": {"key_fields": ["name"], "ignore_fields": []}}'
            '},'
            '"type": "object",'
            '"properties": {'
            '  "metadata": {"type": "object", "properties": {"id": {"type": "string"}, "jurisdiction": {"type": "string"}}},'
            '  "items": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "value": {"type": "number"}}}}'
            '}'
            '}'
        ),
    )
    _write_file(
        packs_dir / "broken/pack.yaml",
        (
            "name: broken\n"
            "version: 1.0.0\n"
            f"schema_path: {schema_path.as_posix()}\n"
            "consolidation:\n"
            "  output:\n"
            "    exclude_fields:\n"
            "      - Missing Column\n"
            "modules:\n"
            "  - module_id: mapper\n"
            "    enabled: true\n"
        ),
    )
    _write_file(
        profiles_dir / "default.profile.json",
        '{"profile_id": "default", "runtime": {"emit_lineage": true}}',
    )

    with pytest.raises(ArtifactCompilerError, match="output.exclude_fields entry could not be resolved"):
        build_runtime_readiness_report(
            pack_ref="broken",
            profile_ref="default",
            repo_root=REPO_ROOT,
            domain_packs_dir=packs_dir,
            profiles_dir=profiles_dir,
        )
