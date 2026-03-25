"""Compile domain-pack and profile inputs into a runtime artifact."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml


class ArtifactCompilerError(ValueError):
    """Raised when pack/profile resolution or compilation fails."""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_yaml_file(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ArtifactCompilerError(f"Pack file must contain an object: {path}")
    return loaded


def _load_json_file(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)

    if not isinstance(loaded, dict):
        raise ArtifactCompilerError(f"Profile file must contain an object: {path}")
    return loaded


def _resolve_pack_path(pack_ref: str, domain_packs_dir: Path) -> Path:
    direct = Path(pack_ref)
    if direct.is_file():
        return direct
    if direct.is_dir() and (direct / "pack.yaml").exists():
        return direct / "pack.yaml"

    candidate_file = domain_packs_dir / pack_ref
    candidate_dir = domain_packs_dir / pack_ref / "pack.yaml"
    if candidate_file.is_file():
        return candidate_file
    if candidate_file.is_dir() and (candidate_file / "pack.yaml").exists():
        return candidate_file / "pack.yaml"
    if candidate_dir.exists():
        return candidate_dir

    raise ArtifactCompilerError(
        f"Could not resolve pack '{pack_ref}' in {domain_packs_dir}"
    )


def _resolve_profile_path(profile_ref: str, profiles_dir: Path) -> Path:
    direct = Path(profile_ref)
    if direct.is_file():
        return direct

    raw_candidate = profiles_dir / profile_ref
    named_candidate = profiles_dir / f"{profile_ref}.profile.json"
    if raw_candidate.exists():
        return raw_candidate
    if named_candidate.exists():
        return named_candidate

    raise ArtifactCompilerError(
        f"Could not resolve profile '{profile_ref}' in {profiles_dir}"
    )


def _contract_versions(repo_root: Path) -> Dict[str, str]:
    extraction_path = repo_root / "schemas/core/extraction_record.schema.json"
    modules_path = repo_root / "schemas/core/modules_catalog.schema.json"

    for contract_path in (extraction_path, modules_path):
        if not contract_path.exists():
            raise ArtifactCompilerError(
                "Required core contract not found: "
                f"{contract_path.as_posix()}"
            )

    extraction_schema = _load_json_file(extraction_path)
    modules_schema = _load_json_file(modules_path)

    extraction_version = extraction_schema.get("$metadata", {}).get("version")
    modules_version = modules_schema.get("$metadata", {}).get("version")

    if not extraction_version or not modules_version:
        raise ArtifactCompilerError(
            "Core contracts must include $metadata.version fields"
        )

    return {
        "extraction_record": extraction_version,
        "modules_catalog": modules_version,
    }


def _pack_identity(pack_data: Dict[str, Any], fallback: str) -> Tuple[str, str]:
    pack_name = pack_data.get("name") or pack_data.get("pack_name") or fallback
    pack_version = pack_data.get("version") or pack_data.get("pack_version")

    if not pack_version:
        raise ArtifactCompilerError(
            "Pack definition must include 'version' or 'pack_version'"
        )

    return str(pack_name), str(pack_version)


def _profile_identity(profile_data: Dict[str, Any], fallback: str) -> str:
    profile_name = profile_data.get("profile_id") or profile_data.get("name")
    if not profile_name:
        profile_name = fallback
    return str(profile_name)


def _artifact_id(seed_data: Dict[str, Any]) -> str:
    canonical = json.dumps(seed_data, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"artifact://runtime/{digest[:16]}"


def compile_runtime_artifact(
    pack_ref: str,
    profile_ref: str,
    *,
    repo_root: Optional[Path] = None,
    domain_packs_dir: Optional[Path] = None,
    profiles_dir: Optional[Path] = None,
    compiled_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Resolve a pack and profile and compile a deterministic runtime artifact."""
    root = repo_root or _project_root()
    packs_root = domain_packs_dir or (root / "schemas/domain_packs")
    profiles_root = profiles_dir or (root / "schemas/profiles")

    pack_path = _resolve_pack_path(pack_ref, packs_root)
    profile_path = _resolve_profile_path(profile_ref, profiles_root)

    pack_data = _load_yaml_file(pack_path)
    profile_data = _load_json_file(profile_path)
    contract_versions = _contract_versions(root)

    pack_name, pack_version = _pack_identity(pack_data, fallback=pack_path.stem)
    profile_name = _profile_identity(profile_data, fallback=profile_path.stem)

    identity_seed = {
        "pack_name": pack_name,
        "pack_version": pack_version,
        "profile_name": profile_name,
        "pack": pack_data,
        "profile": profile_data,
        "contract_versions": contract_versions,
    }
    artifact_id = _artifact_id(identity_seed)

    timestamp = compiled_at or datetime.now(timezone.utc)
    compiled_at_iso = timestamp.isoformat().replace("+00:00", "Z")

    return {
        "artifact_id": artifact_id,
        "pack_name": pack_name,
        "pack_version": pack_version,
        "profile_name": profile_name,
        "compiled_at": compiled_at_iso,
        "contract_versions": contract_versions,
        "lineage": {
            "artifact_id": artifact_id,
            "profile_id": profile_name,
            "pack_name": pack_name,
            "pack_version": pack_version,
            "compiled_at": compiled_at_iso,
        },
        "source": {
            "pack_path": pack_path.as_posix(),
            "profile_path": profile_path.as_posix(),
        },
        "resolved": {
            "pack": pack_data,
            "profile": profile_data,
        },
    }
