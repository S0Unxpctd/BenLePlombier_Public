# V3 Decisions Log

Chronological log of non-trivial decisions made during V3 execution. Each entry: date — decision — rationale — alternatives considered — impact.

---

## 2026-04-14 — Execution mode: single-Opus orchestrator, simulated sub-agent loop

**Decision.** This session runs Phases 0+1 only (confirmed with user). Because the runtime can't spawn truly independent Sonnet sub-agents with isolated contexts, Opus (me) enacts the Builder → Reviewer → QA → Integrator loop itself for each feature, applying each role's checklist strictly. The Builder/Reviewer/QA separation is enforced through discipline, not process isolation.

**Rationale.** The user explicitly scoped this session to Phases 0+1. The multi-agent design in the brief is optimal when spawning is possible, but its quality gates (spec compliance, strict QA contract, no "known issues") translate fine to a single-Opus setting — what matters is the bar, not the identity of the reviewer.

**Alternatives.** Running every step as a sequential Agent sub-call — too expensive in context for a 2-phase scope.

**Impact.** Phase 2+ handoff documented in `CHECKPOINT.md` with an explicit recommendation for the next Opus session: if the environment supports parallel sub-agents, revert to the full role-split pattern for Phases 2-7.

---

## 2026-04-14 — Baseline test state accepted as-is

**Decision.** The existing 2 failures in `test_bot_helpers.py` and 27 errors in `test_client_store.py` / `test_devis_store.py` are pre-existing (not caused by V3). Documented in `BACKLOG.md`. V3 quality gate for Phase 0/1: (a) no NEW test regressions, (b) every new V3 test must pass 100%.

**Rationale.** The brief's strict-quality bar applies to V3 work. Fixing legacy test debt is a separate line item; doing it as part of Phase 0 would blur the regression signal.

**Impact.** All Phase 0/1 test reports compare new state vs the documented baseline (80 pass / 2 fail / 27 errors).

---

## 2026-04-14 — V3 folder excludes runtime data and scraper/v2_fournitures

**Decision.** Copied into `V3/`: the 11 Python modules + tests/ + deployment config + README/TESTING. Excluded: `output/`, `data/pdf/*` contents, `scraper/`, `v2_fournitures/`, `venv/`, `__pycache__/`, `.DS_Store`.

**Rationale.** Scraper and v2_fournitures are Fourniture-track code (Richardson/Cédéo price catalogue). Per `TECH_ASSESSMENT_Fourniture.md` and `CONTEXT_SOUFFLAI.md`, Fourniture is deferred; V3's focus is WhatsApp + prompt v2. Keeping the V3 tree smaller makes refactors and regression tests cleaner.

**Impact.** If a later phase wants Fourniture, it can be re-copied or built fresh aligned with V3's new `core/` structure.

---

## 2026-04-14 — Backup provider deferred, not blocked

**Decision.** Phase 0 ships a provider-agnostic backup scaffold (S3-compatible). No live target wired. The user flagged backup as a decision to make once there are ~10 active artisans. An "Actions Required" entry on the dashboard reminds the user.

**Rationale.** Premature provider choice locks in latency/cost characteristics before we know write volume. Scaffold-and-wait lets us turn it on in minutes later.

**Impact.** `V3/ops/backup.py` has a pluggable `Backend` abstraction; dry-run mode writes to local disk. Turning it on = set env vars + instantiate one real backend.

---

## 2026-04-14 — State store schema: `conversation_states` table, JSONB data

**Decision.** The Phase 1 `core/state_store.py` uses a single table `conversation_states(user_id PRIMARY KEY, channel TEXT, flow TEXT, step TEXT, data JSONB, updated_at TIMESTAMP)` as specified in `4_TECHNICAL_REFERENCE.md`. `user_id` is the channel-specific native ID: Telegram's numeric user_id for Telegram, and a deterministic hash of the E.164 phone for WhatsApp (locked in Phase 2).

**Rationale.** Matches the reference doc. One row per user is the simplest mapping of the existing `context.user_data` model. JSONB lets us store the exact dict shape the bot already puts in `user_data` (pre_prix_items, pending_transcription, waiting_for_email, etc.) — zero re-modelling.

**Alternatives.** A normalized model (one row per flow step) — rejected; the bot treats user_data as a single mutable dict, normalization would fight that.

**Impact.** Phase 1 adds this table via a V3-specific init SQL. The Telegram adapter in `V3/bot.py` can opt-in to the Postgres-backed store while still defaulting to `context.user_data` for compatibility.

---

## 2026-04-14 (Session 2) — WhatsApp user_id strategy: SHA-256 % 10^12 + offset

