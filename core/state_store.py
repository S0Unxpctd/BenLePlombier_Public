"""Souffl.AI V3 — Conversation state store (transport-agnostic).

This module replaces the Telegram-specific `context.user_data` with a durable,
channel-aware store backed by Postgres. It is used by both the Telegram adapter
(`V3/bot.py`) and the WhatsApp adapter (Phase 2, `V3/whatsapp/`).

Design
------
- One row per user, keyed by `(channel, user_id)`. The primary key is `user_id`
  alone for simplicity (matches the spec in `V3_BRIEFS/4_TECHNICAL_REFERENCE.md`);
  `channel` lives as a column so the same user ID namespace can be preserved
  across channels via a hash strategy (see the WhatsApp adapter in Phase 2).
- The `data` column is a JSONB blob: the exact dict shape the bot already puts
  in `context.user_data` (pending_transcription, pre_prix_values, etc.). No
  pre-modeling — the bot decides the shape.
- `flow` and `step` are nullable strings for dashboard visibility; writers are
  encouraged to set them but it's not mandatory.
- Atomic read-modify-write through `update_state()` using `SELECT ... FOR
  UPDATE` to avoid lost updates when two messages land concurrently.
- The store exposes an in-memory backend (`InMemoryStateStore`) for tests.

Schema (created by `migrate()`)
-------------------------------
    CREATE TABLE conversation_states (
        user_id    BIGINT PRIMARY KEY,
        channel    TEXT NOT NULL CHECK (channel IN ('telegram', 'whatsapp')),
        flow       TEXT,
        step       TEXT,
        data       JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_conv_states_channel ON conversation_states(channel);
"""

from __future__ import annotations

import abc
import copy
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Public types
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ConversationState:
    """Snapshot of a user's conversational state at a point in time."""

    user_id: int
    channel: str
    flow: Optional[str] = None
    step: Optional[str] = None
    data: dict = field(default_factory=dict)
    updated_at: Optional[datetime] = None

    def copy(self) -> "ConversationState":
        return ConversationState(
            user_id=self.user_id,
            channel=self.channel,
            flow=self.flow,
            step=self.step,
            data=copy.deepcopy(self.data),
            updated_at=self.updated_at,
        )


CHANNEL_TELEGRAM = "telegram"
CHANNEL_WHATSAPP = "whatsapp"
VALID_CHANNELS = frozenset({CHANNEL_TELEGRAM, CHANNEL_WHATSAPP})


# ─────────────────────────────────────────────────────────────────────────────
# Store interface
# ─────────────────────────────────────────────────────────────────────────────


class StateStore(abc.ABC):
    """Abstract conversation state store. Implementations are Postgres or in-memory."""

    @abc.abstractmethod
    def get(self, user_id: int) -> Optional[ConversationState]:
        """Return the current state for `user_id`, or None if absent."""

    @abc.abstractmethod
    def set(self, state: ConversationState) -> None:
        """Unconditionally write `state`. Upsert semantics."""

    @abc.abstractmethod
    def delete(self, user_id: int) -> None:
        """Remove any state for `user_id`. No-op if absent."""

    @abc.abstractmethod
    def update(
        self,
        user_id: int,
        channel: str,
        mutator: Callable[[ConversationState], None],
    ) -> ConversationState:
        """Atomic read-modify-write.

        - If no state exists, creates one with the given `channel` and an
          empty data dict, then calls `mutator(state)`.
        - If state exists, loads it, calls `mutator(state)`, writes it back.
        - Implementations MUST guarantee serializability between concurrent
          calls for the same `user_id`.
        - Returns the post-mutation state.
        """


# ─────────────────────────────────────────────────────────────────────────────
# In-memory implementation (tests, fallback)
# ─────────────────────────────────────────────────────────────────────────────


class InMemoryStateStore(StateStore):
    """Thread-safe in-memory store. Primary use: unit tests. Also usable as a
    fallback when DATABASE_URL is not configured (dev-only; state is lost on
    restart).
    """

    def __init__(self):
        self._states: dict[int, ConversationState] = {}
        self._lock = threading.RLock()
        # Per-user locks so concurrent updates for different users don't block.
        self._user_locks: dict[int, threading.Lock] = {}

    def _user_lock(self, user_id: int) -> threading.Lock:
        with self._lock:
            lock = self._user_locks.get(user_id)
            if lock is None:
                lock = threading.Lock()
                self._user_locks[user_id] = lock
            return lock

    def get(self, user_id: int) -> Optional[ConversationState]:
        with self._lock:
            state = self._states.get(user_id)
            return state.copy() if state is not None else None

    def set(self, state: ConversationState) -> None:
        _validate(state)
        with self._lock:
            snapshot = state.copy()
            snapshot.updated_at = datetime.now(timezone.utc)
            self._states[state.user_id] = snapshot

    def delete(self, user_id: int) -> None:
        with self._lock:
            self._states.pop(user_id, None)

    def update(
        self,
        user_id: int,
        channel: str,
        mutator: Callable[[ConversationState], None],
    ) -> ConversationState:
        if channel not in VALID_CHANNELS:
            raise ValueError(f"Unknown channel: {channel!r}")
        lock = self._user_lock(user_id)
        with lock:
            # Snapshot inside the per-user lock so the mutator sees a stable state.
            current = self.get(user_id)
            if current is None:
                current = ConversationState(user_id=user_id, channel=channel)
            mutator(current)
            self.set(current)
            return current.copy()


