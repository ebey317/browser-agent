"""Chrome/Chromium engine — direct CDP, no fork, no fork tax.

The lightweight default. Works for the 90% of sites that don't bot-wall.
Requires a Chrome/Chromium running with --remote-debugging-port=9222
(if CDP is down, set BA_AUTOCHROME=1 to let open() launch one headless).

Same AX-only contract as every other engine: the agent gets the
accessibility snapshot, never raw DOM, never in-page JS.

Implements get_current_tab() by returning the most recent non-blank,
non-chrome:// page target.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.request

CDP_BASE = os.environ.get("BA_CDP_BASE", "http://127.0.0.1:9222")
CHROME_BIN = os.environ.get("BA_CHROME_BIN", "chromium-browser")

_CHROME_FLAGS = [
    "--remote-debugging-port=9222",
    "--remote-allow-origins=http://127.0.0.1:9222",
    "--no-first-run", "--no-default-browser-check",
]


def _get(path: str, timeout: int = 30):
    return json.loads(urllib.request.urlopen(CDP_BASE + path, timeout=timeout).read())


def _post_json(path: str, body: dict, timeout: int = 30):
    req = urllib.request.Request(CDP_BASE + path, method="PUT",
                                 data=json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _cdp_alive() -> bool:
    try:
        _get("/json/version", timeout=3)
        return True
    except Exception:
        return False


def ensure_chrome(headless: bool = False) -> bool:
    """If CDP is down, launch Chrome/Chromium with the debug port."""
    if _cdp_alive():
        return True
    if not os.environ.get("BA_AUTOCHROME"):
        return False
    flags = list(_CHROME_FLAGS)
    flags.append("about:blank")  # headless chrome exits without a URL arg
    if headless:
        flags.insert(0, "--headless=new")
    # separate agent profile in a per-user dir (review: chrome-user-data-dir-in-tmp)
    import pathlib as _pl
    cache = _pl.Path(os.environ.get("XDG_CACHE_HOME", _pl.Path.home() / ".cache")) / "browser-agent"
    cache.mkdir(parents=True, exist_ok=True)
    try:
        cache.chmod(0o700)
    except OSError:
        pass
    flags.append(f"--user-data-dir={cache / 'chrome'}")
    tried = [CHROME_BIN, "google-chrome", "chromium", "chromium-browser"]
    for binary in dict.fromkeys(tried):
        try:
            subprocess.Popen([binary, *flags], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            continue
        for _ in range(20):
            if _cdp_alive():
                return True
            time.sleep(1)
    return False


class ChromeCdpEngine:
    name = "chrome-cdp"

    def __init__(self, base: str = CDP_BASE):
        self.base = base
        self._last_tab: str | None = None

    # -- tab management ---------------------------------------------------
    def _targets(self) -> list[dict]:
        return [t for t in _get("/json") if t.get("type") == "page"]

    def get_current_tab(self) -> str | None:
        targets = [t for t in self._targets()
                   if t.get("url", "") not in ("about:blank",) and not t.get("url", "").startswith("chrome://")]
        return targets[-1]["id"] if targets else None

    def open(self, url: str) -> str:
        ensure_chrome(headless=os.environ.get("SENSEI_CHROME_HEADLESS") == "1")
        req = urllib.request.Request(f"{self.base}/json/new?{url}", method="PUT")
        r = json.loads(urllib.request.urlopen(req, timeout=15).read())
        self._last_tab = r["id"]
        return r["id"]

    def close(self, tab_id: str) -> None:
        try:
            urllib.request.urlopen(f"{self.base}/json/close/{tab_id}", timeout=10)
        except Exception:
            pass

    # -- AX-only snapshot via CDP Accessibility domain ----------------------
    def snapshot(self, tab_id: str) -> str:
        ws = self._ws(tab_id)
        try:
            return ws.ax_snapshot()
        finally:
            ws.close()

    # -- websocket helper (stdlib-free: uses CDP HTTP where possible) ------
    #
    # Chrome's /json endpoints don't expose AX over HTTP, so this engine
    # needs one websocket. Kept dependency-free via a tiny frame client.
    # For v1 we cheat where the tooling already exists:
    def _ws(self, tab_id: str):
        from engines._ws_cdp import CdpPage  # local, no third-party deps
        return CdpPage(self.base, tab_id)

    def screenshot(self, tab_id: str) -> bytes | None:
        ws = self._ws(tab_id)
        try:
            b64 = ws.call("Page.captureScreenshot", {}).get("data")
            return base64.b64decode(b64) if b64 else None
        finally:
            ws.close()

    def navigate(self, tab_id: str, url: str) -> None:
        ws = self._ws(tab_id)
        try:
            ws.call("Page.navigate", {"url": url})
        finally:
            ws.close()

    def element_facts(self, tab_id: str, target: str) -> str | None:
        ws = self._ws(tab_id)
        try:
            return ws.element_facts(target)
        finally:
            ws.close()

    def act(self, tab_id: str, kind: str, target: str, value: str | None = None) -> dict:
        ws = self._ws(tab_id)
        try:
            return ws.act(kind, target, value or "")
        finally:
            ws.close()
