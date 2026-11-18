"""Current-admission view of one extraction output.

Record bytes are history; this view says which of them the latest invocation
that covered each document admitted. Compilation reads only admitted records.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from psweep.utils.file_io import atomic_json

ADMISSION_VERSION = "1.0.0"
ADMISSION_NAME = "current_admission.json"
ADMITTED_DISPOSITIONS = frozenset({"new", "reused"})
DISPOSITIONS = ADMITTED_DISPOSITIONS | {"failed", "deferred"}


class AdmissionError(ValueError):
    """Admission evidence is unreadable, malformed, or contradicts record bytes."""


def admission_path(output: Path) -> Path:
    """Location of the atomic current-view pointer (not a run manifest)."""
    return Path(output) / "run_manifests" / ADMISSION_NAME


def _valid_entry(entry: Any) -> bool:
    if not isinstance(entry, dict) or entry.get("disposition") not in DISPOSITIONS:
        return False
    if entry["disposition"] in ADMITTED_DISPOSITIONS:
        digest = entry.get("record_sha256")
        return isinstance(digest, str) and len(digest) == 64
    return True


def load_admission(output: Path) -> dict[str, dict[str, Any]] | None:
    """Return ``{relative record path: entry}``; ``None`` when never published."""
    path = admission_path(output)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AdmissionError(
            f"{path}: admission evidence unreadable ({type(exc).__name__})"
        ) from exc
    records = data.get("records") if isinstance(data, dict) else None
    if (
        data.get("admission_version") != ADMISSION_VERSION
        or not isinstance(records, dict)
        or not all(isinstance(rel, str) and _valid_entry(entry) for rel, entry in records.items())
    ):
        raise AdmissionError(f"{path}: admission evidence is malformed or from another version")
    return records


def publish_admission(
    output: Path,
    *,
    run_id: str,
    scope: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Merge this invocation's scope into prior qualification and publish atomically.

    Unrelated prior entries survive while their record file exists; entries for
    documents in ``scope`` are replaced. Call while holding the output's
    publication lock. Unusable prior evidence qualifies nothing for the
    documents outside ``scope`` until a run covers them.
    """
    try:
        prior = load_admission(output) or {}
    except AdmissionError:
        prior = {}
    records = {rel: entry for rel, entry in prior.items() if (Path(output) / rel).is_file()}
    records.update(scope)
    atomic_json(
        admission_path(output),
        {"admission_version": ADMISSION_VERSION, "run_id": run_id, "records": records},
    )
    return records
