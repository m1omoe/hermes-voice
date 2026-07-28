"""Recurring scheduled Hermes turns.

A schedule is a cadence + a prompt. The executor loop wakes every few seconds,
finds schedules whose next_fire_at has passed, and runs them through the
existing _run_turn path as if the user had asked. Fired turns land in Hermes's
normal session DB and surface in /api/sessions like any other turn.

Phase A scope: store + cron loop + CRUD endpoints. No push notifications and
no Hermes-side tool integration yet — see Phase B and C in
.docs/ai/phases/schedules-spec.md.

The store is SQLite, kept SEPARATE from Hermes's ~/.hermes/state.db. We use
asyncio.to_thread for sync sqlite3 calls to mirror the sessions.py pattern.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger("hermes_voice.schedules")

DEFAULT_DB_PATH = Path.home() / ".hermes-voice" / "schedules.db"

# Hard lower bound on cadence. Below this, runaway misconfigurations could
# burn the backend; above it, every legitimate use case fits.
MIN_CADENCE_SECONDS = 60

# Disable a schedule after this many consecutive failures so a permanently
# broken prompt doesn't keep retrying forever.
MAX_CONSECUTIVE_FAILS = 5

# How often the executor wakes to check for due schedules. With a 60s floor on
# cadence, 5s precision is more than enough.
TICK_SECONDS = 5

# Schedule ids with a fire currently in flight. Guards against the executor
# re-spawning a duplicate on the next tick while a slow Hermes turn (longer than
# TICK_SECONDS) runs — next_fire_at isn't advanced until the turn completes, so
# the row stays "due" until then. _fire_tasks holds strong refs so the
# fire-and-forget tasks aren't garbage-collected (and cancelled) mid-flight.
_in_flight: set[str] = set()
_fire_tasks: set[asyncio.Task] = set()

# Upper bound on schedules firing at once. The _in_flight set dedups a single
# schedule across ticks, but a burst of distinct due schedules would otherwise
# spawn unbounded concurrent Hermes turns. Cap them so a backlog can't swamp
# the backend.
MAX_CONCURRENT_FIRES = 3
_fire_sema = asyncio.Semaphore(MAX_CONCURRENT_FIRES)

# Lock-screen preview length for a fire event. The full assistant text is kept
# separately (full_text) for /api/replay and auto-play.
FIRE_PREVIEW_CHARS = 200

# Bounded retention: keep only the newest N fire events per schedule so the
# table doesn't grow forever. Pruned on every insert.
#
# This DOES mean a client that stops polling long enough can miss intermediate
# events: at the 60s cadence floor a schedule fires ~480x overnight, so with
# background refresh suppressed (Low Power Mode, force-quit) everything but the
# newest N is pruned before the phone next looks.
#
# That is acceptable, and deliberately so: the client collapses to
# latest-per-schedule anyway (see phases/local-notifications-spec.md), and it
# cannot be stranded — the cursor is `seq > after`, so a client holding an old
# cursor still receives every surviving row and still notifies for the newest.
# The user loses intermediate history they would never have been shown.
#
# The consequence to preserve: a client must NEVER treat this feed as a
# complete history. Do not build "you missed N runs" counting, gap detection,
# or anything else assuming continuity on top of it without first adding an
# explicit gap signal — the feed cannot currently distinguish "nothing fired"
# from "older events were pruned".
MAX_FIRE_EVENTS_PER_SCHEDULE = 50


@dataclass
class Schedule:
    id: str
    cadence_seconds: int
    prompt: str
    display_name: str | None
    created_at: float
    last_fired_at: float | None
    next_fire_at: float
    enabled: bool
    consecutive_fails: int
    source: str  # 'ios' | 'voice' | future

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "cadence_seconds": self.cadence_seconds,
            "prompt": self.prompt,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "last_fired_at": self.last_fired_at,
            "next_fire_at": self.next_fire_at,
            "enabled": self.enabled,
            "consecutive_fails": self.consecutive_fails,
            "source": self.source,
        }


@dataclass
class ScheduleFireEvent:
    seq: int
    fire_id: str
    schedule_id: str
    display_name: str | None
    harness_id: str
    session_id: str | None
    preview: str
    full_text: str
    completed_at: float

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "fire_id": self.fire_id,
            "schedule_id": self.schedule_id,
            "display_name": self.display_name,
            "harness_id": self.harness_id,
            "session_id": self.session_id,
            "preview": self.preview,
            "full_text": self.full_text,
            "completed_at": self.completed_at,
        }


def _resolve_path(path: Path | None) -> Path:
    """Resolve a per-call path arg, falling back to the module-level default.

    Reading DEFAULT_DB_PATH here (not as a function default) means tests can
    monkey-patch `schedules.DEFAULT_DB_PATH` and have it take effect on
    subsequent calls — function defaults bind at definition time, this binds
    at call time.
    """
    return path if path is not None else DEFAULT_DB_PATH


def _connect(path: Path | None = None) -> sqlite3.Connection:
    # NOTE: `with sqlite3.connect(...) as conn:` does NOT close the connection
    # — the sqlite3 connection context manager only commits/rolls back the
    # transaction. Every caller MUST wrap this in `contextlib.closing(...)`
    # (stacked with `conn` itself where transaction semantics are also
    # needed) or the file descriptor leaks until GC happens to collect it.
    p = _resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # WAL lets the executor's writers and the API's readers coexist without
    # spurious "database is locked"; busy_timeout waits out brief write
    # contention instead of erroring immediately.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_sync(path: Path | None = None) -> None:
    p = _resolve_path(path)
    with closing(_connect(p)) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schedules (
              id                TEXT    PRIMARY KEY,
              cadence_seconds   INTEGER NOT NULL,
              prompt            TEXT    NOT NULL,
              display_name      TEXT,
              created_at        REAL    NOT NULL,
              last_fired_at     REAL,
              next_fire_at      REAL    NOT NULL,
              enabled           INTEGER NOT NULL DEFAULT 1,
              consecutive_fails INTEGER NOT NULL DEFAULT 0,
              source            TEXT    NOT NULL DEFAULT 'ios'
            );
            CREATE INDEX IF NOT EXISTS schedules_next_fire
              ON schedules(next_fire_at) WHERE enabled = 1;

            -- Device tokens for APNs push delivery. Phase B added.
            -- token is the hex APNs device token; platform is 'ios'/'watchos'.
            CREATE TABLE IF NOT EXISTS devices (
              token          TEXT PRIMARY KEY,
              platform       TEXT NOT NULL,
              bundle_id      TEXT NOT NULL,
              environment    TEXT NOT NULL,
              registered_at  REAL NOT NULL,
              last_seen_at   REAL NOT NULL
            );

            -- Append-only record of every successful scheduled fire, replacing
            -- APNs push as the sync mechanism (local-notifications-spec). This
            -- is a NEW table added via CREATE TABLE IF NOT EXISTS — since
            -- _init_sync runs on every create_app, an existing install's
            -- database picks it up automatically with no migration step.
            CREATE TABLE IF NOT EXISTS schedule_fire_events (
              seq           INTEGER PRIMARY KEY AUTOINCREMENT,
              fire_id       TEXT    NOT NULL,
              schedule_id   TEXT    NOT NULL,
              display_name  TEXT,
              harness_id    TEXT    NOT NULL,
              session_id    TEXT,
              preview       TEXT    NOT NULL,
              full_text     TEXT    NOT NULL,
              completed_at  REAL    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS schedule_fire_events_schedule_id
              ON schedule_fire_events(schedule_id);

            -- fire_id is UNIQUE by index rather than by column constraint:
            -- sqlite cannot ALTER a table to add a constraint, but a unique
            -- index is additive and so lands on databases created before this
            -- line existed — the same reason the table itself needs no
            -- migration. Uniqueness is load-bearing: iOS builds its
            -- notification identifier as scheduleId-fireId, and that is what
            -- makes the poller's deliberate redelivery coalesce instead of
            -- notifying twice. `seq` remains the only cursor identity.
            CREATE UNIQUE INDEX IF NOT EXISTS schedule_fire_events_fire_id
              ON schedule_fire_events(fire_id);
            """
        )
        conn.commit()


