"""Souffl.AI V3 — Sentry integration helper.

Usage at bot startup:

    from V3.ops.sentry import init_sentry
    init_sentry()

- Reads `SENTRY_DSN` and `ENVIRONMENT` from env.
- No-op if `SENTRY_DSN` is empty or missing.
- No-op if the `sentry_sdk` package is not installed (logs a single warning).
- Safe to call multiple times (idempotent via internal flag).

Why a dedicated helper rather than inlined init?
- Makes it trivial to guard imports (so the Telegram bot boots without sentry_sdk
  installed in dev).
- Gives the ops layer a single place to configure sampling, integrations, and
  scrubbing (PII, tokens) as the project matures.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_INITIALIZED = False  # module-level guard against double init


def init_sentry(
    dsn: Optional[str] = None,
    environment: Optional[str] = None,
    traces_sample_rate: float = 0.1,
) -> bool:
    """Initialize Sentry if configured. Returns True iff Sentry was activated.

    - `dsn`: overrides env var for testing. Falls back to SENTRY_DSN.
    - `environment`: "production" / "staging" / "dev". Falls back to
      ENVIRONMENT env var then to "production".
    - `traces_sample_rate`: fraction of transactions sampled for performance
      monitoring. 0.1 is a sane default for a small fleet.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return True

    effective_dsn = dsn if dsn is not None else os.getenv("SENTRY_DSN", "")
    if not effective_dsn:
        logger.info("Sentry disabled: SENTRY_DSN not set.")
        return False

    try:
        import sentry_sdk  # type: ignore
        from sentry_sdk.integrations.logging import LoggingIntegration  # type: ignore
    except ImportError:
        logger.warning(
            "SENTRY_DSN is set but sentry_sdk is not installed. "
            "Install with: pip install sentry-sdk"
        )
        return False

    sentry_sdk.init(
        dsn=effective_dsn,
        environment=environment or os.getenv("ENVIRONMENT", "production"),
        integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        traces_sample_rate=traces_sample_rate,
        # Scrub the obvious secrets from breadcrumbs. Callers can add more.
        send_default_pii=False,
    )
    _INITIALIZED = True
    logger.info("Sentry initialized (environment=%s).", environment or os.getenv("ENVIRONMENT", "production"))
    return True


def capture_exception(exc: BaseException) -> None:
    """Send an exception to Sentry if initialized; otherwise no-op."""
    if not _INITIALIZED:
        return
    try:
        import sentry_sdk  # type: ignore
        sentry_sdk.capture_exception(exc)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to forward exception to Sentry")


def _reset_for_testing() -> None:
    """Test-only: clear the initialized flag so tests can re-init."""
    global _INITIALIZED
    _INITIALIZED = False
