"""
SOUFFL.AI V2 — Bot Telegram multi-tenant
=========================================
Flux principal :
  1. Vérif profil artisan (sinon → onboarding)
  2. Vocal → Whisper → transcription
  3. Transcription + profil → Claude → JSON devis
  4. JSON → PDF WeasyPrint
  5. PDF envoyé + boutons : ✏️ Modifier | 📧 Envoyer | 🧾 Facture

Commandes :
  /start     → onboarding si nouveau, sinon bienvenue
  /devis     → liste les derniers devis
  /facture   → convertir un devis en facture
  /profil    → voir/modifier son profil
  /cancel    → annuler l'opération en cours
"""

import os
import re
import json
import logging
import asyncio
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

from system_prompt import SYSTEM_PROMPT
from pdf_generator import generate_pdf
from facture_generator import devis_to_facture, generate_facture_pdf, devis_to_facture_typed
from client_store import get_client, save_client, client_exists, inject_profile_in_prompt
from devis_store import (
    save_devis, load_devis, list_devis, get_last_devis,
    save_facture, load_facture, get_factures_for_devis, get_total_deja_facture,
)
from email_sender import send_devis_email, send_facture_email, REMISE_EN_MAIN_PROPRE_WARNING
from onboarding import build_onboarding_handler

# V3 Phase 0: optional Sentry wiring. No-op if SENTRY_DSN unset or sentry_sdk missing.
try:
    from ops.sentry import init_sentry, capture_exception
except ImportError:  # pragma: no cover — fallback if V3/ops is unavailable
    def init_sentry(*_args, **_kwargs):
        return False
    def capture_exception(_exc):
        return None

# ── Configuration ─────────────────────────────────────────────────────────────

load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
OPENROUTER_KEY   = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-3-5-sonnet")
PDF_OUTPUT_DIR   = os.getenv("PDF_OUTPUT_DIR", str(Path(__file__).parent / "data" / "pdf"))
ADMIN_USER_ID    = int(os.getenv("ADMIN_USER_ID") or "0")

Path(PDF_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

whisper_client = OpenAI(api_key=OPENAI_API_KEY)
llm = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_KEY)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# États pour la conversation "envoyer par email"
WAITING_EMAIL_DEVIS, WAITING_EMAIL_FACTURE = range(10, 12)


# ── Transcription Whisper ─────────────────────────────────────────────────────

async def transcribe_audio(audio_path: str) -> str:
    loop = asyncio.get_running_loop()
    def _call():
        with open(audio_path, "rb") as f:
            return whisper_client.audio.transcriptions.create(
                model="whisper-1", file=f, language="fr"
            ).text
    return (await loop.run_in_executor(None, _call)).strip()


# ── Utilitaire JSON ───────────────────────────────────────────────────────────

def _parse_llm_json(raw: str | None) -> dict:
    """
    Parse la réponse brute d'un LLM en JSON.
    Gère les cas : réponse vide, None, et JSON enveloppé dans des blocs markdown (```json ... ```).
    """
    if not raw:
        raise ValueError("Le modèle a retourné une réponse vide.")
    raw = raw.strip()
    # Supprimer les blocs markdown si présents (```json ... ``` ou ``` ... ```)
    if raw.startswith("```"):
        lines = raw.splitlines()
        # Supprimer la première ligne (```json ou ```)
        lines = lines[1:]
        # Supprimer la dernière ligne si c'est ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    if not raw:
        raise ValueError("Le modèle a retourné un bloc vide après nettoyage.")
    return json.loads(raw)


# ── Génération du JSON devis ──────────────────────────────────────────────────

SKIP_BTN          = "⏭️ Passer"
NON_BTN           = "❌ Non, c'est bon"
AI_DECIDE_BTN     = "🤖 IA décide"
AI_DECIDE_ALL_BTN = "🤖 IA décide pour tout"

# Toutes les variantes textuelles acceptées comme "skip"
_SKIP_WORDS = {"passer", "plus tard", "plustard", "skip", "non", "nope", "rien",
               "pas maintenant", "plus", "later", "n/a", "na", "—", "-"}

def is_skip(text: str) -> bool:
    return text.strip() in (SKIP_BTN, NON_BTN) or text.strip().lower() in _SKIP_WORDS

# États de la séquence pré-devis
PRE_STEP_CLIENT  = "pre_client"
PRE_STEP_PRIX    = "pre_prix"


def build_user_message(transcription: str, extra: dict = None) -> str:
    today    = datetime.now()
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
                msg += f"  - \"{p['description']}\" → prix unitaire HT : {p['prix_unitaire_ht']} € / {p['unite']}\n"
    msg += "\n\nGénère le devis JSON complet. Réponds uniquement avec le JSON."
    return msg


def call_llm(transcription: str, user_id: int, extra: dict = None) -> dict:
    profile = get_client(user_id) or {}
    prompt  = inject_profile_in_prompt(SYSTEM_PROMPT, profile)

    response = llm.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user",   "content": build_user_message(transcription, extra)},
        ],
        temperature=0.1,
        max_tokens=8192,
    )
    raw = response.choices[0].message.content
    logger.info(f"[LLM] Réponse brute (200 car.) : {str(raw)[:200]}")
    return _parse_llm_json(raw)


# ── PDF ───────────────────────────────────────────────────────────────────────

def save_pdf_file(devis: dict, user_id: int) -> str:
    numero   = devis.get("meta", {}).get("numero_devis", "DEVIS")
    filename = numero.replace("/", "-").replace(" ", "_") + ".pdf"
    out_path = os.path.join(PDF_OUTPUT_DIR, filename)
    profile  = get_client(user_id) or {}
    show_assurance = profile.get("afficher_assurance", False)
    generate_pdf(devis, out_path, show_assurance=show_assurance)
    return out_path


# ── Inline keyboard post-devis ────────────────────────────────────────────────

def devis_keyboard(numero: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Modifier",          callback_data=f"edit:{numero}"),
            InlineKeyboardButton("📧 Envoyer au client", callback_data=f"send_devis:{numero}"),
        ],
        [
            InlineKeyboardButton("🧾 Convertir en facture", callback_data=f"to_facture:{numero}"),
        ],
    ])


def facture_keyboard(numero: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📧 Envoyer la facture", callback_data=f"send_facture:{numero}"),
    ]])


# ── Récapitulatif ─────────────────────────────────────────────────────────────

def build_recap_caption(devis: dict, titre: str = "✅ *Devis généré*") -> str:
    """
    Caption courte pour reply_document — jamais de flags ici.
    Telegram limite les captions à 1024 chars. On reste largement en dessous.
    """
    meta       = devis.get("meta", {})
    totaux     = devis.get("totaux", {})
    conditions = devis.get("conditions", {})
    client     = devis.get("client", {})
    flags      = [f for f in devis.get("flags", []) if f and f not in ("...", "")]

    msg = (
        f"{titre} — `{meta.get('numero_devis', '')}`\n"
        f"📋 *Chantier* : {meta.get('reference_chantier', '—')}\n"
        f"👤 *Client* : {client.get('nom', '[À COMPLÉTER]')}\n"
        f"📅 *Valable jusqu'au* : {meta.get('date_validite', '—')}\n\n"
        f"💶 *Total TTC* : {totaux.get('total_ttc', 0):,.2f} €\n"
        f"🔑 *Acompte* : {conditions.get('acompte_montant_ttc', 0):,.2f} €\n"
        f"📦 *Postes* : {len(devis.get('lignes', []))}\n"
    )
    if flags:
        msg += f"\n⚠️ *{len(flags)} point(s) à vérifier* — voir message suivant."
    return msg


