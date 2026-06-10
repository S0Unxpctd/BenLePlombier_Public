"""
SOUFFL.AI — Calculs déterministes Python
=========================================
Fonctions de recalcul TVA et montants, centralisées et testables.
Utilisées par : bot.py, pdf_generator.py, facture_generator.py
"""

import copy


def _to_float(val, default: float = 0.0) -> float:
    """
    Conversion sécurisée vers float.
    Retourne `default` si val est None, une chaîne non numérique ('[À COMPLÉTER]', etc.)
    ou tout autre type non convertible.
    """
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def recalculer_totaux(devis: dict) -> dict:
    """
    Recalcule de façon déterministe TOUS les totaux et montants d'un devis.

    Formules :
    1. Pour chaque ligne : montant_ht = round(quantite × prix_unitaire_ht, 2)
    2. sous_total_ht = sum(montant_ht de toutes les lignes)
    3. montant_tva_10 = round(sum(montant_ht où taux_tva=0.10) × 0.10, 2)
    4. montant_tva_55 = round(sum(montant_ht où taux_tva=0.055) × 0.055, 2)
    5. montant_tva_20 = round(sum(montant_ht où taux_tva=0.20) × 0.20, 2)
    6. total_ttc = sous_total_ht + montant_tva_10 + montant_tva_55 + montant_tva_20
    7. acompte_ttc = round(total_ttc × (acompte_pourcentage / 100), 2)
    8. solde_ttc = round(total_ttc - acompte_ttc, 2)
    """
    d = copy.deepcopy(devis)

    # 1. Recalcul montant_ht pour chaque ligne
    for ligne in d.get("lignes", []):
        quantite = _to_float(ligne.get("quantite", 0))
        prix_ht  = _to_float(ligne.get("prix_unitaire_ht", 0))
        ligne["montant_ht"] = round(quantite * prix_ht, 2)

    # 2. Sous-total HT
    sous_total_ht = round(
        sum(_to_float(l.get("montant_ht", 0)) for l in d.get("lignes", [])), 2
    )

    # 3. TVA par taux
    def _tva(taux):
        return round(
            sum(
                _to_float(l.get("montant_ht", 0)) * taux
                for l in d.get("lignes", [])
                if abs(_to_float(l.get("taux_tva", 0)) - taux) < 1e-9
            ), 2
        )

    montant_tva_10 = _tva(0.10)
    montant_tva_55 = _tva(0.055)
    montant_tva_20 = _tva(0.20)

    # 4. Total TTC
    total_ttc = round(sous_total_ht + montant_tva_10 + montant_tva_55 + montant_tva_20, 2)

    # 5. Totaux
    d.setdefault("totaux", {})
    d["totaux"]["sous_total_ht"]  = sous_total_ht
    d["totaux"]["montant_tva_10"] = montant_tva_10
    d["totaux"]["montant_tva_55"] = montant_tva_55
    d["totaux"]["montant_tva_20"] = montant_tva_20
    d["totaux"]["total_ttc"]      = total_ttc

    # 6. Acompte / solde
    acompte_pct = d.get("conditions", {}).get("acompte_pourcentage", 30)
    acompte_ttc = round(total_ttc * acompte_pct / 100, 2)
    solde_ttc   = round(total_ttc - acompte_ttc, 2)

    d.setdefault("conditions", {})
    d["conditions"]["acompte_montant_ttc"] = acompte_ttc
    d["conditions"]["solde_montant_ttc"]   = solde_ttc

    return d


def appliquer_taux_tva_uniforme(devis: dict, taux: float) -> dict:
    """
    Applique un taux de TVA uniforme à toutes les lignes du devis,
    puis recalcule les totaux via recalculer_totaux().

    taux : 0.0, 0.10, 0.055, 0.20

    Si taux == 0.0 :
      - artisan["tva_intracom"] = "Non assujetti à la TVA — Art. 293 B du CGI"
      - mentions_legales["tva_note"] = même texte
    """
    d = copy.deepcopy(devis)

    for ligne in d.get("lignes", []):
        ligne["taux_tva"] = float(taux)

    if taux == 0.0:
        d.setdefault("artisan", {})
        d["artisan"]["tva_intracom"] = "Non assujetti à la TVA — Art. 293 B du CGI"
        d.setdefault("mentions_legales", {})
        d["mentions_legales"]["tva_note"] = "Non assujetti à la TVA — Art. 293 B du CGI"

    return recalculer_totaux(d)
