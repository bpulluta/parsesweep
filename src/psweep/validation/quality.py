"""Per-contender quote fidelity, completeness and record-billed cost."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from psweep.compilation.input_provenance import list_compile_record_files, read_compile_record
from psweep.extraction.document_utils import extract_text_from_document, read_text_cache
from psweep.extraction.pdf_utils import extract_pages_text
from psweep.extraction.roster import evidence_nodes, quote_present
from psweep.extraction.schema_utils import evidence_paths
from psweep.utils.schema_metadata import SchemaMetadata


def failed_documents(extracted_dir: Path) -> set[str]:
    """Document paths with errors in the newest run manifest (by ``timing.finished_at``)."""
    newest, finished = None, None
    for path in sorted((Path(extracted_dir) / "run_manifests").glob("*.manifest.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            stamp = datetime.fromisoformat(manifest["timing"]["finished_at"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"{path}: unreadable run manifest ({type(exc).__name__})") from exc
        if finished is None or stamp > finished:
            newest, finished = manifest, stamp
    records = ((newest or {}).get("errors") or {}).get("records") or []
    return {
        record["source"]["document_path"]
        for record in records
        if (record.get("source") or {}).get("document_path")
    }


def _valued(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _flat_rows(items: list, row_array: str | None) -> Iterator[dict]:
    """One dict per compiled row: the item, or parent fields merged with each child."""
    for item in items:
        children = item.get(row_array) if row_array else None
        if not children:
            yield item
        for child in children or ():
            yield {**item, **child}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _source_text(source: str, ocr_corrections: list | None) -> str | None:
    """Cached text (OCR/browser) or native parsing; never runs OCR. ``None`` when empty."""
    path = Path(source)
    try:
        if path.suffix.lower() == ".pdf" and read_text_cache(path) is None:
            text = "\n".join(extract_pages_text(path))
        else:
            text = extract_text_from_document(path, ocr_corrections=ocr_corrections)
    except Exception:  # noqa: BLE001 - an unreadable source leaves its quotes unchecked
        return None
    return text if text and text.strip() else None


@dataclass
class _Tally:
    verified: int = 0
    unverified: int = 0
    unchecked: int = 0
    off_candidate: int = 0
    docs: int = 0
    called: int = 0
    unknown_cost: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    completeness: list = field(default_factory=list)
    seconds: list = field(default_factory=list)

    def add(self, record: dict, paths: list) -> None:
        quality = record.get("quality") or {}
        metrics = record.get("processing_metrics") or {}
        self.docs += 1
        self.off_candidate += quality.get("off_candidate_items") or 0
        if quality.get("completeness") is not None:
            self.completeness.append(quality["completeness"])
        if paths:
            self.verified += sum(
                isinstance(node, dict) and node.get("value") is not None
                for evidence in paths
                for _, node in evidence_nodes(record["payload"], evidence)
            )
            self.unverified += quality.get("unverified_quotes") or 0
        if (metrics.get("input_tokens") or 0) > 0:
            self.called += 1
            if metrics.get("cost_usd") is None:
                self.unknown_cost += 1
            else:
                self.cost += metrics["cost_usd"]
            self.seconds.append(metrics.get("duration_seconds") or 0.0)
        self.input_tokens += metrics.get("input_tokens") or 0
        self.output_tokens += metrics.get("output_tokens") or 0

    def summary(self, extracted_dir: Path, measured: bool) -> dict:
        checked = self.verified + self.unverified
        total = None if self.unknown_cost else self.cost
        return {
            "quotes_verified": self.verified if measured else None,
            "quotes_unverified": self.unverified if measured else None,
            "quotes_unchecked": self.unchecked if measured else None,
            "quote_fidelity": self.verified / checked if checked else None,
            "completeness_mean": _mean(self.completeness),
            "off_candidate_items": self.off_candidate,
            "docs_ok": self.docs,
            "docs_failed": len(failed_documents(extracted_dir)),
            "documents_called": self.called,
            "cost_usd": total,
            "cost_per_document": total / self.called if total is not None and self.called else None,
            "unknown_cost_documents": self.unknown_cost,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "seconds_per_document": _mean(self.seconds),
        }


def model_quality(
    dirs: Mapping[str, Path],
    metadata: SchemaMetadata,
    *,
    quote_field: str | None,
    focus: Sequence[str],
    ocr_corrections: list | None = None,
) -> dict[str, dict]:
    """Quality and cost of each contender's current records (``None`` = blank).

    Contenders share record paths, so each source document is read once for all of them.
    """
    main = metadata.get_main_data_array()
    row_array = metadata.get_row_array()
    paths = [] if quote_field else evidence_paths(metadata.schema)
    files = {
        label: {path.relative_to(root): path for path in list_compile_record_files(Path(root))}
        for label, root in dirs.items()
    }
    tallies = {label: _Tally() for label in dirs}
    for relative in sorted(set().union(*files.values())):
        quoted: list[tuple[str, str | None, list]] = []
        for label, found in files.items():
            if relative not in found:
                continue
            record = read_compile_record(found[relative])
            tallies[label].add(record, paths)
            if quote_field:
                quotes = [
                    row.get(quote_field)
                    for row in _flat_rows(record["payload"].get(main) or [], row_array)
                    if any(_valued(row.get(f)) for f in focus)
                ]
                if quotes:
                    source = (record.get("document") or {}).get("source_path")
                    quoted.append((label, source, quotes))
        texts: dict[str | None, str | None] = {None: None}
        for label, source, quotes in quoted:
            if source not in texts:
                texts[source] = _source_text(source, ocr_corrections)
            tally, text = tallies[label], texts[source]
            if text is None:
                tally.unchecked += len(quotes)
            else:
                present = sum(quote_present(quote, text) for quote in quotes)
                tally.verified += present
                tally.unverified += len(quotes) - present
    measured = bool(quote_field or paths)
    return {label: tallies[label].summary(Path(dirs[label]), measured) for label in dirs}
