"""Shared discovery URL helpers: parse, match, canonicalize, redact.

The link prioritizer (scoring) and the engine (MIME-aware download
gating) keep their own extension parsers on purpose.
"""

from __future__ import annotations

import re
from functools import lru_cache
from html import unescape
from pathlib import Path
from urllib.parse import (
    parse_qsl, unquote, urlencode, urlparse, urlsplit, urlunparse, urlunsplit,
)

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


_TRACKING_PARAMETER_RE = re.compile(
    r"^(?:utm_.*|gclid|gclsrc|dclid|gbraid|wbraid|fbclid|msclkid|yclid|igshid|"
    r"mc_cid|mc_eid|_ga|_gl|srsltid)$",
    re.IGNORECASE,
)


def canonical_url(url: str) -> str:
    """Drop click-tracking query parameters, keeping every other byte.

    Other parameters can identify the document (``?bidId=``,
    ``?fileID=``, ``?<id>_page=N``), so only known tracking keys go.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    if not parsed.query:
        return url
    segments = parsed.query.split("&")
    kept = [
        segment for segment in segments
        if not _TRACKING_PARAMETER_RE.match(unquote(segment.split("=", 1)[0]))
    ]
    if len(kept) == len(segments):
        return url
    return urlunsplit(parsed._replace(query="&".join(kept)))


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


def is_http_url(url: object) -> bool:
    """Return True for an absolute http(s) URL with a host."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
