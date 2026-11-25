# Fix Summary

## Changes Made

### 1. agent.py
- **Removed unused GUARD_PATTERNS** (f1): Deleted the duplicate guard patterns list since engines/guard.py owns the canonical patterns.
- **STOP_FILE now uses shared RUN_DIR** (f2): Changed from hardcoded `/tmp/browser-agent-stop` to `_guard.RUN_DIR / "stop"`, keeping the `BA_STOP_FILE` env override.
- **Fixed STOP_FILE race condition**: Uses `exists()` + `unlink()` with proper exception handling instead of check-then-delete race.

### 2. engines/guard.py
- **Removed site-allow feature** (f3): Deleted `SITE_ALLOW_FILE`, `site_allow_check`, `remember_site_allow`, `forget_site_allow`, and associated comments. This was unused dead code outside the stated intent.
- **Files created with 0o600 permissions** (f4): 
  - `atomic_json()` now writes temp files with `os.open(..., 0o600)`
  - `take_over()` writes TAKEOVER_FILE with `os.open(..., 0o600)`
- **Fixed fail-closed bypass** (user-1): `is_guarded()` now normalizes `element` parameter - `None` and empty/whitespace-only strings are treated identically as "unknown", ensuring opaque-target guard fires correctly.

### 3. tests/test_guard_element.py
- **Added regression test** `test_empty_element_facts_bypasses_opaque_target_guard`: Verifies that `element=''` and `element=' '` for a type action on `bid:3` are guarded (fail-closed), while identified safe elements are not.

## Verification
- All 44 tests pass (excluding `tests/canary_indeed.py`)
- No `__pycache__` or `run/` directories left in tree
- BA_RUNTIME_DIR pointed at temp dir during test runs