**Decision.** `whatsapp.users.phone_to_user_id(phone_e164)` returns
`10**12 + (sha256(phone) % 10**12)`. The deterministic hash is persisted in
`whatsapp_users(phone_e164, user_id)` on first message; subsequent lookups
are indexed reads, not hash recomputes.

**Rationale.** BIGINT column shared with Telegram. Telegram ids sit far below
`10**10`; offsetting to `10**12` keeps the two id spaces fully disjoint, so
no `channel` discriminator is needed on `clients`, `devis`, `factures`, or
`conversation_states` joins. Persisting the mapping means the hash is just
the *initial allocation* — collisions (extremely rare even at 10k users)
would be caught at insert time.

**Alternatives.** (a) Auto-incrementing SERIAL column — rejected: needs an
extra round-trip to look up an existing phone before INSERT. (b) Use phone
digits directly — rejected: `33600000000` collides with a plausible future
Telegram id and makes analytics confusing.

**Impact.** Documented at the top of `V3/whatsapp/users.py`. Tested in
`tests/whatsapp/test_users.py::TestPhoneToUserId`.

---

## 2026-04-14 (Session 2) — FastAPI over Flask for the webhook

**Decision.** The WhatsApp webhook uses FastAPI + Starlette TestClient.

**Rationale.** (a) Native async dispatch lets us offload Whisper+LLM+PDF work
to an `asyncio.create_task` while replying 200 inside Meta's 20s ack window.
(b) Starlette's TestClient gives us high-fidelity integration tests without
spinning up a real server. (c) FastAPI is already the de-facto pick for
modern Python webhooks — community familiarity benefit.

**Alternatives considered.** (a) Flask + gunicorn — simpler but no async
baseline and TestClient story is worse for long-running handlers. (b) Raw
Starlette — identical runtime shape but loses FastAPI's ergonomic Header /
Query injection.

**Impact.** `requirements.txt` gains `fastapi>=0.110` and
`uvicorn[standard]>=0.29`. The Telegram worker doesn't import either; it
still starts via `python bot.py`. The transport-isolation AST test in
`tests/core/` forbids top-level `fastapi` imports in `core/*` so the
boundary stays clean.

---

## 2026-04-14 (Session 2) — Dependency-injected FlowDeps for the WhatsApp adapter

**Decision.** Every external in `whatsapp.flows` (state store, LLM, Whisper,
send primitives, media download, PDF generators, devis store, email sender)
is passed through a `FlowDeps` dataclass. `FlowDeps.default()` wires
production; tests construct a fully-mocked one.

**Rationale.** The Phase 2 QA contract requires 10 unit + integration tests
with no network and no real Postgres. DI is the cheapest way to get there
without meddling with `client_store.py` / `devis_store.py` (which would
break the Telegram worker).

**Impact.** 140 tests run in ~1s with zero external side effects. No module
under `V3/whatsapp/` reads `os.environ` at function scope except
`config.get_config()` itself.

---

## 2026-04-14 (Session 2) — Onboarding routing via `onboarding_step_idx`, not `client_exists`

**Decision.** In `flows.handle_inbound`, the onboarding branch fires when
`client_exists(user_id)` is False **OR** `conversation_state.data` contains
`onboarding_step_idx`. Previously we used `client_exists` alone.

**Rationale.** Saving the first onboarding answer (`raison_sociale`) makes
`client_exists` return True, which short-circuited the second-answer flow
into the general command dispatcher — a real bug caught by the 12-step
end-to-end test. The fix is minimal and matches the Telegram bot behaviour,
where `ConversationHandler` steps own their routing regardless of profile
completion.

**Impact.** One 4-line change in `flows.py`. Covered by
`tests/whatsapp/test_flows.py::TestOnboardingEnd2End::test_full_12_step_walk`.

---

## 2026-04-14 — Phase 1 approach: additive extraction, no behavior change

**Decision.** Phase 1 creates `V3/core/{state_store,quote_flow,edit_flow,profile_flow}.py` that the existing `V3/bot.py` can import. The first refactor is an adapter pattern: the new core modules are pure; `V3/bot.py` keeps its Telegram-specific code but delegates the transport-agnostic work. We do NOT rewrite the 1652-line `bot.py` top-to-bottom in Phase 1 — that risk is high.

**Rationale.** The brief asks Phase 1 to "extract" core modules and keep `V3/bot.py` working and behavior-equivalent. Extraction-by-delegation achieves that with much lower regression risk than a full rewrite. Phase 2's WhatsApp adapter then uses the same `core/` with a new WhatsApp-specific adapter, not a fork of `bot.py`.

**Impact.** Parity check (QA test 2 of Phase 1) is satisfied by the delegation preserving the current call graph. Future phases can thin `bot.py` further without Phase 1 owning that work.

---

## 2026-04-16 (Session 3) — Execution mode: real multi-agent with isolated Sonnet sub-agents

