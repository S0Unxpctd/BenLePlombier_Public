"""Souffl.AI V3 — Profile flow (transport-agnostic).

Thin, channel-agnostic wrapper over the existing `client_store`:
- CRUD on artisan profiles
- Field-level update helpers (with basic sanitization)
- Onboarding step sequencing (ordered list of fields + per-field prompts)

Telegram's `onboarding.py` ConversationHandler continues to exist and is not
modified in Phase 1. The WhatsApp adapter (Phase 2) will drive a linear
onboarding by iterating over `ONBOARDING_STEPS` here and calling
`apply_field_update` for each answer.

Public API
----------
- load_profile(user_id)              -> dict | None
- save_profile(user_id, profile)     -> None
- profile_exists(user_id)            -> bool
- new_empty_profile()                -> dict
- apply_field_update(profile, field, raw_value) -> dict
- ONBOARDING_STEPS                   -> tuple of OnboardingStep
- summarize_profile(profile)         -> str (Markdown)
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Optional


# ─────────────────────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────────────────────


def load_profile(user_id: int) -> Optional[dict]:
    from client_store import get_client

    return get_client(user_id)


def save_profile(user_id: int, profile: dict) -> None:
    from client_store import save_client

    save_client(user_id, profile)


def profile_exists(user_id: int) -> bool:
    from client_store import client_exists

    return client_exists(user_id)


def new_empty_profile() -> dict:
    """Delegates to the canonical shape in client_store so both Telegram and
    WhatsApp adapters stay in sync with the existing schema.
    """
    from client_store import empty_profile

    return empty_profile()


# ─────────────────────────────────────────────────────────────────────────────
# Field-level sanitization
# ─────────────────────────────────────────────────────────────────────────────


def _clean_numeric(raw: str) -> Optional[float]:
    """Parse a user-entered numeric amount like '350', '350,00', '350 €' -> float.

    Returns None on parse failure.
    """
    try:
        return float(str(raw).replace(",", ".").replace("€", "").strip())
    except (ValueError, TypeError):
        return None


_CLEANERS: dict[str, Callable[[str], Any]] = {
    "iban": lambda raw: str(raw).replace(" ", ""),
    "gmail_app_password": lambda raw: str(raw).replace(" ", ""),
    "siret": lambda raw: str(raw).replace(" ", ""),
    "journee_standard_ht": _clean_numeric,
    "journee_specialise_ht": _clean_numeric,
    "deplacement_par_jour": _clean_numeric,
}


def apply_field_update(profile: dict, field: str, raw_value: str) -> dict:
    """Return a new profile dict with `field` set to a sanitized value.

    Raises ValueError if the value can't be cleaned (e.g. non-numeric tarif).
    """
    updated = copy.deepcopy(profile)
    cleaner = _CLEANERS.get(field)
    if cleaner is None:
        updated[field] = str(raw_value).strip()
        return updated
    cleaned = cleaner(raw_value)
    if cleaned is None:
        raise ValueError(f"Invalid value for field {field!r}: {raw_value!r}")
    updated[field] = cleaned
    return updated


# ─────────────────────────────────────────────────────────────────────────────
# Onboarding step sequence
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class OnboardingStep:
    field: str
    prompt_fr: str   # Markdown-safe French prompt shown to the artisan
    skippable: bool = True
    help_hint_fr: Optional[str] = None


ONBOARDING_STEPS: tuple[OnboardingStep, ...] = (
    OnboardingStep(
        field="raison_sociale",
        prompt_fr="🏢 *Nom de votre entreprise ?*",
        help_hint_fr="ex : Plomberie Dupont, ou votre prénom + nom si auto-entrepreneur",
    ),
    OnboardingStep(
        field="siret",
        prompt_fr="🪪 *Votre numéro SIRET ?* (14 chiffres)",
        help_hint_fr="Vous le trouvez sur vos anciens devis ou sur papiers.fr",
    ),
    OnboardingStep(
        field="adresse",
        prompt_fr="📍 *Adresse complète de votre entreprise ?*",
        help_hint_fr="numéro, rue, code postal, ville",
    ),
    OnboardingStep(
        field="telephone",
        prompt_fr="📞 *Votre numéro de téléphone ?*",
    ),
    OnboardingStep(
        field="email",
        prompt_fr="📧 *Votre email professionnel ?*",
        help_hint_fr="ce sera aussi l'adresse d'envoi des devis",
    ),
    OnboardingStep(
        field="assurance_decennale",
        prompt_fr="🛡️ *Votre assureur décennale ?*",
        help_hint_fr="obligatoire sur les devis",
    ),
    OnboardingStep(
        field="numero_police",
        prompt_fr="🔢 *Numéro de police d'assurance décennale ?*",
    ),
    OnboardingStep(
        field="iban",
        prompt_fr="🏦 *Votre IBAN ?*",
        help_hint_fr="pour que vos clients sachent où virer le paiement sur les factures",
    ),
    OnboardingStep(
        field="tva_intracom",
        prompt_fr="💶 *Êtes-vous assujetti à la TVA ?*",
        help_hint_fr="entrez votre numéro TVA intracommunautaire, ou 'Non assujetti à la TVA' si micro-entrepreneur",
    ),
    OnboardingStep(
        field="journee_standard_ht",
        prompt_fr="💰 *Votre tarif journée standard en € HT ?*",
        help_hint_fr="350 € par défaut si vide",
    ),
    OnboardingStep(
        field="gmail_address",
        prompt_fr="✉️ *Votre adresse Gmail pour envoyer les devis ?*",
        help_hint_fr="vous pouvez configurer ça plus tard",
    ),
    OnboardingStep(
        field="gmail_app_password",
        prompt_fr="🔑 *Mot de passe d'application Gmail ?*",
        help_hint_fr="16 caractères obtenus sur myaccount.google.com → Sécurité → Mots de passe d'application",
    ),
)


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────


def summarize_profile(profile: dict) -> str:
    """French Markdown one-screen summary of the profile. Used in recaps."""
    lines = [
        f"🏢 *Entreprise* : {profile.get('raison_sociale', '—')}",
        f"🪪 *SIRET* : {profile.get('siret', '—')}",
        f"📍 *Adresse* : {profile.get('adresse', '—')}",
        f"📞 *Tél.* : {profile.get('telephone', '—')}",
        f"📧 *Email* : {profile.get('email', '—')}",
        (
            f"🛡️ *Assureur* : {profile.get('assurance_decennale', '—')} "
            f"— Police : {profile.get('numero_police', '—')}"
        ),
        f"🏦 *IBAN* : {profile.get('iban', '—')}",
        f"💶 *TVA* : {profile.get('tva_intracom', '—')}",
    ]
    if profile.get("journee_standard_ht"):
        lines.append(f"💰 *Tarif journée* : {profile['journee_standard_ht']} €/j")
    return "\n".join(lines)
