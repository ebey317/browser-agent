"""pytest for the guard layer — Jev's definition of ready: each invariant
gets a test ID. (I1/I2 covered by engine contract; here: I3 redaction,
I4 approval queue, I5 input suppression, I6 fail-closed, H4 sticky flag.)
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engines import guard  # noqa: E402


def test_takeover_stale_resolves_paused(monkeypatch, tmp_path):  # H4
    f = tmp_path / "takeover"
    f.write_text("human\n0\n")  # epoch mtime = ancient
    monkeypatch.setattr(guard, "TAKEOVER_FILE", f)
    assert guard.takeover_active() is True  # stale = fail closed
    guard.release()
    assert guard.TAKEOVER_FILE.exists() is False


def test_takeover_fresh(monkeypatch, tmp_path):  # H4 fresh case
    f = tmp_path / "takeover"
    f.write_text("human\n%d\n" % time.time())
    monkeypatch.setattr(guard, "TAKEOVER_FILE", f)
    assert guard.takeover_active() is True
    guard.release()
    assert guard.takeover_active() is False


def test_input_suppression_at_engine_layer(monkeypatch, tmp_path):  # I5
    # engine-side: while takeover is active, act() must refuse
    from engines import _ws_cdp
    monkeypatch.setattr(guard, "TAKEOVER_FILE", tmp_path / "t")
    guard.take_over()
    try:
        page = _ws_cdp.CdpPage.__new__(_ws_cdp.CdpPage)  # no network needed
        r = _ws_cdp.page_act(page, "click", "x", "")
        assert r.get("skipped") == "takeover"
    finally:
        guard.release()


def test_guard_patterns():  # I4 classification
    assert guard.is_guarded({"kind": "type", "target": "bid:1", "value": "pw"})
    assert guard.is_guarded({"kind": "click", "target": "checkout button"})
    assert not guard.is_guarded({"kind": "click", "target": "next page"})


def test_approval_file_atomic_write(tmp_path):  # approval-file race
    p = tmp_path / "approvals"
    guard.atomic_json(p, {"1-1": "approved"})
    assert json.loads(p.read_text())["1-1"] == "approved"
    # no .tmp residue
    assert not (tmp_path / "approvals.tmp").exists()


def test_redact_password_values():  # H7 / I3
    from engines._ws_cdp import _redact_sensitive
    snap = 'textbox "password" (bid=9) value "hunter2"\nbutton "Sign in" (bid=10)'
    out = _redact_sensitive(snap)
    assert "hunter2" not in out
    assert 'textbox "password"' in out  # structure kept, value gone
