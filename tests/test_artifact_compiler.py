"""Tests for the Phase 1 runtime artifact compiler slice."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from streamline_extract.core import ArtifactCompilerError, compile_runtime_artifact


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
