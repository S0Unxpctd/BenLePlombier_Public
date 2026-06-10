"""Souffl.AI V3 — Quote edit flow (transport-agnostic).

Extracted from bot.py's `_apply_patch` + the LLM "modification" call.

The edit pipeline:
  1. Caller has an existing devis + a free-text modification request.
  2. If the request signals TVA=0, skip the LLM (deterministic path).
  3. Otherwise call the LLM with a constrained patch schema; parse; apply.
  4. Recompute totals via calculs.py.
  5. Re-run the same post-processing as quote_flow: enrich_artisan, empty-line
     pruning, flag filtering.

Patch schema supported (mirrors bot.py):
  prix_unitaire / quantite / description / detail_ligne / unite_ligne
  taux_tva_global / taux_tva_ligne / acompte_pourcentage
  delai_realisation / reference_chantier / modalites_paiement
  client / ajouter_ligne / supprimer_ligne

Public API
----------
- apply_patch(devis, modifications)       — pure function; applies a list of patches.
- edit_quote(devis, modifications_text, profile, llm_client, model) — end-to-end.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Optional

from . import quote_flow as _quote

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constrained patch schema
# ─────────────────────────────────────────────────────────────────────────────


EDIT_SYSTEM_PROMPT = (
    "Tu es un assistant expert en devis du bâtiment.\n"
    "Ta mission : identifier les modifications à apporter à un devis JSON existant.\n\n"
    "Retourne UNIQUEMENT un patch JSON minimal (jamais le devis entier).\n\n"
    "═══ RÈGLES FONDAMENTALES ═══\n\n"
    "1. NE FUSIONNE JAMAIS plusieurs postes en un seul.\n"
    "   Les lignes existantes doivent rester INTACTES sauf si la modification les concerne directement.\n\n"
    "2. Pour AJOUTER du contenu (fournitures, nouvelle prestation, poste supplémentaire),\n"
    "   utilise TOUJOURS le type \"ajouter_ligne\". Ne modifie PAS la description d'une ligne existante.\n\n"
    "3. Si l'artisan donne un montant TTC, convertis en HT en divisant par (1 + taux_tva).\n"
    "   Exemple : 3500€ TTC à 10%% → prix_unitaire_ht = 3500 / 1.10 = 3181.82\n\n"
    "═══ TYPES DISPONIBLES ═══\n\n"
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
    "═══ EXEMPLES CONCRETS ═══\n\n"
    'Demande : "Ajoute des fournitures: Total fourniture 3500€ TTC"\n'
    'Réponse : {"modifications": [{"type": "ajouter_ligne", "ligne": {"description": "Fournitures", "quantite": 1.0, "prix_unitaire_ht": 3181.82, "taux_tva": 0.10}}]}\n\n'
    'Demande : "Ajoute un robinet mitigeur à 250€ HT"\n'
    'Réponse : {"modifications": [{"type": "ajouter_ligne", "ligne": {"description": "Fourniture — Robinet mitigeur", "quantite": 1.0, "prix_unitaire_ht": 250.0, "taux_tva": 0.10}}]}\n\n'
    'Demande : "Change le prix de la main d\'oeuvre à 2000€"\n'
    'Réponse : {"modifications": [{"type": "prix_unitaire", "poste_index": 0, "nouvelle_valeur": 2000.0}]}\n\n'
    'Demande : "Ajoute le déplacement à 80€ HT et les fournitures à 1500€ TTC"\n'
    'Réponse : {"modifications": [\n'
    '  {"type": "ajouter_ligne", "ligne": {"description": "Déplacement", "quantite": 1.0, "prix_unitaire_ht": 80.0, "taux_tva": 0.10}},\n'
    '  {"type": "ajouter_ligne", "ligne": {"description": "Fournitures", "quantite": 1.0, "prix_unitaire_ht": 1363.64, "taux_tva": 0.10}}\n'
    ']}\n\n'
    "═══ RAPPELS ═══\n\n"
    "Taux TVA valides : 0.0 / 0.10 / 0.055 / 0.20\n"
    "poste_index : position dans lignes[] à partir de 0\n"
    "NE recalcule PAS les totaux — Python s'en charge.\n"
    "Identifie UNIQUEMENT les modifications demandées.\n"
    "NE TOUCHE PAS aux lignes existantes sauf si la modification les concerne explicitement.\n"
    "Retourne un JSON valide : {\"modifications\": [...]}"
)


# ─────────────────────────────────────────────────────────────────────────────
# Patch application (pure)
# ─────────────────────────────────────────────────────────────────────────────


def apply_patch(devis: dict, modifications: list) -> dict:
    """Apply a list of patch dicts to a devis. Returns a new dict; original is
    not mutated. Totals are NOT recomputed here — call calculs.recalculer_totaux
    afterwards.
    """
    d = copy.deepcopy(devis)

    for mod in modifications or []:
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
                nouvelle_ligne["taux_tva"] = float(nouvelle_ligne.get("taux_tva", 0.10))
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
            idx = mod.get("poste_index", -1)
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
            logger.warning("[edit_flow] Unknown patch type: %r — %r", mod_type, mod)

    return d


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end edit
# ─────────────────────────────────────────────────────────────────────────────


def edit_quote(
    devis: dict,
    modifications_text: str,
    profile: dict,
    llm_client,
    model: str,
) -> dict:
    """End-to-end edit: apply text-described modifications to an existing devis.

    - If the text triggers `detect_tva_zero`, apply TVA=0 deterministically
      (no LLM call).
    - Otherwise, request a patch from the LLM, parse it, apply it, recompute.
    - Preserves `meta.numero_devis` across the edit.
    - Runs the same post-processing as generation: enrich_artisan, prune empty
      lines, filter flags.
    """
    from calculs import recalculer_totaux

    numero = devis.get("meta", {}).get("numero_devis", "")

    # Fast path: TVA=0 wipe
    if _quote.detect_tva_zero(modifications_text):
        new_devis = _quote.apply_tva_zero(devis)
        if numero:
            new_devis.setdefault("meta", {})["numero_devis"] = numero
        new_devis = _quote.enrich_artisan(new_devis, profile)
        new_devis = _quote.remove_empty_lines(new_devis)
        new_devis = _quote.filter_flags(new_devis, profile)
        return new_devis

    # LLM patch path
    devis_json = json.dumps(devis, ensure_ascii=False)
    user_msg = (
        f"Devis JSON existant :\n{devis_json}\n\n"
        f"Modifications demandées :\n{modifications_text}\n\n"
        "Retourne le JSON patch."
    )
    resp = llm_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": EDIT_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.1,
        max_tokens=1024,
    )
    raw = resp.choices[0].message.content
    logger.info("[edit_flow] Patch preview: %s", str(raw)[:500])
    try:
        patch = _quote.parse_llm_json(raw)
    except (ValueError, json.JSONDecodeError):
        patch = {"modifications": []}

    new_devis = apply_patch(devis, patch.get("modifications", []))
    new_devis = recalculer_totaux(new_devis)
    if numero:
        new_devis.setdefault("meta", {})["numero_devis"] = numero
    new_devis = _quote.enrich_artisan(new_devis, profile)
    new_devis = _quote.remove_empty_lines(new_devis)
    new_devis = _quote.filter_flags(new_devis, profile)
    return new_devis
