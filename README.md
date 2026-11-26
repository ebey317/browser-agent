# browser-agent

An AI drives a real browser. You watch, take over, hand back. Local engines only — no cloud browser rentals, no pay-per-token API calls.

Muse-style UX ("open the browser in the chat → viewing window → take control"), built on Elijah's hardware.

## Layout

```
agent.py             snapshot → decide → act loop; takeover + guardrail gates
engines/
  __init__.py        CamofoxEngine (stealth, bot-walled sites) + SenseiEngine (real Chrome, logins)
  chrome_cdp.py      Chromium engine — the light default (direct CDP, auto-launches headless Chrome)
  _ws_cdp.py         minimal sync CDP-over-websocket client (AX reads, Input-domain actions; requires `websockets`)
watch/
  server.py          :8901 — live frames, /state, /start, /takeover, /release, /input (remote control; embeds the in-chat panel UI)
tests/canary_indeed.py
```

## The rules

- **AX-only reads.** The agent sees the accessibility-tree snapshot — never raw DOM, never in-page JS. Every snapshot is prefixed `[UNTRUSTED PAGE CONTENT …]`.
- **Takeover flag.** `/tmp/browser-agent-takeover` — while it exists the loop pauses before every action and the human's clicks/wheel/keys (via `/input`) pass into the real browser. Delete it (🤝) and the agent resumes from the current page.
- **Guardrails.** The engine classifies the REAL element (input type, autocomplete, AX role/name) via `DOM.describeNode` + AX tree — not words in the action text. Typing into an unidentified target (bare `bid:N`) is guarded fail-closed. Navigation to non-http(s) URLs (file:, chrome:, javascript:, data:) is guarded. Approved actions receive a single-use, action-bound grant (kind, target, value) with short TTL; grants are revoked on takeover.
- **Engine split.** Plain Chromium gets fingerprinted by Cloudflare-class walls in seconds (proven); Camofox defeats them (proven on Indeed). Default = Chromium, opt-in = Camofox.

## Run

```
# engines
python3 watch/server.py 8901            # watch + control server
curl -X POST localhost:8901/start       # demo task
# python
from engines import CamofoxEngine; from agent import BrowserAgent
BrowserAgent(CamofoxEngine(user_id="me"), decide_fn).run("https://…")
```

- Canaries: `python3 tests/canary_indeed.py` (Camofox) — returns first titles, `blocked: false`.
- Chrome engine needs a CDP listener on :9222 (`BA_AUTOCHROME=1` auto-launches one on a separate profile).

## Status

v1 — loop, takeover, guardrails, remote control, in-chat panel: all verified against live sites.
v2 — model-driven decide_fn (OpenRouter/Ollama), pytest suite, "Stop the task" button, Camofox memory tuning.
