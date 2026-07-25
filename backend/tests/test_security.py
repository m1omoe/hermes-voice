"""Security posture: /health detail gating + fail-closed non-loopback bind."""
import pytest

from app import __main__ as entrypoint
from app.config import Settings
from app.main import _is_loopback_host, assert_safe_bind
from tests.conftest import FakeHermes, build_client


def test_health_without_configured_token_returns_details():
    # No token configured (loopback dev) → nothing to gate on, so the
    # reachability check still surfaces the diagnostics details.
    client = build_client(hermes=FakeHermes(), stt=None, tts=None)
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "scheme" in body
    assert body["hermes"] != {}


def test_health_with_token_includes_details(monkeypatch):
    monkeypatch.setenv("HERMES_VOICE_TOKEN", "s3cret")
    from app.config import reset_settings_cache

    reset_settings_cache()
    try:
        client = build_client(hermes=FakeHermes(), stt=None, tts=None)
        # Wrong/missing token → minimal.
        assert client.get("/health").json()["hermes"] == {}
        # Correct token → details disclosed.
        body = client.get(
            "/health", headers={"X-Hermes-Voice-Token": "s3cret"}
        ).json()
        assert body["hermes"] != {}
    finally:
        reset_settings_cache()


@pytest.mark.parametrize(
    "host,loopback",
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("127.0.0.5", True),
        ("0.0.0.0", False),
        ("::", False),
        ("100.64.1.2", False),
        ("192.168.1.10", False),
    ],
)
def test_is_loopback_host(host, loopback):
    assert _is_loopback_host(host) is loopback


def test_validate_runtime_settings_refuses_exposed_without_token():
    with pytest.raises(RuntimeError, match="refusing to start"):
        assert_safe_bind(Settings(host="0.0.0.0", auth_token=""))


def test_validate_runtime_settings_allows_loopback_without_token():
    assert (
        assert_safe_bind(Settings(host="127.0.0.1", auth_token="")) is None
    )


def test_validate_runtime_settings_allows_exposed_with_token():
    assert (
        assert_safe_bind(Settings(host="0.0.0.0", auth_token="tok")) is None
    )


def test_settings_exposes_direct_tls_and_serve_modes():
    serve = Settings(
        host="127.0.0.1",
        auth_token="token",
        trusted_https_proxy="tailscale-serve",
    )
    assert serve.direct_tls is False
    assert serve.serve_mode is True
    assert serve.pairing_enabled is True
    assert serve.external_scheme == "https"

    direct_tls = Settings(
        auth_token="token",
        ssl_certfile="/tmp/cert.pem",
        ssl_keyfile="/tmp/key.pem",
    )
    assert direct_tls.direct_tls is True
    assert direct_tls.serve_mode is False
    assert direct_tls.pairing_enabled is True
    assert direct_tls.external_scheme == "https"

    plain_http = Settings(auth_token="token")
    assert plain_http.direct_tls is False
    assert plain_http.serve_mode is False
    assert plain_http.pairing_enabled is False
    assert plain_http.external_scheme == "http"


@pytest.mark.parametrize(
    "overrides",
    [
        {"host": "0.0.0.0"},
        {"ssl_certfile": "/tmp/cert.pem"},
        {"ssl_keyfile": "/tmp/key.pem"},
        {"forwarded_allow_ips": "*"},
        {"forwarded_allow_ips": "127.0.0.1,10.0.0.1"},
    ],
)
def test_validate_runtime_settings_rejects_invalid_serve_mode(overrides):
    values = {
        "host": "127.0.0.1",
        "auth_token": "token",
        "trusted_https_proxy": "tailscale-serve",
        "forwarded_allow_ips": "127.0.0.1",
    }
    values.update(overrides)

    with pytest.raises(RuntimeError, match="Serve mode"):
        assert_safe_bind(Settings(**values))


def test_validate_runtime_settings_rejects_unknown_proxy_mode():
    with pytest.raises(RuntimeError, match="trusted HTTPS proxy"):
        assert_safe_bind(
            Settings(
                auth_token="token",
                trusted_https_proxy="untrusted-proxy",
                forwarded_allow_ips="127.0.0.1",
            )
        )


def test_validate_runtime_settings_rejects_proxy_trust_with_direct_tls():
    with pytest.raises(RuntimeError, match="Direct TLS"):
        assert_safe_bind(
            Settings(
                auth_token="token",
                ssl_certfile="/tmp/cert.pem",
                ssl_keyfile="/tmp/key.pem",
                forwarded_allow_ips="*",
            )
        )


def test_create_app_validates_proxy_configuration(monkeypatch):
    monkeypatch.setenv("HERMES_VOICE_TOKEN", "token")
    monkeypatch.setenv("HERMES_VOICE_TRUSTED_HTTPS_PROXY", "unknown")
    from app.config import reset_settings_cache

    reset_settings_cache()
    with pytest.raises(RuntimeError, match="trusted HTTPS proxy"):
        build_client(hermes=FakeHermes(), stt=None, tts=None)


def test_entrypoint_enables_loopback_proxy_headers_for_serve(monkeypatch):
    settings = Settings(
        host="127.0.0.1",
        auth_token="token",
        trusted_https_proxy="tailscale-serve",
        forwarded_allow_ips="127.0.0.1",
    )
    run_calls = []
    monkeypatch.setattr(entrypoint, "get_settings", lambda: settings)
    monkeypatch.setattr(
        entrypoint.uvicorn,
        "run",
        lambda *args, **kwargs: run_calls.append((args, kwargs)),
    )

    entrypoint.main()

    assert len(run_calls) == 1
    args, kwargs = run_calls[0]
    assert args == ("app.main:app",)
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == "127.0.0.1"
    assert "ssl_certfile" not in kwargs
    assert "ssl_keyfile" not in kwargs


def test_entrypoint_disables_proxy_headers_for_direct_tls(monkeypatch):
    settings = Settings(
        host="127.0.0.1",
        auth_token="token",
        ssl_certfile="/tmp/cert.pem",
        ssl_keyfile="/tmp/key.pem",
    )
    run_calls = []
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setattr(entrypoint, "get_settings", lambda: settings)
    monkeypatch.setattr(
        entrypoint.uvicorn,
        "run",
        lambda *args, **kwargs: run_calls.append((args, kwargs)),
    )

    entrypoint.main()

    assert len(run_calls) == 1
    _, kwargs = run_calls[0]
    assert kwargs["proxy_headers"] is False
    assert kwargs["forwarded_allow_ips"] == ""
    assert kwargs["ssl_certfile"] == "/tmp/cert.pem"
    assert kwargs["ssl_keyfile"] == "/tmp/key.pem"
