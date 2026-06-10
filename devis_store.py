"""
SOUFFL.AI — Persistance des devis et factures (PostgreSQL)
===========================================================
Stocke chaque devis/facture en JSON dans la table `devis` (Railway PostgreSQL).
L'API publique reste identique à l'ancienne version fichier.
"""

import json
from typing import Optional
from db import get_cursor


# ── Devis ─────────────────────────────────────────────────────────────────────

def save_devis(user_id: int, devis: dict) -> str:
    """
    Sauvegarde (ou met à jour) un devis en base.
    Retourne le numéro du devis.
    """
    from datetime import datetime
    numero = devis.get("meta", {}).get("numero_devis", f"DEVIS-{datetime.now().strftime('%Y%m%d')}-000")
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO devis (user_id, numero, type, data)
            VALUES (%s, %s, 'devis', %s::jsonb)
            ON CONFLICT (user_id, numero) DO UPDATE
              SET data = EXCLUDED.data
            """,
            (user_id, numero, json.dumps(devis, ensure_ascii=False)),
        )
    return numero


def load_devis(user_id: int, numero: str) -> Optional[dict]:
    """Charge un devis par son numéro."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT data FROM devis WHERE user_id = %s AND numero = %s AND type = 'devis'",
            (user_id, numero),
        )
        row = cur.fetchone()
        return dict(row["data"]) if row else None


def list_devis(user_id: int, limit: int = 5) -> list[dict]:
    """
    Liste les derniers devis d'un artisan (les plus récents en premier).
    Retourne une liste de dicts avec numero, date, client, total_ttc.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT data FROM devis
            WHERE user_id = %s AND type = 'devis'
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (user_id, limit),
        )
        result = []
        for row in cur.fetchall():
            data = dict(row["data"])
            meta   = data.get("meta", {})
            totaux = data.get("totaux", {})
            result.append({
                "numero":    meta.get("numero_devis", "—"),
                "date":      meta.get("date_emission", "—"),
                "client":    data.get("client", {}).get("nom", "—"),
                "chantier":  meta.get("reference_chantier", "—"),
                "total_ttc": totaux.get("total_ttc", 0),
            })
        return result


def get_last_devis(user_id: int) -> Optional[dict]:
    """Retourne le dernier devis créé par l'artisan."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT data FROM devis
            WHERE user_id = %s AND type = 'devis'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        row = cur.fetchone()
        return dict(row["data"]) if row else None


# ── Factures ─────────────────────────────────────────────────────────────────

def save_facture(user_id: int, facture: dict) -> str:
    """
    Sauvegarde une facture typée (FAC-...-ACOMPTE, FAC-...-SOLDE, etc.)
    Retourne le numéro de facture.
    """
    from datetime import datetime
    numero = facture.get("meta", {}).get("numero_facture", f"FAC-{datetime.now().strftime('%Y%m%d')}-000")
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO devis (user_id, numero, type, data)
            VALUES (%s, %s, 'facture', %s::jsonb)
            ON CONFLICT (user_id, numero) DO UPDATE
              SET data = EXCLUDED.data
            """,
            (user_id, numero, json.dumps(facture, ensure_ascii=False)),
        )
    return numero


def load_facture(user_id: int, numero_facture: str) -> Optional[dict]:
    """Charge une facture par son numéro (FAC-...)."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT data FROM devis WHERE user_id = %s AND numero = %s AND type = 'facture'",
            (user_id, numero_facture),
        )
        row = cur.fetchone()
        return dict(row["data"]) if row else None


def get_factures_for_devis(user_id: int, numero_devis: str) -> list:
    """
    Retourne toutes les factures liées à un devis donné,
    triées par date d'émission croissante.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT data FROM devis
            WHERE user_id = %s
              AND type = 'facture'
              AND data->'meta'->>'numero_devis_origine' = %s
            ORDER BY created_at ASC
            """,
            (user_id, numero_devis),
        )
        return [dict(row["data"]) for row in cur.fetchall()]


def get_total_deja_facture(user_id: int, numero_devis: str) -> float:
    """Retourne le total TTC déjà facturé pour un devis (toutes factures confondues)."""
    factures = get_factures_for_devis(user_id, numero_devis)
    return round(sum(
        f.get("facturation", {}).get("montant_cette_facture_ttc", 0)
        for f in factures
    ), 2)