async def init_store(path: Path | None = None) -> None:
    await asyncio.to_thread(_init_sync, _resolve_path(path))


def _row_to_schedule(r: sqlite3.Row) -> Schedule:
    return Schedule(
        id=r["id"],
        cadence_seconds=int(r["cadence_seconds"]),
        prompt=r["prompt"],
        display_name=r["display_name"],
        created_at=float(r["created_at"]),
        last_fired_at=(
            float(r["last_fired_at"]) if r["last_fired_at"] is not None else None
        ),
        next_fire_at=float(r["next_fire_at"]),
        enabled=bool(r["enabled"]),
        consecutive_fails=int(r["consecutive_fails"]),
        source=r["source"],
    )


def _row_to_fire_event(r: sqlite3.Row) -> ScheduleFireEvent:
    return ScheduleFireEvent(
        seq=int(r["seq"]),
        fire_id=r["fire_id"],
        schedule_id=r["schedule_id"],
        display_name=r["display_name"],
        harness_id=r["harness_id"],
        session_id=r["session_id"],
        preview=r["preview"],
        full_text=r["full_text"],
        completed_at=float(r["completed_at"]),
    )


# ───────────────────────── CRUD ─────────────────────────


async def list_all(path: Path | None = None) -> list[Schedule]:
    return await asyncio.to_thread(_list_sync, _resolve_path(path))


