"""Shared constants for the discovery module."""

from __future__ import annotations

DEFAULT_MAX_CONCURRENT_DOWNLOADS = 2
USER_AGENT = "ParseSweep/2.0 (+discovery)"
RESERVED_SEARCH_PARAMS = frozenset({"q", "num", "api_key"})


# ---------------------------------------------------------------------------
# File types
# ---------------------------------------------------------------------------

# Real, extractable document files (what a "supported document" means).
DOCUMENT_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".doc", ".docx", ".txt", ".xlsx", ".csv"}
)

# Everything the downloader/crawler may fetch — documents plus web pages.
DOWNLOADABLE_EXTENSIONS: frozenset[str] = DOCUMENT_EXTENSIONS | frozenset(
    {".html", ".htm"}
)

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
