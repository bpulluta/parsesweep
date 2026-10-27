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
from functools import lru_cache
from html import unescape
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

_SEPARATOR_RE = re.compile(r"[_\-/]")


@lru_cache(maxsize=256)
def _compile_url_patterns(patterns: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    """Compile case-insensitive URL patterns once per distinct pattern set."""
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


def compile_url_patterns(patterns) -> tuple[re.Pattern, ...]:
    """Return compiled patterns, reusing a cached compile for repeated calls."""
    return _compile_url_patterns(tuple(patterns or ()))


def normalize_url_text(text: str) -> str:
    """Lowercase and turn ``_``, ``-``, ``/`` into spaces for term matching."""
    return _SEPARATOR_RE.sub(" ", text or "").lower()


_ESCAPED_URL_REPLACEMENTS = {
    "\\u0026": "&",
    "\\u003d": "=",
    "\\u003f": "?",
    "\\u003a": ":",
    "\\u002f": "/",
}


def decode_discovery_url(url: str) -> str:
    """Decode common escaped URL variants emitted by search APIs."""
    decoded = str(url or "").strip()
    if not decoded:
        return decoded
    for needle, repl in _ESCAPED_URL_REPLACEMENTS.items():
        decoded = decoded.replace(needle, repl)
    decoded = decoded.replace("\\/", "/")
    return unescape(decoded)


def matches_url_exclusion(url: str, patterns: list[re.Pattern]) -> bool:
    """Match raw and decoded URLs without discarding host/path punctuation."""
    raw = str(url or "")
    decoded = unquote(decode_discovery_url(raw))
    return any(pattern.search(value) for pattern in patterns for value in (raw, decoded))


def matches_allowed_domain(url: str, allowed_domains: list[str] | None) -> bool:
    """Match an exact hostname or subdomain, never an arbitrary substring."""
    if not allowed_domains:
        return True
    host = url_host(decode_discovery_url(url))
    return any(
        host == domain.strip().lower() or host.endswith("." + domain.strip().lower())
        for domain in allowed_domains if domain and domain.strip()
    )


_SECRET_PARAMETER_RE = re.compile(
    r"token|secret|password|passwd|signature|credential|authorization|api[-_]?key|"
    r"^key$|^auth$|^sig$|^x-amz-|^x-goog-|^awsaccesskeyid$|^googleaccessid$",
    re.IGNORECASE,
)


def redact_url(url: str) -> str:
    """Hide URL credentials/signatures in prompts, preserving document IDs."""
    try:
        parsed = urlparse(decode_discovery_url(url))
        host = parsed.netloc.rsplit("@", 1)[-1]
        query = urlencode([
            (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not _SECRET_PARAMETER_RE.search(key)
        ])
        # Fragments are never sent to the server and may contain OAuth tokens.
        return urlunparse(parsed._replace(netloc=host, query=query, fragment=""))
    except ValueError:
        return "[invalid URL]"


def redact_urls_in_text(text: str) -> str:
    """Sanitize URLs embedded in user-provided target context."""
    return re.sub(
        r"https?://[^\s<>\"']+",
        lambda match: redact_url(match.group()),
        text,
        flags=re.IGNORECASE,
    )


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
