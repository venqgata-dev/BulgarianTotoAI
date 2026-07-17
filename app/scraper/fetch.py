"""Page fetchers.

Two strategies are provided (see package docstring for why):

* :class:`RequestsFetcher` — plain HTTP with retries and rate limiting. Works
  for the Internet Archive; against ``*.toto.bg`` it currently receives a
  Radware bot-management challenge, which is detected and reported as
  :class:`BotProtectionError` (never bypassed).
* :class:`ChromeCdpFetcher` — drives a locally running, visible Chrome via
  the DevTools protocol. Chrome executes the site's JavaScript like any
  normal browser session; if the protection ever escalates to a CAPTCHA the
  user must complete it manually in the Chrome window.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests

from app.config.settings import AppConfig
from app.services.logging_service import get_logger

# Radware injects its bootstrap script (__uzdbm_, stormcaster.js) into
# legitimate pages as well, so script markers cannot be used for detection.
# Actual challenge/CAPTCHA interstitials are identified by their page title
# ("Radware Page", "Radware Captcha Page").
_BOT_CHALLENGE_MARKERS = ("<title>Radware",)

_CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


class FetchError(Exception):
    """A page could not be fetched."""


class BotProtectionError(FetchError):
    """The response was a bot-management challenge instead of content."""


def looks_like_bot_challenge(html: str) -> bool:
    return any(marker in html for marker in _BOT_CHALLENGE_MARKERS)


class Fetcher(Protocol):
    """Anything that turns a URL into an HTML string."""

    def fetch(self, url: str) -> str: ...


class RateLimiter:
    """Enforces a minimum interval between consecutive requests."""

    def __init__(self, min_interval_seconds: float) -> None:
        self._interval = min_interval_seconds
        self._last_request = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last_request = time.monotonic()


class RequestsFetcher:
    """Plain HTTP fetcher with retry, backoff and rate limiting."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._log = get_logger("scraper.http")
        self._limiter = RateLimiter(config.rate_limit_seconds)
        self._session = requests.Session()
        self._session.headers["User-Agent"] = config.user_agent

    def fetch(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self._config.retry_count + 1):
            if attempt:
                delay = self._config.retry_backoff_seconds * (2 ** (attempt - 1))
                self._log.warning("Retry %d for %s in %.1fs (%s)", attempt, url, delay, last_error)
                time.sleep(delay)
            self._limiter.wait()
            try:
                response = self._session.get(url, timeout=self._config.request_timeout_seconds)
            except requests.RequestException as exc:
                last_error = exc
                continue
            if response.status_code in (429,) or response.status_code >= 500:
                last_error = FetchError(f"HTTP {response.status_code} for {url}")
                continue
            if response.status_code != 200:
                raise FetchError(f"HTTP {response.status_code} for {url}")
            if looks_like_bot_challenge(response.text):
                raise BotProtectionError(
                    f"Bot-management challenge served for {url}; a browser-based fetcher is required"
                )
            return response.text
        raise FetchError(f"Giving up on {url} after {self._config.retry_count + 1} attempts: {last_error}")


@dataclass(slots=True)
class _CdpTarget:
    ws_url: str


class ChromeCdpFetcher:
    """Fetches pages through a locally running Chrome (DevTools protocol).

    The Chrome window stays visible on purpose: the user can watch progress
    and, should the site ever present an interactive challenge, complete it
    themselves. This class only navigates and reads the DOM.
    """

    #: Extra seconds allowed for the site's JS challenge to auto-resolve.
    _CHALLENGE_GRACE_SECONDS = 20.0

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._log = get_logger("scraper.cdp")
        self._limiter = RateLimiter(config.rate_limit_seconds)
        self._ws = None  # lazy: websocket.WebSocket
        self._message_id = 0

    # -- connection management -------------------------------------------------

    def is_available(self) -> bool:
        """True when a debuggable Chrome is reachable."""
        try:
            requests.get(f"{self._config.chrome_debug_url}/json/version", timeout=3)
            return True
        except requests.RequestException:
            return False

    def ensure_chrome(self, profile_dir: Path) -> bool:
        """Start Chrome with remote debugging if it is not already running."""
        if self.is_available():
            return True
        port = self._config.chrome_debug_url.rsplit(":", 1)[-1]
        for candidate in _CHROME_CANDIDATES:
            if not candidate.exists():
                continue
            self._log.info("Starting Chrome for scraping (profile %s)", profile_dir)
            subprocess.Popen(  # noqa: S603 - fixed, known executable
                [
                    str(candidate),
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={profile_dir}",
                    "--no-first-run",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(30):
                time.sleep(1)
                if self.is_available():
                    return True
        return self.is_available()

    def _target(self) -> _CdpTarget:
        try:
            targets = requests.get(f"{self._config.chrome_debug_url}/json", timeout=5).json()
        except requests.RequestException as exc:
            raise FetchError(f"Chrome DevTools endpoint unreachable: {exc}") from exc
        for target in targets:
            if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                return _CdpTarget(ws_url=target["webSocketDebuggerUrl"])
        raise FetchError("No debuggable Chrome page tab found")

    def _connect(self):  # type: ignore[no-untyped-def]
        import websocket  # local import: optional dependency at runtime

        if self._ws is not None:
            return self._ws
        target = self._target()
        self._ws = websocket.create_connection(target.ws_url, timeout=60, suppress_origin=True)
        return self._ws

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            finally:
                self._ws = None

    def _command(self, method: str, **params: object) -> dict:
        ws = self._connect()
        self._message_id += 1
        ws.send(json.dumps({"id": self._message_id, "method": method, "params": params}))
        while True:
            message = json.loads(ws.recv())
            if message.get("id") == self._message_id:
                if "error" in message:
                    raise FetchError(f"CDP {method} failed: {message['error']}")
                return message.get("result", {})

    def _evaluate(self, expression: str) -> object:
        result = self._command("Runtime.evaluate", expression=expression, returnByValue=True)
        return result.get("result", {}).get("value")

    # -- fetching ---------------------------------------------------------------

    def fetch(self, url: str) -> str:
        self._limiter.wait()
        try:
            return self._fetch_once(url)
        except FetchError:
            # One reconnect attempt: the tab may have been closed or replaced.
            self.close()
            return self._fetch_once(url)

    def _fetch_once(self, url: str) -> str:
        self._command("Page.navigate", url=url)
        deadline = time.monotonic() + self._config.request_timeout_seconds
        challenge_deadline = deadline + self._CHALLENGE_GRACE_SECONDS
        html = ""
        while time.monotonic() < challenge_deadline:
            time.sleep(1.0)
            ready = self._evaluate("document.readyState")
            if ready != "complete":
                continue
            html = str(self._evaluate("document.documentElement.outerHTML") or "")
            if not looks_like_bot_challenge(html):
                return html
            # Challenge page: give it time to auto-resolve, then re-check.
            if time.monotonic() >= deadline:
                self._log.warning("Bot challenge still active for %s, waiting...", url)
        if looks_like_bot_challenge(html):
            raise BotProtectionError(
                f"Bot challenge did not clear for {url}. If Chrome shows a CAPTCHA, "
                "please complete it in the Chrome window and re-run the import."
            )
        raise FetchError(f"Timed out loading {url}")
