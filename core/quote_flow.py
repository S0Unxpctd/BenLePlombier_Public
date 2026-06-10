"""Souffl.AI V3 — Quote generation flow (transport-agnostic).

Extracted from bot.py. This module owns the pipeline:

    transcription + profile + overrides
        → LLM call (system prompt + user message)
        → parse JSON
        → deterministic post-processing
              (tva-zero detection, artisan enrichment, empty-line removal, flag filtering)
        → final devis dict with totals recomputed

It does NOT:
- transcribe audio (that's Whisper-specific, transport calls it directly)
- generate PDFs (caller uses pdf_generator)
- send messages (the adapter does)
- know anything about Telegram or WhatsApp

All external dependencies (LLM client, profile store) are injectable for tests.

Public API
----------
- parse_llm_json(raw)
- build_user_message(transcription, extra, now=None)
- detect_tva_zero(text)
- apply_tva_zero(devis)
- enrich_artisan(devis, profile)
- remove_empty_lines(devis)
- filter_flags(devis, profile=None)
- generate_quote(transcription, profile, extra, llm_client, model, system_prompt)
    → returns a devis dict with totals recomputed and post-processing applied
- quick_extract(transcription, llm_client, model, log=None)
    → returns {'client': str|None, 'lignes': [...]}
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constants mirrored from bot.py
# ─────────────────────────────────────────────────────────────────────────────


_PLACEHOLDER = ("[À COMPLÉTER]", "[À COMPLÉTER — OBLIGATOIRE]", "null", "None", "")

_ARTISAN_PROFIL_MAP = {
    "raison_sociale": "raison_sociale",
    "adresse": "adresse",
    "telephone": "telephone",
    "email": "email",
    "siret": "siret",
    "tva_intracom": "tva_intracom",
    "rcs_rm": "rcs_rm",
    "assurance_decennale": "assurance_decennale",
    "numero_police": "numero_police",
}

_FLAGS_TOUJOURS_SUPPRIMES = [
    "médiateur", "mediateur", "tribunal",
    "montant global", "répartis à zéro", "repartis a zero",
]

_FLAGS_ARTISAN_CHAMP_MAP = {
    "dirigeant": "dirigeant",
    "e-mail": "email",
    "email": "email",
    "courriel": "email",
    "rcs": "rcs_rm",
    "registre du commerce": "rcs_rm",
    "registre des m": "rcs_rm",
    "raison sociale": "raison_sociale",
    "nom de l'entreprise": "raison_sociale",
    "siret": "siret",
    "adresse": "adresse",
    "téléphone": "telephone",
    "telephone": "telephone",
    "tva intracom": "tva_intracom",
    "n° tva": "tva_intracom",
}

_TVA_ZERO_PATTERNS = [
    "non assujetti", "pas assujetti", "sans tva", "pas de tva",
    "tva 0", "0% tva", "0 % tva", "exempté", "exempt",
    "franchise en base", "micro-entreprise", "micro entreprise",
    "autoentrepreneur", "auto-entrepreneur", "auto entrepreneur",
    "art 293", "article 293", "hors tva", "ht seulement",
]


# ─────────────────────────────────────────────────────────────────────────────
# JSON parsing
# ─────────────────────────────────────────────────────────────────────────────


def parse_llm_json(raw: Optional[str]) -> dict:
    """Parse a JSON response from an LLM.

    Handles:
    - empty / None
    - fenced code blocks ``` or ```json
    Raises ValueError if the payload is unusable.
    """
    if not raw:
        raise ValueError("Le modèle a retourné une réponse vide.")
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    if not cleaned:
        raise ValueError("Le modèle a retourné un bloc vide après nettoyage.")
    return json.loads(cleaned)


# ─────────────────────────────────────────────────────────────────────────────
# User-message builder
# ─────────────────────────────────────────────────────────────────────────────


def build_user_message(
    transcription: str,
    extra: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> str:
    """Build the user message for the main LLM call.

    `extra` carries overrides from the pre-devis sequence:
      - client: str | None
      - duree:  str | None
      - marques: str | None
      - prix_postes: list[{description, prix_unitaire_ht, unite}]
    """
    today = now or datetime.now()
    validity = today + timedelta(days=90)
    msg = (
        f"Date d'aujourd'hui : {today.strftime('%d/%m/%Y')}\n"
        f"Date de validité : {validity.strftime('%d/%m/%Y')}\n\n"
        f"Transcription du message vocal :\n---\n{transcription}\n---\n"
    )
    if extra:
        if extra.get("client"):
            msg += f"\nCoordonnées client fournies par l'artisan : {extra['client']}"
        if extra.get("duree"):
            msg += f"\nDurée estimée des travaux précisée : {extra['duree']}"
        if extra.get("marques"):
            msg += f"\nMarques / références équipements précisées : {extra['marques']}"
        if extra.get("prix_postes"):
            msg += (
                "\n\nPRIX IMPOSÉS PAR L'ARTISAN — À UTILISER IMPÉRATIVEMENT, NE PAS MODIFIER :\n"
                "(Ces prix remplacent les tarifs par défaut pour ces postes spécifiques.)\n"
            )
            for p in extra["prix_postes"]:
                msg += (
                    f"  - \"{p['description']}\" → prix unitaire HT : "
                    f"{p['prix_unitaire_ht']} € / {p['unite']}\n"
                )
    msg += "\n\nGénère le devis JSON complet. Réponds uniquement avec le JSON."
    return msg


# ─────────────────────────────────────────────────────────────────────────────
# TVA-zero shortcut
# ─────────────────────────────────────────────────────────────────────────────


def detect_tva_zero(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in _TVA_ZERO_PATTERNS)


def apply_tva_zero(devis: dict) -> dict:
    """Apply TVA=0 to every line, then recompute totals via calculs.py."""
    from calculs import appliquer_taux_tva_uniforme

    return appliquer_taux_tva_uniforme(devis, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Post-processing
# ─────────────────────────────────────────────────────────────────────────────


def enrich_artisan(devis: dict, profile: dict) -> dict:
    """Fill `[À COMPLÉTER]` artisan fields from the stored profile."""
    artisan = devis.setdefault("artisan", {})

    def _fill(artisan_key: str, profile_value: str):
        current = str(artisan.get(artisan_key, "")).strip()
        if current in _PLACEHOLDER:
            if profile_value and str(profile_value).strip() not in _PLACEHOLDER:
                artisan[artisan_key] = profile_value

    for artisan_key, profile_key in _ARTISAN_PROFIL_MAP.items():
        _fill(artisan_key, profile.get(profile_key, ""))

    _fill("dirigeant", profile.get("dirigeant") or profile.get("raison_sociale", ""))
    return devis


def remove_empty_lines(devis: dict) -> dict:
    """Drop lines where prix_unitaire_ht == 0 and recompute totals."""
    from calculs import recalculer_totaux

    def _val(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    devis["lignes"] = [
        l
        for l in devis.get("lignes", [])
        if _val(l.get("prix_unitaire_ht", 0)) != 0.0
    ]
    for i, ligne in enumerate(devis.get("lignes", []), start=1):
        ligne["poste"] = i
    return recalculer_totaux(devis)


def filter_flags(devis: dict, profile: Optional[dict] = None) -> dict:
    """Strip redundant flags from the generated devis."""
    flags = devis.get("flags", [])
    if not flags:
        return devis

    artisan = devis.get("artisan", {})

    def _champ_rempli(artisan_field: str) -> bool:
        val = str(artisan.get(artisan_field, "")).strip()
        return bool(val) and val not in _PLACEHOLDER

    def _est_superflu(flag: str) -> bool:
        f = flag.lower()
        for kw in _FLAGS_TOUJOURS_SUPPRIMES:
            if kw in f:
                return True
        for kw, artisan_field in _FLAGS_ARTISAN_CHAMP_MAP.items():
            if kw in f and _champ_rempli(artisan_field):
                return True
        return False

    devis["flags"] = [f for f in flags if not _est_superflu(f)]
    return devis


# ─────────────────────────────────────────────────────────────────────────────
# Pre-extraction (LLM single-shot)
# ─────────────────────────────────────────────────────────────────────────────


_QUICK_EXTRACT_PROMPT = (
    "Analyse ce message vocal d'un artisan plombier/chauffagiste.\n"
    "Extrais UNIQUEMENT les informations EXPLICITEMENT mentionnées.\n\n"
    "Retourne un JSON avec exactement 2 clés :\n\n"
    '1. "client" : coordonnées ou nom du client si cités (null sinon)\n\n'
    '2. "lignes" : liste des postes/travaux à chiffrer. Chaque élément :\n'
    '   - "description"       : libellé court du poste (5-10 mots max)\n'
    '   - "unite"             : "jour" / "forfait" / "u" / "m²" / "ml"\n'
    '   - "quantite"          : nombre (float)\n'
    '   - "prix_unitaire_ht"  : prix HT si explicitement mentionné, sinon null\n'
    "   Inclure TOUJOURS une ligne main d'œuvre si non mentionnée.\n"
    "   Maximum 8 lignes.\n\n"
    "Message vocal :\n{transcription}"
)


def quick_extract(
    transcription: str,
    llm_client,
    model: str,
) -> dict:
    """Synchronous single-shot extraction of {client, lignes} from a transcription.

    Returns {} on any parse error so callers can keep moving.
    """
    try:
        resp = llm_client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": _QUICK_EXTRACT_PROMPT.format(
                        transcription=transcription[:2500]
                    ),
                }
            ],
            temperature=0,
            max_tokens=500,
        )
        raw = resp.choices[0].message.content
        data = parse_llm_json(raw)
    except (ValueError, json.JSONDecodeError, KeyError, AttributeError, IndexError):
        return {"client": None, "lignes": []}
    except Exception as exc:  # network / LLM failure
        logger.warning("[quick_extract] Failed: %s", exc)
        return {"client": None, "lignes": []}

    client = data.get("client")
    if client and str(client).lower() in ("null", "none", ""):
        client = None

    raw_lines = data.get("lignes", []) or []
    clean: list[dict] = []
    for l in raw_lines:
        if not isinstance(l, dict) or not l.get("description"):
            continue
        item = {
            "description": str(l.get("description", "Poste")),
            "unite": str(l.get("unite", "forfait")),
            "quantite": float(l.get("quantite", 1)),
        }
        if l.get("prix_unitaire_ht") is not None:
            try:
                item["prix_unitaire_ht"] = float(l["prix_unitaire_ht"])
            except (ValueError, TypeError):
                pass
        clean.append(item)
    return {"client": client, "lignes": clean}


# ─────────────────────────────────────────────────────────────────────────────
# Full quote generation
# ─────────────────────────────────────────────────────────────────────────────


def _assign_numero(devis: dict, now: Optional[datetime] = None) -> dict:
    today = now or datetime.now()
    devis.setdefault("meta", {})
    devis["meta"]["numero_devis"] = (
        f"DEVIS-{today.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    )
    return devis


def generate_quote(
    transcription: str,
    profile: dict,
    extra: Optional[dict],
    llm_client,
    model: str,
    system_prompt: str,
    *,
    now: Optional[datetime] = None,
    number_assigner: Callable[[dict], dict] = _assign_numero,
) -> dict:
    """Produce a fully-processed devis dict from a transcription + overrides.

    - `profile`:       the artisan profile dict (as returned by client_store.get_client).
    - `extra`:         the pre-devis overrides dict (client, duree, marques, prix_postes).
    - `llm_client`:    any OpenAI-compatible client (has .chat.completions.create).
    - `model`:         the OpenRouter model id.
    - `system_prompt`: ALREADY profile-injected (caller does
                        `client_store.inject_profile_in_prompt(SYSTEM_PROMPT, profile)`).
    - `now`:           injectable for deterministic tests.
    - `number_assigner`: injectable for deterministic tests.

    Returns either:
      - a processed devis dict with `meta.numero_devis` assigned, or
      - `{'erreur': ..., 'message': ...}` pass-through if the LLM signals an error
        (the caller decides how to surface that to the user).
    """
    response = llm_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_user_message(transcription, extra, now=now)},
        ],
        temperature=0.1,
        max_tokens=8192,
    )
    raw = response.choices[0].message.content
    logger.info("[quote_flow] LLM response preview: %s", str(raw)[:200])
    devis = parse_llm_json(raw)

    # Deterministic TVA-zero override if the transcription signals it.
    if detect_tva_zero(transcription):
        devis = apply_tva_zero(devis)

    if "erreur" in devis:
        return devis

    devis = number_assigner(devis, now=now) if _accepts_now(number_assigner) else number_assigner(devis)

    devis.setdefault("artisan", {})
    if profile.get("iban"):
        devis["artisan"]["iban"] = profile["iban"]

    devis = enrich_artisan(devis, profile)
    devis = remove_empty_lines(devis)
    devis = filter_flags(devis, profile)
    return devis


def _accepts_now(fn: Callable) -> bool:
    """Tiny helper: does this callable accept a `now` kwarg?"""
    try:
        import inspect

        sig = inspect.signature(fn)
        return "now" in sig.parameters
    except (TypeError, ValueError):
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Backwards-compatible helpers (used by tests referencing the bot.py names)
# ─────────────────────────────────────────────────────────────────────────────


# Expose the historical French-suffixed names as aliases so a future test
# suite can choose either convention.
_parse_llm_json = parse_llm_json
_detecter_tva_zero = detect_tva_zero
_appliquer_tva_zero = apply_tva_zero
_enrichir_artisan = enrich_artisan
_supprimer_lignes_vides = remove_empty_lines
_filtrer_flags = filter_flags
