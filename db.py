"""
SOUFFL.AI — Connexion PostgreSQL
=================================
Gère la connexion à la base de données Railway PostgreSQL.
Utilise DATABASE_URL injecté automatiquement par Railway.
"""

import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_connection():
    """Retourne une connexion psycopg2 brute."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL non définie. Vérifie tes variables d'environnement Railway.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


@contextmanager
def get_cursor():
    """Context manager : ouvre une connexion + curseur, commit ou rollback auto."""
    conn = get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur
    finally:
        conn.close()
