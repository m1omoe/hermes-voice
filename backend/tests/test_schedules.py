"""Tests for the schedules store + CRUD endpoints.

The executor loop tests are kept fast by using a temp DB path injected via
monkeypatch of the schedules module's DEFAULT_DB_PATH and by manually invoking
fire/tick helpers rather than waiting on the real 5s tick.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app import schedules
from tests.conftest import FakeHermes, FakeTTS, build_client


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch) -> Path:
    """Redirect schedules to a per-test SQLite file."""
    db = tmp_path / "schedules.db"
    monkeypatch.setattr(schedules, "DEFAULT_DB_PATH", db)
    return db


# ───────────────────── CRUD via the store directly ─────────────────────


@pytest.mark.asyncio
async def test_create_lists_one_schedule(temp_db: Path) -> None:
    await schedules.init_store(temp_db)
    s = await schedules.create(
        cadence_seconds=60, prompt="weather", display_name="weather", path=temp_db,
    )
    assert s.cadence_seconds == 60
    assert s.enabled is True
    assert s.next_fire_at > time.time()  # in the future

    all_ = await schedules.list_all(temp_db)
    assert len(all_) == 1
    assert all_[0].id == s.id


@pytest.mark.asyncio
async def test_cadence_floor_enforced(temp_db: Path) -> None:
    await schedules.init_store(temp_db)
    # Attempt 30 -> clamped to MIN_CADENCE_SECONDS (60).
    s = await schedules.create(cadence_seconds=30, prompt="hi", path=temp_db)
    assert s.cadence_seconds == schedules.MIN_CADENCE_SECONDS


@pytest.mark.asyncio
async def test_update_changes_cadence_and_pause(temp_db: Path) -> None:
    await schedules.init_store(temp_db)
    s = await schedules.create(cadence_seconds=300, prompt="hi", path=temp_db)

    paused = await schedules.update(s.id, enabled=False, path=temp_db)
    assert paused is not None and paused.enabled is False

    bumped = await schedules.update(s.id, cadence_seconds=120, path=temp_db)
    assert bumped is not None and bumped.cadence_seconds == 120


@pytest.mark.asyncio
async def test_delete_removes_row(temp_db: Path) -> None:
    await schedules.init_store(temp_db)
    s = await schedules.create(cadence_seconds=60, prompt="hi", path=temp_db)
    assert await schedules.delete(s.id, temp_db) is True
    assert await schedules.delete(s.id, temp_db) is False
    assert await schedules.list_all(temp_db) == []


# ───────────────────── CRUD via the FastAPI endpoints ─────────────────────


def test_endpoints_full_lifecycle(temp_db: Path) -> None:
    client = build_client(hermes=FakeHermes(), tts=FakeTTS())

    # Empty list initially.
    r = client.get("/api/schedules")
    assert r.status_code == 200 and r.json() == []

    # Create.
    r = client.post(
        "/api/schedules",
        json={"cadence_seconds": 60, "prompt": "weather", "display_name": "w"},
    )
    assert r.status_code == 200
    created = r.json()
    sid = created["id"]
    assert created["cadence_seconds"] == 60
    assert created["enabled"] is True
    assert created["next_fire_at"] > time.time()

    # List shows it.
    r = client.get("/api/schedules")
    assert len(r.json()) == 1

    # Patch pauses + retunes.
    r = client.patch(
        f"/api/schedules/{sid}",
        json={"enabled": False, "cadence_seconds": 120},
    )
    assert r.status_code == 200
    patched = r.json()
    assert patched["enabled"] is False
    assert patched["cadence_seconds"] == 120

    # Delete.
    r = client.delete(f"/api/schedules/{sid}")
    assert r.status_code == 204

    # 404 on unknown id.
    r = client.delete(f"/api/schedules/{sid}")
    assert r.status_code == 404
    r = client.patch(f"/api/schedules/{sid}", json={"enabled": True})
    assert r.status_code == 404


def test_create_rejects_sub_60_cadence_at_api_layer(temp_db: Path) -> None:
    client = build_client(hermes=FakeHermes(), tts=FakeTTS())
    r = client.post(
        "/api/schedules", json={"cadence_seconds": 10, "prompt": "x"}
    )
    # Pydantic ge=60 makes this a 422 before reaching the store.
    assert r.status_code == 422


# ───────────────────── Executor: due-schedule firing ─────────────────────


@pytest.mark.asyncio
async def test_fetch_due_returns_only_overdue_enabled(temp_db: Path) -> None:
    await schedules.init_store(temp_db)
    past = await schedules.create(cadence_seconds=60, prompt="overdue", path=temp_db)
    # Force its next_fire_at into the past.
    await schedules.update(
        past.id, cadence_seconds=60, path=temp_db
    )
    # Now manually rewind next_fire_at via raw SQL.
    import sqlite3
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "UPDATE schedules SET next_fire_at = ? WHERE id = ?",
            (time.time() - 1, past.id),
        )
        conn.commit()

    future = await schedules.create(cadence_seconds=600, prompt="future", path=temp_db)
    paused = await schedules.create(cadence_seconds=60, prompt="paused", path=temp_db)
    await schedules.update(paused.id, enabled=False, path=temp_db)
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "UPDATE schedules SET next_fire_at = ? WHERE id = ?",
            (time.time() - 1, paused.id),
        )
        conn.commit()

    due = await schedules._fetch_due(time.time(), temp_db)
    due_ids = {s.id for s in due}
    assert past.id in due_ids
    assert future.id not in due_ids
    assert paused.id not in due_ids  # paused is skipped


@pytest.mark.asyncio
async def test_mark_fired_advances_next_fire_at(temp_db: Path) -> None:
    await schedules.init_store(temp_db)
    s = await schedules.create(cadence_seconds=60, prompt="hi", path=temp_db)

    fired_at = time.time()
    await schedules._mark_fired(
        s.id, fired_at, success=True, path=temp_db,
        fire_id="fire-1", display_name=s.display_name, harness_id="hermes",
        session_id=None, assistant_text="ok", completed_at=fired_at,
    )

    updated = await schedules.get(s.id, temp_db)
    assert updated is not None
    assert updated.last_fired_at == pytest.approx(fired_at)
    assert updated.next_fire_at == pytest.approx(fired_at + 60)
    assert updated.consecutive_fails == 0


@pytest.mark.asyncio
async def test_mark_fired_success_requires_event_fields(temp_db: Path) -> None:
    """A success mark without the event payload is a programming error, not a
    silent no-op — it must raise rather than risk a completed schedule with
    no corresponding fire event."""
    await schedules.init_store(temp_db)
    s = await schedules.create(cadence_seconds=60, prompt="hi", path=temp_db)

    with pytest.raises(ValueError):
        await schedules._mark_fired(s.id, time.time(), success=True, path=temp_db)


@pytest.mark.asyncio
async def test_failures_disable_after_max(temp_db: Path) -> None:
    await schedules.init_store(temp_db)
    s = await schedules.create(cadence_seconds=60, prompt="hi", path=temp_db)

    for _ in range(schedules.MAX_CONSECUTIVE_FAILS):
        await schedules._mark_fired(s.id, time.time(), success=False, path=temp_db)

    updated = await schedules.get(s.id, temp_db)
    assert updated is not None
    assert updated.enabled is False
    assert updated.consecutive_fails == schedules.MAX_CONSECUTIVE_FAILS


# ───────────────────── End-to-end: due schedule actually fires a turn ─────────────────────


@pytest.mark.asyncio
async def test_due_schedule_invokes_hermes(temp_db: Path) -> None:
    """Stub _fire_one's _run_turn call site by hitting the real one with a fake Hermes."""
    fake_hermes = FakeHermes(reply="42 degrees and breezy")
    fake_tts = FakeTTS()
    client = build_client(hermes=fake_hermes, tts=fake_tts)
    app = client.app

    s = await schedules.create(
        cadence_seconds=60, prompt="weather", path=temp_db
    )
    # Force into past so the next tick fires it.
    import sqlite3
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "UPDATE schedules SET next_fire_at = ? WHERE id = ?",
            (time.time() - 1, s.id),
        )
        conn.commit()

    # Run one tick manually (the lifespan loop is also running but we don't
    # want to depend on its 5s cadence in a test).
    due = await schedules._fetch_due(time.time(), temp_db)
    assert len(due) == 1
    await schedules._fire_one(app, due[0])

    # Verify Hermes saw the prompt.
    assert fake_hermes.calls
    prompt, session_id = fake_hermes.calls[-1]
    assert "weather" in prompt
    assert session_id is None  # schedules always start a new Hermes session

    # And the schedule's next_fire_at advanced.
    updated = await schedules.get(s.id, temp_db)
    assert updated is not None
    assert updated.next_fire_at > time.time()
    assert updated.consecutive_fails == 0