def build_flags_message(devis: dict) -> str | None:
    """
    Message texte séparé pour les flags — envoyé après le document.
    Retourne None s'il n'y a aucun flag.
    """
    flags = [f for f in devis.get("flags", []) if f and f not in ("...", "")]
    if not flags:
        return None
    msg = f"⚠️ *{len(flags)} point(s) à vérifier avant d'envoyer le devis :*\n\n"
    for flag in flags:
        msg += f"  • {flag}\n"
    return msg


def build_recap(devis: dict) -> str:
    """Alias conservé pour compatibilité — retourne la caption courte."""
    return build_recap_caption(devis)


# ── Post-processing déterministe ──────────────────────────────────────────────

# Mapping : champ artisan JSON → champ profil
_ARTISAN_PROFIL_MAP = {
    "raison_sociale":      "raison_sociale",
    "adresse":             "adresse",
    "telephone":           "telephone",
    "email":               "email",
    "siret":               "siret",
    "tva_intracom":        "tva_intracom",
    "rcs_rm":              "rcs_rm",
    "assurance_decennale": "assurance_decennale",
    "numero_police":       "numero_police",
}

_PLACEHOLDER = ("[À COMPLÉTER]", "[À COMPLÉTER — OBLIGATOIRE]", "null", "None", "")


def _enrichir_artisan(devis: dict, profile: dict) -> dict:
    """
    Remplace les [À COMPLÉTER] dans le bloc artisan par les vraies valeurs du profil.
    Garantit la cohérence entre profil et devis même si l'injection prompt a raté.
    """
    artisan = devis.setdefault("artisan", {})

    def _fill(artisan_key: str, profile_value: str):
        current = str(artisan.get(artisan_key, "")).strip()
        if current in _PLACEHOLDER:
            if profile_value and str(profile_value).strip() not in _PLACEHOLDER:
                artisan[artisan_key] = profile_value

    for artisan_key, profil_key in _ARTISAN_PROFIL_MAP.items():
        _fill(artisan_key, profile.get(profil_key, ""))

    # Cas spécial : dirigeant → utilise le nom du dirigeant ou, à défaut, la raison sociale
    _fill("dirigeant", profile.get("dirigeant") or profile.get("raison_sociale", ""))

    return devis


def _supprimer_lignes_vides(devis: dict) -> dict:
    """
    Supprime les lignes dont le prix unitaire HT est 0 (inutile de les afficher),
    puis recalcule tous les totaux via calculs.py.
    """
    from calculs import recalculer_totaux

    def _val(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    devis["lignes"] = [
        l for l in devis.get("lignes", [])
        if _val(l.get("prix_unitaire_ht", 0)) != 0.0
    ]
    # Renuméroter les postes à partir de 1
    for i, ligne in enumerate(devis.get("lignes", []), start=1):
        ligne["poste"] = i
    return recalculer_totaux(devis)


# Flags toujours supprimés (mentions légales non obligatoires + flags montant global obsolètes)
_FLAGS_TOUJOURS_SUPPRIMES = [
    "médiateur", "mediateur", "tribunal",
    "montant global", "répartis à zéro", "repartis a zero",
]

# Mapping mot-clé dans le texte du flag → champ artisan JSON correspondant
# Si ce champ est rempli dans le devis, le flag est inutile
_FLAGS_ARTISAN_CHAMP_MAP = {
    "dirigeant":             "dirigeant",
    "e-mail":                "email",
    "email":                 "email",
    "courriel":              "email",
    "rcs":                   "rcs_rm",
    "registre du commerce":  "rcs_rm",
    "registre des m":        "rcs_rm",   # match "métiers" / "metiers"
    "raison sociale":        "raison_sociale",
    "nom de l'entreprise":   "raison_sociale",
    "siret":                 "siret",
    "adresse":               "adresse",
    "téléphone":             "telephone",
    "telephone":             "telephone",
    "tva intracom":          "tva_intracom",
    "n° tva":                "tva_intracom",
}


def _filtrer_flags(devis: dict, profile: dict = None) -> dict:
    """
    Allège la liste 'flags' du devis :
    - Supprime les mentions légales non obligatoires (médiateur, tribunal)
    - Supprime les flags dont le champ artisan est déjà renseigné dans le devis
      (appeler APRÈS _enrichir_artisan pour que le bloc artisan soit complet)
    """
    flags = devis.get("flags", [])
    if not flags:
        return devis

    artisan = devis.get("artisan", {})

    def _champ_rempli(artisan_field: str) -> bool:
        val = str(artisan.get(artisan_field, "")).strip()
        return bool(val) and val not in _PLACEHOLDER

    def _est_superflu(flag: str) -> bool:
        f = flag.lower()
        # Toujours supprimer
        for kw in _FLAGS_TOUJOURS_SUPPRIMES:
            if kw in f:
                return True
        # Supprimer si le champ artisan correspondant est déjà rempli
        for kw, artisan_field in _FLAGS_ARTISAN_CHAMP_MAP.items():
            if kw in f and _champ_rempli(artisan_field):
                return True
        return False

    devis["flags"] = [f for f in flags if not _est_superflu(f)]
    return devis


# ── Extraction pré-devis + helpers ───────────────────────────────────────────

async def _quick_extract_all(transcription: str) -> dict:
    """
    Appel LLM unique : extrait le client ET les postes en une seule requête.
    Retourne {"client": str|None, "lignes": [...]}
    """
    loop = asyncio.get_running_loop()

    def _call():
        resp = llm.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[{
                "role": "user",
                "content": (
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
                    f"Message vocal :\n{transcription[:2500]}"
                )
            }],
            temperature=0,
            max_tokens=500,
        )
        raw = resp.choices[0].message.content
        try:
            data = _parse_llm_json(raw)
        except (ValueError, json.JSONDecodeError):
            return {"client": None, "lignes": []}

        client = data.get("client")
        if client and str(client).lower() in ("null", "none", ""):
            client = None

        lignes = data.get("lignes", [])
        clean = []
        for l in lignes:
            if isinstance(l, dict) and l.get("description"):
                item = {
                    "description": str(l.get("description", "Poste")),
                    "unite":       str(l.get("unite", "forfait")),
                    "quantite":    float(l.get("quantite", 1)),
                }
                if l.get("prix_unitaire_ht") is not None:
                    try:
                        item["prix_unitaire_ht"] = float(l["prix_unitaire_ht"])
                    except (ValueError, TypeError):
                        pass
                clean.append(item)

        return {"client": client, "lignes": clean}

    try:
        return await loop.run_in_executor(None, _call)
    except Exception as e:
        logger.warning(f"[EXTRACT_ALL] Erreur : {str(e)[:200]}")
        return {"client": None, "lignes": []}


async def _ask_next_pre_question(
    message, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    """
    Pose la question client si pas encore connue, puis lance l'étape prix.
    """
    pre_extra = context.user_data.get("pre_extra", {})

    if "client" not in pre_extra:
        # Proposer d'ajouter les infos client via boutons inline (plus fluide)
        await message.reply_text(
            "👤 *Voulez-vous ajouter les coordonnées du client ?*\n"
            "_Nom, adresse, téléphone — utile pour personnaliser le devis._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👤 Oui, ajouter", callback_data="pre_client:add"),
                InlineKeyboardButton("⏭️ Non, générer",  callback_data="pre_client:skip"),
            ]]),
        )
        return

    # Client connu → passage à l'étape prix
    context.user_data.pop("pre_step", None)
    await _launch_prix_step(message, context, user_id)


