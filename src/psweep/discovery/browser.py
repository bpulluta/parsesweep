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

import subprocess
import time
from typing import Any


class BrowserUnavailableError(RuntimeError):
    """Raised when Selenium or a usable Chrome/driver is not available."""


_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def browser_available() -> bool:
    """Return True if Selenium is importable (Chrome resolved lazily)."""
    try:
        import selenium  # noqa: F401
    except ImportError:
        return False
    return True


def _detect_chrome_version() -> str | None:
    """Return the installed Chrome major version, if detectable."""
    import re
    import shutil

    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for binary in candidates:
        if not binary:
            continue
        try:
            out = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            ).stdout
        except Exception:  # noqa: BLE001
            continue
        match = re.search(r"(\d+)\.\d+\.\d+", out or "")
        if match:
            return match.group(1)
    return None


def _resolve_chrome_service() -> Any:
    """Build a Chrome ``Service`` with a driver matching the installed Chrome.

    Selenium Manager normally auto-resolves the driver, but a stale
    ``chromedriver`` on PATH can shadow it with a mismatched version. We ask
    Selenium Manager for a driver matching the *detected* browser version and
    pin the Service to it; on any failure we fall back to default resolution.
    """
    import json

    from selenium.webdriver.chrome.service import Service

    try:
        from selenium.webdriver.common.selenium_manager import (
            SeleniumManager,
        )

        binary = SeleniumManager()._get_binary()  # noqa: SLF001
        cmd = [str(binary), "--browser", "chrome", "--output", "json"]
        version = _detect_chrome_version()
        if version:
            cmd += ["--browser-version", version]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, check=False
        )
        payload = json.loads(result.stdout or "{}").get("result", {})
        driver_path = payload.get("driver_path")
        if driver_path:
            return Service(driver_path)
    except Exception:  # noqa: BLE001 - fall back to default resolution
        pass
    return Service()


class BrowserSession:
    """Context-managed headless Chrome session with anti-automation flags."""

    def __init__(
        self,
        *,
        headless: bool = True,
        user_agent: str = _DEFAULT_UA,
        page_load_timeout: int = 45,
    ) -> None:
        self.user_agent = user_agent
        self._headless = headless
        self._page_load_timeout = page_load_timeout
        self._driver: Any = None

    def __enter__(self) -> BrowserSession:
        self._start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _start(self) -> None:
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
            "--no-sandbox",
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

    def fetch_html(self, url: str, *, settle_seconds: float = 2.0) -> str:
        """Navigate to *url* and return the rendered page source."""
        if self._driver is None:
            msg = "BrowserSession used outside its context manager."
            raise BrowserUnavailableError(msg)
        self._driver.get(url)
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
    ) -> tuple[bytes, str | None]:
        """Fetch *url* through the browser and return ``(bytes, content_type)``.

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
            self._driver.get(origin)
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
                      data: btoa(bin)
                  }));
              }))
              .catch(e => cb(JSON.stringify({ok: false, error: String(e)})));
        """
        import json

        raw = self._driver.execute_async_script(script, url)
        payload = json.loads(raw) if raw else {}
        if not payload.get("ok"):
            detail = payload.get("error") or f"HTTP {payload.get('status')}"
            msg = f"Browser download failed for {url}: {detail}"
            raise RuntimeError(msg)
        return base64.b64decode(payload.get("data") or ""), payload.get("ct")

    def close(self) -> None:
        """Quit the underlying browser driver and release its resources."""
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:  # noqa: BLE001
                pass
            self._driver = None