@pytest.mark.asyncio
async def test_fire_skips_tts_synthesis(temp_db: Path) -> None:
    """A scheduled fire never synthesizes audio — the push re-synthesizes on replay."""
    fake_hermes = FakeHermes(reply="42 degrees and breezy")
    fake_tts = FakeTTS()
    client = build_client(hermes=fake_hermes, tts=fake_tts)
    app = client.app

    s = await schedules.create(cadence_seconds=60, prompt="weather", path=temp_db)
    await schedules._fire_one(app, s)

    # Hermes ran, but the TTS provider recorded zero synth calls.
    assert fake_hermes.calls
    assert fake_tts.calls == []


@pytest.mark.asyncio
async def test_semaphore_caps_concurrent_fires(temp_db: Path) -> None:
    """No more than MAX_CONCURRENT_FIRES schedules reach Hermes at once."""
    from app.schedules import MAX_CONCURRENT_FIRES

    gate = asyncio.Event()
    in_hermes = 0
    max_seen = 0

    class GatedHermes(FakeHermes):
        async def ask(self, prompt, session_id=None):
            nonlocal in_hermes, max_seen
            in_hermes += 1
            max_seen = max(max_seen, in_hermes)
            await gate.wait()
            in_hermes -= 1
            return await super().ask(prompt, session_id=session_id)

    client = build_client(hermes=GatedHermes(), tts=FakeTTS())
    app = client.app

    n = MAX_CONCURRENT_FIRES + 2
    scheds = [
        await schedules.create(cadence_seconds=60, prompt=f"p{i}", path=temp_db)
        for i in range(n)
    ]

    fires = [asyncio.create_task(schedules._fire_one(app, s)) for s in scheds]

    # Let the first wave reach (and block in) Hermes, then assert the cap held.
    for _ in range(50):
        await asyncio.sleep(0)
        if in_hermes >= MAX_CONCURRENT_FIRES:
            break
    # Exactly MAX get through — proves both bounds: not more (the cap holds) and
    # not fewer (the cap is reachable, so a regression to a too-small cap fails).
    assert max_seen == MAX_CONCURRENT_FIRES
    assert in_hermes == MAX_CONCURRENT_FIRES  # the rest are blocked on the sema

    # Release the gate and let everything drain.
    gate.set()
    await asyncio.gather(*fires)
    assert max_seen <= MAX_CONCURRENT_FIRES


