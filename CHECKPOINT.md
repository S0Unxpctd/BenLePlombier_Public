# Souffl.AI V3 — Session Handoff Checkpoint

**Written:** 2026-04-16, end of Session 3 (Opus 4.6).
**Purpose:** make it trivial for a fresh Opus session to resume exactly where Session 3 stopped and carry on with Phase 4 (native WhatsApp Flows for onboarding).

> If you're the fresh Opus reading this: read this file first, then `V3/STATE.md`, then `V3/DECISIONS.md`, then `V3/BACKLOG.md`. The original 4 briefs in `V3_BRIEFS/` remain the source of truth for Phases 4–7.

---

## 1. What Session 3 delivered

**Phase 2.5 — System prompt v2.** Complete.

- `V3/system_prompt.py` rewritten (~330 prompt lines; constants unchanged). Addresses the 4 known issues from `CONTEXT_SOUFFLAI.md`:
  - Hard cap on line items (≤5 for simple jobs ≤1000 € HT, ≤7 for medium, ≤10 max).
  - Coherence self-review before final JSON (material ↔ room, MO/fournitures ratio, magnitude).
  - Reference-grid pricing from `Tarifs_Reference_Artisan.xlsx` embedded as prompt text; no +15% rénovation coefficient by default.
  - `[À COMPLÉTER]` narrowed to 3 cases (client name, site address, missing assurance). Délai gets a smart estimate; generic material gets a generic label.
- `V3/tests/fixtures/prompts/` — 5 synthetic fixtures + README (explicitly labelled synthetic, not real customer quotes).
- `V3/tests/core/test_system_prompt.py` — 22 structural invariant tests, 22/22 pass.
- Reviewer-caught fix: `D.441-5` legal-article citation restored.

**Phase 3 — WhatsApp "better than Telegram" — 5 UX upgrades.** Complete.

Every feature is behind a feature flag (`FlowDeps` dataclass default = "off", `FlowDeps.default()` sets it to "on"). This preserves the Phase 2 regression guarantee: existing 126 WhatsApp tests construct `FlowDeps` manually and observe the OLD behavior; prod sees the new one.

- **3.1 Recap** — after the LLM emits a devis JSON, send a FR recap with 3 buttons (✅ Confirm / ✏️ Edit / ❌ Cancel) and defer PDF until confirm. Edit path re-opens the recap with new content. Feature flag: `FlowDeps.recap_enabled` (default False, prod True).
- **3.2 Menu wiring** — the 5 `menu:*` ids emitted by `_show_main_menu` now dispatch in `_handle_interactive`: new_quote → hint + state clear, list_quotes → `_list_recent_devis`, new_invoice → `_start_facture_picker`, profile → `_show_profile_summary`, help → `HELP_TEXT`.
- **3.3 Welcome voice note** — `_send_welcome_if_possible` plays `V3/whatsapp/media/welcome.ogg` after onboarding completion, idempotent via `state.data["welcome_sent"]`. Falls back to `HELP_TEXT` text if the `.ogg` is absent. Script at `V3/whatsapp/media/welcome_script.md`.
- **3.4 Multi-voice debounce** — new Postgres table `voice_debounce_queue` (auto-created via `db_init.py`). `V3/whatsapp/debounce.py` wraps it. Voice arrival: enqueue → transcribe → store → if multiple pending in window, concatenate and process as one; if a newer one exists, wait. Flush piggybacks on the next inbound message (no APScheduler yet; noted in BACKLOG). Feature flag: `FlowDeps.debounce_window_s` (default 0 = instant, prod 10).
- **3.5 Export archive** — `Command.EXPORT` (FR: "export", "exporter", "archiver", "archive", "mes documents"). `_handle_export` zips current-year devis + factures + JSON + README.txt, ≤90 MB, upload as WhatsApp document; >90 MB → text fallback.
- Tests: `V3/tests/whatsapp/test_debounce.py` (14 unit tests on the Postgres wrapper) + `V3/tests/whatsapp/test_flows_phase3.py` (21 integration tests covering all 5 features). 35/35 new tests pass.

**Full V3 suite: 373 passed, 2 failed, 27 errors (clean CI)** / with `--deselect tests/ops/test_backup.py::TestRunBackupDryRun`: **372 passed, 2 failed, 2 deselected, 27 errors, 0 warnings**. The 2 failures + 27 errors are the Session 1 baseline (pre-existing, documented). **Zero new regressions in Session 3.**

---

## 2. Session scope decision — read this before continuing

Session 3 was a **Phase 2.5 + Phase 3** session by explicit user scoping. Both QA contracts are satisfied:
- Phase 2.5: 22 structural tests pass; live-quality validation is a user-side manual step post-deploy.
- Phase 3: 35 unit + integration tests pass; live-UX validation is a user-side manual step post-deploy.

