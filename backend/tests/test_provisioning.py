from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import provision_macos
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
)

EXACT_SERVE_STATUS = """\
{
  "TCP": {"8765": {"HTTPS": true}},
  "Web": {
    "mandalore.tailnet.ts.net:8765": {
      "Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}
    }
  }
}
"""
ABSENT_SERVE_STATUS = '{"TCP": {}, "Web": {}}'
WRONG_PROXY_SERVE_STATUS = """\
{
  "TCP": {"8765": {"HTTPS": true}},
  "Web": {
    "mandalore.tailnet.ts.net:8765": {
      "Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}
    }
  }
}
"""


def test_serve_status_parser_accepts_only_the_exact_mapping() -> None:
    state = provision_macos.parse_serve_status(
        EXACT_SERVE_STATUS, "mandalore.tailnet.ts.net"
    )

    assert state.exact is True
    assert state.absent is False
    assert state.conflict is None


def test_serve_status_parser_recognizes_fully_absent_mapping() -> None:
    state = provision_macos.parse_serve_status(
        ABSENT_SERVE_STATUS, "mandalore.tailnet.ts.net"
    )

    assert state.exact is False
    assert state.absent is True
    assert state.conflict is None


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (WRONG_PROXY_SERVE_STATUS, "proxy"),
        (
            """\
{
  "TCP": {"8765": {"HTTPS": false}},
  "Web": {
    "mandalore.tailnet.ts.net:8765": {
      "Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}
    }
  }
}
""",
            "HTTPS",
        ),
        (
            """\
{
  "TCP": {"8765": {"HTTPS": true}},
  "Web": {
    "mandalore.tailnet.ts.net:8765": {
      "Handlers": {"/alternate": {"Proxy": "http://127.0.0.1:8765"}}
    }
  }
}
""",
            "handler",
        ),
    ],
)
def test_serve_status_parser_reports_conflicting_mapping(
    payload: str, reason: str
) -> None:
    state = provision_macos.parse_serve_status(
        payload, "mandalore.tailnet.ts.net"
    )

    assert state.exact is False
    assert state.absent is False
    assert state.conflict is not None
    assert reason.lower() in state.conflict.lower()


def test_serve_status_parser_treats_malformed_json_as_conflict() -> None:
    state = provision_macos.parse_serve_status(
        "{not-json", "mandalore.tailnet.ts.net"
    )

    assert state.exact is False
    assert state.absent is False
    assert state.conflict is not None
    assert "malformed" in state.conflict.lower()


def test_serve_status_parser_ignores_a_different_host_and_port() -> None:
    payload = """\
{
  "TCP": {"9443": {"HTTPS": true}},
  "Web": {
    "other.tailnet.ts.net:9443": {
      "Handlers": {"/": {"Proxy": "http://127.0.0.1:9443"}}
    }
  }
}
"""

    state = provision_macos.parse_serve_status(
        payload, "mandalore.tailnet.ts.net"
    )

    assert state.exact is False
    assert state.absent is True
    assert state.conflict is None


def make_layout(tmp_path: Path) -> MacServerLayout:
    return MacServerLayout(
        repo_root=tmp_path / "checkout",
        home=tmp_path / "home",
        uv=Path("/opt/custom/bin/uv"),
        hermes_bin=Path("/Users/tester/.local/bin/hermes"),
        dns_name="mandalore.tailnet.ts.net",
        support_dir=tmp_path / "Library/Application Support/Harness Voice",
    )


def load_plist(rendered: str) -> dict:
    return plistlib.loads(rendered.encode())


def test_default_launchagent_identity_is_product_owned(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)

    assert layout.backend_label == "io.harnessvoice.backend"


