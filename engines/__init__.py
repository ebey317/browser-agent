"""Engine adapters — how the agent talks to a real browser.

Every engine implements the same small contract:

    open(url) -> tab_id
    snapshot(tab_id) -> str   (accessibility-tree text; this is ALL the agent sees)
    screenshot(tab_id) -> bytes | None
    act(tab_id, kind, target, value) -> dict
    close(tab_id) -> None

Rule from the Sentinel design: engines expose NO raw DOM and NO in-page JS
to the agent. Click/type/nav only, judged against the AX snapshot.
"""

from __future__ import annotations

import json
import os
import urllib.request

CAMOFOX_BASE = os.environ.get("CAMOFOX_URL", "http://localhost:9377")
SENSEI_BASE = os.environ.get("SENSEI_BRIDGE_URL", "http://127.0.0.1:8791")


def _post(url: str, body: dict | None = None, timeout: int = 120):
    req = urllib.request.Request(
        url,
        method="POST" if body is not None else "GET",
        data=json.dumps(body).encode() if body is not None else None,
    )
    if body is not None:
        req.add_header("Content-Type", "application/json")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _get(url: str, timeout: int = 120):
    return json.loads(urllib.request.urlopen(url, timeout=timeout).read())


class CamofoxEngine:
    """Local anti-detection browser (Camoufox via the camofox-browser REST API).

    Anonymous work on bot-walled sites. Requires `camofox-browser` running
    (see the anti-detect-browser-automation skill).
    """

    name = "camofox"

    def __init__(self, user_id: str = "browser-agent", base: str = CAMOFOX_BASE):
        self.base = base.rstrip("/")
        self.user = user_id

    def _t(self, tab_id: str, path: str) -> str:
        return f"{self.base}/tabs/{tab_id}{path}?userId={self.user}"

    def get_current_tab(self) -> str | None:
        try:
            with urllib.request.urlopen(
                f"{self.base}/tabs?userId={self.user}", timeout=15
            ) as r:
                tabs = json.loads(r.read()).get("tabs") or []
            return tabs[-1].get("id") if tabs else None
        except Exception:
            return None

    def open(self, url: str) -> str:
        r = _post(f"{self.base}/tabs/open", {"userId": self.user, "url": url})
        return r["tabId"]

    def navigate(self, tab_id: str, url: str) -> None:
        _post(f"{self.base}/tabs/{tab_id}/navigate", {"userId": self.user, "url": url})

    def snapshot(self, tab_id: str) -> str:
        return _get(self._t(tab_id, "/snapshot"))["snapshot"]

    def screenshot(self, tab_id: str) -> bytes | None:
        with urllib.request.urlopen(self._t(tab_id, "/screenshot"), timeout=60) as r:
            return r.read()

    def act(self, tab_id: str, kind: str, target: str, value: str | None = None) -> dict:
        body: dict = {"userId": self.user}
        if kind == "click":
            return _post(f"{self.base}/tabs/{tab_id}/click", body)
        if kind == "type":
            return _post(f"{self.base}/tabs/{tab_id}/type", {**body, "text": value or ""})
        if kind == "press":
            return _post(f"{self.base}/tabs/{tab_id}/press", {**body, "key": target})
        if kind == "scroll":
            return _post(f"{self.base}/tabs/{tab_id}/scroll", body)
        if kind == "wait":
            return _post(f"{self.base}/tabs/{tab_id}/wait", {**body, "timeout": 5000})
        raise ValueError(f"unsupported action kind: {kind}")

    def close(self, tab_id: str) -> None:
        req = urllib.request.Request(
            f"{self.base}/tabs/{tab_id}?userId={self.user}", method="DELETE"
        )
        urllib.request.urlopen(req, timeout=30)


class SenseiEngine:
    """The user's REAL Chrome with REAL logins, via the Sensei bridge (:8791).

    Logged-in work only. Requires Chrome running with --remote-debugging-port=9222
    and the Sensei bridge up.
    """

    name = "sensei"

    def __init__(self, base: str = SENSEI_BASE):
        self.base = base.rstrip("/")

    def get_current_tab(self) -> str | None:
        try:
            r = _get(f"{self.base}/agent/state")
            return r.get("tab_id") or r.get("page_id") or "current"
        except Exception:
            return None

    def open(self, url: str) -> str:
        r = _post(f"{self.base}/agent/navigate", {"url": url})
        return r.get("tab_id") or r.get("page_id") or "current"

    def navigate(self, tab_id: str, url: str) -> None:
        _post(f"{self.base}/agent/navigate", {"url": url})

    def snapshot(self, tab_id: str) -> str:
        r = _get(f"{self.base}/agent/state")
        return r.get("page_text") or json.dumps(r)[:20000]

    def screenshot(self, tab_id: str) -> bytes | None:
        return None  # v1: camofox is the watchable engine

    def act(self, tab_id: str, kind: str, target: str, value: str | None = None) -> dict:
        return _post(f"{self.base}/agent/run", {"goal": "", "action": {"kind": kind, "target": target, "value": value}})

    def close(self, tab_id: str) -> None:
        pass
