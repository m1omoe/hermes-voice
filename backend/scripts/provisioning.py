from __future__ import annotations

import os
import plistlib
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MacServerLayout:
    repo_root: Path
    home: Path
    uv: Path
    hermes_bin: Path
    dns_name: str
    support_dir: Path
    backend_label: str = "io.harnessvoice.backend"


def _render_plist(payload: dict[str, object]) -> str:
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode()


def render_backend_wrapper(
    layout: MacServerLayout,
    keychain_service: str,
    keychain_account: str | None = None,
) -> str:
    security_service = shlex.quote(keychain_service)
    security_account = shlex.quote(keychain_account or str(os.getuid()))
    uv_path = shlex.quote(str(layout.uv))
    hermes_bin = shlex.quote(str(layout.hermes_bin))
    backend_dir = shlex.quote(str(layout.repo_root / "backend"))

    return f"""#!/usr/bin/env bash
set -euo pipefail

TOKEN="$(security find-generic-password -a {security_account} \
    -s {security_service} -w 2>/dev/null || true)"
if [[ -z "$TOKEN" ]]; then
    echo "Keychain lookup returned no backend token" >&2
    exit 1
fi

UV={uv_path}
HERMES_BIN={hermes_bin} \\
HERMES_VOICE_HOST="127.0.0.1" \\
HERMES_VOICE_TRUSTED_HTTPS_PROXY="tailscale-serve" \\
FORWARDED_ALLOW_IPS="127.0.0.1" \\
HERMES_VOICE_DISABLE_DOTENV="1" \\
HERMES_VOICE_TOKEN="$TOKEN" \\
exec "$UV" run --directory {backend_dir} python -m app
"""


def render_backend_plist(layout: MacServerLayout, wrapper_path: Path) -> str:
    backend_dir = layout.repo_root / "backend"
    return _render_plist(
        {
            "Label": layout.backend_label,
            "ProgramArguments": [str(wrapper_path)],
            "EnvironmentVariables": {
                "HERMES_VOICE_HOST": "127.0.0.1",
                "HERMES_VOICE_TRUSTED_HTTPS_PROXY": "tailscale-serve",
                "FORWARDED_ALLOW_IPS": "127.0.0.1",
                "HERMES_BIN": str(layout.hermes_bin),
            },
            "WorkingDirectory": str(backend_dir),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
            "ThrottleInterval": 10,
            "StandardOutPath": str(layout.support_dir / "backend.log"),
            "StandardErrorPath": str(layout.support_dir / "backend.log"),
        }
    )