def test_backend_assets_use_detected_paths_only(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)

    rendered = render_backend_plist(layout, layout.support_dir / "run-backend")
    values = load_plist(rendered)

    assert str(layout.repo_root / "backend") in rendered
    assert values["Label"] == layout.backend_label
    assert values["ProgramArguments"] == [str(layout.support_dir / "run-backend")]
    assert str(layout.repo_root / "backend") == values["WorkingDirectory"]
    assert "/Users/tfinklea" not in rendered
    assert "scadrial" not in rendered
    assert "HERMES_VOICE_CERT" not in rendered
    assert "HERMES_VOICE_KEY" not in rendered
    assert "server.crt" not in rendered
    assert "server.key" not in rendered


def test_backend_plist_escapes_dynamic_paths(tmp_path: Path) -> None:
    layout = MacServerLayout(
        repo_root=tmp_path / "checkout & copy",
        home=tmp_path / "home",
        uv=Path("/opt/custom/bin/uv"),
        hermes_bin=Path("/Users/tester/.local/bin/hermes"),
        dns_name="mandalore.tailnet.ts.net",
        support_dir=tmp_path / "Library/Application Support/Harness & Voice",
    )

    values = load_plist(render_backend_plist(layout, layout.support_dir / "run-backend"))

    assert values["WorkingDirectory"] == str(layout.repo_root / "backend")
    assert values["ProgramArguments"] == [str(layout.support_dir / "run-backend")]


def test_backend_plist_carries_serve_mode_environment_only(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)

    rendered = render_backend_plist(layout, layout.support_dir / "run-backend")
    values = load_plist(rendered)
    env = values["EnvironmentVariables"]

    assert env["HERMES_VOICE_HOST"] == "127.0.0.1"
    assert env["HERMES_VOICE_TRUSTED_HTTPS_PROXY"] == "tailscale-serve"
    assert env["FORWARDED_ALLOW_IPS"] == "127.0.0.1"
    assert env["HERMES_BIN"] == str(layout.hermes_bin)
    assert "HERMES_VOICE_CERT" not in env
    assert "HERMES_VOICE_KEY" not in env
    assert "HERMES_VOICE_TOKEN" not in env


def test_backend_wrapper_looks_up_keychain_token_for_child_only(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)

    rendered = render_backend_wrapper(layout, "custom-service", "501")

    assert "security find-generic-password -a 501" in rendered
    assert "-s custom-service -w" in rendered
    assert 'security find-generic-password -a "$USER"' not in rendered
    assert 'HERMES_VOICE_TOKEN="$TOKEN"' in rendered
    assert 'HERMES_VOICE_DISABLE_DOTENV="1"' in rendered
    assert "exec env" not in rendered
    assert f"UV={shlex.quote(str(layout.uv))}" in rendered
    assert f'exec "$UV" run --directory {layout.repo_root / "backend"} python -m app' in rendered
    assert 'HERMES_VOICE_HOST="127.0.0.1"' in rendered
    assert 'HERMES_VOICE_TRUSTED_HTTPS_PROXY="tailscale-serve"' in rendered
    assert 'FORWARDED_ALLOW_IPS="127.0.0.1"' in rendered
    assert f"HERMES_BIN={shlex.quote(str(layout.hermes_bin))}" in rendered
    assert "HERMES_VOICE_CERT" not in rendered
    assert "HERMES_VOICE_KEY" not in rendered
    assert "server.crt" not in rendered
    assert "server.key" not in rendered
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
        i for i, line in enumerate(lines) if line.startswith("HERMES_")
    ]
    assert assignment_indices
    assert max(assignment_indices) < exec_index
    assert lines[exec_index] == (
        f'exec "$UV" run --directory {layout.repo_root / "backend"} python -m app'
    )


def test_backend_wrapper_quotes_dynamic_hermes_path(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)

    rendered = render_backend_wrapper(layout, "custom-service")

    assert f"HERMES_BIN={shlex.quote(str(layout.hermes_bin))}" in rendered


def test_setup_does_not_write_or_bootstrap_renewal_assets(tmp_path: Path) -> None:
    provisioner, _, writer = make_provisioner(tmp_path, FakeRunner())

    provisioner.setup()

    support = make_layout(tmp_path).support_dir
    assert support / "renew-cert" not in writer.text
    bootstrap_targets = [
        arg[-1]
        for call in provisioner.runner.calls
        if len(call) >= 1 and len(call[0]) >= 2 and call[0][0] == "launchctl"
        and call[0][1] == "bootstrap"
        for arg in [call[0][2:]]
    ]
    assert not any("renew" in target for target in bootstrap_targets)

