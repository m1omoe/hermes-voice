# Empty Tailscale Serve Status on Fresh macOS Setup

## Status

Approved by the user after live reproduction on Coruscant; revised after adversarial review by Opus 5 and GLM 5.2.

## Problem

On a fresh App Store Tailscale installation with no Serve configuration, `tailscale serve status --json` returns `{}`. `parse_serve_status` currently requires both `TCP` and `Web` dictionaries, so guided setup reports a malformed-status conflict and stops before installing anything. The existing absent-state test covers only `{"TCP": {}, "Web": {}}`.

## Decision

After `json.loads` succeeds and the result passes the existing `isinstance(status, dict)` check, test `len(status) == 0` and return `ServeMappingState(exact=False, absent=True, conflict=None)`. The boundary is the parsed top-level dictionary—not raw payload text and not missing/falsey `TCP` or `Web` values.

Keep every other fail-closed rule unchanged:

- malformed JSON and non-object documents remain conflicts;
- non-empty objects missing either `TCP` or `Web` remain malformed conflicts, including documents containing only unrelated future sections;
- partial target mappings remain conflicts;
- wrong HTTPS flags, handlers, or proxy targets remain conflicts;
- mappings for unrelated hosts or ports remain ignored as absent for the requested endpoint.

Do not run `tailscale serve reset` and do not default arbitrary missing sections to empty dictionaries.

## Verification

Add a parser regression that passes `{}` with arbitrary surrounding whitespace. Add a guided fresh-setup regression with the exact four-state sequence the transaction consumes: `serve_statuses=["{}", "{}", EXACT_SERVE_STATUS, EXACT_SERVE_STATUS]`—snapshot absent, pre-add absent, post-add exact, final readiness exact. The setup regression must complete successfully rather than exhaust the fake sequence or stop on a malformed/absent mapping. Run the focused provisioning tests, full backend suite, and Ruff for the changed files. Mirror the verified commit to Coruscant, retry guided setup, and confirm public HTTPS health before continuing two-profile device acceptance.
