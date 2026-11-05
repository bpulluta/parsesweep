"""Shared constants for the discovery module."""

from __future__ import annotations

from psweep._version import __version__
from psweep.extraction import document_utils

DEFAULT_MAX_CONCURRENT_DOWNLOADS = 2
# robots.txt product token, and the one HTTP/browser User-Agent built from it.
# The "compatible" form passes WAFs that reject non-browser agents or agents
# carrying URLs/words like "discovery", without claiming a browser build.
ROBOTS_AGENT = "ParseSweep"
USER_AGENT = f"Mozilla/5.0 (compatible; {ROBOTS_AGENT}/{__version__})"
RESERVED_SEARCH_PARAMS = frozenset({"q", "num", "api_key"})


# ---------------------------------------------------------------------------
# File types
# ---------------------------------------------------------------------------

# Real documents (what a "supported document" means) and everything the
# downloader/crawler may fetch (documents plus web pages): extraction's
# one file-type definition.
DOCUMENT_EXTENSIONS: frozenset[str] = document_utils.DOCUMENT_EXTENSIONS
DOWNLOADABLE_EXTENSIONS: frozenset[str] = document_utils.SUPPORTED_EXTENSIONS

# Content-Type (MIME) → canonical extension.
MIME_TO_EXTENSION: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
    "text/html": ".html",
    "text/csv": ".csv",
    "application/csv": ".csv",
    "application/vnd.ms-excel": ".xlsx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/json": ".json",
    "application/geo+json": ".json",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
}


# ---------------------------------------------------------------------------
# Candidate provenance markers
# ---------------------------------------------------------------------------

# Candidate source for explicitly configured URLs (target ``urls`` and shared
# ``sources``): always downloaded and kept, exempt from selection/review caps.
CONFIGURED_SOURCE = "configured"
ROUTE_REASON_CRAWLED = "Linked from a crawled source page"


# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------

# Seeker-only candidates come from a search-engine ranking; there is no crawl,
# so anchor and content signals are structurally absent. Judging them with the
# default crawl-oriented weights caps every such candidate at ~0.26 (always
# "rejected"). These weights redistribute the missing anchor/content weight
# onto the signals a search result actually has (url + trust), so a genuine
# seeker result reaches "needs_review" and a strong, prioritized one can reach
# "accepted".
SEEKER_ONLY_SCORE_WEIGHTS: dict[str, float] = {
    "url_signal": 0.6,
    "trust_signal": 0.4,
}
