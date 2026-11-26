# Fix Summary

Applied three fixes from review findings:

## 1. STOP_FILE race fix (agent.py:80-84)
- **Invariant**: Stop signal must be consumed atomically — check-then-delete has TOCTOU window
- **Fix**: Use `Path.unlink()` with `FileNotFoundError` catch; only triggers stop if file actually existed
- **Before**: `exists()` + `unlink()` with broad `except OSError` — race window, always executed else block
- **After**: Atomic unlink, stop only on successful deletion

## 2. Grant memory leak fix (engines/guard.py:147-152)
- **Invariant**: Expired grants must not accumulate indefinitely in `_grants` dict
- **Fix**: Added `_purge_expired_grants()` called at start of `consume_grant()` to remove all expired entries
- **Before**: Only exact-match grants removed via `pop()`; timed-out grants leaked forever
- **After**: All expired grants cleaned on every `consume_grant()` call

## 3. Navigate action gate fix (engines/_ws_cdp.py:32-45, 170-172)
- **Invariant**: Engine gate must enforce guard for ALL action kinds including navigate (docstring: "engine-side check is authoritative")
- **Fix**: Added "navigate" to guarded kinds in `page_act()`; implemented `_act_inner` navigate via `Page.navigate` CDP
- **Before**: Navigate bypassed engine gate entirely; `_act_inner` raised ValueError
- **After**: Non-http(s) navigation guarded at engine layer; http(s) allowed; grants work for navigate

All 44 tests pass (excluding test_thread_contract.py per intent).