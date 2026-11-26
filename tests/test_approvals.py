"""Approval bar backend: behaviour of watch/approvals.py with the real guard wait."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "watch"))

from engines import guard  # noqa: E402
import approvals  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "APPROVAL_FILE", tmp_path / "approvals")
    monkeypatch.setattr(guard, "takeover_active", lambda: False)


def ask(aid, kind="click", target="bid:7", value=None, element=None, step=0):
    return {"ts": time.time(), "kind": "guard", "detail": {
        "action": {"kind": kind, "target": target, "value": value},
        "element": element, "action_id": aid, "step": step}}


def test_pending_lists_unanswered_and_drops_resolved():
    ev = [ask("1-a"), ask("2-b"),
          {"ts": 1, "kind": "guard", "detail": {"action_id": "1-a", "status": "approved"}}]
    assert [p["action_id"] for p in approvals.pending(ev)] == ["2-b"]


def test_timeout_removes_request_so_a_late_click_cannot_answer_it():
    ev = [ask("1-a"), {"ts": 1, "kind": "guard",
                       "detail": {"action_id": "1-a", "status": "denied-or-timeout"}}]
    assert approvals.pending(ev) == []
    assert approvals.decide("1-a", "approved", ev)[0] == 404


def test_typed_text_into_password_field_is_masked():
    ev = [ask("1-a", kind="type", target="bid:3", value="hunter2",
              element="input type=password")]
    shown = approvals.pending(ev)[0]
    assert shown["value"] == "•••••" and "hunter2" not in str(shown)


def test_human_approval_unblocks_the_waiting_agent():
    """The original bug: the agent waited its full timeout because nothing wrote the answer."""
    ev = [ask("5-x")]
    got = {}
    t = threading.Thread(target=lambda: got.update(ok=guard.approval_granted("5-x", timeout=6)))
    t.start()
    time.sleep(0.3)
    assert approvals.decide("5-x", "approved", ev)[0] == 200
    t.join(8)
    assert got["ok"] is True


def test_denial_returns_false_to_the_agent():
    ev = [ask("5-x")]
    assert approvals.decide("5-x", "denied", ev)[0] == 200
    assert guard.approval_granted("5-x", timeout=3) is False


def test_cannot_pre_approve_an_unknown_action():
    code, _ = approvals.decide("9-zzz", "approved", [])
    assert code == 404 and not guard.APPROVAL_FILE.exists()


def test_bad_decision_rejected():
    assert approvals.decide("1-a", "yes", [ask("1-a")])[0] == 400


def test_no_answer_accepted_while_human_has_the_wheel(monkeypatch):
    monkeypatch.setattr(guard, "takeover_active", lambda: True)
    assert approvals.decide("1-a", "approved", [ask("1-a")])[0] == 409
    assert not guard.APPROVAL_FILE.exists()


def test_decisions_file_is_bounded():
    for i in range(80):
        approvals.decide(f"{i}-t", "approved", [ask(f"{i}-t")])
    assert len(json.loads(guard.APPROVAL_FILE.read_text())) == approvals.KEEP_DECISIONS


@pytest.mark.parametrize("headers,ok", [
    ({"Origin": "http://localhost:8901", "Host": "localhost:8901"}, True),
    ({"Origin": "https://evil.example", "Host": "localhost:8901"}, False),
    ({"Host": "localhost:8901"}, False),   # no Origin: not a browser same-origin call
    ({}, False),
])
def test_same_origin_check(headers, ok, monkeypatch):
    monkeypatch.delenv("BA_RUN_TOKEN", raising=False)
    assert approvals.authorized(headers) is ok


def test_token_holder_is_authorized(monkeypatch):
    monkeypatch.setenv("BA_RUN_TOKEN", "t" * 40)
    assert approvals.authorized({"Authorization": "Bearer " + "t" * 40}) is True
    assert approvals.authorized({"Authorization": "Bearer wrong"}) is False
