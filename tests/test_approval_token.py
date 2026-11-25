"""An approved guarded action runs exactly once; nothing else gets through. No browser."""
import os
import sys
import tempfile

import pytest

os.environ.setdefault("BA_RUNTIME_DIR", tempfile.mkdtemp(prefix="ba-appr-"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402
from engines import _ws_cdp, guard  # noqa: E402

PW = {"kind": "type", "target": "bid:3", "value": "secret"}


class Page:
    def __init__(self, facts="input type=password"):
        self.facts = facts

    def element_facts(self, target):
        return self.facts


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    guard.revoke_grants()
    monkeypatch.setattr(guard, "TAKEOVER_FILE", tmp_path / "takeover")
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    yield
    guard.revoke_grants()


def gate(action=PW, facts="input type=password"):
    return _ws_cdp.page_act(Page(facts), action["kind"], action["target"], action["value"])


def test_without_grant_guarded_action_is_skipped():
    assert gate()["skipped"] == "guarded"


def test_grant_lets_exactly_that_action_through_once():
    guard.grant(PW)
    assert gate() is None                       # approved: proceeds
    assert gate()["skipped"] == "guarded"       # single use: second time is blocked


def test_grant_is_bound_to_the_exact_action():
    guard.grant(PW)
    assert gate({"kind": "type", "target": "bid:3", "value": "other"})["skipped"] == "guarded"
    assert gate({"kind": "type", "target": "bid:4", "value": "secret"})["skipped"] == "guarded"
    assert gate({"kind": "click", "target": "bid:3", "value": ""})["skipped"] == "guarded"
    assert gate() is None                        # the real one still works: wrong tries did not burn it


def test_grant_expires(monkeypatch):
    guard.grant(PW)
    t = guard.time.time()
    monkeypatch.setattr(guard.time, "time", lambda: t + guard.GRANT_TTL_S + 1)
    assert gate()["skipped"] == "guarded"


def test_takeover_revokes_grants_and_blocks_even_a_granted_action(monkeypatch):
    guard.grant(PW)
    guard.take_over()
    guard.release()
    assert gate()["skipped"] == "guarded"
    guard.grant(PW)
    guard.take_over()                            # human holds the wheel
    assert gate()["skipped"] == "takeover"
    guard.release()


def test_none_and_empty_value_are_the_same_action():
    guard.grant({"kind": "click", "target": "bid:9", "value": None})
    assert _ws_cdp.page_act(Page("button name=Place order"), "click", "bid:9", "") is None


class Engine:
    """Real engine gate, fake browser."""
    def __init__(self, facts="input type=password"):
        self.page, self.performed = Page(facts), []

    def open(self, url):
        return "t1"

    def snapshot(self, tab):
        return "- textbox (bid=3)"

    def element_facts(self, tab, target):
        return self.page.facts

    def act(self, tab, kind, target, value=None):
        r = _ws_cdp.page_act(self.page, kind, target, value or "")
        if r is not None:
            return r
        self.performed.append((kind, target, value))
        return {"ok": True}


def run_loop(monkeypatch, approved, facts="input type=password"):
    steps = iter([dict(PW), None])
    monkeypatch.setattr(agent, "_approval_granted", lambda aid, timeout=300.0: approved)
    eng = Engine(facts)
    agent.BrowserAgent(eng, lambda s, h: next(steps), max_steps=3).run("https://x.test")
    return eng


def test_loop_approved_password_step_actually_runs_once(monkeypatch):
    eng = run_loop(monkeypatch, approved=True)
    assert eng.performed == [("type", "bid:3", "secret")]


def test_loop_denied_step_never_reaches_the_browser(monkeypatch):
    assert run_loop(monkeypatch, approved=False).performed == []


def test_loop_takeover_during_approval_wait_voids_the_approval(monkeypatch):
    calls = {"n": 0}

    def takeover_after_first_check():
        calls["n"] += 1
        return calls["n"] > 1                    # first loop-top check false, then human grabs wheel
    monkeypatch.setattr(agent, "_takeover_active", takeover_after_first_check)
    monkeypatch.setattr(agent, "_wait_for_release", lambda poll=1.0: None)
    eng = run_loop(monkeypatch, approved=True)
    assert eng.performed == []
