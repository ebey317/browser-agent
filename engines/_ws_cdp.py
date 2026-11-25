"""Minimal sync CDP-over-websocket client for the Chrome engine.

No third-party deps beyond the already-installed `websockets` lib.
The agent-facing rule: page reads come from the Accessibility tree;
clicks/types go through the Input domain at node coordinates —
the agent never gets Runtime.evaluate and never sees raw DOM.
"""
from __future__ import annotations

import json
import urllib.request

from websockets.sync.client import connect

MSG_TIMEOUT = 20


class CdpPage:
    def __init__(self, cdp_base: str, tab_id: str):
        ws_url = None
        for t in json.loads(urllib.request.urlopen(f"{cdp_base}/json", timeout=10).read()):
            if t.get("id") == tab_id and t.get("type") == "page":
                ws_url = t.get("webSocketDebuggerUrl")
                break
        if not ws_url:
            raise RuntimeError(f"tab {tab_id} not found or has no debugger URL")
        self.ws = connect(ws_url, open_timeout=10, close_timeout=5)
        self._id = 0

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv(timeout=MSG_TIMEOUT))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message', msg['error'])}")
                return msg.get("result", {})

    # ---- AX snapshot (the ONLY page read exposed to the agent) ----------
    def ax_snapshot(self, max_nodes: int = 400) -> str:
        tree = self.call("Accessibility.getFullAXTree", {}).get("nodes", [])
        lines = []
        for n in tree[:max_nodes]:
            role = (n.get("role", {}) or {}).get("value", "")
            name = (n.get("name", {}) or {}).get("value", "")
            if role in ("generic", "none", "InlineTextBox", "ignored") and not name:
                continue
            desc = f'{role} "{name}"' if name else role
            for prop in n.get("properties", []) or []:
                if prop.get("name") == "focusable" and prop.get("value", {}).get("value"):
                    desc += " [focusable]"
            lines.append(f"- {desc} (bid={n.get('backendDOMNodeId')})" if n.get("backendDOMNodeId") else f"- {desc}")
        return "\n".join(lines)

    # ---- actions (Input domain at AX-node coordinates) -------------------
    def _node_center(self, bid: int) -> tuple[float, float]:
        box = self.call("DOM.getBoxModel", {"backendNodeId": bid}).get("model", {})
        c = box.get("content") or box.get("border")
        if not c:
            raise RuntimeError(f"no box for node {bid}")
        xs, ys = c[0::2], c[1::2]
        return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2

    def _click_at(self, x: float, y: float) -> dict:
        for etype in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {
                "type": etype, "x": x, "y": y,
                "button": "left", "clickCount": 1,
            })
        return {"clicked": [x, y]}

    def _resolve_bid(self, target: str) -> int:
        """target: either a bid:<n> token from the snapshot or a quoted name."""
        if target.startswith("bid:"):
            return int(target[4:])
        tree = self.call("Accessibility.getFullAXTree", {}).get("nodes", [])
        t = target.strip('"').lower()
        for n in tree:
            nm = ((n.get("name", {}) or {}).get("value") or "").lower()
            if nm == t or t in nm:
                bid = n.get("backendDOMNodeId")
                if bid:
                    return bid
        raise RuntimeError(f"no AX node matching {target!r}")

    def act(self, kind: str, target: str, value: str) -> dict:
        if kind == "click":
            return self._click_at(*self._node_center(self._resolve_bid(target)))
        if kind == "type":
            bid = self._resolve_bid(target)
            self._click_at(*self._node_center(bid))
            if value:
                self.call("Input.insertText", {"text": value})
            return {"typed": value}
        if kind == "press":
            key = value or target or "Enter"
            for etype, code in (("keyDown", key), ("keyUp", key)):
                self.call("Input.dispatchKeyEvent", {"type": etype, "key": code, "code": code})
            return {"pressed": key}
        if kind == "wait":
            import time as _t
            _t.sleep(3)
            return {"waited": 3}
        if kind == "scroll":
            self.call("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": 400, "y": 400,
                "deltaX": 0, "deltaY": 600,
            })
            return {"scrolled": 600}
        raise ValueError(f"unsupported kind: {kind}")