@pytest.mark.asyncio
async def test_fired_at_captured_after_semaphore_not_before(
    temp_db: Path, monkeypatch
) -> None:
    """fired_at (and therefore next_fire_at) must be measured after the
    semaphore is acquired, not at _fire_one's invocation — otherwise time
    spent queued behind MAX_CONCURRENT_FIRES silently shortens the next
    interval. Swap in a fake semaphore with a slow __aenter__ to simulate that
    queue delay deterministically."""

    class SlowGate:
        async def __aenter__(self):
            await asyncio.sleep(0.1)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(schedules, "_fire_sema", SlowGate())

    await schedules.init_store(temp_db)
    s = await schedules.create(cadence_seconds=60, prompt="hi", path=temp_db)

    client = build_client(hermes=FakeHermes(reply="ok"), tts=FakeTTS())
    app = client.app

    before = time.time()
    await schedules._fire_one(app, s)

    updated = await schedules.get(s.id, temp_db)
    assert updated is not None and updated.last_fired_at is not None
    # The fix: fired_at reflects time AFTER the ~0.1s semaphore wait. Before
    # the fix, fired_at was captured at `before` and this would fail.
    assert updated.last_fired_at - before >= 0.05


# ───────────────────── Fire events (schedule sync) ─────────────────────


@pytest.mark.asyncio
async def test_fire_writes_event_on_success(temp_db: Path) -> None:
    fake_hermes = FakeHermes(reply="42 degrees and breezy, with a chance of more text")
    client = build_client(hermes=fake_hermes, tts=FakeTTS())
    app = client.app

    s = await schedules.create(
        cadence_seconds=60, prompt="weather", display_name="Weather", path=temp_db,
    )
    await schedules._fire_one(app, s)

    events = await schedules.list_fire_events(path=temp_db)
    assert len(events) == 1
    ev = events[0]
    assert ev.fire_id  # generated, non-empty
    assert ev.schedule_id == s.id
    assert ev.display_name == "Weather"
    # _run_turn was called without a harness, so it fell back to
    # app.state.default_harness — that's the harness that actually ran.
    assert ev.harness_id == app.state.default_harness
    assert ev.full_text == fake_hermes._reply
    assert ev.preview == fake_hermes._reply[: schedules.FIRE_PREVIEW_CHARS]
    assert ev.completed_at > 0


