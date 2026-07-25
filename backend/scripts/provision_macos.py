"""Guided macOS setup and one-time Harness Voice pairing QR CLI."""
from __future__ import annotations

import argparse
import ctypes
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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.provisioning import (
    MacServerLayout,
    render_backend_plist,
    render_backend_wrapper,
)

KEYCHAIN_SERVICE = "HERMES_VOICE_TOKEN"
DEFAULT_PORT = 8765
HEALTH_TIMEOUT = 30.0
HEALTH_INTERVAL = 1.0
TAILSCALE_APP_PATH = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ServeMappingState:
    exact: bool
    absent: bool
    conflict: str | None


def parse_serve_status(
    payload: str, dns_name: str, port: int = DEFAULT_PORT
) -> ServeMappingState:
    try:
        status = json.loads(payload)
    except (TypeError, ValueError):
        return ServeMappingState(False, False, "malformed Serve status JSON")
    if not isinstance(status, dict):
        return ServeMappingState(False, False, "malformed Serve status document")

    tcp = status.get("TCP")
    web = status.get("Web")
    if not isinstance(tcp, dict) or not isinstance(web, dict):
        return ServeMappingState(False, False, "malformed TCP or Web Serve status")

    port_key = str(port)
    web_key = f"{dns_name.strip().rstrip('.')}:{port}"
    tcp_present = port_key in tcp
    web_present = web_key in web
    if not tcp_present and not web_present:
        return ServeMappingState(False, True, None)
    if tcp_present != web_present:
        return ServeMappingState(False, False, "partial Serve mapping")

    tcp_entry = tcp[port_key]
    if (
        not isinstance(tcp_entry, dict)
        or set(tcp_entry) != {"HTTPS"}
        or tcp_entry["HTTPS"] is not True
    ):
        return ServeMappingState(False, False, "TCP entry is not exact HTTPS")

    web_entry = web[web_key]
    if not isinstance(web_entry, dict) or set(web_entry) != {"Handlers"}:
        return ServeMappingState(False, False, "Web entry is malformed")
    handlers = web_entry["Handlers"]
    if not isinstance(handlers, dict) or set(handlers) != {"/"}:
        return ServeMappingState(False, False, "Web entry has a conflicting root handler")
    root = handlers["/"]
    expected_proxy = f"http://127.0.0.1:{port}"
    if (
        not isinstance(root, dict)
        or set(root) != {"Proxy"}
        or root["Proxy"] != expected_proxy
    ):
        return ServeMappingState(False, False, "root handler has a conflicting proxy")
    return ServeMappingState(True, False, None)


class CommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        input: str | None = None,
        capture_output: bool = True,
    ) -> CommandResult: ...


class FileWriter(Protocol):
    def write_text(self, path: Path, content: str, *, mode: int | None = None) -> None: ...

    def mkdir(self, path: Path) -> None: ...

    def copy(self, source: Path, destination: Path) -> None: ...

    def chmod(self, path: Path, mode: int) -> None: ...