def _list_sync(path: Path) -> list[Schedule]:
    if not path.exists():
        return []
    with closing(_connect(path)) as conn:
        rows = conn.execute(
            "SELECT * FROM schedules ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_schedule(r) for r in rows]


async def get(id: str, path: Path | None = None) -> Schedule | None:
    return await asyncio.to_thread(_get_sync, id, _resolve_path(path))


def _get_sync(id: str, path: Path) -> Schedule | None:
    if not path.exists():
        return None
    with closing(_connect(path)) as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (id,)).fetchone()
    return _row_to_schedule(row) if row else None


async def create(
    *,
    cadence_seconds: int,
    prompt: str,
    display_name: str | None = None,
    source: str = "ios",
    path: Path | None = None,
) -> Schedule:
    cadence = max(MIN_CADENCE_SECONDS, int(cadence_seconds))
    now = time.time()
    new = Schedule(
        id=uuid.uuid4().hex,
        cadence_seconds=cadence,
        prompt=prompt.strip(),
        display_name=(display_name or None),
        created_at=now,
        last_fired_at=None,
        next_fire_at=now + cadence,  # don't fire instantly — give a full cycle first
        enabled=True,
        consecutive_fails=0,
        source=source,
    )
    await asyncio.to_thread(_insert_sync, new, _resolve_path(path))
    return new