async def _launch_prix_step(
    message, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    """
    Utilise les items déjà extraits par _quick_extract_all et pose les questions de prix.
    Si aucun poste détecté, génère le devis directement.
    """
    items = context.user_data.get("pre_transcription_items", [])

    if not items:
        await _do_generate_devis(message, context, user_id)
        return

    context.user_data["pre_prix_items"]  = items
    context.user_data["pre_prix_idx"]    = 0
    context.user_data["pre_prix_values"] = {}
    await _ask_next_prix_question(message, context, user_id)


async def _ask_next_prix_question(
    message, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    """
    Pose la question de prix pour le prochain poste non encore évalué.
    Si tous les postes sont traités, lance la génération du devis.
    """
    items  = context.user_data.get("pre_prix_items", [])
    values = context.user_data.get("pre_prix_values", {})
    idx    = context.user_data.get("pre_prix_idx", 0)

    # Avancer jusqu'au prochain poste non traité
    while idx < len(items):
        item = items[idx]
        if str(idx) in values:
            idx += 1
            continue
        if item.get("prix_unitaire_ht") is not None:
            # Prix déjà connu depuis le vocal, enregistrer et passer
            values[str(idx)] = item["prix_unitaire_ht"]
            idx += 1
            continue
        break  # Ce poste nécessite une question

    context.user_data["pre_prix_values"] = values
    context.user_data["pre_prix_idx"] = idx

    if idx >= len(items):
        # Tous les postes ont été traités → générer le devis
        context.user_data.pop("pre_step", None)
        await _do_generate_devis(message, context, user_id)
        return

    item = items[idx]
    desc = item.get("description", "Poste")
    qte  = item.get("quantite", 1)
    unit = item.get("unite", "forfait")
    total_items = len(items)

    context.user_data["pre_step"] = PRE_STEP_PRIX

    await message.reply_text(
        f"💶 *Poste {idx + 1}/{total_items} — {desc}*\n"
        f"_Quantité estimée : {qte} {unit}_\n\n"
        f"Quel prix unitaire HT souhaitez-vous facturer ?\n"
        f"_Entrez un montant en € (ex : 350) ou laissez l'IA décider._",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [[AI_DECIDE_BTN], [AI_DECIDE_ALL_BTN]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )


async def _do_generate_devis(
    message, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    """Génère le devis avec les données collectées dans user_data."""
    transcription = context.user_data.pop("pending_transcription", "")
    update_id     = context.user_data.pop("pending_update_id", 0)
    extra         = context.user_data.pop("pre_extra", {}) or {}
    context.user_data.pop("pre_step", None)

    # Récupérer les prix définis par l'artisan (étape pre_prix)
    prix_items  = context.user_data.pop("pre_prix_items", [])
    prix_values = context.user_data.pop("pre_prix_values", {})
    context.user_data.pop("pre_prix_idx", None)

    prix_postes = []
    for i, item in enumerate(prix_items):
        val = prix_values.get(str(i))
        if val is not None:  # None = IA décide, on n'injecte pas
            prix_postes.append({
                "description":      item.get("description", ""),
                "prix_unitaire_ht": val,
                "unite":            item.get("unite", "forfait"),
            })
    if prix_postes:
        extra["prix_postes"] = prix_postes

    status = await message.reply_text(
        "🤖 *Génération du devis en cours…*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    try:
        loop = asyncio.get_running_loop()
        devis = await loop.run_in_executor(None, lambda: call_llm(transcription, user_id, extra))

        # Post-processing déterministe : TVA zéro si mentionné dans le vocal
        if _detecter_tva_zero(transcription):
            devis = _appliquer_tva_zero(devis)

        if "erreur" in devis:
            await status.delete()
            await message.reply_text(f"⚠️ {devis.get('message', 'Description insuffisante.')}")
            return

        date_str = datetime.now().strftime("%Y%m%d")
        unique_id = uuid.uuid4().hex[:6].upper()
        devis.setdefault("meta", {})
        devis["meta"]["numero_devis"] = f"DEVIS-{date_str}-{unique_id}"

        profile = get_client(user_id) or {}
        devis.setdefault("artisan", {})
        if profile.get("iban"):
            devis["artisan"]["iban"] = profile["iban"]

        # Post-processing : enrichissement artisan + lignes vides + flags allégés
        devis = _enrichir_artisan(devis, profile)
        devis = _supprimer_lignes_vides(devis)
        devis = _filtrer_flags(devis)

        pdf_path = await loop.run_in_executor(None, lambda: save_pdf_file(devis, user_id))
        save_devis(user_id, devis)

        numero  = devis["meta"]["numero_devis"]
        caption = build_recap_caption(devis)
        flags_msg = build_flags_message(devis)

        await status.delete()
        with open(pdf_path, "rb") as f:
            await message.reply_document(
                document=f,
                filename=os.path.basename(pdf_path),
                caption=caption,
                parse_mode="Markdown",
                reply_markup=devis_keyboard(numero),
            )
        if flags_msg:
            await message.reply_text(flags_msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erreur génération devis : {e}", exc_info=True)
        try:
            await status.delete()
        except Exception:
            pass
        await message.reply_text(
            f"❌ *Erreur*\n`{type(e).__name__}: {str(e)[:200]}`", parse_mode="Markdown"
        )


# ── Modifications déterministes (sans LLM) ───────────────────────────────────

_TVA_ZERO_PATTERNS = [
    "non assujetti", "pas assujetti", "sans tva", "pas de tva",
    "tva 0", "0% tva", "0 % tva", "exempté", "exempt",
    "franchise en base", "micro-entreprise", "micro entreprise",
    "autoentrepreneur", "auto-entrepreneur", "auto entrepreneur",
    "art 293", "article 293", "hors tva", "ht seulement",
]

def _detecter_tva_zero(text: str) -> bool:
    """Renvoie True si le texte demande de supprimer/mettre à zéro la TVA."""
    t = text.lower()
    for p in _TVA_ZERO_PATTERNS:
        if p in t:
            return True
    return False


def _appliquer_tva_zero(devis: dict) -> dict:
    """
    Applique la suppression totale de TVA sur un devis.
    Délègue au module centralisé calculs.py — résultat garanti correct.
    """
    from calculs import appliquer_taux_tva_uniforme
    return appliquer_taux_tva_uniforme(devis, 0.0)


# ── Handler : message vocal ───────────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message = update.message

    if not client_exists(user_id):
        await message.reply_text(
            "👋 Bienvenue ! Vous devez d'abord créer votre profil.\nTapez /start pour commencer.",
            parse_mode="Markdown",
        )
        return

    status = await message.reply_text("🎙️ *Message reçu !* Transcription…", parse_mode="Markdown")
    ogg_path = None

    try:
        voice_file = await context.bot.get_file(message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            ogg_path = tmp.name
        await voice_file.download_to_drive(ogg_path)

        await status.edit_text("🔤 *Transcription Whisper…*", parse_mode="Markdown")
        transcription = await transcribe_audio(ogg_path)

        if not transcription:
            await status.edit_text("❌ Transcription vide. Réessaie dans un endroit calme.")
            return

        await status.delete()

        # ── Si une modification de devis est en cours, router le vocal comme texte ──
        if context.user_data.get("editing_devis"):
            # Simuler un message texte avec la transcription → même chemin que handle_text
            class _FakeUpdate:
                class _FakeMsg:
                    text = transcription
                    reply_text = message.reply_text
                    reply_document = message.reply_document
                message = _FakeMsg()
                effective_user = update.effective_user
            fake_update = _FakeUpdate()
            await handle_text(fake_update, context)
            return

        # Extraction unifiée : client + postes en un seul appel LLM
        extracted = await _quick_extract_all(transcription)

        context.user_data["pending_transcription"]   = transcription
        context.user_data["pending_update_id"]       = update.update_id
        context.user_data["pre_extra"]               = {"client": extracted.get("client")}
        context.user_data["pre_transcription_items"] = extracted.get("lignes", [])

        recap_line = "_( client déjà détecté)_\n\n" if extracted.get("client") else ""
        await message.reply_text(
            f"✅ *Transcription OK* ({len(transcription)} car.)\n{recap_line}",
            parse_mode="Markdown",
        )
        await _ask_next_pre_question(message, context, user_id)

    except Exception as e:
        logger.error(f"Erreur : {e}", exc_info=True)
        await status.edit_text(f"❌ *Erreur*\n`{type(e).__name__}: {str(e)[:200]}`", parse_mode="Markdown")
    finally:
        if ogg_path and os.path.exists(ogg_path):
            os.unlink(ogg_path)


# ── Callback : boutons inline ─────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    action, numero = query.data.split(":", 1)

    # ── Édition d'un champ du profil ─────────────────────────────────────────────
    if action == "profil_edit":
        field    = numero   # le champ à modifier
        question = _PROFIL_FIELD_QUESTIONS.get(field, f"✏️ *Nouvelle valeur pour {field} ?*")
        context.user_data["editing_profil_field"] = field
        await query.message.reply_text(
            question + "\n\n_Tapez 'annuler' pour ne rien changer._",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["Annuler"]], resize_keyboard=True, one_time_keyboard=True),
        )
        return

    # ── Bascule d'un booléen du profil (ex : afficher_assurance) ──────────────
    if action == "profil_toggle":
        field   = numero
        profile = get_client(user_id) or {}
        profile[field] = not profile.get(field, False)
        save_client(user_id, profile)
        icon = "✅" if profile[field] else "❌"
        await query.answer(f"{'Activé' if profile[field] else 'Désactivé'} !")
        # Rafraîchir l'affichage du profil
        await _show_profil_menu(query.message, user_id)
        return

    # ── Réponse à la question "Voulez-vous ajouter les infos client ?" ──────────
    if action == "pre_client":
        if numero == "add":
            # L'utilisateur veut entrer les coordonnées → passer en mode saisie texte
            context.user_data["pre_step"] = PRE_STEP_CLIENT
            await query.message.reply_text(
                "👤 *Coordonnées du client ?*\n"
                "_Nom, adresse, téléphone — ou tapez 'passer' si pas disponible._",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardMarkup([[SKIP_BTN]], resize_keyboard=True, one_time_keyboard=True),
            )
        else:  # skip → générer directement sans info client
            context.user_data.setdefault("pre_extra", {})["client"] = None
            await _launch_prix_step(query.message, context, user_id)
        return

    # ── Sélection du type de facture ─────────────────────────────────────────
    if action == "to_facture":
        devis = load_devis(user_id, numero)
        if not devis:
            await query.message.reply_text("❌ Devis introuvable.")
            return

        total_ttc     = devis.get("totaux", {}).get("total_ttc", 0)
        deja_facture  = get_total_deja_facture(user_id, numero)
        reste         = round(total_ttc - deja_facture, 2)
        acompte_pct   = devis.get("conditions", {}).get("acompte_pourcentage", 30)
        acompte_ttc   = round(total_ttc * acompte_pct / 100, 2)

        lines = [f"🧾 *Quel type de facture pour* `{numero}` ?"]
        if deja_facture > 0:
            lines.append(f"\n_Déjà facturé : {deja_facture:,.2f} € / Reste : {reste:,.2f} €_")

        await query.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        f"💰 Acompte ({acompte_pct}% — {acompte_ttc:,.2f} €)",
                        callback_data=f"facture_type:{numero}:acompte",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📋 Situation intermédiaire",
                        callback_data=f"facture_type:{numero}:intermediaire",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        f"✅ Solde final ({reste:,.2f} €)",
                        callback_data=f"facture_type:{numero}:solde",
                    ),
                ],
            ]),
        )

    # ── Génération d'une facture typée ────────────────────────────────────────
    elif action == "facture_type":
        # numero contient "DEVIS-XXXXX:type"
        numero_devis, type_facture = numero.rsplit(":", 1)

        devis = load_devis(user_id, numero_devis)
        if not devis:
            await query.message.reply_text("❌ Devis introuvable.")
            return

        if type_facture == "intermediaire":
            # On demande le montant avant de générer
            context.user_data["waiting_facture_inter"] = {"numero_devis": numero_devis}
            await query.message.reply_text(
                "📋 *Situation intermédiaire*\n\n"
                "Quel montant (€ TTC) facturer pour cette étape ?\n"
                "_Ex : 2500 ou 2500.00_",
                parse_mode="Markdown",
            )
            return

        total_ttc    = devis.get("totaux", {}).get("total_ttc", 0)
        factures_prec = get_factures_for_devis(user_id, numero_devis)
        deja_facture  = get_total_deja_facture(user_id, numero_devis)

        if type_facture == "acompte":
            acompte_pct = devis.get("conditions", {}).get("acompte_pourcentage", 30)
            montant_ttc = round(total_ttc * acompte_pct / 100, 2)
        else:  # solde
            montant_ttc = round(total_ttc - deja_facture, 2)
            if montant_ttc <= 0:
                await query.message.reply_text(
                    "⚠️ Le devis est déjà intégralement facturé — aucun solde restant."
                )
                return

        await _generer_facture_typee(
            query.message, context, user_id, devis, type_facture,
            montant_ttc, deja_facture, factures_prec
        )

    # ── Envoyer devis par email ───────────────────────────────────────────────
    elif action == "send_devis":
        devis = load_devis(user_id, numero)
        if not devis:
            await query.message.reply_text("❌ Devis introuvable.")
            return

        profile = get_client(user_id) or {}
        if not profile.get("gmail_address") or not profile.get("gmail_app_password"):
            await query.message.reply_text(
                "⚠️ Vous n'avez pas configuré votre Gmail.\n"
                "Tapez /profil pour ajouter votre adresse et mot de passe d'application."
            )
            return

        # Récupérer l'email du client depuis le devis ou le demander
        client_email = devis.get("client", {}).get("email", "")
        if client_email and "@" in client_email:
            context.user_data["pending_send"] = {"type": "devis", "numero": numero, "email": client_email}
            await _do_send_devis(query.message, context, user_id, numero, client_email)
        else:
            context.user_data["pending_send"] = {"type": "devis", "numero": numero}
            await query.message.reply_text(
                "📧 *Email du client ?*\n_(répondez avec l'adresse email)_",
                parse_mode="Markdown",
            )
            context.user_data["waiting_for_email"] = "devis"

    # ── Envoyer facture par email ─────────────────────────────────────────────
    elif action == "send_facture":
        facture = load_facture(user_id, numero) or load_devis(user_id, numero)
        if not facture:
            await query.message.reply_text("❌ Facture introuvable.")
            return

        profile = get_client(user_id) or {}
        if not profile.get("gmail_address"):
            await query.message.reply_text("⚠️ Gmail non configuré. Tapez /profil.")
            return

        client_email = facture.get("client", {}).get("email", "")
        if client_email and "@" in client_email:
            await _do_send_facture(query.message, context, user_id, numero, client_email)
        else:
            context.user_data["pending_send"] = {"type": "facture", "numero": numero}
            await query.message.reply_text(
                "📧 *Email du client ?*\n_(répondez avec l'adresse email)_",
                parse_mode="Markdown",
            )
            context.user_data["waiting_for_email"] = "facture"

    # ── Modifier devis ────────────────────────────────────────────────────────
    elif action == "edit":
        # Nettoyer tout state pré-devis en cours pour éviter les conflits
        context.user_data.pop("pre_step", None)
        context.user_data.pop("pending_transcription", None)
        context.user_data.pop("pre_extra", None)
        context.user_data.pop("pending_update_id", None)
        context.user_data.pop("pre_prix_items", None)
        context.user_data.pop("pre_prix_idx", None)
        context.user_data.pop("pre_prix_values", None)
        context.user_data["editing_devis"] = numero
        await query.message.reply_text(
            "✏️ *Décrivez les modifications à apporter* :\n\n"
            "_Exemple : « Je ne suis pas assujetti à la TVA », « Changer la MO à 2 jours »_",
            parse_mode="Markdown",
        )