class FakeRunner:
    def __init__(
        self,
        *,
        uv: bool = True,
        tailscale: bool = True,
        hermes_name: str = "hermes",
        hermes_path: str | None = "/Users/tester/.local/bin/hermes",
        dns_name: str = "mandalore.tailnet.ts.net.",
        cert_ok: bool = True,
        bws_ok: bool = True,
        bws_missing: bool = False,
        health_body: dict | None = None,
        health_bodies: list[dict] | None = None,
        harnesses_body: list[dict] | None = None,
        serve_statuses: list[str] | None = None,
    ) -> None:
        self.uv = uv
        self.tailscale = tailscale
        self.hermes_name = hermes_name
        self.hermes_path = hermes_path
        self.dns_name = dns_name
        self.cert_ok = cert_ok
        self.bws_ok = bws_ok
        self.bws_missing = bws_missing
        self.serve_statuses = (
            list(serve_statuses)
            if serve_statuses is not None
            else [EXACT_SERVE_STATUS]
        )
        self.health_body = health_body or {
            "status": "ok",
            "scheme": "https",
            "mock": False,
        }
        self.health_bodies = health_bodies
        self.harnesses_body = (
            harnesses_body
            if harnesses_body is not None
            else [{"id": "hermes", "name": "Hermes", "available": True}]
        )
        self.health_calls = 0
        self.harnesses_calls = 0
        self.harnesses_tokens: list[str] = []
        self.which_calls: list[str] = []
        self.calls: list[tuple[list[str], dict[str, str] | None, str | None]] = []
        self.capture_output: list[bool] = []
    def which(self, name: str) -> str | None:
        self.which_calls.append(name)
        if name == "uv" and self.uv:
            return "/opt/custom/bin/uv"
        if name == "tailscale" and self.tailscale:
            return "/opt/custom/bin/tailscale"
        if name == self.hermes_name:
            return self.hermes_path
        return None

    def run(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        input: str | None = None,
        capture_output: bool = True,
    ) -> CommandResult:
        self.calls.append((args, env, input))
        self.capture_output.append(capture_output)
        if args[1:] == ["serve", "status", "--json"]:
            if not self.serve_statuses:
                raise AssertionError("unexpected extra Tailscale Serve status check")
            return CommandResult(0, self.serve_statuses.pop(0), "")
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

    def harnesses(self, url: str, token: str) -> list[dict]:
        self.harnesses_calls += 1
        self.harnesses_tokens.append(token)
        return self.harnesses_body


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
    def __init__(self, existing_token: str | None = None) -> None:
        self.existing_token = existing_token
        self.saved: list[tuple[str, str]] = []

    def get(self, service: str) -> str | None:
        return self.existing_token

    def set(self, service: str, token: str) -> None:
        self.saved.append((service, token))
        self.existing_token = token


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
    existing_token: str | None = None,
) -> tuple[MacProvisioner, FakeKeychain, FakeWriter]:
    keychain = FakeKeychain(existing_token)
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


def test_exact_serve_mapping_is_observed_without_mutation(tmp_path: Path) -> None:
    runner = FakeRunner(serve_statuses=[EXACT_SERVE_STATUS])
    provisioner, _, _ = make_provisioner(tmp_path, runner)

    added = provisioner._require_serve_mapping(
        make_layout(tmp_path), Path("/opt/custom/bin/tailscale")
    )

    assert added is False
    assert [call[0] for call in runner.calls] == [
        ["/opt/custom/bin/tailscale", "serve", "status", "--json"]
    ]


