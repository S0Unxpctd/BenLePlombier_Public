# Souffl.AI V3 — Progress

_Session 3 closed: 2026-04-16. Scope delivered: Phase 2.5 (system prompt v2) + Phase 3 (WhatsApp UX upgrades). Resumes at Phase 4 per `CHECKPOINT.md`._

## ⏸️ Actions Required (user-side)

1. **Deploy Session 3 to Railway.** Push the V3 folder; the WhatsApp worker picks up `system_prompt.py` v2, the new `voice_debounce_queue` table (auto-created via `db_init`), and Phase 3 UX upgrades. Telegram worker: unchanged, no redeploy needed unless you want the prompt v2 there too (single shared file).
2. **Live-test the new prompt.** Send 2–3 real voice memos on WhatsApp with a whitelisted phone. Compare rendered PDFs against your 5 April 2026 reference quotes: ≤5 line items on simple jobs, no `[À COMPLÉTER]` on délai, prices aligned with `Tarifs_Reference_Artisan.xlsx`, coherence flag fires on an absurd dictation.
3. **Live-test the new Phase 3 UX.** Check: (a) recap shows up before the PDF and the 3 buttons work; (b) tapping menu rows triggers the right action; (c) two back-to-back vocals within 10s are merged; (d) `export` command produces a zip.
4. **Record ~30-second welcome voice note.** Place at `V3/whatsapp/media/welcome.ogg`. Until you do, new artisans who finish onboarding get `HELP_TEXT` as a text message — no audio. Script is at `V3/whatsapp/media/welcome_script.md`.
5. **Rotate `WHATSAPP_TOKEN`** to a permanent System User token before sustained use (dev token expires every 24h).
6. **Backup target** — still deferred until ~10 active artisans.
7. **Sentry (optional)** — set `SENTRY_DSN` if you want error tracking.

## Overall status

| Phase | Title | Status |
|---|---|---|
| 0 | Stabilize current production | ✅ Done |
| 1 | Prep refactor: extract shared core modules | ✅ Done |
| 2 | WhatsApp MVP — feature parity | ✅ Done (live-validated S2) |
| 2.5 | System prompt v2 | ✅ Done (Session 3) |
| 3 | WhatsApp UX upgrades (5) | ✅ Done (Session 3) |
| 4 | WhatsApp Flows | Pending |
| 5 | Photos + Vision | Pending |
| 6 | Message templates + retention | Pending |
| 7 | Web access + sharing | Pending |

## What shipped this session

### Phase 2.5 — System prompt v2

Rewrote `V3/system_prompt.py` to fix the 4 known quality issues in `CONTEXT_SOUFFLAI.md`:
- **Too many line items** → hard cap (≤5 for simple jobs ≤1000 €, ≤7 for medium, ≤10 max) with an LLM self-check.
- **Missing coherence checks** → self-review block before final JSON: material ↔ room match, MO/fournitures ratio, total magnitude vs scope. Single `[COHÉRENCE — reason]` flag on anomaly.
- **Prices too high** → embedded reference grid from `Tarifs_Reference_Artisan.xlsx` (WC 125 €, vasque 80 €, chauffe-eau MO 280 €, etc.). Explicit rule: no +15% rénovation coefficient by default. Pivot from daily-rate to forfait-first.
- **Too many `[À COMPLÉTER]`** → reserved for 3 cases only (nom client, adresse chantier, assurance décennale). Délai → estimate; generic material → "mitigeur thermostatique standard".

Synthetic non-regression fixtures under `V3/tests/fixtures/prompts/`:
`01_remplacement_chaudiere.json`, `02_salle_de_bain_complete.json`, `03_depannage_fuite_simple.json`, `04_remplacement_chauffe_eau.json`, `05_incoherent_double_vasque_cuisine.json` + `README.md` labelling them synthetic.

Legal citation `D.441-5` restored after Reviewer caught its omission.

### Phase 3 — WhatsApp UX upgrades (5 features)

All 5 features land behind a consistent feature-flag pattern: dataclass default = "off" (preserves Phase 2 regression guarantee), `FlowDeps.default()` flips to "on" for prod.

**3.1 Conversational pre-PDF recap** (`recap_enabled=True` in prod). After the LLM emits a valid devis JSON, send a recap text (per-line descriptions + total TTC + client/chantier) with 3 reply buttons ✅ Générer le PDF / ✏️ Modifier / ❌ Recommencer. Edit path ingests a new voice or text, modifies the pending devis, re-emits the recap. Pending devis persists in `ConversationState.data["pending_devis"]`.

**3.2 Main menu List wiring** — the 5 `menu:*` ids emitted by `_show_main_menu` are now dispatched in `_handle_interactive`: new_quote → hint + state clear, list_quotes → `_list_recent_devis`, new_invoice → `_start_facture_picker`, profile → `_show_profile_summary`, help → `HELP_TEXT`.

