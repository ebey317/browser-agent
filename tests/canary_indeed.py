#!/usr/bin/env python3
"""Canary: prove the full agent loop against live Camofox.

Task: open Indeed (bot-walled), read the results page via AX snapshot,
report the first three job titles. Decision logic is scripted here so the
canary tests the LOOP (snapshot -> decide -> act -> finish), not a model.

Run:  python3 tests/canary_indeed.py
"""
import json
import re
import sys
import time
import urllib.request

sys.path.insert(0, "/home/elijah/projects/browser-agent")
from engines import CamofoxEngine  # noqa: E402
from agent import BrowserAgent, UNTRUSTED_BANNER  # noqa: E402

ENGINE = CamofoxEngine(user_id="canary")
TITLES = re.compile(r'heading "full details of ([^"]+)"')


def decide(snapshot: str, history: list) -> dict | None:
    """Step 1: read the page. Step 2: we're done — pull titles."""
    if any(h.get("action", {}).get("kind") == "wait" for h in history):
        return None  # second pass: finish
    return {"kind": "wait", "target": "", "value": "", "thought": "let the page settle"}


def main() -> int:
    a = BrowserAgent(ENGINE, decide, max_steps=4)
    t0 = time.time()
    out = a.run("https://www.indeed.com/jobs?q=AI+workflow+engineer&l=Indianapolis%2C+IN&fromage=14")

    snap = ENGINE.snapshot(out["tab"])
    titles = list(dict.fromkeys(TITLES.findall(snap)))[:3]

    events = out["events"]
    kinds = [e["kind"] for e in events]
    ok = out["ok"] and "nav" in kinds and "done" in kinds and titles

    print(json.dumps({
        "ok": ok,
        "elapsed_s": round(time.time() - t0, 1),
        "first_titles": titles,
        "event_kinds": kinds,
        "blocked": "Blocked" in snap[:500],
    }, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