def test_absent_serve_mapping_is_added_exactly_then_rechecked(tmp_path: Path) -> None:
    runner = FakeRunner(
        serve_statuses=[ABSENT_SERVE_STATUS, EXACT_SERVE_STATUS]
    )
    provisioner, _, _ = make_provisioner(tmp_path, runner)

    added = provisioner._require_serve_mapping(
        make_layout(tmp_path), Path("/opt/custom/bin/tailscale")
    )

    assert added is True
    assert [call[0] for call in runner.calls] == [
        ["/opt/custom/bin/tailscale", "serve", "status", "--json"],
        [
            "/opt/custom/bin/tailscale",
            "serve",
            "--bg",
            "--https=8765",
            "http://127.0.0.1:8765",
        ],
        ["/opt/custom/bin/tailscale", "serve", "status", "--json"],
    ]


def test_serve_conflict_stops_before_any_local_mutation(tmp_path: Path) -> None:
    runner = FakeRunner(serve_statuses=[WRONG_PROXY_SERVE_STATUS])
    provisioner, keychain, writer = make_provisioner(tmp_path, runner)

    with pytest.raises(ProvisioningError, match="conflict"):
        provisioner.setup()

    assert writer.text == {}
    assert writer.modes == []
    assert keychain.saved == []
    assert not any("cert" in call[0] for call in runner.calls)
    assert not any("launchctl" in call[0] for call in runner.calls)
    assert all("reset" not in call[0] for call in runner.calls)


def test_serve_mapping_unverified_add_is_removed_and_rejected(tmp_path: Path) -> None:
    runner = FakeRunner(
        serve_statuses=[ABSENT_SERVE_STATUS, WRONG_PROXY_SERVE_STATUS]
    )
    provisioner, _, _ = make_provisioner(tmp_path, runner)

    with pytest.raises(ProvisioningError, match="verify"):
        provisioner._require_serve_mapping(
            make_layout(tmp_path), Path("/opt/custom/bin/tailscale")
        )

    assert [call[0] for call in runner.calls][-1] == [
        "/opt/custom/bin/tailscale",
        "serve",
        "--https=8765",
        "off",
    ]
    assert all("reset" not in call[0] for call in runner.calls)


def test_setup_rolls_back_only_serve_mapping_added_by_this_run(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        cert_ok=False,
        serve_statuses=[ABSENT_SERVE_STATUS, EXACT_SERVE_STATUS],
    )
    provisioner, _, _ = make_provisioner(tmp_path, runner)

    with pytest.raises(ProvisioningError, match="certificate"):
        provisioner.setup()

    serve_calls = [call[0] for call in runner.calls if "serve" in call[0]]
    assert serve_calls == [
        ["/opt/custom/bin/tailscale", "serve", "status", "--json"],
        [
            "/opt/custom/bin/tailscale",
            "serve",
            "--bg",
            "--https=8765",
            "http://127.0.0.1:8765",
        ],
        ["/opt/custom/bin/tailscale", "serve", "status", "--json"],
        [
            "/opt/custom/bin/tailscale",
            "serve",
            "--https=8765",
            "off",
        ],
    ]
    assert all("reset" not in call for call in serve_calls)


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


def test_setup_resolves_requested_hermes_to_absolute_runtime_path(tmp_path: Path) -> None:
    runner = FakeRunner(
        hermes_name="custom-hermes",
        hermes_path="/Users/tester/.local/bin/custom-hermes",
    )
    provisioner, _, writer = make_provisioner(
        tmp_path,
        runner,
        env={"HERMES_BIN": "custom-hermes"},
    )

    provisioner.setup()

    wrapper = writer.text[make_layout(tmp_path).support_dir / "run-backend"]
    assert "custom-hermes" in runner.which_calls
    assert "HERMES_BIN=/Users/tester/.local/bin/custom-hermes" in wrapper
    assert runner.harnesses_tokens == ["public-token"]


