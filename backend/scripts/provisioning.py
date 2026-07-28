from __future__ import annotations

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
    # Absolute path to `claude`, when it is installed. Optional: a Hermes-only
    # machine has none. Defaulted so existing layout constructions stay valid.
    claude_bin: Path | None = None
    backend_label: str = "io.harnessvoice.backend"


def _render_plist(payload: dict[str, object]) -> str:
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode()


def render_backend_wrapper(layout: MacServerLayout) -> str:
    uv_path = shlex.quote(str(layout.uv))
    hermes_bin = shlex.quote(str(layout.hermes_bin))
    backend_dir = shlex.quote(str(layout.repo_root / "backend"))
    provisioner = shlex.quote(
        str(layout.repo_root / "backend/scripts/provision_macos.py")
    )
    public_host = shlex.quote(layout.dns_name)
    # launchd hands a service a minimal PATH (no /opt/homebrew/bin, no
    # ~/.local/bin), so the backend cannot find `claude` by name even though
    # setup resolved it fine in the operator's interactive shell. Export the
    # absolute path exactly as HERMES_BIN is exported. Omitted when Claude is
    # not installed, so the backend falls back to a PATH lookup.
    claude_bin_line = (
        f"CLAUDE_BIN={shlex.quote(str(layout.claude_bin))} \\\n"
        if layout.claude_bin
        else ""
    )

    return f"""#!/usr/bin/env bash
set -euo pipefail

UV={uv_path}
{claude_bin_line}HERMES_BIN={hermes_bin} \\
HERMES_VOICE_HOST="127.0.0.1" \\
HERMES_VOICE_TRUSTED_HTTPS_PROXY="tailscale-serve" \\
FORWARDED_ALLOW_IPS="127.0.0.1" \\
HERMES_VOICE_PUBLIC_HOST={public_host} \\
HERMES_VOICE_DISABLE_DOTENV="1" \\
exec "$UV" run --directory {backend_dir} python {provisioner} _launch-backend
"""


def render_backend_plist(layout: MacServerLayout, wrapper_path: Path) -> str:
    backend_dir = layout.repo_root / "backend"
    environment = {
        "HERMES_VOICE_HOST": "127.0.0.1",
        "HERMES_VOICE_TRUSTED_HTTPS_PROXY": "tailscale-serve",
        "FORWARDED_ALLOW_IPS": "127.0.0.1",
        "HERMES_BIN": str(layout.hermes_bin),
        "HERMES_VOICE_PUBLIC_HOST": layout.dns_name,
    }
    # Mirrors HERMES_BIN, which is set in both the plist environment and the
    # wrapper. Omitted entirely when Claude is not installed.
    if layout.claude_bin:
        environment["CLAUDE_BIN"] = str(layout.claude_bin)
    return _render_plist(
        {
            "Label": layout.backend_label,
            "ProgramArguments": [str(wrapper_path)],
            "EnvironmentVariables": environment,
            "WorkingDirectory": str(backend_dir),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
            "ThrottleInterval": 10,
            "StandardOutPath": str(layout.support_dir / "backend.log"),
            "StandardErrorPath": str(layout.support_dir / "backend.log"),
        }
    )
