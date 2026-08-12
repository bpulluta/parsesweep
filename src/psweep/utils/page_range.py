"""Utilities for handling page range specifications."""

import csv
from pathlib import Path
from typing import Dict, Optional, Tuple


def parse_page_range(page_spec: str) -> Tuple[int, int]:
    """
    Parse a page range specification string.

    Supports formats:
    - "100-200" (dash separator)
    - "100:200" (colon separator)
    - "100,200" (comma separator)

    Args:
        page_spec: Page range string (e.g., "615-759")

    Returns
    -------
        Tuple of (start_page, end_page) as 1-indexed integers

    Raises
    ------
        ValueError: If format is invalid
    """
    # Try different separators
    for sep in ["-", ":", ","]:
        if sep in page_spec:
            parts = page_spec.split(sep)
            if len(parts) == 2:
                try:
                    start = int(parts[0].strip())
                    end = int(parts[1].strip())

                    if start < 1 or end < 1:
                        raise ValueError("Page numbers must be >= 1")
                    if start > end:
                        raise ValueError(
                            f"Start page ({start}) must be <= end page ({end})"
                        )

                    return (start, end)
                except ValueError as e:
                    if "invalid literal" in str(e):
                        raise ValueError(
                            f"Invalid page numbers in '{page_spec}'"
                        )
                    raise

    raise ValueError(
        f"Invalid page range format: '{page_spec}'. "
        f"Use format like '615-759' or '100:200'"
    )


def load_pages_csv(csv_path: Path) -> Dict[str, Optional[Tuple[int, int]]]:
    """
    Load page range mappings from CSV file.

    CSV format:
        file_path,start_page,end_page
        tariff1.pdf,615,759
        tariff2.pdf,400,550
        small_doc.pdf,,

    Empty start/end pages mean process full document.

    Args:
        csv_path: Path to CSV file

    Returns
    -------
        Dictionary mapping file paths to page ranges (or None for full document)

    Raises
    ------
        ValueError: If CSV format is invalid
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    page_mappings = {}

    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)

        # Validate headers
        if not all(
            h in reader.fieldnames
            for h in ["file_path", "start_page", "end_page"]
        ):
            raise ValueError(
                "CSV must have columns: file_path, start_page, end_page"
            )

        for row_num, row in enumerate(
            reader, start=2
        ):  # Start at 2 (header is 1)
            file_path = row["file_path"].strip()
            start_str = row["start_page"].strip()
            end_str = row["end_page"].strip()

            if not file_path:
                continue  # Skip empty rows

            # If both start and end are empty, use full document
            if not start_str and not end_str:
                page_mappings[file_path] = None
            elif start_str and end_str:
                try:
                    start = int(start_str)
                    end = int(end_str)

                    if start < 1 or end < 1:
                        raise ValueError(
                            f"Page numbers must be >= 1 (row {row_num})"
                        )
                    if start > end:
                        raise ValueError(
                            f"Start page ({start}) must be <= end page ({end}) for {file_path} (row {row_num})"
                        )

                    page_mappings[file_path] = (start, end)
                except ValueError as e:
                    if "invalid literal" in str(e):
                        raise ValueError(
                            f"Invalid page numbers in row {row_num}: {file_path}"
                        )
                    raise
            else:
                raise ValueError(
                    f"Row {row_num}: Both start_page and end_page must be specified or both empty for {file_path}"
                )

    return page_mappings
