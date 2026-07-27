# Empty Tailscale Serve Status on Fresh macOS Setup

## Status

Approved by the user after live reproduction on Coruscant.

## Problem

On a fresh App Store Tailscale installation with no Serve configuration, `tailscale serve status --json` returns `{}`. `parse_serve_status` currently requires both `TCP` and `Web` dictionaries, so guided setup reports a malformed-status conflict and stops before installing anything. The existing absent-state test covers only `{"TCP": {}, "Web": {}}`.

## Decision

Treat exactly an empty top-level JSON object as `ServeMappingState(exact=False, absent=True, conflict=None)` before validating the normal `TCP` and `Web` schema.

Keep every other fail-closed rule unchanged:

- malformed JSON and non-object documents remain conflicts;
- non-empty objects missing either `TCP` or `Web` remain malformed conflicts;
- partial target mappings remain conflicts;
- wrong HTTPS flags, handlers, or proxy targets remain conflicts;
- mappings for unrelated hosts or ports remain ignored as absent for the requested endpoint.

Do not run `tailscale serve reset` and do not default arbitrary missing sections to empty dictionaries.

## Verification

Add a parser regression for `{}` and a guided fresh-setup regression using the observed empty status. The setup regression must reach successful Serve installation rather than the malformed-status stop. Run the focused provisioning tests, full backend suite, and Ruff for the changed files. Mirror the verified commit to Coruscant, retry guided setup, and confirm public HTTPS health before continuing two-profile device acceptance.
