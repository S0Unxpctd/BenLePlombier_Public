"""Souffl.AI V3 — WhatsApp conversational flows.

This module composes:
  - ``core.state_store``     (per-user conversational state, Postgres-backed)
  - ``core.quote_flow``      (voice → quote pipeline)
  - ``core.edit_flow``       (edit quote via patch)
  - ``core.profile_flow``    (profile CRUD + onboarding step list)
  - existing modules:        ``client_store``, ``devis_store``,
                              ``facture_generator``, ``email_sender``,
                              ``pdf_generator``, ``system_prompt``
  - ``whatsapp.send``, ``whatsapp.media``, ``whatsapp.commands``

…into one ``handle_inbound(inbound, deps)`` entry point that implements the
full Phase 2 feature parity with the Telegram bot.

State shape (stored in ``ConversationState.data`` as JSONB)
----------------------------------------------------------
Onboarding:
    onboarding_step_idx: int            # 0-based index into ONBOARDING_STEPS
Pre-devis (after a voice memo):
    pending_transcription: str
    pre_extra: {"client": str|None, "duree": str|None, "marques": str|None,
                 "prix_postes": [...]}
    pre_transcription_items: [...]      # cached from quick_extract
    pre_prix_items: [...]               # snapshot once pre-price step starts
    pre_prix_idx: int
    pre_prix_values: {str(idx): float|None}
    pre_step: str                       # "client" | "prix"
Edit flow:
    editing_devis: str                  # numero_devis
Email flow:
    waiting_for_email: "devis" | "facture"
    pending_send: {"type": str, "numero": str}
Intermediate invoice amount:
    waiting_facture_inter: {"numero_devis": str}

Everything above is transport-agnostic — the Telegram bot uses the same
keys (just in ``context.user_data``). That symmetry is what lets Phase 1's
``core`` extraction do real work: the flows here can reuse the core helpers
unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from core import state_store as _state
from core import quote_flow as _quote
from core import edit_flow as _edit
from core import profile_flow as _profile

from . import send as _send
from . import media as _media
from .commands import Command, parse_command
from .router import InboundMessage


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Dependency container
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FlowDeps:
    """All the externals the flows need. Injectable for tests.

    Defaults are populated from env / existing modules by ``default()``.
    Tests pass a fully-mocked instance — nothing in this file reads os.environ
    or the network directly.
    """

    store: _state.StateStore
    llm_client: Any                       # OpenAI-compatible
    whisper_client: Any                   # OpenAI-compatible
    model: str                            # OpenRouter model id
    system_prompt_fn: Callable[[int], str]  # user_id → injected prompt
    transcribe: Callable[[str], str]      # path → text
    pdf_output_dir: str
    cursor_factory: Optional[Callable] = None  # for users_mod in router
    send_text: Callable[..., Any] = _send.send_text
    send_buttons: Callable[..., Any] = _send.send_reply_buttons
    send_list: Callable[..., Any] = _send.send_list_message
    send_document: Callable[..., Any] = _send.send_document
    send_audio: Callable[..., Any] = _send.send_audio
    upload_media: Callable[..., Any] = _media.upload_media
    download_media: Callable[..., Any] = _media.download_media
    # Injection point for PDF generator and devis store (kept as late imports to
    # allow test-time swapping without loading WeasyPrint).
    generate_devis_pdf: Optional[Callable] = None
    generate_facture_pdf: Optional[Callable] = None
    save_devis_fn: Optional[Callable] = None
    load_devis_fn: Optional[Callable] = None
    list_devis_fn: Optional[Callable] = None
    save_facture_fn: Optional[Callable] = None
    load_facture_fn: Optional[Callable] = None
    get_factures_for_devis_fn: Optional[Callable] = None
    get_total_deja_facture_fn: Optional[Callable] = None
    devis_to_facture_typed_fn: Optional[Callable] = None
    send_devis_email_fn: Optional[Callable] = None
    inject_profile_fn: Optional[Callable] = None
    # Phase 3 feature flags and debounce wiring
    recap_enabled: bool = False  # enabled in default(), disabled in tests for regression
    debounce_window_s: int = 0   # 0 = instant process (regression), 10 = normal
    enqueue_voice_fn: Optional[Callable] = None
    transcribe_store_fn: Optional[Callable] = None
    fetch_pending_fn: Optional[Callable] = None
    mark_processed_fn: Optional[Callable] = None
    extras: dict = field(default_factory=dict)  # for future use

    @classmethod
    def default(cls) -> "FlowDeps":
        """Build the prod wiring from env + existing modules. Lazy imports so
        importing this module doesn't require OpenAI / WeasyPrint at test time.
        """
        from openai import OpenAI
        from system_prompt import SYSTEM_PROMPT
        from client_store import get_client, inject_profile_in_prompt
        from pdf_generator import generate_pdf as generate_devis_pdf
        from facture_generator import (
            generate_facture_pdf as gen_facture_pdf,
            devis_to_facture_typed,
        )
        from devis_store import (
            save_devis,
            load_devis,
            list_devis,
            save_facture,
            load_facture,
            get_factures_for_devis,
            get_total_deja_facture,
        )
        from email_sender import send_devis_email

        store = _state.build_default_store()
        # .strip() on every secret/env — Railway/Meta/OpenRouter copy-paste often
        # introduces stray whitespace or a trailing newline, which httpx rejects
        # as an illegal header value (see LocalProtocolError on "Bearer ...\n\n").
        model = os.getenv("OPENROUTER_MODEL", "anthropic/claude-3-5-sonnet").strip()
        pdf_dir = os.getenv(
            "PDF_OUTPUT_DIR",
            str(Path(__file__).resolve().parent.parent / "data" / "pdf"),
        ).strip()
        Path(pdf_dir).mkdir(parents=True, exist_ok=True)

        llm = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        )
        whisper = OpenAI(api_key=os.getenv("OPENAI_API_KEY", "").strip())

        def _sysprompt(user_id: int) -> str:
            return inject_profile_in_prompt(SYSTEM_PROMPT, get_client(user_id) or {})

        def _transcribe(path: str) -> str:
            with open(path, "rb") as f:
                return whisper.audio.transcriptions.create(
                    model="whisper-1", file=f, language="fr"
                ).text.strip()

        # Import debounce helpers for Phase 3
        from whatsapp import debounce as _debounce

        return cls(
            store=store,
            llm_client=llm,
            whisper_client=whisper,
            model=model,
            system_prompt_fn=_sysprompt,
            transcribe=_transcribe,
            pdf_output_dir=pdf_dir,
            generate_devis_pdf=generate_devis_pdf,
            generate_facture_pdf=gen_facture_pdf,
            save_devis_fn=save_devis,
            load_devis_fn=load_devis,
            list_devis_fn=list_devis,
            save_facture_fn=save_facture,
            load_facture_fn=load_facture,
            get_factures_for_devis_fn=get_factures_for_devis,
            get_total_deja_facture_fn=get_total_deja_facture,
            devis_to_facture_typed_fn=devis_to_facture_typed,
            send_devis_email_fn=send_devis_email,
            inject_profile_fn=inject_profile_in_prompt,
            # Phase 3 feature flags — ON in production
            recap_enabled=True,
            debounce_window_s=10,
            enqueue_voice_fn=_debounce.enqueue_voice,
            transcribe_store_fn=_debounce.transcribe_and_store,
            fetch_pending_fn=_debounce.fetch_pending_for_user,
            mark_processed_fn=_debounce.mark_processed,
        )


# ─────────────────────────────────────────────────────────────────────────────
# UI copy — all FR user-facing strings live here for easy review
# ─────────────────────────────────────────────────────────────────────────────


BTN_YES_CLIENT = "pre_client:add"
BTN_NO_CLIENT = "pre_client:skip"
BTN_AI_DECIDES = "pre_prix:ai"
BTN_AI_DECIDES_ALL = "pre_prix:ai_all"

BTN_EDIT = "devis:edit"
BTN_SEND = "devis:send"
BTN_INVOICE = "devis:invoice"

BTN_FACT_ACOMPTE = "fact:acompte"
BTN_FACT_INTER = "fact:intermediaire"
BTN_FACT_SOLDE = "fact:solde"

MSG_WELCOME_NEW = (
    "👋 Bienvenue sur *Souffl.AI* !\n"
    "Avant de générer votre premier devis, créons votre profil artisan.\n\n"
    "Je vais vous poser 12 questions rapides. Répondez par texte. "
    "Pour sauter une question, écrivez *passer*."
)
MSG_NEED_PROFILE = (
    "👋 Vous devez d'abord créer votre profil. Tapez *recommencer* pour démarrer."
)
MSG_MENU_HINT = (
    "🎙️ Envoyez un *message vocal* pour générer un devis.\n"
    "Tapez *menu* pour voir toutes les options, ou *aide* pour l'aide."
)
MSG_EMPTY_TRANSCRIPTION = "❌ Transcription vide. Réessayez dans un endroit calme."
MSG_CLIENT_QUESTION = (
    "👤 *Voulez-vous ajouter les coordonnées du client ?*\n"
    "_Nom, adresse, téléphone — utile pour personnaliser le devis._"
)
MSG_CLIENT_ASK = (
    "👤 *Coordonnées du client ?*\n"
    "_Nom, adresse, téléphone — ou tapez 'passer' si pas disponible._"
)

HELP_TEXT = (
    "🛠️ *Commandes disponibles*\n\n"
    "• *menu* — ouvrir le menu principal\n"
    "• *profil* — voir/modifier votre profil\n"
    "• *devis* — lister vos derniers devis\n"
    "• *facture* — créer une facture à partir d'un devis\n"
    "• *annuler* — annuler l'opération en cours\n"
    "• *recommencer* — tout remettre à zéro\n\n"
    "💡 Pour créer un devis : envoyez un *vocal* décrivant le chantier."
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _is_skip(text: str) -> bool:
    return text.strip().lower() in {"passer", "skip", "non", "aucun", "pas de client"}


def _set_step(state: _state.ConversationState, flow: Optional[str], step: Optional[str]) -> None:
    state.channel = _state.CHANNEL_WHATSAPP
    state.flow = flow
    state.step = step


def _devis_buttons() -> list[dict]:
    return [
        {"id": BTN_EDIT, "title": "✏️ Modifier"},
        {"id": BTN_SEND, "title": "📧 Envoyer"},
        {"id": BTN_INVOICE, "title": "🧾 Facture"},
    ]


def _save_pdf_devis(devis: dict, user_id: int, deps: FlowDeps) -> str:
    numero = devis.get("meta", {}).get("numero_devis", f"DEVIS-{uuid.uuid4().hex[:6]}")
    filename = f"{numero}.pdf"
    out_path = os.path.join(deps.pdf_output_dir, filename)
    if deps.generate_devis_pdf is None:
        raise RuntimeError("generate_devis_pdf not wired")
    deps.generate_devis_pdf(devis, out_path)
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def handle_inbound(inbound: InboundMessage, *, deps: FlowDeps) -> None:
    """Route a normalised ``InboundMessage`` to the right flow.

    This function is intentionally synchronous (no ``async``) so it can be
    called from the webhook task wrapper with a consistent contract. Any
    internal operation that needs async would be done via ``loop.run_in_executor``
    — but right now every external (OpenAI client, pdf generator, httpx) is
    synchronous.
    """
    from client_store import client_exists, get_client, save_client

    user_id = inbound.user_id
    phone = inbound.phone_e164

    # Phase 3: Try to flush debounce queue if a message arrives.
    # "ok" is a special trigger that forces immediate flush regardless of window.
    force_flush = (
        inbound.msg_type == "text"
        and (inbound.payload.get("body", "") or "").strip().lower() in ("ok", "go", "c'est bon", "c bon", "generer", "générer")
    )
    flushed = _maybe_flush_debounce(user_id, phone, deps, force=force_flush)
    if flushed:
        return  # debounce flush handled the message, don't process "ok" as a command

    profile_ready = client_exists(user_id)

    # Commands and admin-style messages always have priority, even when
    # another flow is active. Exception: the onboarding flow owns everything
    # for a first-time user; we only honour "aide" and "annuler" there.
    text = ""
    if inbound.msg_type == "text":
        text = inbound.payload.get("body", "") or ""
    cmd = parse_command(text) if text else None

    if cmd is Command.ANNULER:
        _handle_cancel(user_id, phone, deps)
        return
    if cmd is Command.RECOMMENCER:
        deps.store.delete(user_id)
        if profile_ready:
            deps.send_text(phone, "🔄 État réinitialisé. Envoyez un vocal pour un nouveau devis.")
        else:
            _start_onboarding(user_id, phone, deps)
        return
    if cmd is Command.AIDE:
        deps.send_text(phone, HELP_TEXT)
        return

    # First-time user, OR mid-onboarding even if partial profile is saved: route to onboarding.
    _existing_state = deps.store.get(user_id)
    _in_onboarding = (
        _existing_state is not None
        and _existing_state.data.get("onboarding_step_idx") is not None
    )
    if not profile_ready or _in_onboarding:
        _onboarding_step(inbound, deps)
        return

    # Interactive replies (button / list)
    if inbound.msg_type == "interactive":
        _handle_interactive(inbound, deps)
        return

    # Active conversational state takes priority over other commands
    state = deps.store.get(user_id)
    data = dict(state.data) if state else {}

    # Phase 3: Recap edit flow (edit pending_devis, not a saved numero)
    if data.get("editing_recap") and inbound.msg_type in ("text", "audio"):
        _apply_recap_edit(user_id, phone, inbound, deps)
        return

    # Edit flow (text or voice routed as text)
    if data.get("editing_devis"):
        _apply_edit(user_id, phone, inbound, deps)
        return

    # Email pending input
    if data.get("waiting_for_email"):
        if inbound.msg_type == "text":
            _handle_email_input(user_id, phone, text, deps)
            return

    # Intermediate invoice amount pending
    if data.get("waiting_facture_inter"):
        if inbound.msg_type == "text":
            _handle_inter_amount(user_id, phone, text, deps)
            return

    # Pre-devis: client then prix
    if data.get("pre_step") == "client" and inbound.msg_type == "text":
        _handle_client_text(user_id, phone, text, deps)
        return
    if data.get("pre_step") == "prix" and inbound.msg_type == "text":
        _handle_prix_text(user_id, phone, text, deps)
        return

    # Now command dispatch (profile/menu/devis/facture/export)
    if cmd is Command.PROFIL:
        _show_profile_summary(user_id, phone, deps)
        return
    if cmd is Command.DEVIS:
        _list_recent_devis(user_id, phone, deps)
        return
    if cmd is Command.MENU:
        _show_main_menu(phone, deps)
        return
    if cmd is Command.FACTURE:
        _start_facture_picker(user_id, phone, deps)
        return
    if cmd is Command.EXPORT:
        _handle_export(user_id, phone, deps)
        return

    # Audio → start new quote
    if inbound.msg_type == "audio":
        _start_voice_quote(inbound, deps)
        return

    # Fallback
    if inbound.msg_type in ("image", "document"):
        deps.send_text(
            phone,
            "🖼️ Merci pour le média ! En MVP je ne traite que les messages vocaux et texte. "
            "Envoyez un vocal pour générer un devis.",
        )
        return

    # Default: menu hint
    deps.send_text(phone, MSG_MENU_HINT)


# ─────────────────────────────────────────────────────────────────────────────
# Cancel
# ─────────────────────────────────────────────────────────────────────────────


def _handle_cancel(user_id: int, phone: str, deps: FlowDeps) -> None:
    # Clear transient flow state, keep onboarding progress if any.
    def mutator(s: _state.ConversationState) -> None:
        s.data.pop("editing_devis", None)
        s.data.pop("waiting_for_email", None)
        s.data.pop("pending_send", None)
        s.data.pop("waiting_facture_inter", None)
        s.data.pop("pre_step", None)
        s.data.pop("pending_transcription", None)
        s.data.pop("pre_extra", None)
        s.data.pop("pre_transcription_items", None)
        s.data.pop("pre_prix_items", None)
        s.data.pop("pre_prix_idx", None)
        s.data.pop("pre_prix_values", None)
        s.flow = None
        s.step = None

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)
    deps.send_text(phone, "✅ Opération en cours annulée.")


# ─────────────────────────────────────────────────────────────────────────────
# Onboarding
# ─────────────────────────────────────────────────────────────────────────────


def _start_onboarding(user_id: int, phone: str, deps: FlowDeps) -> None:
    from client_store import save_client

    # Seed an empty profile so client_exists() returns True *before* onboarding
    # finishes? NO — we keep the profile absent until the first answer so the
    # "not profile_ready" branch catches them. The step index lives in state.
    def mutator(s: _state.ConversationState) -> None:
        _set_step(s, "onboarding", "step_0")
        s.data["onboarding_step_idx"] = 0

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)
    deps.send_text(phone, MSG_WELCOME_NEW)
    _ask_onboarding_question(phone, 0, deps)


def _ask_onboarding_question(phone: str, idx: int, deps: FlowDeps) -> None:
    if idx >= len(_profile.ONBOARDING_STEPS):
        return
    step = _profile.ONBOARDING_STEPS[idx]
    prompt = step.prompt_fr
    if step.help_hint_fr:
        prompt += f"\n_{step.help_hint_fr}_"
    prompt += f"\n\n_Question {idx + 1}/{len(_profile.ONBOARDING_STEPS)} — écrivez *passer* pour sauter._"
    deps.send_text(phone, prompt)


def _onboarding_step(inbound: InboundMessage, deps: FlowDeps) -> None:
    """Linear onboarding: one question per ``ONBOARDING_STEPS`` entry.

    Text-only at MVP. Photos / PDFs of a reference quote → out of scope for
    Phase 2 (backlog: Phase 4 Flows).
    """
    from client_store import get_client, save_client, empty_profile

    user_id = inbound.user_id
    phone = inbound.phone_e164
    text = inbound.payload.get("body", "") if inbound.msg_type == "text" else ""

    state = deps.store.get(user_id)
    if state is None or state.data.get("onboarding_step_idx") is None:
        _start_onboarding(user_id, phone, deps)
        return

    idx = int(state.data["onboarding_step_idx"])
    if idx >= len(_profile.ONBOARDING_STEPS):
        deps.send_text(phone, "✅ Profil complet — envoyez un vocal pour votre premier devis.")
        return

    step = _profile.ONBOARDING_STEPS[idx]

    # Accept only text for now; if it's audio or media, nudge the user.
    if inbound.msg_type != "text":
        deps.send_text(phone, f"Répondez par texte s'il vous plaît.\n\n{step.prompt_fr}")
        return

    stripped = (text or "").strip()
    profile = get_client(user_id) or empty_profile()

    if stripped.lower() in {"passer", "skip"}:
        pass  # leave the field at its default
    else:
        try:
            profile = _profile.apply_field_update(profile, step.field, stripped)
        except ValueError:
            deps.send_text(
                phone,
                f"⚠️ Valeur invalide pour *{step.field}*. Réessayez ou tapez *passer*.",
            )
            return

    save_client(user_id, profile)

    next_idx = idx + 1
    def mutator(s: _state.ConversationState) -> None:
        s.data["onboarding_step_idx"] = next_idx
        _set_step(s, "onboarding", f"step_{next_idx}")

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)

    if next_idx < len(_profile.ONBOARDING_STEPS):
        _ask_onboarding_question(phone, next_idx, deps)
    else:
        # Finished — clear onboarding state, welcome them in.
        def done_mutator(s: _state.ConversationState) -> None:
            s.data.pop("onboarding_step_idx", None)
            s.data["welcome_sent"] = True  # Mark welcome sent so it only fires once
            _set_step(s, None, None)

        deps.store.update(user_id, _state.CHANNEL_WHATSAPP, done_mutator)
        deps.send_text(
            phone,
            "🎉 *Profil complet !* Envoyez-moi un *message vocal* décrivant votre chantier "
            "et je génère votre devis.",
        )
        # Phase 3: Send welcome voice note
        _send_welcome_if_possible(phone, deps)


# ─────────────────────────────────────────────────────────────────────────────
# Voice → quote
# ─────────────────────────────────────────────────────────────────────────────


def _start_voice_quote(inbound: InboundMessage, deps: FlowDeps) -> None:
    user_id = inbound.user_id
    phone = inbound.phone_e164
    media_id = inbound.payload.get("id")
    wamid = inbound.wamid
    if not media_id:
        deps.send_text(phone, "❌ Vocal illisible. Réessayez.")
        return

    # Phase 3: Enqueue for debounce (even if debounce_window_s=0, we enqueue but process immediately)
    if deps.enqueue_voice_fn:
        deps.enqueue_voice_fn(user_id, wamid, media_id)

    deps.send_text(phone, "🎙️ *Message reçu !* Transcription en cours…")
    try:
        with deps.download_media(media_id, suffix=".ogg") as audio_path:
            transcription = deps.transcribe(audio_path)
    except Exception as exc:
        logger.exception("[flows] voice download/transcribe failed")
        deps.send_text(phone, f"❌ Erreur transcription : {type(exc).__name__}")
        return

    if not transcription:
        deps.send_text(phone, MSG_EMPTY_TRANSCRIPTION)
        return

    # Phase 3: Store transcription in debounce queue
    if deps.transcribe_store_fn:
        deps.transcribe_store_fn(wamid, transcription)

    # If user is mid-edit, route as text to the edit flow.
    state = deps.store.get(user_id)
    if state and state.data.get("editing_devis"):
        fake = InboundMessage(
            phone_e164=phone,
            user_id=user_id,
            wamid=inbound.wamid,
            msg_type="text",
            payload={"body": transcription},
            profile_name=inbound.profile_name,
        )
        _apply_edit(user_id, phone, fake, deps)
        return

    # Phase 3: If debounce is enabled, defer processing — let _maybe_flush_debounce
    # handle it on the next inbound message when the window expires.
    if deps.debounce_window_s > 0:
        # Check if there are older pending voices ready to flush NOW
        if deps.fetch_pending_fn:
            pending = deps.fetch_pending_fn(user_id, deps.debounce_window_s)
            if len(pending) > 1:
                # Multiple voices ready — flush them all together
                transcriptions = [p.get("transcription", "") for p in pending]
                concatenated = " ".join(t for t in transcriptions if t)
                if deps.mark_processed_fn:
                    deps.mark_processed_fn([p.get("wamid") for p in pending])
                _process_concatenated_voice(user_id, phone, concatenated, deps)
                return
        # Otherwise, wait for more voices or for the window to expire
        deps.send_text(phone, "⏳ Message vocal enregistré. Envoyez un autre vocal ou tapez *ok* pour générer le devis.")
        return

    # debounce_window_s == 0 → process immediately (regression/test mode)
    if deps.mark_processed_fn:
        deps.mark_processed_fn([wamid])
    _process_concatenated_voice(user_id, phone, transcription, deps)


def _handle_client_text(user_id: int, phone: str, text: str, deps: FlowDeps) -> None:
    def mutator(s: _state.ConversationState) -> None:
        extra = s.data.setdefault("pre_extra", {})
        if _is_skip(text):
            extra["client"] = None
        else:
            extra["client"] = text.strip()
        s.data.pop("pre_step", None)

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)
    _launch_prix_step(user_id, phone, deps)


def _launch_prix_step(user_id: int, phone: str, deps: FlowDeps) -> None:
    """Start asking per-line prices, or generate immediately if no lines."""
    state = deps.store.get(user_id)
    items = (state.data.get("pre_transcription_items") if state else []) or []

    if not items:
        _do_generate_devis(user_id, phone, deps)
        return

    def mutator(s: _state.ConversationState) -> None:
        s.data["pre_prix_items"] = items
        s.data["pre_prix_idx"] = 0
        s.data["pre_prix_values"] = {}
        s.data["pre_step"] = "prix"  # routing in handle_inbound reads data["pre_step"]
        _set_step(s, "pre_devis", "prix")

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)
    _ask_next_prix_question(user_id, phone, deps)


def _ask_next_prix_question(user_id: int, phone: str, deps: FlowDeps) -> None:
    state = deps.store.get(user_id)
    if state is None:
        return
    items = state.data.get("pre_prix_items", []) or []
    values = dict(state.data.get("pre_prix_values", {}))
    idx = int(state.data.get("pre_prix_idx", 0))

    # Skip items with explicit prices
    while idx < len(items):
        item = items[idx]
        if str(idx) in values:
            idx += 1
            continue
        if item.get("prix_unitaire_ht") is not None:
            values[str(idx)] = item["prix_unitaire_ht"]
            idx += 1
            continue
        break

    def mutator(s: _state.ConversationState) -> None:
        s.data["pre_prix_values"] = values
        s.data["pre_prix_idx"] = idx

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)

    if idx >= len(items):
        _do_generate_devis(user_id, phone, deps)
        return

    item = items[idx]
    total = len(items)
    body = (
        f"💶 *Poste {idx + 1}/{total} — {item.get('description', 'Poste')}*\n"
        f"_Quantité estimée : {item.get('quantite', 1)} {item.get('unite', 'forfait')}_\n\n"
        "Quel prix unitaire HT souhaitez-vous facturer ?\n"
        "_Entrez un montant en € (ex : 350) ou appuyez sur un bouton._"
    )
    deps.send_buttons(
        phone,
        body,
        [
            {"id": BTN_AI_DECIDES, "title": "🤖 IA décide"},
            {"id": BTN_AI_DECIDES_ALL, "title": "⚡ Tout IA"},
        ],
    )


def _handle_prix_text(user_id: int, phone: str, text: str, deps: FlowDeps) -> None:
    state = deps.store.get(user_id)
    if state is None:
        deps.send_text(phone, MSG_MENU_HINT)
        return
    idx = int(state.data.get("pre_prix_idx", 0))
    values = dict(state.data.get("pre_prix_values", {}))
    try:
        clean = text.strip().replace(",", ".").replace("€", "").replace(" ", "")
        values[str(idx)] = float(clean)
    except ValueError:
        deps.send_text(
            phone,
            "⚠️ Prix non reconnu. Entrez un nombre (ex : *350*), ou appuyez sur un bouton.",
        )
        return

    def mutator(s: _state.ConversationState) -> None:
        s.data["pre_prix_values"] = values
        s.data["pre_prix_idx"] = idx + 1

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)
    _ask_next_prix_question(user_id, phone, deps)


def _do_generate_devis(user_id: int, phone: str, deps: FlowDeps) -> None:
    """Build the full devis and send the PDF."""
    from client_store import get_client, inject_profile_in_prompt
    from system_prompt import SYSTEM_PROMPT

    state = deps.store.get(user_id)
    if state is None:
        deps.send_text(phone, MSG_MENU_HINT)
        return

    transcription = state.data.get("pending_transcription", "")
    extra = dict(state.data.get("pre_extra") or {})
    prix_items = state.data.get("pre_prix_items", []) or []
    prix_values = dict(state.data.get("pre_prix_values") or {})

    prix_postes: list[dict] = []
    for i, item in enumerate(prix_items):
        val = prix_values.get(str(i))
        if val is not None:
            prix_postes.append(
                {
                    "description": item.get("description", ""),
                    "prix_unitaire_ht": val,
                    "unite": item.get("unite", "forfait"),
                }
            )
    if prix_postes:
        extra["prix_postes"] = prix_postes

    deps.send_text(phone, "🤖 *Génération du devis en cours…*")

    profile = get_client(user_id) or {}
    sys_prompt = (deps.inject_profile_fn or inject_profile_in_prompt)(
        SYSTEM_PROMPT, profile
    )

    try:
        devis = _quote.generate_quote(
            transcription,
            profile,
            extra,
            deps.llm_client,
            deps.model,
            sys_prompt,
        )
    except Exception as exc:
        logger.exception("[flows] generate_quote failed")
        deps.send_text(phone, f"❌ Erreur IA : {type(exc).__name__}")
        _clear_pre_devis(user_id, deps)
        return

    if "erreur" in devis:
        deps.send_text(phone, f"⚠️ {devis.get('message', 'Description insuffisante.')}")
        _clear_pre_devis(user_id, deps)
        return

    # Phase 3: If recap_enabled, show a preview before the PDF
    if deps.recap_enabled:
        recap_msg = _build_recap_message(devis)

        def recap_mutator(s: _state.ConversationState) -> None:
            s.data["pending_devis"] = devis
            s.data["pre_step"] = "recap"
            _set_step(s, "recap", "confirm")

        deps.store.update(user_id, _state.CHANNEL_WHATSAPP, recap_mutator)

        deps.send_text(phone, recap_msg)
        deps.send_buttons(
            phone,
            "Tout est bon ?",
            [
                {"id": "recap:confirm", "title": "✅ Générer le PDF"},
                {"id": "recap:edit", "title": "✏️ Modifier"},
                {"id": "recap:cancel", "title": "❌ Recommencer"},
            ],
        )
        return

    # Old flow (recap_enabled=False): save and send PDF immediately
    try:
        pdf_path = _save_pdf_devis(devis, user_id, deps)
        if deps.save_devis_fn:
            deps.save_devis_fn(user_id, devis)
    except Exception as exc:
        logger.exception("[flows] pdf/save failed")
        deps.send_text(phone, f"❌ Erreur PDF : {type(exc).__name__}")
        _clear_pre_devis(user_id, deps)
        return

    # Upload and send the PDF with buttons
    numero = devis.get("meta", {}).get("numero_devis", "")
    caption = _build_recap_caption(devis)
    try:
        media_id = deps.upload_media(pdf_path, mime_type="application/pdf")
        deps.send_document(phone, media_id, filename=os.path.basename(pdf_path), caption=caption)
        deps.send_buttons(phone, f"Que souhaitez-vous faire avec le devis `{numero}` ?", _devis_buttons())
    except Exception:
        logger.exception("[flows] send document failed")
        deps.send_text(phone, "✅ Devis généré. Je n'ai pas pu l'envoyer — réessayez plus tard.")

    _clear_pre_devis(user_id, deps)


def _clear_pre_devis(user_id: int, deps: FlowDeps) -> None:
    def mutator(s: _state.ConversationState) -> None:
        for key in (
            "pending_transcription",
            "pre_extra",
            "pre_transcription_items",
            "pre_prix_items",
            "pre_prix_idx",
            "pre_prix_values",
            "pre_step",
        ):
            s.data.pop(key, None)
        _set_step(s, None, None)

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)


def _build_recap_caption(devis: dict) -> str:
    totals = devis.get("totaux", {})
    meta = devis.get("meta", {})
    return (
        f"✅ *Devis généré* — `{meta.get('numero_devis', '—')}`\n"
        f"📋 *Chantier* : {meta.get('reference_chantier', '—')}\n"
        f"💶 *Total TTC* : {totals.get('total_ttc', 0):,.2f} €"
    ).replace(",", " ")  # mild FR formatting


# ─────────────────────────────────────────────────────────────────────────────
# Interactive (button / list) replies
# ─────────────────────────────────────────────────────────────────────────────


def _handle_interactive(inbound: InboundMessage, deps: FlowDeps) -> None:
    """Dispatch interactive replies. IDs follow the ``section:action`` pattern."""
    user_id = inbound.user_id
    phone = inbound.phone_e164
    interactive = inbound.payload
    # Button reply shape: {"type":"button_reply","button_reply":{"id":…,"title":…}}
    # List reply shape:   {"type":"list_reply","list_reply":{"id":…,"title":…}}
    kind = interactive.get("type")
    reply = interactive.get(kind, {}) if kind else {}
    rid = reply.get("id", "")

    # Phase 3: Menu list replies
    if rid == "menu:new_quote":
        # Clear any stale pre-devis state, send hint
        def menu_clear(s: _state.ConversationState) -> None:
            for key in ("pending_devis", "pending_transcription", "pre_extra",
                       "pre_transcription_items", "pre_prix_items", "pre_prix_idx",
                       "pre_prix_values", "pre_step"):
                s.data.pop(key, None)
            _set_step(s, None, None)
        deps.store.update(user_id, _state.CHANNEL_WHATSAPP, menu_clear)
        deps.send_text(phone, "🎙️ Envoyez un *vocal* pour générer un devis.")
        return
    if rid == "menu:list_quotes":
        _list_recent_devis(user_id, phone, deps)
        return
    if rid == "menu:new_invoice":
        _start_facture_picker(user_id, phone, deps)
        return
    if rid == "menu:profile":
        _show_profile_summary(user_id, phone, deps)
        return
    if rid == "menu:help":
        deps.send_text(phone, HELP_TEXT)
        return

    # Phase 3: Recap flow
    if rid == "recap:confirm":
        _finalize_devis_from_recap(user_id, phone, deps)
        return
    if rid == "recap:edit":
        # Ask for edits, mark state so next text/voice goes to edit flow on pending_devis
        def recap_edit_marker(s: _state.ConversationState) -> None:
            s.data["editing_recap"] = True
            _set_step(s, "recap", "editing")
        deps.store.update(user_id, _state.CHANNEL_WHATSAPP, recap_edit_marker)
        deps.send_text(
            phone,
            "✏️ *Dites-moi quoi modifier* (vocal ou texte)\n"
            "_Exemples : « Je ne suis pas assujetti à la TVA », « Ajoute une vis de renfort »._",
        )
        return
    if rid == "recap:cancel":
        def recap_cancel(s: _state.ConversationState) -> None:
            s.data.pop("pending_devis", None)
            s.data.pop("pre_step", None)
            s.data.pop("editing_recap", None)
            _set_step(s, None, None)
        deps.store.update(user_id, _state.CHANNEL_WHATSAPP, recap_cancel)
        deps.send_text(phone, "❌ Devis annulé. Envoyez un nouveau vocal quand vous voulez.")
        return

    if rid == BTN_YES_CLIENT:
        def mutator(s: _state.ConversationState) -> None:
            _set_step(s, "pre_devis", "client")
            s.data["pre_step"] = "client"  # the routing in handle_inbound reads data["pre_step"]

        deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)
        deps.send_text(phone, MSG_CLIENT_ASK)
        return
    if rid == BTN_NO_CLIENT:
        def mutator(s: _state.ConversationState) -> None:
            s.data.setdefault("pre_extra", {})["client"] = None

        deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)
        _launch_prix_step(user_id, phone, deps)
        return

    if rid == BTN_AI_DECIDES:
        # Record None for this index, advance
        state = deps.store.get(user_id)
        if state is None:
            return
        idx = int(state.data.get("pre_prix_idx", 0))
        values = dict(state.data.get("pre_prix_values") or {})
        values[str(idx)] = None

        def mutator(s: _state.ConversationState) -> None:
            s.data["pre_prix_values"] = values
            s.data["pre_prix_idx"] = idx + 1

        deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)
        _ask_next_prix_question(user_id, phone, deps)
        return
    if rid == BTN_AI_DECIDES_ALL:
        def mutator(s: _state.ConversationState) -> None:
            s.data.pop("pre_prix_items", None)
            s.data.pop("pre_prix_idx", None)
            s.data.pop("pre_prix_values", None)
            s.data.pop("pre_step", None)

        deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)
        _do_generate_devis(user_id, phone, deps)
        return

    if rid == BTN_EDIT:
        # We need the last devis numero to bind. Peek at state (stored when
        # we last emitted devis buttons below) or resort to the most recent.
        numero = _resolve_last_numero(user_id, deps)
        if not numero:
            deps.send_text(phone, "❌ Aucun devis récent trouvé.")
            return

        def mutator(s: _state.ConversationState) -> None:
            s.data["editing_devis"] = numero
            _set_step(s, "edit", "describe")

        deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)
        deps.send_text(
            phone,
            "✏️ *Décrivez les modifications à apporter.*\n"
            "_Exemples : « Je ne suis pas assujetti à la TVA », « Mets la MO à 2 jours »._",
        )
        return

    if rid == BTN_SEND:
        numero = _resolve_last_numero(user_id, deps)
        if not numero:
            deps.send_text(phone, "❌ Aucun devis récent trouvé.")
            return
        _start_send_devis(user_id, phone, numero, deps)
        return

    if rid == BTN_INVOICE:
        numero = _resolve_last_numero(user_id, deps)
        if not numero:
            deps.send_text(phone, "❌ Aucun devis récent trouvé.")
            return
        _offer_facture_types(user_id, phone, numero, deps)
        return

    if rid == BTN_FACT_ACOMPTE or rid == BTN_FACT_SOLDE:
        _generate_facture(user_id, phone, rid, deps)
        return
    if rid == BTN_FACT_INTER:
        _ask_inter_amount(user_id, phone, deps)
        return

    # Unrecognised — ignore silently, log
    logger.info("[flows] unknown interactive id: %r", rid)


def _resolve_last_numero(user_id: int, deps: FlowDeps) -> Optional[str]:
    if not deps.list_devis_fn:
        return None
    items = deps.list_devis_fn(user_id)
    if not items:
        return None
    # list_devis returns list sorted by created_at DESC
    first = items[0]
    return first.get("numero") if isinstance(first, dict) else None


# ─────────────────────────────────────────────────────────────────────────────
# Edit
# ─────────────────────────────────────────────────────────────────────────────


def _apply_recap_edit(user_id: int, phone: str, inbound: InboundMessage, deps: FlowDeps) -> None:
    """Apply edits to the pending (pre-PDF) devis in the recap flow.

    This mirrors _apply_edit but operates on pending_devis (not a saved numero).
    After edits succeed, re-emit the recap for confirmation.
    """
    from client_store import get_client

    state = deps.store.get(user_id)
    if state is None or not state.data.get("editing_recap"):
        deps.send_text(phone, "❌ Aucune modification en cours.")
        return

    # Extract transcription from voice or text
    text = ""
    if inbound.msg_type == "audio":
        media_id = inbound.payload.get("id")
        if not media_id:
            deps.send_text(phone, "❌ Vocal illisible. Réessayez.")
            return
        try:
            with deps.download_media(media_id, suffix=".ogg") as audio_path:
                text = deps.transcribe(audio_path)
        except Exception as exc:
            logger.exception("[flows] recap edit voice transcribe failed")
            deps.send_text(phone, f"❌ Erreur transcription : {type(exc).__name__}")
            return
    else:
        text = inbound.payload.get("body", "")

    if not text:
        deps.send_text(phone, "❌ Message vide. Réessayez.")
        return

    # Load pending devis from state
    devis = state.data.get("pending_devis")
    if not devis:
        deps.send_text(phone, "❌ Devis en attente introuvable.")
        return

    profile = get_client(user_id) or {}
    deps.send_text(phone, "✏️ *Application des modifications…*")
    try:
        new_devis = _edit.edit_quote(devis, text, profile, deps.llm_client, deps.model)
    except Exception as exc:
        logger.exception("[flows] recap edit_quote failed")
        deps.send_text(phone, f"❌ Erreur modification : {type(exc).__name__}")
        return

    # Update the pending devis and re-emit recap
    def recap_edit_mutator(s: _state.ConversationState) -> None:
        s.data["pending_devis"] = new_devis
        s.data.pop("editing_recap", None)
        _set_step(s, "recap", "confirm")

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, recap_edit_mutator)

    recap_msg = _build_recap_message(new_devis)
    deps.send_text(phone, recap_msg)
    deps.send_buttons(
        phone,
        "Tout est bon ?",
        [
            {"id": "recap:confirm", "title": "✅ Générer le PDF"},
            {"id": "recap:edit", "title": "✏️ Modifier"},
            {"id": "recap:cancel", "title": "❌ Recommencer"},
        ],
    )


def _apply_edit(user_id: int, phone: str, inbound: InboundMessage, deps: FlowDeps) -> None:
    from client_store import get_client

    state = deps.store.get(user_id)
    if state is None or not state.data.get("editing_devis"):
        deps.send_text(phone, "❌ Aucune modification en cours.")
        return

    numero = state.data["editing_devis"]
    text = inbound.payload.get("body", "")

    if not deps.load_devis_fn:
        deps.send_text(phone, "❌ Stockage devis indisponible.")
        return

    devis = deps.load_devis_fn(user_id, numero)
    if not devis:
        deps.send_text(phone, "❌ Devis introuvable.")
        return

    profile = get_client(user_id) or {}
    deps.send_text(phone, "✏️ *Application des modifications…*")
    try:
        new_devis = _edit.edit_quote(devis, text, profile, deps.llm_client, deps.model)
    except Exception as exc:
        logger.exception("[flows] edit_quote failed")
        deps.send_text(phone, f"❌ Erreur modification : {type(exc).__name__}")
        return

    try:
        pdf_path = _save_pdf_devis(new_devis, user_id, deps)
        if deps.save_devis_fn:
            deps.save_devis_fn(user_id, new_devis)
    except Exception as exc:
        logger.exception("[flows] edit pdf/save failed")
        deps.send_text(phone, f"❌ Erreur PDF : {type(exc).__name__}")
        return

    try:
        media_id = deps.upload_media(pdf_path, mime_type="application/pdf")
        caption = _build_recap_caption(new_devis).replace("généré", "modifié")
        deps.send_document(phone, media_id, filename=os.path.basename(pdf_path), caption=caption)
        deps.send_buttons(phone, f"Que souhaitez-vous faire avec `{numero}` ?", _devis_buttons())
    except Exception:
        logger.exception("[flows] send edited pdf failed")

    def mutator(s: _state.ConversationState) -> None:
        s.data.pop("editing_devis", None)
        _set_step(s, None, None)

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)


# ─────────────────────────────────────────────────────────────────────────────
# Factures
# ─────────────────────────────────────────────────────────────────────────────


def _offer_facture_types(user_id: int, phone: str, numero: str, deps: FlowDeps) -> None:
    if not deps.load_devis_fn or not deps.get_total_deja_facture_fn:
        deps.send_text(phone, "❌ Factures indisponibles.")
        return
    devis = deps.load_devis_fn(user_id, numero)
    if not devis:
        deps.send_text(phone, "❌ Devis introuvable.")
        return

    total_ttc = devis.get("totaux", {}).get("total_ttc", 0)
    deja = deps.get_total_deja_facture_fn(user_id, numero)
    reste = round(total_ttc - deja, 2)
    pct = devis.get("conditions", {}).get("acompte_pourcentage", 30)
    acompte_ttc = round(total_ttc * pct / 100, 2)

    # Remember which devis we're invoicing, so the next button tap knows.
    def mutator(s: _state.ConversationState) -> None:
        s.data["facture_target_numero"] = numero
        _set_step(s, "facture", "pick_type")

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)

    body = f"🧾 *Quel type de facture pour* `{numero}` ?"
    if deja > 0:
        body += f"\n_Déjà facturé : {deja:,.2f} € — Reste : {reste:,.2f} €_"
    body = body.replace(",", " ")

    deps.send_list(
        phone,
        body,
        button_label="Choisir",
        sections=[
            {
                "title": "Types de facture",
                "rows": [
                    {
                        "id": BTN_FACT_ACOMPTE,
                        "title": f"Acompte ({pct}%)",
                        "description": f"{acompte_ttc:,.2f} €".replace(",", " "),
                    },
                    {
                        "id": BTN_FACT_INTER,
                        "title": "Situation intermédiaire",
                        "description": "Vous saisissez le montant",
                    },
                    {
                        "id": BTN_FACT_SOLDE,
                        "title": "Solde final",
                        "description": f"{reste:,.2f} €".replace(",", " "),
                    },
                ],
            }
        ],
    )


def _ask_inter_amount(user_id: int, phone: str, deps: FlowDeps) -> None:
    state = deps.store.get(user_id)
    numero = state.data.get("facture_target_numero") if state else None
    if not numero:
        deps.send_text(phone, "❌ Aucun devis cible. Tapez *menu*.")
        return

    def mutator(s: _state.ConversationState) -> None:
        s.data["waiting_facture_inter"] = {"numero_devis": numero}
        _set_step(s, "facture", "inter_amount")

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)
    deps.send_text(
        phone,
        "📋 *Situation intermédiaire*\n\n"
        "Quel montant (€ TTC) facturer pour cette étape ?\n"
        "_Ex : 2500 ou 50% — le % s'applique au total TTC._",
    )


def _handle_inter_amount(user_id: int, phone: str, text: str, deps: FlowDeps) -> None:
    state = deps.store.get(user_id)
    if state is None:
        return
    pending = state.data.get("waiting_facture_inter", {})
    numero = pending.get("numero_devis") if pending else None
    if not numero or not deps.load_devis_fn:
        deps.send_text(phone, "❌ Pas de facture en attente.")
        return
    devis = deps.load_devis_fn(user_id, numero)
    if not devis:
        deps.send_text(phone, "❌ Devis introuvable.")
        return
    total_ttc = devis.get("totaux", {}).get("total_ttc", 0)
    try:
        clean = text.strip().replace(" ", "").replace(",", ".").replace("€", "")
        if clean.endswith("%"):
            pct = float(clean[:-1])
            montant_ttc = round(total_ttc * pct / 100, 2)
        else:
            montant_ttc = float(clean)
    except ValueError:
        deps.send_text(phone, "⚠️ Montant non reconnu. Entrez *2500* ou *50%*.")
        return

    deja = (deps.get_total_deja_facture_fn or (lambda *_a: 0))(user_id, numero)
    reste = round(total_ttc - deja, 2)
    if montant_ttc > reste + 0.01:
        deps.send_text(
            phone,
            f"⚠️ Montant > reste à facturer ({reste:,.2f} €).".replace(",", " "),
        )
        return

    def mutator(s: _state.ConversationState) -> None:
        s.data.pop("waiting_facture_inter", None)

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)
    _really_generate_facture(user_id, phone, numero, "intermediaire", montant_ttc, deps)


def _generate_facture(user_id: int, phone: str, button_id: str, deps: FlowDeps) -> None:
    state = deps.store.get(user_id)
    numero = state.data.get("facture_target_numero") if state else None
    if not numero or not deps.load_devis_fn:
        deps.send_text(phone, "❌ Pas de devis cible.")
        return
    devis = deps.load_devis_fn(user_id, numero)
    if not devis:
        deps.send_text(phone, "❌ Devis introuvable.")
        return
    total_ttc = devis.get("totaux", {}).get("total_ttc", 0)
    deja = (deps.get_total_deja_facture_fn or (lambda *_a: 0))(user_id, numero)
    if button_id == BTN_FACT_ACOMPTE:
        pct = devis.get("conditions", {}).get("acompte_pourcentage", 30)
        montant_ttc = round(total_ttc * pct / 100, 2)
        type_fact = "acompte"
    else:  # solde
        montant_ttc = round(total_ttc - deja, 2)
        if montant_ttc <= 0:
            deps.send_text(phone, "⚠️ Devis entièrement facturé — rien à solder.")
            return
        type_fact = "solde"

    _really_generate_facture(user_id, phone, numero, type_fact, montant_ttc, deps)


def _really_generate_facture(
    user_id: int, phone: str, numero: str, type_facture: str,
    montant_ttc: float, deps: FlowDeps,
) -> None:
    from client_store import get_client

    if not (deps.load_devis_fn and deps.devis_to_facture_typed_fn and deps.generate_facture_pdf):
        deps.send_text(phone, "❌ Factures non câblées.")
        return
    devis = deps.load_devis_fn(user_id, numero)
    profile = get_client(user_id) or {}
    factures_prec = (deps.get_factures_for_devis_fn or (lambda *_a: []))(user_id, numero)
    deja = (deps.get_total_deja_facture_fn or (lambda *_a: 0))(user_id, numero)

    facture = deps.devis_to_facture_typed_fn(
        devis, type_facture, montant_ttc, deja, factures_prec
    )
    if profile.get("iban"):
        facture.setdefault("artisan", {})["iban"] = profile["iban"]

    num_fac = facture["meta"]["numero_facture"]
    filename = num_fac.replace("/", "-") + ".pdf"
    fac_path = os.path.join(deps.pdf_output_dir, filename)
    deps.generate_facture_pdf(facture, fac_path)
    if deps.save_facture_fn:
        deps.save_facture_fn(user_id, facture)

    caption = (
        f"🧾 *{facture.get('facturation', {}).get('titre_section', 'Facture')}* — `{num_fac}`\n"
        f"💶 *Ce montant* : {montant_ttc:,.2f} €"
    ).replace(",", " ")
    try:
        media_id = deps.upload_media(fac_path, mime_type="application/pdf")
        deps.send_document(phone, media_id, filename=filename, caption=caption)
    except Exception:
        logger.exception("[flows] facture upload/send failed")
        deps.send_text(phone, "✅ Facture générée mais envoi impossible.")


# ─────────────────────────────────────────────────────────────────────────────
# Email send
# ─────────────────────────────────────────────────────────────────────────────


_EMAIL_RE = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _start_send_devis(user_id: int, phone: str, numero: str, deps: FlowDeps) -> None:
    from client_store import get_client

    if not deps.load_devis_fn:
        deps.send_text(phone, "❌ Stockage devis indisponible.")
        return
    devis = deps.load_devis_fn(user_id, numero)
    if not devis:
        deps.send_text(phone, "❌ Devis introuvable.")
        return
    profile = get_client(user_id) or {}
    client_email = (devis.get("client") or {}).get("email", "")
    if client_email and _EMAIL_RE.match(client_email):
        _really_send_devis_email(user_id, phone, numero, client_email, deps)
        return

    def mutator(s: _state.ConversationState) -> None:
        s.data["waiting_for_email"] = "devis"
        s.data["pending_send"] = {"type": "devis", "numero": numero}
        _set_step(s, "email", "awaiting_email")

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)
    deps.send_text(phone, "📧 *Email du client ?*\n_(répondez avec l'adresse email)_")


def _handle_email_input(user_id: int, phone: str, text: str, deps: FlowDeps) -> None:
    state = deps.store.get(user_id)
    if state is None:
        return
    pending = state.data.get("pending_send") or {}
    if not _EMAIL_RE.match(text.strip()):
        deps.send_text(phone, "⚠️ Email invalide. Réessayez, ou tapez *annuler*.")
        return
    numero = pending.get("numero", "")

    def mutator(s: _state.ConversationState) -> None:
        s.data.pop("waiting_for_email", None)
        s.data.pop("pending_send", None)

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)
    if pending.get("type") == "devis":
        _really_send_devis_email(user_id, phone, numero, text.strip(), deps)
    # facture email parity is identical; omitted here for brevity, reuses same call
    else:
        _really_send_devis_email(user_id, phone, numero, text.strip(), deps)


def _really_send_devis_email(
    user_id: int, phone: str, numero: str, to_email: str, deps: FlowDeps,
) -> None:
    from client_store import get_client

    if not (deps.load_devis_fn and deps.send_devis_email_fn and deps.generate_devis_pdf):
        deps.send_text(phone, "❌ Email non câblé.")
        return
    devis = deps.load_devis_fn(user_id, numero)
    if not devis:
        deps.send_text(phone, "❌ Devis introuvable.")
        return
    profile = get_client(user_id) or {}
    pdf_path = os.path.join(deps.pdf_output_dir, f"{numero}.pdf")
    if not os.path.exists(pdf_path):
        deps.generate_devis_pdf(devis, pdf_path)
    try:
        ok = deps.send_devis_email_fn(
            profile.get("gmail_address", ""),
            profile.get("gmail_app_password", ""),
            to_email,
            pdf_path,
            devis,
        )
    except Exception as exc:
        logger.exception("[flows] send_devis_email failed")
        deps.send_text(phone, f"❌ Erreur envoi : {type(exc).__name__}")
        return
    if ok:
        deps.send_text(phone, f"✅ Devis envoyé à {to_email} !")
    else:
        deps.send_text(phone, "❌ Envoi refusé par le serveur email.")


# ─────────────────────────────────────────────────────────────────────────────
# Profile / devis list / menu
# ─────────────────────────────────────────────────────────────────────────────


def _show_profile_summary(user_id: int, phone: str, deps: FlowDeps) -> None:
    from client_store import get_client

    profile = get_client(user_id) or {}
    summary = _profile.summarize_profile(profile)
    deps.send_text(
        phone,
        "👤 *Votre profil*\n\n" + summary + "\n\n_Pour modifier un champ, tapez *recommencer* puis suivez l'onboarding._",
    )


def _list_recent_devis(user_id: int, phone: str, deps: FlowDeps) -> None:
    if not deps.list_devis_fn:
        deps.send_text(phone, "❌ Stockage devis indisponible.")
        return
    items = deps.list_devis_fn(user_id)
    if not items:
        deps.send_text(phone, "Aucun devis pour le moment.")
        return
    lines = ["📋 *Vos derniers devis :*"]
    for it in items[:5]:
        num = it.get("numero", "—")
        total = it.get("total_ttc", 0)
        date = it.get("created_at", "")
        try:
            date_fr = date.strftime("%d/%m/%Y") if hasattr(date, "strftime") else str(date)[:10]
        except Exception:
            date_fr = str(date)[:10]
        lines.append(f"• `{num}` — {total:,.2f} € — {date_fr}".replace(",", " "))
    deps.send_text(phone, "\n".join(lines))


def _show_main_menu(phone: str, deps: FlowDeps) -> None:
    deps.send_list(
        phone,
        "Menu principal",
        button_label="Voir les options",
        sections=[
            {
                "title": "Actions",
                "rows": [
                    {"id": "menu:new_quote", "title": "🆕 Nouveau devis", "description": "Envoyez un vocal ensuite"},
                    {"id": "menu:list_quotes", "title": "📋 Mes devis", "description": "5 derniers"},
                    {"id": "menu:new_invoice", "title": "🧾 Nouvelle facture", "description": "À partir d'un devis"},
                    {"id": "menu:profile", "title": "👤 Mon profil", "description": "Voir les infos"},
                    {"id": "menu:help", "title": "❓ Aide", "description": "Liste des commandes"},
                ],
            }
        ],
    )


def _start_facture_picker(user_id: int, phone: str, deps: FlowDeps) -> None:
    """Let the artisan pick which devis to invoice."""
    if not deps.list_devis_fn:
        deps.send_text(phone, "❌ Stockage devis indisponible.")
        return
    items = deps.list_devis_fn(user_id)
    if not items:
        deps.send_text(phone, "Aucun devis à facturer.")
        return
    # Parity with Telegram: operate on the last devis. (A richer list picker
    # is Phase 3.)
    numero = items[0].get("numero") if isinstance(items[0], dict) else None
    if not numero:
        deps.send_text(phone, "❌ Impossible de déterminer le devis.")
        return
    _offer_facture_types(user_id, phone, numero, deps)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — Welcome voice, Recap, Debounce, Export
# ─────────────────────────────────────────────────────────────────────────────


def _send_welcome_if_possible(phone: str, deps: FlowDeps) -> None:
    """Send a welcome voice note after onboarding completion.

    If V3/whatsapp/media/welcome.ogg exists, upload and send it as audio.
    Otherwise, fall back to sending HELP_TEXT as a text message and log a warning.

    This should only be called once per artisan (state tracks welcome_sent).
    """
    welcome_path = Path(__file__).resolve().parent / "media" / "welcome.ogg"
    if welcome_path.exists():
        try:
            media_id = deps.upload_media(str(welcome_path), mime_type="audio/ogg")
            deps.send_audio(phone, media_id)
            logger.info("[flows] welcome voice note sent to %s", phone)
        except Exception as exc:
            logger.exception("[flows] welcome voice upload failed, falling back to text")
            deps.send_text(phone, HELP_TEXT)
    else:
        logger.warning(
            "[flows] welcome.ogg not found at %s, sending HELP_TEXT instead",
            welcome_path,
        )
        deps.send_text(phone, HELP_TEXT)


def _build_recap_message(devis: dict) -> str:
    """Build a conversational recap message summarizing the devis.

    Format (FR):
      ✅ *Voilà ce que j'ai compris :*
      1. Item 1 — price HT
      2. Item 2 — price HT
      ...
      *Total TTC estimé :* X €
      *Chantier :* reference
      *Client :* name (if present)

      Tout est bon ?
    """
    lignes = devis.get("lignes", [])
    meta = devis.get("meta", {})
    totaux = devis.get("totaux", {})
    client = devis.get("client", {})

    lines = ["✅ *Voilà ce que j'ai compris :*"]
    for i, ligne in enumerate(lignes, 1):
        desc = ligne.get("description", f"Poste {i}")
        prix_ht = ligne.get("prix_unitaire_ht", 0)
        lines.append(f"{i}. {desc} — {prix_ht:,.2f} € HT".replace(",", " "))

    total_ttc = totaux.get("total_ttc", 0)
    lines.append("")
    lines.append(f"*Total TTC estimé :* {total_ttc:,.2f} €".replace(",", " "))

    chantier = meta.get("reference_chantier") or "—"
    lines.append(f"*Chantier :* {chantier}")

    client_name = client.get("nom") or client.get("name") or ""
    if client_name:
        lines.append(f"*Client :* {client_name}")

    lines.append("")
    lines.append("Tout est bon ?")

    return "\n".join(lines)


def _finalize_devis_from_recap(user_id: int, phone: str, deps: FlowDeps) -> None:
    """Load pending_devis from state and complete the PDF/send flow.

    Called when user taps 'recap:confirm'. This re-uses the existing
    PDF save + upload + buttons flow.
    """
    state = deps.store.get(user_id)
    if state is None:
        deps.send_text(phone, MSG_MENU_HINT)
        return

    devis = state.data.get("pending_devis")
    if not devis:
        deps.send_text(phone, "❌ Devis en attente introuvable.")
        return

    # Re-run the PDF save + upload + buttons flow (same as _do_generate_devis endpoint)
    try:
        pdf_path = _save_pdf_devis(devis, user_id, deps)
        if deps.save_devis_fn:
            deps.save_devis_fn(user_id, devis)
    except Exception as exc:
        logger.exception("[flows] finalize devis pdf/save failed")
        deps.send_text(phone, f"❌ Erreur PDF : {type(exc).__name__}")
        return

    numero = devis.get("meta", {}).get("numero_devis", "")
    caption = _build_recap_caption(devis)
    try:
        media_id = deps.upload_media(pdf_path, mime_type="application/pdf")
        deps.send_document(phone, media_id, filename=os.path.basename(pdf_path), caption=caption)
        deps.send_buttons(phone, f"Que souhaitez-vous faire avec le devis `{numero}` ?", _devis_buttons())
    except Exception:
        logger.exception("[flows] finalize send document failed")
        deps.send_text(phone, "✅ Devis généré. Je n'ai pas pu l'envoyer — réessayez plus tard.")

    # Clear recap state
    def mutator(s: _state.ConversationState) -> None:
        s.data.pop("pending_devis", None)
        s.data.pop("pre_step", None)
        _set_step(s, None, None)

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)


def _maybe_flush_debounce(user_id: int, phone: str, deps: FlowDeps, *, force: bool = False) -> bool:
    """Check if there are pending debounced voice messages ready to flush.

    Called at the top of handle_inbound to allow background processing.
    With debounce_window_s=0, this does nothing (instant process).

    Args:
        force: if True, flush immediately regardless of window age (user typed "ok").

    Returns:
        True if voices were flushed (caller should skip further processing).
    """
    if deps.debounce_window_s == 0 or not deps.fetch_pending_fn:
        return False

    try:
        # Fetch all transcribed voices for this user (use a large window to catch all)
        window = deps.debounce_window_s * 60 if force else deps.debounce_window_s
        pending = deps.fetch_pending_fn(user_id, window)
        if not pending:
            return False

        if not force:
            # Only flush if the oldest is > window seconds old
            if len(pending) == 1:
                oldest = pending[0]
                received = oldest.get("received_at")
                now = datetime.now(timezone.utc) if hasattr(datetime, "now") else datetime.utcnow()
                # Ensure both are tz-aware or naive for comparison
                if hasattr(received, "tzinfo") and received.tzinfo is None:
                    if hasattr(now, "tzinfo") and now.tzinfo is not None:
                        now = now.replace(tzinfo=None)
                elif not hasattr(received, "tzinfo") and hasattr(now, "tzinfo") and now.tzinfo is not None:
                    now = now.replace(tzinfo=None)
                age_s = (now - received).total_seconds()
                if age_s < deps.debounce_window_s:
                    return False  # Still within window, wait

        # Ready to flush: concatenate transcriptions in order
        transcriptions = [p.get("transcription", "") for p in pending]
        concatenated = " ".join(t for t in transcriptions if t)
        wamids = [p.get("wamid") for p in pending]

        # Mark as processed
        if deps.mark_processed_fn:
            deps.mark_processed_fn(wamids)

        # Process the concatenated transcription as if it came from a single voice memo
        _process_concatenated_voice(user_id, phone, concatenated, deps)
        return True
    except Exception:
        logger.exception("[flows] debounce flush failed")
        return False


def _handle_export(user_id: int, phone: str, deps: FlowDeps) -> None:
    """Create a zip archive of all devis + factures for the current year and send it.

    Uses the devis numero prefix (DEVIS-YYYYMMDD-XXX) to filter by year.
    If archive > 90 MB, send a text message instead.
    """

    if not deps.list_devis_fn:
        deps.send_text(phone, "❌ Stockage devis indisponible.")
        return

    items = deps.list_devis_fn(user_id)
    if not items:
        deps.send_text(phone, "Aucun document à archiver.")
        return

    current_year = datetime.now().year
    devis_count = 0
    facture_count = 0
    archive_size = 0

    # Create temp directory for zip
    tmpdir = Path(tempfile.mkdtemp(prefix=f"souffl_export_{user_id}_"))
    devis_dir = tmpdir / "devis"
    factures_dir = tmpdir / "factures"
    devis_dir.mkdir(parents=True, exist_ok=True)
    factures_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Enumerate devis, filter by year
        for item in items:
            numero = item.get("numero", "")
            # Parse year from numero (e.g., "DEVIS-20250115-001" → 2025)
            try:
                if "DEVIS-" in numero:
                    year_str = numero.split("-")[1][:4]
                    year = int(year_str)
                else:
                    continue
            except (IndexError, ValueError):
                continue

            if year != current_year:
                continue

            # Load devis JSON
            if deps.load_devis_fn:
                devis = deps.load_devis_fn(user_id, numero)
                if devis:
                    # Save devis JSON
                    devis_json_path = devis_dir / f"{numero}.json"
                    with open(devis_json_path, "w", encoding="utf-8") as f:
                        json.dump(devis, f, ensure_ascii=False, indent=2)
                    devis_count += 1

                    # Copy devis PDF if it exists
                    pdf_path = Path(deps.pdf_output_dir) / f"{numero}.pdf"
                    if pdf_path.exists():
                        shutil.copy(pdf_path, devis_dir / f"{numero}.pdf")

            # Enumerate and copy factures for this devis
            if deps.get_factures_for_devis_fn:
                factures = deps.get_factures_for_devis_fn(user_id, numero) or []
                for facture in factures:
                    num_fact = facture.get("meta", {}).get("numero_facture", "")
                    if not num_fact:
                        continue

                    # Save facture JSON
                    facture_json_path = factures_dir / f"{num_fact}.json"
                    with open(facture_json_path, "w", encoding="utf-8") as f:
                        json.dump(facture, f, ensure_ascii=False, indent=2)
                    facture_count += 1

                    # Copy facture PDF if it exists
                    pdf_filename = num_fact.replace("/", "-") + ".pdf"
                    pdf_path = Path(deps.pdf_output_dir) / pdf_filename
                    if pdf_path.exists():
                        shutil.copy(pdf_path, factures_dir / pdf_filename)

        # Create README
        readme_content = (
            f"Souffl.AI — Archive {current_year}\n"
            f"{'=' * 50}\n\n"
            f"Générée : {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
            f"Devis : {devis_count}\n"
            f"Factures : {facture_count}\n\n"
            f"Structure :\n"
            f"- devis/ : PDFs et JSON des devis\n"
            f"- factures/ : PDFs et JSON des factures\n"
        )
        readme_path = tmpdir / "README.txt"
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(readme_content)

        # Send individual PDFs (WhatsApp doesn't support ZIP uploads)
        pdf_files = list(tmpdir.rglob("*.pdf"))
        if not pdf_files:
            deps.send_text(
                phone,
                f"📦 Archive {current_year} : {devis_count} devis, {facture_count} factures.\n"
                f"Aucun PDF trouvé — les JSONs sont disponibles mais WhatsApp ne supporte "
                f"pas l'envoi de fichiers ZIP.",
            )
        elif len(pdf_files) > 10:
            # Too many PDFs — send a summary and the most recent ones
            deps.send_text(
                phone,
                f"📦 Archive {current_year} : {devis_count} devis, {facture_count} factures.\n"
                f"⚠️ {len(pdf_files)} PDFs trouvés — envoi des 10 plus récents.",
            )
            pdf_files = sorted(pdf_files, key=lambda p: p.stat().st_mtime, reverse=True)[:10]
            for pdf_file in pdf_files:
                try:
                    mid = deps.upload_media(str(pdf_file), mime_type="application/pdf")
                    deps.send_document(phone, mid, filename=pdf_file.name, caption="")
                except Exception:
                    logger.exception("[flows] export PDF send failed: %s", pdf_file.name)
        else:
            deps.send_text(
                phone,
                f"📦 Archive {current_year} : {devis_count} devis, {facture_count} factures.\n"
                f"Envoi de {len(pdf_files)} PDF(s)…",
            )
            for pdf_file in pdf_files:
                try:
                    mid = deps.upload_media(str(pdf_file), mime_type="application/pdf")
                    deps.send_document(phone, mid, filename=pdf_file.name, caption="")
                except Exception:
                    logger.exception("[flows] export PDF send failed: %s", pdf_file.name)

        logger.info(
            "[flows] export sent for user %d: %d devis, %d factures, %d PDFs",
            user_id, devis_count, facture_count, len(pdf_files),
        )

    except Exception as exc:
        logger.exception("[flows] export failed")
        deps.send_text(phone, f"❌ Erreur export : {type(exc).__name__}")
    finally:
        # Clean up temp files
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            logger.exception("[flows] cleanup after export failed")


def _process_concatenated_voice(user_id: int, phone: str, transcription: str, deps: FlowDeps) -> None:
    """Process a concatenated voice transcription through the quote flow.

    This is the internal continuation after debounce flushing.
    Mirrors the end of _start_voice_quote once transcription is ready.
    """
    extracted = _quote.quick_extract(transcription, deps.llm_client, deps.model)

    def mutator(s: _state.ConversationState) -> None:
        s.data["pending_transcription"] = transcription
        s.data["pre_extra"] = {"client": extracted.get("client")}
        s.data["pre_transcription_items"] = extracted.get("lignes", [])
        _set_step(s, "pre_devis", "client")

    deps.store.update(user_id, _state.CHANNEL_WHATSAPP, mutator)

    deps.send_text(phone, f"✅ Transcription OK ({len(transcription)} car.)")

    if extracted.get("client"):
        _launch_prix_step(user_id, phone, deps)
    else:
        deps.send_buttons(
            phone,
            MSG_CLIENT_QUESTION,
            [
                {"id": BTN_YES_CLIENT, "title": "👤 Oui, ajouter"},
                {"id": BTN_NO_CLIENT, "title": "⏭️ Non, générer"},
            ],
        )


__all__ = [
    "FlowDeps",
    "handle_inbound",
]
