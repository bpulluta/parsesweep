"""Code-host content adapters: fetch server-rendered ordinance text from
JavaScript code-hosting platforms whose public pages are empty SPA shells.

When a downloaded HTML file is a JS shell (little server-rendered text) and its
URL is on a supported code host, the discovery engine asks the matching adapter
to fetch the real content via that host's server-side API — no browser required.
This runs *before* the Selenium browser-escalation fallback because it is
cheaper, deterministic, and more reliable; anything an adapter cannot resolve
falls through to the browser path unchanged.

Adapters are pure: they receive an injected HTTP ``get`` callable
(``get(url, headers) -> (status_code, text)``) so this module has no network,
engine, or config dependency and is unit-testable with a fake ``get``.

The bundled :class:`MunicodeAdapter` targets ``library.municode.com``, the
dominant US municipal-code host. Municode serves public municipal law
(government edicts); this collects it as reference material, honors the site's
robots policy through the engine's existing policy gate, and uses a generic
User-Agent (never a named AI-crawler UA, which the site disallows).
"""

from __future__ import annotations

import html
import json
import re
from typing import Callable, Optional, Protocol
from urllib.parse import parse_qs, urlparse

# get(url, headers) -> (status_code, response_text). Raises on transport error.
HttpGet = Callable[[str, dict], "tuple[int, str]"]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_NORM_RE = re.compile(r"[^a-z0-9]+")


def _strip_html(fragment: str) -> str:
    """Turn an HTML content fragment into readable plain text."""
    # Coerce defensively: a non-str fragment must not raise (never-raise
    # contract; re.sub would TypeError on a truthy non-str/bytes value).
    text = _TAG_RE.sub(" ", str(fragment or ""))
    # Decode entities, then fold non-breaking spaces (&nbsp; -> \xa0) so numeric
    # values like "500 feet" stay contiguous and searchable.
    text = html.unescape(text).replace("\xa0", " ")
    text = _WS_RE.sub(" ", text)
    # Collapse runs of blank lines left by stripped block tags.
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _norm_name(value: str) -> str:
    """Normalize a jurisdiction name/slug for tolerant matching."""
    return _NORM_RE.sub("", str(value or "").lower())


class CodeHostAdapter(Protocol):
    """A per-host content fetcher for a JS code-hosting platform."""

    name: str

    def can_handle(self, url: str) -> bool:
        """Return True if this adapter handles ``url``'s host."""
        ...

    def fetch_text(self, url: str, get: HttpGet) -> Optional[str]:
        """Return clean text for ``url`` via ``get``, or None if unresolved."""
        ...


class MunicodeAdapter:
    """Fetch clean ordinance text from ``library.municode.com`` via its JSON API.

    The public page is an Angular SPA; the real content comes from ``/api``
    endpoints reachable with a single ``X-CSRF: 1`` header (no auth, cookie, or
    token). Given a public URL such as
    ``https://library.municode.com/tx/friendswood/codes/code_of_ordinances?nodeId=<node>``
    the resolution chain is: client (state + name) -> product -> latest job ->
    node content. ``groupChunks=true`` returns the node's whole subtree in one
    call so a link that points at a chapter/article still yields its sections.
    """

    name = "municode"
    # Only the content host has the /<state>/<client>/... layout the chain
    # parses; bare municode.com / www are marketing pages, not ordinances.
    _HOSTS = ("library.municode.com",)
    _API = "https://library.municode.com/api"
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; ParseSweep/1.0; "
            "reference municipal-law collection)"
        ),
        "Accept": "application/json",
        # The SPA's only anti-scraping gate: /api returns 401 without this.
        "X-CSRF": "1",
    }

    def can_handle(self, url: str) -> bool:
        """Return True for ``library.municode.com`` URLs."""
        host = (urlparse(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self._HOSTS)

    def fetch_text(self, url: str, get: HttpGet) -> Optional[str]:
        """Resolve the Municode API chain for ``url`` and return clean text."""
        parts = urlparse(url)
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) < 2:
            return None
        state, client = segments[0], segments[1]
        node = parse_qs(parts.query).get("nodeId", [None])[0]

        client_id = self._resolve_client_id(state, client, get)
        if client_id is None:
            return None
        product_id = self._resolve_product_id(client_id, get)
        if product_id is None:
            return None
        job_id = self._resolve_job_id(product_id, get)
        if job_id is None:
            return None
        return self._fetch_content(product_id, job_id, node, get)

    # -- chain steps -------------------------------------------------------

    def _get_json(self, url: str, get: HttpGet) -> object | None:
        try:
            status, body = get(url, dict(self._HEADERS))
        except Exception:  # noqa: BLE001 - transport error -> unresolved, fall through
            return None
        if status != 200 or not body:
            return None
        try:
            return json.loads(body)
        except (ValueError, TypeError):
            return None

    def _resolve_client_id(
        self, state: str, client: str, get: HttpGet
    ) -> int | None:
        data = self._get_json(
            f"{self._API}/Clients/stateAbbr?stateAbbr={state}", get
        )
        if not isinstance(data, list):
            return None
        # Exact normalized match ONLY. Normalization already absorbs
        # punctuation/case/separator differences (e.g. slug "st_paul" and
        # ClientName "St. Paul" both -> "stpaul"), so a prefix fallback buys
        # nothing and risks selecting a different, shorter municipality
        # (e.g. "Van" for slug "van_alstyne"). No match -> None -> browser.
        want = _norm_name(client)
        for entry in data:
            if not isinstance(entry, dict):
                continue
            cid = entry.get("ClientID")
            if isinstance(cid, int) and _norm_name(entry.get("ClientName", "")) == want:
                return cid
        return None

    def _resolve_product_id(self, client_id: int, get: HttpGet) -> int | None:
        data = self._get_json(f"{self._API}/ClientContent/{client_id}", get)
        if not isinstance(data, dict):
            return None
        codes = data.get("codes")
        if not isinstance(codes, list) or not codes:
            return None
        first = codes[0]
        pid = first.get("productId") if isinstance(first, dict) else None
        return pid if isinstance(pid, int) else None

    def _resolve_job_id(self, product_id: int, get: HttpGet) -> int | None:
        data = self._get_json(f"{self._API}/Jobs/latest/{product_id}", get)
        if not isinstance(data, dict):
            return None
        jid = data.get("Id")
        return jid if isinstance(jid, int) else None

    def _fetch_content(
        self, product_id: int, job_id: int, node: str | None, get: HttpGet
    ) -> str | None:
        # Without a nodeId we cannot target a section; fall through to browser.
        if not node:
            return None
        url = (
            f"{self._API}/CodesContent?productId={product_id}"
            f"&jobId={job_id}&nodeId={node}&groupChunks=true"
        )
        data = self._get_json(url, get)
        if not isinstance(data, dict):
            return None
        docs = data.get("Docs")
        if not isinstance(docs, list) or not docs:
            return None
        parts = [
            _strip_html(d["Content"])
            for d in docs
            if isinstance(d, dict) and isinstance(d.get("Content"), str)
        ]
        text = "\n".join(p for p in parts if p)
        return text.strip() or None


# Registry of bundled adapters, tried in order.
_ADAPTERS: list[CodeHostAdapter] = [MunicodeAdapter()]


def resolve_adapter(url: str) -> Optional[CodeHostAdapter]:
    """Return the first bundled adapter that can handle ``url``, or ``None``."""
    for adapter in _ADAPTERS:
        try:
            if adapter.can_handle(url):
                return adapter
        except Exception:  # noqa: BLE001 - a bad adapter must not break discovery
            continue
    return None
