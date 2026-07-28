"""Regression test for the production connection-leak outage.

`with sqlite3.connect(...) as conn:` does NOT close `conn` — the sqlite3
connection context manager only commits or rolls back the pending
transaction; the underlying connection (and its file descriptor) stays open
until something explicitly calls `.close()`. On the live backend this leaked
one connection per `_connect()` call across schedules.py, pairing.py, and
sessions.py, and the schedules executor's 5s tick was frequent enough to
exhaust the process's file-descriptor limit within ~18h, taking the backend
down (accept() started failing with EMFILE while the event loop stayed
"idle").

This test proves the fix holds by patching `sqlite3.connect` to inject a
`sqlite3.Connection` subclass that records every `.close()` call, exercising
a representative operation from each of the three affected stores, and then
asserting `.close()` was actually invoked on every connection handed out.

Note on method: we track `.close()` calls directly rather than probing
"is this connection usable?" after the fact, because all three stores run
their sync sqlite3 code via `asyncio.to_thread` — a connection is
thread-affine (`check_same_thread=True`, the default), so calling `.execute()`
on it from the test's thread raises `sqlite3.ProgrammingError` for "wrong
thread" regardless of whether it was ever closed, which would make an
after-the-fact probe from the test body an unreliable (false-positive-prone)
check. Recording `.close()` calls has no such issue, since `.close()` always
runs on the same thread that created the connection — the thread the
production code (and `contextlib.closing`) actually runs on.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import pairing, schedules, sessions


@pytest.fixture
def tracked_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[sqlite3.Connection], set[int]]:
    """Patch sqlite3.connect to hand out a Connection subclass that records
    its own id() whenever `.close()` is called, plus a list of every
    connection object created.

    schedules.py, pairing.py, and sessions.py each call `sqlite3.connect(...)`
    via attribute lookup on the `sqlite3` module at call time (not a name
    imported at definition time), so patching the module attribute here
    intercepts connections opened by all three.
    """
    conns: list[sqlite3.Connection] = []
    closed_ids: set[int] = set()

    class _TrackingConnection(sqlite3.Connection):
        def close(self) -> None:
            closed_ids.add(id(self))
            super().close()

    real_connect = sqlite3.connect

    def _wrapped(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs.setdefault("factory", _TrackingConnection)
        conn = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        conns.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", _wrapped)
    return conns, closed_ids


@pytest.mark.asyncio
async def test_no_leaked_connections_across_stores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracked_connections: tuple[list[sqlite3.Connection], set[int]],
) -> None:
    """Exercise a create, a read, a list, a mark-fired, a pairing claim, and a
    session read, then assert every sqlite3.connect() call was closed. A
    future `with _connect(...) as conn:` added anywhere in the three modules
    (without `closing`) would leave its connection unclosed and fail this
    assertion.
    """
    conns, closed_ids = tracked_connections

    # ---- schedules.py: create, get, list, mark-fired ----
    schedules_db = tmp_path / "schedules.db"
    monkeypatch.setattr(schedules, "DEFAULT_DB_PATH", schedules_db)
    await schedules.init_store(schedules_db)
    sched = await schedules.create(
        cadence_seconds=60, prompt="hi", path=schedules_db,
    )
    await schedules.list_all(schedules_db)
    await schedules.get(sched.id, schedules_db)
    await schedules._mark_fired(
        sched.id, 1000.0, success=False, path=schedules_db,
    )

    # ---- pairing.py: create + claim ----
    pairing_db = tmp_path / "pairings.db"
    store = pairing.PairingStore(pairing_db, now=lambda: 1000.0)
    ticket = store.create(ttl_seconds=300)
    assert store.claim(ticket.pairing_id, ticket.secret) is True

    # ---- sessions.py: read against a Hermes-shaped state.db ----
    state_db = tmp_path / "state.db"
    seed_conn = sqlite3.connect(state_db)
    try:
        seed_conn.executescript(
            """
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY, source TEXT, started_at REAL, title TEXT,
              message_count INTEGER, tool_call_count INTEGER
            );
            CREATE TABLE messages (
              id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
              timestamp REAL, tool_name TEXT, tool_calls TEXT
            );
            INSERT INTO sessions VALUES ('s1', 'ios', 1000.0, 'Title', 1, 0);
            INSERT INTO messages
              VALUES (1, 's1', 'user', 'hi', 1000.0, NULL, NULL);
            """
        )
        seed_conn.commit()
    finally:
        seed_conn.close()

    await sessions.list_sessions(db_path=state_db)
    detail = await sessions.get_session("s1", db_path=state_db)
    assert detail is not None

    # Sanity: make sure the patched sqlite3.connect actually captured the
    # calls above (schedules init/create/list/get/mark-fired, pairing
    # init/create/claim, sessions seed/list/get) before trusting the
    # "nothing leaked" assertion below.
    assert len(conns) >= 10

    unclosed = [c for c in conns if id(c) not in closed_ids]
    assert not unclosed, (
        f"{len(unclosed)} of {len(conns)} sqlite3 connection(s) were never "
        "closed (missing contextlib.closing somewhere in schedules.py, "
        "pairing.py, or sessions.py)"
    )
