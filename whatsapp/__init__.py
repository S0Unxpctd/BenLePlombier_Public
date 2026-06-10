"""Souffl.AI V3 — WhatsApp Cloud API adapter.

This package is the transport-layer adapter for WhatsApp. It consumes
`V3/core/*` (transport-agnostic business logic) and the existing modules
(`client_store`, `devis_store`, `facture_generator`, `email_sender`,
`pdf_generator`, `system_prompt`) without modifying them.

Module map
----------
- ``config``      environment-variable loading and shared client factories
- ``users``      phone_e164 ↔ user_id mapping (Postgres-backed)
- ``media``      inbound media download + outbound media upload (Meta 2-step)
- ``send``       outbound message primitives: text / reply_buttons / list /
                 document / audio / image / typing
- ``commands``   French-language text-command parser (``profil``, ``devis``…)
- ``webhook``    FastAPI app: ``GET /webhook`` verification + signed ``POST``
- ``router``     dispatches inbound messages to flows
- ``flows``      voice→quote, onboarding, edit, facture, email
"""

__all__ = [
    "config",
    "users",
    "media",
    "send",
    "commands",
    "webhook",
    "router",
    "flows",
]
