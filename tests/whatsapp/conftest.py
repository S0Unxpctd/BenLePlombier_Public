"""Shared fixtures for WhatsApp adapter tests.

The goals:
1. No real network traffic.
2. No real Postgres — a minimal ``FakeCursor`` implements enough of the
   ``get_cursor`` contract (the same one ``core.state_store`` tests use).
3. No real env vars needed — ``WhatsAppConfig`` is built directly.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
from typing import Any, Callable, Iterator, Optional

import pytest

from whatsapp.config import WhatsAppConfig, reset_config_cache


# ─────────────────────────────────────────────────────────────────────────────
# Fake Postgres cursor covering the subset used by whatsapp.users + state_store
# ─────────────────────────────────────────────────────────────────────────────


class _FakeRow(dict):
    """Dict that also supports index access (for positional unpacking)."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class FakeCursor:
    """In-memory stand-in for ``psycopg2``'s cursor used by whatsapp.users.

    Supports just the queries we need:
    - ``INSERT INTO whatsapp_users … ON CONFLICT DO NOTHING``
    - ``SELECT user_id FROM whatsapp_users WHERE phone_e164 = %s``
    - ``SELECT phone_e164 FROM whatsapp_users WHERE user_id = %s``
    """

    def __init__(self, store: dict):
        self._store = store
        self._last: Optional[_FakeRow] = None

    def execute(self, sql: str, params: tuple = ()) -> None:
        stripped = " ".join(sql.split()).lower()
        if stripped.startswith("insert into whatsapp_users"):
            phone, uid = params
            self._store.setdefault("whatsapp_users", {})
            self._store["whatsapp_users"].setdefault(phone, uid)
            self._last = None
            return
        if stripped.startswith("select user_id from whatsapp_users where phone_e164"):
            (phone,) = params
            uid = self._store.get("whatsapp_users", {}).get(phone)
            self._last = _FakeRow(user_id=uid) if uid is not None else None
            return
        if stripped.startswith("select phone_e164 from whatsapp_users where user_id"):
            (uid,) = params
            phone = None
            for p, u in self._store.get("whatsapp_users", {}).items():
                if u == uid:
                    phone = p
                    break
            self._last = _FakeRow(phone_e164=phone) if phone else None
            return
        raise AssertionError(f"FakeCursor: unhandled SQL {sql!r}")

    def fetchone(self):
        return self._last


@pytest.fixture
def fake_store():
    return {"whatsapp_users": {}}


@pytest.fixture
def fake_cursor_factory(fake_store):
    @contextlib.contextmanager
    def factory():
        yield FakeCursor(fake_store)

    return factory


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic WhatsAppConfig — no env vars needed
# ─────────────────────────────────────────────────────────────────────────────


TEST_TOKEN = "test-token"
TEST_PHONE_ID = "1039449349255433"
TEST_VERIFY = "verify-me"
TEST_SECRET = "super-secret"


@pytest.fixture(autouse=True)
def _reset_cfg_cache():
    reset_config_cache()
    yield
    reset_config_cache()


@pytest.fixture
def test_config() -> WhatsAppConfig:
    return WhatsAppConfig(
        token=TEST_TOKEN,
        phone_number_id=TEST_PHONE_ID,
        verify_token=TEST_VERIFY,
        app_secret=TEST_SECRET,
    )


def sign(body: bytes, secret: str = TEST_SECRET) -> str:
    """Compute the `sha256=…` header Meta expects for a given body."""
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Recorder for Meta outbound calls
# ─────────────────────────────────────────────────────────────────────────────


class SendRecorder:
    """Drop-in replacement for the send_* primitives.

    Tests inspect ``calls`` to verify the outbound sequence. Each entry is
    ``(fn_name, args, kwargs)``.
    """

    def __init__(self, failing: frozenset = frozenset()):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.failing = failing

    def _record(self, fn_name: str):
        def wrapper(*args, **kwargs):
            self.calls.append((fn_name, args, kwargs))
            if fn_name in self.failing:
                raise RuntimeError(f"simulated failure in {fn_name}")
            return {"messages": [{"id": f"wamid.fake.{len(self.calls)}"}]}

        return wrapper

    def __getattr__(self, name: str):
        return self._record(name)


@pytest.fixture
def recorder():
    return SendRecorder()