@pytest.mark.asyncio
async def test_fire_event_display_name_survives_later_rename(temp_db: Path) -> None:
    """The event's display_name is a snapshot at fire time — a later rename
    of the schedule must not relabel history."""
    fake_hermes = FakeHermes(reply="ok")
    client = build_client(hermes=fake_hermes, tts=FakeTTS())
    app = client.app

    s = await schedules.create(
        cadence_seconds=60, prompt="p", display_name="Original Name", path=temp_db,
    )
    await schedules._fire_one(app, s)
    await schedules.update(s.id, display_name="Renamed", path=temp_db)

    events = await schedules.list_fire_events(path=temp_db)
    assert len(events) == 1
    assert events[0].display_name == "Original Name"


@pytest.mark.asyncio
async def test_fire_writes_no_event_on_failure(temp_db: Path) -> None:
    class FailingHermes(FakeHermes):
        async def ask(self, prompt, session_id=None):
            raise RuntimeError("boom")

    client = build_client(hermes=FailingHermes(), tts=FakeTTS())
    app = client.app

    s = await schedules.create(cadence_seconds=60, prompt="weather", path=temp_db)
    await schedules._fire_one(app, s)

    updated = await schedules.get(s.id, temp_db)
    assert updated is not None
    assert updated.consecutive_fails == 1

    assert await schedules.list_fire_events(path=temp_db) == []


@pytest.mark.asyncio
async def test_fire_events_after_cursor_returns_only_newer_ascending(
    temp_db: Path,
) -> None:
    await schedules.init_store(temp_db)
    s = await schedules.create(cadence_seconds=60, prompt="hi", path=temp_db)

    for i in range(5):
        await schedules._mark_fired(
            s.id, time.time(), success=True, path=temp_db,
            fire_id=f"fire-{i}", display_name=s.display_name, harness_id="hermes",
            session_id=None, assistant_text=f"text-{i}", completed_at=time.time(),
        )

    all_events = await schedules.list_fire_events(limit=100, path=temp_db)
    assert len(all_events) == 5
    seqs = [e.seq for e in all_events]
    assert seqs == sorted(seqs)

    cursor = all_events[2].seq
    newer = await schedules.list_fire_events(after=cursor, limit=100, path=temp_db)
    assert [e.seq for e in newer] == seqs[3:]
    assert all(e.seq > cursor for e in newer)


@pytest.mark.asyncio
async def test_schedule_fires_endpoint_paginates(temp_db: Path) -> None:
    fake_hermes = FakeHermes(reply="ok")
    client = build_client(hermes=fake_hermes, tts=FakeTTS())
    app = client.app

    s = await schedules.create(cadence_seconds=60, prompt="p", path=temp_db)
    for _ in range(3):
        await schedules._fire_one(app, s)

    r = client.get("/api/schedule-fires")
    assert r.status_code == 200
    events = r.json()
    assert len(events) == 3
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)

    r2 = client.get(f"/api/schedule-fires?after={seqs[0]}")
    assert r2.status_code == 200
    newer = r2.json()
    assert [e["seq"] for e in newer] == seqs[1:]


@pytest.mark.asyncio
async def test_fire_events_retention_prunes_oldest(temp_db: Path) -> None:
    await schedules.init_store(temp_db)
    s = await schedules.create(cadence_seconds=60, prompt="hi", path=temp_db)

    total = schedules.MAX_FIRE_EVENTS_PER_SCHEDULE + 10
    for i in range(total):
        await schedules._mark_fired(
            s.id, time.time(), success=True, path=temp_db,
            fire_id=f"fire-{i}", display_name=s.display_name, harness_id="hermes",
            session_id=None, assistant_text=f"text-{i}", completed_at=time.time(),
        )

    remaining = await schedules.list_fire_events(limit=10_000, path=temp_db)
    assert len(remaining) == schedules.MAX_FIRE_EVENTS_PER_SCHEDULE
    fire_ids = {e.fire_id for e in remaining}
    # Oldest pruned, newest retained.
    assert "fire-0" not in fire_ids
    assert f"fire-{total - 1}" in fire_ids


def test_fire_events_table_created_on_pre_existing_db(tmp_path: Path) -> None:
    """Regression guard: an existing install's schedules.db (created before
    this feature shipped) must pick up schedule_fire_events the next time
    _init_sync runs — via plain CREATE TABLE IF NOT EXISTS, no migration
    framework. Simulate that by hand-rolling the OLD schema first."""
    import sqlite3

    db_path = tmp_path / "old_schedules.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE schedules (
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
            CREATE TABLE devices (
              token          TEXT PRIMARY KEY,
              platform       TEXT NOT NULL,
              bundle_id      TEXT NOT NULL,
              environment    TEXT NOT NULL,
              registered_at  REAL NOT NULL,
              last_seen_at   REAL NOT NULL
            );
            """
        )
        conn.commit()

    with sqlite3.connect(db_path) as conn:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "schedule_fire_events" not in tables

    # The regression under test: running init on a pre-existing old-schema DB.
    schedules._init_sync(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "schedule_fire_events" in tables

        conn.execute(
            """
            INSERT INTO schedule_fire_events (
              fire_id, schedule_id, display_name, harness_id,
              session_id, preview, full_text, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("fire-x", "sched-x", "name", "hermes", None, "prev", "full", time.time()),
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM schedule_fire_events"
        ).fetchone()[0]
    assert count == 1


