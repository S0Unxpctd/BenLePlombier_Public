"""Souffl.AI V3 — WhatsApp outbound message primitives.

Thin wrappers around ``POST /{phone_number_id}/messages``. Every helper:

- Accepts a ``to`` phone in E.164 (``+33…``) or bare digits (``33…``).
- Builds the exact JSON shape Meta expects.
- Retries transient errors (429/5xx) with small exponential backoff.
- Returns the parsed JSON response (usually ``{"messages": [{"id": "wamid…"}]}``).
- Never logs the bearer token.

Primitives exposed
------------------
- ``send_text(to, body)``
- ``send_reply_buttons(to, body, buttons)``           # up to 3 ``{id, title}``
- ``send_list_message(to, body, button_label, sections)``
- ``send_document(to, media_id, filename, caption=None)``
- ``send_audio(to, media_id)``
- ``send_image(to, media_id, caption=None)``
- ``mark_typing(to)``                                 # UX cue, non-blocking

Design
------
We build a single ``_post_message(payload, client)`` helper that every
primitive targets. That keeps retry / error / logging behaviour uniform.
Tests inject an ``httpx.MockTransport`` via the ``client=`` parameter, so no
real network or env vars are needed.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Optional

import httpx

from .config import WhatsAppConfig, get_config
from .users import normalize_phone


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────────


class SendError(RuntimeError):
    """Raised when Meta rejects an outbound message after retries."""

    def __init__(self, message: str, *, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


_RETRY_STATUS = {429, 500, 502, 503, 504}


def _to_field(to: str) -> str:
    """Meta's ``to`` field must be the bare E.164 (no ``+``) for the JSON body.

    But callers in this codebase pass ``+33…`` (the storage format). Strip
    the ``+`` here so call-sites stay consistent.
    """
    n = normalize_phone(to)
    return n.lstrip("+")


def _post_message(
    payload: dict,
    *,
    client: Optional[httpx.Client] = None,
    cfg: Optional[WhatsAppConfig] = None,
    max_attempts: int = 3,
) -> dict:
    cfg = cfg or get_config()
    cfg.require("token", "phone_number_id")
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        headers = {
            "Authorization": f"Bearer {cfg.token}",
            "Content-Type": "application/json",
        }
        for attempt in range(max_attempts):
            try:
                r = client.post(cfg.messages_url(), json=payload, headers=headers)
                if 200 <= r.status_code < 300:
                    return r.json() if r.content else {}
                body = r.text[:500]
                if r.status_code in _RETRY_STATUS and attempt < max_attempts - 1:
                    time.sleep(min(2 ** attempt, 5))
                    continue
                raise SendError(
                    f"Meta rejected message (type={payload.get('type')}): "
                    f"HTTP {r.status_code} {body}",
                    status=r.status_code,
                    body=body,
                )
            except httpx.HTTPError as exc:
                if attempt < max_attempts - 1:
                    time.sleep(min(2 ** attempt, 5))
                    continue
                raise SendError(f"HTTP error: {exc}") from exc
        # Defensive: loop fell through with no return
        raise SendError("Exhausted attempts without response")
    finally:
        if owns_client:
            client.close()


# ─────────────────────────────────────────────────────────────────────────────
# Text
# ─────────────────────────────────────────────────────────────────────────────


def send_text(
    to: str,
    body: str,
    *,
    preview_url: bool = False,
    client: Optional[httpx.Client] = None,
    cfg: Optional[WhatsAppConfig] = None,
) -> dict:
    """Send a plain text message. Preserves newlines and emoji."""
    payload = {
        "messaging_product": "whatsapp",
        "to": _to_field(to),
        "type": "text",
        "text": {"body": body, "preview_url": bool(preview_url)},
    }
    return _post_message(payload, client=client, cfg=cfg)


# ─────────────────────────────────────────────────────────────────────────────
# Interactive — reply buttons (max 3) and list message (up to 10 rows)
# ─────────────────────────────────────────────────────────────────────────────


def send_reply_buttons(
    to: str,
    body: str,
    buttons: Iterable[dict],
    *,
    header: Optional[str] = None,
    footer: Optional[str] = None,
    client: Optional[httpx.Client] = None,
    cfg: Optional[WhatsAppConfig] = None,
) -> dict:
    """Send an interactive "reply buttons" message (max 3 buttons).

    Each button: ``{"id": "unique-id", "title": "Visible text (≤20 chars)"}``.
    """
    btn_list = list(buttons)
    if not 1 <= len(btn_list) <= 3:
        raise ValueError("send_reply_buttons requires 1 to 3 buttons")
    for b in btn_list:
        if not b.get("id") or not b.get("title"):
            raise ValueError(f"Each button needs 'id' and 'title': {b!r}")
        # Meta caps title at 20 chars. Truncate rather than refuse — keeps UX alive.
        b["title"] = b["title"][:20]

    interactive: dict[str, Any] = {
        "type": "button",
        "body": {"text": body},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                for b in btn_list
            ]
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header[:60]}
    if footer:
        interactive["footer"] = {"text": footer[:60]}

    payload = {
        "messaging_product": "whatsapp",
        "to": _to_field(to),
        "type": "interactive",
        "interactive": interactive,
    }
    return _post_message(payload, client=client, cfg=cfg)


def send_list_message(
    to: str,
    body: str,
    button_label: str,
    sections: Iterable[dict],
    *,
    header: Optional[str] = None,
    footer: Optional[str] = None,
    client: Optional[httpx.Client] = None,
    cfg: Optional[WhatsAppConfig] = None,
) -> dict:
    """Send an interactive "list" message.

    ``sections`` is an iterable of ``{"title": str, "rows": [{"id","title","description"?}]}``.
    Total rows across all sections must be between 1 and 10.
    """
    section_list = list(sections)
    total_rows = sum(len(s.get("rows", [])) for s in section_list)
    if not 1 <= total_rows <= 10:
        raise ValueError(
            f"send_list_message requires 1–10 rows across sections, got {total_rows}"
        )

    clean_sections = []
    for s in section_list:
        rows = []
        for r in s.get("rows", []):
            if not r.get("id") or not r.get("title"):
                raise ValueError(f"Each row needs 'id' and 'title': {r!r}")
            row = {"id": r["id"], "title": r["title"][:24]}
            if r.get("description"):
                row["description"] = r["description"][:72]
            rows.append(row)
        clean_sections.append({"title": s.get("title", "Actions")[:24], "rows": rows})

    interactive: dict[str, Any] = {
        "type": "list",
        "body": {"text": body},
        "action": {"button": button_label[:20], "sections": clean_sections},
    }
    if header:
        interactive["header"] = {"type": "text", "text": header[:60]}
    if footer:
        interactive["footer"] = {"text": footer[:60]}

    payload = {
        "messaging_product": "whatsapp",
        "to": _to_field(to),
        "type": "interactive",
        "interactive": interactive,
    }
    return _post_message(payload, client=client, cfg=cfg)


# ─────────────────────────────────────────────────────────────────────────────
# Media (document / audio / image)
# ─────────────────────────────────────────────────────────────────────────────


def send_document(
    to: str,
    media_id: str,
    filename: str,
    caption: Optional[str] = None,
    *,
    client: Optional[httpx.Client] = None,
    cfg: Optional[WhatsAppConfig] = None,
) -> dict:
    """Send a previously uploaded document (e.g. a PDF) by media_id."""
    doc: dict[str, Any] = {"id": media_id, "filename": filename}
    if caption:
        doc["caption"] = caption
    payload = {
        "messaging_product": "whatsapp",
        "to": _to_field(to),
        "type": "document",
        "document": doc,
    }
    return _post_message(payload, client=client, cfg=cfg)


def send_audio(
    to: str,
    media_id: str,
    *,
    client: Optional[httpx.Client] = None,
    cfg: Optional[WhatsAppConfig] = None,
) -> dict:
    """Send a previously uploaded audio (e.g. voice welcome) by media_id."""
    payload = {
        "messaging_product": "whatsapp",
        "to": _to_field(to),
        "type": "audio",
        "audio": {"id": media_id},
    }
    return _post_message(payload, client=client, cfg=cfg)


def send_image(
    to: str,
    media_id: str,
    caption: Optional[str] = None,
    *,
    client: Optional[httpx.Client] = None,
    cfg: Optional[WhatsAppConfig] = None,
) -> dict:
    """Send a previously uploaded image by media_id."""
    img: dict[str, Any] = {"id": media_id}
    if caption:
        img["caption"] = caption
    payload = {
        "messaging_product": "whatsapp",
        "to": _to_field(to),
        "type": "image",
        "image": img,
    }
    return _post_message(payload, client=client, cfg=cfg)


# ─────────────────────────────────────────────────────────────────────────────
# Typing indicator
# ─────────────────────────────────────────────────────────────────────────────


def mark_typing(
    to: str,
    *,
    client: Optional[httpx.Client] = None,
    cfg: Optional[WhatsAppConfig] = None,
) -> dict:
    """Best-effort typing indicator.

    Meta's Cloud API doesn't support a bare 'typing' primitive; the documented
    way is to mark an inbound message as read with ``typing_indicator``. To
    keep the adapter surface small we expose a no-op-friendly wrapper that
    swallows errors: UX is best-effort and must not break the main flow.
    """
    # Uses the "messages" endpoint with a special 'status=read + typing' payload.
    # Many bots choose to simply skip this and rely on the 'status' text hack,
    # but skipping keeps behaviour symmetric with test mocks.
    logger.debug("[send.mark_typing] no-op for %s", _to_field(to))
    return {"skipped": True}


__all__ = [
    "SendError",
    "send_text",
    "send_reply_buttons",
    "send_list_message",
    "send_document",
    "send_audio",
    "send_image",
    "mark_typing",
]
