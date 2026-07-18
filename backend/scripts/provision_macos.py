"""Guided macOS setup and one-time Harness Voice pairing QR CLI."""
from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.provisioning import (
    MacServerLayout,
    render_backend_plist,
    render_backend_wrapper,
    render_renew_plist,
    render_renew_script,
)

KEYCHAIN_SERVICE = "HERMES_VOICE_TOKEN"
DEFAULT_PORT = 8765
CERT_CONTAINER = Path("Library/Containers/io.tailscale.ipn.macos/Data")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        input: str | None = None,
    ) -> CommandResult: ...


class FileWriter(Protocol):
    def write_text(self, path: Path, content: str, *, mode: int | None = None) -> None: ...

    def copy(self, source: Path, destination: Path) -> None: ...

    def chmod(self, path: Path, mode: int) -> None: ...


class Keychain(Protocol):
    def set(self, service: str, token: str) -> None: ...


class ProvisioningError(RuntimeError):
    """A prerequisite or guided setup action prevented provisioning."""


class SubprocessRunner:
    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        input: str | None = None,
    ) -> CommandResult:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            input=input,
            env=env,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def health(self, url: str) -> dict[str, object]:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return json.load(response)
        except Exception as exc:
            raise ProvisioningError("HTTPS health check could not reach the backend") from exc


