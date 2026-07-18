from __future__ import annotations

import plistlib
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.provision_macos import (
    CommandResult,
    MacProvisioner,
    ProvisioningError,
    SecurityKeychain,
)
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
    assert 'HERMES_VOICE_DISABLE_DOTENV="1"' in rendered
    assert "exec env" not in rendered
    assert f"UV={shlex.quote(str(layout.uv))}" in rendered
    assert f'exec "$UV" run --directory {layout.repo_root / "backend"} python -m app' in rendered
    assert 'HERMES_VOICE_HOST="0.0.0.0"' in rendered
    assert f"HERMES_VOICE_PUBLIC_HOST={shlex.quote(layout.dns_name)}" in rendered
    assert 'echo "$TOKEN"' not in rendered
    assert "export HERMES_VOICE_TOKEN=" not in rendered
    assert "if [[ -z \"$TOKEN\" ]]" in rendered
    assert "exit 1" in rendered


def test_backend_wrapper_places_environment_before_exec_and_is_valid_shell(
    tmp_path: Path,
) -> None:
    layout = make_layout(tmp_path)

    rendered = render_backend_wrapper(layout, "custom-service")
    syntax = subprocess.run(
        ["bash", "-n"],
        input=rendered,
        capture_output=True,
        text=True,
        check=False,
    )

    assert syntax.returncode == 0, syntax.stderr
    lines = rendered.splitlines()
    exec_index = next(i for i, line in enumerate(lines) if line.startswith('exec "$UV" run'))
    assignment_indices = [
        i for i, line in enumerate(lines) if line.startswith("HERMES_VOICE_")
    ]
    assert assignment_indices
    assert max(assignment_indices) < exec_index
    assert lines[exec_index] == (
        f'exec "$UV" run --directory {layout.repo_root / "backend"} python -m app'
    )


def test_backend_wrapper_uses_shared_generated_certificate_paths(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)

    rendered = render_backend_wrapper(layout, "custom-service")

    assert f"HERMES_VOICE_CERT={shlex.quote(str(layout.support_dir / 'server.crt'))}" in rendered
    assert f"HERMES_VOICE_KEY={shlex.quote(str(layout.support_dir / 'server.key'))}" in rendered


def test_renew_script_uses_the_discovered_tailscale_executable(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    tailscale = Path("/opt/custom/bin/tailscale")

    rendered = render_renew_script(layout, tailscale)

    assert f"TAILSCALE={shlex.quote(str(tailscale))}" in rendered
    assert "/Applications/Tailscale.app/Contents/MacOS/Tailscale" not in rendered


def test_renew_script_renews_and_restarts_generated_backend(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)

    rendered = render_renew_script(layout)

    assert "TAILSCALE" in rendered
    assert (
        'cert --cert-file "$DEST_DIR/server.crt" '
        '--key-file "$DEST_DIR/server.key" "$DNS_NAME"'
    ) in rendered
    assert f"DNS_NAME={shlex.quote(layout.dns_name)}" in rendered
    assert "TAILSCALE_CERT_DIR" not in rendered
    assert "Library/Containers/io.tailscale.ipn.macos" not in rendered
    assert f"DEST_DIR={shlex.quote(str(layout.support_dir))}" in rendered
    assert 'chmod 644 "$DEST_DIR/server.crt"' in rendered
    assert 'chmod 600 "$DEST_DIR/server.key"' in rendered
    assert 'launchctl kickstart -k "gui/$(id -u)/$BACKEND_LABEL"' in rendered
    assert layout.renew_label not in rendered


def test_renew_script_quotes_custom_backend_label_without_literal_quotes(tmp_path: Path) -> None:
    layout = MacServerLayout(
        repo_root=tmp_path / "checkout",
        home=tmp_path / "home",
        uv=Path("/opt/custom/bin/uv"),
        dns_name="mandalore.tailnet.ts.net",
        support_dir=tmp_path / "Library/Application Support/Harness Voice",
        backend_label="custom.backend label",
    )

    rendered = render_renew_script(layout)

    assert f"BACKEND_LABEL={shlex.quote(layout.backend_label)}" in rendered
    assert 'launchctl kickstart -k "gui/$(id -u)/$BACKEND_LABEL"' in rendered
    assert f"/$(id -u)/{shlex.quote(layout.backend_label)}" not in rendered


def test_renew_plist_points_at_script_and_uses_renew_label(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    script_path = layout.support_dir / "renew-cert"

    values = load_plist(render_renew_plist(layout, script_path))

    assert values["Label"] == layout.renew_label
    assert values["ProgramArguments"] == ["/bin/bash", str(script_path)]
    assert values["StartInterval"] == 2_592_000
    assert values["RunAtLoad"] is False


class FakeRunner:
    def __init__(
        self,
        *,
        uv: bool = True,
        tailscale: bool = True,
        dns_name: str = "mandalore.tailnet.ts.net.",
        cert_ok: bool = True,
        bws_ok: bool = True,
        bws_missing: bool = False,
        health_body: dict | None = None,
        health_bodies: list[dict] | None = None,
    ) -> None:
        self.uv = uv
        self.tailscale = tailscale
        self.dns_name = dns_name
        self.cert_ok = cert_ok
        self.bws_ok = bws_ok
        self.bws_missing = bws_missing
        self.health_body = health_body or {"status": "ok", "scheme": "https"}
        self.health_bodies = health_bodies
        self.health_calls = 0
        self.calls: list[tuple[list[str], dict[str, str] | None, str | None]] = []

    def which(self, name: str) -> str | None:
        if name == "uv" and self.uv:
            return "/opt/custom/bin/uv"
        if name == "tailscale" and self.tailscale:
            return "/opt/custom/bin/tailscale"
        return None

    def run(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        input: str | None = None,
    ) -> CommandResult:
        self.calls.append((args, env, input))
        if "cert" in args:
            return CommandResult(0 if self.cert_ok else 1, "", "certificate failed")
        if args[:2] == ["bws-project", "run"]:
            if self.bws_missing:
                raise FileNotFoundError("bws-project")
            return CommandResult(0 if self.bws_ok else 1, "", "bws unavailable")
        if "status" in args and "--json" in args:
            return CommandResult(
                0,
                f'{{"Self": {{"DNSName": "{self.dns_name}"}}}}',
                "",
            )
        return CommandResult(0, "", "")

    def health(self, url: str) -> dict:
        self.health_calls += 1
        if self.health_bodies:
            return self.health_bodies.pop(0)
        return self.health_body


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeKeychain:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str]] = []

    def set(self, service: str, token: str) -> None:
        self.saved.append((service, token))


