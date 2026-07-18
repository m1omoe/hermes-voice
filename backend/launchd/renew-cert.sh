#!/usr/bin/env bash
set -euo pipefail

# DEPRECATED: this checked-in script is intentionally non-operational.
# Run the guided provisioner to generate a user-specific renewal agent:
# uv run --project backend --extra provision python \
#   backend/scripts/provision_macos.py setup

echo "This checked-in renewal script is deprecated; run provision_macos.py setup." >&2
exit 1
