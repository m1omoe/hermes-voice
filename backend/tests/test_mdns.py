"""Tests for the Bonjour/mDNS advertiser and app lifespan integration.

Network operations are either skipped by pure-logic seams or replaced with an
async fake, so nothing here touches multicast.
"""
from app import main, mdns
from app.config import reset_settings_cache
from tests.conftest import FakeHermes, build_client


def test_in_cgnat_classifies_tailscale_range():
    assert mdns._in_cgnat("100.64.0.1")
    assert mdns._in_cgnat("100.112.34.59")   # a real Tailscale addr on this host
    assert mdns._in_cgnat("100.127.255.255")
    assert not mdns._in_cgnat("100.63.0.1")  # just below 100.64/10
    assert not mdns._in_cgnat("100.128.0.1")  # just above
    assert not mdns._in_cgnat("192.168.1.5")
    assert not mdns._in_cgnat("10.0.0.1")
    assert not mdns._in_cgnat("not.an.ip")


async def test_stop_mdns_none_is_noop():
    # Must not raise when advertising was skipped (start returned None).
    await mdns.stop_mdns(None)


async def test_start_mdns_skips_without_lan(monkeypatch):
    # No LAN IPv4 → returns None and never touches the network.
    monkeypatch.setattr(mdns, "_primary_lan_ipv4", lambda: None)
    assert await mdns.start_mdns(port=8765, scheme="http") is None


def test_serve_mode_advertises_https(monkeypatch):
    calls = []

    async def fake_start_mdns(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setenv("HERMES_VOICE_TOKEN", "test-token")
    monkeypatch.setenv("HERMES_VOICE_HOST", "127.0.0.1")
    monkeypatch.setenv(
        "HERMES_VOICE_TRUSTED_HTTPS_PROXY", "tailscale-serve"
    )
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
    monkeypatch.setattr(main, "_should_auto_mock", lambda settings: False)
    monkeypatch.setattr(mdns, "start_mdns", fake_start_mdns)
    reset_settings_cache()
    try:
        with build_client(hermes=FakeHermes(), stt=None, tts=None):
            pass
    finally:
        reset_settings_cache()

    assert len(calls) == 1
    assert calls[0]["scheme"] == "https"


def test_serve_mode_advertises_the_magic_dns_hostname(monkeypatch):
    calls = []

    async def fake_start_mdns(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setenv("HERMES_VOICE_TOKEN", "test-token")
    monkeypatch.setenv("HERMES_VOICE_HOST", "127.0.0.1")
    monkeypatch.setenv(
        "HERMES_VOICE_TRUSTED_HTTPS_PROXY", "tailscale-serve"
    )
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
    monkeypatch.setenv("HERMES_VOICE_PUBLIC_HOST", "mandalore.tailnet.ts.net")
    monkeypatch.setattr(main, "_should_auto_mock", lambda settings: False)
    monkeypatch.setattr(mdns, "start_mdns", fake_start_mdns)
    reset_settings_cache()
    try:
        with build_client(hermes=FakeHermes(), stt=None, tts=None):
            pass
    finally:
        reset_settings_cache()

    assert len(calls) == 1
    assert calls[0]["scheme"] == "https"
    # Serve mode terminates TLS on the MagicDNS name, not the LAN IP — the
    # Bonjour TXT record must carry that hostname or the iOS client builds
    # a URL the certificate/proxy mapping won't match.
    assert calls[0]["public_host"] == "mandalore.tailnet.ts.net"
