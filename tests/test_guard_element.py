"""Guard classifies the REAL element, not just words in the action text. No browser."""
import os
import sys
import tempfile

os.environ.setdefault("BA_RUNTIME_DIR", tempfile.mkdtemp(prefix="ba-guard-"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engines import guard  # noqa: E402
from engines import _ws_cdp  # noqa: E402


class FakePage:
    def __init__(self, facts):
        self.facts = facts

    def element_facts(self, target):
        return self.facts


def test_typing_into_unidentified_bid_is_guarded():
    assert guard.is_guarded({"kind": "type", "target": "bid:1", "value": "pw"})
    assert guard.is_guarded({"kind": "type", "target": "42", "value": "x"})


def test_password_input_caught_by_element_not_by_name():
    a = {"kind": "type", "target": "bid:7", "value": "hunter2"}
    assert guard.is_guarded(a, "input type=password id=f7 role=textbox name=Code")


def test_cc_and_password_autocomplete_caught():
    a = {"kind": "type", "target": "bid:9", "value": "4111"}
    assert guard.is_guarded(a, "input type=text autocomplete=cc-number")
    assert guard.is_guarded(a, "input type=text autocomplete=current-password")


def test_identified_search_box_is_not_guarded():
    a = {"kind": "type", "target": "bid:5", "value": "hvac jobs"}
    assert not guard.is_guarded(a, "input type=search aria-label=Search role=searchbox name=Search")


def test_button_named_place_order_caught_by_ax_name():
    a = {"kind": "click", "target": "bid:11"}
    assert guard.is_guarded(a, "button role=button name=Place order")
    assert not guard.is_guarded({"kind": "click", "target": "bid:12"}, "a role=link name=Next page")


def test_engine_gate_skips_password_field_it_resolved(monkeypatch):
    monkeypatch.setattr(guard, "takeover_active", lambda: False)
    r = _ws_cdp.page_act(FakePage("input type=password"), "type", "bid:3", "secret")
    assert r and r["skipped"] == "guarded"


def test_engine_gate_fails_closed_when_type_target_unresolved(monkeypatch):
    monkeypatch.setattr(guard, "takeover_active", lambda: False)
    r = _ws_cdp.page_act(FakePage(None), "type", "bid:3", "secret")
    assert r and r["skipped"] == "guarded"


def test_engine_gate_allows_identified_safe_click(monkeypatch):
    monkeypatch.setattr(guard, "takeover_active", lambda: False)
    assert _ws_cdp.page_act(FakePage("a role=link name=Next page"), "click", "bid:4", "") is None


def test_navigation_outside_http_is_guarded_and_http_is_not():
    for u in ("file:///etc/passwd", "chrome://settings", "javascript:void(0)", "data:text/html,x", ""):
        assert guard.is_guarded({"kind": "navigate", "value": u}), u
        assert guard.is_guarded({"kind": "navigate", "target": u}), u
    assert not guard.is_guarded({"kind": "navigate", "value": "https://example.com/jobs"})


def test_empty_element_facts_bypasses_opaque_target_guard(monkeypatch):
    """Regression: empty/whitespace element facts must not bypass opaque-target guard.
    element='' or element=' ' should be treated as unknown (None), so typing into
    a bare numeric/bid target is guarded.
    """
    a = {"kind": "type", "target": "bid:3", "value": "secret"}
    # Empty string
    assert guard.is_guarded(a, ""), "empty element facts should be guarded"
    # Whitespace only
    assert guard.is_guarded(a, " "), "whitespace element facts should be guarded"
    # None (already tested but explicit)
    assert guard.is_guarded(a, None), "None element facts should be guarded"
    # But with actual element facts, it should not be guarded by opaque-target rule
    assert not guard.is_guarded(a, "input type=search"), "identified safe element should not be guarded"
