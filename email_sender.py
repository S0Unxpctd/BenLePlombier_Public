"""
SOUFFL.AI — Envoi email via Resend API
=======================================
Remplace l'ancien envoi SMTP Gmail (bloqué sur Railway)
par l'API Resend (appel HTTPS, aucun port SMTP nécessaire).
"""

import os
import base64
import resend


REMISE_EN_MAIN_PROPRE_WARNING = (
    "💡 Rappel professionnel : un devis remis en main propre au client "
    "a statistiquement plus de chances d'être accepté. "
    "Vous pouvez aussi l'imprimer et le présenter en direct pour répondre "
    "aux objections et signer sur place."
)

# ── Initialisation Resend ──
resend.api_key = os.getenv("RESEND_API_KEY", "")

# Adresse d'envoi par défaut (domaine de test Resend)
# Remplacer par "devis@ton-domaine.fr" quand tu auras vérifié un domaine
DEFAULT_FROM_EMAIL = "onboarding@resend.dev"


def send_devis_email(
    gmail_address: str,
    gmail_app_password: str,
    to_email: str,
    pdf_path: str,
    devis: dict,
) -> bool:
    """
    Envoie le PDF du devis par email via Resend.
    Garde la même signature pour ne pas casser le bot.
    (gmail_address et gmail_app_password sont ignorés mais gardés
     pour compatibilité avec les appels existants)
    """
    meta       = devis.get("meta", {})
    artisan    = devis.get("artisan", {})
    client     = devis.get("client", {})
    totaux     = devis.get("totaux", {})

    numero     = meta.get("numero_devis", "DEVIS")
    chantier   = meta.get("reference_chantier", "")
    nom_client = client.get("nom", "")
    total_ttc  = totaux.get("total_ttc", 0)
    validite   = meta.get("date_validite", "")
    nom_artisan = artisan.get("raison_sociale", "Souffl.AI")

    # ── Sujet ──
    subject = f"{numero} — Devis {chantier} — {nom_artisan}"

    # ── Corps du mail (HTML) ──
    body = f"""Bonjour{' ' + nom_client if nom_client and '[' not in nom_client else ''},
<br><br>
Veuillez trouver ci-joint votre devis concernant : {chantier}.
<br><br>
<strong>Récapitulatif :</strong><br>
• Numéro : {numero}<br>
• Total TTC : {total_ttc:,.2f} €<br>
• Valable jusqu'au : {validite}
<br><br>
N'hésitez pas à me contacter pour toute question.
<br><br>
Cordialement,<br>
{nom_artisan}<br>
{artisan.get('telephone', '')}<br>
{artisan.get('email', '')}
"""

    # ── Pièce jointe PDF ──
    filename = os.path.basename(pdf_path)
    with open(pdf_path, "rb") as f:
        pdf_content = base64.b64encode(f.read()).decode("utf-8")

    # ── Reply-To = email réel de l'artisan ──
    reply_to_email = artisan.get("email", "") or gmail_address

    # ── Envoi via Resend API ──
    params: resend.Emails.SendParams = {
        "from": f"{nom_artisan} <{DEFAULT_FROM_EMAIL}>",
        "to": [to_email],
        "reply_to": [reply_to_email],
        "subject": subject,
        "html": body,
        "attachments": [
            {
                "filename": filename,
                "content": pdf_content,
            }
        ],
    }

    resend.Emails.send(params)
    return True


def send_facture_email(
    gmail_address: str,
    gmail_app_password: str,
    to_email: str,
    pdf_path: str,
    facture: dict,
) -> bool:
    """Même logique que send_devis_email mais pour une facture, via Resend."""
    meta       = facture.get("meta", {})
    artisan    = facture.get("artisan", {})
    client     = facture.get("client", {})
    totaux     = facture.get("totaux", {})

    numero     = meta.get("numero_facture", "FAC")
    chantier   = meta.get("reference_chantier", "")
    nom_client = client.get("nom", "")
    total_ttc  = totaux.get("total_ttc", 0)
    echeance   = meta.get("date_echeance", "")
    nom_artisan = artisan.get("raison_sociale", "Souffl.AI")

    subject = f"{numero} — Facture {chantier} — {nom_artisan}"

    body = f"""Bonjour{' ' + nom_client if nom_client and '[' not in nom_client else ''},
<br><br>
Veuillez trouver ci-joint votre facture concernant : {chantier}.
<br><br>
<strong>Récapitulatif :</strong><br>
• Numéro : {numero}<br>
• Total TTC : {total_ttc:,.2f} €<br>
• À régler avant le : {echeance}<br>
• IBAN : {artisan.get('iban', '[À COMPLÉTER]')}
<br><br>
Cordialement,<br>
{nom_artisan}<br>
{artisan.get('telephone', '')}
"""

    # ── Reply-To = email réel de l'artisan ──
    reply_to_email = artisan.get("email", "") or gmail_address

    filename = os.path.basename(pdf_path)
    with open(pdf_path, "rb") as f:
        pdf_content = base64.b64encode(f.read()).decode("utf-8")

    params: resend.Emails.SendParams = {
        "from": f"{nom_artisan} <{DEFAULT_FROM_EMAIL}>",
        "to": [to_email],
        "reply_to": [reply_to_email],
        "subject": subject,
        "html": body,
        "attachments": [
            {
                "filename": filename,
                "content": pdf_content,
            }
        ],
    }

    resend.Emails.send(params)
    return True
