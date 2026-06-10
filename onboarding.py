"""
SOUFFL.AI — Onboarding artisan via Telegram
============================================
Deux chemins :
  A) L'artisan envoie un fichier (PDF/Word/image d'un ancien devis)
     → Claude extrait les infos automatiquement → confirmation → questions manquantes
  B) Pas de fichier → questions une par une

États de la conversation :
  WAITING_TEMPLATE → ASK_NOM → ASK_SIRET → ASK_ADRESSE → ASK_TELEPHONE
  → ASK_EMAIL → ASK_ASSUREUR → ASK_POLICE → ASK_IBAN → ASK_TVA
  → ASK_TARIF → CONFIRM → DONE
"""

import json
import logging
import base64
import tempfile
import os
from pathlib import Path

from openai import OpenAI
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from client_store import save_client, empty_profile, client_exists

logger = logging.getLogger(__name__)

# ── États de conversation ──────────────────────────────────────────────────────
(
    WAITING_TEMPLATE,
    ASK_NOM,
    ASK_SIRET,
    ASK_ADRESSE,
    ASK_TELEPHONE,
    ASK_EMAIL,
    ASK_ASSUREUR,
    ASK_POLICE,
    ASK_AFFICHER_ASSURANCE,
    ASK_IBAN,
    ASK_TVA,
    ASK_TARIF,
    ASK_GMAIL,
    ASK_GMAIL_PASSWORD,
    CONFIRM,
) = range(15)

SKIP = "⏭️ Passer"
YES  = "✅ Oui"
NO   = "❌ Non"

_SKIP_WORDS = {"passer", "plus tard", "plustard", "skip", "non", "rien",
               "pas maintenant", "plus", "later", "n/a", "na", "—", "-"}

def is_skip(text: str) -> bool:
    return text.strip() == SKIP or text.strip().lower() in _SKIP_WORDS


# ── Extraction depuis un document (Claude vision) ─────────────────────────────

def extract_profile_from_file(file_path: str, file_type: str, llm: OpenAI, model: str) -> dict:
    """
    Envoie le fichier à Claude pour extraire les infos artisan.
    - Images (PNG, JPEG, WEBP) → vision base64
    - PDF → extraction texte via pypdf, envoi comme texte
    Retourne un dict partiel avec les champs trouvés.
    """
    prompt = (
        "Tu es un assistant qui extrait des informations depuis un document commercial (devis ou facture d'artisan).\n"
        "Extrais les champs suivants en JSON. Met null si le champ est absent.\n\n"
        "Champs à extraire :\n"
        "- raison_sociale : nom de l'entreprise ou nom+prénom de l'artisan\n"
        "- dirigeant : prénom et nom du dirigeant\n"
        "- adresse : adresse complète de l'entreprise\n"
        "- telephone : numéro de téléphone\n"
        "- email : adresse email\n"
        "- siret : numéro SIRET (14 chiffres)\n"
        "- tva_intracom : numéro TVA intracommunautaire ou 'Non assujetti à la TVA'\n"
        "- assurance_decennale : nom de l'assureur décennale\n"
        "- numero_police : numéro de police d'assurance\n"
        "- iban : IBAN (commence par FR, BE, etc.)\n\n"
        "Réponds UNIQUEMENT avec un objet JSON valide, rien d'autre."
    )

    # ── Chemin 1 : image (vision) ──────────────────────────────────────────────
    if file_type in ("image/png", "image/jpeg", "image/jpg", "image/webp"):
        with open(file_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{file_type};base64,{b64}"}},
            ]
        }]

    # ── Chemin 2 : PDF → extraction texte ─────────────────────────────────────
    else:
        text = _extract_text_from_pdf(file_path)
        if not text.strip():
            raise ValueError("Le PDF ne contient pas de texte extractible.")
        messages = [{
            "role": "user",
            "content": f"{prompt}\n\n--- Contenu du document ---\n{text[:4000]}"
        }]

    response = llm.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=512,
    )

    raw = response.choices[0].message.content
    if not raw:
        return {}
    raw = raw.strip()
    # Nettoyer les blocs markdown si présents
    if raw.startswith("```"):
        lines = raw.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    if not raw:
        return {}
    try:
        extracted = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {k: v for k, v in extracted.items() if v and v != "null"}


