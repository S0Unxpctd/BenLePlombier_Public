"""
SOUFFL.AI — Gestion des profils artisans (PostgreSQL)
======================================================
Chaque artisan est identifié par son telegram_user_id.
Les profils sont stockés dans la table `clients` (PostgreSQL Railway).
"""

import json
from typing import Optional
from db import get_cursor


# ── CRUD ──────────────────────────────────────────────────────────────────────

def client_exists(user_id: int) -> bool:
    with get_cursor() as cur:
        cur.execute("SELECT 1 FROM clients WHERE user_id = %s", (user_id,))
        return cur.fetchone() is not None


def get_client(user_id: int) -> Optional[dict]:
    with get_cursor() as cur:
        cur.execute("SELECT profile FROM clients WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return dict(row["profile"]) if row else None


def save_client(user_id: int, profile: dict):
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO clients (user_id, profile, updated_at)
            VALUES (%s, %s::jsonb, NOW())
            ON CONFLICT (user_id) DO UPDATE
              SET profile    = EXCLUDED.profile,
                  updated_at = NOW()
            """,
            (user_id, json.dumps(profile, ensure_ascii=False)),
        )


def delete_client(user_id: int):
    with get_cursor() as cur:
        cur.execute("DELETE FROM clients WHERE user_id = %s", (user_id,))


def list_clients() -> dict:
    """Retourne tous les profils (usage admin). {user_id: profile}"""
    with get_cursor() as cur:
        cur.execute("SELECT user_id, profile FROM clients")
        return {str(row["user_id"]): dict(row["profile"]) for row in cur.fetchall()}


# ── Profil vide (template) ────────────────────────────────────────────────────

def empty_profile() -> dict:
    return {
        "raison_sociale":        "",
        "dirigeant":             "",
        "adresse":               "",
        "telephone":             "",
        "email":                 "",
        "siret":                 "",
        "tva_intracom":          "Non assujetti à la TVA",
        "rcs_rm":                "[À COMPLÉTER]",
        "assurance_decennale":   "",
        "numero_police":         "",
        "afficher_assurance":    False,  # Afficher le bloc assurance décennale sur les devis
        "iban":                  "[À COMPLÉTER]",
        # Tarifs perso (None = utiliser les defaults du system_prompt)
        "journee_standard_ht":   None,
        "journee_specialise_ht": None,
        "deplacement_par_jour":  None,
        # Gmail pour l'envoi des devis
        "gmail_address":         "",
        "gmail_app_password":    "",
    }


# ── Injection dans le prompt système ─────────────────────────────────────────

def inject_profile_in_prompt(system_prompt: str, profile: dict) -> str:
    """
    Remplace les placeholders [NOM_ENTREPRISE], [SIRET], etc.
    par les vraies valeurs du profil de l'artisan.
    """
    replacements = {
        "[NOM_ENTREPRISE]":           profile.get("raison_sociale", "[À COMPLÉTER]"),
        "[FORME_JURIDIQUE]":          profile.get("forme_juridique", "[À COMPLÉTER]"),
        # Pour les EI/auto-entrepreneurs, la raison sociale est souvent le nom du dirigeant
        "[NOM_PRENOM_DIRIGEANT]":     profile.get("dirigeant") or profile.get("raison_sociale", "[À COMPLÉTER]"),
        "[ADRESSE_COMPLETE]":         profile.get("adresse", "[À COMPLÉTER]"),
        "[TELEPHONE]":                profile.get("telephone", "[À COMPLÉTER]"),
        "[EMAIL]":                    profile.get("email", "[À COMPLÉTER]"),
        "[SITE_WEB]":                 profile.get("site_web", ""),
        "[NUMERO_SIRET]":             profile.get("siret", "[À COMPLÉTER]"),
        "[NUMERO_TVA_INTRACOM]":      profile.get("tva_intracom", "Non assujetti à la TVA"),
        "[NUMERO_RCS_OU_RM]":         profile.get("rcs_rm", "[À COMPLÉTER]"),
        "[CODE_APE]":                 profile.get("code_ape", "4322A"),
        "[NOM_ASSUREUR]":             profile.get("assurance_decennale", "[À COMPLÉTER]"),
        "[NUMERO_POLICE_ASSURANCE]":  profile.get("numero_police", "[À COMPLÉTER]"),
        "[ANNEE_VALIDITE_ASSURANCE]": profile.get("annee_assurance", "[À COMPLÉTER]"),
    }

    # Tarifs perso
    if profile.get("journee_standard_ht"):
        replacements["350,00 € HT / jour"] = f"{profile['journee_standard_ht']:.2f} € HT / jour"
    if profile.get("journee_specialise_ht"):
        replacements["450,00 € HT / jour"] = f"{profile['journee_specialise_ht']:.2f} € HT / jour"
    if profile.get("deplacement_par_jour"):
        replacements["40,00 € HT / jour de présence"] = f"{profile['deplacement_par_jour']:.2f} € HT / jour de présence"

    result = system_prompt
    for placeholder, value in replacements.items():
        if value:  # Ne remplace que si la valeur est non vide
            result = result.replace(placeholder, value)

    return result