async def _generer_facture_typee(
    message, context, user_id: int, devis: dict,
    type_facture: str, montant_ttc: float,
    deja_facture_ttc: float, factures_prec: list,
) -> None:
    """Génère, sauvegarde et envoie une facture typée (acompte / intermediaire / solde)."""
    profile = get_client(user_id) or {}
    facture = devis_to_facture_typed(
        devis, type_facture, montant_ttc, deja_facture_ttc, factures_prec
    )
    if profile.get("iban"):
        facture["artisan"]["iban"] = profile["iban"]

    num_fac  = facture["meta"]["numero_facture"]
    filename = num_fac.replace("/", "-") + ".pdf"
    fac_path = os.path.join(PDF_OUTPUT_DIR, filename)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: generate_facture_pdf(facture, fac_path))
    save_facture(user_id, facture)

    labels_emoji = {"acompte": "💰", "intermediaire": "📋", "solde": "✅"}
    emoji = labels_emoji.get(type_facture, "🧾")

    with open(fac_path, "rb") as f:
        await message.reply_document(
            document=f,
            filename=filename,
            caption=(
                f"{emoji} *{facture['facturation']['titre_section']}* — `{num_fac}`\n"
                f"📋 *Chantier* : {facture['meta'].get('reference_chantier', '—')}\n"
                f"💶 *Ce montant* : {montant_ttc:,.2f} €\n"
                f"📅 *Échéance* : {facture['meta'].get('date_echeance', '—')}"
            ),
            parse_mode="Markdown",
            reply_markup=facture_keyboard(num_fac),
        )


