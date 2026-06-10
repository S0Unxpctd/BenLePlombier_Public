"""
SOUFFL.AI V3 — Initialisation du schéma PostgreSQL
==================================================
À lancer UNE SEULE FOIS après avoir provisionné la DB Railway.

    python db_init.py

Crée (si absentes) :
- clients               — profils artisans (V2)
- devis                 — devis + factures (V2)
- conversation_states   — état conversationnel transport-agnostique (V3, Phase 1)
- whatsapp_users        — mapping phone E.164 → user_id (V3, Phase 2,
                          créée ici pour ne pas avoir deux migrations distinctes;
                          la Phase 2 remplira les lignes)

L'opération est idempotente (CREATE ... IF NOT EXISTS).
"""

from db import get_cursor


SCHEMA = """
-- ──────────────────────────────────────────────────────────────────
-- V2 tables (identiques à la racine)
-- ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS clients (
    user_id         BIGINT PRIMARY KEY,
    profile         JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS devis (
    id              SERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    numero          TEXT NOT NULL,
    type            TEXT NOT NULL DEFAULT 'devis',   -- 'devis' | 'facture'
    data            JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, numero)
);

CREATE INDEX IF NOT EXISTS idx_devis_user_id ON devis(user_id);
CREATE INDEX IF NOT EXISTS idx_devis_created ON devis(user_id, created_at DESC);

-- ──────────────────────────────────────────────────────────────────
-- V3 Phase 1 — État conversationnel (agnostique au canal)
-- ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS conversation_states (
    user_id    BIGINT PRIMARY KEY,
    channel    TEXT NOT NULL CHECK (channel IN ('telegram', 'whatsapp')),
    flow       TEXT,
    step       TEXT,
    data       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conv_states_channel ON conversation_states(channel);

-- ──────────────────────────────────────────────────────────────────
-- V3 Phase 2 — Mapping WhatsApp (créée ici, peuplée plus tard)
-- ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS whatsapp_users (
    phone_e164  TEXT PRIMARY KEY,
    user_id     BIGINT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ──────────────────────────────────────────────────────────────────
-- V3 Phase 3 — Voice message debounce queue
-- ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS voice_debounce_queue (
    user_id     BIGINT NOT NULL,
    wamid       TEXT PRIMARY KEY,
    media_id    TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    transcription TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'  -- 'pending' | 'transcribed' | 'processed'
);

CREATE INDEX IF NOT EXISTS idx_voice_debounce_user ON voice_debounce_queue (user_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_voice_debounce_status ON voice_debounce_queue (status, received_at);
"""


def init_db():
    with get_cursor() as cur:
        cur.execute(SCHEMA)
    print("✅ Schéma PostgreSQL V3 initialisé avec succès.")


if __name__ == "__main__":
    init_db()
