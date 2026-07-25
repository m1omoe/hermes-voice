"""Guided macOS setup and one-time Harness Voice pairing QR CLI."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import plistlib
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
LEGACY_BACKEND_LABEL = "dev.finklea.harnessvoice.backend"
RENEWAL_LABEL = "io.harnessvoice.cert-renew"


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

    def write_bytes(
        self, path: Path, content: bytes, *, mode: int | None = None
    ) -> None: ...

    def read_bytes(self, path: Path) -> bytes | None: ...

    def exists(self, path: Path) -> bool: ...

    def replace(self, source: Path, destination: Path) -> None: ...

    def unlink(self, path: Path) -> None: ...

    def mkdir(self, path: Path) -> None: ...

    def copy(self, source: Path, destination: Path) -> None: ...

    def chmod(self, path: Path, mode: int) -> None: ...


class Keychain(Protocol):
    def get(self, service: str) -> str | None: ...

    def set(self, service: str, token: str) -> None: ...

    def delete(self, service: str) -> None: ...


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

    def write_bytes(
        self, path: Path, content: bytes, *, mode: int | None = None
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if mode is not None:
            self.chmod(path, mode)

    def read_bytes(self, path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def exists(self, path: Path) -> bool:
        return path.exists()

    def replace(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)

    def unlink(self, path: Path) -> None:
        path.unlink(missing_ok=True)

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

    def delete_generic_password(self, service: bytes, account: bytes) -> None: ...


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
        self._delete = security.SecKeychainItemDelete
        self._delete.argtypes = [pointer]
        self._delete.restype = ctypes.c_int32
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

    def delete_generic_password(self, service: bytes, account: bytes) -> None:
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
        if password_data:
            self._free_content(None, password_data)
        if status == self._ITEM_NOT_FOUND:
            return
        if status:
            raise OSError(status, "SecKeychainFindGenericPassword failed")
        try:
            status = self._delete(item)
        finally:
            self._release(item)
        if status:
            raise OSError(status, "SecKeychainItemDelete failed")

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

    def delete(self, service: str) -> None:
        try:
            api = self._api or _SecurityFrameworkKeychain()
            api.delete_generic_password(service.encode(), self._account)
        except OSError as exc:
            raise ProvisioningError(
                "macOS Keychain could not delete the server token"
            ) from exc


@dataclass(frozen=True)
class _FileSnapshot:
    path: Path
    existed: bool
    content: bytes | None


@dataclass(frozen=True)
class _DeploymentSnapshot:
    files: tuple[_FileSnapshot, ...]
    token: str | None
    loaded_labels: frozenset[str]
    serve_state: ServeMappingState

    def file(self, path: Path) -> _FileSnapshot:
        return next(snapshot for snapshot in self.files if snapshot.path == path)


class _DeploymentTransaction:
    def __init__(
        self,
        provisioner: MacProvisioner,
        layout: MacServerLayout,
        tailscale: Path,
        *,
        requested_token: str | None,
        replace_token: bool,
    ) -> None:
        self.provisioner = provisioner
        self.layout = layout
        self.tailscale = tailscale
        self.requested_token = requested_token
        self.replace_token = replace_token
        self.committed = False
        self._rollback_actions: list[tuple[str, Callable[[], None]]] = []
        nonce = f"{os.getpid()}-{secrets.token_hex(4)}"
        self.wrapper = layout.support_dir / "run-backend"
        launch_agents = layout.home / "Library/LaunchAgents"
        self.backend_plist = launch_agents / f"{layout.backend_label}.plist"
        self.renewal_plist = launch_agents / f"{RENEWAL_LABEL}.plist"
        self.renewal_script = layout.support_dir / "renew-cert"
        self.legacy_plist = launch_agents / f"{LEGACY_BACKEND_LABEL}.plist"
        self.staged_wrapper = self._temporary_path(self.wrapper, "staged", nonce)
        self.staged_plist = self._temporary_path(
            self.backend_plist, "staged", nonce
        )
        self._secrets: list[str] = []

    @staticmethod
    def _temporary_path(path: Path, purpose: str, nonce: str) -> Path:
        suffix = path.suffix
        name = f".{path.name}.{purpose}-{nonce}{suffix}"
        return path.with_name(name)

    def execute(self) -> None:
        snapshot = self._snapshot()
        if snapshot.token:
            self._secrets.append(snapshot.token)
        token = self._select_token(snapshot.token)
        self._secrets.append(token)
        try:
            self._stage_and_validate()

            mapping_added = self.provisioner._require_serve_mapping(
                self.layout, self.tailscale
            )
            if mapping_added:
                self._register(
                    "Serve mapping removal",
                    lambda: self.provisioner._remove_serve_mapping(self.tailscale),
                )

            for label in (self.layout.backend_label, LEGACY_BACKEND_LABEL):
                if label not in snapshot.loaded_labels:
                    continue
                plist = (
                    self.backend_plist
                    if label == self.layout.backend_label
                    else self.legacy_plist
                )
                self._register(
                    f"{label} restart",
                    lambda label=label, plist=plist: self.provisioner._bootstrap_agent(
                        label, plist
                    ),
                )
                self.provisioner._bootout_agent(
                    label, f"existing {label} LaunchAgent shutdown"
                )
            if self.replace_token or snapshot.token is None:
                self._register(
                    "Keychain restoration",
                    lambda: self._restore_token(snapshot.token),
                )
                self._boundary(
                    "Keychain update",
                    lambda: self.provisioner.keychain.set(KEYCHAIN_SERVICE, token),
                )


            self._install_asset(snapshot.file(self.wrapper), self.staged_wrapper)
            self._install_asset(
                snapshot.file(self.backend_plist), self.staged_plist
            )
            self.provisioner._require_exact_serve_mapping(
                self.layout, self.tailscale
            )
            self._register(
                "new neutral LaunchAgent removal",
                lambda: self.provisioner._bootout_agent_if_loaded(
                    self.layout.backend_label
                ),
            )
            self.provisioner._bootstrap_agent(
                self.layout.backend_label, self.backend_plist
            )
            self.provisioner._require_https_health(self.layout, token)
        except Exception as exc:
            self._raise_precommit_failure(exc)

        self.commit()
        cleanup_failures = self._cleanup(snapshot)
        if cleanup_failures:
            boundaries = ", ".join(cleanup_failures)
            raise ProvisioningError(
                f"deployment committed; cleanup incomplete at {boundaries}"
            )

    def commit(self) -> None:
        self.committed = True
        self._rollback_actions.clear()

    def _snapshot(self) -> _DeploymentSnapshot:
        paths = (
            self.wrapper,
            self.backend_plist,
            self.renewal_plist,
            self.renewal_script,
            self.legacy_plist,
        )
        files = tuple(self._snapshot_file(path) for path in paths)
        loaded_labels = frozenset(
            label
            for label in (
                self.layout.backend_label,
                RENEWAL_LABEL,
                LEGACY_BACKEND_LABEL,
            )
            if self.provisioner._agent_loaded(label)
        )
        serve_state = self.provisioner._serve_mapping_state(
            self.layout, self.tailscale
        )
        if serve_state.conflict is not None:
            raise ProvisioningError(
                f"Tailscale Serve conflict: {serve_state.conflict}"
            )
        token = self.provisioner.keychain.get(KEYCHAIN_SERVICE)
        return _DeploymentSnapshot(files, token, loaded_labels, serve_state)

    def _snapshot_file(self, path: Path) -> _FileSnapshot:
        existed = self.provisioner.writer.exists(path)
        content = self.provisioner.writer.read_bytes(path) if existed else None
        if existed and content is None:
            raise ProvisioningError(f"generated asset snapshot failed for {path.name}")
        return _FileSnapshot(path, existed, content)

    def _select_token(self, prior_token: str | None) -> str:
        token = (
            self.requested_token
            if self.replace_token
            else prior_token
            if prior_token is not None
            else self.provisioner.token_factory()
        )
        if not token:
            raise ProvisioningError("no server token was provided")
        return token

    def _stage_and_validate(self) -> None:
        wrapper = render_backend_wrapper(
            self.layout, KEYCHAIN_SERVICE, str(self.provisioner.user_id)
        )
        plist = render_backend_plist(self.layout, self.wrapper)
        self._register(
            "staged wrapper removal",
            lambda: self.provisioner.writer.unlink(self.staged_wrapper),
        )
        self._boundary(
            "staged wrapper write",
            lambda: self.provisioner.writer.write_text(
                self.staged_wrapper, wrapper, mode=0o755
            ),
        )
        self._register(
            "staged plist removal",
            lambda: self.provisioner.writer.unlink(self.staged_plist),
        )
        self._boundary(
            "staged plist write",
            lambda: self.provisioner.writer.write_text(
                self.staged_plist, plist, mode=0o644
            ),
        )
        validation = self.provisioner.runner.run(
            ["bash", "-n", str(self.staged_wrapper)]
        )
        if validation.returncode:
            raise ProvisioningError("staged backend wrapper validation failed")
        staged_plist = self.provisioner.writer.read_bytes(self.staged_plist)
        if staged_plist is None:
            raise ProvisioningError("staged backend plist validation failed")
        try:
            parsed = plistlib.loads(staged_plist)
        except (TypeError, ValueError, plistlib.InvalidFileException) as exc:
            raise ProvisioningError("staged backend plist validation failed") from exc
        if not isinstance(parsed, dict):
            raise ProvisioningError("staged backend plist validation failed")

    def _install_asset(self, prior: _FileSnapshot, staged: Path) -> None:
        self._register(
            f"{prior.path.name} restoration",
            lambda: self._restore_file(prior),
        )
        self._boundary(
            f"{prior.path.name} atomic replacement",
            lambda: self.provisioner.writer.replace(staged, prior.path),
        )

    def _restore_file(self, snapshot: _FileSnapshot) -> None:
        if not snapshot.existed:
            self.provisioner.writer.unlink(snapshot.path)
            return
        if snapshot.content is None:
            raise ProvisioningError(
                f"missing snapshot content for {snapshot.path.name}"
            )
        nonce = f"{os.getpid()}-{secrets.token_hex(4)}"
        staged = self._temporary_path(snapshot.path, "rollback", nonce)
        mode = 0o755 if snapshot.path == self.wrapper else 0o644
        try:
            self.provisioner.writer.write_bytes(
                staged, snapshot.content, mode=mode
            )
            self.provisioner.writer.replace(staged, snapshot.path)
        finally:
            self.provisioner.writer.unlink(staged)

    def _restore_token(self, prior_token: str | None) -> None:
        if prior_token is None:
            self.provisioner.keychain.delete(KEYCHAIN_SERVICE)
        else:
            self.provisioner.keychain.set(KEYCHAIN_SERVICE, prior_token)

    def _cleanup(self, snapshot: _DeploymentSnapshot) -> list[str]:
        failures: list[str] = []
        if RENEWAL_LABEL in snapshot.loaded_labels:
            try:
                self.provisioner._bootout_agent(
                    RENEWAL_LABEL, "obsolete renewal agent bootout"
                )
            except Exception:
                failures.append("renewal agent bootout")
        renewal_failed = False
        for path in (self.renewal_plist, self.renewal_script):
            try:
                self.provisioner.writer.unlink(path)
            except Exception:
                renewal_failed = True
        if renewal_failed:
            failures.append("renewal asset removal")
        try:
            self.provisioner.writer.unlink(self.legacy_plist)
        except Exception:
            failures.append("legacy asset removal")
        return failures

    def _register(self, boundary: str, action: Callable[[], None]) -> None:
        self._rollback_actions.append((boundary, action))

    def _boundary(self, boundary: str, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as exc:
            raise ProvisioningError(f"{boundary} failed") from exc

    def _raise_precommit_failure(self, original: Exception) -> None:
        failures: list[str] = []
        for boundary, action in reversed(self._rollback_actions):
            try:
                action()
            except Exception:
                failures.append(boundary)
        message = (
            "deployment failed before commit: "
            + self._redact(str(original) or type(original).__name__)
        )
        if failures:
            message += "; rollback failed at " + ", ".join(failures)
        raise ProvisioningError(message) from original

    def _redact(self, message: str) -> str:
        for secret in self._secrets:
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return message


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
        self.output(
            "Configure a persistent tailnet-only HTTPS proxy for "
            f"{layout.dns_name} and install one backend LaunchAgent."
        )
        if not self.confirm(
            "configure the tailnet-only HTTPS proxy and backend LaunchAgent"
        ):
            raise ProvisioningError("server setup cancelled")
        transaction = _DeploymentTransaction(
            self,
            layout,
            tailscale,
            requested_token=bws_token,
            replace_token=from_bws_child,
        )
        transaction.execute()
        self.output(
            f"Harness Voice is ready at https://{layout.dns_name}:{DEFAULT_PORT}"
        )

    def create_pairing_qr(self) -> str:
        layout = self.layout or self.discover(
            runner=self.runner,
            platform_name=self.platform_name,
            hermes_bin=self.env.get("HERMES_BIN") or "hermes",
        )
        self.layout = layout
        if self.tailscale is None:
            self.tailscale = self._find_tailscale()
        token = self.keychain.get(KEYCHAIN_SERVICE)
        if not token:
            raise ProvisioningError(
                "no server token in Keychain; run setup before pairing"
            )
        direct_tls = bool(
            self.env.get("HERMES_VOICE_CERT") and self.env.get("HERMES_VOICE_KEY")
        )
        if not direct_tls:
            state = self._serve_mapping_state(layout, self.tailscale)
            if state.conflict is not None:
                raise ProvisioningError(
                    f"Tailscale Serve mapping conflict: {state.conflict}"
                )
            if not state.exact:
                raise ProvisioningError(
                    "Tailscale Serve mapping is absent and direct-TLS "
                    "runtime is incomplete; rerun setup or set both "
                    "HERMES_VOICE_CERT and HERMES_VOICE_KEY"
                )
        self._require_https_health(layout, token=token)
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

    def _agent_loaded(self, label: str) -> bool:
        service = f"gui/{self.user_id}/{label}"
        return self.runner.run(["launchctl", "print", service]).returncode == 0

    def _bootout_agent(self, label: str, action: str) -> None:
        service = f"gui/{self.user_id}/{label}"
        self._run_checked(["launchctl", "bootout", service], action)

    def _bootout_agent_if_loaded(self, label: str) -> None:
        if self._agent_loaded(label):
            self._bootout_agent(label, f"{label} rollback shutdown")

    def _bootstrap_agent(self, label: str, plist: Path) -> None:
        uid = f"gui/{self.user_id}"
        self._run_checked(
            ["launchctl", "bootstrap", uid, str(plist)],
            f"{label} LaunchAgent bootstrap",
        )
        self._run_checked(
            ["launchctl", "kickstart", "-k", f"{uid}/{label}"],
            f"{label} LaunchAgent start",
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

        try:
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
            verified = self._serve_mapping_state(layout, tailscale)
            if not verified.exact:
                detail = verified.conflict or "mapping remained absent"
                raise ProvisioningError(
                    f"failed to verify exact Tailscale Serve mapping: {detail}"
                )
        except Exception as original:
            try:
                self._remove_serve_mapping(tailscale)
            except Exception as rollback:
                raise ProvisioningError(
                    f"{original}; Tailscale Serve mapping rollback failed"
                ) from rollback
            raise
        return True

    def _require_exact_serve_mapping(
        self, layout: MacServerLayout, tailscale: Path
    ) -> None:
        state = self._serve_mapping_state(layout, tailscale)
        if not state.exact:
            detail = state.conflict or "mapping is absent"
            raise ProvisioningError(
                "exact Tailscale Serve mapping changed before LaunchAgent "
                f"bootstrap: {detail}"
            )

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
