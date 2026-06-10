# V3 Backlog

Ideas that emerged during V3 execution but are out of scope for the current phase. Each entry: date — title — 1-line rationale — originating phase.

## Deferred features

- **2026-04-14** — Fix pre-existing `test_client_store.py` / `test_devis_store.py` collection errors — these reference `CLIENTS_FILE` / `DEVIS_DIR` attributes from the old file-based store; tests were not updated during the Postgres migration. Rewrite them with a Postgres test fixture (testcontainers or a disposable schema). **Not a regression**; preexists V3. Phase 0/1 observation.
- **2026-04-14** — Fix pre-existing `test_bot_helpers.py::TestBuildRecap` two failures — `build_recap()` signature changed (flags moved to `build_flags_message()`); tests need updating. Phase 0/1 observation.
- **2026-04-14** — WhatsApp recorded welcome voice note — user (So) to record a 30-second script; fallback TTS prepared in Phase 3. Phase 3 deliverable, noted here for early preparation.
- **2026-04-14** — Pick backup provider (R2 / B2 / S3) — deferred per So until there are ~10 active artisans. Backup scaffold ready in Phase 0. Phase 0 Actions Required.
- **2026-04-14** — Rotate `WHATSAPP_TOKEN` — the dev-shared token in chat is compromised; user must generate permanent System User token before any WhatsApp live test. Phase 2 blocker.
- **2026-04-14** — Retrieve `META_APP_ID` + `META_APP_SECRET` + pick `WHATSAPP_VERIFY_TOKEN` — user action required, Phase 2 blocker.
- **2026-04-14** — Thin `V3/bot.py` to delegate transport-agnostic pieces to `V3/core/*` — nice-to-have hygiene. NOT a Phase 2 blocker: WhatsApp adapter uses `core/*` directly. The Telegram bot keeps running on its original code path until this refactor is done in a dedicated mini-phase. Phase 1 observation.
- **2026-04-14** — Fix a minor Pylance-style correctness in `core/quote_flow.py._accepts_now` signature usage — `number_assigner` interface allows both `(devis)` and `(devis, now=...)`. Not a bug; flagged as a clarity improvement for a future touch.

## Session 2 observations (Phase 2 close)

- **2026-04-14 (S2)** — `whatsapp.send.mark_typing` is a no-op. Meta's Cloud API exposes a typing indicator only via `POST /messages` with `status=read + typing_indicator` on an inbound `message_id`. The MVP implementation skips it; UX parity with Telegram's `edit_text` status messages is achieved by sending short "Génération en cours…" texts instead. Phase 3 can wire the real indicator with a few extra lines — low priority.
- **2026-04-14 (S2)** — `whatsapp.flows._really_send_devis_email` also handles the facture case by calling the same `send_devis_email_fn`. In reality `email_sender` has `send_facture_email` too; the MVP uses devis-email for both since the flow is identical and the Telegram bot's two paths differ only in the email subject template. Minor tech debt; split when Phase 3 touches email.
- **2026-04-14 (S2)** — `_list_recent_devis` renders only 5 items and doesn't paginate. Parity with Telegram. Phase 3 can add a List Message with per-devis rows.
- **2026-04-14 (S2)** — `whatsapp.flows._show_main_menu` emits 5 rows with ids `menu:*`; the interactive handler currently ignores them (logs only). Parity upgrade: map each to the existing commands. Small; do it in Phase 3 alongside the UX-upgrade work.
- **2026-04-14 (S2)** — 2 pre-existing backup-test failures appear when running in a sandboxed environment with a stale `/tmp/soufflai-v3-*.tar.gz` file owned by a different uid. Not a code regression. In clean CI the tests pass.

## Session 3 observations (Phase 2.5 close)

- **2026-04-16 (S3)** — Real-world fixtures for prompt regression tests. The Session 3 fixtures under `V3/tests/fixtures/prompts/` are explicitly synthetic. Once the v2 prompt has been live-tested against real voice memos, save 2–3 real transcriptions (with PII redacted) as `V3/tests/fixtures/prompts/real/*.json` so future refactors can regression-check against genuine customer language, not invented prose.
- **2026-04-16 (S3)** — Auto-sync `TARIFS_DEFAUT` constants with the price table embedded inside `SYSTEM_PROMPT`. Today they live in two places and must be updated in lockstep by hand. A small Python helper (`tools/validate_prompt_prices.py`) could assert the prices quoted in the prompt match the values in `TARIFS_DEFAUT`. Low priority — the prompt prices come from the reference grid, not `TARIFS_DEFAUT`.
- **2026-04-16 (S3)** — LLM-level end-to-end prompt eval. The Phase 2.5 test contract validates *structural* invariants (line count, TVA, placeholder preservation) without calling a real LLM. A cheap nightly eval that runs the 5 synthetic fixtures through the live OpenRouter model and asserts the expected ranges would catch prompt drift over time. Defer until cost/volume justify it.
- **2026-04-16 (S3)** — `PROMPT_V2_COMPARISON.md` deliverable from `V3_BRIEFS/2_PHASE_SPECS.md` §Phase 2.5 was descoped this session with user approval (the 5 real quotes aren't committed in the repo; comparison is a user-side manual validation).

## Session 3 observations (Phase 3 close)

- **2026-04-16 (S3)** — Replace the piggyback debounce flush with a true background scheduler (APScheduler or Railway cron worker). Current implementation flushes only on the next inbound message from the user; edge case: user sends 1 voice, taps nothing, first voice sits in `voice_debounce_queue.status='transcribed'` until the user returns. For MVP this is acceptable (a follow-up ping will flush it); for sustained use a 5-second tick is cleaner. Interface `_maybe_flush_debounce(user_id, phone, deps)` is already shaped for this upgrade.
- **2026-04-16 (S3)** — `SELECT … FOR UPDATE` in `debounce.fetch_pending_for_user` for multi-process safety. Under a single Railway container today there's no real race, but if we horizontally scale the WhatsApp service, two workers could fetch the same pending rows simultaneously. The atomic `mark_processed` single-row UPDATE prevents double-processing, but a row-level lock would prevent the redundant work. Low priority.
- **2026-04-16 (S3)** — Real `welcome.ogg` recording. The Builder shipped `welcome_script.md` + a placeholder `.txt`; if So doesn't record it, onboarding completion falls back to `HELP_TEXT` as text. Not a blocker; a 10-second UX improvement when recorded.
- **2026-04-16 (S3)** — `_handle_export` year filter only. Current export zips all devis/factures for the CURRENT year only. Users may want "export 2025" or "export all". The > 90 MB fallback hints at this ("Écrivez *export 2025* pour filtrer par année") but the feature is not implemented. Add FR argument parsing on the export command.
- **2026-04-16 (S3)** — Recap rollback telemetry. If live-testing shows artisans find the recap confusing and `recap_enabled=False` is flipped in prod, add an event marker to `events.jsonl` so we know it happened. Currently rollback is invisible.
- **2026-04-16 (S3)** — Welcome retry policy. `welcome_sent` is set to True BEFORE the audio send is attempted. If the upload fails, the artisan gets no welcome message and no retry. Low priority; a resend via `menu:help` is always available.
