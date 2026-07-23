"""Shared URL helpers for the discovery module.

These were previously duplicated (byte-for-byte in some cases) across the
engine and the candidate selector. Centralizing them removes the drift risk.

Note: the link prioritizer keeps its own length-guarded extension parser
(a scoring heuristic) and the engine keeps its MIME-aware extension resolver
(download gating) — those serve different purposes and are intentionally
not folded in here.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

_SEPARATOR_RE = re.compile(r"[_\-/]")


def normalize_url_text(text: str) -> str:
    """Lowercase and turn ``_``, ``-``, ``/`` into spaces for term matching."""
    return _SEPARATOR_RE.sub(" ", text or "").lower()


def url_host(url: str) -> str:
    """Return the lowercase hostname of *url*, or ``""`` if unparseable."""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def url_extension(url: str) -> str:
    """Return the lowercase file extension of the URL path (``""`` if none)."""
    try:
        path = urlparse(url).path.lower()
    except Exception:
        path = (url or "").lower()
    return Path(path).suffix.lower()