async def _do_send_devis(message, context, user_id, numero, to_email):
    devis   = load_devis(user_id, numero)
    profile = get_client(user_id)

    filename = numero.replace("/", "-") + ".pdf"
    pdf_path = os.path.join(PDF_OUTPUT_DIR, filename)

    if not os.path.exists(pdf_path):
        _profile_send = get_client(user_id) or {}
        generate_pdf(devis, pdf_path, show_assurance=_profile_send.get("afficher_assurance", False))

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: send_devis_email(
                gmail_address=profile["gmail_address"],
                gmail_app_password=profile["gmail_app_password"],
                to_email=to_email,
                pdf_path=pdf_path,
                devis=devis,
            )
        )
        await message.reply_text(
            f"✅ *Devis envoyé* à `{to_email}` !\n\n💡 {REMISE_EN_MAIN_PROPRE_WARNING}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await message.reply_text(f"❌ Erreur d'envoi : `{str(e)[:200]}`", parse_mode="Markdown")


async def _do_send_facture(message, context, user_id, numero, to_email):
    facture  = load_facture(user_id, numero) or load_devis(user_id, numero)
    profile  = get_client(user_id)

    filename = numero.replace("/", "-") + ".pdf"
    fac_path = os.path.join(PDF_OUTPUT_DIR, filename)

    if not os.path.exists(fac_path):
        generate_facture_pdf(facture, fac_path)

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: send_facture_email(
                gmail_address=profile["gmail_address"],
                gmail_app_password=profile["gmail_app_password"],
                to_email=to_email,
                pdf_path=fac_path,
                facture=facture,
            )
        )
        await message.reply_text(f"✅ *Facture envoyée* à `{to_email}` !", parse_mode="Markdown")
    except Exception as e:
        await message.reply_text(f"❌ Erreur d'envoi : `{str(e)[:200]}`", parse_mode="Markdown")


# ── Modifications déterministes (patch + recalcul) ──────────────────────────

async def _apply_patch(devis: dict, modifications: list) -> dict:
    """
    Applique une liste de modifications atomiques sur un devis.
    NE recalcule PAS les totaux — appeler recalculer_totaux() après.
    """
    import copy
    d = copy.deepcopy(devis)

    for mod in modifications:
        mod_type = mod.get("type")

        if mod_type == "prix_unitaire":
            idx = mod.get("poste_index", -1)
            val = mod.get("nouvelle_valeur", 0)
            if 0 <= idx < len(d.get("lignes", [])):
                d["lignes"][idx]["prix_unitaire_ht"] = float(val)

        elif mod_type == "quantite":
            idx = mod.get("poste_index", -1)
            val = mod.get("nouvelle_valeur", 0)
            if 0 <= idx < len(d.get("lignes", [])):
                d["lignes"][idx]["quantite"] = float(val)

        elif mod_type == "description":
            idx = mod.get("poste_index", -1)
            val = mod.get("nouvelle_valeur", "")
            if 0 <= idx < len(d.get("lignes", [])):
                d["lignes"][idx]["description"] = str(val)

        elif mod_type == "taux_tva_global":
            taux = float(mod.get("nouvelle_valeur", 0.10))
            for ligne in d.get("lignes", []):
                ligne["taux_tva"] = taux
            if taux == 0.0:
                d.setdefault("artisan", {})
                d["artisan"]["tva_intracom"] = "Non assujetti à la TVA — Art. 293 B du CGI"
                d.setdefault("mentions_legales", {})
                d["mentions_legales"]["tva_note"] = "Non assujetti à la TVA — Art. 293 B du CGI"

        elif mod_type == "acompte_pourcentage":
            val = int(mod.get("nouvelle_valeur", 30))
            d.setdefault("conditions", {})
            d["conditions"]["acompte_pourcentage"] = val

        elif mod_type == "ajouter_ligne":
            nouvelle_ligne = mod.get("ligne", {})
            if nouvelle_ligne.get("description"):
                q = float(nouvelle_ligne.get("quantite", 1))
                p = float(nouvelle_ligne.get("prix_unitaire_ht", 0))
                nouvelle_ligne["montant_ht"] = round(q * p, 2)
                nouvelle_ligne["taux_tva"]   = float(nouvelle_ligne.get("taux_tva", 0.10))
                d.setdefault("lignes", []).append(nouvelle_ligne)

        elif mod_type == "supprimer_ligne":
            idx = mod.get("poste_index", -1)
            if 0 <= idx < len(d.get("lignes", [])):
                d["lignes"].pop(idx)

        elif mod_type == "client":
            champs = mod.get("champs", {})
            d.setdefault("client", {})
            for key, val in champs.items():
                if val and str(val).lower() not in ("null", "none", ""):
                    d["client"][key] = str(val)

        elif mod_type == "delai_realisation":
            val = mod.get("nouvelle_valeur", "")
            if val:
                d.setdefault("conditions", {})
                d["conditions"]["delai_realisation"] = str(val)

        elif mod_type == "reference_chantier":
            val = mod.get("nouvelle_valeur", "")
            if val:
                d.setdefault("meta", {})
                d["meta"]["reference_chantier"] = str(val)

        elif mod_type == "taux_tva_ligne":
            idx  = mod.get("poste_index", -1)
            taux = float(mod.get("nouvelle_valeur", 0.10))
            if 0 <= idx < len(d.get("lignes", [])):
                d["lignes"][idx]["taux_tva"] = taux

        elif mod_type == "detail_ligne":
            idx = mod.get("poste_index", -1)
            val = mod.get("nouvelle_valeur", "")
            if 0 <= idx < len(d.get("lignes", [])):
                d["lignes"][idx]["detail"] = str(val)

        elif mod_type == "unite_ligne":
            idx = mod.get("poste_index", -1)
            val = mod.get("nouvelle_valeur", "")
            if 0 <= idx < len(d.get("lignes", [])):
                d["lignes"][idx]["unite"] = str(val)

        elif mod_type == "modalites_paiement":
            val = mod.get("nouvelle_valeur", "")
            if val:
                d.setdefault("conditions", {})
                d["conditions"]["modalites_paiement"] = str(val)

        else:
            logger.warning(f"[PATCH IGNORÉ] Type inconnu : '{mod_type}' — {mod}")

    return d