**Before moving on to Phase 4, ask the user whether the Session 3 live tests passed.** Specifically:
- Phase 2.5 quality: 2–3 real voice memos on WhatsApp produce ≤5 line items on simple jobs, no `[À COMPLÉTER]` on délai, prices ≈ reference grid, coherence flag fires on an absurd dictation.
- Phase 3 UX: recap buttons work; menu dispatch works; two back-to-back vocals <10s are merged; `export` produces a zip.

If any live test failed, that's the highest-priority thing to fix before Phase 4 begins.

---

## 3. First actions for the fresh Opus session

Do these in order.

### 3.1 Bootstrap context

1. Read `V3/CHECKPOINT.md` (this file).
2. Read `V3/STATE.md` (current tree, env vars, test baseline).
3. Read `V3/DECISIONS.md` (now 14 decisions — 6 from Session 1, 4 from Session 2, 4 from Session 3).
4. Read `V3/BACKLOG.md` (deferred items — do NOT implement without user approval; several Phase 3 items in there).
5. Read `V3_BRIEFS/1_MASTER_BRIEF.md` §3 (execution pattern) and `V3_BRIEFS/2_PHASE_SPECS.md` §Phase 4.

You do NOT need to re-read the full codebase. `system_prompt.py`, `whatsapp/`, and the new `whatsapp/debounce.py` are the Session 3 additions. If Phase 4 wires WhatsApp Flows, you'll be modifying mostly `whatsapp/webhook.py` (new `/flow-submit` route), `whatsapp/router.py` (Flow payload normalization), and adding `whatsapp/flows_native/` or similar for the Flow JSON definitions.

### 3.2 Verify the baseline still holds

Before writing any new code:

```bash
cd "/sessions/gifted-fervent-lamport/mnt/Souffl.AI - Devis AI Vocal/V3"
python3 -m pytest tests/ --deselect tests/ops/test_backup.py::TestRunBackupDryRun -q 2>&1 | tail -5
# Expect: 372 passed, 2 failed, 2 deselected, 27 errors, 0 warnings  (clean CI)
```

If the numbers drift downward, investigate — something regressed.

### 3.3 Confirm the live-test outcome with the user

Ask:

> Did the Session 3 live tests pass?
> (a) Phase 2.5 quality — real vocals on a whitelisted phone produced quotes you could send without editing.
> (b) Phase 3 UX — recap buttons, menu dispatch, 10s debounce merge, `export` command.

If any answer is "no", stop Phase 4 work and fix the Session 3 bug first.

### 3.4 Start Phase 4

Follow `V3_BRIEFS/2_PHASE_SPECS.md` §Phase 4 verbatim. Key objectives (summarised):

- Build 2–3 WhatsApp Flows using Meta's Flow JSON schema:
  - Flow 1: New user onboarding (3 screens: identity, TVA/tarif, assurance/email).
  - Flow 2: Profile edit (single screen, all editable fields).
  - Flow 3: Quick-quote template ("Remplacement chauffe-eau" with only surface, client name, client address).