def _insert_sync(s: Schedule, path: Path) -> None:
    with closing(_connect(path)) as conn, conn:
        conn.execute(
            """
            INSERT INTO schedules (
              id, cadence_seconds, prompt, display_name,
              created_at, last_fired_at, next_fire_at,
              enabled, consecutive_fails, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                s.id, s.cadence_seconds, s.prompt, s.display_name,
                s.created_at, s.last_fired_at, s.next_fire_at,
                1 if s.enabled else 0, s.consecutive_fails, s.source,
            ),
        )
        conn.commit()


async def update(
    id: str,
    *,
    cadence_seconds: int | None = None,
    prompt: str | None = None,
    display_name: str | None = None,
    enabled: bool | None = None,
    path: Path | None = None,
) -> Schedule | None:
    return await asyncio.to_thread(
        _update_sync, id, cadence_seconds, prompt, display_name, enabled,
        _resolve_path(path),
    )


def _update_sync(
    id: str,
    cadence_seconds: int | None,
    prompt: str | None,
    display_name: str | None,
    enabled: bool | None,
    path: Path,
) -> Schedule | None:
    if not path.exists():
        return None
    with closing(_connect(path)) as conn, conn:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (id,)).fetchone()
        if row is None:
            return None
        new_cadence = (
            max(MIN_CADENCE_SECONDS, int(cadence_seconds))
            if cadence_seconds is not None
            else int(row["cadence_seconds"])
        )
        new_prompt = prompt.strip() if prompt is not None else row["prompt"]
        new_display = display_name if display_name is not None else row["display_name"]
        new_enabled = enabled if enabled is not None else bool(row["enabled"])

        # If cadence shrunk, recompute next_fire_at relative to last_fired_at
        # (or now if never fired) so the new cadence takes effect immediately.
        new_next = float(row["next_fire_at"])
        if cadence_seconds is not None:
            anchor = (
                float(row["last_fired_at"])
                if row["last_fired_at"] is not None
                else float(row["created_at"])
            )
            new_next = anchor + new_cadence

        conn.execute(
            """
            UPDATE schedules SET
              cadence_seconds = ?,
              prompt = ?,
              display_name = ?,
              enabled = ?,
              next_fire_at = ?,
              consecutive_fails = CASE WHEN ? = 1 THEN 0 ELSE consecutive_fails END
            WHERE id = ?
            """,
            (
                new_cadence,
                new_prompt,
                new_display,
                1 if new_enabled else 0,
                new_next,
                1 if new_enabled else 0,
                id,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (id,)).fetchone()
    return _row_to_schedule(row) if row else None


async def delete(id: str, path: Path | None = None) -> bool:
    return await asyncio.to_thread(_delete_sync, id, _resolve_path(path))


# ───────────────────────── Devices (APNs push targets) ─────────────────────────


@dataclass
class Device:
    token: str
    platform: str
    bundle_id: str
    environment: str
    registered_at: float
    last_seen_at: float


def _device_row(r: sqlite3.Row) -> Device:
    return Device(
        token=r["token"],
        platform=r["platform"],
        bundle_id=r["bundle_id"],
        environment=r["environment"],
        registered_at=float(r["registered_at"]),
        last_seen_at=float(r["last_seen_at"]),
    )


async def upsert_device(
    *,
    token: str,
    platform: str,
    bundle_id: str,
    environment: str,
    path: Path | None = None,
) -> Device:
    return await asyncio.to_thread(
        _upsert_device_sync, token, platform, bundle_id, environment,
        _resolve_path(path),
    )


def _upsert_device_sync(
    token: str, platform: str, bundle_id: str, environment: str, path: Path
) -> Device:
    now = time.time()
    with closing(_connect(path)) as conn, conn:
        existing = conn.execute(
            "SELECT * FROM devices WHERE token = ?", (token,)
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO devices (
                  token, platform, bundle_id, environment,
                  registered_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (token, platform, bundle_id, environment, now, now),
            )
        else:
            conn.execute(
                """
                UPDATE devices SET
                  platform = ?, bundle_id = ?, environment = ?, last_seen_at = ?
                WHERE token = ?
                """,
                (platform, bundle_id, environment, now, token),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM devices WHERE token = ?", (token,)
        ).fetchone()
    return _device_row(row)


async def delete_device(token: str, path: Path | None = None) -> bool:
    return await asyncio.to_thread(_delete_device_sync, token, _resolve_path(path))


def _delete_device_sync(token: str, path: Path) -> bool:
    if not path.exists():
        return False
    with closing(_connect(path)) as conn, conn:
        cur = conn.execute("DELETE FROM devices WHERE token = ?", (token,))
        conn.commit()
        return cur.rowcount > 0


async def list_devices(path: Path | None = None) -> list[Device]:
    return await asyncio.to_thread(_list_devices_sync, _resolve_path(path))


def _list_devices_sync(path: Path) -> list[Device]:
    if not path.exists():
        return []
    with closing(_connect(path)) as conn:
        rows = conn.execute("SELECT * FROM devices").fetchall()
    return [_device_row(r) for r in rows]


def _delete_sync(id: str, path: Path) -> bool:
    if not path.exists():
        return False
    with closing(_connect(path)) as conn, conn:
        cur = conn.execute("DELETE FROM schedules WHERE id = ?", (id,))
        conn.commit()
        return cur.rowcount > 0


# ───────────────────────── Executor ─────────────────────────


async def _fetch_due(now: float, path: Path | None = None) -> list[Schedule]:
    return await asyncio.to_thread(_fetch_due_sync, now, _resolve_path(path))


def _fetch_due_sync(now: float, path: Path) -> list[Schedule]:
    if not path.exists():
        return []
    with closing(_connect(path)) as conn:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_fire_at <= ?",
            (now,),
        ).fetchall()
    return [_row_to_schedule(r) for r in rows]


async def _mark_fired(
    id: str,
    fired_at: float,
    success: bool,
    path: Path | None = None,
    *,
    fire_id: str | None = None,
    display_name: str | None = None,
    harness_id: str | None = None,
    session_id: str | None = None,
    assistant_text: str | None = None,
    completed_at: float | None = None,
) -> None:
    await asyncio.to_thread(
        _mark_fired_sync,
        id, fired_at, success, _resolve_path(path),
        fire_id, display_name, harness_id, session_id, assistant_text, completed_at,
    )


def _mark_fired_sync(
    id: str,
    fired_at: float,
    success: bool,
    path: Path,
    fire_id: str | None = None,
    display_name: str | None = None,
    harness_id: str | None = None,
    session_id: str | None = None,
    assistant_text: str | None = None,
    completed_at: float | None = None,
) -> None:
    with closing(_connect(path)) as conn, conn:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (id,)).fetchone()
        if row is None:
            return
        cadence = int(row["cadence_seconds"])
        if success:
            missing_event_data = (
                fire_id is None or harness_id is None
                or assistant_text is None or completed_at is None
            )
            if missing_event_data:
                raise ValueError(
                    "_mark_fired_sync(success=True) requires fire_id, "
                    "harness_id, assistant_text, and completed_at"
                )
            conn.execute(
                """
                UPDATE schedules SET
                  last_fired_at = ?, next_fire_at = ?, consecutive_fails = 0
                WHERE id = ?
                """,
                (fired_at, fired_at + cadence, id),
            )
            # Append-only fire event, written in the SAME transaction/connection
            # as the success mark above — a second, separate transaction could
            # leave a completed schedule with no event if the process dies
            # between the two writes.
            conn.execute(
                """
                INSERT INTO schedule_fire_events (
                  fire_id, schedule_id, display_name, harness_id,
                  session_id, preview, full_text, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fire_id, id, display_name, harness_id, session_id,
                    assistant_text[:FIRE_PREVIEW_CHARS], assistant_text,
                    completed_at,
                ),
            )
            # Bounded retention: keep only the newest N events for this
            # schedule so the table doesn't grow forever.
            conn.execute(
                """
                DELETE FROM schedule_fire_events
                WHERE schedule_id = ? AND seq NOT IN (
                  SELECT seq FROM schedule_fire_events
                  WHERE schedule_id = ?
                  ORDER BY seq DESC
                  LIMIT ?
                )
                """,
                (id, id, MAX_FIRE_EVENTS_PER_SCHEDULE),
            )
        else:
            new_fails = int(row["consecutive_fails"]) + 1
            # On failure: still advance next_fire_at by cadence so we don't
            # hammer Hermes. Disable after MAX_CONSECUTIVE_FAILS.
            new_enabled = 0 if new_fails >= MAX_CONSECUTIVE_FAILS else 1
            conn.execute(
                """
                UPDATE schedules SET
                  last_fired_at = ?, next_fire_at = ?,
                  consecutive_fails = ?, enabled = ?
                WHERE id = ?
                """,
                (fired_at, fired_at + cadence, new_fails, new_enabled, id),
            )
        conn.commit()