**3.3 Pre-recorded welcome voice note.** `_send_welcome_if_possible` plays `V3/whatsapp/media/welcome.ogg` after the 12-step onboarding completes, idempotent via `state.data["welcome_sent"]`. File absent → falls back to `HELP_TEXT` text. Script at `V3/whatsapp/media/welcome_script.md` (~30s FR).

**3.4 Multi-voice debounce** (`debounce_window_s=10` in prod). New Postgres table `voice_debounce_queue` (wamid PK, user_id, media_id, received_at, transcription, status). `whatsapp/debounce.py` exposes `enqueue_voice`, `transcribe_and_store`, `fetch_pending_for_user`, `mark_processed`. At the top of `handle_inbound`, `_maybe_flush_debounce` flushes stale queues on any inbound. Voice arrival path: enqueue → transcribe → if multiple pending within window, concatenate and process as one; if a newer one exists, wait. MVP piggybacks on next inbound message for flush (no APScheduler yet; see BACKLOG).

**3.5 Export archive command.** New `Command.EXPORT` (FR variants: "export", "exporter", "archiver", "archive", "mes documents"). `_handle_export` zips the current year's devis + factures PDFs + JSONs + a README.txt, 90 MB headroom, uploads as WhatsApp document. > 90 MB → text fallback.

### Phase 3 QA contract — results

| Item | Scope | Status |
|---|---|---|
| 3.1 Recap | 6 flow tests (off/on/confirm/cancel/edit/format) | ✅ |
| 3.2 Menu | 5 dispatch tests (one per menu id) | ✅ |
| 3.3 Welcome | 2 tests (fallback + audio path) | ✅ |
| 3.4 Debounce unit | 14 tests on the Postgres wrapper (enqueue, transcribe-store, fetch, mark_processed, parameterised SQL meta-test, tz-aware) | ✅ |
| 3.4 Debounce integration | 4 tests (window=0 regression, merge-in-window, 15s-apart separate, maybe_flush noop) | ✅ |
| 3.5 Export | 4 tests (command parse, zero-devis, zip structure, size-fallback) | ✅ |
| Full-suite regression | Baseline + 35 new tests green | ✅ |
| Live quality validation | **Manual** — user post-deploy | ⏸ |

## Tests

| Suite | Pass | Fail | Error |
|---|---|---|---|
| Pre-V3 baseline | 80 | 2 | 27 |
| V3 Phase 0 (`tests/ops/`) | 21 | 0 | 0 |
| V3 Phase 1 (`tests/core/`) | 89 | 0 | 0 |
| V3 Phase 2 (`tests/whatsapp/`) | 126 | 0 | 0 |
| V3 Phase 2.5 (`tests/core/test_system_prompt.py`) | 22 | 0 | 0 |
| V3 Phase 3 (`tests/whatsapp/test_debounce.py` + `test_flows_phase3.py`) | 35 | 0 | 0 |
| **Total** | **373** | **2** | **27** |

With `--deselect tests/ops/test_backup.py::TestRunBackupDryRun`:
**372 passed, 2 failed, 2 deselected, 27 errors, 0 warnings**.

The 2 failures (`TestBuildRecap`) and 27 errors (stale client/devis store tests) are the Session 1 baseline. **Zero new regressions in Session 3.**

## Execution mode

Session 3 ran the multi-agent pattern from `V3_BRIEFS/1_MASTER_BRIEF.md` §3 with real Sonnet 4.6 sub-agents (isolated contexts) for Builder / Reviewer / QA. Opus orchestrated + ran the final pass on both phases. The Reviewer's isolated context caught (and forced a fix of) the missing `D.441-5` legal citation in Phase 2.5, and the Opus gap-audit caught 3 missing debounce tests in the first QA pass that a focused second QA sub-agent then filled.

## Session 3 artifacts

- `V3/system_prompt.py` — v2 rewrite (~330 lines of prompt + unchanged constants)
- `V3/tests/fixtures/prompts/` — 5 synthetic fixtures + README
- `V3/tests/core/test_system_prompt.py` — 22 structural invariant tests
- `V3/whatsapp/flows.py` — +~550 lines for 5 UX upgrades (recap/menu/welcome/debounce/export)
- `V3/whatsapp/debounce.py` — new, ~130 lines Postgres wrapper
- `V3/whatsapp/commands.py` — `Command.EXPORT` + FR variants
- `V3/whatsapp/media/welcome_script.md` + `welcome.ogg.placeholder.txt`
- `V3/db_init.py` — `voice_debounce_queue` DDL
- `V3/tests/whatsapp/test_debounce.py` — 14 unit tests
- `V3/tests/whatsapp/test_flows_phase3.py` — 21 integration tests

## Metrics

- LOC added Session 3: ~2200 (prompt v2 ~330, flows.py +550, debounce.py 130, tests ~1100, fixtures + docs ~90)
- Modules touched outside `V3/whatsapp/` or `V3/tests/`: 2 (`system_prompt.py`, `db_init.py`)
- Test execution time: ~6s for the full V3 suite