# ── Handler : réception email client ─────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text    = update.message.text.strip()

    # ── Guard : utilisateur sans profil → onboarding ─────────────────────────
    if not context.user_data.get("editing_profil_field") and not client_exists(user_id):
        await update.message.reply_text(
            "👋 Bienvenue sur *Souffl.AI* !\n"
            "Avant de générer votre premier devis, créons votre profil artisan.\n\n"
            "👉 Tapez /start pour commencer.",
            parse_mode="Markdown",
        )
        return

    # ── 0. Édition d'un champ du profil (priorité absolue) ───────────────────
    if context.user_data.get("editing_profil_field"):
        field = context.user_data.pop("editing_profil_field")

        if text.lower() in ("annuler", "cancel", "⏭️ passer"):
            await update.message.reply_text(
                "Modification annulée.", reply_markup=ReplyKeyboardRemove()
            )
            await _show_profil_menu(update.message, user_id)
            return

        profile = get_client(user_id) or {}

        # Nettoyage / validation selon le champ
        if field == "journee_standard_ht":
            try:
                profile[field] = float(text.replace(",", ".").replace("€", "").strip())
            except ValueError:
                await update.message.reply_text(
                    "⚠️ Valeur invalide — entrez un nombre (ex : *350*).",
                    parse_mode="Markdown",
                )
                context.user_data["editing_profil_field"] = field   # remettre en attente
                return
        elif field in ("iban", "gmail_app_password"):
            profile[field] = text.replace(" ", "")
        elif field == "siret":
            profile[field] = text.replace(" ", "")
        else:
            profile[field] = text.strip()

        save_client(user_id, profile)
        label = _PROFIL_FIELD_LABELS.get(field, field)
        await update.message.reply_text(
            f"✅ *{label}* mis à jour !",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        await _show_profil_menu(update.message, user_id)
        return

    # ── 1. Modification de devis (priorité maximale) ──────────────────────────
    if context.user_data.get("editing_devis"):
        numero = context.user_data.pop("editing_devis")
        devis  = load_devis(user_id, numero)
        if not devis:
            await update.message.reply_text("❌ Devis introuvable.")
            return

        status = await update.message.reply_text("✏️ *Application des modifications…*", parse_mode="Markdown")
        try:
            # ── Cas TVA = 0 : traitement 100% Python, sans LLM ───────────────
            if _detecter_tva_zero(text):
                new_devis = _appliquer_tva_zero(devis)
                new_devis["meta"]["numero_devis"] = numero
                _profile = get_client(user_id) or {}
                new_devis = _enrichir_artisan(new_devis, _profile)
                new_devis = _supprimer_lignes_vides(new_devis)
                new_devis = _filtrer_flags(new_devis)
                loop = asyncio.get_running_loop()
                pdf_path = await loop.run_in_executor(None, lambda: save_pdf_file(new_devis, user_id))
                save_devis(user_id, new_devis)
                await status.delete()
                caption_mod = build_recap_caption(new_devis, titre="✅ *Devis modifié — TVA supprimée*")
                flags_msg   = build_flags_message(new_devis)
                with open(pdf_path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=os.path.basename(pdf_path),
                        caption=caption_mod,
                        parse_mode="Markdown",
                        reply_markup=devis_keyboard(numero),
                    )
                if flags_msg:
                    await update.message.reply_text(flags_msg, parse_mode="Markdown")
                return

            # ── Autres modifications : LLM patch + Python calcule ─────────────────────────
            devis_json  = json.dumps(devis, ensure_ascii=False)
            modif_texte = text

            mod_system = (
                "Tu es un assistant expert en devis du bâtiment.\n"
                "Ta mission : identifier les modifications à apporter à un devis JSON existant.\n\n"
                "Retourne UNIQUEMENT un patch JSON minimal (jamais le devis entier).\n\n"
                "Types disponibles :\n"
                '  {"type": "prix_unitaire",      "poste_index": 2,   "nouvelle_valeur": 350.0}\n'
                '  {"type": "quantite",           "poste_index": 1,   "nouvelle_valeur": 2.5}\n'
                '  {"type": "description",        "poste_index": 1,   "nouvelle_valeur": "Nouveau libellé"}\n'
                '  {"type": "detail_ligne",       "poste_index": 1,   "nouvelle_valeur": "Détail technique"}\n'
                '  {"type": "unite_ligne",        "poste_index": 1,   "nouvelle_valeur": "jour"}\n'
                '  {"type": "taux_tva_global",                        "nouvelle_valeur": 0.20}\n'
                '  {"type": "taux_tva_ligne",     "poste_index": 2,   "nouvelle_valeur": 0.055}\n'
                '  {"type": "acompte_pourcentage",                    "nouvelle_valeur": 50}\n'
                '  {"type": "delai_realisation",                      "nouvelle_valeur": "3 jours ouvrés"}\n'
                '  {"type": "reference_chantier",                     "nouvelle_valeur": "Salle de bain RDC"}\n'
                '  {"type": "modalites_paiement",                     "nouvelle_valeur": "Virement uniquement"}\n'
                '  {"type": "client",             "champs": {"nom": "...", "adresse_chantier": "...", "telephone": "...", "email": "..."}}\n'
                '  {"type": "ajouter_ligne",      "ligne": {"description": "...", "quantite": 1.0, "prix_unitaire_ht": 100.0, "taux_tva": 0.10}}\n'
                '  {"type": "supprimer_ligne",    "poste_index": 4}\n\n'
                "Taux TVA valides : 0.0 / 0.10 / 0.055 / 0.20\n"
                "poste_index : position dans lignes[] à partir de 0\n"
                "NE recalcule PAS les totaux — Python s'en charge.\n"
                "Identifie UNIQUEMENT les modifications demandées.\n"
                "Retourne un JSON valide : {\"modifications\": [...]}"
            )
            mod_user = (
                f"Devis JSON existant :\n{devis_json}\n\n"
                f"Modifications demandées :\n{modif_texte}\n\n"
                "Retourne le JSON patch."
            )

            def _call_mod():
                resp = llm.chat.completions.create(
                    model=OPENROUTER_MODEL,
                    messages=[
                        {"role": "system", "content": mod_system},
                        {"role": "user",   "content": mod_user},
                    ],
                    temperature=0.1, max_tokens=1024,
                )
                raw = resp.choices[0].message.content
                logger.info(f"[MOD] Patch LLM : {str(raw)[:500]}")
                try:
                    return _parse_llm_json(raw)
                except (ValueError, json.JSONDecodeError):
                    return {"modifications": []}

            loop = asyncio.get_running_loop()
            patch    = await loop.run_in_executor(None, _call_mod)
            new_devis = await _apply_patch(devis, patch.get("modifications", []))

            # Recalcul déterministe Python
            from calculs import recalculer_totaux
            new_devis = recalculer_totaux(new_devis)
            new_devis["meta"]["numero_devis"] = numero
            _profile = get_client(user_id) or {}
            new_devis = _enrichir_artisan(new_devis, _profile)
            new_devis = _supprimer_lignes_vides(new_devis)
            new_devis = _filtrer_flags(new_devis)

            pdf_path = await loop.run_in_executor(None, lambda: save_pdf_file(new_devis, user_id))
            save_devis(user_id, new_devis)

            await status.delete()
            caption_mod = build_recap_caption(new_devis, titre="✅ *Devis modifié*")
            flags_msg   = build_flags_message(new_devis)
            with open(pdf_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=os.path.basename(pdf_path),
                    caption=caption_mod,
                    parse_mode="Markdown",
                    reply_markup=devis_keyboard(numero),
                )
            if flags_msg:
                await update.message.reply_text(flags_msg, parse_mode="Markdown")
        except Exception as e:
            try:
                await status.delete()
            except Exception:
                pass
            await update.message.reply_text(f"❌ Erreur modification : `{str(e)[:200]}`", parse_mode="Markdown")
        return

    # ── 2. Séquence pré-devis ─────────────────────────────────────────────────
    step = context.user_data.get("pre_step")
    if step == PRE_STEP_CLIENT:
        if not is_skip(text):
            context.user_data["pre_extra"]["client"] = text
        context.user_data["pre_extra"].setdefault("client", None)
        await _ask_next_pre_question(update.message, context, user_id)
        return

    # ── 3. Prix des postes pré-devis ──────────────────────────────────────────
    if context.user_data.get("pre_step") == PRE_STEP_PRIX:
        items  = context.user_data.get("pre_prix_items", [])
        idx    = context.user_data.get("pre_prix_idx", 0)
        values = context.user_data.setdefault("pre_prix_values", {})

        if text.strip() == AI_DECIDE_ALL_BTN:
            # Sauter toutes les questions de prix → générer directement
            context.user_data.pop("pre_step", None)
            context.user_data.pop("pre_prix_items", None)
            context.user_data.pop("pre_prix_idx", None)
            context.user_data.pop("pre_prix_values", None)
            await _do_generate_devis(update.message, context, user_id)
            return

        if is_skip(text) or text.strip() == AI_DECIDE_BTN:
            # IA décide pour ce poste
            values[str(idx)] = None
        else:
            # L'artisan donne un prix
            try:
                clean_price = text.strip().replace(",", ".").replace("€", "").replace(" ", "")
                values[str(idx)] = float(clean_price)
            except ValueError:
                await update.message.reply_text(
                    "⚠️ Prix non reconnu. Entrez un montant en € _(ex : 350 ou 125.50)_ "
                    f"ou appuyez sur *{AI_DECIDE_BTN}*.",
                    parse_mode="Markdown",
                )
                return

        context.user_data["pre_prix_idx"] = idx + 1
        await _ask_next_prix_question(update.message, context, user_id)
        return

    # ── Montant situation intermédiaire en attente ────────────────────────────
    if context.user_data.get("waiting_facture_inter"):
        pending_inter = context.user_data.pop("waiting_facture_inter")
        numero_devis  = pending_inter["numero_devis"]
        devis         = load_devis(user_id, numero_devis)
        if not devis:
            await update.message.reply_text("❌ Devis introuvable.")
            return
        total_ttc = devis.get("totaux", {}).get("total_ttc", 0)
        try:
            # Accepte "50%" ou "2500" ou "2 500,00"
            clean = text.strip().replace(" ", "").replace(",", ".")
            if clean.endswith("%"):
                pct         = float(clean[:-1])
                montant_ttc = round(total_ttc * pct / 100, 2)
            else:
                montant_ttc = float(clean.replace("€", ""))
        except ValueError:
            context.user_data["waiting_facture_inter"] = pending_inter  # remettre l'état
            await update.message.reply_text(
                "⚠️ Montant non reconnu. Entrez un nombre en € (ex : *2500*) ou un pourcentage (ex : *50%*).",
                parse_mode="Markdown",
            )
            return

        deja_facture  = get_total_deja_facture(user_id, numero_devis)
        factures_prec = get_factures_for_devis(user_id, numero_devis)
        reste         = round(total_ttc - deja_facture, 2)

        if montant_ttc > reste + 0.01:
            await update.message.reply_text(
                f"⚠️ Montant supérieur au reste à facturer ({reste:,.2f} €).\n"
                "Entrez un montant ≤ au reste, ou utilisez *Solde final*.",
                parse_mode="Markdown",
            )
            context.user_data["waiting_facture_inter"] = pending_inter
            return

        status = await update.message.reply_text("📋 *Génération de la situation…*", parse_mode="Markdown")
        try:
            await _generer_facture_typee(
                update.message, context, user_id, devis,
                "intermediaire", montant_ttc, deja_facture, factures_prec
            )
        except Exception as e:
            logger.error(f"Erreur situation intermédiaire : {e}", exc_info=True)
            await update.message.reply_text(f"❌ Erreur : `{str(e)[:200]}`", parse_mode="Markdown")
        finally:
            try:
                await status.delete()
            except Exception:
                pass
        return

    # Envoi email en attente
    if context.user_data.get("waiting_for_email") and re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', text.strip()):
        pending = context.user_data.pop("pending_send", {})
        context.user_data.pop("waiting_for_email", None)
        numero = pending.get("numero", "")

        if pending.get("type") == "devis":
            await _do_send_devis(update.message, context, user_id, numero, text)
        else:
            await _do_send_facture(update.message, context, user_id, numero, text)
        return

    await update.message.reply_text(
        "🎙️ Envoyez un *message vocal* pour générer un devis.\nOu tapez /devis pour voir vos derniers devis.",
        parse_mode="Markdown",
    )


# ── Commandes ─────────────────────────────────────────────────────────────────

async def cmd_devis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    items   = list_devis(user_id)

    if not items:
        await update.message.reply_text("Vous n'avez pas encore de devis. Envoyez un vocal pour en créer un !")
        return

    lines = ["📋 *Vos derniers devis :*\n"]
    for i, d in enumerate(items, 1):
        lines.append(
            f"{i}. `{d['numero']}` — {d['date']}\n"
            f"   👤 {d['client']} | 💶 {d['total_ttc']:,.2f} €\n"
            f"   📌 {d['chantier']}"
        )
    lines.append("\nTapez le numéro d'un devis pour le sélectionner, ou envoyez un vocal pour en créer un nouveau.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_facture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    items   = list_devis(user_id)

    if not items:
        await update.message.reply_text("Aucun devis disponible pour créer une facture.")
        return

    keyboard = []
    for d in items:
        keyboard.append([InlineKeyboardButton(
            f"{d['numero']} — {d['client']} — {d['total_ttc']:,.2f} €",
            callback_data=f"to_facture:{d['numero']}"
        )])

    await update.message.reply_text(
        "🧾 *Quel devis convertir en facture ?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── Profil : affichage + édition inline ──────────────────────────────────────

# Labels affichés dans le bot de confirmation
_PROFIL_FIELD_LABELS = {
    "raison_sociale":      "Entreprise",
    "siret":               "SIRET",
    "adresse":             "Adresse",
    "telephone":           "Téléphone",
    "email":               "Email pro",
    "assurance_decennale": "Assureur décennale",
    "numero_police":       "N° police assurance",
    "iban":                "IBAN",
    "tva_intracom":        "N° TVA / statut TVA",
    "gmail_address":       "Adresse Gmail",
    "gmail_app_password":  "Mot de passe Gmail",
    "journee_standard_ht": "Tarif journée (€ HT)",
}

# Questions posées lors de l'édition de chaque champ
_PROFIL_FIELD_QUESTIONS = {
    "raison_sociale":      "🏢 *Nouveau nom d'entreprise ?*",
    "siret":               "🪪 *Nouveau numéro SIRET ?* (14 chiffres)",
    "adresse":             "📍 *Nouvelle adresse complète ?*",
    "telephone":           "📞 *Nouveau numéro de téléphone ?*",
    "email":               "📧 *Nouvel email professionnel ?*",
    "assurance_decennale": "🛡️ *Nom du nouvel assureur décennale ?*",
    "numero_police":       "🔢 *Nouveau numéro de police d'assurance ?*",
    "iban":                "🏦 *Nouvel IBAN ?*",
    "tva_intracom":        "💶 *Numéro TVA intracommunautaire ?*\n_(ou tapez 'Non assujetti à la TVA')_",
    "gmail_address":       "✉️ *Adresse Gmail pour l'envoi des devis ?*",
    "gmail_app_password":  "🔑 *Mot de passe d'application Gmail ?*\n_(16 caractères — pas votre vrai mot de passe)_",
    "journee_standard_ht": "💰 *Tarif journée standard en € HT ?*\n_(ex : 350)_",
}


def _profil_keyboard(profile: dict) -> InlineKeyboardMarkup:
    assurance_icon = "✅" if profile.get("afficher_assurance") else "❌"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏢 Entreprise",       callback_data="profil_edit:raison_sociale"),
            InlineKeyboardButton("🪪 SIRET",             callback_data="profil_edit:siret"),
        ],
        [InlineKeyboardButton("📍 Adresse",             callback_data="profil_edit:adresse")],
        [
            InlineKeyboardButton("📞 Téléphone",        callback_data="profil_edit:telephone"),
            InlineKeyboardButton("📧 Email pro",         callback_data="profil_edit:email"),
        ],
        [
            InlineKeyboardButton("🛡️ Assureur",        callback_data="profil_edit:assurance_decennale"),
            InlineKeyboardButton("🔢 N° police",        callback_data="profil_edit:numero_police"),
        ],
        [
            InlineKeyboardButton("🏦 IBAN",              callback_data="profil_edit:iban"),
            InlineKeyboardButton("💶 TVA",               callback_data="profil_edit:tva_intracom"),
        ],
        [
            InlineKeyboardButton("✉️ Gmail",             callback_data="profil_edit:gmail_address"),
            InlineKeyboardButton("🔑 Mdp Gmail",         callback_data="profil_edit:gmail_app_password"),
        ],
        [InlineKeyboardButton("💰 Tarif journée HT",    callback_data="profil_edit:journee_standard_ht")],
        [InlineKeyboardButton(
            f"🛡️ Assurance sur devis : {assurance_icon}",
            callback_data="profil_toggle:afficher_assurance",
        )],
    ])


