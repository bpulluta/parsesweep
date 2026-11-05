"""Input provenance checks for compilation safety warnings."""

from __future__ import annotations

import json
from pathlib import Path
from psweep.security import protected_mode


INTERNAL_RECORD_DIRS = {"validation", "run_manifests", "qa_qc"}
INTERNAL_RECORD_FILES = {"metadata.json", "comparison_summary.json"}
_UNRESOLVED_ARTIFACT_ID = "artifact://runtime/unresolved"


def read_compile_record(path: Path) -> dict:
    """Read the compilation envelope, rejecting invalid or missing payloads."""
    try:
        with path.open(encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: cannot read extraction record ({type(exc).__name__})") from exc
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
        raise ValueError(  # noqa: TRY004 - callers handle ValueError
            f"{path}: extraction record must contain an object payload"
        )
    if protected_mode():
        from psweep.extraction.schema_utils import record_validator

        if not record_validator().is_valid(record):
            raise ValueError(
                f"{path}: protected compilation requires a canonical extraction record"
            )
    return record


def _canonical_path(path_value: str | Path, *, cwd: Path) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def list_compile_record_files(input_dir: Path) -> list[Path]:
    """Return sorted record JSON files under input_dir, excluding internal dirs."""
    return sorted(
        path
        for path in input_dir.rglob("*.json")
        if not INTERNAL_RECORD_DIRS.intersection(path.relative_to(input_dir).parts)
        and path.name not in INTERNAL_RECORD_FILES
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
    invalid_envelope_records = 0
    invalid_envelope_names = []
    from psweep.extraction.schema_utils import record_validator

    for record_path in record_files:
        canonical_records.add(record_path.resolve())
        payload = read_compile_record(record_path)
        if not record_validator().is_valid(payload):
            invalid_envelope_records += 1
            if len(invalid_envelope_names) < 5:
                invalid_envelope_names.append(record_path.name)
        lineage = payload.get("lineage")
        if not isinstance(lineage, dict):
            missing_schema_id_records += 1
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
    if invalid_envelope_records:
        warnings.append(
            f"invalid_record_envelope: {invalid_envelope_records} extraction record(s) "
            "do not satisfy the canonical extraction-record schema "
            f"({', '.join(invalid_envelope_names)})"
        )
    if len(schema_ids) > 1:
        warnings.append(
            _format_multi_value_warning(label="schema_id", values=schema_ids, file_count=file_count)
        )
    if len(models) > 1:
        warnings.append(
            _format_multi_value_warning(label="model", values=models, file_count=file_count)
        )
    if len(providers) > 1:
        warnings.append(
            _format_multi_value_warning(label="provider", values=providers, file_count=file_count)
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
        except Exception:  # noqa: BLE001, S112 - corrupt manifests carry no evidence
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
