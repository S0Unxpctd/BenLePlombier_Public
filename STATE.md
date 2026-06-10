# V3 Live State

Source of truth for "what exists right now in V3/". A fresh Opus session can read this file first to understand the state without replaying the full `events.jsonl`.

## Current tree

```
V3/
├── README.md                       # unchanged
├── TESTING.md                      # unchanged
├── Dockerfile                      # unchanged
├── nixpacks.toml                   # unchanged
├── railway.json                    # Telegram worker (unchanged)
├── railway.whatsapp.json           # NEW — WhatsApp webhook service config
├── .env.example                    # NEW — env-var template for both services
├── requirements.txt                # +sentry-sdk; +fastapi; +uvicorn[standard]
├── bot.py                          # unchanged since Session 1 (Sentry wired)
├── onboarding.py                   # unchanged
├── system_prompt.py                # REWRITTEN in Session 3 (Phase 2.5 v2)
├── calculs.py                      # unchanged — deterministic money lives here
├── pdf_generator.py                # unchanged
├── facture_generator.py            # unchanged
├── client_store.py                 # unchanged
├── devis_store.py                  # unchanged
├── db.py                           # unchanged
├── db_init.py                      # whatsapp_users table already present
├── email_sender.py                 # unchanged
├── core/                           # Phase 1 extraction — unchanged this session
│   ├── state_store.py  quote_flow.py  edit_flow.py  profile_flow.py
├── ops/                            # Phase 0 — unchanged this session
│   ├── backup.py  sentry.py  README.md
├── whatsapp/                       # Phase 2 transport adapter; Phase 3 extensions
│   ├── __init__.py
│   ├── config.py                   # WhatsAppConfig dataclass + get_config() cache
│   ├── users.py                    # phone ↔ user_id mapping (Postgres)
│   ├── media.py                    # 2-step download + upload, retries, cleanup
│   ├── send.py                     # text/buttons/list/document/audio/image/typing
│   ├── commands.py                 # FR parser (Session 3: +EXPORT command)
│   ├── webhook.py                  # FastAPI: GET verify + signed POST ingest
│   ├── router.py                   # dispatch_webhook_event → per-message routing
│   ├── debounce.py                 # NEW S3 — Postgres queue for voice debounce
│   ├── flows.py                    # handle_inbound — Phase 2 + 3 feature set (~1800 LOC)
│   ├── media/                      # NEW S3 — welcome voice assets
│   │   ├── welcome_script.md       # FR script (~30s) for welcome.ogg
│   │   └── welcome.ogg.placeholder.txt   # guide for providing real .ogg
│   └── README.md                   # ngrok dev + Meta/Railway config guide
├── tests/
│   ├── … existing + ops/ (unchanged)
│   ├── core/
│   │   └── test_system_prompt.py   # NEW Session 3 — 22 structural invariant tests
│   ├── fixtures/
│   │   └── prompts/                # NEW Session 3 — 5 synthetic fixtures + README
│   └── whatsapp/                   # Session 2 (Phase 2) + Session 3 (Phase 3 additions)
│       ├── test_debounce.py        # NEW S3 — 14 unit tests on whatsapp/debounce.py
│       └── test_flows_phase3.py    # NEW S3 — 21 integration tests for the 5 upgrades
│       ├── __init__.py
│       ├── conftest.py             # FakeCursor, SendRecorder, test_config, sign()
│       ├── test_webhook_verification.py   # 15 tests (QA item 1)
│       ├── test_send.py                    # 18 tests (QA item 2)
│       ├── test_media.py                   # 12 tests (QA item 3)
│       ├── test_users.py                   # 17 tests
│       ├── test_commands.py                # 38 tests (QA item 5)
│       ├── test_router.py                  #  8 tests
│       └── test_flows.py                   # 18 tests (QA items 4, 6–10)
├── PROGRESS.md                     # dashboard (refreshed this session)
├── BACKLOG.md                      # deferred ideas
├── DECISIONS.md                    # decision log
├── STATE.md                        # this file
└── CHECKPOINT.md                   # resume instructions for Phase 2.5
```

## Session scope

**Session 1 (2026-04-14):** Phase 0 + Phase 1.
**Session 2 (2026-04-14):** Phase 2 — WhatsApp MVP feature parity (live-validated).
**Session 3 (2026-04-16):** Phase 2.5 (system prompt v2) + Phase 3 (5 WhatsApp UX upgrades).

## Test state

