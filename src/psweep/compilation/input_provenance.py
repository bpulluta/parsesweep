"""Input provenance checks for compilation safety warnings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_EXCLUDED_DIRS = {"validation", "run_manifests", "qa_qc"}
_UNRESOLVED_ARTIFACT_ID = "artifact://runtime/unresolved"


def _canonical_path(path_value: str | Path, *, cwd: Path) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def list_compile_record_files(input_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in input_dir.rglob("*.json")
        if not _EXCLUDED_DIRS.intersection(path.relative_to(input_dir).parts)
    )


def _format_multi_value_warning(
    *,
    label: str,
    values: set[str],
    file_count: int,
) -> str:
    sorted_values = sorted(values)
    preview = ", ".join(sorted_values[:4])
    if len(sorted_values) > 4:
        preview += f", +{len(sorted_values) - 4} more"
    return (
        f"mixed_{label}: {len(sorted_values)} distinct {label} values across "
        f"{file_count} extraction record(s) ({preview})"
    )


def analyze_compile_input_provenance(input_dir: Path) -> list[str]:
    """Return non-fatal warnings about mixed/stale extraction inputs."""
    warnings: list[str] = []
    record_files = list_compile_record_files(input_dir)
    if not record_files:
        return warnings

    schema_ids: set[str] = set()
    models: set[str] = set()
    providers: set[str] = set()
    canonical_records: set[Path] = set()
    unresolved_artifact_records = 0
    missing_schema_id_records = 0

    for record_path in record_files:
        canonical_records.add(record_path.resolve())
        try:
            payload = json.loads(record_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("payload"), dict):
            continue
        lineage = payload.get("lineage")
        if not isinstance(lineage, dict):
            continue
        schema_id = lineage.get("schema_id")
        if isinstance(schema_id, str) and schema_id.strip():
            schema_ids.add(schema_id.strip())
        else:
            missing_schema_id_records += 1
        model = lineage.get("model")
        if isinstance(model, str) and model.strip():
            models.add(model.strip())
        provider = lineage.get("provider")
        if isinstance(provider, str) and provider.strip():
            providers.add(provider.strip())
        artifact_id = lineage.get("artifact_id")
        if artifact_id == _UNRESOLVED_ARTIFACT_ID:
            unresolved_artifact_records += 1

    file_count = len(canonical_records)
    if len(schema_ids) > 1:
        warnings.append(
            _format_multi_value_warning(
                label="schema_id", values=schema_ids, file_count=file_count
            )
        )
    if len(models) > 1:
        warnings.append(
            _format_multi_value_warning(
                label="model", values=models, file_count=file_count
            )
        )
    if len(providers) > 1:
        warnings.append(
            _format_multi_value_warning(
                label="provider", values=providers, file_count=file_count
            )
        )
    if missing_schema_id_records > 0:
        warnings.append(
            f"missing_schema_id: {missing_schema_id_records} extraction record(s) "
            "have null/empty lineage.schema_id"
        )
    if unresolved_artifact_records > 0:
        warnings.append(
            f"unresolved_artifact_id: {unresolved_artifact_records} extraction "
            "record(s) contain lineage.artifact_id=artifact://runtime/unresolved"
        )

    manifests_dir = input_dir / "run_manifests"
    manifest_files = sorted(manifests_dir.glob("*.manifest.json"))
    if not manifest_files:
        return warnings

    referenced_records: set[Path] = set()
    parsed_manifests = 0
    cwd = Path.cwd()
    for manifest_path in manifest_files:
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(manifest_data, dict):
            continue
        parsed_manifests += 1
        outputs = manifest_data.get("outputs")
        if not isinstance(outputs, dict):
            continue
        records = outputs.get("records")
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, str) and record.strip():
                referenced_records.add(_canonical_path(record.strip(), cwd=cwd))

    if parsed_manifests > 0:
        orphaned = sorted(canonical_records - referenced_records)
        if orphaned:
            warnings.append(
                f"orphan_extraction_records: {len(orphaned)} extraction record(s) "
                f"in {input_dir.as_posix()} are not referenced by any run manifest"
            )

    return warnings
