"""Shared constants for the discovery module.

Single source of truth for file-type sets, MIME mapping, and the routing-reason
string literals that are produced in one place and matched by equality in
another. Centralizing these removes the historical duplication/drift across
engine, selector, prioritizer, and connectors.
"""

from __future__ import annotations

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
# Routing reason strings — produced in one method, matched by equality in the
# metrics builders. Kept here so producer and matcher can never drift apart.
# ---------------------------------------------------------------------------

ROUTE_REASON_DISTRIBUTED = "Routed via distributed digger path"
ROUTE_REASON_CENTRALIZED = "Routed via centralized hub sweep"


# ---------------------------------------------------------------------------
# Query-context aliases
# ---------------------------------------------------------------------------

# Coalesce aliases for query templates: an alias resolves to the first
# non-empty source field in its list. Domains override via config
# (discovery.query_context_aliases). The default preserves the legacy
# behavior where "{utility_or_jurisdiction}" fell back to jurisdiction then
# manufacturer, so existing configs keep working unchanged.
DEFAULT_QUERY_CONTEXT_ALIASES: dict[str, list[str]] = {
    "utility_or_jurisdiction": ["jurisdiction", "manufacturer"],
}


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
