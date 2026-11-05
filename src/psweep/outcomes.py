"""Stage outcome statuses and the process exit codes every stage shares."""

from __future__ import annotations

EXIT_FAILED = 1
EXIT_EMPTY = 2
EXIT_PARTIAL = 3

EXIT_CODES = {
    "success": 0,
    "reused": 0,
    "dry_run": 0,
    "failed": EXIT_FAILED,
    "empty": EXIT_EMPTY,
    "partial_failure": EXIT_PARTIAL,
}


def exit_code(status: str) -> int:
    """Return the exit code for a stage outcome status.

    ``partial_failure`` means usable output exists alongside recorded errors;
    ``run`` continues downstream on it and re-runs retry the failed items.
    """
    try:
        return EXIT_CODES[status]
    except KeyError:
        message = f"Unknown stage outcome status: {status!r}"
        raise ValueError(message) from None