def _extract_text_from_pdf(file_path: str) -> str:
    """Extrait le texte brut d'un PDF via pypdf."""
    try:
        import pypdf
        reader = pypdf.PdfReader(file_path)
        pages_text = []
        for page in reader.pages[:5]:
            t = page.extract_text()
            if t:
                pages_text.append(t)
        return "\n".join(pages_text)
    except ImportError:
        raise ImportError("pypdf n'est pas installé. Lance : pip install pypdf")
    except Exception as e:
        raise RuntimeError(f"Impossible de lire le PDF : {e}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _profile_summary(profile: dict) -> str:
    lines = [
        f"🏢 *Entreprise* : {profile.get('raison_sociale', '—')}",
        f"🪪 *SIRET* : {profile.get('siret', '—')}",
        f"📍 *Adresse* : {profile.get('adresse', '—')}",
        f"📞 *Tél.* : {profile.get('telephone', '—')}",
        f"📧 *Email* : {profile.get('email', '—')}",
        f"🛡️ *Assureur* : {profile.get('assurance_decennale', '—')} — Police : {profile.get('numero_police', '—')}",
        f"🏦 *IBAN* : {profile.get('iban', '—')}",
        f"💶 *TVA* : {profile.get('tva_intracom', '—')}",
    ]
    if profile.get("journee_standard_ht"):
        lines.append(f"💰 *Tarif journée* : {profile['journee_standard_ht']} €/j")
    return "\n".join(lines)


# ── Handlers de l'onboarding ──────────────────────────────────────────────────

async def start_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id

    if client_exists(user_id):
        await update.message.reply_text(
            "👷 Vous êtes déjà inscrit !\nEnvoyez un *message vocal* pour générer un devis.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    context.user_data["profile"] = empty_profile()

    await update.message.reply_text(
        "👋 Bienvenue sur *Souffl.AI* !\n\n"
        "Je vais créer votre profil artisan en 2 minutes.\n\n"
        "📄 *Avez-vous un ancien devis ou une facture type ?*\n"
        "Si oui, envoyez-le moi (PDF, Word ou photo) et je pré-remplis tout automatiquement.\n"
        "Sinon, tapez *non* et on remplit ensemble.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[NO]], resize_keyboard=True, one_time_keyboard=True),
    )
    return WAITING_TEMPLATE


async def handle_template_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """L'artisan a envoyé un fichier — on l'analyse avec Claude."""
    llm   = context.bot_data["llm"]
    model = context.bot_data["model"]

    msg = await update.message.reply_text("🔍 Analyse du document en cours…")

    try:
        # Téléchargement du fichier
        doc = update.message.document or update.message.photo
        if update.message.photo:
            file_obj  = await context.bot.get_file(update.message.photo[-1].file_id)
            mime_type = "image/jpeg"
        else:
            file_obj  = await context.bot.get_file(doc.file_id)
            mime_type = doc.mime_type or "application/pdf"

        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file_obj.file_path).suffix) as tmp:
            tmp_path = tmp.name
        await file_obj.download_to_drive(tmp_path)

        # Extraction via Claude
        extracted = extract_profile_from_file(tmp_path, mime_type, llm, model)
        os.unlink(tmp_path)

        if not extracted:
            await msg.edit_text("😕 Je n'ai pas pu extraire les infos. On va les saisir manuellement.")
            return await _ask_nom(update, context)

        # Fusion avec le profil vide
        profile = context.user_data["profile"]
        profile.update(extracted)
        context.user_data["profile"] = profile

        summary = _profile_summary(profile)
        await msg.delete()
        await update.message.reply_text(
            f"✅ *Voici ce que j'ai trouvé :*\n\n{summary}\n\n"
            "C'est correct ? Je complèterai ce qui manque ensuite.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([[YES, NO]], resize_keyboard=True, one_time_keyboard=True),
        )
        return CONFIRM

    except Exception as e:
        logger.error(f"Erreur extraction template : {e}", exc_info=True)
        await msg.edit_text(
            f"⚠️ Impossible d'analyser le fichier automatiquement.\n`{str(e)[:200]}`\n\nOn va saisir manuellement.",
            parse_mode="Markdown",
        )
        return await _ask_nom(update, context)


async def handle_no_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _ask_nom(update, context)


async def _ask_nom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "✏️ *Nom de votre entreprise ?*\n_(ex : Plomberie Dupont, ou votre prénom + nom si auto-entrepreneur)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[SKIP]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ASK_NOM


