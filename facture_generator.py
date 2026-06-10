"""
SOUFFL.AI — Conversion Devis → Facture
=======================================
Prend un dict JSON devis et génère un dict JSON facture,
puis le PDF correspondant.

Différences devis / facture :
  - Titre DEVIS → FACTURE
  - Numéro DEVIS-... → FAC-...
  - Date validité → Date d'échéance (30 jours)
  - Bloc "bon pour accord" → Coordonnées bancaires (IBAN)
  - Mention "acquittée" si facture payée
"""

import copy
import os
from datetime import datetime, timedelta
from pathlib import Path

from jinja2 import Environment, BaseLoader
from weasyprint import HTML


# ── Template HTML Facture ──────────────────────────────────────────────────────

FACTURE_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
    font-size: 9pt;
    color: #1a1a1a;
    background: #fff;
  }
  @page {
    size: A4;
    margin: 15mm 15mm 20mm 15mm;
    @bottom-center {
      content: "Facture " attr(data-numero) " — Page " counter(page) " / " counter(pages);
      font-size: 7pt;
      color: #888;
    }
  }
  .header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    padding-bottom: 10mm;
    border-bottom: 2px solid #166534;
    margin-bottom: 7mm;
  }
  .header-artisan h1 { font-size: 16pt; font-weight: 800; color: #166534; }
  .header-artisan .subtitle { font-size: 9pt; color: #555; margin-top: 2px; }
  .header-artisan .contact-info { margin-top: 6px; font-size: 8pt; color: #333; line-height: 1.6; }
  .header-facture { text-align: right; }
  .header-facture .facture-label { font-size: 22pt; font-weight: 800; color: #166534; line-height: 1; }
  .header-facture .facture-numero { font-size: 10pt; font-weight: 600; color: #333; margin-top: 4px; }
  .header-facture .dates { margin-top: 8px; font-size: 8pt; color: #555; line-height: 1.7; }

  .info-block { display: flex; gap: 10mm; margin-bottom: 7mm; }
  .info-box { flex: 1; padding: 5mm; border: 1px solid #e0e0e0; border-radius: 4px; background: #f8f9fc; }
  .info-box h3 { font-size: 8pt; font-weight: 700; text-transform: uppercase; color: #166534; margin-bottom: 4px; border-bottom: 1px solid #dde3f0; padding-bottom: 3px; }
  .info-box p { font-size: 9pt; line-height: 1.6; color: #333; }

  .objet-block { padding: 4mm 5mm; background: #dcfce7; border-left: 4px solid #166534; border-radius: 0 4px 4px 0; margin-bottom: 7mm; font-size: 9pt; }

  table { width: 100%; border-collapse: collapse; margin-bottom: 5mm; }
  thead tr { background: #166534; color: #fff; }
  thead th { padding: 4px 6px; font-size: 8pt; font-weight: 600; text-transform: uppercase; text-align: left; }
  thead th.right { text-align: right; }
  thead th.center { text-align: center; }
  tbody tr:nth-child(even) { background: #f0fdf4; }
  tbody tr:nth-child(odd)  { background: #ffffff; }
  tbody td { padding: 5px 6px; font-size: 8.5pt; vertical-align: top; border-bottom: 1px solid #e8e8e8; }
  .td-poste   { width: 6%;  font-weight: 700; color: #166534; text-align: center; }
  .td-desc    { width: 38%; }
  .td-unite   { width: 8%;  text-align: center; }
  .td-qte     { width: 8%;  text-align: right; }
  .td-pu      { width: 13%; text-align: right; }
  .td-tva     { width: 8%;  text-align: center; }
  .td-montant { width: 13%; text-align: right; font-weight: 600; }

  .totaux-container { display: flex; justify-content: flex-end; margin-bottom: 7mm; }
  .totaux-table { width: 55mm; }
  .totaux-table td { padding: 3px 5px; font-size: 9pt; border: none; }
  .totaux-table .label { color: #555; }
  .totaux-table .montant { text-align: right; font-weight: 600; }
  .totaux-table .total-ttc { background: #166534; color: #fff; font-weight: 800; font-size: 11pt; border-radius: 3px; }
  .totaux-table .total-ttc .label { color: #fff; font-weight: 800; }
  .totaux-separator td { border-top: 1px solid #ccc; padding-top: 4px; }

  .paiement-box { padding: 5mm; background: #f0fdf4; border: 1px solid #86efac; border-radius: 4px; margin-bottom: 7mm; }
  .paiement-box h4 { font-size: 8.5pt; font-weight: 700; color: #166534; margin-bottom: 5px; }
  .paiement-box p { font-size: 9pt; line-height: 1.7; }
  .echeance-badge { display: inline-block; background: #fef08a; color: #713f12; font-weight: 700; font-size: 10pt; padding: 3px 8px; border-radius: 4px; margin-top: 4px; }

  {% if acquittee %}
  .acquittee-stamp {
    position: fixed;
    top: 60mm;
    right: 20mm;
    border: 4px solid #16a34a;
    color: #16a34a;
    font-size: 28pt;
    font-weight: 900;
    padding: 4px 12px;
    border-radius: 6px;
    transform: rotate(-15deg);
    opacity: 0.35;
    text-transform: uppercase;
  }
  {% endif %}

  /* Récap facturation partielle (acompte / situation / solde) */
  .facturation-recap { margin-bottom: 7mm; padding: 5mm; background: #f0fdf4; border: 1px solid #86efac; border-radius: 4px; }
  .facturation-recap h4 { font-size: 9pt; font-weight: 700; text-transform: uppercase; color: #166534; margin-bottom: 5px; border-bottom: 1px solid #86efac; padding-bottom: 3px; }
  .recap-table { width: 100%; border-collapse: collapse; }
  .recap-table td { padding: 3px 5px; font-size: 9pt; border: none; }
  .recap-table .montant { text-align: right; font-weight: 600; }
  .recap-total-devis td { color: #555; }
  .recap-precedent td { color: #888; font-size: 8.5pt; }
  .recap-separator td { border-top: 1px solid #ccc; padding-top: 4px; }
  .recap-net-payer { background: #166534; color: #fff !important; border-radius: 3px; font-weight: 800; font-size: 11pt; }
  .recap-net-payer td { color: #fff !important; font-weight: 800; }
  .recap-reste td { color: #555; font-style: italic; font-size: 8.5pt; }

  .legal { border-top: 1px solid #ddd; padding-top: 5mm; margin-top: 5mm; }
  .legal h4 { font-size: 8pt; font-weight: 700; text-transform: uppercase; color: #444; margin-bottom: 4px; }
  .legal p { font-size: 7.5pt; line-height: 1.6; color: #555; margin-bottom: 3px; }
</style>
</head>
<body data-numero="{{ meta.numero_facture }}">

{% if acquittee %}
<div class="acquittee-stamp">ACQUITTÉE</div>
{% endif %}

<!-- EN-TÊTE -->
<div class="header">
  <div class="header-artisan">
    <h1>{{ artisan.raison_sociale }}</h1>
    <div class="subtitle">Plombier — Chauffagiste</div>
    <div class="contact-info">
      {{ artisan.adresse }}<br>
      Tél. {{ artisan.telephone }}&nbsp;&nbsp;·&nbsp;&nbsp;{{ artisan.email }}<br>
      SIRET&nbsp;: {{ artisan.siret }}&nbsp;&nbsp;·&nbsp;&nbsp;{{ artisan.rcs_rm }}
    </div>
  </div>
  <div class="header-facture">
    <div class="facture-label">FACTURE</div>
    <div class="facture-numero">{{ meta.numero_facture }}</div>
    <div class="dates">
      Date d'émission&nbsp;: <strong>{{ meta.date_emission }}</strong><br>
      Date d'exécution&nbsp;: <strong>{{ meta.date_execution }}</strong><br>
      Échéance&nbsp;: <strong>{{ meta.date_echeance }}</strong>
    </div>
  </div>
</div>

<!-- CLIENT -->
<div class="info-block">
  <div class="info-box">
    <h3>Client</h3>
    <p>
      <strong>{{ client.nom }}</strong><br>
      {% if client.adresse_chantier %}{{ client.adresse_chantier }}<br>{% endif %}
      {% if client.telephone %}Tél. {{ client.telephone }}{% endif %}
    </p>
  </div>
  <div class="info-box">
    <h3>Référence devis d'origine</h3>
    <p>{{ meta.numero_devis_origine }}<br>Du {{ meta.date_devis_origine }}</p>
  </div>
</div>

<!-- OBJET -->
<div class="objet-block">
  <strong>Objet&nbsp;:</strong> {{ meta.reference_chantier }}
</div>

<!-- TABLEAU -->
<table>
  <thead>
    <tr>
      <th class="center">N°</th>
      <th>Désignation</th>
      <th class="center">Unité</th>
      <th class="right">Qté</th>
      <th class="right">P.U. HT</th>
      <th class="center">TVA</th>
      <th class="right">Montant HT</th>
    </tr>
  </thead>
  <tbody>
    {% for ligne in lignes %}
    <tr>
      <td class="td-poste">{{ ligne.poste }}</td>
      <td class="td-desc">{{ ligne.description }}</td>
      <td class="td-unite">{{ ligne.unite }}</td>
      <td class="td-qte">{{ "%.2f"|format(ligne.quantite) }}</td>
      <td class="td-pu">{{ "%.2f"|format(ligne.prix_unitaire_ht) }}&nbsp;€</td>
      <td class="td-tva">{{ (ligne.taux_tva * 100)|int }}&nbsp;%</td>
      <td class="td-montant">{{ "%.2f"|format(ligne.montant_ht) }}&nbsp;€</td>
    </tr>
    {% endfor %}
  </tbody>
</table>

<!-- TOTAUX -->
<div class="totaux-container">
  <table class="totaux-table">
    <tr><td class="label">Sous-total HT</td><td class="montant">{{ "%.2f"|format(totaux.sous_total_ht) }}&nbsp;€</td></tr>
    {% if totaux.montant_tva_10 > 0 %}
    <tr><td class="label">TVA 10 %</td><td class="montant">{{ "%.2f"|format(totaux.montant_tva_10) }}&nbsp;€</td></tr>
    {% endif %}
    {% if totaux.montant_tva_55 > 0 %}
    <tr><td class="label">TVA 5,5 %</td><td class="montant">{{ "%.2f"|format(totaux.montant_tva_55) }}&nbsp;€</td></tr>
    {% endif %}
    {% if totaux.montant_tva_20 > 0 %}
    <tr><td class="label">TVA 20 %</td><td class="montant">{{ "%.2f"|format(totaux.montant_tva_20) }}&nbsp;€</td></tr>
    {% endif %}
    <tr class="totaux-separator"><td colspan="2"></td></tr>
    <tr class="total-ttc"><td class="label">TOTAL TTC</td><td class="montant">{{ "%.2f"|format(totaux.total_ttc) }}&nbsp;€</td></tr>
  </table>
</div>

{% if facturation %}
<!-- RÉCAPITULATIF DE FACTURATION (acompte / situation / solde) -->
<div class="facturation-recap">
  <h4>{{ facturation.titre_section }}</h4>
  <table class="recap-table">
    <tr class="recap-total-devis">
      <td>Montant total du devis TTC</td>
      <td class="montant">{{ "%.2f"|format(totaux.total_ttc) }}&nbsp;€</td>
    </tr>
    {% for p in facturation.historique %}
    <tr class="recap-precedent">
      <td>{{ p.label }}</td>
      <td class="montant">− {{ "%.2f"|format(p.montant_ttc) }}&nbsp;€</td>
    </tr>
    {% endfor %}
    <tr class="recap-separator"><td colspan="2"></td></tr>
    <tr class="recap-net-payer">
      <td><strong>NET À PAYER — {{ facturation.label }}</strong></td>
      <td class="montant"><strong>{{ "%.2f"|format(facturation.montant_cette_facture_ttc) }}&nbsp;€</strong></td>
    </tr>
    {% if facturation.reste_apres_ttc > 0.01 %}
    <tr class="recap-reste">
      <td>Solde restant après cette facture</td>
      <td class="montant">{{ "%.2f"|format(facturation.reste_apres_ttc) }}&nbsp;€</td>
    </tr>
    {% endif %}
  </table>
</div>
{% endif %}

<!-- PAIEMENT -->
<div class="paiement-box">
  <h4>Modalités de paiement</h4>
  <p>Virement bancaire à l'ordre de <strong>{{ artisan.raison_sociale }}</strong></p>
  <p>IBAN&nbsp;: <strong>{{ artisan.iban }}</strong></p>
  <p>{{ conditions.modalites_paiement }}</p>
  <div class="echeance-badge">
    {% if facturation %}
      {{ "%.2f"|format(facturation.montant_cette_facture_ttc) }}&nbsp;€ à régler avant le {{ meta.date_echeance }}
    {% else %}
      À régler avant le {{ meta.date_echeance }}
    {% endif %}
  </div>
</div>

<!-- MENTIONS LÉGALES -->
<div class="legal">
  <h4>Mentions légales</h4>
  <p><strong>Pénalités de retard :</strong> {{ conditions.penalites_retard }}. Indemnité forfaitaire de recouvrement&nbsp;: 40&nbsp;€ (art. D.441-5 Code de commerce).</p>
  <p><strong>Garanties :</strong> {{ mentions_legales.garantie }}</p>
  <p><strong>TVA :</strong> {{ mentions_legales.tva_note }}</p>
</div>

</body>
</html>
"""


# ── Conversion devis dict → facture dict ─────────────────────────────────────

def devis_to_facture(devis: dict, date_execution: str = None) -> dict:
    """
    Transforme un dict JSON devis en dict JSON facture.
    date_execution : "JJ/MM/AAAA" — si None, utilise la date du jour.
    """
    facture = copy.deepcopy(devis)

    today = datetime.now()
    date_exec = date_execution or today.strftime("%d/%m/%Y")
    date_echeance = (today + timedelta(days=30)).strftime("%d/%m/%Y")

    numero_devis   = devis.get("meta", {}).get("numero_devis", "DEVIS")
    date_devis     = devis.get("meta", {}).get("date_emission", "—")
    numero_facture = numero_devis.replace("DEVIS-", "FAC-")

    facture["meta"]["numero_facture"]      = numero_facture
    facture["meta"]["date_execution"]      = date_exec
    facture["meta"]["date_echeance"]       = date_echeance
    facture["meta"]["numero_devis_origine"] = numero_devis
    facture["meta"]["date_devis_origine"]  = date_devis

    # Supprimer les champs spécifiques au devis
    facture["meta"].pop("date_validite", None)

    # Ajouter IBAN depuis le profil artisan si disponible
    artisan = facture.get("artisan", {})
    if not artisan.get("iban"):
        artisan["iban"] = "[À COMPLÉTER — IBAN requis pour le paiement]"
    facture["artisan"] = artisan

    return facture


def devis_to_facture_typed(
    devis: dict,
    type_facture: str,          # "acompte" | "intermediaire" | "solde"
    montant_ttc: float,         # montant TTC de CETTE facture
    deja_facture_ttc: float = 0.0,
    factures_precedentes: list = None,
) -> dict:
    """
    Crée un dict facture partielle depuis un devis.

    type_facture :
      - "acompte"       → premier versement (ex : 30 %)
      - "intermediaire" → situation de travaux en cours de chantier
      - "solde"         → règlement final, avec récap de tout ce qui a été versé

    montant_ttc         : montant TTC À PERCEVOIR pour CETTE facture
    deja_facture_ttc    : cumul TTC déjà facturé avant celle-ci
    factures_precedentes: liste des dicts factures précédentes (pour le récap PDF)
    """
    facture = copy.deepcopy(devis)
    factures_precedentes = factures_precedentes or []

    today         = datetime.now()
    date_echeance = (today + timedelta(days=30)).strftime("%d/%m/%Y")

    numero_devis = devis.get("meta", {}).get("numero_devis", "DEVIS")
    date_devis   = devis.get("meta", {}).get("date_emission", "—")
    total_ttc    = devis.get("totaux", {}).get("total_ttc", 0)

    # Compteur des situations intermédiaires déjà émises
    nb_inter = sum(
        1 for f in factures_precedentes
        if f.get("meta", {}).get("type_facture") == "intermediaire"
    )
    suffixes = {
        "acompte":       "ACOMPTE",
        "intermediaire": f"INTER-{nb_inter + 1:02d}",
        "solde":         "SOLDE",
    }
    numero_facture = numero_devis.replace("DEVIS-", "FAC-") + f"-{suffixes[type_facture]}"

    titres = {
        "acompte":       "Facture d'acompte",
        "intermediaire": "Situation de travaux",
        "solde":         "Décompte final — Solde",
    }
    pourcentage = round(montant_ttc / total_ttc * 100) if total_ttc else 0
    labels = {
        "acompte":       f"Acompte {pourcentage}%",
        "intermediaire": f"Situation {pourcentage}%",
        "solde":         "Solde — règlement final",
    }

    # Historique pour le récap PDF
    historique = []
    for fp in factures_precedentes:
        fp_meta = fp.get("meta", {})
        fp_fact = fp.get("facturation", {})
        historique.append({
            "label":       f"{fp_meta.get('numero_facture', '—')} — {fp_fact.get('label', '—')}",
            "montant_ttc": fp_fact.get("montant_cette_facture_ttc", 0),
        })

    facture["meta"]["numero_facture"]        = numero_facture
    facture["meta"]["type_facture"]          = type_facture
    facture["meta"]["date_emission"]         = today.strftime("%d/%m/%Y")
    facture["meta"]["date_execution"]        = today.strftime("%d/%m/%Y")
    facture["meta"]["date_echeance"]         = date_echeance
    facture["meta"]["numero_devis_origine"]  = numero_devis
    facture["meta"]["date_devis_origine"]    = date_devis
    facture["meta"].pop("date_validite", None)

    facture["facturation"] = {
        "type":                      type_facture,
        "titre_section":             titres[type_facture],
        "label":                     labels[type_facture],
        "montant_cette_facture_ttc": round(montant_ttc, 2),
        "deja_facture_ttc":          round(deja_facture_ttc, 2),
        "reste_apres_ttc":           round(total_ttc - deja_facture_ttc - montant_ttc, 2),
        "historique":                historique,
    }

    artisan = facture.get("artisan", {})
    if not artisan.get("iban"):
        artisan["iban"] = "[À COMPLÉTER — IBAN requis pour le paiement]"
    facture["artisan"] = artisan

    return facture


def generate_facture_pdf(facture: dict, output_path: str, acquittee: bool = False) -> str:
    """Génère le PDF de la facture."""

    # Recalcul centralisé et déterministe via calculs.py
    from calculs import recalculer_totaux
    facture = recalculer_totaux(facture)

    facture.setdefault("mentions_legales", {})
    facture["mentions_legales"].setdefault("tva_note", "TVA 10% appliquée — travaux de rénovation sur logement > 2 ans.")
    facture["mentions_legales"].setdefault("garantie", "Garantie décennale et biennale conformément aux articles 1792 et suivants du Code civil.")

    env  = Environment(loader=BaseLoader())
    tmpl = env.from_string(FACTURE_HTML_TEMPLATE)
    html = tmpl.render(**facture, acquittee=acquittee)

    HTML(string=html).write_pdf(output_path)
    return output_path
