#!/usr/bin/env python3
"""Watch server — see the agent's browser live, take over, hand back.

    python3 watch/server.py            # serves http://localhost:8901

POST /start requires Authorization: Bearer <BA_RUN_TOKEN> (at least 32 chars)
and JSON {"task": "user's goal", "url": "https://..."}. It runs the optional
BA_LLM_* model adapter, NOT the existing Hermes conversational model. Missing
token configuration disables /start. Legacy viewing/control endpoints still
need the separate security integration; do not expose this service publicly.

Endpoints:
    GET  /            the UI (live screenshot + status + buttons)
    GET  /frame       latest screenshot from the agent's tab (raw PNG)
    GET  /state       JSON: takeover status, events, snapshot tail
    POST /takeover    human grabs the wheel (agent pauses before next action)
    POST /release     human hands back
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import TAKEOVER_FILE, BrowserAgent  # noqa: E402
from decide_llm import make_decide_fn  # noqa: E402
from engines import (
    CamofoxEngine,  # noqa: E402
    guard,  # noqa: E402
)
from engines.chrome_cdp import ChromeCdpEngine  # noqa: E402
import approvals  # noqa: E402  (watch/approvals.py: pending guard requests + human decision)

PORT = int(os.environ.get("BA_WATCH_PORT", "8901"))
STATE = {"events": [], "snapshot": "", "tab": None, "running": False,
         "task": "", "status": "idle", "error": None}
STATE_LOCK = threading.RLock()

ENGINE = ChromeCdpEngine()
CAMO = CamofoxEngine(user_id="watch")
STATE.setdefault("camofox_tab", None)  # lightweight default; CamofoxEngine(user_id="watch") for bot-walled sites
BA_TAKEOVER = TAKEOVER_FILE


class LiveAgent(BrowserAgent):
    """Publish task identity and events before asking the model for an action."""

    def log(self, kind, **detail):
        event = super().log(kind, **detail)
        with STATE_LOCK:
            if kind == "nav":
                STATE["tab"] = detail.get("tab")
            STATE.setdefault("events", []).append(event.__dict__)
        return event


class AgentRunner(threading.Thread):
    """One explicitly started task. The captured engine owns its tab throughout."""

    def __init__(self, task_url: str, decide_fn):
        super().__init__(daemon=True)
        self.task_url = task_url
        self.decide_fn = decide_fn
        self.engine = ENGINE

    def run(self):
        with STATE_LOCK:
            STATE.update(running=True, status="running", error=None)
        agent = None
        try:
            agent = LiveAgent(self.engine, self.decide_fn, max_steps=20)
            result = agent.run(self.task_url)
            status = "complete" if result.get("ok") else (
                "stopped" if result.get("reason") == "stopped" else "failed")
            with STATE_LOCK:
                STATE["status"] = status
                STATE["error"] = None if status != "failed" else result.get("reason", "task_failed")
            if status != "complete":
                agent.log(status, reason=STATE["error"])
        except Exception as exc:
            # No raw provider/page exception text: it can contain secrets or URLs.
            with STATE_LOCK:
                STATE.update(status="failed", error=type(exc).__name__)
            if agent is not None:
                agent.log("failed", error=type(exc).__name__)
            else:
                with STATE_LOCK:
                    STATE["events"].append({"ts": time.time(), "kind": "failed",
                                            "detail": {"error": type(exc).__name__}})
        finally:
            with STATE_LOCK:
                STATE["running"] = False


def start_task(body):
    """Reserve the single task slot before starting a thread; never queue secretly."""
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "expected_object"}
    task, url = body.get("task"), body.get("url")
    if not isinstance(task, str) or not task.strip() or len(task) > 8000:
        return 400, {"ok": False, "error": "task_required_max_8000_chars"}
    if not isinstance(url, str) or len(url) > 8192:
        return 400, {"ok": False, "error": "http_url_required"}
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("invalid URL")
    except ValueError:
        return 400, {"ok": False, "error": "http_url_required"}
    with STATE_LOCK:
        if STATE["running"]:
            return 409, {"ok": False, "error": "task_already_running"}
        try:
            decide = make_decide_fn(task.strip())
            runner = AgentRunner(url, decide)
            STATE.update(task=task.strip(), tab=None, events=[], running=True,
                         status="starting", error=None)
            runner.start()
        except Exception as exc:
            STATE.update(running=False, status="failed", error=type(exc).__name__)
            return 500, {"ok": False, "error": "task_start_failed"}
    return 202, {"ok": True, "started": True}


_last_tab_cache = {"tab": None, "ts": 0.0}


def dispatch_input(engine, tab, b: dict) -> dict:
    """Human remote control: real Input-domain events in the real browser.

    Coords arrive in screenshot pixels; scale to CSS px via Page.getLayoutMetrics.
    Only enabled while takeover is active — the wheel belongs to the human.
    """
    if not guard.takeover_active():
        return {"skipped": "take takeover first (✊)"}

    kind = b.get("kind", "click")
    ws = _page_ws(engine, tab)
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
        self.path = self.path.split("?", 1)[0]
        if self.path == "/":
            self._send(200, UI.encode())
        elif self.path == "/widget":
            html = (Path(__file__).parent / "thread_widget.html").read_text()
            self._send(200, html.encode(), "text/html")
        elif self.path == "/camframe":
            tab = STATE.get("camofox_tab")
            try:
                png = CAMO.screenshot(tab) if tab else None
                if png:
                    STATE["last_frame"] = png
                    STATE["last_frame_ts"] = time.time()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(png)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(png)
                    return
            except Exception:
                pass
            # engine dead: stream the last good frame and flag it, so the human
            # sees a frozen page instead of a silent dark box
            stale = STATE.get("last_frame")
            if stale:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(stale)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Frame-Stale", "1")
                self.end_headers()
                self.wfile.write(stale)
                return
            self._send(200, b"no camofox frame", "text/plain")
        elif self.path.startswith("/frame"):
            try:
                tab = STATE.get("tab")
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
        elif self.path == "/approvals":
            with STATE_LOCK:
                events = list(STATE.get("events", []))
            body = {"pending": approvals.pending(events), "takeover": guard.takeover_active()}
            self._send(200, json.dumps(body).encode(), "application/json")
        elif self.path == "/state":
            with STATE_LOCK:
                st = {k: v for k, v in STATE.items() if not k.startswith("last_frame")}
                st = {**st, "events": list(STATE["events"]), "takeover": guard.takeover_active()}
            self._send(200, json.dumps(st).encode(), "application/json")
        else:
            self._send(404, b"nope", "text/plain")

    def do_POST(self):
        self.path = self.path.split("?", 1)[0]
        if self.path == "/open":
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            tab = CAMO.open(b.get("url") or "https://www.reddit.com/")
            STATE["camofox_tab"] = tab
            STATE.setdefault("events", []).append({"ts": time.time(), "kind": "nav", "detail": {"engine": "camofox", "url": b.get("url")}})
            self._send(200, json.dumps({"ok": True, "tab": tab}).encode(), "application/json")
        elif self.path == "/caminput":
            # Human input into the CAMOFOX browser. Gated on takeover (same guard).
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            STATE.setdefault("events", []).append({
                "ts": time.time(), "kind": "input",
                "detail": {"body": b.get("kind"), "gate": "open" if guard.takeover_active() else "closed"}})
            if len(STATE.get("events", [])) > 100: del STATE["events"][:50]
            if not guard.takeover_active():
                self._send(200, b'{"ok":false,"skipped":"takeover"}', "application/json")
                return
            tab = STATE.get("camofox_tab")
            kind = b.get("kind")
            # auto-recover: if the camofox tab is dead (410/404), relaunch + reopen the last URL
            def _tab_alive():
                try:
                    with urllib.request.urlopen(f"{CAMO.base}/tabs/{tab}/evaluate", timeout=15) as r:
                        return json.loads(r.read()).get("ok") is True
                except Exception:
                    return False
            if kind in ("click", "type", "key", "wheel") and tab and not _tab_alive():
                last = ""
                for e in reversed(STATE.get("events", [])):
                    if e.get("kind") == "nav":
                        last = e["detail"].get("url", ""); break
                if not last: last = "https://www.google.com/"
                try:
                    STATE["camofox_tab"] = CAMO.open(last)
                    STATE.setdefault("events", []).append({"ts": time.time(), "kind": "nav", "detail": {"engine": "camofox", "url": last, "auto": "crash-recovery"}})
                    tab = STATE["camofox_tab"]
                except Exception:
                    pass
            def cam(path, extra):
                body = {"userId": CAMO.user, **extra}
                with urllib.request.urlopen(urllib.request.Request(
                        f"{CAMO.base}/tabs/{tab}/{path}",
                        data=json.dumps(body).encode(),
                        headers={"Content-Type": "application/json"}), timeout=30) as r:
                    return json.loads(r.read())
            try:
                if kind == "click":
                    # shot pixels -> CSS px. Camofox screenshots are 1:1 with the
                    # CSS viewport (DPR 1, verified via evaluate) — pass through.
                    # If a client supplies cssW, scale by the real ratio.
                    sw, sh = float(b.get("shotW") or 0), float(b.get("shotH") or 0)
                    css_w = float(b.get("cssW") or 0)
                    css_h = float(b.get("cssH") or 0)
                    x, y = float(b.get("x", 0)), float(b.get("y", 0))
                    if sw and css_w:
                        x = x * css_w / sw
                        y = y * (css_h or css_w * sh / sw) / sh
                    res = cam("click", {"coordinates": {"x": x, "y": y}})
                elif kind == "wheel":
                    res = cam("scroll", {"dy": int(b.get("dy", 0))})
                elif kind == "type":
                    # keyboard mode types into current focus (no ref needed)
                    res = cam("type", {"text": b.get("text", ""), "mode": "keyboard"})
                elif kind == "key":
                    res = cam("press", {"key": b.get("key", "")})
                else:
                    res = {"error": f"kind {kind} unsupported"}
                self._send(200, json.dumps({"ok": True, "res": res}).encode(), "application/json")
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e)[:200]}).encode(), "application/json")
        elif self.path == "/start":
            # Never expose new model-driven work through the legacy unauthenticated UI.
            import hmac
            token = os.environ.get("BA_RUN_TOKEN", "")
            if len(token) < 32:
                self._send(503, b'{"ok":false,"error":"configure_BA_RUN_TOKEN_min_32_chars"}', "application/json")
                return
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, "Bearer " + token):
                self._send(403, b'{"ok":false,"error":"unauthorized"}', "application/json")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    raise ValueError("bad body length")
                body = json.loads(self.rfile.read(size))
            except (ValueError, UnicodeError):
                self._send(400, b'{"ok":false,"error":"invalid_body"}', "application/json")
                return
            code, result = start_task(body)
            self._send(code, json.dumps(result).encode(), "application/json")
        elif self.path == "/approve":
            if not approvals.authorized(self.headers):
                self._send(403, b'{"ok":false,"error":"unauthorized"}', "application/json")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ValueError("bad body length")
                b = json.loads(self.rfile.read(size))
                aid, decision = str(b["action_id"]), str(b["decision"])
            except (ValueError, KeyError, TypeError, UnicodeError):
                self._send(400, b'{"ok":false,"error":"invalid_body"}', "application/json")
                return
            with STATE_LOCK:
                events = list(STATE.get("events", []))
            code, result = approvals.decide(aid, decision, events)
            if code == 200:
                with STATE_LOCK:
                    STATE.setdefault("events", []).append({"ts": time.time(), "kind": "guard",
                        "detail": {"action_id": aid, "status": "human-" + decision}})
            self._send(code, json.dumps(result).encode(), "application/json")
        elif self.path == "/takeover":
            BrowserAgent.take_over()
            STATE.setdefault("events", []).append(
                {"ts": time.time(), "kind": "takeover", "detail": {"status": "human took over"}}
            )
            self._send(200, b'{"ok":true}', "application/json")
        elif self.path == "/input":
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            try:
                tab = STATE.get("tab")
                if not tab:
                    self._send(409, b'{"ok":false,"error":"no_task_tab"}', "application/json")
                    return
                res = dispatch_input(ENGINE, tab, b)
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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