class LocalFileWriter:
    def write_text(self, path: Path, content: str, *, mode: int | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if mode is not None:
            self.chmod(path, mode)

    def copy(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    def chmod(self, path: Path, mode: int) -> None:
        path.chmod(mode)


class SecurityKeychain:
    def __init__(self, runner: CommandRunner, user: str | None = None) -> None:
        self._runner = runner
        self._user = user or os.environ.get("USER", "")

    def set(self, service: str, token: str) -> None:
        result = self._runner.run(
            ["security", "add-generic-password", "-a", self._user, "-s", service, "-U"],
            input=token,
        )
        if result.returncode:
            raise ProvisioningError("macOS Keychain refused the server token")


class MacProvisioner:
    def __init__(
        self,
        *,
        layout: MacServerLayout | None = None,
        runner: CommandRunner | None = None,
        keychain: Keychain | None = None,
        clock: Callable[[], float] | None = None,
        writer: FileWriter | None = None,
        confirm: Callable[[str], bool] | None = None,
        platform_name: str | None = None,
        env: Mapping[str, str] | None = None,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        pairing_store: object | None = None,
        output: Callable[[str], None] = print,
        user_id: int | None = None,
        tailscale: Path | None = None,
    ) -> None:
        self.layout = layout
        self.runner = runner or SubprocessRunner()
        self.keychain = keychain or SecurityKeychain(self.runner)
        self.clock = clock or time.time
        self.writer = writer or LocalFileWriter()
        self.confirm = confirm or self._interactive_confirm
        self.platform_name = platform_name or platform.system()
        self.env = dict(os.environ if env is None else env)
        self.token_factory = token_factory
        self.pairing_store = pairing_store
        self.output = output
        self.user_id = os.getuid() if user_id is None else user_id
        self.tailscale = tailscale

    @classmethod
    def discover(
        cls,
        *,
        runner: CommandRunner | None = None,
        home: Path | None = None,
        repo_root: Path | None = None,
        platform_name: str | None = None,
    ) -> MacServerLayout:
        runner = runner or SubprocessRunner()
        system = platform_name or platform.system()
        cls._require_macos(system)
        uv = cls._which(runner, "uv")
        if uv is None:
            raise ProvisioningError("uv is required; install uv and try again")
        tailscale = cls._which(runner, "tailscale")
        if tailscale is None and not hasattr(runner, "which"):
            app_path = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
            if app_path.is_file():
                tailscale = str(app_path)
        if tailscale is None:
            raise ProvisioningError("Tailscale is required; install and sign in first")
        dns_name = cls._detect_dns(runner, Path(tailscale))
        if not dns_name:
            raise ProvisioningError("Tailscale MagicDNS is unavailable")
        home = home or Path.home()
        repo_root = repo_root or Path(__file__).resolve().parents[2]
        return MacServerLayout(
            repo_root=repo_root,
            home=home,
            uv=Path(uv),
            dns_name=dns_name,
            support_dir=home / "Library/Application Support/Harness Voice",
        )

    def setup(self, *, from_bws: bool = False, from_bws_child: bool = False) -> None:
        self._require_macos(self.platform_name)
        if from_bws and not from_bws_child:
            self._invoke_bws_child()
            return
        if from_bws_child and not self.env.get("HERMES_VOICE_TOKEN"):
            raise ProvisioningError("BWS did not provide HERMES_VOICE_TOKEN to the child process")

        layout, tailscale = self._prepare_layout()
        self.layout = layout
        self.tailscale = tailscale

        self.output(f"Request a Tailscale HTTPS certificate for {layout.dns_name}.")
        if not self.confirm("request the Tailscale HTTPS certificate"):
            raise ProvisioningError("certificate request cancelled")
        certificate = self.runner.run([str(tailscale), "cert", layout.dns_name])
        if certificate.returncode:
            raise ProvisioningError("Tailscale certificate creation failed; setup stopped")
        self._copy_certificates(layout)

        token = self.env.get("HERMES_VOICE_TOKEN") if from_bws_child else self.token_factory()
        if not token:
            raise ProvisioningError("no server token was provided")
        self.keychain.set(KEYCHAIN_SERVICE, token)

        self.output("Install and start the Harness Voice LaunchAgents.")
        if not self.confirm("install and start the LaunchAgents"):
            raise ProvisioningError("LaunchAgent installation cancelled")
        self._write_assets(layout)
        self._start_agents(layout)
        self._require_https_health(layout)
        self.output(f"Harness Voice is ready at https://{layout.dns_name}:{DEFAULT_PORT}")

    def create_pairing_qr(self) -> str:
        layout = self.layout or self.discover(runner=self.runner, platform_name=self.platform_name)
        self.layout = layout
        self._require_https_health(layout)
        store = self.pairing_store or self._default_pairing_store()
        ticket = store.create(ttl_seconds=300)
        payload = self._pairing_payload(layout, ticket.pairing_id, ticket.secret)
        try:
            import qrcode
        except ImportError as exc:
            raise ProvisioningError(
                "install the provision extra to render pairing QR codes"
            ) from exc

        self.output("Scan this QR code with the Harness Voice app:")
        self.output(
            "Pairing code expires at "
            + datetime.fromtimestamp(ticket.expires_at, tz=UTC).isoformat()
        )
        qr = qrcode.QRCode(border=1)
        qr.add_data(payload)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        return payload

    def _prepare_layout(self) -> tuple[MacServerLayout, Path]:
        if self.layout is None:
            layout = self.discover(runner=self.runner, platform_name=self.platform_name)
            tailscale = self._find_tailscale()
            return layout, tailscale

        layout = self.layout
        if hasattr(self.runner, "which"):
            uv = self._which(self.runner, "uv")
            if uv is None:
                raise ProvisioningError("uv is required; install uv and try again")
        else:
            uv = str(layout.uv)
        tailscale = self._find_tailscale()
        dns_name = self._normalize_dns(layout.dns_name)
        if not dns_name:
            dns_name = self._detect_dns(self.runner, tailscale)
        if not dns_name:
            raise ProvisioningError("Tailscale MagicDNS is unavailable")
        if Path(uv) != layout.uv:
            layout = MacServerLayout(
                repo_root=layout.repo_root,
                home=layout.home,
                uv=Path(uv),
                dns_name=dns_name,
                support_dir=layout.support_dir,
                backend_label=layout.backend_label,
                renew_label=layout.renew_label,
            )
        elif dns_name != layout.dns_name:
            layout = MacServerLayout(
                repo_root=layout.repo_root,
                home=layout.home,
                uv=layout.uv,
                dns_name=dns_name,
                support_dir=layout.support_dir,
                backend_label=layout.backend_label,
                renew_label=layout.renew_label,
            )
        return layout, tailscale

    def _write_assets(self, layout: MacServerLayout) -> None:
        wrapper = layout.support_dir / "run-backend"
        renew = layout.support_dir / "renew-cert"
        launch_agents = layout.home / "Library/LaunchAgents"
        backend_plist = launch_agents / f"{layout.backend_label}.plist"
        renew_plist = launch_agents / f"{layout.renew_label}.plist"
        self.writer.write_text(wrapper, render_backend_wrapper(layout, KEYCHAIN_SERVICE))
        self.writer.chmod(wrapper, 0o755)
        self.writer.write_text(renew, render_renew_script(layout))
        self.writer.chmod(renew, 0o755)
        self.writer.write_text(backend_plist, render_backend_plist(layout, wrapper))
        self.writer.write_text(renew_plist, render_renew_plist(layout, renew))

    def _copy_certificates(self, layout: MacServerLayout) -> None:
        source_dir = layout.home / CERT_CONTAINER
        self.writer.copy(source_dir / f"{layout.dns_name}.crt", layout.support_dir / "server.crt")
        self.writer.copy(source_dir / f"{layout.dns_name}.key", layout.support_dir / "server.key")
        self.writer.chmod(layout.support_dir / "server.crt", 0o644)
        self.writer.chmod(layout.support_dir / "server.key", 0o600)

    def _start_agents(self, layout: MacServerLayout) -> None:
        uid = f"gui/{self.user_id}"
        launch_agents = layout.home / "Library/LaunchAgents"
        for label in (layout.backend_label, layout.renew_label):
            plist = launch_agents / f"{label}.plist"
            self._run_checked(["launchctl", "bootstrap", uid, str(plist)], "LaunchAgent bootstrap")
        self._run_checked(
            ["launchctl", "kickstart", "-k", f"{uid}/{layout.backend_label}"],
            "backend LaunchAgent start",
        )

    def _require_https_health(self, layout: MacServerLayout) -> None:
        url = self._health_url(layout)
        health = self._health(url)
        if health.get("status") != "ok" or health.get("scheme") != "https":
            raise ProvisioningError("HTTPS health check is not ready")

    def _health(self, url: str) -> dict[str, object]:
        health = getattr(self.runner, "health", None)
        if health is None:
            raise ProvisioningError("HTTPS health check is unavailable")
        try:
            return dict(health(url))
        except ProvisioningError:
            raise
        except Exception as exc:
            raise ProvisioningError("HTTPS health check could not reach the backend") from exc

    def _invoke_bws_child(self) -> None:
        args = [
            "bws-project",
            "run",
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            "setup",
            "--from-bws-child",
        ]
        env = {key: value for key, value in self.env.items() if key != "HERMES_VOICE_TOKEN"}
        result = self.runner.run(args, env=env)
        if result.returncode:
            raise ProvisioningError(
                "bws-project could not provide the registered server token; "
                "check the project registration and try again"
            )

    def _run_checked(self, args: list[str], action: str) -> None:
        result = self.runner.run(args)
        if result.returncode:
            raise ProvisioningError(f"{action} failed")

    def _find_tailscale(self) -> Path:
        if self.tailscale is not None:
            return self.tailscale
        found = self._which(self.runner, "tailscale")
        if found is None and not hasattr(self.runner, "which"):
            found = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        if found is None:
            raise ProvisioningError("Tailscale is required; install and sign in first")
        return Path(found)

    @staticmethod
    def _which(runner: CommandRunner, name: str) -> str | None:
        which = getattr(runner, "which", None)
        return which(name) if which is not None else None

    @classmethod
    def _detect_dns(cls, runner: CommandRunner, tailscale: Path) -> str:
        result = runner.run([str(tailscale), "status", "--json"])
        if result.returncode:
            return ""
        try:
            value = json.loads(result.stdout).get("Self", {}).get("DNSName", "")
        except (TypeError, ValueError, AttributeError):
            return ""
        return cls._normalize_dns(str(value))

    @staticmethod
    def _normalize_dns(value: str) -> str:
        return value.strip().rstrip(".")

    @staticmethod
    def _require_macos(system: str) -> None:
        if system != "Darwin":
            raise ProvisioningError("the macOS provisioner only runs on macOS")

    def _health_url(self, layout: MacServerLayout) -> str:
        return f"https://{layout.dns_name}:{DEFAULT_PORT}/health"

    @staticmethod
    def _pairing_payload(layout: MacServerLayout, pairing_id: str, secret: str) -> str:
        from urllib.parse import urlencode

        return urlencode(
            {
                "v": "1",
                "url": f"https://{layout.dns_name}:{DEFAULT_PORT}",
                "id": pairing_id,
                "secret": secret,
            },
            safe=":",
        ).join(("harness-voice://pair?", ""))

    def _default_pairing_store(self) -> object:
        try:
            from app.pairing import PairingStore
        except ImportError as exc:
            raise ProvisioningError("the backend pairing store is unavailable") from exc
        return PairingStore(now=self.clock)

    def _interactive_confirm(self, action: str) -> bool:
        return input(f"Confirm: {action} [y/N] ").strip().lower() in {"y", "yes"}

    def _print(self, message: str) -> None:
        self.output(message)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision a Harness Voice macOS server")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup")
    setup.add_argument("--from-bws", action="store_true")
    setup.add_argument("--from-bws-child", action="store_true", help=argparse.SUPPRESS)
    commands.add_parser("pair")
    args = parser.parse_args(argv)
    provisioner = MacProvisioner()
    try:
        if args.command == "setup":
            provisioner.setup(
                from_bws=args.from_bws,
                from_bws_child=args.from_bws_child,
            )
        else:
            provisioner.create_pairing_qr()
    except ProvisioningError as exc:
        print(f"Provisioning stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
