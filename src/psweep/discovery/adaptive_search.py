"""Bounded fallback scheduling and exact-source coverage (no provider calls)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping

from psweep.utils.retry import is_retryable

from .constants import CONFIGURED_SOURCE
from .targets import identify_target
from .urls import decode_discovery_url


def attempted_urls(records: Iterable[Mapping]) -> dict[str, set[str]]:
    """Exclude requested and redirected URLs independently for each target."""
    urls: dict[str, set[str]] = defaultdict(set)
    for record in records:
        key = str(record.get("target_id") or "")
        for field in ("url", "final_url"):
            if record.get(field):
                urls[key].add(decode_discovery_url(str(record[field])))
    return dict(urls)


def distinct_accepted_count(records: Iterable[Mapping]) -> int:
    """Count automatic qualified sources, collapsing final URL or byte mirrors."""
    urls: set[str] = set()
    hashes: set[str] = set()
    count = 0
    for record in records:
        if (
            record.get("source") == CONFIGURED_SOURCE
            or record.get("status") != "downloaded"
            or not record.get("review_selected")
            or record.get("review_failed")
        ):
            continue
        url = decode_discovery_url(str(record.get("final_url") or record.get("url") or ""))
        content = str(record.get("content_hash") or "")
        duplicate = (url and url in urls) or (content and content in hashes)
        if url:
            urls.add(url)
        if content:
            hashes.add(content)
        count += not duplicate
    return count


def covered_labels(records: Iterable[Mapping]) -> set[str]:
    """Read verified roles only from successfully selected primary sources.

    The reviewer owns quote verification; scheduling never infers roles from
    grades, reasons, configured URLs, or rejected/secondary evidence.
    """
    labels: set[str] = set()
    for record in records:
        if (
            record.get("status") != "downloaded"
            or not record.get("review_selected")
            or not record.get("review_is_primary")
            or record.get("review_failed")
        ):
            continue
        evidence = record.get("review_evidence")
        coverage = evidence.get("coverage") if isinstance(evidence, Mapping) else None
        if isinstance(coverage, Mapping):
            labels.update(
                label
                for label, support in coverage.items()
                if isinstance(label, str)
                and isinstance(support, Mapping)
                and isinstance(support.get("quote"), str)
                and support["quote"].strip()
            )
    return labels


def eligible_targets(
    targets: Iterable[dict[str, object]],
    records: Iterable[dict[str, object]],
    *,
    keep_top: int,
    partition_by: list[str] | None,
    incomplete_keys: set[str],
    coverage: Mapping[str, str] | None = None,
) -> list[dict[str, object]]:
    """Schedule weak targets only after all initial work has a final outcome."""
    by_target: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in records:
        by_target[str(record.get("target_id") or "")].append(record)
    eligible = []
    for target in targets:
        key = identify_target(target, partition_by)
        outcomes = by_target[key]
        if key in incomplete_keys or any(is_retryable(record) for record in outcomes):
            continue
        if distinct_accepted_count(outcomes) < keep_top or (
            coverage and set(coverage) - covered_labels(outcomes)
        ):
            eligible.append(target)
    return eligible


def source_unique_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Keep mirrors for audit, but grade/select only one exact source per target.

    Failures are deliberately excluded: merged review must not retry a failed
    decision or clear its pending flags in the same run. Rejected/held documents
    are handled by the existing reviewer and eligibility gate.
    """
    urls: dict[str, set[str]] = defaultdict(set)
    hashes: dict[str, set[str]] = defaultdict(set)
    unique = []
    for record in records:
        if record.get("review_failed"):
            continue
        if record.get("status") != "downloaded":
            unique.append(record)
            continue
        key = str(record.get("target_id") or "")
        url = decode_discovery_url(str(record.get("final_url") or record.get("url") or ""))
        content = str(record.get("content_hash") or "")
        duplicate = (url and url in urls[key]) or (content and content in hashes[key])
        if url:
            urls[key].add(url)
        if content:
            hashes[key].add(content)
        if duplicate and record.get("source") != CONFIGURED_SOURCE:
            record["review_selected"] = False
            record["review_redundant"] = True
            record["review_reason"] = "Duplicate final URL or content"
        else:
            unique.append(record)
    return unique
