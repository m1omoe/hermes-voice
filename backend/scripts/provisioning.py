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
    dns_name: str
    support_dir: Path
    backend_label: str = "dev.finklea.harnessvoice.backend"
    renew_label: str = "dev.finklea.harnessvoice.cert-renew"


def _certificate_paths(layout: MacServerLayout) -> tuple[Path, Path]:
    return layout.support_dir / "server.crt", layout.support_dir / "server.key"


def _render_plist(payload: dict[str, object]) -> str:
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode()


def render_backend_wrapper(layout: MacServerLayout, keychain_service: str) -> str:
    cert_path, key_path = _certificate_paths(layout)
    security_service = shlex.quote(keychain_service)
    uv_path = shlex.quote(str(layout.uv))
    backend_dir = shlex.quote(str(layout.repo_root / "backend"))
    cert_path = shlex.quote(str(cert_path))
    key_path = shlex.quote(str(key_path))
    public_host = shlex.quote(layout.dns_name)

    return f"""#!/usr/bin/env bash
set -euo pipefail

TOKEN="$(security find-generic-password -a "$USER" -s {security_service} -w 2>/dev/null || true)"
if [[ -z "$TOKEN" ]]; then
    echo "Keychain lookup returned no backend token" >&2
    exit 1
fi

UV={uv_path}
HERMES_VOICE_HOST="0.0.0.0" \\
HERMES_VOICE_CERT={cert_path} \\
HERMES_VOICE_KEY={key_path} \\
HERMES_VOICE_PUBLIC_HOST={public_host} \\
HERMES_VOICE_TOKEN="$TOKEN" \\
exec "$UV" run --directory {backend_dir} python -m app
"""


def render_backend_plist(layout: MacServerLayout, wrapper_path: Path) -> str:
    cert_path, key_path = _certificate_paths(layout)
    backend_dir = layout.repo_root / "backend"
    return _render_plist(
        {
            "Label": layout.backend_label,
            "ProgramArguments": [str(wrapper_path)],
            "EnvironmentVariables": {
                "HERMES_VOICE_HOST": "0.0.0.0",
                "HERMES_VOICE_CERT": str(cert_path),
                "HERMES_VOICE_KEY": str(key_path),
                "HERMES_VOICE_PUBLIC_HOST": layout.dns_name,
            },
            "WorkingDirectory": str(backend_dir),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
            "ThrottleInterval": 10,
            "StandardOutPath": str(layout.support_dir / "backend.log"),
            "StandardErrorPath": str(layout.support_dir / "backend.log"),
        }
    )


def render_renew_plist(layout: MacServerLayout, renew_script_path: Path) -> str:
    return _render_plist(
        {
            "Label": layout.renew_label,
            "ProgramArguments": ["/bin/bash", str(renew_script_path)],
            "StartInterval": 2_592_000,
            "RunAtLoad": False,
            "StandardOutPath": str(layout.support_dir / "cert-renew.log"),
            "StandardErrorPath": str(layout.support_dir / "cert-renew.log"),
        }
    )


def render_renew_script(
    layout: MacServerLayout, tailscale_path: Path | str | None = None
) -> str:
    tailscale_cert_dir = layout.home / "Library/Containers/io.tailscale.ipn.macos/Data"
    tailscale = str(
        tailscale_path or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    )
    domain = shlex.quote(layout.dns_name)
    tailscale = shlex.quote(tailscale)
    tailscale_cert_dir = shlex.quote(str(tailscale_cert_dir))
    support_dir = shlex.quote(str(layout.support_dir))
    backend_label = shlex.quote(layout.backend_label)
    return f"""#!/usr/bin/env bash
set -euo pipefail

DNS_NAME={domain}
TAILSCALE={tailscale}
TAILSCALE_CERT_DIR={tailscale_cert_dir}
DEST_DIR={support_dir}
BACKEND_LABEL={backend_label}

"$TAILSCALE" cert "$DNS_NAME"
mkdir -p "$DEST_DIR"
cp "$TAILSCALE_CERT_DIR/$DNS_NAME.crt" "$DEST_DIR/server.crt"
cp "$TAILSCALE_CERT_DIR/$DNS_NAME.key" "$DEST_DIR/server.key"
chmod 644 "$DEST_DIR/server.crt"
chmod 600 "$DEST_DIR/server.key"
launchctl kickstart -k "gui/$(id -u)/$BACKEND_LABEL"
"""