**Decision.** Session 3 uses the Builder → Reviewer → QA pattern with *real* Sonnet 4.6 sub-agents launched via the Task/Agent tool, each with its own isolated context. Opus remains the orchestrator and runs the final personal pass on Phase 2.5 + Phase 3. This is the preferred pattern per `V3_BRIEFS/1_MASTER_BRIEF.md` §3 and supersedes Session 1 + 2's single-Opus fallback for all future sessions where sub-agent spawning is available.

**Rationale.** Isolated contexts on Reviewer and QA produce independent verification. The Reviewer catches what the Builder missed *because* it doesn't see the Builder's reasoning — only the artifact. This directly caught (and fixed) the missing `D.441-5` legal-article citation in Phase 2.5's prompt.

**Impact.** Future sessions should default to this pattern. If ever the Task tool is unavailable, document that in a new decision entry and run the checklist discipline manually.

---

## 2026-04-16 (Session 3) — Reference-grid prices embedded as prompt text, not loaded at runtime

**Decision.** The Phase 2.5 v2 system prompt embeds the reference price grid (WC 125 €, vasque 80 €, chauffe-eau MO 280 €, réno SdB complète 1850 €, etc.) directly as plain text inside `SYSTEM_PROMPT`. No runtime load from `Tarifs_Reference_Artisan.xlsx`.

**Rationale.** (a) The grid has ~20 rows and changes rarely. (b) Runtime loading would add an `openpyxl` dependency to every WhatsApp/Telegram request path and a possible cold-start penalty. (c) An LLM consumes text, not a pandas DataFrame; embedding means the LLM sees the pricing directly in its working context and can reference it fluently.

**Alternatives considered.** (a) Load the xlsx at bot start and inject via `inject_profile_in_prompt` — rejected for dependency creep and complexity. (b) Per-artisan profile override — already possible via `profile["journee_standard_ht"]` etc., covers the common customisation case.

**Impact.** When the reference grid genuinely changes, edit `V3/system_prompt.py` and add a DECISIONS entry. Documented in both `PROGRESS.md` and the `TARIFS_DEFAUT` comment in `system_prompt.py`.

---

## 2026-04-16 (Session 3) — Phase 2.5 "[À COMPLÉTER]" reserved for 3 specific cases only

**Decision.** The v2 prompt narrows `[À COMPLÉTER]` usage to exactly three cases: (a) nom client manquant, (b) adresse chantier manquante, (c) assurance décennale artisan manquante (obligatoire légalement). For everything else — délai de réalisation, marque de matériau générique, quantité estimée — the LLM must use a smart default and NOT emit a flag.

**Rationale.** The strategic pivot in `CONTEXT_SOUFFLAI.md` is "devis envoyable sans retouche". Every `[À COMPLÉTER]` flag the artisan sees requires a manual edit before sending. Measuring against the 5 April 2026 quotes, the v1 prompt averaged 3–5 flags per quote, mostly on non-critical fields. Narrowing to 3 cases brings that to ≤1 per well-scoped job.

**Alternatives considered.** (a) Keep the broad flag usage and surface them only at QA time — rejected: the artisan still sees them in the PDF. (b) Eliminate `[À COMPLÉTER]` entirely — rejected: a missing décennale is a regulatory issue the artisan must address, and silently filling client name would be deceptive.

**Impact.** Tested by `tests/core/test_system_prompt.py::test_a_completer_narrowed`. Live validation is a Session 3 user Action Required.

---

## 2026-04-16 (Session 3) — Phase 2.5 pivoted unit from "journée" to "forfait" for standardised work

**Decision.** The v2 prompt says: for standard prestations (pose, remplacement, réfection joint, etc.), use forfait prices from the embedded reference grid. Reserve the daily rate (350 €/jour) exclusively for non-standardised work (complex dépannage, recherche de fuite, rénovation sur mesure).

**Rationale.** The reference grid shows real-world Dupont Plomberie prices are 90% forfaits (e.g. "Pose WC suspendu → 125 €"), not day-rates. v1 prompt's `quantite × prix_journée` model systematically overshot by applying 350 €/jour + 15% réno coefficient even for a 1-hour joint swap. Forfait-first aligns prompt output with actual invoicing patterns.

**Alternatives considered.** Drop daily rate entirely — rejected: some jobs genuinely are time-and-material (recherche de fuite, dépannage complexe). Keep both available; let the LLM pick based on job type.

**Impact.** The three daily-rate placeholder tokens (`350,00 € HT / jour`, `450,00 € HT / jour`, `40,00 € HT / jour de présence`) remain in the prompt so `inject_profile_in_prompt` can still apply per-artisan overrides — they're just used less often.

---

## 2026-04-16 (Session 3) — Phase 3 feature flags: "off" on dataclass, "on" in FlowDeps.default()

