"""Human approval for guarded actions: what is waiting, and the human's answer.

The agent loop (agent.py) logs a `guard` event for a risky action, then blocks in
guard.approval_granted() until the approvals file says "approved" or "denied".
This module is the missing other half: it lists the requests still waiting and
records the human's decision, so the watch widget can show Approve / Deny.

Safety rules enforced here, not in the page:
  * only a request that is actually pending can be answered (no pre-approval);
  * no answer is accepted while the human has the wheel (takeover voids approvals);
  * the caller must be same-origin (Origin matches Host) or hold BA_RUN_TOKEN, so
    another web page cannot approve its own action through a cross-origin POST;
  * typed text aimed at a password / card field is never echoed back.
"""
from __future__ import annotations

import hmac
import json
import os
import threading
from urllib.parse import urlsplit

from engines import guard

DECISIONS = ("approved", "denied")
RESOLVED = ("approved", "denied-or-timeout", "void-takeover", "human-approved", "human-denied")
KEEP_DECISIONS = 50
_lock = threading.Lock()


def _masked(value, element) -> str | None:
    if value in (None, ""):
        return None
    if element and ("password" in element or "cc-" in element):
        return "•••••"
    return str(value)[:80]


def pending(events: list[dict]) -> list[dict]:
    """Guard requests with no outcome yet, oldest first."""
    asked: dict[str, dict] = {}
    for ev in events:
        if ev.get("kind") != "guard":
            continue
        d = ev.get("detail") or {}
        aid = d.get("action_id")
        if not aid:
            continue
        if d.get("status") in RESOLVED:
            asked.pop(aid, None)
        elif d.get("action"):
            asked[aid] = {"ts": ev.get("ts"), **d}
    out = []
    for aid, d in asked.items():
        a = d["action"]
        el = d.get("element")
        out.append({"action_id": aid, "step": d.get("step"), "ts": d.get("ts"),
                    "kind": a.get("kind"), "target": str(a.get("target") or "")[:120],
                    "value": _masked(a.get("value"), el), "element": el})
    return out


def authorized(headers) -> bool:
    """Same-origin browser call, or a caller holding BA_RUN_TOKEN."""
    token = os.environ.get("BA_RUN_TOKEN", "")
    supplied = headers.get("Authorization", "")
    if len(token) >= 32 and hmac.compare_digest(supplied, "Bearer " + token):
        return True
    origin, host = headers.get("Origin", ""), headers.get("Host", "")
    return bool(origin and host and urlsplit(origin).netloc == host)


def decide(action_id: str, decision: str, events: list[dict]) -> tuple[int, dict]:
    """Record the human's answer. Returns (http status, body)."""
    if decision not in DECISIONS:
        return 400, {"ok": False, "error": "decision must be approved or denied"}
    if guard.takeover_active():
        return 409, {"ok": False, "error": "release the wheel first; takeover voids approvals"}
    if action_id not in {p["action_id"] for p in pending(events)}:
        return 404, {"ok": False, "error": "no such pending request"}
    with _lock:
        try:
            data = json.loads(guard.APPROVAL_FILE.read_text())
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        data[action_id] = decision
        data = dict(list(data.items())[-KEEP_DECISIONS:])
        guard.atomic_json(guard.APPROVAL_FILE, data)
    return 200, {"ok": True, "action_id": action_id, "decision": decision}
