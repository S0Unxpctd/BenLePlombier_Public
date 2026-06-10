"""Souffl.AI V3 — WhatsApp adapter configuration.

Centralises env-var reads for the WhatsApp transport so the rest of the
package gets type-checked, validated values from a single place. Reading the
config is side-effect-free: ``get_config()`` returns a frozen dataclass.
``get_config()`` is cached; tests can clear it with ``reset_config_cache()``.

Environment variables (see ``V3_BRIEFS/4_TECHNICAL_REFERENCE.md``)
-----------------------------------------------------------------
- ``WHATSAPP_TOKEN``                — long-lived System User access token
- ``WHATSAPP_PHONE_NUMBER_ID``      — numeric id of the sender phone
- ``WHATSAPP_VERIFY_TOKEN``         — secret used during GET /webhook verification
- ``META_APP_SECRET``               — used for ``X-Hub-Signature-256`` HMAC
- ``META_APP_ID``                   — optional, logged at startup
- ``WHATSAPP_BUSINESS_ACCOUNT_ID``  — optional, logged at startup
- ``WEBHOOK_BASE_URL``              — optional, logged at startup, used in doc
- ``WHATSAPP_GRAPH_VERSION``        — default ``v21.0``

Only the **bold** variables are strictly required at boot. The adapter degrades
gracefully (prints a warning) in dev/test contexts where one or more are
missing — allowing unit tests to run without secrets.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WhatsAppConfig:
    """Immutable snapshot of the WhatsApp adapter configuration.

    ``require()`` raises at runtime when a secret is missing. The dataclass
    itself stores the optional strings as ``""`` (empty) so it can still be
    constructed in tests without any env vars set.
    """

    token: str
    phone_number_id: str
    verify_token: str
    app_secret: str
    app_id: str = ""
    business_account_id: str = ""
    webhook_base_url: str = ""
    graph_version: str = "v21.0"

    @property
    def base_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_version}/"

    def messages_url(self) -> str:
        self.require("phone_number_id")
        return f"{self.base_url}{self.phone_number_id}/messages"

    def media_upload_url(self) -> str:
        self.require("phone_number_id")
        return f"{self.base_url}{self.phone_number_id}/media"

    def media_metadata_url(self, media_id: str) -> str:
        return f"{self.base_url}{media_id}"

    def require(self, *fields: str) -> None:
        """Raise ``RuntimeError`` if any of the named fields is missing."""
        missing = [f for f in fields if not getattr(self, f)]
        if missing:
            raise RuntimeError(
                "WhatsApp adapter is missing required environment variable(s): "
                + ", ".join(missing)
            )


_cached: Optional[WhatsAppConfig] = None


def get_config() -> WhatsAppConfig:
    """Return the cached config instance, loading env vars on first call."""
    global _cached
    if _cached is None:
        _cached = _load_from_env()
    return _cached


def reset_config_cache() -> None:
    """Clear the memoised config. Tests call this between cases."""
    global _cached
    _cached = None


def _load_from_env() -> WhatsAppConfig:
    # .strip() on every secret — Railway / Meta copy-paste often leaves a
    # trailing newline, which httpx rejects with a LocalProtocolError on
    # "Bearer ...\n". Strip once here so every consumer is safe.
    def _env(name: str, default: str = "") -> str:
        return os.getenv(name, default).strip()

    cfg = WhatsAppConfig(
        token=_env("WHATSAPP_TOKEN"),
        phone_number_id=_env("WHATSAPP_PHONE_NUMBER_ID"),
        verify_token=_env("WHATSAPP_VERIFY_TOKEN"),
        app_secret=_env("META_APP_SECRET"),
        app_id=_env("META_APP_ID"),
        business_account_id=_env("WHATSAPP_BUSINESS_ACCOUNT_ID"),
        webhook_base_url=_env("WEBHOOK_BASE_URL"),
        graph_version=_env("WHATSAPP_GRAPH_VERSION", "v21.0"),
    )
    missing_strict = [
        name
        for name, val in [
            ("WHATSAPP_TOKEN", cfg.token),
            ("WHATSAPP_PHONE_NUMBER_ID", cfg.phone_number_id),
            ("WHATSAPP_VERIFY_TOKEN", cfg.verify_token),
            ("META_APP_SECRET", cfg.app_secret),
        ]
        if not val
    ]
    if missing_strict:
        logger.warning(
            "[whatsapp.config] Missing env vars (dev/test mode?): %s",
            ", ".join(missing_strict),
        )
    return cfg


__all__ = ["WhatsAppConfig", "get_config", "reset_config_cache"]
