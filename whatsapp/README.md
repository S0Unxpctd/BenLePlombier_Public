# Souffl.AI V3 — WhatsApp adapter

FastAPI webhook + Meta Cloud API integration that delivers feature parity with
the Telegram bot (`V3/bot.py`). Everything transport-agnostic lives in
`V3/core/`; this directory is the **transport layer only**.

## Architecture at a glance

```
 Meta Cloud API
      │
      ▼
 POST /webhook (signed)
 GET  /webhook (verification)
      │
      ▼
 whatsapp/webhook.py   ── FastAPI app, signature check, lazy dispatch
      │
      ▼
 whatsapp/router.py    ── normalises Meta JSON → InboundMessage
      │
      ▼
 whatsapp/flows.py     ── one handler per inbound, composes:
      │                      · core/state_store   (Postgres state)
      │                      · core/quote_flow    (quick_extract, generate_quote)
      │                      · core/edit_flow     (edit_quote)
      │                      · core/profile_flow  (ONBOARDING_STEPS, apply_field_update)
      │                      · client_store, devis_store, facture_generator, email_sender
      ▼
 whatsapp/send.py      ── outbound: text / buttons / list / document / audio / image
 whatsapp/media.py     ── two-step media download (inbound) + upload (outbound)
 whatsapp/users.py     ── phone_e164 ↔ user_id (Postgres-backed)
 whatsapp/commands.py  ── FR natural-language command parser
 whatsapp/config.py    ── env-var loading
```

The Telegram bot (`V3/bot.py`) runs untouched. The two workers share the same
Postgres, `clients`, `devis`, `conversation_states` tables. A new table
`whatsapp_users` holds the phone ↔ user_id mapping (created by
`V3/db_init.py`).

## Required environment variables

See `V3/.env.example`. Four are strictly required at boot:

| Variable | Purpose |
|---|---|
| `WHATSAPP_TOKEN` | Long-lived System User token. Generate via Meta Business Settings. |
| `WHATSAPP_PHONE_NUMBER_ID` | From Meta for Developers. |
| `WHATSAPP_VERIFY_TOKEN` | Arbitrary secret you pick; paste into Meta when configuring the webhook. |
| `META_APP_SECRET` | App Secret from Meta for Developers → Basic. Used to verify every inbound POST. |

The adapter warns loudly (and tests continue) when any are missing.

## Running locally with ngrok

1. Install dependencies once:
   ```bash
   pip install -r V3/requirements.txt
   ```
2. Export env vars (or `source .env`):
   ```bash
   export WHATSAPP_TOKEN=… WHATSAPP_PHONE_NUMBER_ID=… \
          WHATSAPP_VERIFY_TOKEN=… META_APP_SECRET=… DATABASE_URL=postgresql://…
   ```
3. Start the webhook:
   ```bash
   cd V3
   uvicorn whatsapp.webhook:_lazy_app --factory --host 0.0.0.0 --port 8080 --reload
   ```
4. In another shell, expose it:
   ```bash
   ngrok http 8080
   ```
5. In Meta for Developers → WhatsApp → Configuration:
   - URL de rappel: `https://<your-ngrok>.ngrok-free.app/webhook`
   - Vérifier le token: **the same value as `WHATSAPP_VERIFY_TOKEN`**
   - Click **Verify and Save**. Meta will send a `GET` with
     `hub.mode=subscribe` and the challenge; our handler echoes the challenge.
6. Subscribe to the `messages` webhook field.
7. Add your own phone to the test whitelist (up to 5 numbers while Meta
   Business Verification is pending).

## Deploying on Railway

A separate Railway service points at `railway.whatsapp.json`:

```json
{
  "build": {"builder": "DOCKERFILE", "dockerfilePath": "Dockerfile"},
  "deploy": {
    "startCommand": "uvicorn whatsapp.webhook:_lazy_app --factory --host 0.0.0.0 --port ${PORT:-8080}",
    "healthcheckPath": "/health",
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 3
  }
}
```

Steps in the Railway UI:

1. Create a new service inside the same project as the Telegram worker.
2. Connect it to the same Git repo, pointing at `V3/`.
3. Add the env vars from `.env.example` (same values as the Telegram service
   for Postgres / OpenAI / OpenRouter, plus the four WhatsApp secrets).
4. Set the railway config path to `railway.whatsapp.json`.
5. Deploy. Railway emits a public URL — paste that into Meta's **URL de
   rappel** with `/webhook` appended and set the verify token there.

### Coexistence with the Telegram worker
- They share the same `DATABASE_URL`, but `whatsapp_users.user_id` lives in a
  different numerical range (≥ `10^12`) so there's no collision with
  Telegram user ids.
- `conversation_states` has a `channel` column — each row is owned by one
  adapter.
- The Telegram bot's `context.user_data` is still in-memory per-process; the
  WhatsApp adapter is fully Postgres-backed.

## Limitations known at Phase 2 close

- **Whitelist-only** while Meta Business Verification is pending. Up to 5
  numbers and 1000 free messages per 90 days.
- **24h customer-service window.** Outside that window only pre-approved
  message templates can be sent. Phase 6 introduces templates.
- **No debouncing of multi-voice-memo chains** — if the artisan sends two
  voice notes 3 seconds apart, the MVP treats them as two quotes. Phase 3
  will add a 10-second debounce via `pending_audio`.
- **Text-only onboarding.** Phase 4 will add Meta Flows for a native form.
- **No image/PDF ingestion during normal chat.** Phase 5 adds Claude Vision.

## Test coverage

Under `V3/tests/whatsapp/`. Runnable with:

```bash
cd V3
python3 -m pytest tests/whatsapp/ -v
```

The suite covers the Phase 2 § QA test contract items 1–10 (unit + mock-Meta
integration). Items 11–13 are live tests against the real Meta API — they
must be run manually with a whitelisted test phone.