def test_setup_stops_before_install_when_hermes_cannot_be_resolved(tmp_path: Path) -> None:
    runner = FakeRunner(hermes_path=None)
    provisioner, _, writer = make_provisioner(tmp_path, runner)
    output: list[str] = []
    provisioner.output = output.append

    with pytest.raises(ProvisioningError, match="Hermes"):
        provisioner.setup()

    assert not writer.text
    assert runner.health_calls == 0
    assert not any("ready" in message.lower() for message in output)


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
        hermes_bin=make_layout(tmp_path).hermes_bin,
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


def test_setup_generates_product_owned_launchagent_filenames(
    tmp_path: Path,
) -> None:
    provisioner, _, writer = make_provisioner(tmp_path, FakeRunner())

    provisioner.setup()

    launch_agents = make_layout(tmp_path).home / "Library/LaunchAgents"
    assert launch_agents / "io.harnessvoice.backend.plist" in writer.text
    assert launch_agents / "io.harnessvoice.cert-renew.plist" not in writer.text
    assert all("dev.finklea" not in str(path) for path in writer.text)


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


def test_security_keychain_accepts_the_uid_derived_account() -> None:
    native_calls: list[tuple[bytes, bytes, bytes]] = []

    class FakeNativeAPI:
        def set_generic_password(
            self, service: bytes, account: bytes, password: bytes
        ) -> None:
            native_calls.append((service, account, password))

    SecurityKeychain(api=FakeNativeAPI(), account="501").set("service", "token")

    assert native_calls == [(b"service", b"501", b"token")]


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
            {"status": "starting", "scheme": "https", "mock": False},
            {"status": "ok", "scheme": "https", "mock": False},
        ]
    )
    clock = FakeClock()
    provisioner, _, _ = make_provisioner(
        tmp_path, runner, clock=clock, sleeper=clock.sleep
    )

    provisioner.setup()

    assert runner.health_calls == 2
    assert clock.sleeps == [1.0]


def test_setup_rejects_mock_backend_before_reporting_ready(tmp_path: Path) -> None:
    runner = FakeRunner(
        health_body={"status": "ok", "scheme": "https", "mock": True}
    )
    clock = FakeClock()
    provisioner, _, _ = make_provisioner(
        tmp_path, runner, clock=clock, sleeper=clock.sleep
    )
    output: list[str] = []
    provisioner.output = output.append

    with pytest.raises(ProvisioningError, match="real Hermes"):
        provisioner.setup()

    assert not any("ready" in message.lower() for message in output)


def test_setup_requires_available_authenticated_hermes_before_ready(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        harnesses_body=[{"id": "hermes", "name": "Hermes", "available": False}]
    )
    clock = FakeClock()
    provisioner, _, _ = make_provisioner(
        tmp_path, runner, clock=clock, sleeper=clock.sleep
    )
    output: list[str] = []
    provisioner.output = output.append

    with pytest.raises(ProvisioningError, match="real Hermes"):
        provisioner.setup()

    assert runner.harnesses_calls == 31
    assert runner.harnesses_tokens == ["public-token"] * 31
    assert not any("ready" in message.lower() for message in output)


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


def test_setup_preserves_existing_public_keychain_token(tmp_path: Path) -> None:
    provisioner, keychain, _ = make_provisioner(
        tmp_path,
        FakeRunner(),
        existing_token="existing-public-token",
        token_factory=lambda: pytest.fail("existing token must not be regenerated"),
    )

    provisioner.setup()

    assert keychain.saved == []
    assert keychain.existing_token == "existing-public-token"


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


def test_bws_child_keeps_interactive_io_visible_and_unsuppressed(tmp_path: Path) -> None:
    runner = FakeRunner()
    provisioner, _, _ = make_provisioner(tmp_path, runner)

    provisioner.setup(from_bws=True)

    assert runner.capture_output[0] is False
    assert runner.calls[0][2] is None


