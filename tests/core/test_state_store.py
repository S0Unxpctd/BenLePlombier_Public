"""Unit tests for V3/core/state_store.py.

Covers:
- InMemoryStateStore CRUD
- Atomic update semantics
- Validation (bad channel, non-JSON data)
- Thread-safety under concurrent updates for the same user_id
- PostgresStateStore: injected cursor factory exercises the SQL branches
  without needing a real Postgres connection
"""

from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

V3_ROOT = Path(__file__).resolve().parents[2]
if str(V3_ROOT) not in sys.path:
    sys.path.insert(0, str(V3_ROOT))

from core.state_store import (  # noqa: E402
    CHANNEL_TELEGRAM,
    CHANNEL_WHATSAPP,
    ConversationState,
    InMemoryStateStore,
    MIGRATION_SQL,
    PostgresStateStore,
    VALID_CHANNELS,
    _validate,
    migrate,
)


# ─────────────────────────────────────────────────────────────────────────────
# InMemoryStateStore — CRUD
# ─────────────────────────────────────────────────────────────────────────────


class TestInMemoryCRUD:
    def test_get_missing_returns_none(self):
        store = InMemoryStateStore()
        assert store.get(42) is None

    def test_set_then_get_roundtrip(self):
        store = InMemoryStateStore()
        state = ConversationState(
            user_id=1,
            channel=CHANNEL_TELEGRAM,
            flow="quote",
            step="pre_prix",
            data={"pending_transcription": "bonjour"},
        )
        store.set(state)
        loaded = store.get(1)
        assert loaded is not None
        assert loaded.channel == CHANNEL_TELEGRAM
        assert loaded.flow == "quote"
        assert loaded.data == {"pending_transcription": "bonjour"}
        assert loaded.updated_at is not None

    def test_get_returns_a_copy(self):
        store = InMemoryStateStore()
        state = ConversationState(user_id=1, channel=CHANNEL_TELEGRAM, data={"k": [1, 2]})
        store.set(state)
        loaded = store.get(1)
        loaded.data["k"].append(3)
        # Re-read must not show the mutation.
        again = store.get(1)
        assert again.data["k"] == [1, 2]

    def test_delete_removes(self):
        store = InMemoryStateStore()
        store.set(ConversationState(user_id=1, channel=CHANNEL_TELEGRAM))
        store.delete(1)
        assert store.get(1) is None

    def test_delete_missing_is_silent(self):
        store = InMemoryStateStore()
        # No exception.
        store.delete(9999)


# ─────────────────────────────────────────────────────────────────────────────
# Atomic update
# ─────────────────────────────────────────────────────────────────────────────


class TestUpdate:
    def test_update_creates_if_absent(self):
        store = InMemoryStateStore()

        def mutator(state: ConversationState):
            state.data["hello"] = "world"
            state.flow = "onboarding"

        result = store.update(7, CHANNEL_WHATSAPP, mutator)
        assert result.user_id == 7
        assert result.channel == CHANNEL_WHATSAPP
        assert result.flow == "onboarding"
        assert result.data == {"hello": "world"}

    def test_update_mutates_existing(self):
        store = InMemoryStateStore()
        store.set(ConversationState(user_id=3, channel=CHANNEL_TELEGRAM, data={"n": 1}))

        def mutator(state: ConversationState):
            state.data["n"] += 1

        r1 = store.update(3, CHANNEL_TELEGRAM, mutator)
        r2 = store.update(3, CHANNEL_TELEGRAM, mutator)
        assert r1.data["n"] == 2
        assert r2.data["n"] == 3

    def test_update_rejects_unknown_channel(self):
        store = InMemoryStateStore()
        with pytest.raises(ValueError, match="Unknown channel"):
            store.update(1, "discord", lambda s: None)


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────


class TestValidation:
    def test_set_rejects_unknown_channel(self):
        store = InMemoryStateStore()
        with pytest.raises(ValueError, match="Unknown channel"):
            store.set(ConversationState(user_id=1, channel="myspace"))

    def test_set_rejects_nonserializable_data(self):
        store = InMemoryStateStore()
        state = ConversationState(
            user_id=1,
            channel=CHANNEL_TELEGRAM,
            data={"bad": object()},
        )
        with pytest.raises(ValueError, match="not JSON-serializable"):
            store.set(state)

    def test_valid_channels_are_telegram_and_whatsapp(self):
        assert VALID_CHANNELS == {CHANNEL_TELEGRAM, CHANNEL_WHATSAPP}


# ─────────────────────────────────────────────────────────────────────────────
# Concurrency — required by Phase 1 QA contract item 4
# ─────────────────────────────────────────────────────────────────────────────