# ───────────────────── Devices (Phase B) ─────────────────────


@pytest.mark.asyncio
async def test_device_upsert_idempotent(temp_db: Path) -> None:
    await schedules.init_store(temp_db)

    d1 = await schedules.upsert_device(
        token="abc123", platform="ios",
        bundle_id="dev.finklea.hermesvoice", environment="sandbox",
        path=temp_db,
    )
    assert d1.token == "abc123"
    assert d1.registered_at > 0

    # Re-upsert should not duplicate; should update last_seen_at.
    d2 = await schedules.upsert_device(
        token="abc123", platform="ios",
        bundle_id="dev.finklea.hermesvoice", environment="sandbox",
        path=temp_db,
    )
    assert d2.token == "abc123"
    assert d2.last_seen_at >= d1.last_seen_at

    all_devices = await schedules.list_devices(temp_db)
    assert len(all_devices) == 1


@pytest.mark.asyncio
async def test_device_delete(temp_db: Path) -> None:
    await schedules.init_store(temp_db)
    await schedules.upsert_device(
        token="t1", platform="ios", bundle_id="x", environment="sandbox",
        path=temp_db,
    )
    assert await schedules.delete_device("t1", temp_db) is True
    assert await schedules.delete_device("t1", temp_db) is False


def test_devices_endpoint_roundtrip(temp_db: Path) -> None:
    client = build_client(hermes=FakeHermes(), tts=FakeTTS())

    r = client.post("/api/devices", json={
        "token": "a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4",
        "platform": "ios",
        "bundle_id": "dev.finklea.hermesvoice",
        "environment": "sandbox",
    })
    assert r.status_code == 200
    assert r.json()["token"] == "a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4"

    # Re-register same token returns 200 again (upsert).
    r = client.post("/api/devices", json={
        "token": "a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4",
        "platform": "ios",
        "bundle_id": "dev.finklea.hermesvoice",
        "environment": "sandbox",
    })
    assert r.status_code == 200

    # Unregister.
    r = client.delete(
        "/api/devices/a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4"
    )
    assert r.status_code == 204
    # Unregister again — idempotent 204.
    r = client.delete(
        "/api/devices/a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4"
    )
    assert r.status_code == 204


def test_devices_endpoint_validates_platform(temp_db: Path) -> None:
    client = build_client(hermes=FakeHermes(), tts=FakeTTS())
    r = client.post("/api/devices", json={
        "token": "x" * 16,
        "platform": "android",  # not in the (ios|watchos) pattern
        "bundle_id": "dev.finklea.hermesvoice",
        "environment": "sandbox",
    })
    assert r.status_code == 422


# ───────────────────── Push delivery (Phase B) ─────────────────────


@pytest.mark.asyncio
async def test_send_push_noop_when_apns_unconfigured(temp_db: Path) -> None:
    """If APNS_KEY_PATH is empty, push silently no-ops with 0 devices delivered."""
    from app.config import get_settings
    from app.push import send_push

    # Default settings have empty apns_key_path.
    settings = get_settings()
    sent = await send_push(
        settings,
        body="hello world",
        schedule_id="sched-x",
        session_id="sess-y",
    )
    assert sent == 0


@pytest.mark.asyncio
async def test_send_push_skipped_when_key_file_missing(
    temp_db: Path, monkeypatch
) -> None:
    """Even with key id + team id set, if the .p8 file is missing we skip cleanly."""
    monkeypatch.setenv("APNS_KEY_PATH", "/nonexistent/key.p8")
    monkeypatch.setenv("APNS_KEY_ID", "ABCDEFGHIJ")
    monkeypatch.setenv("APNS_TEAM_ID", "1234567890")
    from app.config import get_settings, reset_settings_cache
    from app.push import is_configured, send_push

    reset_settings_cache()
    settings = get_settings()

    assert is_configured(settings) is False
    sent = await send_push(
        settings,
        body="hi",
        schedule_id="sched-x",
        session_id="sess-y",
    )
    assert sent == 0