def test_setup_unloads_existing_agents_before_replacing_keychain_token(tmp_path: Path) -> None:
    class ExistingAgentRunner(FakeRunner):
        def run(
            self,
            args: list[str],
            *,
            env: dict[str, str] | None = None,
            input: str | None = None,
            capture_output: bool = True,
        ) -> CommandResult:
            result = super().run(
                args, env=env, input=input, capture_output=capture_output
            )
            if args[:2] == ["launchctl", "print"]:
                return CommandResult(0, "loaded", "")
            return result

    runner = ExistingAgentRunner()
    provisioner, keychain, _ = make_provisioner(tmp_path, runner)

    provisioner.setup()

    operations = [call[0][:2] for call in runner.calls if call[0][0] == "launchctl"]
    assert operations == [
        ["launchctl", "print"],
        ["launchctl", "bootout"],
        ["launchctl", "bootstrap"],
        ["launchctl", "kickstart"],
    ]
    bootout_indexes = [
        index for index, call in enumerate(runner.calls) if call[0][:2] == ["launchctl", "bootout"]
    ]
    keychain_call_index = next(
        index
        for index, call in enumerate(runner.calls)
        if call[0][:2] == ["launchctl", "bootstrap"]
    )
    assert all(index < keychain_call_index for index in bootout_indexes)
    assert keychain.saved == [("HERMES_VOICE_TOKEN", "public-token")]


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


def test_bws_child_explicitly_replaces_existing_keychain_token(tmp_path: Path) -> None:
    token = "managed-token-replacement"
    provisioner, keychain, _ = make_provisioner(
        tmp_path,
        FakeRunner(),
        env={"HERMES_VOICE_TOKEN": token},
        existing_token="old-public-token",
    )

    provisioner.setup(from_bws_child=True)

    assert keychain.saved == [("HERMES_VOICE_TOKEN", token)]
    assert keychain.existing_token == token


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


def test_bws_child_sanitizes_token_from_later_subprocess_environments(
    tmp_path: Path, monkeypatch
) -> None:
    token = "managed-token-only-at-keychain-boundary"
    monkeypatch.setenv("HERMES_VOICE_TOKEN", token)

    class EnvironmentCapturingRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.subprocess_envs: list[dict[str, str]] = []

        def run(
            self,
            args: list[str],
            *,
            env: dict[str, str] | None = None,
            input: str | None = None,
            capture_output: bool = True,
        ) -> CommandResult:
            self.subprocess_envs.append(dict(os.environ if env is None else env))
            return super().run(
                args,
                env=env,
                input=input,
                capture_output=capture_output,
            )

    runner = EnvironmentCapturingRunner()
    provisioner, keychain, _ = make_provisioner(tmp_path, runner)

    provisioner.setup(from_bws_child=True)

    assert keychain.saved == [("HERMES_VOICE_TOKEN", token)]
    assert runner.subprocess_envs
    assert all(env.get("HERMES_VOICE_TOKEN") != token for env in runner.subprocess_envs)


def test_fresh_pair_process_reuses_custom_hermes_bin_from_setup_environment(
    tmp_path: Path,
) -> None:
    pytest.importorskip("qrcode")
    hermes_bin = "/Users/tester/.local/bin/custom-hermes"
    env = {"HERMES_BIN": "custom-hermes"}
    setup_runner = FakeRunner(
        hermes_name="custom-hermes",
        hermes_path=hermes_bin,
    )
    setup, _, _ = make_provisioner(tmp_path, setup_runner, env=env)
    setup.setup()

    class FakePairingStore:
        def create(self, *, ttl_seconds: int) -> SimpleNamespace:
            return SimpleNamespace(
                pairing_id="pairing-id",
                secret="pairing-secret",
                expires_at=1_800_000_000.0,
            )

    pair_runner = FakeRunner(
        hermes_name="custom-hermes",
        hermes_path=hermes_bin,
    )
    pair = MacProvisioner(
        runner=pair_runner,
        keychain=FakeKeychain("public-token"),
        platform_name="Darwin",
        env=env,
        pairing_store=FakePairingStore(),
        output=lambda _: None,
    )

    payload = pair.create_pairing_qr()

    assert payload.startswith("harness-voice://pair?")
    assert pair.layout is not None
    assert pair.layout.hermes_bin == Path(hermes_bin)
    assert "custom-hermes" in pair_runner.which_calls


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
