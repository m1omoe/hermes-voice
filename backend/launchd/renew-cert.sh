#!/usr/bin/env bash
set -euo pipefail

# DEPRECATED: this checked-in script is intentionally non-operational and is
# not part of the guided setup path. Guided setup uses Tailscale Serve, which
# renews its own certificate transparently and never runs a script like this
# one. This file only documents the shape of a manual certificate-renewal
# script for an advanced direct-TLS runtime that manages its own certificate;
# the guided provisioner does not generate or invoke it:
# uv run --project backend --extra provision python \
#   backend/scripts/provision_macos.py setup

echo "This checked-in renewal script is not used by the guided Tailscale Serve setup path." >&2
exit 1
