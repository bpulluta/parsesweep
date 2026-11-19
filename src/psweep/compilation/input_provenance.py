"""Input provenance checks for compilation safety warnings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from psweep.security import protected_mode
from psweep.utils.file_io import artifact_sha256


INTERNAL_RECORD_DIRS = {"validation", "run_manifests", "qa_qc"}
INTERNAL_RECORD_FILES = {"metadata.json", "comparison_summary.json"}
_UNRESOLVED_ARTIFACT_ID = "artifact://runtime/unresolved"
DEFAULT_PROVENANCE_POLICY = "warn"


def read_compile_record(path: Path, *, sha256: str | None = None) -> dict:
    """Read the compilation envelope, rejecting invalid or missing payloads.

    With ``sha256`` the same bytes that are parsed must match the admitted digest.
    """
    try:
        raw = path.read_bytes()
        if sha256 is not None and hashlib.sha256(raw).hexdigest() != sha256:
            raise ValueError(f"{path}: record changed since admission; rerun extraction")
        record = json.loads(raw)
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


def list_compile_record_files(input_dir: Path) -> list[Path]:
    """Return sorted record JSON files under input_dir, excluding internal dirs."""
    return sorted(
        path
        for path in input_dir.rglob("*.json")
        if not INTERNAL_RECORD_DIRS.intersection(path.relative_to(input_dir).parts)
        and path.name not in INTERNAL_RECORD_FILES
    )


@dataclass(frozen=True)
class CompileInputs:
    """The one record set compilation may use, with its qualification.

    ``current``: admitted by the extraction output's current-admission view.
    ``historical_unverified``: an explicit standalone directory with no run
    evidence; its records were never verified against live sources.
    """

    qualification: str
    records: tuple[tuple[Path, str], ...]
    excluded: tuple[str, ...] = ()
    failed_documents: int = 0

    @property
    def summary(self) -> dict[str, int | str]:
        """Counts recorded in manifests and accounting."""
        return {
            "qualification": self.qualification,
            "admitted_records": len(self.records),
            "excluded_records": len(self.excluded),
            "failed_documents": self.failed_documents,
        }

    def empty_message(self, input_dir: Path) -> str:
        """Why nothing can be compiled."""
        if not self.excluded:
            return f"No extraction records found in {input_dir}"
        return (
            f"No admitted extraction records found in {input_dir} "
            f"({len(self.excluded)} preserved record(s) not admitted by the latest extraction)"
        )

    @property
    def files(self) -> list[Path]:
        """Admitted record paths."""
        return [path for path, _ in self.records]


def select_compile_inputs(input_dir: Path) -> CompileInputs:
    """Select admitted records, verifying the exact bytes admission described.

    Preserved historical records that are not admitted are reported, never
    compiled. Run evidence that is missing, broken, or contradicted by record
    bytes fails closed instead of falling back to directory enumeration.
    """
    from psweep.extraction.admission import ADMITTED_DISPOSITIONS, load_admission

    input_dir = Path(input_dir)
    on_disk = {
        path.relative_to(input_dir).as_posix(): path
        for path in list_compile_record_files(input_dir)
    }
    admission = load_admission(input_dir)
    if admission is None:
        if any((input_dir / "run_manifests").glob("*.manifest.json")):
            raise ValueError(
                f"{input_dir}: extraction run manifests exist but no current admission "
                "was published; rerun `psweep extract` to qualify its records"
            )
        records = tuple((path, artifact_sha256(path)) for path in on_disk.values())
        return CompileInputs("historical_unverified", records)
    records = []
    for rel, entry in sorted(admission.items()):
        if entry["disposition"] not in ADMITTED_DISPOSITIONS:
            continue
        path = on_disk.get(rel)
        if path is None:
            raise ValueError(f"{input_dir}: admitted record is missing: {rel}")
        if artifact_sha256(path) != entry["record_sha256"]:
            raise ValueError(f"{path}: record changed since admission; rerun extraction")
        records.append((path, entry["record_sha256"]))
    return CompileInputs(
        "current",
        tuple(records),
        excluded=tuple(
            sorted(set(on_disk) - {path.relative_to(input_dir).as_posix() for path, _ in records})
        ),
        failed_documents=sum(entry["disposition"] == "failed" for entry in admission.values()),
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


def analyze_compile_input_provenance(inputs: CompileInputs) -> list[str]:
    """Return non-fatal warnings about mixed lineage among the admitted records."""
    warnings: list[str] = []
    if not inputs.records:
        return warnings

    schema_ids: set[str] = set()
    models: set[str] = set()
    providers: set[str] = set()
    unresolved_artifact_records = 0
    missing_schema_id_records = 0
    invalid_envelope_records = 0
    invalid_envelope_names = []
    from psweep.extraction.schema_utils import record_validator

    for record_path, digest in inputs.records:
        payload = read_compile_record(record_path, sha256=digest)
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

    file_count = len(inputs.records)
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

    return warnings
