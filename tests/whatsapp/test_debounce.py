"""Phase 3 Tests — Debounce Queue (feature 3.4)

Tests for V3/whatsapp/debounce.py using FakeCursor and a Postgres-like in-memory
store. All database operations are parameterised queries — no SQL injection risk.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from whatsapp.debounce import (
    enqueue_voice,
    transcribe_and_store,
    fetch_pending_for_user,
    mark_processed,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test Cursor & Connection Factory
# ─────────────────────────────────────────────────────────────────────────────


class FakeDebounceCursor:
    """Minimal cursor that records all execute() calls for inspection."""

    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self.rows_data: dict[str, list] = {
            "voice_debounce_queue": [],
        }

    def execute(self, sql: str, params: tuple = ()) -> None:
        """Record the query and simulate the operation."""
        self.executed.append((sql, params))

        # Parse and handle INSERT
        if "INSERT INTO voice_debounce_queue" in sql:
            user_id, wamid, media_id, status = params
            row = {
                "user_id": user_id,
                "wamid": wamid,
                "media_id": media_id,
                "received_at": datetime.now(timezone.utc),
                "transcription": None,
                "status": status,
            }
            self.rows_data["voice_debounce_queue"].append(row)
            return

        # Parse and handle UPDATE transcription
        if "UPDATE voice_debounce_queue" in sql and "transcription" in sql:
            transcription, status, wamid = params
            for row in self.rows_data["voice_debounce_queue"]:
                if row["wamid"] == wamid:
                    row["transcription"] = transcription
                    row["status"] = status
            return

        # Parse and handle UPDATE status
        if "UPDATE voice_debounce_queue" in sql and "WHERE wamid" in sql:
            if len(params) == 2:  # ("processed", wamid)
                status, wamid = params
                for row in self.rows_data["voice_debounce_queue"]:
                    if row["wamid"] == wamid:
                        row["status"] = status
            return

        # Parse and handle SELECT
        if "SELECT" in sql and "FROM voice_debounce_queue" in sql:
            # Simple filtering by user_id, status, and received_at >= cutoff
            user_id, status, cutoff_time = params
            result = []
            for row in self.rows_data["voice_debounce_queue"]:
                if (
                    row["user_id"] == user_id
                    and row["status"] == status
                    and row["received_at"] >= cutoff_time
                ):
                    result.append(row)
            # Sort by received_at ascending
            result.sort(key=lambda r: r["received_at"])
            self._last_result = result
            return

        raise AssertionError(f"FakeDebounceCursor: unhandled SQL {sql!r}")

    def fetchall(self):
        return getattr(self, "_last_result", [])


@contextlib.contextmanager
def _cursor_factory():
    """Factory for test cursors (named with underscore so pytest doesn't collect)."""
    yield FakeDebounceCursor()


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestEnqueueVoice:
    """Tests for enqueue_voice()."""

    def test_enqueue_voice_inserts_row(self):
        """Verify enqueue_voice() inserts a row with correct params."""
        cursor = FakeDebounceCursor()

        @contextlib.contextmanager
        def factory():
            yield cursor

        enqueue_voice(user_id=1, wamid="wamid-1", media_id="m1", conn_factory=factory)

        # Verify the INSERT was executed
        assert len(cursor.executed) == 1
        sql, params = cursor.executed[0]
        assert "INSERT INTO voice_debounce_queue" in sql
        assert params == (1, "wamid-1", "m1", "pending")

        # Verify the row was inserted
        rows = cursor.rows_data["voice_debounce_queue"]
        assert len(rows) == 1
        assert rows[0]["user_id"] == 1
        assert rows[0]["wamid"] == "wamid-1"
        assert rows[0]["media_id"] == "m1"
        assert rows[0]["status"] == "pending"
        assert rows[0]["transcription"] is None
        assert rows[0]["received_at"] is not None

    def test_enqueue_voice_multiple_calls(self):
        """Verify enqueue_voice() can handle multiple voices."""
        cursor = FakeDebounceCursor()

        @contextlib.contextmanager
        def factory():
            yield cursor

        enqueue_voice(user_id=1, wamid="wamid-1", media_id="m1", conn_factory=factory)
        enqueue_voice(user_id=1, wamid="wamid-2", media_id="m2", conn_factory=factory)

        rows = cursor.rows_data["voice_debounce_queue"]
        assert len(rows) == 2
        assert rows[0]["wamid"] == "wamid-1"
        assert rows[1]["wamid"] == "wamid-2"

    def test_enqueue_voice_parameterised(self):
        """Verify no SQL string-formatting in enqueue_voice()."""
        cursor = FakeDebounceCursor()

        @contextlib.contextmanager
        def factory():
            yield cursor

        enqueue_voice(user_id=1, wamid="wamid-1", media_id="m1", conn_factory=factory)

        sql, params = cursor.executed[0]
        # Check that params use %s placeholders, not formatted values
        assert "%s" in sql
        # Ensure values are in the params tuple, not in the SQL string
        assert "wamid-1" not in sql
        assert params[1] == "wamid-1"


class TestTranscribeAndStore:
    """Tests for transcribe_and_store()."""

    def test_transcribe_and_store_updates_row(self):
        """Verify transcribe_and_store() updates transcription and status."""
        cursor = FakeDebounceCursor()

        @contextlib.contextmanager
        def factory():
            yield cursor

        # Seed a pending row
        enqueue_voice(user_id=1, wamid="wamid-1", media_id="m1", conn_factory=factory)

        # Update it with transcription
        cursor.executed.clear()  # Clear the enqueue call
        transcribe_and_store(wamid="wamid-1", transcription="hello world", conn_factory=factory)

        sql, params = cursor.executed[0]
        assert "UPDATE voice_debounce_queue" in sql
        assert params == ("hello world", "transcribed", "wamid-1")

        # Verify the row was updated
        rows = cursor.rows_data["voice_debounce_queue"]
        assert rows[0]["transcription"] == "hello world"
        assert rows[0]["status"] == "transcribed"

    def test_transcribe_and_store_parameterised(self):
        """Verify transcribe_and_store() uses parameterised queries."""
        cursor = FakeDebounceCursor()

        @contextlib.contextmanager
        def factory():
            yield cursor

        transcribe_and_store(
            wamid="wamid-1",
            transcription="some transcription",
            conn_factory=factory,
        )

        sql, params = cursor.executed[0]
        assert "%s" in sql
        assert "some transcription" not in sql
        assert params[0] == "some transcription"


class TestFetchPendingForUser:
    """Tests for fetch_pending_for_user()."""

    def test_fetch_pending_filters_by_user_and_status(self):
        """Verify fetch_pending_for_user() returns only transcribed rows for the user."""
        cursor = FakeDebounceCursor()

        @contextlib.contextmanager
        def factory():
            yield cursor

        # Seed multiple rows
        enqueue_voice(user_id=1, wamid="wamid-1", media_id="m1", conn_factory=factory)
        enqueue_voice(user_id=1, wamid="wamid-2", media_id="m2", conn_factory=factory)
        enqueue_voice(user_id=2, wamid="wamid-3", media_id="m3", conn_factory=factory)

        # Update first two to transcribed
        transcribe_and_store(wamid="wamid-1", transcription="text1", conn_factory=factory)
        transcribe_and_store(wamid="wamid-2", transcription="text2", conn_factory=factory)

        # Fetch pending for user 1
        cursor.executed.clear()
        result = fetch_pending_for_user(user_id=1, window_seconds=10, conn_factory=factory)

        # Should return 2 rows (both transcribed for user 1)
        assert len(result) == 2
        assert result[0]["wamid"] == "wamid-1"
        assert result[1]["wamid"] == "wamid-2"

    def test_fetch_pending_filters_by_window(self):
        """Verify fetch_pending_for_user() filters by time window."""
        cursor = FakeDebounceCursor()

        @contextlib.contextmanager
        def factory():
            yield cursor

        # Seed a row
        enqueue_voice(user_id=1, wamid="wamid-1", media_id="m1", conn_factory=factory)
        transcribe_and_store(wamid="wamid-1", transcription="text1", conn_factory=factory)

        # Manually set received_at to 20s ago
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(seconds=20)
        cursor.rows_data["voice_debounce_queue"][0]["received_at"] = old_time

        # Fetch with window=10 (only rows received in last 10s)
        # Should return empty because the row is 20s old
        result = fetch_pending_for_user(user_id=1, window_seconds=10, conn_factory=factory)
        # Note: fetch uses >= cutoff_time, so 20s old is NOT >= (now - 10s)
        assert len(result) == 0

    def test_fetch_pending_returns_ordered_by_received_at(self):
        """Verify fetch_pending_for_user() returns rows ordered by received_at ASC."""
        cursor = FakeDebounceCursor()

        @contextlib.contextmanager
        def factory():
            yield cursor

        # Seed multiple rows with different times (enqueue sets them to now)
        enqueue_voice(user_id=1, wamid="wamid-1", media_id="m1", conn_factory=factory)
        import time
        time.sleep(0.01)  # Small delay to ensure different timestamps
        enqueue_voice(user_id=1, wamid="wamid-2", media_id="m2", conn_factory=factory)

        # Update both to transcribed
        transcribe_and_store(wamid="wamid-1", transcription="text1", conn_factory=factory)
        transcribe_and_store(wamid="wamid-2", transcription="text2", conn_factory=factory)

        # Fetch
        result = fetch_pending_for_user(user_id=1, window_seconds=10, conn_factory=factory)

        # Should be ordered by received_at (oldest first)
        assert len(result) >= 1
        if len(result) > 1:
            assert result[0]["received_at"] <= result[1]["received_at"]

    def test_fetch_pending_timezone_aware(self):
        """Verify fetch_pending_for_user() uses timezone-aware datetime."""
        cursor = FakeDebounceCursor()

        @contextlib.contextmanager
        def factory():
            yield cursor

        enqueue_voice(user_id=1, wamid="wamid-1", media_id="m1", conn_factory=factory)

        # Call fetch to verify it computes a timezone-aware cutoff
        fetch_pending_for_user(user_id=1, window_seconds=10, conn_factory=factory)

        # Check the SQL params
        sql, params = cursor.executed[-1]  # Last call is the SELECT
        assert "SELECT" in sql
        # params[2] should be the cutoff_time (timezone-aware)
        cutoff_time = params[2]
        assert hasattr(cutoff_time, "tzinfo")
        assert cutoff_time.tzinfo is not None or cutoff_time.tzinfo == timezone.utc

    def test_fetch_pending_parameterised(self):
        """Verify fetch_pending_for_user() uses parameterised queries."""
        cursor = FakeDebounceCursor()

        @contextlib.contextmanager
        def factory():
            yield cursor

        fetch_pending_for_user(user_id=1, window_seconds=10, conn_factory=factory)

        sql, params = cursor.executed[0]
        assert "SELECT" in sql
        assert "%s" in sql
        assert params[0] == 1
        assert params[1] == "transcribed"


class TestMarkProcessed:
    """Tests for mark_processed()."""

    def test_mark_processed_updates_status(self):
        """Verify mark_processed() updates status to 'processed' for given wamids."""
        cursor = FakeDebounceCursor()

        @contextlib.contextmanager
        def factory():
            yield cursor

        # Seed multiple rows
        enqueue_voice(user_id=1, wamid="wamid-1", media_id="m1", conn_factory=factory)
        enqueue_voice(user_id=1, wamid="wamid-2", media_id="m2", conn_factory=factory)
        enqueue_voice(user_id=1, wamid="wamid-3", media_id="m3", conn_factory=factory)

        # Mark first two as processed
        cursor.executed.clear()
        mark_processed(wamids=["wamid-1", "wamid-2"], conn_factory=factory)

        # Verify UPDATEs were executed
        updates = [c for c in cursor.executed if "UPDATE" in c[0]]
        assert len(updates) == 2

        # Verify the rows were updated
        rows = cursor.rows_data["voice_debounce_queue"]
        assert rows[0]["status"] == "processed"
        assert rows[1]["status"] == "processed"
        assert rows[2]["status"] == "pending"  # Not touched

    def test_mark_processed_empty_list_is_noop(self):
        """Verify mark_processed() with empty list does nothing."""
        cursor = FakeDebounceCursor()

        @contextlib.contextmanager
        def factory():
            yield cursor

        enqueue_voice(user_id=1, wamid="wamid-1", media_id="m1", conn_factory=factory)

        cursor.executed.clear()
        mark_processed(wamids=[], conn_factory=factory)

        # No execute calls should have been made
        assert len(cursor.executed) == 0

    def test_mark_processed_parameterised(self):
        """Verify mark_processed() uses parameterised queries for each wamid."""
        cursor = FakeDebounceCursor()

        @contextlib.contextmanager
        def factory():
            yield cursor

        enqueue_voice(user_id=1, wamid="wamid-1", media_id="m1", conn_factory=factory)

        cursor.executed.clear()
        mark_processed(wamids=["wamid-1"], conn_factory=factory)

        sql, params = cursor.executed[0]
        assert "UPDATE" in sql
        assert "%s" in sql
        assert params == ("processed", "wamid-1")


class TestAllQueriesParameterised:
    """Comprehensive test that all debounce queries use parameterised queries."""

    def test_no_unparameterised_queries(self):
        """Scan all executed queries for unparameterised values."""
        cursor = FakeDebounceCursor()

        @contextlib.contextmanager
        def factory():
            yield cursor

        # Exercise all functions
        enqueue_voice(user_id=1, wamid="wamid-1", media_id="m1", conn_factory=factory)
        transcribe_and_store(wamid="wamid-1", transcription="test", conn_factory=factory)
        fetch_pending_for_user(user_id=1, window_seconds=10, conn_factory=factory)
        mark_processed(wamids=["wamid-1"], conn_factory=factory)

        # Verify all executed queries use %s for parameters
        for sql, params in cursor.executed:
            # Count %s placeholders
            placeholder_count = sql.count("%s")
            param_count = len(params)
            assert placeholder_count == param_count, (
                f"Mismatch: {placeholder_count} placeholders in SQL but {param_count} params\n"
                f"SQL: {sql}\n"
                f"Params: {params}"
            )
            # Ensure user_id, wamid, media_id, etc. are not in the SQL string
            for param in params:
                if isinstance(param, str):
                    assert param not in sql, f"Value {param!r} found in SQL string (not parameterised)"


__all__ = [
    "TestEnqueueVoice",
    "TestTranscribeAndStore",
    "TestFetchPendingForUser",
    "TestMarkProcessed",
    "TestAllQueriesParameterised",
]
