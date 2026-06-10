"""
SOUFFL.AI — Générateur PDF (WeasyPrint + Jinja2)
=================================================
Prend un dict JSON devis et génère un PDF professionnel
au format devis artisan français.
"""

import os
from datetime import datetime
from jinja2 import Environment, BaseLoader
from weasyprint import HTML, CSS

# ── Template HTML du devis ─────────────────────────────────────────────────────

DEVIS_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<style>
  /* ── Reset & Base ── */
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
    font-size: 9pt;
    color: #1a1a1a;
    background: #fff;
  }

  /* ── Page ── */
  @page {
    size: A4;
    margin: 15mm 15mm 20mm 15mm;
    @bottom-center {
      content: "Devis " attr(data-numero) " — Page " counter(page) " / " counter(pages);
      font-size: 7pt;
      color: #888;
    }
  }

  /* ── En-tête ── */
  .header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    padding-bottom: 10mm;
    border-bottom: 2px solid #1a56db;
    margin-bottom: 7mm;
  }

  .header-artisan h1 {
    font-size: 16pt;
    font-weight: 800;
    color: #1a56db;
    letter-spacing: -0.5px;
  }
  .header-artisan .subtitle {
    font-size: 9pt;
    color: #555;
    margin-top: 2px;
  }
  .header-artisan .contact-info {
    margin-top: 6px;
    font-size: 8pt;
    color: #333;
    line-height: 1.6;
  }

  .header-devis {
    text-align: right;
  }
  .header-devis .devis-label {
    font-size: 22pt;
    font-weight: 800;
    color: #1a56db;
    letter-spacing: -1px;
    line-height: 1;
  }
  .header-devis .devis-numero {
    font-size: 10pt;
    font-weight: 600;
    color: #333;
    margin-top: 4px;
  }
  .header-devis .dates {
    margin-top: 8px;
    font-size: 8pt;
    color: #555;
    line-height: 1.7;
  }

  /* ── Bloc Client & Chantier ── */
  .info-block {
    display: flex;
    gap: 10mm;
    margin-bottom: 7mm;
  }
  .info-box {
    flex: 1;
    padding: 5mm;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    background: #f8f9fc;
  }
  .info-box h3 {
    font-size: 8pt;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #1a56db;
    margin-bottom: 4px;
    border-bottom: 1px solid #dde3f0;
    padding-bottom: 3px;
  }
  .info-box p {
    font-size: 9pt;
    line-height: 1.6;
    color: #333;
  }

  /* ── Référence chantier ── */
  .objet-block {
    padding: 4mm 5mm;
    background: #eef2ff;
    border-left: 4px solid #1a56db;
    border-radius: 0 4px 4px 0;
    margin-bottom: 7mm;
    font-size: 9pt;
  }
  .objet-block strong { font-weight: 700; }

  /* ── Tableau des postes ── */
  table {
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 5mm;
  }
  thead tr {
    background: #1a56db;
    color: #fff;
  }
  thead th {
    padding: 4px 6px;
    font-size: 8pt;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    text-align: left;
  }
  thead th.right { text-align: right; }
  thead th.center { text-align: center; }

  tbody tr:nth-child(even) { background: #f4f7ff; }
  tbody tr:nth-child(odd)  { background: #ffffff; }

  tbody td {
    padding: 5px 6px;
    font-size: 8.5pt;
    vertical-align: top;
    border-bottom: 1px solid #e8e8e8;
  }
  .td-poste   { width: 6%;  font-weight: 700; color: #1a56db; text-align: center; }
  .td-desc    { width: 38%; }
  .td-desc .detail { font-size: 7.5pt; color: #666; margin-top: 2px; }
  .td-unite   { width: 8%;  text-align: center; }
  .td-qte     { width: 8%;  text-align: right; }
  .td-pu      { width: 13%; text-align: right; }
  .td-tva     { width: 8%;  text-align: center; }
  .td-montant { width: 13%; text-align: right; font-weight: 600; }

  .flag-inline {
    background: #fff3cd;
    color: #856404;
    font-size: 7.5pt;
    padding: 1px 4px;
    border-radius: 3px;
    font-weight: 600;
  }

  /* ── Totaux ── */
  .totaux-container {
    display: flex;
    justify-content: flex-end;
    margin-bottom: 7mm;
  }
  .totaux-table {
    width: 55mm;
  }
  .totaux-table td {
    padding: 3px 5px;
    font-size: 9pt;
    border: none;
  }
  .totaux-table .label { color: #555; }
  .totaux-table .montant { text-align: right; font-weight: 600; }
  .totaux-table .total-ttc {
    background: #1a56db;
    color: #fff;
    font-weight: 800;
    font-size: 11pt;
    border-radius: 3px;
  }
  .totaux-table .total-ttc .label { color: #fff; font-weight: 800; }
  .totaux-separator td { border-top: 1px solid #ccc; padding-top: 4px; }

  /* ── Conditions ── */
  .conditions {
    display: flex;
    gap: 8mm;
    margin-bottom: 7mm;
  }
  .conditions-box {
    flex: 1;
    padding: 4mm;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
  }
  .conditions-box h4 {
    font-size: 8pt;
    font-weight: 700;
    text-transform: uppercase;
    color: #1a56db;
    margin-bottom: 4px;
    letter-spacing: 0.4px;
  }
  .conditions-box p {
    font-size: 8.5pt;
    line-height: 1.6;
    color: #333;
  }
  .acompte-badge {
    display: inline-block;
    background: #dcfce7;
    color: #166534;
    font-weight: 700;
    font-size: 12pt;
    padding: 3px 8px;
    border-radius: 4px;
    margin-top: 3px;
  }

  /* ── Flags ── */
  .flags-section {
    padding: 4mm 5mm;
    background: #fffbeb;
    border: 1px solid #f59e0b;
    border-radius: 4px;
    margin-bottom: 7mm;
  }
  .flags-section h4 {
    font-size: 8.5pt;
    font-weight: 700;
    color: #92400e;
    margin-bottom: 4px;
  }
  .flags-section ul {
    padding-left: 4mm;
  }
  .flags-section li {
    font-size: 8.5pt;
    color: #78350f;
    line-height: 1.8;
  }

  /* ── Mentions légales ── */
  .legal {
    border-top: 1px solid #ddd;
    padding-top: 5mm;
    margin-top: 5mm;
  }
  .legal h4 {
    font-size: 8pt;
    font-weight: 700;
    text-transform: uppercase;
    color: #444;
    margin-bottom: 4px;
    letter-spacing: 0.4px;
  }
  .legal p, .legal li {
    font-size: 7.5pt;
    line-height: 1.6;
    color: #555;
    margin-bottom: 3px;
  }
  .legal ul { padding-left: 4mm; margin-bottom: 5px; }

  /* ── Signature ── */
  .signature-block {
    display: flex;
    gap: 10mm;
    margin-top: 8mm;
    page-break-inside: avoid;
  }
  .signature-box {
    flex: 1;
    border: 1px dashed #bbb;
    border-radius: 4px;
    padding: 4mm;
    min-height: 25mm;
  }
  .signature-box p {
    font-size: 8pt;
    color: #777;
    margin-bottom: 2px;
  }
  .signature-box .mention {
    font-size: 7.5pt;
    color: #999;
    font-style: italic;
  }
</style>
</head>
<body data-numero="{{ meta.numero_devis }}">

<!-- ═══ EN-TÊTE ═══ -->
<div class="header">
  <div class="header-artisan">
    <h1>{{ artisan.raison_sociale }}</h1>
    <div class="subtitle">Plombier — Chauffagiste</div>
    <div class="contact-info">
      {{ artisan.adresse }}<br>
      Tél. {{ artisan.telephone }}
      {%- set email_ok = artisan.email and artisan.email not in ('[À COMPLÉTER]', '[À COMPLÉTER — OBLIGATOIRE]', 'null', 'None') -%}
      {% if email_ok %}&nbsp;&nbsp;·&nbsp;&nbsp;{{ artisan.email }}{% endif %}<br>
      SIRET&nbsp;: {{ artisan.siret }}
      {%- set rcs_ok = artisan.rcs_rm and artisan.rcs_rm not in ('[À COMPLÉTER]', '[À COMPLÉTER — OBLIGATOIRE]', 'null', 'None') -%}
      {% if rcs_ok %}&nbsp;&nbsp;·&nbsp;&nbsp;{{ artisan.rcs_rm }}{% endif %}
      {% if artisan.tva_intracom and artisan.tva_intracom != 'Non assujetti à la TVA' %}
      <br>N° TVA&nbsp;: {{ artisan.tva_intracom }}
      {% endif %}
    </div>
  </div>
  <div class="header-devis">
    <div class="devis-label">DEVIS</div>
    <div class="devis-numero">{{ meta.numero_devis }}</div>
    <div class="dates">
      Date d'émission&nbsp;: <strong>{{ meta.date_emission }}</strong><br>
      Valable jusqu'au&nbsp;: <strong>{{ meta.date_validite }}</strong>
    </div>
  </div>
</div>

<!-- ═══ CLIENT + CHANTIER ═══ -->
<div class="info-block">
  <div class="info-box">
    <h3>Client</h3>
    <p>
      <strong>{{ client.nom }}</strong><br>
      {% if client.adresse_chantier %}{{ client.adresse_chantier }}<br>{% endif %}
      {% if client.telephone %}Tél. {{ client.telephone }}<br>{% endif %}
      {% if client.email %}{{ client.email }}{% endif %}
    </p>
  </div>
  {% if show_assurance %}
  <div class="info-box">
    <h3>Assurance décennale</h3>
    <p>
      {{ artisan.assurance_decennale }}<br>
      Police n°&nbsp;{{ artisan.numero_police }}
    </p>
  </div>
  {% endif %}
</div>

<!-- ═══ OBJET ═══ -->
<div class="objet-block">
  <strong>Objet&nbsp;:</strong> {{ meta.reference_chantier }}
</div>

<!-- ═══ TABLEAU DES POSTES ═══ -->
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
      <td class="td-desc">
        {{ ligne.description }}
        {% if ligne.detail %}<div class="detail">{{ ligne.detail }}</div>{% endif %}
        {% if '[À COMPLÉTER]' in ligne.description or '[À COMPLÉTER]' in (ligne.detail or '') %}
        {% endif %}
      </td>
      <td class="td-unite">{{ ligne.unite }}</td>
      <td class="td-qte">{{ "%.2f"|format(ligne.quantite) }}</td>
      <td class="td-pu">{{ "%.2f"|format(ligne.prix_unitaire_ht) }}&nbsp;€</td>
      <td class="td-tva">{{ (ligne.taux_tva * 100)|int }}&nbsp;%</td>
      <td class="td-montant">{{ "%.2f"|format(ligne.montant_ht) }}&nbsp;€</td>
    </tr>
    {% endfor %}
  </tbody>
</table>

<!-- ═══ TOTAUX ═══ -->
<div class="totaux-container">
  <table class="totaux-table">
    <tr>
      <td class="label">Sous-total HT</td>
      <td class="montant">{{ "%.2f"|format(totaux.sous_total_ht) }}&nbsp;€</td>
    </tr>
    {% if totaux.montant_tva_10 > 0 %}
    <tr>
      <td class="label">TVA 10 %</td>
      <td class="montant">{{ "%.2f"|format(totaux.montant_tva_10) }}&nbsp;€</td>
    </tr>
    {% endif %}
    {% if totaux.montant_tva_55 > 0 %}
    <tr>
      <td class="label">TVA 5,5 %</td>
      <td class="montant">{{ "%.2f"|format(totaux.montant_tva_55) }}&nbsp;€</td>
    </tr>
    {% endif %}
    {% if totaux.montant_tva_20 > 0 %}
    <tr>
      <td class="label">TVA 20 %</td>
      <td class="montant">{{ "%.2f"|format(totaux.montant_tva_20) }}&nbsp;€</td>
    </tr>
    {% endif %}
    <tr class="totaux-separator">
      <td colspan="2"></td>
    </tr>
    <tr class="total-ttc">
      <td class="label">TOTAL TTC</td>
      <td class="montant">{{ "%.2f"|format(totaux.total_ttc) }}&nbsp;€</td>
    </tr>
  </table>
</div>

<!-- ═══ CONDITIONS ═══ -->
<div class="conditions">
  <div class="conditions-box">
    <h4>Acompte à la commande</h4>
    <p>{{ conditions.acompte_pourcentage }}&nbsp;% à la signature</p>
    <div class="acompte-badge">{{ "%.2f"|format(conditions.acompte_montant_ttc) }}&nbsp;€ TTC</div>
  </div>
  <div class="conditions-box">
    <h4>Délai de réalisation</h4>
    <p>{{ conditions.delai_realisation }}</p>
  </div>
  <div class="conditions-box">
    <h4>Modalités de paiement</h4>
    <p>{{ conditions.modalites_paiement }}</p>
    <p style="margin-top:4px; font-size:7.5pt; color:#888;">{{ conditions.penalites_retard }}</p>
  </div>
</div>

<!-- ═══ FLAGS ═══ -->
{% if show_flags %}
{% set clean_flags = flags | reject('equalto', '...') | reject('equalto', '') | list %}
{% if clean_flags %}
<div class="flags-section">
  <h4>⚠️ Points à vérifier avant envoi au client</h4>
  <ul>
    {% for flag in clean_flags %}
    <li>{{ flag }}</li>
    {% endfor %}
  </ul>
</div>
{% endif %}
{% endif %}

<!-- ═══ MENTIONS LÉGALES ═══ -->
<div class="legal">
  <h4>Mentions légales</h4>

  {% if mentions_legales.tva_note %}
  <p><strong>TVA :</strong> {{ mentions_legales.tva_note }}</p>
  {% endif %}

  <p><strong>Attestation TVA réduite :</strong> Travaux éligibles au taux réduit de TVA conformément
  à l'article 278-0 bis du CGI. Le client atteste que le logement est achevé depuis plus de 2 ans
  et est affecté à usage d'habitation. Toute fausse déclaration engage la responsabilité du client.</p>

  <p><strong>Garanties :</strong> {{ mentions_legales.garantie }}</p>

  {% if mentions_legales.droit_retractation %}
  <p><strong>Droit de rétractation :</strong> Conformément à l'article L.221-18 du Code de la
  consommation, vous disposez d'un délai de <strong>14 jours francs</strong> à compter de la
  signature du présent devis pour exercer votre droit de rétractation, sans avoir à justifier de
  motifs ni à payer de pénalités. Pour exercer ce droit, adressez une déclaration écrite à
  {{ artisan.raison_sociale }}, {{ artisan.adresse }}.</p>
  {% endif %}

  <p><strong>Validité :</strong> Ce devis est valable 3 mois à compter de la date d'émission.</p>

  <p><strong>Litiges :</strong> En cas de litige, les parties s'engagent à rechercher une solution
  amiable avant tout recours judiciaire. Médiateur de la consommation&nbsp;:
  CM2C — www.cm2c.net</p>
</div>

<!-- ═══ SIGNATURES ═══ -->
<div class="signature-block">
  <div class="signature-box">
    <p><strong>Bon pour accord — L'artisan</strong></p>
    <p>{{ artisan.raison_sociale }}</p>
    <p style="margin-top:14mm;">Signature :</p>
    <p class="mention">Date&nbsp;: {{ meta.date_emission }}</p>
  </div>
  <div class="signature-box">
    <p><strong>Bon pour accord — Le client</strong></p>
    <p>{{ client.nom }}</p>
    <p style="margin-top:14mm;">Signature&nbsp;: <span style="font-style:italic;color:#aaa;">(précéder de la mention «&nbsp;Lu et approuvé&nbsp;»)</span></p>
    <p class="mention">Date&nbsp;: ___________________</p>
  </div>
</div>

</body>
</html>
"""


# ── Fonction principale de génération ─────────────────────────────────────────

def generate_pdf(devis: dict, output_path: str, show_flags: bool = False, show_assurance: bool = True) -> str:
    """
    Prend un dict JSON devis et génère un PDF à output_path.
    Retourne le chemin du fichier généré.
    """
    # Valeurs par défaut pour les champs optionnels
    devis.setdefault("flags", [])
    devis.setdefault("mentions_legales", {})
    devis["mentions_legales"].setdefault("droit_retractation", True)
    devis["mentions_legales"].setdefault("tva_note", "")
    devis["mentions_legales"].setdefault(
        "garantie",
        "Garantie décennale et biennale conformément aux articles 1792 et suivants du Code civil."
    )

    # Recalcul centralisé et déterministe via calculs.py
    from calculs import recalculer_totaux
    devis = recalculer_totaux(devis)

    # Rendu Jinja2 → HTML
    env  = Environment(loader=BaseLoader())
    tmpl = env.from_string(DEVIS_HTML_TEMPLATE)
    html_content = tmpl.render(**devis, show_flags=show_flags, show_assurance=show_assurance)

    # Génération PDF avec WeasyPrint
    HTML(string=html_content).write_pdf(output_path)

    return output_path
