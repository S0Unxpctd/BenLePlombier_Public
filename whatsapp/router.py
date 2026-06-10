"""Souffl.AI V3 — WhatsApp inbound event router.

Single entry point: ``dispatch_webhook_event(payload)``. Given a parsed
Meta webhook body (``{"object":"whatsapp_business_account","entry":[…]}``),
it walks the nested ``entry → changes → value → messages`` structure,
resolves or creates the artisan ``user_id`` for each message, and hands
off to the right flow.

The router intentionally keeps *no* channel state of its own: every piece of
conversational context goes through ``core.state_store`` keyed by ``user_id``.

Extraction shape (one message at a time)
----------------------------------------
For each inbound ``message`` we produce:

    InboundMessage(
        phone_e164=str,             # +33600000000
        user_id=int,                # resolved via whatsapp.users.get_or_create_user
        wamid=str,                  # Meta message id (for idempotency logs)
        msg_type=str,               # text | audio | image | document | interactive | …
        payload=dict,               # the typed sub-dict (message["text"], ["audio"], …)
        profile_name=str,           # contacts[0].profile.name, may be ""
    )

Dispatching rules
-----------------
1. If the user has no artisan profile yet → onboarding flow handles every
   message type.
2. Else if the message is an **interactive reply** (button / list), route to
   the button-action handler (lives in ``flows``).
3. Else if the user is in an active conversational state (edit / email /
   pre-price) → route to the state-specific handler.
4. Else:
   - text: parse as a command; if recognised, dispatch; else, if no active
     state, fall back to "menu" hint.
   - audio: start a new quote flow.
   - image / document: offer to use the document for onboarding or,
     in Phase 2, politely inform we only process voice memos (Phase 5 will
     enrich with images).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from . import users as users_mod


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Inbound message normalisation
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class InboundMessage:
    phone_e164: str
    user_id: int
    wamid: str
    msg_type: str
    payload: dict
    profile_name: str = ""


def iter_messages(webhook_payload: dict):
    """Yield ``(contacts[0], messages[i])`` dicts from a Meta webhook payload.

    Skips status updates (``statuses`` key inside ``value``) — they don't
    represent inbound user messages.
    """
    if not isinstance(webhook_payload, dict):
        return
    if webhook_payload.get("object") != "whatsapp_business_account":
        return
    for entry in webhook_payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            contacts = value.get("contacts") or []
            contact0 = contacts[0] if contacts else {}
            for msg in value.get("messages", []) or []:
                yield contact0, msg


def build_inbound(
    contact: dict,
    message: dict,
    *,
    cursor_factory=None,
) -> Optional[InboundMessage]:
    """Normalise one Meta message dict into an ``InboundMessage``.

    Returns ``None`` if the message has no sender (bizarre but possible).
    """
    from_field = message.get("from")
    if not from_field:
        return None
    phone = users_mod.normalize_phone(from_field)
    user_id = users_mod.get_or_create_user(phone, cursor_factory=cursor_factory)
    msg_type = message.get("type", "unknown")
    payload = message.get(msg_type, {}) if isinstance(message.get(msg_type), dict) else {}
    profile_name = ""
    prof = (contact or {}).get("profile") or {}
    if isinstance(prof, dict):
        profile_name = prof.get("name", "") or ""
    return InboundMessage(
        phone_e164=phone,
        user_id=user_id,
        wamid=message.get("id", ""),
        msg_type=msg_type,
        payload=payload,
        profile_name=profile_name,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Top-level dispatch
# ─────────────────────────────────────────────────────────────────────────────


def dispatch_webhook_event(
    webhook_payload: dict,
    *,
    deps: Optional["FlowDeps"] = None,
) -> list[dict]:
    """Walk the webhook payload and run each message through the flow engine.

    Returns a list of small ``{status, wamid}`` dicts — mostly useful for
    testing / debugging. Production callers don't inspect the return value.
    """
    from .flows import handle_inbound, FlowDeps  # local import: avoid cycle

    deps = deps or FlowDeps.default()
    results: list[dict] = []
    for contact, message in iter_messages(webhook_payload):
        try:
            inbound = build_inbound(
                contact, message, cursor_factory=deps.cursor_factory
            )
            if inbound is None:
                continue
            handle_inbound(inbound, deps=deps)
            results.append({"status": "ok", "wamid": inbound.wamid})
        except Exception as exc:
            logger.exception("[router] failed to process message: %s", exc)
            results.append({"status": "error", "error": str(exc)[:200]})
    return results


__all__ = [
    "InboundMessage",
    "iter_messages",
    "build_inbound",
    "dispatch_webhook_event",
]
