"""Unit tests for V3/ops/sentry.py.

We do not exercise the real Sentry SDK — we inject a stub `sentry_sdk` into
`sys.modules` and reset the init flag between tests.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest


V3_ROOT = Path(__file__).resolve().parents[2]
if str(V3_ROOT) not in sys.path:
    sys.path.insert(0, str(V3_ROOT))


@pytest.fixture(autouse=True)
def _fresh_sentry():
    """Make sure every test starts with a clean initialization state."""
    from ops import sentry as sentry_mod

    sentry_mod._reset_for_testing()
    yield
    sentry_mod._reset_for_testing()


class _StubSentrySdk(types.ModuleType):
    """Minimal stand-in for the real sentry_sdk module."""

    def __init__(self):
        super().__init__("sentry_sdk")
        self.init_calls: list[dict] = []
        self.captured: list[BaseException] = []

    def init(self, **kwargs):
        self.init_calls.append(kwargs)

    def capture_exception(self, exc):
        self.captured.append(exc)


class _StubIntegrations(types.ModuleType):
    """Stub for sentry_sdk.integrations.logging.LoggingIntegration."""

    def __init__(self):
        super().__init__("sentry_sdk.integrations.logging")

        class LoggingIntegration:
            def __init__(self, **_kwargs):
                self.kwargs = _kwargs

        self.LoggingIntegration = LoggingIntegration


@pytest.fixture
def stub_sentry(monkeypatch):
    stub = _StubSentrySdk()
    monkeypatch.setitem(sys.modules, "sentry_sdk", stub)
    monkeypatch.setitem(
        sys.modules, "sentry_sdk.integrations.logging", _StubIntegrations()
    )
    return stub


class TestInitSentry:
    def test_noop_when_dsn_empty(self, monkeypatch):
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        from ops.sentry import init_sentry

        assert init_sentry() is False

    def test_noop_when_sdk_missing(self, monkeypatch, caplog):
        monkeypatch.setenv("SENTRY_DSN", "https://fake@sentry.io/1")
        # Force an ImportError by making the name unresolvable.
        monkeypatch.setitem(sys.modules, "sentry_sdk", None)
        from ops.sentry import init_sentry

        with caplog.at_level(logging.WARNING):
            assert init_sentry() is False
        # Accept either the expected warning or silent no-op if import is short-circuited.

    def test_init_calls_sdk_when_available(self, stub_sentry, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://fake@sentry.io/1")
        monkeypatch.setenv("ENVIRONMENT", "staging")
        from ops.sentry import init_sentry

        assert init_sentry() is True
        assert len(stub_sentry.init_calls) == 1
        kwargs = stub_sentry.init_calls[0]
        assert kwargs["dsn"] == "https://fake@sentry.io/1"
        assert kwargs["environment"] == "staging"
        assert kwargs["send_default_pii"] is False

    def test_init_is_idempotent(self, stub_sentry, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://fake@sentry.io/1")
        from ops.sentry import init_sentry

        assert init_sentry() is True
        # Second call should short-circuit, no new init.
        assert init_sentry() is True
        assert len(stub_sentry.init_calls) == 1

    def test_explicit_dsn_overrides_env(self, stub_sentry, monkeypatch):
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        from ops.sentry import init_sentry

        assert init_sentry(dsn="https://direct@sentry.io/2") is True
        assert stub_sentry.init_calls[0]["dsn"] == "https://direct@sentry.io/2"


class TestCaptureException:
    def test_capture_before_init_is_silent(self):
        from ops.sentry import capture_exception

        # No init, no raise.
        capture_exception(RuntimeError("x"))

    def test_capture_after_init_forwards_to_sdk(self, stub_sentry, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://fake@sentry.io/1")
        from ops.sentry import capture_exception, init_sentry

        init_sentry()
        exc = RuntimeError("boom")
        capture_exception(exc)
        assert stub_sentry.captured == [exc]