- Flow JSON files under `V3/whatsapp/flows/` (or `flows_native/` — pick a name that doesn't conflict with the existing `flows.py` module).
- Endpoint `POST /flow-submit` in `V3/whatsapp/webhook.py` to handle Flow submissions.
- Integration tests with mocked Flow payloads.
- Keep the conversational onboarding from Phase 2 as fallback (don't delete `_start_onboarding` etc.).

### 3.5 Honor the execution contract

- **Execution mode:** use the multi-agent pattern (Sonnet 4.6 Builder / Reviewer / QA sub-agents with isolated contexts via the Task/Agent tool). Session 3 validated this works well and caught real issues. See `DECISIONS.md` → "Execution mode: real multi-agent with isolated Sonnet sub-agents" for the pattern. Cross-check QA sub-agent inventories against the original spec — don't just trust the pass count (see the gap-audit decision in `DECISIONS.md`).
- Hit the strict QA gate: 100% of Phase 4's declared tests must pass before handoff.
- No "known issues" list. If something can't be fixed this session, descope to `BACKLOG.md` with justification.
- Opus personally runs the final QA pass on Phase 4 (it's a native Meta integration — Flow validator is run externally, so extra care needed).
- Never touch files outside `V3/` (except `V3_BRIEFS/` read-only).

### 3.6 Known Phase 4 risks

- **Flow JSON schema drift.** Meta's Flow JSON spec evolves; validation happens in Meta's dashboard, not locally. Plan for 1 cycle of "submit, get validation error, patch, resubmit".
- **Fallback path preservation.** The existing conversational onboarding in `whatsapp/flows.py::_start_onboarding` must still work — for users on older WhatsApp builds that don't support Flows, or if Meta rate-limits Flow submissions.
- **Profile shape consistency.** The Postgres `clients` table has 14 fields today. The Flow payload must map cleanly to those; any field a Flow can set must already exist on `clients` or you need a schema migration.

---

## 4. Open backlog items relevant to Phase 4+

From `V3/BACKLOG.md` (Session 3 additions in bold):

- **Replace piggyback debounce flush with APScheduler** — current flush triggers on the next inbound message; edge case is silent user. Phase 4 might add a scheduler already (Flow submissions arrive on a different endpoint, so a scheduler becomes natural infra).
- **`SELECT … FOR UPDATE` in `debounce.fetch_pending_for_user`** — multi-process safety. Not a blocker at current scale.
- **Real `welcome.ogg` recording** — user (So) action; a 10-second UX improvement. Not a Phase 4 blocker.
- **Export year-filter arguments** (`export 2025`, `export all`) — user-facing feature improvement; not structural.
- **Recap rollback telemetry** — if `recap_enabled=False` ever gets flipped in prod, log the event to `events.jsonl`. Small addition.
- Thin `V3/bot.py` to delegate to `V3/core/*` — still nice-to-have hygiene; not a Phase 4 blocker.
- Fix pre-existing `test_client_store.py` / `test_devis_store.py` errors (27) — stale tests referencing the old file store.
- Fix pre-existing `test_bot_helpers.py::TestBuildRecap` (2 failures) — stale tests asserting the old `build_recap` shape.
- Phase 2 micro-backlog: `mark_typing` no-op, `send_facture_email` vs `send_devis_email` merge, devis list pagination, stale-tmp-file backup-test glitch.

---

## 5. Quick orientation commands

```bash
# Where am I?
cd "Souffl.AI - Devis AI Vocal"
ls V3/                              # sanity-check the tree
ls V3/whatsapp/                     # Phase 2 + Phase 3 additions
cat V3/STATE.md                     # current state
cat V3/DECISIONS.md                 # why things are the way they are

# Run all tests (expect 372/2/2/27/0w clean)
cd V3
python3 -m pytest tests/ --deselect tests/ops/test_backup.py::TestRunBackupDryRun -q

# Run just the Session 3 new tests
python3 -m pytest tests/core/test_system_prompt.py tests/whatsapp/test_debounce.py tests/whatsapp/test_flows_phase3.py -v

# Phase 3 features are gated via FlowDeps flags; to verify from Python:
python3 -c "from whatsapp.flows import FlowDeps; d = FlowDeps.default(); print('recap_enabled=', d.recap_enabled, 'debounce_window_s=', d.debounce_window_s)"
# expected: recap_enabled=True debounce_window_s=10

# Start the WhatsApp webhook locally for smoke testing
uvicorn whatsapp.webhook:_lazy_app --factory --host 0.0.0.0 --port 8080 --reload
```

---

## 6. Glossary (Session 3 additions in bold)

- **"V3"** = the rewrite that lives under `V3/`. The Telegram bot keeps running untouched.
- **`core/`** = transport-agnostic business logic. No `telegram`, `fastapi`, `flask`, `whatsapp` imports at top level (enforced by a test).
- **`whatsapp/`** = the Phase 2 transport adapter + Phase 3 UX extensions.
- **`whatsapp/debounce.py`** (Session 3) = the Postgres wrapper for the `voice_debounce_queue` table.
- **`ops/`** = operational scaffolding (backup, monitoring). Not hot-path.
- **`FlowDeps`** = dependency container for `whatsapp.flows`. `FlowDeps.default()` wires prod; tests inject a fake. Session 3 added `recap_enabled`, `debounce_window_s`, 4 debounce callables, and `send_audio`.
- **`ConversationState`** = one row per user, keyed by `user_id`, with `channel` discriminator (`'telegram'` or `'whatsapp'`). Session 3 added `pending_devis`, `editing_recap`, `welcome_sent` keys in `data`.
- **`Actions Required`** = dashboard banner listing user-side blockers. Refreshed in `PROGRESS.md` at the start of every session and this file at the end.
- **Recap flow** (Session 3) = the 3-button ✅/✏️/❌ interaction between quote generation and PDF delivery.
- **Debounce window** (Session 3) = the 10-second interval within which multiple voice memos from the same phone get concatenated and processed as one.

---

## 7. Closing

Session 3 shipped the two highest-leverage improvements after the WhatsApp pivot: (a) a system prompt that makes quotes "envoyable sans retouche", and (b) a UX layer (recap, menu, welcome, debounce, export) that makes WhatsApp genuinely better than the Telegram experience. Phase 4 (native Flows) is the next native-Meta leverage point and the onboarding-time-to-first-devis win.

The hand-off is clean. 372 tests passing, zero new regressions, zero warnings, all tracking docs in lockstep.

— Session 3 Opus