```
373 passed, 2 failed, 27 errors  (full V3 suite, clean CI)
372 passed, 2 failed, 2 deselected, 27 errors, 0 warnings  (with TestRunBackupDryRun deselected)
```

Breakdown:
- 80 pre-V3 tests still pass (no regression).
- 21 V3 Phase 0 tests pass.
- 89 V3 Phase 1 tests pass.
- 126 V3 Phase 2 tests pass.
- 22 V3 Phase 2.5 tests pass (Session 3 — `tests/core/test_system_prompt.py`).
- 35 V3 Phase 3 tests pass (Session 3 — `tests/whatsapp/test_debounce.py` (14) + `tests/whatsapp/test_flows_phase3.py` (21)).
- 2 pre-existing failures in `tests/test_bot_helpers.py::TestBuildRecap` (NOT caused by V3; documented in `BACKLOG.md`).
- 27 pre-existing errors in `tests/test_client_store.py` + `tests/test_devis_store.py` (NOT caused by V3; stale tests referencing `CLIENTS_FILE` / `DEVIS_DIR` attributes removed in the Postgres migration).

## Environment variables

Already required by the copied Telegram code:
```
TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, OPENROUTER_API_KEY, OPENROUTER_MODEL,
DATABASE_URL, PDF_OUTPUT_DIR, ADMIN_USER_ID, RESEND_API_KEY
```

New (optional, read in V3):
```
SENTRY_DSN                 # Phase 0 — error monitoring
ENVIRONMENT                # Phase 0 — "production" | "staging" | "dev"
BACKUP_PROVIDER            # Phase 0 — "dryrun" (default) | "s3" | "r2" | "b2"
BACKUP_LOCAL_DIR           # Phase 0 — dry-run output dir (default ./data/backups)
BACKUP_ENDPOINT_URL        # Phase 0 — for r2/b2 or custom
BACKUP_BUCKET              # Phase 0 — bucket name (required if provider != dryrun)
BACKUP_ACCESS_KEY_ID       # Phase 0
BACKUP_SECRET_ACCESS_KEY   # Phase 0
BACKUP_REGION              # Phase 0 — default "auto"
```

Phase 2 credentials status (as of 2026-04-14 end of Session 2):
```
META_APP_ID                   # 797345939755437 (public, logged)
META_APP_SECRET               # ✅ in Railway
WHATSAPP_VERIFY_TOKEN         # ✅ in Railway
WHATSAPP_TOKEN                # ✅ in Railway (dev token — rotate before sustained use)
WHATSAPP_PHONE_NUMBER_ID      # 1039449349255433 (to set in Railway)
WHATSAPP_BUSINESS_ACCOUNT_ID  # 1279215394335435 (to set in Railway)
WHATSAPP_GRAPH_VERSION        # default v21.0 (only override if Meta bumps versions)
WEBHOOK_BASE_URL              # PENDING — appears after the WhatsApp Railway service
                              #    deploys; paste into Meta's "URL de rappel" field
                              #    along with `WHATSAPP_VERIFY_TOKEN`.
```

## Actions Required from user (So) — end of Session 3

1. **Deploy Session 3 to Railway.** Push V3. The WhatsApp worker picks up
   `system_prompt.py` v2 and Phase 3 UX upgrades. The new table
   `voice_debounce_queue` is auto-created via `db_init` on startup.
2. **Live-test the new prompt** (Phase 2.5 validation): 2–3 real voice
   memos on a whitelisted phone. Check ≤5 line items, no `[À COMPLÉTER]`
   on délai, prices aligned with `Tarifs_Reference_Artisan.xlsx`,
   coherence flag fires on an absurd dictation.
3. **Live-test Phase 3 UX**: (a) recap buttons, (b) menu dispatch, (c)
   back-to-back vocals <10s merge, (d) `export` command produces a zip.
4. **Record ~30-second welcome voice note**. Place at
   `V3/whatsapp/media/welcome.ogg`. Script:
   `V3/whatsapp/media/welcome_script.md`. Without it, new artisans who
   finish onboarding get `HELP_TEXT` as text — no audio.
5. **Rotate `WHATSAPP_TOKEN`** to a permanent System User token before
   sustained use — dev token expires every 24h.
6. **Pick backup target** — still deferred until ~10 active artisans.
7. **Sentry (optional)** — create a free-tier project and set `SENTRY_DSN`
   if you want error tracking.

**Settled in Session 2:** WhatsApp Railway deploy + Meta webhook + phone
whitelist + Phase 2 QA items 11–13 — all live-validated.
