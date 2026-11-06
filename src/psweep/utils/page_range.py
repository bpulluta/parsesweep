"""Page selections: sorted, disjoint, inclusive 1-indexed page ranges.

One grammar is shared by ``--pages``, ``extraction.pages.range`` and messages:
comma-separated pages or ``start-end`` ranges, e.g. ``"27,68-73"``. A pages CSV
lists one ``start_page``/``end_page`` row per range, so a file may have several.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path

PageRanges = tuple[tuple[int, int], ...]

_CSV_COLUMNS = ("file_path", "start_page", "end_page")


def normalize_page_ranges(ranges: Iterable) -> PageRanges:
    """Validate ``(start, end)`` pairs, then sort and merge touching ranges."""
    pairs = []
    for rng in ranges:
        if not isinstance(rng, (tuple, list)) or len(rng) != 2:
            message = f"Invalid page range {rng!r}: expected a (start, end) pair"
            raise ValueError(message)
        start, end = rng
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            message = f"Invalid page range {rng!r}: pages are integers >= 1 and start <= end"
            raise ValueError(message)
        pairs.append((start, end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(pairs):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def ranges_from_pages(pages: Iterable[int]) -> PageRanges:
    """Compress page numbers into ranges."""
    return normalize_page_ranges((page, page) for page in pages)


def page_numbers(ranges: PageRanges) -> list[int]:
    """Expand ranges into sorted page numbers."""
    return [page for start, end in ranges for page in range(start, end + 1)]


def count_pages(ranges: PageRanges) -> int:
    """Total pages in a selection."""
    return sum(end - start + 1 for start, end in ranges)


def format_page_ranges(ranges: PageRanges) -> str:
    """Render ranges in the shared grammar, e.g. ``27,68-73``."""
    return ",".join(str(start) if start == end else f"{start}-{end}" for start, end in ranges)


def _page_number(text: str, context: str) -> int:
    value = text.strip()
    if not value.isdigit():
        message = f"Invalid page number {text!r} in {context}"
        raise ValueError(message)
    return int(value)


def parse_page_ranges(spec: str) -> PageRanges:
    """Parse ``"27,68-73"`` into normalized ranges; reject anything else."""
    context = f"page selection {spec!r} (use pages or ranges like '27,68-73')"
    pairs = []
    for item in spec.split(","):
        bounds = item.split("-")
        if len(bounds) > 2:
            message = f"Invalid page range {item!r} in {context}"
            raise ValueError(message)
        numbers = [_page_number(bound, context) for bound in bounds]
        pairs.append((numbers[0], numbers[-1]))
    return normalize_page_ranges(pairs)


def load_pages_csv(csv_path: Path) -> dict[str, PageRanges | None]:
    """Map each file to its page ranges, or ``None`` for the full document.

    .. code-block:: text

        file_path,start_page,end_page
        tariff.pdf,27,27
        tariff.pdf,68,73
        small_doc.pdf,,

    Rows for the same file combine. Blank bounds select the full document and
    cannot be combined with ranges for that file.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    rows: dict[str, list[tuple[int, int]] | None] = {}
    with Path(csv_path).open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not all(column in (reader.fieldnames or []) for column in _CSV_COLUMNS):
            message = f"CSV must have columns: {', '.join(_CSV_COLUMNS)}"
            raise ValueError(message)
        for row_num, row in enumerate(reader, start=2):
            file_path = row["file_path"].strip()
            if not file_path:
                continue
            start, end = row["start_page"].strip(), row["end_page"].strip()
            context = f"row {row_num} ({file_path})"
            if bool(start) != bool(end):
                message = f"Set both start_page and end_page, or neither, in {context}"
                raise ValueError(message)
            full_document = not start
            if file_path in rows and (rows[file_path] is None) != full_document:
                message = f"Blank bounds cannot be combined with page ranges in {context}"
                raise ValueError(message)
            if full_document:
                rows[file_path] = None
                continue
            pair = (_page_number(start, context), _page_number(end, context))
            try:
                normalize_page_ranges([pair])
            except ValueError as exc:
                message = f"{exc} in {context}"
                raise ValueError(message) from exc
            rows.setdefault(file_path, []).append(pair)
    return {
        file_path: None if pairs is None else normalize_page_ranges(pairs)
        for file_path, pairs in rows.items()
    }