class FakeWriter:
    def __init__(self) -> None:
        self.text: dict[Path, str] = {}
        self.copies: list[tuple[Path, Path]] = []
        self.modes: list[tuple[Path, int]] = []

    def write_text(self, path: Path, content: str, *, mode: int | None = None) -> None:
        self.text[path] = content
        if mode is not None:
            self.modes.append((path, mode))

    def mkdir(self, path: Path) -> None:
        pass

    def copy(self, source: Path, destination: Path) -> None:
        self.copies.append((source, destination))

    def chmod(self, path: Path, mode: int) -> None:
        self.modes.append((path, mode))


def make_provisioner(
    tmp_path: Path,
    runner: FakeRunner,
    *,
    token_factory=lambda: "public-token",
    env: dict[str, str] | None = None,
    platform_name: str = "Darwin",
    clock=None,
    sleeper=None,
) -> tuple[MacProvisioner, FakeKeychain, FakeWriter]:
    keychain = FakeKeychain()
    writer = FakeWriter()
    provisioner_args = {
        "layout": make_layout(tmp_path),
        "runner": runner,
        "keychain": keychain,
        "writer": writer,
        "confirm": lambda _: True,
        "token_factory": token_factory,
        "platform_name": platform_name,
        "env": env,
        "clock": clock,
    }
    if sleeper is not None:
        provisioner_args["sleeper"] = sleeper
    provisioner = MacProvisioner(**provisioner_args)
    return provisioner, keychain, writer


def test_setup_rejects_non_macos(tmp_path: Path) -> None:
    provisioner, _, _ = make_provisioner(tmp_path, FakeRunner(), platform_name="Linux")

    with pytest.raises(ProvisioningError, match="macOS"):
        provisioner.setup()


def test_setup_stops_when_tailscale_is_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "is_file", lambda path: False)
    provisioner, _, _ = make_provisioner(tmp_path, FakeRunner(tailscale=False))

    with pytest.raises(ProvisioningError, match="Tailscale"):
        provisioner.setup()


def test_setup_stops_when_uv_is_missing(tmp_path: Path) -> None:
    provisioner, _, _ = make_provisioner(tmp_path, FakeRunner(uv=False))

    with pytest.raises(ProvisioningError, match="uv"):
        provisioner.setup()


def test_discover_uses_tailscale_app_bundle_when_path_lookup_is_absent(
    tmp_path: Path, monkeypatch
) -> None:
    runner = FakeRunner(tailscale=False)
    app_path = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    monkeypatch.setattr(Path, "is_file", lambda path: path == app_path)

    MacProvisioner.discover(
        runner=runner,
        home=tmp_path / "home",
        repo_root=tmp_path / "checkout",
        platform_name="Darwin",
    )

    assert any(call[0] == [str(app_path), "status", "--json"] for call in runner.calls)