async def handle_nom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    if not is_skip(val):
        context.user_data["profile"]["raison_sociale"] = val
    await update.message.reply_text(
        "✏️ *Votre numéro SIRET ?* (14 chiffres)\n_Vous le trouvez sur vos anciens devis ou sur papiers.fr_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[SKIP]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ASK_SIRET


async def handle_siret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip().replace(" ", "")
    if not is_skip(val):
        context.user_data["profile"]["siret"] = val
    await update.message.reply_text(
        "✏️ *Adresse complète de votre entreprise ?*\n_(numéro, rue, code postal, ville)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[SKIP]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ASK_ADRESSE


async def handle_adresse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    if not is_skip(val):
        context.user_data["profile"]["adresse"] = val
    await update.message.reply_text(
        "✏️ *Votre numéro de téléphone ?*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[SKIP]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ASK_TELEPHONE


async def handle_telephone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    if not is_skip(val):
        context.user_data["profile"]["telephone"] = val
    await update.message.reply_text(
        "✏️ *Votre email professionnel ?*\n_(ce sera aussi l'adresse d'envoi des devis)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[SKIP]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ASK_EMAIL


async def handle_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    if not is_skip(val):
        context.user_data["profile"]["email"] = val
    await update.message.reply_text(
        "✏️ *Votre assureur décennale ?*\n_(nom de la compagnie d'assurance — obligatoire sur les devis)_",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[SKIP]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ASK_ASSUREUR


async def handle_assureur(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    if not is_skip(val):
        context.user_data["profile"]["assurance_decennale"] = val
    await update.message.reply_text(
        "✏️ *Numéro de police d'assurance décennale ?*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[SKIP]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ASK_POLICE


async def handle_police(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    if not is_skip(val):
        context.user_data["profile"]["numero_police"] = val
    await update.message.reply_text(
        "🛡️ *Afficher le bloc assurance décennale sur vos devis ?*\n\n"
        "_Si vous n'avez pas encore vos infos d'assurance, vous pouvez masquer ce bloc "
        "pour l'instant — vous le réactiverez plus tard._",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[YES, NO]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ASK_AFFICHER_ASSURANCE


async def handle_afficher_assurance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    # YES = "✅ Oui", tout autre réponse (y compris NO / passer) = masquer
    context.user_data["profile"]["afficher_assurance"] = (val == YES)
    await update.message.reply_text(
        "✏️ *Votre IBAN ?*\n_(pour que vos clients sachent où virer le paiement sur les factures)_\n"
        "Tapez *passer* si vous n'avez pas ça sous la main.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[SKIP]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ASK_IBAN


async def handle_iban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    if not is_skip(val):
        context.user_data["profile"]["iban"] = val.replace(" ", "")
    await update.message.reply_text(
        "✏️ *Êtes-vous assujetti à la TVA ?*\n"
        "• *Non* → micro-entrepreneur, pas de TVA sur vos devis\n"
        "• *Oui* → entrez votre numéro TVA intracommunautaire (FR + 11 chiffres)",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["Non assujetti à la TVA"], [SKIP]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ASK_TVA


async def handle_tva(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    if not is_skip(val):
        context.user_data["profile"]["tva_intracom"] = val
    await update.message.reply_text(
        "✏️ *Votre tarif journée (en € HT) ?*\n"
        "Laissez vide pour utiliser le tarif standard (350 €/j).",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["350 (standard)", SKIP]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ASK_TARIF


async def handle_tarif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    if not is_skip(val) and val != "350 (standard)":
        try:
            context.user_data["profile"]["journee_standard_ht"] = float(val.replace(",", ".").replace("€", "").strip())
        except ValueError:
            pass
    await update.message.reply_text(
        "✉️ *Votre adresse Gmail pour envoyer les devis ?*\n"
        "_(Souffl.AI enverra les PDF depuis votre propre adresse)_\n"
        "Tapez *passer* pour configurer ça plus tard.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[SKIP]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ASK_GMAIL


async def handle_gmail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    if is_skip(val):
        return await _finalize_profile(update, context)
    context.user_data["profile"]["gmail_address"] = val
    await update.message.reply_text(
        "🔑 *Mot de passe d'application Gmail ?*\n\n"
        "👉 Comment l'obtenir (30 secondes) :\n"
        "1. myaccount.google.com\n"
        "2. Sécurité → Validation en 2 étapes\n"
        "3. Mots de passe d'application → Autre → Copier le code\n\n"
        "_Ce n'est pas votre vrai mot de passe — c'est un code à 16 caractères._",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[SKIP]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ASK_GMAIL_PASSWORD


async def handle_gmail_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    if not is_skip(val):
        context.user_data["profile"]["gmail_app_password"] = val.replace(" ", "")
    return await _finalize_profile(update, context)


async def _finalize_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    profile = context.user_data["profile"]
    summary = _profile_summary(profile)
    await update.message.reply_text(
        f"📋 *Récapitulatif de votre profil :*\n\n{summary}\n\n"
        "Tout est correct ?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[YES, "🔄 Recommencer"]], resize_keyboard=True, one_time_keyboard=True),
    )
    return CONFIRM


async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer = update.message.text.strip()

    if "Recommencer" in answer:
        context.user_data["profile"] = empty_profile()
        return await _ask_nom(update, context)

    user_id = update.effective_user.id
    save_client(user_id, context.user_data["profile"])

    await update.message.reply_text(
        "✅ *Profil enregistré !*\n\n"
        "Vous êtes prêt à utiliser Souffl.AI.\n"
        "🎙️ *Envoyez un message vocal* pour générer votre premier devis.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Onboarding annulé. Tapez /start pour recommencer.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── Construction du ConversationHandler ───────────────────────────────────────

def build_onboarding_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", start_onboarding)],
        states={
            WAITING_TEMPLATE: [
                MessageHandler(filters.Document.ALL | filters.PHOTO, handle_template_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_no_template),
            ],
            CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_confirm),
            ],
            ASK_NOM:          [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_nom)],
            ASK_SIRET:        [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_siret)],
            ASK_ADRESSE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_adresse)],
            ASK_TELEPHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telephone)],
            ASK_EMAIL:        [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_email)],
            ASK_ASSUREUR:            [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_assureur)],
            ASK_POLICE:              [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_police)],
            ASK_AFFICHER_ASSURANCE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_afficher_assurance)],
            ASK_IBAN:                [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_iban)],
            ASK_TVA:          [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tva)],
            ASK_TARIF:        [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tarif)],
            ASK_GMAIL:        [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_gmail)],
            ASK_GMAIL_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_gmail_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