class TestConcurrency:
    def test_concurrent_updates_same_user_serialize(self):
        """500 concurrent increments from 10 threads → data['n'] == 500.

        If the per-user lock didn't work we'd see lost updates.
        """
        store = InMemoryStateStore()
        store.set(ConversationState(user_id=1, channel=CHANNEL_TELEGRAM, data={"n": 0}))
        iterations_per_thread = 50
        num_threads = 10

        def worker():
            for _ in range(iterations_per_thread):
                def mutator(state):
                    # Force a race by sleeping briefly between read and write.
                    current = state.data["n"]
                    time.sleep(0.0001)
                    state.data["n"] = current + 1

                store.update(1, CHANNEL_TELEGRAM, mutator)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = store.get(1)
        assert final.data["n"] == num_threads * iterations_per_thread

    def test_concurrent_updates_different_users_dont_block(self):
        """Verifies the per-user lock doesn't serialize across users."""
        store = InMemoryStateStore()
        for uid in range(5):
            store.set(ConversationState(user_id=uid, channel=CHANNEL_TELEGRAM, data={"n": 0}))

        def worker(uid):
            for _ in range(20):
                store.update(
                    uid, CHANNEL_TELEGRAM, lambda s: s.data.__setitem__("n", s.data["n"] + 1)
                )

        threads = [threading.Thread(target=worker, args=(uid,)) for uid in range(5)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.time() - t0

        # All counts must reach exactly 20.
        for uid in range(5):
            assert store.get(uid).data["n"] == 20
        # Sanity: with 5 parallel users × 20 iters, elapsed should be well under 5s.
        assert elapsed < 5.0


# ─────────────────────────────────────────────────────────────────────────────
# PostgresStateStore — exercise SQL branches with a fake cursor
# ─────────────────────────────────────────────────────────────────────────────


class FakeCursor:
    """Minimal psycopg2-cursor stand-in. Records SQL + parameters; returns
    whatever `fetchone` is scripted to return.
    """

    def __init__(self, fetchone_queue=None):
        self.executed: list[tuple[str, tuple]] = []
        self._fetchone_queue = list(fetchone_queue or [])

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone_queue.pop(0) if self._fetchone_queue else None

    def fetchall(self):
        return []


class FakeCursorFactory:
    def __init__(self, cursor: FakeCursor):
        self.cursor = cursor
        self.enters = 0
        self.exits = 0

    @contextmanager
    def __call__(self):
        self.enters += 1
        try:
            yield self.cursor
        finally:
            self.exits += 1


class TestPostgresStateStore:
    def test_get_returns_none_when_no_row(self):
        cur = FakeCursor(fetchone_queue=[None])
        factory = FakeCursorFactory(cur)
        store = PostgresStateStore(cursor_factory=factory)
        assert store.get(42) is None
        assert factory.enters == 1 == factory.exits
        assert "SELECT" in cur.executed[0][0]

    def test_get_parses_row(self):
        row = {
            "user_id": 7,
            "channel": "whatsapp",
            "flow": "quote",
            "step": "pre_prix",
            "data": {"k": "v"},
            "updated_at": None,
        }
        cur = FakeCursor(fetchone_queue=[row])
        store = PostgresStateStore(cursor_factory=FakeCursorFactory(cur))
        s = store.get(7)
        assert s.user_id == 7
        assert s.channel == "whatsapp"
        assert s.flow == "quote"
        assert s.data == {"k": "v"}

    def test_set_issues_upsert(self):
        cur = FakeCursor()
        store = PostgresStateStore(cursor_factory=FakeCursorFactory(cur))
        state = ConversationState(
            user_id=1, channel=CHANNEL_TELEGRAM, flow="x", step="y", data={"a": 1}
        )
        store.set(state)
        assert cur.executed, "expected SQL to be issued"
        sql, params = cur.executed[0]
        assert "INSERT INTO conversation_states" in sql
        assert "ON CONFLICT (user_id)" in sql
        assert params[0] == 1
        assert params[1] == CHANNEL_TELEGRAM
        # data is JSON-serialized for jsonb cast
        assert json.loads(params[4]) == {"a": 1}

    def test_delete_issues_delete(self):
        cur = FakeCursor()
        store = PostgresStateStore(cursor_factory=FakeCursorFactory(cur))
        store.delete(3)
        assert cur.executed[0][0].strip().startswith("DELETE FROM conversation_states")
        assert cur.executed[0][1] == (3,)

    def test_update_uses_select_for_update(self):
        cur = FakeCursor(fetchone_queue=[None])
        store = PostgresStateStore(cursor_factory=FakeCursorFactory(cur))

        def mutator(state: ConversationState):
            state.data["x"] = 1
            state.flow = "q"

        result = store.update(5, CHANNEL_TELEGRAM, mutator)
        assert result.data == {"x": 1}
        assert "SELECT" in cur.executed[0][0]
        assert "FOR UPDATE" in cur.executed[0][0]
        # Then an UPSERT should have been issued
        assert any("INSERT INTO conversation_states" in sql for sql, _ in cur.executed)


# ─────────────────────────────────────────────────────────────────────────────
# Migration
# ─────────────────────────────────────────────────────────────────────────────


class TestMigration:
    def test_migration_sql_mentions_both_tables(self):
        assert "CREATE TABLE IF NOT EXISTS conversation_states" in MIGRATION_SQL

    def test_migrate_runs_migration_sql(self):
        cur = FakeCursor()
        migrate(cursor_factory=FakeCursorFactory(cur))
        assert cur.executed, "migrate() should issue at least one SQL statement"
        assert "conversation_states" in cur.executed[0][0]