def test_discovery_normalizes_magic_dns_trailing_dot(tmp_path: Path) -> None:
    runner = FakeRunner(dns_name="mandalore.tailnet.ts.net.")

    layout = MacProvisioner.discover(
        runner=runner,
        home=tmp_path / "home",
        repo_root=tmp_path / "checkout",
        platform_name="Darwin",
    )

    assert layout.dns_name == "mandalore.tailnet.ts.net"


def test_setup_stops_when_magic_dns_is_missing(tmp_path: Path) -> None:
    provisioner, _, _ = make_provisioner(tmp_path, FakeRunner(dns_name=""))
    provisioner.layout = make_layout(tmp_path).__class__(
        repo_root=make_layout(tmp_path).repo_root,
        home=make_layout(tmp_path).home,
        uv=make_layout(tmp_path).uv,
        dns_name="",
        support_dir=make_layout(tmp_path).support_dir,
    )

    with pytest.raises(ProvisioningError, match="MagicDNS"):
        provisioner.setup()


def test_setup_requires_confirmation_before_certificate_request(tmp_path: Path) -> None:
    runner = FakeRunner()
    provisioner, _, _ = make_provisioner(tmp_path, runner)
    provisioner.confirm = lambda _: False

    with pytest.raises(ProvisioningError, match="cancelled"):
        provisioner.setup()

    assert not any("cert" in call[0] for call in runner.calls)


def test_setup_refuses_to_continue_after_certificate_failure(tmp_path: Path) -> None:
    runner = FakeRunner(cert_ok=False)
    provisioner, _, writer = make_provisioner(tmp_path, runner)

    with pytest.raises(ProvisioningError, match="certificate"):
        provisioner.setup()

    assert writer.text == {}
    assert not any("launchctl" in call[0] for call in runner.calls)


def test_setup_passes_discovered_tailscale_to_renewal_asset(tmp_path: Path) -> None:
    provisioner, _, writer = make_provisioner(tmp_path, FakeRunner())

    provisioner.setup()

    rendered = writer.text[make_layout(tmp_path).support_dir / "renew-cert"]
    assert "TAILSCALE=/opt/custom/bin/tailscale" in rendered
    assert "/Applications/Tailscale.app/Contents/MacOS/Tailscale" not in rendered