**Decision.** Every Phase 3 feature that changes existing Phase 2 behavior uses a feature flag on the `FlowDeps` dataclass with a default of "off" (or 0), and is explicitly set to "on" (or 10) inside `FlowDeps.default()`. Applies to: `recap_enabled: bool = False` (prod: True) and `debounce_window_s: int = 0` (prod: 10).

**Rationale.** The Phase 2 test suite builds `FlowDeps` manually with a minimum set of mocks. Any Phase-3 feature turned on by default would silently change what those tests observe — breaking the regression guarantee. Defaulting to "off" keeps the full existing Phase 2 behaviour intact; turning on in `default()` means production still gets the new UX. No existing test was modified — a hard requirement of the Phase 3 brief.

**Alternatives.** (a) Default on + rewrite Phase 2 tests — rejected, would blur the regression signal and explicitly forbidden. (b) Two dataclasses (legacy / v3) — rejected, overkill for two boolean/int flags.

**Impact.** Rollback in prod is one-line: drop the explicit `recap_enabled=True` or `debounce_window_s=10` from `FlowDeps.default()` and the feature reverts to Phase-2 behavior. Useful for fast post-deploy rollback without a git revert.

---

## 2026-04-16 (Session 3) — Debounce flush via piggyback on next inbound, no APScheduler yet

**Decision.** The debounce flush mechanism does NOT use a separate scheduler (APScheduler / Celery / cron). Instead, `_maybe_flush_debounce(user_id, phone, deps)` is called at the top of every `handle_inbound`, and flushes pending-but-aged voices for that user only. A user who sends two vocals then goes silent will have their queue flushed on the NEXT inbound message (button click, text, voice).

**Rationale.** (a) Railway's hobby tier doesn't make it easy to run a separate always-on worker. (b) The target case is "artisan dictates 3 voices in 30s then taps a menu item" — piggyback handles this cleanly. (c) The edge case (user sends 1 voice then goes silent for an hour) still works: when they eventually return, the first thing they send triggers the flush. The worst-case user experience is "my first voice got stuck; a ping message unblocks it" — acceptable for MVP.

**Alternatives.** (a) APScheduler with a 5s tick — higher operational cost. (b) Redis with a delayed-job queue — net new infra. Both deferred; filed in `BACKLOG.md` for post-MVP.

**Impact.** The code has a stable interface (`_maybe_flush_debounce`) that can be upgraded to an APScheduler hook without API changes when we add it.

---

## 2026-04-16 (Session 3) — Phase 3 shipped as one coherent Builder diff, not 5 parallel Builders

**Decision.** Although `V3_BRIEFS/2_PHASE_SPECS.md` §Phase 3 says "each of the 5 upgrades is a separately testable unit; can be built by 5 parallel Builders", Session 3 used ONE Builder sub-agent delivering all 5 features in a single coherent diff, followed by ONE Reviewer + ONE QA + one QA gap-fill. This differs from the brief's suggested fan-out.

**Rationale.** All 5 upgrades touch `whatsapp/flows.py`. Five parallel Builders would produce five diffs targeting the same file with near-certain merge conflicts, and the Integrator would burn more context resolving conflicts than a single Builder would spend writing the consistent whole. For features that share state (e.g. debounce + recap both persist into `ConversationState.data`), a single author gets internal coherence for free.

**Alternatives.** (a) 5 parallel Builders with feature-branch-style sequential merges — considered; estimated 2–3x the Integrator context cost. (b) Split "debounce" (isolated module) from the 4 others that touch flows.py — would have been reasonable; went with the simpler "one Builder" pattern to make the timing predictable.

**Impact.** Documented here so future sessions don't over-index on the brief's parallelism suggestion when the underlying file graph doesn't justify it. For Phase 4+ features that genuinely spread across multiple files, reassess.

---

## 2026-04-16 (Session 3) — Opus gap-audit caught thin debounce test coverage

**Decision.** After the first QA sub-agent reported 33 tests passing, Opus manually re-read its spec and noticed it asked for 4 debounce-window behavior tests but QA wrote only 1 (plus the 14 debounce unit tests were a different layer). Opus spawned a SECOND focused QA sub-agent to fill the 3 missing tests (concatenation-merge, 15s-separate, maybe_flush noop).

**Rationale.** The "quality gate" in the brief is "100% of declared tests pass". If the QA sub-agent silently dropped 3 of the declared tests, the gate would pass on fewer tests than contracted — a hidden quality hit. Reading the sub-agent's report critically (not just the pass count) catches this. Result: 373 → 373 passes with complete coverage of the 4 declared debounce behaviors.

**Impact.** For future sessions: the orchestrator must cross-check a QA sub-agent's test inventory against the original spec, not just trust the final pass count. Added to mental checklist for Session 4+.