class Keychain(Protocol):
    def get(self, service: str) -> str | None: ...

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
        capture_output: bool = True,
    ) -> CommandResult:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=capture_output,
            text=True,
            input=input,
            env=env,
        )
        return CommandResult(
            completed.returncode, completed.stdout or "", completed.stderr or ""
        )

    def health(self, url: str) -> dict[str, object]:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return json.load(response)
        except Exception as exc:
            raise ProvisioningError("HTTPS health check could not reach the backend") from exc

    def harnesses(self, url: str, token: str) -> list[dict[str, object]]:
        request = urllib.request.Request(
            url,
            headers={"X-Hermes-Voice-Token": token},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.load(response)
        except Exception as exc:
            raise ProvisioningError(
                "authenticated harness readiness check could not reach the backend"
            ) from exc
        if not isinstance(payload, list):
            raise ProvisioningError(
                "authenticated harness readiness check returned an invalid response"
            )
        return payload


class LocalFileWriter:
    def write_text(self, path: Path, content: str, *, mode: int | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if mode is not None:
            self.chmod(path, mode)

    def mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def copy(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    def chmod(self, path: Path, mode: int) -> None:
        path.chmod(mode)


class NativeKeychainAPI(Protocol):
    def get_generic_password(self, service: bytes, account: bytes) -> bytes | None: ...

    def set_generic_password(self, service: bytes, account: bytes, password: bytes) -> None: ...


class _SecurityFrameworkKeychain:
    _ITEM_NOT_FOUND = -25300
    _DUPLICATE_ITEM = -25299

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise ProvisioningError("native macOS Keychain is unavailable on this platform")
        try:
            security = ctypes.CDLL(
                "/System/Library/Frameworks/Security.framework/Security"
            )
            core_foundation = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
        except OSError as exc:
            raise ProvisioningError("native macOS Keychain is unavailable") from exc
        pointer = ctypes.c_void_p
        uint32 = ctypes.c_uint32
        char_pointer = ctypes.c_char_p
        item_pointer = ctypes.POINTER(pointer)
        self._add = security.SecKeychainAddGenericPassword
        self._add.argtypes = [
            pointer,
            uint32,
            char_pointer,
            uint32,
            char_pointer,
            uint32,
            pointer,
            item_pointer,
        ]
        self._add.restype = ctypes.c_int32
        self._find = security.SecKeychainFindGenericPassword
        self._find.argtypes = [
            pointer,
            uint32,
            char_pointer,
            uint32,
            char_pointer,
            ctypes.POINTER(uint32),
            ctypes.POINTER(pointer),
            item_pointer,
        ]
        self._find.restype = ctypes.c_int32
        self._modify = security.SecKeychainItemModifyAttributesAndData
        self._modify.argtypes = [pointer, pointer, uint32, pointer]
        self._modify.restype = ctypes.c_int32
        self._free_content = security.SecKeychainItemFreeContent
        self._free_content.argtypes = [pointer, pointer]
        self._free_content.restype = ctypes.c_int32
        self._release = core_foundation.CFRelease
        self._release.argtypes = [pointer]
        self._release.restype = None

    def get_generic_password(self, service: bytes, account: bytes) -> bytes | None:
        pointer = ctypes.c_void_p
        service_buffer = ctypes.create_string_buffer(service)
        account_buffer = ctypes.create_string_buffer(account)
        password_length = ctypes.c_uint32()
        password_data = pointer()
        item = pointer()
        status = self._find(
            None,
            len(service),
            service_buffer,
            len(account),
            account_buffer,
            ctypes.byref(password_length),
            ctypes.byref(password_data),
            ctypes.byref(item),
        )
        if status == self._ITEM_NOT_FOUND:
            return None
        if status:
            raise OSError(status, "SecKeychainFindGenericPassword failed")
        try:
            return ctypes.string_at(password_data, password_length.value)
        finally:
            self._free_content(None, password_data)
            self._release(item)

    def set_generic_password(self, service: bytes, account: bytes, password: bytes) -> None:
        service_buffer = ctypes.create_string_buffer(service)
        account_buffer = ctypes.create_string_buffer(account)
        password_buffer = ctypes.create_string_buffer(password)
        status = self._add(
            None,
            len(service),
            service_buffer,
            len(account),
            account_buffer,
            len(password),
            password_buffer,
            None,
        )
        if status == self._DUPLICATE_ITEM:
            self._update(service, account, password)
        elif status:
            raise OSError(status, "SecKeychainAddGenericPassword failed")

    def _update(self, service: bytes, account: bytes, password: bytes) -> None:
        pointer = ctypes.c_void_p
        service_buffer = ctypes.create_string_buffer(service)
        account_buffer = ctypes.create_string_buffer(account)
        password_buffer = ctypes.create_string_buffer(password)
        password_length = ctypes.c_uint32()
        password_data = pointer()
        item = pointer()
        status = self._find(
            None,
            len(service),
            service_buffer,
            len(account),
            account_buffer,
            ctypes.byref(password_length),
            ctypes.byref(password_data),
            ctypes.byref(item),
        )
        if password_data:
            self._free_content(None, password_data)
        if status:
            raise OSError(status, "SecKeychainFindGenericPassword failed")
        try:
            status = self._modify(item, None, len(password), password_buffer)
        finally:
            self._release(item)
        if status:
            raise OSError(status, "SecKeychainItemModifyAttributesAndData failed")


class SecurityKeychain:
    def __init__(
        self,
        api: NativeKeychainAPI | None = None,
        user: str | None = None,
        account: str | None = None,
    ) -> None:
        self._api = api
        self._account = (
            account if account is not None else user if user is not None else str(os.getuid())
        ).encode()

    def get(self, service: str) -> str | None:
        try:
            api = self._api or _SecurityFrameworkKeychain()
            token = api.get_generic_password(service.encode(), self._account)
        except OSError as exc:
            raise ProvisioningError("macOS Keychain could not read the server token") from exc
        return token.decode() if token is not None else None

    def set(self, service: str, token: str) -> None:
        try:
            api = self._api or _SecurityFrameworkKeychain()
            api.set_generic_password(
                service.encode(), self._account, token.encode()
            )
        except OSError as exc:
            raise ProvisioningError("macOS Keychain refused the server token") from exc


class MacProvisioner:
    def __init__(
        self,
        *,
        layout: MacServerLayout | None = None,
        runner: CommandRunner | None = None,
        keychain: Keychain | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
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
        self.user_id = os.getuid() if user_id is None else user_id
        self.keychain = keychain or SecurityKeychain(account=str(self.user_id))
        self.clock = clock or time.time
        self.sleeper = sleeper or time.sleep
        self.writer = writer or LocalFileWriter()
        self.confirm = confirm or self._interactive_confirm
        self.platform_name = platform_name or platform.system()
        self.env = dict(os.environ if env is None else env)
        self.token_factory = token_factory
        self.pairing_store = pairing_store
        self.output = output
        self.tailscale = tailscale

    @classmethod
    def discover(
        cls,
        *,
        runner: CommandRunner | None = None,
        home: Path | None = None,
        repo_root: Path | None = None,
        platform_name: str | None = None,
        hermes_bin: str = "hermes",
    ) -> MacServerLayout:
        runner = runner or SubprocessRunner()
        system = platform_name or platform.system()
        cls._require_macos(system)
        uv = cls._which(runner, "uv")
        if uv is None:
            raise ProvisioningError("uv is required; install uv and try again")
        resolved_hermes = cls._which(runner, hermes_bin)
        if resolved_hermes is None:
            raise ProvisioningError(
                "Hermes harness is required; install Hermes or set HERMES_BIN "
                "to an executable and try again"
            )
        resolved_hermes_path = Path(resolved_hermes).expanduser().resolve()
        tailscale = cls._which(runner, "tailscale")
        if tailscale is None and TAILSCALE_APP_PATH.is_file():
            tailscale = str(TAILSCALE_APP_PATH)
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
            hermes_bin=resolved_hermes_path,
            dns_name=dns_name,
            support_dir=home / "Library/Application Support/Harness Voice",
        )

    def setup(self, *, from_bws: bool = False, from_bws_child: bool = False) -> None:
        self._require_macos(self.platform_name)
        bws_token = None
        if from_bws_child:
            bws_token = self._capture_bws_token()
            if not bws_token:
                raise ProvisioningError(
                    "BWS did not provide HERMES_VOICE_TOKEN to the child process"
                )
        if from_bws and not from_bws_child:
            self._invoke_bws_child()
            return

        layout, tailscale = self._prepare_layout()
        self.layout = layout
        self.tailscale = tailscale
        serve_mapping_added = self._require_serve_mapping(layout, tailscale)
        try:

            self.output(f"Request a Tailscale HTTPS certificate for {layout.dns_name}.")
            if not self.confirm("request the Tailscale HTTPS certificate"):
                raise ProvisioningError("certificate request cancelled")
            self.writer.mkdir(layout.support_dir)
            certificate = self.runner.run(
                [
                    str(tailscale),
                    "cert",
                    "--cert-file",
                    str(layout.support_dir / "server.crt"),
                    "--key-file",
                    str(layout.support_dir / "server.key"),
                    layout.dns_name,
                ]
            )
            if certificate.returncode:
                raise ProvisioningError(
                    "Tailscale certificate creation failed; setup stopped"
                )
            self.writer.chmod(layout.support_dir / "server.crt", 0o644)
            self.writer.chmod(layout.support_dir / "server.key", 0o600)

            existing_token = (
                None if from_bws_child else self.keychain.get(KEYCHAIN_SERVICE)
            )
            token = (
                bws_token
                if from_bws_child
                else self.token_factory()
                if existing_token is None
                else existing_token
            )
            if not token:
                raise ProvisioningError("no server token was provided")
            replace_token = from_bws_child or existing_token is None
            self.output("Install and start the Harness Voice LaunchAgents.")
            if not self.confirm("install and start the LaunchAgents"):
                raise ProvisioningError("LaunchAgent installation cancelled")
            self._unload_existing_agents(layout)
            if replace_token:
                self.keychain.set(KEYCHAIN_SERVICE, token)
            self._write_assets(layout)
            self._start_agents(layout)
            self._require_https_health(layout, token)
            self.output(
                f"Harness Voice is ready at https://{layout.dns_name}:{DEFAULT_PORT}"
            )
        except Exception:
            if serve_mapping_added:
                self._remove_serve_mapping(tailscale)
            raise

    def create_pairing_qr(self) -> str:
        layout = self.layout or self.discover(
            runner=self.runner,
            platform_name=self.platform_name,
            hermes_bin=self.env.get("HERMES_BIN") or "hermes",
        )
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
        requested_hermes = self.env.get("HERMES_BIN") or "hermes"
        if self.layout is None:
            layout = self.discover(
                runner=self.runner,
                platform_name=self.platform_name,
                hermes_bin=requested_hermes,
            )
            tailscale = self._find_tailscale()
            return layout, tailscale

        layout = self.layout
        if hasattr(self.runner, "which"):
            uv = self._which(self.runner, "uv")
            if uv is None:
                raise ProvisioningError("uv is required; install uv and try again")
            hermes = self._which(self.runner, requested_hermes)
            if hermes is None:
                raise ProvisioningError(
                    "Hermes harness is required; install Hermes or set HERMES_BIN "
                    "to an executable and try again"
                )
            hermes_bin = Path(hermes).expanduser().resolve()
        else:
            uv = str(layout.uv)
            hermes_bin = layout.hermes_bin
        tailscale = self._find_tailscale()
        dns_name = self._normalize_dns(layout.dns_name)
        if not dns_name:
            dns_name = self._detect_dns(self.runner, tailscale)
        if not dns_name:
            raise ProvisioningError("Tailscale MagicDNS is unavailable")
        return (
            replace(
                layout,
                uv=Path(uv),
                hermes_bin=hermes_bin,
                dns_name=dns_name,
            ),
            tailscale,
        )

    def _write_assets(self, layout: MacServerLayout) -> None:
        wrapper = layout.support_dir / "run-backend"
        launch_agents = layout.home / "Library/LaunchAgents"
        backend_plist = launch_agents / f"{layout.backend_label}.plist"
        self.writer.write_text(
            wrapper,
            render_backend_wrapper(layout, KEYCHAIN_SERVICE, str(self.user_id)),
        )
        self.writer.chmod(wrapper, 0o755)
        self.writer.write_text(backend_plist, render_backend_plist(layout, wrapper))

    def _unload_existing_agents(self, layout: MacServerLayout) -> None:
        uid = f"gui/{self.user_id}"
        service = f"{uid}/{layout.backend_label}"
        result = self.runner.run(["launchctl", "print", service])
        if result.returncode == 0:
            self._run_checked(
                ["launchctl", "bootout", service],
                "existing LaunchAgent shutdown",
            )

    def _start_agents(self, layout: MacServerLayout) -> None:
        uid = f"gui/{self.user_id}"
        launch_agents = layout.home / "Library/LaunchAgents"
        plist = launch_agents / f"{layout.backend_label}.plist"
        self._run_checked(["launchctl", "bootstrap", uid, str(plist)], "LaunchAgent bootstrap")
        self._run_checked(
            ["launchctl", "kickstart", "-k", f"{uid}/{layout.backend_label}"],
            "backend LaunchAgent start",
        )

    def _require_https_health(
        self,
        layout: MacServerLayout,
        token: str | None = None,
    ) -> None:
        url = self._health_url(layout)
        deadline = self.clock() + HEALTH_TIMEOUT
        last_error: ProvisioningError | None = None
        while True:
            try:
                health = self._health(url)
                if health.get("status") != "ok" or health.get("scheme") != "https":
                    raise ProvisioningError("HTTPS health check is not ready")
                if token is not None:
                    if health.get("mock") is not False:
                        raise ProvisioningError(
                            "backend readiness requires an available real Hermes harness"
                        )
                    harnesses = self._harnesses(self._harnesses_url(layout), token)
                    if not any(
                        harness.get("id") == "hermes"
                        and harness.get("available") is True
                        for harness in harnesses
                    ):
                        raise ProvisioningError(
                            "backend readiness requires an available real Hermes harness"
                        )
                return
            except ProvisioningError as exc:
                last_error = exc

            remaining = deadline - self.clock()
            if remaining <= 0:
                raise last_error
            self.sleeper(min(HEALTH_INTERVAL, remaining))

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

    def _harnesses(self, url: str, token: str) -> list[dict[str, object]]:
        harnesses = getattr(self.runner, "harnesses", None)
        if harnesses is None:
            raise ProvisioningError(
                "authenticated harness readiness check is unavailable"
            )
        try:
            payload = harnesses(url, token)
        except ProvisioningError:
            raise
        except Exception as exc:
            raise ProvisioningError(
                "authenticated harness readiness check could not reach the backend"
            ) from exc
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise ProvisioningError(
                "authenticated harness readiness check returned an invalid response"
            )
        return payload

    def _capture_bws_token(self) -> str | None:
        token = self.env.pop("HERMES_VOICE_TOKEN", None)
        os.environ.pop("HERMES_VOICE_TOKEN", None)
        return token

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
        try:
            result = self.runner.run(args, env=env, capture_output=False)
        except OSError as exc:
            raise ProvisioningError(
                "bws-project is unavailable; install it and check the project registration"
            ) from exc
        if result.returncode:
            raise ProvisioningError(
                "bws-project could not provide the registered server token; "
                "check the project registration and try again"
            )

    def _require_serve_mapping(
        self, layout: MacServerLayout, tailscale: Path
    ) -> bool:
        state = self._serve_mapping_state(layout, tailscale)
        if state.exact:
            return False
        if state.conflict is not None:
            raise ProvisioningError(f"Tailscale Serve conflict: {state.conflict}")

        add = self.runner.run(
            [
                str(tailscale),
                "serve",
                "--bg",
                f"--https={DEFAULT_PORT}",
                f"http://127.0.0.1:{DEFAULT_PORT}",
            ]
        )
        if add.returncode:
            raise ProvisioningError("Tailscale Serve mapping creation failed")

        try:
            verified = self._serve_mapping_state(layout, tailscale)
            if not verified.exact:
                detail = verified.conflict or "mapping remained absent"
                raise ProvisioningError(
                    f"failed to verify exact Tailscale Serve mapping: {detail}"
                )
        except Exception:
            self._remove_serve_mapping(tailscale)
            raise
        return True

    def _serve_mapping_state(
        self, layout: MacServerLayout, tailscale: Path
    ) -> ServeMappingState:
        result = self.runner.run(
            [str(tailscale), "serve", "status", "--json"]
        )
        if result.returncode:
            raise ProvisioningError("Tailscale Serve status inspection failed")
        return parse_serve_status(result.stdout, layout.dns_name, DEFAULT_PORT)

    def _remove_serve_mapping(self, tailscale: Path) -> None:
        result = self.runner.run(
            [
                str(tailscale),
                "serve",
                f"--https={DEFAULT_PORT}",
                "off",
            ]
        )
        if result.returncode:
            raise ProvisioningError("Tailscale Serve mapping rollback failed")

    def _run_checked(self, args: list[str], action: str) -> None:
        result = self.runner.run(args)
        if result.returncode:
            raise ProvisioningError(f"{action} failed")

    def _find_tailscale(self) -> Path:
        if self.tailscale is not None:
            return self.tailscale
        found = self._which(self.runner, "tailscale")
        if found is None and TAILSCALE_APP_PATH.is_file():
            found = str(TAILSCALE_APP_PATH)
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

    def _harnesses_url(self, layout: MacServerLayout) -> str:
        return f"https://{layout.dns_name}:{DEFAULT_PORT}/api/harnesses"

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
