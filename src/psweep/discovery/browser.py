"""Headless-browser session for bot-protected sites (e.g. Akamai/Cloudflare).

Requests-based fetching is blocked at the edge by bot managers that inspect
TLS fingerprints and require JS challenge execution. A real Chrome instance
passes those challenges. This module wraps Selenium Chrome with sensible
anti-automation defaults and exposes:

- ``fetch_html(url)``      — return rendered page HTML (for crawling)
- ``current_links()``      — anchors on the current page (for link discovery)
- ``download_bytes(url)``  — fetch a file THROUGH the browser (real TLS), the
                             only reliable way past edge managers (Akamai) that
                             block ``requests`` even with transplanted cookies

Selenium/Chrome are optional; :func:`browser_available` reports readiness and
:class:`BrowserUnavailableError` is raised on use when they are missing.
"""

from __future__ import annotations

import time
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from requests.structures import CaseInsensitiveDict

from psweep.security import protected_mode

from .constants import USER_AGENT


class BrowserUnavailableError(RuntimeError):
    """Raised when Selenium or a usable Chrome/driver is not available."""


class BrowserDownloadError(RuntimeError):
    """An in-browser ``fetch()`` failed; ``status_code`` is None when it rejected."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BrowserNavigationError(RuntimeError):
    """Chrome showed its error page; the message carries the ``net::ERR_*``
    code so retry classification matches Selenium navigation errors."""


_CHROME_ERROR_CODE = re.compile(r"\bERR_[A-Z_]+\b")


def browser_available() -> bool:
    """Return True if Selenium is importable (Chrome resolved lazily)."""
    try:
        import selenium  # noqa: F401
    except ImportError:
        return False
    return True


def _resolve_chrome_service() -> Any:
    """Require a pre-provisioned driver; never download executable code at runtime."""
    from selenium.webdriver.chrome.service import Service

    driver = os.environ.get("PSWEEP_CHROMEDRIVER", "")
    if not driver or not Path(driver).is_file() or not os.access(driver, os.X_OK):
        raise BrowserUnavailableError("PSWEEP_CHROMEDRIVER must name an installed executable")
    return Service(driver)


class BrowserSession:
    """Context-managed headless Chrome session with anti-automation flags."""

    def __init__(
        self,
        *,
        headless: bool = True,
        request_headers: Mapping[str, str] | None = None,
        page_load_timeout: int = 45,
    ) -> None:
        # Chrome can only override the User-Agent; it follows the HTTP fetches.
        headers = CaseInsensitiveDict(request_headers or {})
        self.user_agent = headers.get("User-Agent") or USER_AGENT
        self._headless = headless
        self._page_load_timeout = page_load_timeout
        self._driver: Any = None

    def __enter__(self) -> BrowserSession:
        self._start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _start(self) -> None:
        if protected_mode() or os.environ.get("PSWEEP_BROWSER_ISOLATED") != "1":
            raise BrowserUnavailableError(
                "Browser discovery requires public mode and PSWEEP_BROWSER_ISOLATED=1 "
                "in an externally network-isolated sandbox; it is forbidden in protected mode"
            )
        if not browser_available():
            msg = (
                "Selenium is required for browser-based discovery. "
                "Install with: pip install selenium (and Google Chrome)."
            )
            raise BrowserUnavailableError(msg)

        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        opts = Options()
        if self._headless:
            opts.add_argument("--headless=new")
        for arg in (
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1400,1000",
            f"user-agent={self.user_agent}",
        ):
            opts.add_argument(arg)
        opts.add_experimental_option(
            "excludeSwitches", ["enable-automation"]
        )
        opts.add_experimental_option("useAutomationExtension", False)

        try:
            self._driver = webdriver.Chrome(
                service=_resolve_chrome_service(), options=opts
            )
        except Exception as exc:
            msg = f"Failed to start Chrome for browser discovery: {exc}"
            raise BrowserUnavailableError(msg) from exc

        # Hide the webdriver flag some bot managers check.
        try:
            self._driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": "Object.defineProperty(navigator,"
                    "'webdriver',{get:()=>undefined})"
                },
            )
        except Exception:  # noqa: BLE001 - non-fatal hardening
            pass
        self._driver.set_page_load_timeout(self._page_load_timeout)

    @property
    def current_url(self) -> str:
        """Return the observed browser location, never the requested URL."""
        return str(self._driver.current_url or "") if self._driver is not None else ""

    def fetch_html(self, url: str, *, settle_seconds: float = 2.0) -> str:
        """Navigate to *url* and return the rendered page source."""
        if self._driver is None:
            msg = "BrowserSession used outside its context manager."
            raise BrowserUnavailableError(msg)
        self._navigate(url)
        if settle_seconds > 0:
            time.sleep(settle_seconds)
        return self._driver.page_source or ""

    def current_links(self) -> list[dict[str, str]]:
        """Return ``[{url, text}]`` for every anchor on the current page."""
        if self._driver is None:
            return []
        links: list[dict[str, str]] = []
        for anchor in self._driver.find_elements("css selector", "a[href]"):
            try:
                href = anchor.get_attribute("href") or ""
                if href:
                    links.append(
                        {"url": href, "text": (anchor.text or "").strip()}
                    )
            except Exception:  # noqa: BLE001 - stale element, skip
                continue
        return links

    def download_bytes(
        self, url: str, *, timeout_seconds: int = 60
    ) -> tuple[bytes, str | None, str]:
        """Fetch *url* and return bytes, content type, and observed response URL.

        Runs a same-origin ``fetch()`` inside the page so the request uses the
        browser's real TLS fingerprint and session — the only reliable way past
        edge bot managers (Akamai) that block ``requests`` even with cookies.
        Navigates to the URL's origin first to satisfy same-origin policy.
        """
        import base64
        from urllib.parse import urlparse

        if self._driver is None:
            msg = "BrowserSession used outside its context manager."
            raise BrowserUnavailableError(msg)

        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}/"
        current = self._driver.current_url or ""
        if not current.startswith(f"{parsed.scheme}://{parsed.netloc}"):
            self._navigate(origin)
            time.sleep(2)

        self._driver.set_script_timeout(timeout_seconds + 5)
        script = """
            const url = arguments[0];
            const cb = arguments[arguments.length - 1];
            fetch(url, {credentials: 'include'})
              .then(r => r.arrayBuffer().then(buf => {
                  const bytes = new Uint8Array(buf);
                  let bin = '';
                  const chunk = 0x8000;
                  for (let i = 0; i < bytes.length; i += chunk) {
                      bin += String.fromCharCode.apply(
                          null, bytes.subarray(i, i + chunk));
                  }
                  cb(JSON.stringify({
                      ok: r.ok, status: r.status,
                      ct: r.headers.get('content-type'),
                      url: r.url,
                      data: btoa(bin)
                  }));
              }))
              .catch(e => cb(JSON.stringify({ok: false, error: String(e)})));
        """
        import json

        raw = self._driver.execute_async_script(script, url)
        payload = json.loads(raw) if raw else {}
        if not payload.get("ok"):
            status = payload.get("status")
            detail = payload.get("error") or f"HTTP {status}"
            msg = f"Browser download failed for {url}: {detail}"
            raise BrowserDownloadError(
                msg, status_code=int(status) if status else None
            )
        return (
            base64.b64decode(payload.get("data") or ""),
            payload.get("ct"),
            str(payload.get("url") or ""),
        )

    def _navigate(self, url: str) -> None:
        """Load *url*, raising when Chrome lands on its network error page."""
        self._driver.get(url)
        if str(self._driver.current_url or "").startswith("chrome-error://"):
            match = _CHROME_ERROR_CODE.search(self._driver.page_source or "")
            code = f"net::{match.group()}" if match else "chrome error page"
            raise BrowserNavigationError(f"Browser navigation failed for {url}: {code}")

    def close(self) -> None:
        """Quit the underlying browser driver and release its resources."""
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:  # noqa: BLE001
                pass
            self._driver = None
