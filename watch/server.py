#!/usr/bin/env python3
"""Watch server — see the agent's browser live, take over, hand back.

    python3 watch/server.py            # serves http://localhost:8795

Endpoints:
    GET  /            the UI (live screenshot + status + buttons)
    GET  /frame       latest screenshot from the agent's tab (raw PNG)
    GET  /state       JSON: takeover status, events, snapshot tail
    POST /takeover    human grabs the wheel (agent pauses before next action)
    POST /release     human hands back
"""
from __future__ import annotations

import json
import urllib.request
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, "/home/elijah/projects/browser-agent")
from agent import BrowserAgent, TAKEOVER_FILE  # noqa: E402
from engines import CamofoxEngine  # noqa: E402
from engines.chrome_cdp import ChromeCdpEngine  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8795
STATE = {"events": [], "snapshot": "", "tab": None, "running": False, "task": ""}

ENGINE = ChromeCdpEngine()  # lightweight default; CamofoxEngine(user_id="watch") for bot-walled sites
BA_TAKEOVER = TAKEOVER_FILE


class AgentRunner(threading.Thread):
    """Runs a real agent task; publishes its events to STATE."""

    def __init__(self, task_url: str, decide_fn):
        super().__init__(daemon=True)
        self.task_url = task_url
        self.decide_fn = decide_fn

    def run(self):
        STATE["running"] = True
        agent = BrowserAgent(ENGINE, self.decide_fn, max_steps=20)
        import time as _t
        try:
            agent.run(self.task_url)
        finally:
            STATE["tab"] = getattr(agent.engine, "_last_tab", None) or STATE["tab"]
            STATE["events"] = [e.__dict__ for e in agent.events]
            STATE["running"] = False


_last_tab_cache = {"tab": None, "ts": 0.0}


def dispatch_input(engine, tab, b: dict) -> dict:
    """Human remote control: real Input-domain events in the real browser.

    Coords arrive in screenshot pixels; scale to CSS px via Page.getLayoutMetrics.
    Only enabled while takeover is active — the wheel belongs to the human.
    """
    if not TAKEOVER_FILE.exists():
        return {"skipped": "take takeover first (✊)"}
    from engines._ws_cdp import CdpPage

    kind = b.get("kind", "click")
    ws = _page_ws(ENGINE, tab)
    try:
        if kind in ("click", "move"):
            x, y = _to_css(ws, b)
            if kind == "move":
                return ws.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            for etype in ("mousePressed", "mouseReleased"):
                ws.call("Input.dispatchMouseEvent", {
                    "type": etype, "x": x, "y": y, "button": "left", "clickCount": 1,
                })
            return {"clicked": [x, y]}
        if kind == "wheel":
            x, y = _to_css(ws, b)
            return ws.call("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": x, "y": y,
                "deltaX": b.get("dx", 0), "deltaY": b.get("dy", 400),
            })
        if kind == "type":
            return ws.call("Input.insertText", {"text": b.get("text", "")})
        if kind == "key":
            key = b.get("key", "Escape")
            for etype in ("keyDown", "keyUp"):
                ws.call("Input.dispatchKeyEvent", {"type": etype, "key": key, "code": key})
            return {"pressed": key}
    finally:
        ws.close()
    return {"skipped": f"unknown kind {kind}"}


def _page_ws(engine, tab):
    from engines._ws_cdp import CdpPage
    return CdpPage(engine.base, tab)


def _to_css(ws, b):
    """Client sends natural screenshot pixels + the screenshot size; scale to CSS px."""
    shot_w = float(b.get("shotW") or 0)
    shot_h = float(b.get("shotH") or 0)
    x, y = float(b.get("x", 0)), float(b.get("y", 0))
    if shot_w and shot_h:
        m = ws.call("Page.getLayoutMetrics", {})
        v = m.get("cssVisualViewport") or m.get("contentSize") or {}
        cw, ch = v.get("width") or shot_w, v.get("height") or shot_h
        return x * cw / shot_w, y * ch / shot_h
    return x, y


def demo_decide(snapshot: str, history: list) -> dict | None:
    """Demo task: open Indeed, let it settle, pull job titles, finish.

    This is the scripted stand-in for the model-driven decide_fn (v2).
    """

    if not history:
        time.sleep(3)  # let the page paint
        return {"kind": "wait", "target": "", "value": "", "thought": "let the page settle"}
    if len(history) == 1:
        import re

        titles = list(dict.fromkeys(
            re.findall(r'link "([^"]+)" \[focusable\] \(bid=\d+\)', snapshot)
        ))[:6]
        STATE.setdefault("events", []).append(
            {"ts": time.time(), "kind": "result", "detail": {"first_titles": titles}}
        )
        return None
    return None


