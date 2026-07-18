"""One-time pairing ticket store.

A pairing ticket is a short-lived (default 5-minute), single-use credential
exchanged over HTTPS to bootstrap a new device. The QR code on the user's
Mac carries a high-entropy `pairing_id` and `secret`; the iOS app posts them
to `POST /api/pairings/claim` and receives the long-lived backend token
in return.

The store is SQLite, kept SEPARATE from Hermes's ~/.hermes/state.db and
from the schedules store (~/.hermes-voice/schedules.db). We use the same
sync-sqlite3 + asyncio.to_thread pattern as `schedules.py`.

**Threat model.** The HTTP route is the only thing that can surrender the
token, and it only does so when (a) the request transport is HTTPS, (b) TLS
is configured, and (c) the ticket is in the store, unclaimed, and not expired. To keep a stolen
DB file from being useful, we never persist the raw secret — only a
SHA-256 verifier. The route compares with `secrets.compare_digest` to
avoid leaking the secret via timing.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("hermes_voice.pairing")

DEFAULT_DB_PATH = Path.home() / ".hermes-voice" / "pairings.db"

# Five-minute default — long enough for a human to scan a QR, short enough
# that a captured screenshot expires before a thief can act on it.
DEFAULT_TTL_SECONDS = 300

# Field sizes. 16 bytes of entropy → ~22 base64url chars; we keep some slack
# above that to make the assertion ("we are not regressing to uuid4") easy.
PAIRING_ID_BYTES = 16
SECRET_BYTES = 32

# Read later than write: WAL + busy_timeout keep reads non-blocking; if
# something else is mid-write, we'd rather wait than 500 the iOS client.
_BUSY_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class PairingTicket:
    pairing_id: str
    secret: str
    expires_at: float

    def as_dict(self) -> dict:
        return {
            "pairing_id": self.pairing_id,
            "secret": self.secret,
            "expires_at": self.expires_at,
        }


def _resolve_path(path: Path | None) -> Path:
    """Per-call path arg, falling back to the module-level default. Reading
    DEFAULT_DB_PATH here (not as a function default) means tests can
    monkey-patch `pairing.DEFAULT_DB_PATH` and have it take effect."""
    return path if path is not None else DEFAULT_DB_PATH


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn


def _init_sync(path: Path) -> None:
    with _connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pairings (
              pairing_id   TEXT    PRIMARY KEY,
              secret_hash  TEXT    NOT NULL,
              created_at   REAL    NOT NULL,
              expires_at   REAL    NOT NULL,
              claimed_at   REAL
            );
            -- Pruning scans by expiry, so an index keeps the O(n) walk cheap
            -- even if the table grows (e.g. from a misbehaving CLI).
            CREATE INDEX IF NOT EXISTS pairings_expires
              ON pairings(expires_at);
            """
        )
        conn.commit()


async def init_store(path: Path | None = None) -> None:
    await asyncio.to_thread(_init_sync, _resolve_path(path))


def _hash_secret(secret: str) -> str:
    """SHA-256 hex of the secret — what we actually persist."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _gen_id_and_secret() -> tuple[str, str]:
    """Two independent 16/32-byte secrets. Using `token_urlsafe` (not
    `uuid4`) is what gives us the entropy property the tests assert on."""
    return (
        secrets.token_urlsafe(PAIRING_ID_BYTES),
        secrets.token_urlsafe(SECRET_BYTES),
    )


class PairingStore:
    """Sync, single-host pairing-ticket store.

    Methods are sync because sqlite3 is; tests that want async can wrap
    with `asyncio.to_thread` (the schedules module's pattern). The HTTP
    route is a thin async wrapper that does exactly that.

    `now` is injected so tests can drive the clock without sleeping.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._path = _resolve_path(path)
        self._now = now
        # Initialize the schema eagerly so a fresh DB file is created on
        # first use — matches the schedules store's behavior and removes
        # a class of "first claim races first create" footguns.
        _init_sync(self._path)

    # ── public API ───────────────────────────────────────────────────

    def create(self, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> PairingTicket:
        """Generate a fresh, unconsumed ticket and persist it.

        Returns the *raw* secret to the caller (who is the only entity
        that should see it — the provisioner prints it to a QR, the
        server keeps only the verifier)."""
        pairing_id, secret = _gen_id_and_secret()
        created_at = self._now()
        expires_at = created_at + ttl_seconds
        with _connect(self._path) as conn:
            conn.execute(
                """
                INSERT INTO pairings (
                  pairing_id, secret_hash, created_at, expires_at, claimed_at
                ) VALUES (?, ?, ?, ?, NULL)
                """,
                (pairing_id, _hash_secret(secret), created_at, expires_at),
            )
            conn.commit()
        return PairingTicket(
            pairing_id=pairing_id, secret=secret, expires_at=expires_at,
        )

    def claim(self, pairing_id: str, secret: str) -> bool:
        """Atomically claim a ticket. Returns True exactly once.

        Atomicity comes from BEGIN IMMEDIATE (write lock from the start of
        the txn, so two concurrent claimers serialize) plus the WHERE guard
        `claimed_at IS NULL AND expires_at > now` — the second writer sees
        claimed_at != NULL and the UPDATE affects 0 rows. Returns False
        on every path that isn't a successful first claim (unknown id,
        wrong secret, expired, replay)."""
        if not pairing_id or not secret:
            return False
        verifier = _hash_secret(secret)
        now = self._now()
        with _connect(self._path) as conn:
            # BEGIN IMMEDIATE acquires the write lock up front. Without it,
            # two transactions could both read claimed_at IS NULL, both
            # proceed to UPDATE, and one would silently overwrite the other.
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT secret_hash, claimed_at, expires_at "
                    "FROM pairings WHERE pairing_id = ?",
                    (pairing_id,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return False
                # Constant-time comparison defends against timing oracles
                # that would otherwise let an attacker recover the verifier
                # byte-by-byte.
                if not secrets.compare_digest(row["secret_hash"], verifier):
                    conn.execute("ROLLBACK")
                    return False
                if row["claimed_at"] is not None:
                    conn.execute("ROLLBACK")
                    return False
                if float(row["expires_at"]) <= now:
                    conn.execute("ROLLBACK")
                    return False
                # Exactly one of N concurrent claims survives this UPDATE:
                # the row's WHERE clause requires claimed_at IS NULL, which
                # the previous claim has already set.
                cur = conn.execute(
                    "UPDATE pairings SET claimed_at = ? "
                    "WHERE pairing_id = ? AND claimed_at IS NULL "
                    "  AND expires_at > ?",
                    (now, pairing_id, now),
                )
                conn.execute("COMMIT")
                return cur.rowcount == 1
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def prune_expired(self) -> int:
        """Remove rows whose expiry has passed. Returns the count removed.

        Called while `create_app` initializes; safe to call concurrently
        with claim() (claims ignore claimed_at/expires_at on rows that are
        already past expiry)."""
        now = self._now()
        with _connect(self._path) as conn:
            cur = conn.execute(
                "DELETE FROM pairings WHERE expires_at <= ?", (now,),
            )
            conn.commit()
            return cur.rowcount
