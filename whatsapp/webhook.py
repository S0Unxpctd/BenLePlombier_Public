"""Souffl.AI V3 — WhatsApp webhook (FastAPI).

Exposes two endpoints at ``/webhook``:

- ``GET /webhook`` — one-shot verification: Meta sends ``hub.mode=subscribe``,
  ``hub.verify_token``, ``hub.challenge``; if the verify token matches we echo
  ``hub.challenge`` as plain text with status 200.
- ``POST /webhook`` — message ingestion. Every request is signed with
  ``X-Hub-Signature-256`` (HMAC-SHA256 of the raw body using the app secret).
  We validate the signature before parsing.

Why FastAPI
-----------
- Native async; lets the router offload to an ``asyncio.create_task`` so Meta
  gets a 200 inside its 20-second timeout even when Whisper+LLM take longer.
- Pydantic isn't required — Meta's payload is too variable to model
  strictly; we parse as a ``dict`` and dispatch lazily.
- Easy to instantiate for tests via ``starlette.testclient.TestClient``.

Health endpoints
----------------
``GET /`` and ``GET /health`` return a tiny JSON heartbeat. Railway uses one
of these for its health check.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Any, Callable, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import WhatsAppConfig, get_config


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Signature verification
# ─────────────────────────────────────────────────────────────────────────────


def verify_signature(body: bytes, signature_header: str, app_secret: str) -> bool:
    """Return True iff ``signature_header`` is a valid ``X-Hub-Signature-256``.

    Format: ``sha256=<hex>``. We compute HMAC-SHA256 of ``body`` with
    ``app_secret`` and compare in constant time.
    """
    if not signature_header or not app_secret:
        return False
    if not signature_header.startswith("sha256="):
        return False
    provided = signature_header[len("sha256=") :]
    expected = hmac.new(
        app_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


# ─────────────────────────────────────────────────────────────────────────────
# App factory
# ─────────────────────────────────────────────────────────────────────────────


# The router is imported lazily inside ``create_app`` so that tests that only
# exercise verification / signature checks can instantiate the app without
# pulling the full business-logic graph (pdf_generator, openai, etc).


def create_app(
    *,
    cfg: Optional[WhatsAppConfig] = None,
    dispatch: Optional[Callable[[dict], Any]] = None,
) -> FastAPI:
    """Build a FastAPI instance for the WhatsApp webhook.

    ``dispatch`` overrides the default router — tests pass a stub that records
    payloads. Production code leaves it ``None`` and gets the real router.
    """
    app = FastAPI(title="Souffl.AI WhatsApp webhook", version="v3-phase2")

    cfg_local = cfg or get_config()

    def _dispatch_factory() -> Callable[[dict], Any]:
        if dispatch is not None:
            return dispatch
        # Deferred import to avoid a circular module dependency.
        from .router import dispatch_webhook_event

        return dispatch_webhook_event

    dispatcher = _dispatch_factory()

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return JSONResponse({"ok": True, "service": "souffl-ai-whatsapp"})

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/webhook")
    async def verify(
        mode: str = Query("", alias="hub.mode"),
        token: str = Query("", alias="hub.verify_token"),
        challenge: str = Query("", alias="hub.challenge"),
    ):
        """One-shot webhook verification (Meta sends this at config time)."""
        if not cfg_local.verify_token:
            raise HTTPException(status_code=500, detail="verify token not configured")
        if mode == "subscribe" and token == cfg_local.verify_token:
            return PlainTextResponse(challenge, status_code=200)
        raise HTTPException(status_code=403, detail="verification failed")

    @app.post("/webhook")
    async def ingest(
        request: Request,
        x_hub_signature_256: Optional[str] = Header(default=None),
    ):
        raw = await request.body()
        if not verify_signature(
            raw, x_hub_signature_256 or "", cfg_local.app_secret
        ):
            # Non-200 so Meta retries; but Meta also retries on any
            # non-200, so we still accept malformed attempts quietly with 403.
            logger.warning(
                "[webhook] invalid signature (len=%d header=%r)",
                len(raw),
                (x_hub_signature_256 or "")[:12] + "…",
            )
            raise HTTPException(status_code=403, detail="invalid signature")

        try:
            payload = await request.json()
        except Exception as exc:
            logger.warning("[webhook] malformed JSON: %s", exc)
            # Meta expects a 2xx ack quickly; return 200 so it doesn't retry.
            return JSONResponse({"ignored": "bad_json"}, status_code=200)

        # Meta's docs say: respond 200 quickly. Process in the background.
        try:
            result = dispatcher(payload)
            if asyncio.iscoroutine(result):
                # Fire-and-forget; exceptions are swallowed to avoid
                # crashing the event loop. Sentry already hooks globals.
                asyncio.create_task(_safe_await(result))
        except Exception:
            logger.exception("[webhook] dispatch raised")

        return JSONResponse({"status": "received"}, status_code=200)

    return app


async def _safe_await(coro):
    try:
        await coro
    except Exception:
        logger.exception("[webhook] background task failed")


# ─────────────────────────────────────────────────────────────────────────────
# WSGI / uvicorn entry point
# ─────────────────────────────────────────────────────────────────────────────


# Module-level ``app`` so ``uvicorn whatsapp.webhook:app`` works without
# requiring users to call ``create_app()`` themselves. Lazy; only built when
# something actually imports ``app``.
app = None  # type: ignore[assignment]


def _lazy_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app


__all__ = ["create_app", "verify_signature", "app"]
