# Empty Tailscale Serve Status Implementation Plan

> Design: `docs/superpowers/specs/2026-07-27-empty-serve-status-design.md`

## Goal

Allow guided setup on a fresh App Store Tailscale installation whose Serve status is `{}`, without weakening fail-closed conflict classification.

## Task 1: Normalize the exact parsed empty document

**Files:**
- Modify: `backend/tests/test_provisioning.py`
- Modify: `backend/scripts/provision_macos.py`

1. Add a parser test passing whitespace-wrapped `{}` and asserting `exact=False`, `absent=True`, `conflict=None`.
2. Run only that test and confirm it fails because the current parser reports malformed TCP/Web status.
3. In `parse_serve_status`, immediately after the parsed-value dictionary check, return absent only when `len(status) == 0`.
4. Add/retain a regression proving a non-empty object missing TCP/Web remains a conflict.
5. Run the parser tests and Ruff for both changed files.

## Task 2: Prove and deploy fresh guided setup

**Files:**
- Modify: `backend/tests/test_provisioning.py`
- Modify: `.docs/ai/current-state.md` and the server-profile design/spec only after live results

1. Add a guided setup test with `serve_statuses=["{}", "{}", EXACT_SERVE_STATUS, EXACT_SERVE_STATUS]` and assert setup completes with exact Serve installation and a healthy backend.
2. Confirm the new setup test fails before any additional implementation; if Task 1 already makes it pass, verify it fails against Task 1's parent commit or explain that the parser RED test already proves the defect and this test defends the transaction sequence.
3. Run focused provisioning tests, full backend `pytest`, and Ruff for the changed Python files.
4. Commit the bug fix and obtain code review.
5. Mirror the clean repository to Coruscant, rerun guided setup, and approve its confirmation prompt.
6. Verify `https://coruscant.tailceb58.ts.net:8765/health` reports `status=ok`, `scheme=https`, and `mock=false`; then generate a Coruscant pairing QR and continue the existing two-profile device acceptance checklist.
7. Record only observed live results in the server-profile design/current-state files and commit documentation changes.