def _validate(state: ConversationState) -> None:
    if state.channel not in VALID_CHANNELS:
        raise ValueError(f"Unknown channel: {state.channel!r}")
    # Sanity check: data must be JSON-serializable WITHOUT a coercing default.
    # Using default=str would silently stringify arbitrary objects; we want to
    # surface the mistake so the caller fixes the shape at the source.
    try:
        json.dumps(state.data)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"State data is not JSON-serializable: {exc}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Postgres implementation
# ─────────────────────────────────────────────────────────────────────────────


class PostgresStateStore(StateStore):
    """Postgres-backed store. Uses `SELECT ... FOR UPDATE` for atomic updates.

    The module-level factory `build_default_store()` constructs one of these
    when `DATABASE_URL` is available; otherwise it returns an `InMemoryStateStore`.
    """

    def __init__(self, cursor_factory: Optional[Callable] = None):
        """`cursor_factory` is a callable returning a Postgres cursor context
        manager. By default it uses `db.get_cursor` from the existing module.
        Tests can pass a fake for isolation.
        """
        if cursor_factory is None:
            from db import get_cursor  # local import so tests don't need psycopg2

            cursor_factory = get_cursor
        self._cursor_factory = cursor_factory

    def get(self, user_id: int) -> Optional[ConversationState]:
        with self._cursor_factory() as cur:
            cur.execute(
                """
                SELECT user_id, channel, flow, step, data, updated_at
                FROM conversation_states
                WHERE user_id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
            return None if row is None else _row_to_state(row)

    def set(self, state: ConversationState) -> None:
        _validate(state)
        with self._cursor_factory() as cur:
            cur.execute(
                """
                INSERT INTO conversation_states (user_id, channel, flow, step, data, updated_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (user_id) DO UPDATE
                  SET channel    = EXCLUDED.channel,
                      flow       = EXCLUDED.flow,
                      step       = EXCLUDED.step,
                      data       = EXCLUDED.data,
                      updated_at = NOW()
                """,
                (
                    state.user_id,
                    state.channel,
                    state.flow,
                    state.step,
                    json.dumps(state.data, ensure_ascii=False, default=str),
                ),
            )

    def delete(self, user_id: int) -> None:
        with self._cursor_factory() as cur:
            cur.execute(
                "DELETE FROM conversation_states WHERE user_id = %s", (user_id,)
            )

    def update(
        self,
        user_id: int,
        channel: str,
        mutator: Callable[[ConversationState], None],
    ) -> ConversationState:
        if channel not in VALID_CHANNELS:
            raise ValueError(f"Unknown channel: {channel!r}")
        with self._cursor_factory() as cur:
            # Row-level lock: blocks other SELECT FOR UPDATE for the same user_id
            # until this transaction commits. Different user_ids don't block.
            cur.execute(
                """
                SELECT user_id, channel, flow, step, data, updated_at
                FROM conversation_states
                WHERE user_id = %s
                FOR UPDATE
                """,
                (user_id,),
            )
            row = cur.fetchone()
            if row is None:
                state = ConversationState(user_id=user_id, channel=channel)
            else:
                state = _row_to_state(row)
            mutator(state)
            _validate(state)
            cur.execute(
                """
                INSERT INTO conversation_states (user_id, channel, flow, step, data, updated_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (user_id) DO UPDATE
                  SET channel    = EXCLUDED.channel,
                      flow       = EXCLUDED.flow,
                      step       = EXCLUDED.step,
                      data       = EXCLUDED.data,
                      updated_at = NOW()
                """,
                (
                    state.user_id,
                    state.channel,
                    state.flow,
                    state.step,
                    json.dumps(state.data, ensure_ascii=False, default=str),
                ),
            )
            return state.copy()


def _row_to_state(row) -> ConversationState:
    """Convert a psycopg2 RealDictRow (or mapping) to a ConversationState."""
    # Row is dict-like (RealDictCursor). Fall back to positional for plain tuples.
    if hasattr(row, "keys"):
        d = row.get("data", {}) or {}
        if isinstance(d, str):
            d = json.loads(d)
        return ConversationState(
            user_id=row["user_id"],
            channel=row["channel"],
            flow=row.get("flow"),
            step=row.get("step"),
            data=dict(d),
            updated_at=row.get("updated_at"),
        )
    # Positional fallback
    user_id, channel, flow, step, data, updated_at = row
    if isinstance(data, str):
        data = json.loads(data)
    return ConversationState(
        user_id=user_id,
        channel=channel,
        flow=flow,
        step=step,
        data=dict(data or {}),
        updated_at=updated_at,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Migration
# ─────────────────────────────────────────────────────────────────────────────


MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS conversation_states (
    user_id    BIGINT PRIMARY KEY,
    channel    TEXT NOT NULL CHECK (channel IN ('telegram', 'whatsapp')),
    flow       TEXT,
    step       TEXT,
    data       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_conv_states_channel ON conversation_states(channel);
"""


def migrate(cursor_factory: Optional[Callable] = None) -> None:
    """Create the `conversation_states` table if missing. Idempotent."""
    if cursor_factory is None:
        from db import get_cursor

        cursor_factory = get_cursor
    with cursor_factory() as cur:
        cur.execute(MIGRATION_SQL)


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────


def build_default_store() -> StateStore:
    """Return a Postgres store if DATABASE_URL is set, else an in-memory store.

    In-memory mode is a dev-only fallback that prints a one-time warning.
    """
    import os

    if os.getenv("DATABASE_URL"):
        return PostgresStateStore()
    import logging

    logging.getLogger(__name__).warning(
        "DATABASE_URL not set — using in-memory state store. "
        "This is fine for tests/dev only; state is lost on restart."
    )
    return InMemoryStateStore()