def current_tab() -> str | None:
    """Best-effort: the engine's current page target."""
    if getattr(ENGINE, "name", "") == "chrome-cdp":
        targets = [t for t in ENGINE._targets()
                   if t.get("url", "") not in ("about:blank",) and not t.get("url", "").startswith("chrome://")]
        return targets[-1]["id"] if targets else None
    with urllib.request.urlopen(
        f"{ENGINE.base}/tabs?userId={ENGINE.user}", timeout=15
    ) as r:
        tabs = json.loads(r.read()).get("tabs") or []
    return tabs[-1].get("id") if tabs else None


def _png_for(tab: str) -> bytes | None:
    return ENGINE.screenshot(tab)


UI = """<!doctype html><html><head><meta charset=utf-8><title>Browser Agent — Watch</title>
<style>
  body{margin:0;font-family:system-ui;background:#111;color:#eee;display:flex;flex-direction:column;height:100vh}
  header{display:flex;gap:12px;align-items:center;padding:8px 12px;background:#1a1a1a}
  h1{font-size:14px;margin:0;flex:1}
  button{padding:6px 14px;border:0;border-radius:6px;font-weight:600;cursor:pointer}
  #grab{background:#e5484d;color:#fff} #back{background:#46a758;color:#fff}
  #status{font-size:12px;padding:4px 12px;background:#000}
  #frame{flex:1;background:#000;display:flex;justify-content:center}
  #frame img{max-width:100%;max-height:100%;object-fit:contain}
  #events{height:110px;overflow:auto;font:11px monospace;background:#161616;padding:6px 10px;white-space:pre-wrap}
  .on #grab{opacity:.3;pointer-events:none}
</style></head><body>
<header><h1>Browser Agent — Camofox</h1>
<button id=grab>✊ TAKE OVER</button><button id=back>🤝 HAND BACK</button></header>
<div id=status>connecting…</div>
<div id=frame><img id=img alt="agent browser view"></div>
<div id=events></div>
<script>
const $=id=>document.getElementById(id);
$('grab').onclick=()=>fetch('/takeover',{method:'POST'});
$('back').onclick=()=>fetch('/release',{method:'POST'});
async function tick(){
  try{
    const st=await (await fetch('/state')).json();
    $('status').textContent = st.running? 'agent RUNNING'+(st.takeover?' — PAUSED: human has the wheel':' — autonomous') : 'agent idle'+(st.takeover?' — human has the wheel':'');
    document.body.classList.toggle('on', !!st.takeover);
    $('events').textContent = st.events.slice(-14).map(e=>e.ts&&new Date(e.ts*1000).toLocaleTimeString()+' '+e.kind+' '+JSON.stringify(e.detail).slice(0,120)).join('\\n');
    $('img').src='/frame?r='+Date.now();
  }catch(e){}
}
setInterval(tick,1500); tick();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/":
            self._send(200, UI.encode())
        elif self.path.startswith("/frame"):
            try:
                tab = STATE.get("tab") or current_tab()
                png = _png_for(tab) if tab else None
                if png:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(png)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(png)
                    return
            except Exception:
                pass
            self._send(200, b"no frame", "text/plain")
        elif self.path == "/state":
            st = {**STATE, "takeover": TAKEOVER_FILE.exists()}
            self._send(200, json.dumps(st).encode(), "application/json")
        else:
            self._send(404, b"nope", "text/plain")

    def do_POST(self):
        if self.path == "/start":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            url = body.get("url") or "https://news.ycombinator.com/"
            t = AgentRunner(url, demo_decide)
            t.start()
            STATE["task"] = url
            self._send(200, b'{"ok":true,"started":true}', "application/json")
        elif self.path == "/takeover":
            BrowserAgent.take_over()
            STATE.setdefault("events", []).append(
                {"ts": time.time(), "kind": "takeover", "detail": {"status": "human took over"}}
            )
            self._send(200, b'{"ok":true}', "application/json")
        elif self.path == "/input":
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            try:
                res = dispatch_input(ENGINE, STATE.get("tab") or current_tab(), b)
                self._send(200, json.dumps({"ok": True, **res}).encode(), "application/json")
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e)[:200]}).encode(), "application/json")
        elif self.path == "/release":
            BrowserAgent.release()
            STATE.setdefault("events", []).append(
                {"ts": time.time(), "kind": "takeover", "detail": {"status": "handed back to agent"}}
            )
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(404, b"nope", "text/plain")

    def log_message(self, *a):  # silence
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