async def _show_profil_menu(target, user_id: int) -> None:
    """Envoie (ou édite) le message de profil avec le clavier d'édition inline."""
    profile = get_client(user_id) or {}
    assurance_icon = "✅" if profile.get("afficher_assurance") else "❌"
    gmail_status   = "✅ Configuré" if profile.get("gmail_address") else "❌ Non configuré"
    tarif          = profile.get("journee_standard_ht") or 350

    text = (
        "*Votre profil :*\n\n"
        f"🏢 *Entreprise* : {profile.get('raison_sociale', '—')}\n"
        f"🪪 *SIRET* : {profile.get('siret', '—')}\n"
        f"📍 *Adresse* : {profile.get('adresse', '—')}\n"
        f"📞 *Téléphone* : {profile.get('telephone', '—')}\n"
        f"📧 *Email* : {profile.get('email', '—')}\n"
        f"🛡️ *Assureur* : {profile.get('assurance_decennale', '—')} — Police : {profile.get('numero_police', '—')}\n"
        f"🏦 *IBAN* : {profile.get('iban', '—')}\n"
        f"💶 *TVA* : {profile.get('tva_intracom', '—')}\n"
        f"✉️ *Gmail* : {gmail_status}\n"
        f"💰 *Tarif journée* : {tarif} €/j\n"
        f"🛡️ *Assurance sur devis* : {assurance_icon}\n\n"
        "_Tapez sur un champ pour le modifier._"
    )
    await target.reply_text(text, parse_mode="Markdown", reply_markup=_profil_keyboard(profile))


