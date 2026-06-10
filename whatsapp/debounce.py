"""Souffl.AI V3 — Voice message debounce for WhatsApp.

Implements a Postgres-backed queue for voice messages, allowing multiple
voice memos sent within a 10-second window to be concatenated and processed
as a single input. Prevents accidental double-processing when a user sends
multiple voice memos in rapid succession.

Key functions:
- enqueue_voice(user_id, wamid, media_id, conn_factory) → void
- transcribe_and_store(wamid, transcription, conn_factory) → void
- fetch_pending_for_user(user_id, window_seconds, conn_factory) → list[dict]
- mark_processed(wamids, conn_factory) → void

All database operations use parameterised queries with cursor.execute(..., (params,))
for SQL injection safety. No f-strings in queries.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def enqueue_voice(
    user_id: int,
    wamid: str,
    media_id: str,
    conn_factory: Optional[Callable] = None,
) -> None:
    """Enqueue a voice message. Initial status is 'pending'.

    Args:
        user_id: numeric user id
        wamid: WhatsApp message id (unique)
        media_id: WhatsApp media id (returned from Meta)
        conn_factory: callable that returns a connection; if None, uses db.get_cursor()
    """
    if conn_factory is None:
        from db import get_cursor
        _conn_factory = get_cursor
    else:
        _conn_factory = conn_factory

    with _conn_factory() as cur:
        cur.execute(
            """
            INSERT INTO voice_debounce_queue
            (user_id, wamid, media_id, received_at, status)
            VALUES (%s, %s, %s, NOW(), %s)
            ON CONFLICT (wamid) DO NOTHING
            """,
            (user_id, wamid, media_id, "pending"),
        )


def transcribe_and_store(
    wamid: str,
    transcription: str,
    conn_factory: Optional[Callable] = None,
) -> None:
    """Store transcription for a voice message and mark it transcribed.

    Args:
        wamid: WhatsApp message id
        transcription: transcribed text
        conn_factory: callable that returns a connection
    """
    if conn_factory is None:
        from db import get_cursor
        _conn_factory = get_cursor
    else:
        _conn_factory = conn_factory

    with _conn_factory() as cur:
        cur.execute(
            """
            UPDATE voice_debounce_queue
            SET transcription = %s, status = %s
            WHERE wamid = %s
            """,
            (transcription, "transcribed", wamid),
        )


def fetch_pending_for_user(
    user_id: int,
    window_seconds: int,
    conn_factory: Optional[Callable] = None,
) -> list[dict]:
    """Fetch all 'transcribed' voice messages within the time window for a user.

    Returns rows ordered by received_at (oldest first), so callers can
    concatenate transcriptions in order.

    Args:
        user_id: numeric user id
        window_seconds: seconds into the past to look
        conn_factory: callable that returns a connection

    Returns:
        List of dicts with keys: {user_id, wamid, media_id, received_at, transcription, status}
    """
    if conn_factory is None:
        from db import get_cursor
        _conn_factory = get_cursor
    else:
        _conn_factory = conn_factory

    with _conn_factory() as cur:
        cutoff_time = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        cur.execute(
            """
            SELECT user_id, wamid, media_id, received_at, transcription, status
            FROM voice_debounce_queue
            WHERE user_id = %s
              AND status = %s
              AND received_at >= %s
            ORDER BY received_at ASC
            """,
            (user_id, "transcribed", cutoff_time),
        )
        rows = cur.fetchall()
        # Convert rows to dicts (rows are tuples when using cursor; psycopg adapters vary)
        if not rows:
            return []
        # If the cursor returns DictRow (psycopg3) or Row namedtuples, this works;
        # if plain tuples, we construct dicts manually
        result = []
        for row in rows:
            if isinstance(row, dict):
                result.append(row)
            else:
                # Fallback: assume column order from the SELECT
                result.append({
                    "user_id": row[0],
                    "wamid": row[1],
                    "media_id": row[2],
                    "received_at": row[3],
                    "transcription": row[4],
                    "status": row[5],
                })
        return result


def mark_processed(
    wamids: list[str],
    conn_factory: Optional[Callable] = None,
) -> None:
    """Mark a batch of voice messages as 'processed'.

    Args:
        wamids: list of WhatsApp message ids
        conn_factory: callable that returns a connection
    """
    if not wamids:
        return

    if conn_factory is None:
        from db import get_cursor
        _conn_factory = get_cursor
    else:
        _conn_factory = conn_factory

    with _conn_factory() as cur:
        # Use a loop to mark each wamid (safer than constructing dynamic WHERE IN)
        for wamid in wamids:
            cur.execute(
                """
                UPDATE voice_debounce_queue
                SET status = %s
                WHERE wamid = %s
                """,
                ("processed", wamid),
            )


__all__ = [
    "enqueue_voice",
    "transcribe_and_store",
    "fetch_pending_for_user",
    "mark_processed",
]
