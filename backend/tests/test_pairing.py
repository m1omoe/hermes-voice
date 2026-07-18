"""Tests for the one-time pairing ticket store + HTTPS claim API.

Covers the store boundary (Task 1) and the narrow unauthenticated claim route
(Task 2). Pairing is a security-sensitive surface: a claim returns the
long-lived backend token, so the tests assert atomicity under concurrency, that
invalid/expired/replayed/unknown claims are indistinguishable to a caller, and
that neither the secret nor the token ever leaks into error responses.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

import pytest

from app import pairing
from app.config import reset_settings_cache
from tests.conftest import FakeHermes, build_client

# ─────────────────────────── Store unit tests ───────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> pairing.PairingStore:
    """Fresh, isolated pairing store per test (file is auto-created)."""
    return pairing.PairingStore(tmp_path / "pairings.db", now=lambda: 1000.0)


def test_create_returns_high_entropy_id_and_secret(store: pairing.PairingStore) -> None:
    ticket = store.create(ttl_seconds=300)
    # token_urlsafe(32) → ~43 chars; we generate 16 bytes per field, giving
    # ≥22 base64url chars. 16 char min on id, 32 char min on secret. The high
    # entropy (each field = 128 bits) is the real property, not the length.
    assert len(ticket.pairing_id) >= 16
    assert len(ticket.secret) >= 32
    # 128 bits of entropy → base64url expands 16 random bytes into ≥22 chars;
    # asserting the generated strings are well above the field minimums catches
    # accidental entropy reduction (e.g. switching to uuid4) without locking
    # the exact length.
    assert len(ticket.pairing_id) >= 22
    assert len(ticket.secret) >= 43
    # Sanity: the secret must be a valid base64url-shaped token (no padding,
    # URL-safe alphabet only). The pairing id can use the same alphabet.
    import re

    b64url = re.compile(r"^[A-Za-z0-9_-]+$")
    assert b64url.fullmatch(ticket.pairing_id)
    assert b64url.fullmatch(ticket.secret)


def test_store_does_not_persist_raw_secret(tmp_path: Path) -> None:
    """Verifier-only on disk: a stolen DB file is useless without the secret."""
    db = tmp_path / "pairings.db"
    s = pairing.PairingStore(db, now=lambda: 1000.0)
    ticket = s.create(ttl_seconds=300)
    raw = db.read_bytes()
    # The secret is 128 bits of entropy; the chance of it appearing in the
    # binary DB file by coincidence is negligible. If we ever started storing
    # the raw secret, this would fail.
    assert ticket.secret.encode() not in raw


def test_ticket_claim_is_single_use(store: pairing.PairingStore) -> None:
    ticket = store.create(ttl_seconds=300)
    assert store.claim(ticket.pairing_id, ticket.secret) is True
    # Replay: same id + secret must NOT be honored a second time.
    assert store.claim(ticket.pairing_id, ticket.secret) is False


def test_wrong_secret_cannot_claim(store: pairing.PairingStore) -> None:
    ticket = store.create(ttl_seconds=300)
    assert store.claim(ticket.pairing_id, ticket.secret + "x") is False
    # A failed attempt must not consume the ticket — a real client that fat-
    # fingered the secret still gets a retry with the correct one.
    assert store.claim(ticket.pairing_id, ticket.secret) is True


def test_unknown_pairing_id_cannot_be_claimed(store: pairing.PairingStore) -> None:
    assert store.claim("never-issued-id", "never-issued-secret") is False


def test_expired_ticket_cannot_be_claimed(tmp_path: Path) -> None:
    now = [1000.0]
    s = pairing.PairingStore(tmp_path / "pairings.db", now=lambda: now[0])
    ticket = s.create(ttl_seconds=10)
    now[0] = 1011.0  # past the 10s ttl
    assert s.claim(ticket.pairing_id, ticket.secret) is False


def test_concurrent_claim_attempts_only_one_succeeds(tmp_path: Path) -> None:
    """Atomicity: under contention exactly one of two simultaneous claim
    attempts is honored. SQLite's BEGIN IMMEDIATE + the WHERE-guard on
    claimed_at/expiry must serialize the writes."""
    s = pairing.PairingStore(tmp_path / "pairings.db", now=lambda: 1000.0)
    ticket = s.create(ttl_seconds=300)

    # Thread barrier so both threads release the claim call at the same
    # moment — without it one thread could finish before the other starts
    # and the test would degenerate to a sequential check.
    import threading

    barrier = threading.Barrier(2)

    def attempt() -> bool:
        barrier.wait()
        return s.claim(ticket.pairing_id, ticket.secret)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(attempt)
        f2 = pool.submit(attempt)
        wait([f1, f2])

    results = [f1.result(), f2.result()]
    assert results.count(True) == 1
    assert results.count(False) == 1


def test_prune_expired_removes_only_expired_rows(tmp_path: Path) -> None:
    now = [1000.0]
    s = pairing.PairingStore(tmp_path / "pairings.db", now=lambda: now[0])
    expired_a = s.create(ttl_seconds=10)
    live = s.create(ttl_seconds=300)
    expired_b = s.create(ttl_seconds=5)
    # Consume live; it stays in the table as a claimed row.
    assert s.claim(live.pairing_id, live.secret) is True

    now[0] = 1020.0  # past both expirations

    removed = s.prune_expired()
    assert removed == 2  # expired_a + expired_b (live is claimed, not expired)

    # Re-claiming an expired one is still impossible post-prune.
    assert s.claim(expired_a.pairing_id, expired_a.secret) is False
    assert s.claim(expired_b.pairing_id, expired_b.secret) is False


def test_each_create_generates_independent_id_and_secret(store: pairing.PairingStore) -> None:
    a = store.create()
    b = store.create()
    assert a.pairing_id != b.pairing_id
    assert a.secret != b.secret
    # Claiming a with b's secret fails; with a's secret succeeds.
    assert store.claim(a.pairing_id, b.secret) is False
    assert store.claim(a.pairing_id, a.secret) is True


# ─────────────────────────── HTTP claim API ───────────────────────────


@pytest.fixture
def configured_pairing_client(monkeypatch, tmp_path: Path):
    """Build a TestClient with TLS + token configured so the pairing route
    is available. Returns (client, settings) so callers can poke at the
    configured token when validating the claim response body."""
    monkeypatch.setattr(pairing, "DEFAULT_DB_PATH", tmp_path / "pairings.db")
    monkeypatch.setenv("HERMES_VOICE_TOKEN", "test-token")
    monkeypatch.setenv("HERMES_VOICE_CERT", "/tmp/fake-cert.pem")
    monkeypatch.setenv("HERMES_VOICE_KEY", "/tmp/fake-key.pem")
    reset_settings_cache()
    try:
        client = build_client(
            hermes=FakeHermes(),
            tts=None,
            stt=None,
            base_url="https://testserver",
        )
        yield client
    finally:
        reset_settings_cache()


def test_claim_endpoint_returns_token_once_over_https_transport(
    configured_pairing_client,
) -> None:
    client = configured_pairing_client
    ticket = client.app.state.pairings.create()

    first = client.post(
        "/api/pairings/claim",
        json={"pairing_id": ticket.pairing_id, "secret": ticket.secret},
    )
    assert first.status_code == 200
    assert first.json() == {"token": "test-token"}

    replay = client.post(
        "/api/pairings/claim",
        json={"pairing_id": ticket.pairing_id, "secret": ticket.secret},
    )
    assert replay.status_code == 404
    assert replay.json() == {"detail": "pairing code unavailable"}


def test_claim_endpoint_rejects_http_transport_even_when_tls_is_configured(
    configured_pairing_client,
) -> None:
    client = configured_pairing_client
    client.base_url = client.base_url.copy_with(scheme="http")
    ticket = client.app.state.pairings.create()

    response = client.post(
        "/api/pairings/claim",
        json={"pairing_id": ticket.pairing_id, "secret": ticket.secret},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "pairing code unavailable"}
    assert "test-token" not in response.text


def test_claim_endpoint_returns_404_when_tls_unconfigured(monkeypatch) -> None:
    """Without TLS configured the pairing route must be unavailable so a plain
    HTTP server can never be tricked into surrendering the token."""
    monkeypatch.delenv("HERMES_VOICE_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_VOICE_CERT", raising=False)
    monkeypatch.delenv("HERMES_VOICE_KEY", raising=False)
    reset_settings_cache()
    try:
        client = build_client(hermes=FakeHermes(), tts=None, stt=None)
        # No ticket creation — the route itself should 404, even with garbage.
        body = {"pairing_id": "abcdefghijklmnop", "secret": "x" * 64}
        r = client.post("/api/pairings/claim", json=body)
        assert r.status_code == 404
        assert r.json() == {"detail": "pairing code unavailable"}
    finally:
        reset_settings_cache()


def test_claim_endpoint_unauthenticated_no_token_required(
    configured_pairing_client,
) -> None:
    """Pairing is the ONE unauthenticated route by design — the iOS app
    claims before it has any credential. The route must NOT require the
    X-Hermes-Voice-Token header."""
    client = configured_pairing_client
    ticket = client.app.state.pairings.create()
    r = client.post(
        "/api/pairings/claim",
        json={"pairing_id": ticket.pairing_id, "secret": ticket.secret},
    )
    # No X-Hermes-Voice-Token header sent, yet the request succeeds.
    assert r.status_code == 200


def test_claim_endpoint_unknown_id_returns_404(
    configured_pairing_client,
) -> None:
    client = configured_pairing_client
    r = client.post(
        "/api/pairings/claim",
        json={"pairing_id": "a" * 32, "secret": "b" * 64},
    )
    assert r.status_code == 404
    assert r.json() == {"detail": "pairing code unavailable"}


def test_claim_endpoint_expired_returns_404(
    configured_pairing_client, monkeypatch,
) -> None:
    """An expired ticket must produce the same 404 body as an unknown one —
    callers (and attackers) must not be able to distinguish the two."""
    client = configured_pairing_client
    # Set the store clock directly so we can create a ticket then move past
    # its expiry without sleeping.
    state_clock = {"now": 1000.0}
    monkeypatch.setattr(
        client.app.state.pairings, "_now", lambda: state_clock["now"]
    )
    ticket = client.app.state.pairings.create(ttl_seconds=10)
    state_clock["now"] = 1100.0  # far past expiry

    r = client.post(
        "/api/pairings/claim",
        json={"pairing_id": ticket.pairing_id, "secret": ticket.secret},
    )
    assert r.status_code == 404
    assert r.json() == {"detail": "pairing code unavailable"}


def test_claim_endpoint_malformed_payload_returns_422(
    configured_pairing_client,
) -> None:
    """Bad input is a client error (422), not a 404 — keeps the two paths
    honest. The 404-vs-422 split lets us still tell "you sent garbage"
    (your bug) from "your code is unknown/expired" (correctly-shaped but
    unactionable)."""
    client = configured_pairing_client

    # Empty body → 422 (Pydantic validation).
    r1 = client.post("/api/pairings/claim", json={})
    assert r1.status_code == 422

    # Wrong characters → 422 (Pydantic pattern).
    r2 = client.post(
        "/api/pairings/claim",
        json={"pairing_id": "has spaces!", "secret": "b" * 64},
    )
    assert r2.status_code == 422

    # Too short → 422.
    r3 = client.post(
        "/api/pairings/claim",
        json={"pairing_id": "short", "secret": "b" * 64},
    )
    assert r3.status_code == 422


def test_claim_endpoint_does_not_leak_token_in_error_responses(
    configured_pairing_client,
) -> None:
    """A 404 response body must never echo the configured long-lived token,
    the secret, or the pairing id. Token in error bodies is a regression
    we'd never see in production but an attacker would discover with a
    single cURL."""
    client = configured_pairing_client
    r = client.post(
        "/api/pairings/claim",
        json={"pairing_id": "a" * 32, "secret": "b" * 64},
    )
    body_text = r.text
    assert "test-token" not in body_text
    assert ("a" * 32) not in body_text
    assert ("b" * 64) not in body_text


def test_test_environment_uses_a_temporary_pairing_database(tmp_path: Path) -> None:
    assert pairing.DEFAULT_DB_PATH == tmp_path / "pairings.db"


def test_create_app_prunes_expired_pairing_records(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "pairings.db"
    monkeypatch.setattr(pairing, "DEFAULT_DB_PATH", db_path)
    stale_store = pairing.PairingStore(db_path, now=lambda: 0.0)
    stale_store.create(ttl_seconds=-1)

    build_client(hermes=FakeHermes(), tts=None, stt=None)

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM pairings").fetchone()[0]
    assert remaining == 0


def test_claim_endpoint_does_not_leak_via_logging(
    configured_pairing_client, caplog,
) -> None:
    """The token, the secret, and the pairing id must never appear in logs."""
    import logging

    client = configured_pairing_client
    ticket = client.app.state.pairings.create()
    with caplog.at_level(logging.DEBUG):
        # Force a failed claim so the log line for the 404 is emitted.
        bad = client.post(
            "/api/pairings/claim",
            json={"pairing_id": "a" * 32, "secret": "b" * 64},
        )
        assert bad.status_code == 404
        # And a successful one for the real ticket.
        ok = client.post(
            "/api/pairings/claim",
            json={"pairing_id": ticket.pairing_id, "secret": ticket.secret},
        )
        assert ok.status_code == 200

    log_blob = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "test-token" not in log_blob
    assert ticket.secret not in log_blob
    assert ticket.pairing_id not in log_blob
