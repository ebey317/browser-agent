"""Shared guard layer (Jev H6 + I4): ONE gate below the engine abstraction.

Both engines call these before executing input; the agent loop calls the
same functions too, but the engine-side check is authoritative — engine
divergence can no longer bypass the gates.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

RUN_DIR = Path(os.environ.get("BA_RUNTIME_DIR",
                              os.environ.get("XDG_RUNTIME_DIR", "/tmp"))) / "browser-agent"
RUN_DIR.mkdir(parents=True, exist_ok=True)
try:
    RUN_DIR.chmod(0o700)  # not world-writable (review: world-writable-tmp-files)
except OSError:
    pass

TAKEOVER_FILE = Path(os.environ.get("BA_TAKEOVER_FILE", str(RUN_DIR / "takeover")))
APPROVAL_FILE = Path(os.environ.get("BA_APPROVAL_FILE", str(RUN_DIR / "approvals")))
TAKEOVER_STALE_S = float(os.environ.get("BA_TAKEOVER_STALE", "600"))  # Jev H4

GUARD_PATTERNS = [
    r"password", r"checkout", r"payment", r"credit.?card", r"billing",
    r"confirm.?purchase", r"place.?order", r"sign.?in.?secure",
]


def takeover_active() -> bool:
    """Jev I6 fail-closed: any existing flag (fresh OR stale) = paused.

    A stale flag is surfaced via takeover_stale() for the UI; it never
    auto-releases the agent — only an explicit release() does (H4).
    """
    try:
        return TAKEOVER_FILE.exists()
    except OSError:
        return True  # fail closed


def takeover_stale() -> bool:
    try:
        return TAKEOVER_FILE.exists() and \
            (time.time() - TAKEOVER_FILE.stat().st_mtime) > TAKEOVER_STALE_S
    except OSError:
        return True


def take_over() -> None:
    revoke_grants()  # an approval given before the human grabbed the wheel is void
    # Write with owner-only permissions (0o600)
    fd = os.open(TAKEOVER_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"human\n{time.time():.0f}\n")
    except Exception:
        os.unlink(TAKEOVER_FILE)
        raise


def release() -> None:
    if TAKEOVER_FILE.exists():
        TAKEOVER_FILE.unlink()


def atomic_json(path: Path, data: dict) -> None:  # Jev: approval race
    tmp = path.with_suffix(".tmp")
    # Write with owner-only permissions (0o600)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data))
    except Exception:
        os.unlink(tmp)
        raise
    tmp.replace(path)


# Facts about the REAL element (from the browser), not words in the action text.
ELEMENT_PATTERNS = [
    r"type=password",
    r"autocomplete=\S*(password|cc-)",
]
# A bare number or bid:N says nothing about what the field is.
OPAQUE_TARGET = re.compile(r"^\s*(bid\s*[:=]\s*)?\d+\s*$", re.I)


def _domain_of(target: str) -> str:
    m = re.search(r"https?://([^/'\s\"]+)", str(target))
    return m.group(1).lower() if m else ""


def is_guarded(action: dict, element: str | None = None) -> bool:
    """element = what the browser says the target is (see CdpPage.element_facts).

    Fail closed: typing into a target we cannot identify (bare bid, no element
    facts) is guarded, because a numeric id carries no word to match and that
    is exactly how a password field would slip past a text-only check.
    """
    # Normalize: None and empty/whitespace-only element facts = unknown
    if element is not None:
        element = element.strip()
        if not element:
            element = None
    blob = json.dumps(action).lower()
    if element:
        blob += " " + element.lower()
    if any(re.search(p, blob) for p in GUARD_PATTERNS + ELEMENT_PATTERNS):
        return True
    if action.get("kind") == "navigate":
        # anything but plain http(s) (file:, chrome:, javascript:, data:, ...) needs a human
        url = str(action.get("value") or action.get("target") or "").strip().lower()
        if not re.match(r"https?://[^/\s]", url):
            return True
    if (action.get("kind") == "type" and element is None
            and OPAQUE_TARGET.match(str(action.get("target", "")))):
        return True
    return False


# ---- single-use grants: how a human approval reaches the engine gate ---------------
# The engine gate (page_act) is authoritative and re-checks every action. Without a way to
# know a human already approved THIS action, it skipped it again and approved password or
# checkout steps never ran. The loop calls grant() only after approval_granted() returned
# True; the gate calls consume_grant(). A grant is bound to the exact action (kind, target,
# value), is used once, expires, and is dropped when the human takes over.
GRANT_TTL_S = float(os.environ.get("BA_GRANT_TTL", "120"))
_grants: dict[str, float] = {}
_grants_lock = threading.Lock()


def _grant_key(action: dict) -> str:
    return json.dumps([str(action.get("kind") or ""), str(action.get("target") or ""),
                       str(action.get("value") or "")])


def grant(action: dict) -> None:
    _purge_expired_grants()
    with _grants_lock:
        _grants[_grant_key(action)] = time.time() + GRANT_TTL_S


def _purge_expired_grants() -> None:
    now = time.time()
    with _grants_lock:
        expired = [k for k, exp in _grants.items() if exp <= now]
        for k in expired:
            _grants.pop(k, None)


def consume_grant(action: dict) -> bool:
    """True once for an approved, unexpired, identical action; then it is gone."""
    _purge_expired_grants()
    with _grants_lock:
        expires = _grants.pop(_grant_key(action), None)
    return expires is not None and time.time() <= expires


def revoke_grants() -> None:
    with _grants_lock:
        _grants.clear()


def approval_granted(action_id: str, timeout: float = 300.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if APPROVAL_FILE.exists():
            try:
                data = json.loads(APPROVAL_FILE.read_text())
            except (ValueError, OSError):
                data = {}
            if data.get(action_id) == "approved":
                return True
            if data.get(action_id) == "denied":
                return False
        time.sleep(1.0)
    return False
