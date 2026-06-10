"""Souffl.AI V3 — WhatsApp phone ↔ user_id mapping.

Why a mapping table?
--------------------
The rest of the codebase (``clients``, ``devis``, ``conversation_states``)
uses a single ``user_id`` column typed ``BIGINT``. For Telegram, ``user_id``
is the numeric Telegram user id. For WhatsApp, the stable identifier is the
phone number in E.164 format, e.g. ``+33600000000``. We need to translate
between the two without colliding with real Telegram ids.

Strategy (deterministic hash, documented)
-----------------------------------------
``phone_to_user_id(phone_e164)`` returns ``10**12 + (sha256(phone) % 10**12)``.

- Telegram user ids today are ≤ ``10**10`` in practice (and specified to fit
  in a signed 64-bit integer). Forcing WhatsApp ids into the ``1_000_000_000_000``
  range cleanly separates the two namespaces without a ``channel`` column
  join on the hot path.
- ``sha256 % 10**12`` gives a 12-digit deterministic integer. Birthday risk
  for 10k users: < 10^-4. We persist the mapping anyway so the hash is just
  the initial allocation strategy, not a trust anchor.
- Storing the mapping in ``whatsapp_users(phone_e164 PRIMARY KEY, user_id)``
  means the inverse lookup is an index read, not a hash recompute.

Public API
----------
- ``normalize_phone(raw)``       — robust E.164 normalisation (``+33…``)
- ``phone_to_user_id(phone)``    — pure deterministic id function
- ``get_or_create_user(phone)``  — idempotent Postgres insert, returns ``user_id``
- ``get_user_id_from_phone(p)``  — lookup only, returns ``None`` if absent
- ``get_phone_from_user_id(uid)``— reverse lookup, returns ``None`` if absent

Callers
-------
``router`` calls ``get_or_create_user`` the first time it sees a phone. All
subsequent flows use the returned ``user_id`` directly.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Callable, Optional


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Phone normalisation
# ─────────────────────────────────────────────────────────────────────────────


_DIGITS_ONLY = re.compile(r"\D+")


def normalize_phone(raw: str) -> str:
    """Return ``raw`` as an E.164 string starting with ``+``.

    Accepts:
        "33600000000"     → "+33600000000"
        "+33600000000"    → "+33600000000"
        "+33 7 67 96 46 62" → "+33600000000"
    Rejects blatantly wrong inputs (empty, non-digit).
    """
    if raw is None:
        raise ValueError("phone is None")
    s = str(raw).strip()
    if not s:
        raise ValueError("phone is empty")
    # Preserve leading '+' if present, strip everything else non-digit.
    plus = s.startswith("+")
    digits = _DIGITS_ONLY.sub("", s)
    if not digits:
        raise ValueError(f"phone has no digits: {raw!r}")
    # Meta often sends ``wa_id`` without the leading ``+`` — always add it.
    return "+" + digits if plus or not s.startswith("+") else "+" + digits


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic user_id allocator
# ─────────────────────────────────────────────────────────────────────────────


# Range ``[10**12, 2*10**12 - 1]``. Well above any real Telegram id (which are
# still in the low 10**10 band as of 2026) and well inside BIGINT range.
_USER_ID_BASE = 10**12
_USER_ID_MOD = 10**12


def phone_to_user_id(phone_e164: str) -> int:
    """Pure deterministic hash: E.164 phone → BIGINT-safe user_id.

    Uses SHA-256 truncated to the 12 lowest decimal digits, offset by
    ``_USER_ID_BASE`` to avoid the Telegram namespace.
    """
    normalised = normalize_phone(phone_e164)
    digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
    return _USER_ID_BASE + (int(digest, 16) % _USER_ID_MOD)


# ─────────────────────────────────────────────────────────────────────────────
# Postgres-backed mapping
# ─────────────────────────────────────────────────────────────────────────────


def _default_cursor_factory():
    from db import get_cursor

    return get_cursor


def get_user_id_from_phone(
    phone_e164: str,
    cursor_factory: Optional[Callable] = None,
) -> Optional[int]:
    """Return the stored ``user_id`` for ``phone_e164``, or ``None`` if absent."""
    factory = cursor_factory or _default_cursor_factory()
    normalised = normalize_phone(phone_e164)
    with factory() as cur:
        cur.execute(
            "SELECT user_id FROM whatsapp_users WHERE phone_e164 = %s",
            (normalised,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return int(row["user_id"]) if hasattr(row, "keys") else int(row[0])


def get_phone_from_user_id(
    user_id: int,
    cursor_factory: Optional[Callable] = None,
) -> Optional[str]:
    """Return the E.164 phone for ``user_id``, or ``None`` if absent."""
    factory = cursor_factory or _default_cursor_factory()
    with factory() as cur:
        cur.execute(
            "SELECT phone_e164 FROM whatsapp_users WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return (row["phone_e164"] if hasattr(row, "keys") else row[0])


def get_or_create_user(
    phone_e164: str,
    cursor_factory: Optional[Callable] = None,
) -> int:
    """Return the ``user_id`` for ``phone_e164``; insert a new row if needed.

    Idempotent under concurrent inbound messages: uses ``ON CONFLICT DO NOTHING``
    then re-reads to handle races where two webhook workers hit the same phone
    at once.
    """
    factory = cursor_factory or _default_cursor_factory()
    normalised = normalize_phone(phone_e164)
    deterministic_uid = phone_to_user_id(normalised)
    with factory() as cur:
        cur.execute(
            """
            INSERT INTO whatsapp_users (phone_e164, user_id)
            VALUES (%s, %s)
            ON CONFLICT (phone_e164) DO NOTHING
            """,
            (normalised, deterministic_uid),
        )
        cur.execute(
            "SELECT user_id FROM whatsapp_users WHERE phone_e164 = %s",
            (normalised,),
        )
        row = cur.fetchone()
        if row is None:
            # Extremely unlikely (insert lost to a concurrent delete); surface loudly.
            raise RuntimeError(f"whatsapp_users race: no row for {normalised}")
        return int(row["user_id"]) if hasattr(row, "keys") else int(row[0])


__all__ = [
    "normalize_phone",
    "phone_to_user_id",
    "get_user_id_from_phone",
    "get_phone_from_user_id",
    "get_or_create_user",
]
