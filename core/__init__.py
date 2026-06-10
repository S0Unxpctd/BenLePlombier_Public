"""Souffl.AI V3 — Transport-agnostic core package.

This package contains pure business logic extracted from the original bot.py.
It has ZERO dependency on a transport layer (Telegram, WhatsApp, web, CLI).

Modules
-------
- state_store : conversation state persistence (replaces `context.user_data`).
                Postgres-backed; channel-agnostic.
- quote_flow  : transcribe → extract → LLM generate → post-process.
- edit_flow   : patch existing quote + recompute totals (LLM patch + Python math).
- profile_flow: CRUD on artisan profile with a unified API (used by both
                Telegram's ConversationHandler and WhatsApp's inline flow).

Rules of engagement for this package
------------------------------------
1. No import of `telegram.*`, `fastapi.*`, `flask.*`, `whatsapp.*`.
2. No async I/O that is transport-specific. `async def` is fine as long as the
   function is pure or talks only to Postgres / OpenAI / OpenRouter.
3. All external effects (LLM call, DB call, file I/O) go through injectable
   callables so tests can mock them. Free functions are still allowed when
   they talk to already-pluggable modules (`db`, `client_store`, etc.).
4. The package must be import-safe without the transport libs installed
   (python-telegram-bot, fastapi, etc.). A test asserts this.
"""

__all__ = ["state_store", "quote_flow", "edit_flow", "profile_flow"]