async def list_fire_events(
    after: int = 0, limit: int = 30, path: Path | None = None
) -> list[ScheduleFireEvent]:
    return await asyncio.to_thread(
        _list_fire_events_sync, after, limit, _resolve_path(path)
    )


def _list_fire_events_sync(
    after: int, limit: int, path: Path
) -> list[ScheduleFireEvent]:
    if not path.exists():
        return []
    with closing(_connect(path)) as conn:
        rows = conn.execute(
            """
            SELECT * FROM schedule_fire_events
            WHERE seq > ?
            ORDER BY seq ASC
            LIMIT ?
            """,
            (after, limit),
        ).fetchall()
    return [_row_to_fire_event(r) for r in rows]


async def _fire_one(app: FastAPI, sched: Schedule) -> None:
    """Run one schedule through the existing turn pipeline + send push.

    Imports locally to avoid the schedules ↔ main import cycle.
    """
    from .main import _run_turn
    from .push import send_push

    # Placeholder for the (rare) case an exception hits before we ever get
    # inside the semaphore. Overwritten immediately once acquired below —
    # capturing it AFTER the semaphore is the fix: time spent queued behind
    # MAX_CONCURRENT_FIRES must not count against this fire's interval, or a
    # busy backend keeps shortening the next one.
    fired_at = time.time()
    try:
        async with _fire_sema:
            fired_at = time.time()
            # Skip TTS synthesis: the scheduled turn's audio is never consumed
            # here. The push auto-plays via /api/replay, which re-synthesizes
            # from the stored assistant_text on demand.
            response = await _run_turn(
                app, user_text=sched.prompt, session_id=None, tts_mode="none",
            )
            completed_at = time.time()
            # _run_turn was called without a harness, so it fell back to
            # app.state.default_harness (see main.py:_run_turn) — that's the
            # harness that actually ran this turn, not a hardcoded string.
            await _mark_fired(
                sched.id, fired_at, success=True,
                fire_id=uuid.uuid4().hex,
                display_name=sched.display_name,
                harness_id=app.state.default_harness,
                session_id=response.session_id or None,
                assistant_text=response.assistant_text,
                completed_at=completed_at,
            )
            logger.info(
                "schedule fired id=%s display=%r prompt=%r",
                sched.id, sched.display_name, sched.prompt[:60],
            )

            # Push delivery — best-effort, never blocks the schedule from
            # being marked fired. If APNs isn't configured this is a no-op.
            try:
                settings = app.state.settings
                count = await send_push(
                    settings,
                    body=response.assistant_text,
                    schedule_id=sched.id,
                    session_id=response.session_id,
                )
                if count:
                    logger.info(
                        "push delivered to %d device(s) for schedule=%s", count, sched.id
                    )
            except Exception as e:
                logger.warning("push failed for schedule=%s: %s", sched.id, e)
    except Exception as e:
        # "Skip silently" per the spec — log but no push, no alert.
        #
        # Mark first, then log the count the row ACTUALLY holds. `sched` is a
        # detached snapshot taken by _fetch_due at tick time, so
        # `sched.consecutive_fails + 1` could disagree with the persisted
        # value (_mark_fired_sync re-reads the live row). The write was always
        # right; the log was not — and this line is what an operator reads
        # while diagnosing an outage, so it has to be true.
        await _mark_fired(sched.id, fired_at, success=False)
        current = await get(sched.id, path=None)
        fails = current.consecutive_fails if current else sched.consecutive_fails + 1
        logger.warning(
            "schedule failed id=%s err=%s (fails=%d/%d)",
            sched.id, e, fails, MAX_CONSECUTIVE_FAILS,
        )
    finally:
        _in_flight.discard(sched.id)


async def executor_loop(app: FastAPI) -> None:
    """Background task: poll for due schedules and fire them.

    Loop runs forever until cancelled by the app's lifespan shutdown.
    Each due schedule fires concurrently — slow Hermes call on one
    schedule doesn't delay others.
    """
    logger.info("schedule executor started (tick=%ds)", TICK_SECONDS)
    try:
        while True:
            try:
                due = await _fetch_due(time.time())
                for sched in due:
                    # Skip a schedule still firing from an earlier tick: a slow
                    # turn keeps the row "due" (next_fire_at not yet advanced)
                    # and would otherwise be re-spawned every TICK_SECONDS.
                    if sched.id in _in_flight:
                        continue
                    _in_flight.add(sched.id)
                    task = asyncio.create_task(_fire_one(app, sched))
                    _fire_tasks.add(task)
                    task.add_done_callback(_fire_tasks.discard)
            except Exception as e:
                # Don't let a DB hiccup kill the loop.
                logger.warning("executor tick error: %s", e)
            await asyncio.sleep(TICK_SECONDS)
    except asyncio.CancelledError:
        logger.info("schedule executor cancelled, exiting cleanly")
        raise