def test_setup_requests_certificate_directly_into_generated_support_paths(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    provisioner, _, _ = make_provisioner(tmp_path, runner)

    provisioner.setup()

    cert_calls = [call for call in runner.calls if "cert" in call[0]]
    assert len(cert_calls) == 1
    assert cert_calls[0][0] == [
        "/opt/custom/bin/tailscale",
        "cert",
        "--cert-file",
        str(make_layout(tmp_path).support_dir / "server.crt"),
        "--key-file",
        str(make_layout(tmp_path).support_dir / "server.key"),
        "mandalore.tailnet.ts.net",
    ]
    assert all("Library/Containers/io.tailscale.ipn.macos" not in arg for arg in cert_calls[0][0])


def test_security_keychain_uses_native_boundary_not_security_stdin(
    monkeypatch,
) -> None:
    token = "token-never-in-subprocess-argv"
    native_calls: list[tuple[bytes, bytes, bytes]] = []
    subprocess_argv: list[list[str]] = []

    class FakeNativeAPI:
        def set_generic_password(
            self, service: bytes, account: bytes, password: bytes
        ) -> None:
            native_calls.append((service, account, password))

    def record_subprocess(args, **kwargs):
        subprocess_argv.append(args)
        raise AssertionError("Keychain storage must not invoke security via subprocess")

    monkeypatch.setattr(subprocess, "run", record_subprocess)

    SecurityKeychain(api=FakeNativeAPI(), user="alice").set("service", token)

    assert native_calls == [(b"service", b"alice", token.encode())]
    assert all(token not in arg for argv in subprocess_argv for arg in argv)


def test_provisioned_backend_explicitly_skips_dotenv_loading(monkeypatch) -> None:
    import app.config as config

    monkeypatch.setenv("HERMES_VOICE_DISABLE_DOTENV", "1")
    loader_calls: list[tuple[object, ...]] = []

    class ForbiddenPath:
        def exists(self) -> bool:
            raise AssertionError("provisioned process must not inspect backend/.env")

    monkeypatch.setattr(config, "_ENV_PATH", ForbiddenPath())
    monkeypatch.setattr(
        config,
        "load_dotenv",
        lambda *args, **kwargs: loader_calls.append(args),
    )

    config._load_dotenv_if_enabled()

    assert loader_calls == []


def test_setup_accepts_transient_health_failure_before_success(tmp_path: Path) -> None:
    runner = FakeRunner(
        health_bodies=[
            {"status": "starting", "scheme": "https"},
            {"status": "ok", "scheme": "https"},
        ]
    )
    clock = FakeClock()
    provisioner, _, _ = make_provisioner(
        tmp_path, runner, clock=clock, sleeper=clock.sleep
    )

    provisioner.setup()

    assert runner.health_calls == 2
    assert clock.sleeps == [1.0]


def test_pair_refuses_after_bounded_health_deadline(tmp_path: Path) -> None:
    runner = FakeRunner(health_body={"status": "ok", "scheme": "http"})
    clock = FakeClock()
    provisioner, _, _ = make_provisioner(
        tmp_path, runner, clock=clock, sleeper=clock.sleep
    )

    with pytest.raises(ProvisioningError, match="HTTPS"):
        provisioner.create_pairing_qr()

    assert clock.now == 30.0
    assert runner.health_calls == 31


def test_public_token_only_reaches_keychain_not_assets_output_or_argv(
    tmp_path: Path,
    capsys,
) -> None:
    token = "public-token-never-rendered"
    runner = FakeRunner()
    provisioner, keychain, writer = make_provisioner(
        tmp_path, runner, token_factory=lambda: token
    )

    provisioner.setup()

    assert keychain.saved == [("HERMES_VOICE_TOKEN", token)]
    assert token not in "".join(writer.text.values())
    assert token not in capsys.readouterr().out
    assert all(token not in arg for call, _, _ in runner.calls for arg in call)


def test_setup_from_bws_delegates_without_secret_in_command_or_environment(
    tmp_path: Path,
) -> None:
    token = "managed-token-never-in-argv"
    runner = FakeRunner()
    provisioner, _, _ = make_provisioner(
        tmp_path,
        runner,
        env={"HERMES_VOICE_TOKEN": token},
    )

    provisioner.setup(from_bws=True)

    assert runner.calls[0][0][:3] == ["bws-project", "run", "--"]
    assert token not in runner.calls[0][0]
    assert token not in (runner.calls[0][1] or {})


def test_setup_from_bws_reports_missing_bws_without_traceback(tmp_path: Path) -> None:
    runner = FakeRunner(bws_missing=True)
    provisioner, _, _ = make_provisioner(tmp_path, runner)

    with pytest.raises(ProvisioningError, match="bws-project") as error:
        provisioner.setup(from_bws=True)

    assert "Traceback" not in str(error.value)


def test_setup_from_bws_reports_unavailable_registration_without_secret(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(bws_ok=False)
    provisioner, _, _ = make_provisioner(tmp_path, runner)

    with pytest.raises(ProvisioningError, match="project registration") as error:
        provisioner.setup(from_bws=True)

    assert "bws unavailable" not in str(error.value)


def test_bws_child_reads_token_from_environment_only(tmp_path: Path) -> None:
    token = "managed-token-never-in-argv"
    runner = FakeRunner()
    provisioner, keychain, _ = make_provisioner(
        tmp_path,
        runner,
        env={"HERMES_VOICE_TOKEN": token},
    )

    provisioner.setup(from_bws_child=True)

    assert keychain.saved == [("HERMES_VOICE_TOKEN", token)]
    assert all(token not in arg for call, _, _ in runner.calls for arg in call)


def test_pair_refuses_when_https_health_is_not_ready(tmp_path: Path) -> None:
    runner = FakeRunner(health_body={"status": "ok", "scheme": "http"})
    clock = FakeClock()
    provisioner, _, _ = make_provisioner(
        tmp_path, runner, clock=clock, sleeper=clock.sleep
    )

    with pytest.raises(ProvisioningError, match="HTTPS"):
        provisioner.create_pairing_qr()


def test_pair_prints_only_instruction_expiry_and_qr(tmp_path: Path, capsys) -> None:
    pytest.importorskip("qrcode")
    secret = "pairing-secret-never-printed"

    class FakePairingStore:
        def create(self, *, ttl_seconds: int) -> SimpleNamespace:
            assert ttl_seconds == 300
            return SimpleNamespace(
                pairing_id="pairing-id",
                secret=secret,
                expires_at=1_800_000_000.0,
            )

    provisioner, _, _ = make_provisioner(tmp_path, FakeRunner())
    provisioner.pairing_store = FakePairingStore()

    payload = provisioner.create_pairing_qr()
    output = capsys.readouterr().out

    assert payload.startswith("harness-voice://pair?")
    assert "Scan this QR code" in output
    assert "Pairing code expires at" in output
    assert secret not in output
    assert secret in payload
