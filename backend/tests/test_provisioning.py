from __future__ import annotations

import plistlib
import shlex
from pathlib import Path

from scripts.provisioning import (
    MacServerLayout,
    render_backend_plist,
    render_backend_wrapper,
    render_renew_plist,
    render_renew_script,
)


def make_layout(tmp_path: Path) -> MacServerLayout:
    return MacServerLayout(
        repo_root=tmp_path / "checkout",
        home=tmp_path / "home",
        uv=Path("/opt/custom/bin/uv"),
        dns_name="mandalore.tailnet.ts.net",
        support_dir=tmp_path / "Library/Application Support/Harness Voice",
    )


def load_plist(rendered: str) -> dict:
    return plistlib.loads(rendered.encode())


def test_backend_assets_use_detected_paths_only(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)

    rendered = render_backend_plist(layout, layout.support_dir / "run-backend")
    values = load_plist(rendered)

    assert str(layout.repo_root / "backend") in rendered
    assert "mandalore.tailnet.ts.net" in rendered
    assert values["Label"] == layout.backend_label
    assert values["ProgramArguments"] == [str(layout.support_dir / "run-backend")]
    assert str(layout.repo_root / "backend") == values["WorkingDirectory"]
    assert "/Users/tfinklea" not in rendered
    assert "scadrial" not in rendered


def test_backend_plist_escapes_dynamic_paths(tmp_path: Path) -> None:
    layout = MacServerLayout(
        repo_root=tmp_path / "checkout & copy",
        home=tmp_path / "home",
        uv=Path("/opt/custom/bin/uv"),
        dns_name="mandalore.tailnet.ts.net",
        support_dir=tmp_path / "Library/Application Support/Harness & Voice",
    )

    values = load_plist(render_backend_plist(layout, layout.support_dir / "run-backend"))

    assert values["WorkingDirectory"] == str(layout.repo_root / "backend")
    assert values["ProgramArguments"] == [str(layout.support_dir / "run-backend")]


def test_backend_wrapper_looks_up_keychain_token_for_child_only(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)

    rendered = render_backend_wrapper(layout, "custom-service")

    assert 'security find-generic-password -a "$USER" -s custom-service -w' in rendered
    assert 'HERMES_VOICE_TOKEN="$TOKEN"' in rendered
    assert "exec env" in rendered
    assert f"{layout.uv} run --directory {layout.repo_root / 'backend'} python -m app" in rendered
    assert 'HERMES_VOICE_HOST="0.0.0.0"' in rendered
    assert f"HERMES_VOICE_PUBLIC_HOST={shlex.quote(layout.dns_name)}" in rendered
    assert 'echo "$TOKEN"' not in rendered
    assert "export HERMES_VOICE_TOKEN=" not in rendered
    assert "if [[ -z \"$TOKEN\" ]]" in rendered
    assert "exit 1" in rendered


def test_backend_wrapper_uses_shared_generated_certificate_paths(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)

    rendered = render_backend_wrapper(layout, "custom-service")

    assert f"HERMES_VOICE_CERT={shlex.quote(str(layout.support_dir / 'server.crt'))}" in rendered
    assert f"HERMES_VOICE_KEY={shlex.quote(str(layout.support_dir / 'server.key'))}" in rendered


def test_renew_script_renews_and_restarts_generated_backend(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)

    rendered = render_renew_script(layout)

    assert "tailscale" in rendered
    assert 'cert "$DNS_NAME"' in rendered
    assert f"DNS_NAME={shlex.quote(layout.dns_name)}" in rendered
    tailscale_cert_dir = layout.home / "Library/Containers/io.tailscale.ipn.macos/Data"
    assert shlex.quote(str(tailscale_cert_dir)) in rendered
    assert f"DEST_DIR={shlex.quote(str(layout.support_dir))}" in rendered
    assert 'cp "$TAILSCALE_CERT_DIR/$DNS_NAME.crt" "$DEST_DIR/server.crt"' in rendered
    assert 'cp "$TAILSCALE_CERT_DIR/$DNS_NAME.key" "$DEST_DIR/server.key"' in rendered
    assert 'chmod 644 "$DEST_DIR/server.crt"' in rendered
    assert 'chmod 600 "$DEST_DIR/server.key"' in rendered
    assert f'launchctl kickstart -k "gui/$(id -u)/{layout.backend_label}"' in rendered
    assert layout.renew_label not in rendered


def test_renew_plist_points_at_script_and_uses_renew_label(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    script_path = layout.support_dir / "renew-cert"

    values = load_plist(render_renew_plist(layout, script_path))

    assert values["Label"] == layout.renew_label
    assert values["ProgramArguments"] == ["/bin/bash", str(script_path)]
    assert values["StartInterval"] == 2_592_000
    assert values["RunAtLoad"] is False