async def cmd_profil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not client_exists(user_id):
        await update.message.reply_text("Profil non trouvé. Tapez /start pour créer votre profil.")
        return
    await _show_profil_menu(update.message, user_id)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /cancel — nettoie tout état utilisateur en cours."""
    context.user_data.clear()
    await update.message.reply_text(
        "✅ *Opération annulée*. Tous les états en cours ont été supprimés.\n\n"
        "Envoyez un *message vocal* pour créer un nouveau devis.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


# ── Admin : ajouter un client manuellement ───────────────────────────────────

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remet l'utilisateur à zéro — utile si bloqué dans l'onboarding."""
    context.user_data.clear()
    await update.message.reply_text(
        "🔄 *Remis à zéro.*\nTapez /start pour recommencer l'onboarding.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


async def cmd_add_client(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Commande réservée à l'administrateur.")
        return
    await update.message.reply_text(
        "Pour ajouter un client manuellement, éditez le fichier `data/clients.json`\n"
        "ou demandez à l'utilisateur de faire /start sur ce bot.",
        parse_mode="Markdown",
    )


# ── Point d'entrée ────────────────────────────────────────────────────────────

def main() -> None:
    missing = [k for k, v in {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN,
        "OPENAI_API_KEY":     OPENAI_API_KEY,
        "OPENROUTER_API_KEY": OPENROUTER_KEY,
    }.items() if not v]
    if missing:
        raise EnvironmentError(f"Variables manquantes dans .env : {', '.join(missing)}")

    # V3 Phase 0: initialize Sentry if SENTRY_DSN is set. Safe no-op otherwise.
    init_sentry()

    logger.info("Démarrage Souffl.AI V3 (Telegram adapter)…")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Partager les clients LLM avec les handlers d'onboarding
    app.bot_data["llm"]   = llm
    app.bot_data["model"] = OPENROUTER_MODEL

    # Onboarding (ConversationHandler — priorité haute)
    app.add_handler(build_onboarding_handler())

    # Commandes
    app.add_handler(CommandHandler("reset",      cmd_reset))
    app.add_handler(CommandHandler("cancel",     cmd_cancel))
    app.add_handler(CommandHandler("devis",      cmd_devis))
    app.add_handler(CommandHandler("facture",    cmd_facture))
    app.add_handler(CommandHandler("profil",     cmd_profil))
    app.add_handler(CommandHandler("add_client", cmd_add_client))

    # Vocal → devis
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Texte (email en attente, modification devis)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Boutons inline
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot V2 en écoute…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
