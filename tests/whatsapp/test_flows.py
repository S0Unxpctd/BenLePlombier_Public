"""QA contract items 4, 6, 7, 8, 9, 10 — flow state transitions + end-to-end.

We build a fully-mocked ``FlowDeps`` so no network / Postgres / WeasyPrint /
OpenAI is needed. Each test covers one slice of the Phase 2 feature set.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from core.state_store import InMemoryStateStore, CHANNEL_WHATSAPP
from whatsapp.flows import FlowDeps, handle_inbound
from whatsapp.flows import (
    BTN_YES_CLIENT,
    BTN_NO_CLIENT,
    BTN_AI_DECIDES,
    BTN_AI_DECIDES_ALL,
    BTN_EDIT,
    BTN_SEND,
    BTN_INVOICE,
    BTN_FACT_ACOMPTE,
    BTN_FACT_INTER,
    BTN_FACT_SOLDE,
)
from whatsapp.router import InboundMessage


# ─────────────────────────────────────────────────────────────────────────────
# In-memory client store + devis store
# ─────────────────────────────────────────────────────────────────────────────


class InMemoryClientStore:
    """Minimal stand-in for ``client_store`` module."""

    def __init__(self):
        self.profiles: dict[int, dict] = {}

    def get_client(self, uid):
        return self.profiles.get(uid)

    def save_client(self, uid, profile):
        self.profiles[uid] = dict(profile)

    def client_exists(self, uid):
        return uid in self.profiles

    def empty_profile(self):
        return {}

    def inject_profile_in_prompt(self, prompt, profile):
        return prompt  # not exercised in these tests


class InMemoryDevisStore:
    def __init__(self):
        self.devis: dict[tuple, dict] = {}
        self.factures: dict[tuple, dict] = {}

    def save_devis(self, uid, devis):
        self.devis[(uid, devis["meta"]["numero_devis"])] = devis
        return devis["meta"]["numero_devis"]

    def load_devis(self, uid, numero):
        return self.devis.get((uid, numero))

    def list_devis(self, uid, limit=5):
        items = [
            {"numero": k[1], "data": v}
            for k, v in self.devis.items()
            if k[0] == uid
        ]
        return items[-limit:][::-1]

    def save_facture(self, uid, facture):
        self.factures[(uid, facture["meta"]["numero_facture"])] = facture

    def load_facture(self, uid, numero):
        return self.factures.get((uid, numero))

    def get_factures_for_devis(self, uid, numero_devis):
        return [
            v for k, v in self.factures.items()
            if k[0] == uid and v.get("meta", {}).get("numero_devis_origine") == numero_devis
        ]

    def get_total_deja_facture(self, uid, numero_devis):
        return sum(
            f.get("facturation", {}).get("montant_cette_facture_ttc", 0)
            for k, f in self.factures.items()
            if k[0] == uid and f.get("meta", {}).get("numero_devis_origine") == numero_devis
        )


# ─────────────────────────────────────────────────────────────────────────────
# LLM + Whisper fakes
# ─────────────────────────────────────────────────────────────────────────────


class FakeChoice:
    def __init__(self, content):
        self.message = type("m", (), {"content": content})()


class FakeResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeLLM:
    """Sequential responder: pops from ``scripted`` FIFO."""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.chat = self
        self.completions = self
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.scripted:
            raise AssertionError("FakeLLM ran out of scripted responses")
        payload = self.scripted.pop(0)
        return FakeResponse(payload)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


PHONE = "+33600000000"
# phone_to_user_id("+33600000000") is deterministic; read it once at test load.
from whatsapp.users import phone_to_user_id
USER_ID = phone_to_user_id(PHONE)


@pytest.fixture
def client_store(monkeypatch):
    s = InMemoryClientStore()
    # Patch the real client_store module functions that flows imports directly.
    import client_store as _real_cs

    monkeypatch.setattr(_real_cs, "get_client", s.get_client)
    monkeypatch.setattr(_real_cs, "save_client", s.save_client)
    monkeypatch.setattr(_real_cs, "client_exists", s.client_exists)
    monkeypatch.setattr(_real_cs, "empty_profile", s.empty_profile)
    monkeypatch.setattr(_real_cs, "inject_profile_in_prompt", s.inject_profile_in_prompt)
    # Also patch system_prompt import target
    import system_prompt as _sp

    monkeypatch.setattr(_sp, "SYSTEM_PROMPT", "(stub system prompt)")
    return s


@pytest.fixture
def devis_store():
    return InMemoryDevisStore()


@pytest.fixture
def recorder():
    """Records calls to the send_* / upload_media / generate_*_pdf primitives."""
    calls: list[tuple[str, tuple, dict]] = []

    class Rec:
        def _fn(self, name):
            def wrapper(*a, **k):
                calls.append((name, a, k))
                if name.startswith("send_"):
                    return {"messages": [{"id": f"wamid.{name}.{len(calls)}"}]}
                if name == "upload_media":
                    return f"media.{len(calls)}"
                if name.startswith("generate_"):
                    # simulate writing the PDF
                    path = a[1] if len(a) > 1 else k.get("output_path", "/tmp/x.pdf")
                    with open(path, "wb") as f:
                        f.write(b"%PDF-fake")
                    return path
                return None

            return wrapper

        def __getattr__(self, name):
            return self._fn(name)

    return Rec(), calls


@pytest.fixture
def deps(tmp_path, client_store, devis_store, recorder):
    rec, _calls = recorder
    # Default LLM produces an empty quick_extract (no client, no lines)
    # so the pre-devis flow jumps straight to generation if needed.
    llm = FakeLLM(scripted=[])
    whisper = MagicMock()

    # devis_to_facture_typed reference — delegate to the real one
    from facture_generator import devis_to_facture_typed as real_typed

    def transcribe(_path):
        return "remplacement robinet cuisine, 1h, tarif 150 euros"

    return FlowDeps(
        store=InMemoryStateStore(),
        llm_client=llm,
        whisper_client=whisper,
        model="stub-model",
        system_prompt_fn=lambda _uid: "(stub)",
        transcribe=transcribe,
        pdf_output_dir=str(tmp_path),
        cursor_factory=None,
        send_text=rec.send_text,
        send_buttons=rec.send_reply_buttons,
        send_list=rec.send_list_message,
        send_document=rec.send_document,
        upload_media=rec.upload_media,
        download_media=MagicMock(),
        generate_devis_pdf=rec.generate_devis_pdf,
        generate_facture_pdf=rec.generate_facture_pdf,
        save_devis_fn=devis_store.save_devis,
        load_devis_fn=devis_store.load_devis,
        list_devis_fn=devis_store.list_devis,
        save_facture_fn=devis_store.save_facture,
        load_facture_fn=devis_store.load_facture,
        get_factures_for_devis_fn=devis_store.get_factures_for_devis,
        get_total_deja_facture_fn=devis_store.get_total_deja_facture,
        devis_to_facture_typed_fn=real_typed,
        send_devis_email_fn=lambda *a, **k: True,
        inject_profile_fn=lambda p, pr: p,
    )


def _inbound(msg_type, payload, *, phone=PHONE, uid=USER_ID) -> InboundMessage:
    return InboundMessage(
        phone_e164=phone, user_id=uid, wamid=f"wamid.test.{msg_type}",
        msg_type=msg_type, payload=payload, profile_name="Test",
    )


def _texts_sent(calls):
    return [c[1][1] for c in calls if c[0] == "send_text"]


# ─────────────────────────────────────────────────────────────────────────────
# State store transition tests (QA item 4)
# ─────────────────────────────────────────────────────────────────────────────


class TestStateTransitions:
    """Asserting the conversational state machine moves where it should."""

    def test_new_user_gets_onboarding(self, deps, recorder):
        _rec, calls = recorder
        # No profile yet → any text routes to onboarding
        handle_inbound(_inbound("text", {"body": "bonjour"}), deps=deps)
        state = deps.store.get(USER_ID)
        assert state is not None
        assert state.flow == "onboarding"
        assert state.data["onboarding_step_idx"] == 0
        assert any("Bienvenue" in t for t in _texts_sent(calls))

    def test_onboarding_answer_advances_step(self, deps, client_store):
        handle_inbound(_inbound("text", {"body": "first msg"}), deps=deps)
        handle_inbound(_inbound("text", {"body": "Plomberie Dupont"}), deps=deps)
        state = deps.store.get(USER_ID)
        # step 0 answered → idx advanced to 1, profile gained raison_sociale
        assert state.data["onboarding_step_idx"] == 1
        assert client_store.profiles[USER_ID]["raison_sociale"] == "Plomberie Dupont"

    def test_onboarding_skip_advances_without_writing(self, deps, client_store):
        handle_inbound(_inbound("text", {"body": "first"}), deps=deps)
        handle_inbound(_inbound("text", {"body": "passer"}), deps=deps)
        state = deps.store.get(USER_ID)
        assert state.data["onboarding_step_idx"] == 1
        # profile exists but raison_sociale not set
        assert "raison_sociale" not in (client_store.profiles.get(USER_ID) or {})

    def test_onboarding_invalid_numeric_reprompts(self, deps, client_store, recorder):
        """The tarif field is numeric; passing text should trigger an error."""
        _rec, calls = recorder
        # Fast-forward to the tarif step: answer step 0-8 with arbitrary strings
        from core.profile_flow import ONBOARDING_STEPS

        tarif_idx = next(
            i for i, s in enumerate(ONBOARDING_STEPS) if s.field == "journee_standard_ht"
        )
        # First inbound starts onboarding
        handle_inbound(_inbound("text", {"body": "hi"}), deps=deps)
        for _ in range(tarif_idx):
            handle_inbound(_inbound("text", {"body": "something"}), deps=deps)
        before_state = deps.store.get(USER_ID)
        assert before_state.data["onboarding_step_idx"] == tarif_idx
        # Now send non-numeric text to the tarif step
        pre_calls = len(calls)
        handle_inbound(_inbound("text", {"body": "pas un nombre"}), deps=deps)
        after_state = deps.store.get(USER_ID)
        # Did NOT advance; warning was sent
        assert after_state.data["onboarding_step_idx"] == tarif_idx
        new_msgs = [t for (n, _a, _k) in calls[pre_calls:] for t in ([_a[1]] if n == "send_text" else [])]
        assert any("Valeur invalide" in t for t in new_msgs)

    def test_cancel_clears_transient_flow_state(self, deps, client_store):
        # Fake a profile
        client_store.profiles[USER_ID] = {"raison_sociale": "X"}
        # Seed state with editing_devis
        deps.store.update(
            USER_ID, CHANNEL_WHATSAPP,
            lambda s: s.data.update({"editing_devis": "DEVIS-123"}),
        )
        handle_inbound(_inbound("text", {"body": "annuler"}), deps=deps)
        state = deps.store.get(USER_ID)
        assert "editing_devis" not in (state.data if state else {})

    def test_recommencer_wipes_state(self, deps, client_store):
        client_store.profiles[USER_ID] = {"raison_sociale": "X"}
        deps.store.update(
            USER_ID, CHANNEL_WHATSAPP,
            lambda s: s.data.update({"pending_transcription": "xx"}),
        )
        handle_inbound(_inbound("text", {"body": "recommencer"}), deps=deps)
        assert deps.store.get(USER_ID) is None

    def test_unknown_type_stays_inactive(self, deps, client_store):
        client_store.profiles[USER_ID] = {"raison_sociale": "X"}
        handle_inbound(_inbound("image", {"id": "mid"}), deps=deps)
        # Should have sent the "not yet supported" text, not changed state.
        assert deps.store.get(USER_ID) is None or deps.store.get(USER_ID).flow is None


# ─────────────────────────────────────────────────────────────────────────────
# Voice → quote (QA item 6) and double voice memo (QA item 10)
# ─────────────────────────────────────────────────────────────────────────────


def _quick_extract_script(client=None, lines=None):
    import json
    return json.dumps({"client": client, "lignes": lines or []})


def _full_devis_script():
    import json
    return json.dumps(
        {
            "artisan": {"raison_sociale": "Plomberie Dupont"},
            "client": {"nom": "M. Dupont"},
            "lignes": [
                {
                    "poste": 1,
                    "description": "Remplacement robinet",
                    "quantite": 1,
                    "unite": "forfait",
                    "prix_unitaire_ht": 150.0,
                    "taux_tva": 0.10,
                }
            ],
            "totaux": {"total_ht": 150.0, "total_ttc": 165.0},
            "conditions": {"acompte_pourcentage": 30},
            "meta": {"reference_chantier": "RDC"},
        }
    )


class TestVoiceToQuote:
    def test_full_chain_produces_pdf_and_buttons(self, deps, client_store, recorder, monkeypatch):
        _rec, calls = recorder
        client_store.profiles[USER_ID] = {"raison_sociale": "Plomberie"}
        # Patch download_media to be a no-op ctx manager
        from contextlib import contextmanager

        @contextmanager
        def fake_download(_mid, **_kw):
            yield "/tmp/fake.ogg"

        deps.download_media = fake_download
        # quick_extract returns client → skip the client question.
        # lignes=[] → jumps straight to generation (no per-line price loop).
        deps.llm_client = FakeLLM([
            _quick_extract_script(client="M. Dupont, Paris", lines=[]),
            _full_devis_script(),
        ])

        handle_inbound(_inbound("audio", {"id": "mid-1"}), deps=deps)

        names = [c[0] for c in calls]
        # At a minimum: transcription ack, then since no client & no lines, it
        # generates directly and sends a document.
        assert "send_text" in names  # ack and/or "Génération…"
        assert "upload_media" in names
        assert "send_document" in names
        # The buttons message comes after the document
        button_calls = [c for c in calls if c[0] == "send_reply_buttons"]
        assert button_calls, "expected a reply_buttons call after sending devis"
        btn_ids = [b["id"] for b in button_calls[-1][1][2]]
        assert btn_ids == [BTN_EDIT, BTN_SEND, BTN_INVOICE]

    def test_client_extracted_skips_client_question(self, deps, client_store, recorder, monkeypatch):
        _rec, calls = recorder
        client_store.profiles[USER_ID] = {"raison_sociale": "Plomberie"}
        from contextlib import contextmanager

        @contextmanager
        def fake_download(_mid, **_kw):
            yield "/tmp/fake.ogg"

        deps.download_media = fake_download
        deps.llm_client = FakeLLM([
            _quick_extract_script(client="M. Lemaire, Paris", lines=[]),
            _full_devis_script(),
        ])
        handle_inbound(_inbound("audio", {"id": "mid-1"}), deps=deps)
        # Client was already known → must NOT have asked the client question.
        button_calls = [c for c in calls if c[0] == "send_reply_buttons"]
        # The only buttons sent should be the devis buttons at the end.
        for c in button_calls:
            ids = {b["id"] for b in c[1][2]}
            assert BTN_YES_CLIENT not in ids

    def test_empty_transcription_surfaces_error_and_stops(self, deps, client_store, recorder):
        _rec, calls = recorder
        client_store.profiles[USER_ID] = {"raison_sociale": "X"}
        from contextlib import contextmanager

        @contextmanager
        def fake_download(_mid, **_kw):
            yield "/tmp/empty.ogg"

        deps.download_media = fake_download
        deps.transcribe = lambda _p: ""  # empty transcription
        handle_inbound(_inbound("audio", {"id": "mid-1"}), deps=deps)
        assert any("Transcription vide" in t for t in _texts_sent(calls))
        assert not any(c[0] == "send_document" for c in calls)

    def test_two_voice_memos_produce_two_quotes(self, deps, client_store, recorder):
        """QA item 10 — second memo in a row starts a new quote (no debounce at MVP)."""
        _rec, calls = recorder
        client_store.profiles[USER_ID] = {"raison_sociale": "P"}
        from contextlib import contextmanager

        @contextmanager
        def fake_download(_mid, **_kw):
            yield "/tmp/x.ogg"

        deps.download_media = fake_download
        deps.llm_client = FakeLLM([
            _quick_extract_script(client="C1", lines=[]),
            _full_devis_script(),
            _quick_extract_script(client="C2", lines=[]),
            _full_devis_script(),
        ])
        handle_inbound(_inbound("audio", {"id": "mid-1"}), deps=deps)
        handle_inbound(_inbound("audio", {"id": "mid-2"}), deps=deps)
        docs_sent = [c for c in calls if c[0] == "send_document"]
        assert len(docs_sent) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Onboarding end-to-end (QA item 7)
# ─────────────────────────────────────────────────────────────────────────────


class TestOnboardingEnd2End:
    def test_full_12_step_walk(self, deps, client_store):
        from core.profile_flow import ONBOARDING_STEPS

        # First inbound — triggers onboarding
        handle_inbound(_inbound("text", {"body": "bonjour"}), deps=deps)
        # Provide an answer for every step — for numeric ones, use a number.
        answers = {
            "raison_sociale": "Plomberie Dupont",
            "siret": "123 456 789 00012",
            "adresse": "12 rue des Lilas, 75001 Paris",
            "telephone": "+33 6 12 34 56 78",
            "email": "dupont@example.fr",
            "assurance_decennale": "Allianz",
            "numero_police": "AZ-2024-001",
            "iban": "FR76 3000 1000 0100 0000 0001 234",
            "tva_intracom": "Non assujetti à la TVA",
            "journee_standard_ht": "350",
            "gmail_address": "dupont@gmail.com",
            "gmail_app_password": "abcd efgh ijkl mnop",
        }
        for step in ONBOARDING_STEPS:
            handle_inbound(
                _inbound("text", {"body": answers[step.field]}), deps=deps
            )

        profile = client_store.profiles.get(USER_ID, {})
        assert profile["raison_sociale"] == "Plomberie Dupont"
        assert profile["siret"] == "123456789 00012".replace(" ", "")
        assert profile["telephone"] == "+33 6 12 34 56 78"
        assert profile["journee_standard_ht"] == 350.0
        assert profile["iban"] == "FR7630001000010000000001234"
        # Onboarding state cleared on completion
        state = deps.store.get(USER_ID)
        if state is not None:
            assert "onboarding_step_idx" not in state.data


# ─────────────────────────────────────────────────────────────────────────────
# Edit flow (QA item 8)
# ─────────────────────────────────────────────────────────────────────────────


def _seed_devis(devis_store, uid, numero="DEVIS-TEST-001", total_ttc=1000.0):
    d = {
        "artisan": {"raison_sociale": "P"},
        "client": {"nom": "C"},
        "lignes": [
            {
                "poste": 1,
                "description": "Main d'œuvre",
                "quantite": 1,
                "unite": "forfait",
                "prix_unitaire_ht": 800.0,
                "taux_tva": 0.10,
                "montant_ht": 800.0,
            }
        ],
        "totaux": {"total_ht": 800.0, "total_ttc": total_ttc},
        "conditions": {"acompte_pourcentage": 30},
        "meta": {"numero_devis": numero, "reference_chantier": "X"},
    }
    devis_store.save_devis(uid, d)
    return d


class TestEditFlow:
    def test_edit_button_sets_editing_state(self, deps, client_store, devis_store, recorder):
        _rec, _calls = recorder
        client_store.profiles[USER_ID] = {"raison_sociale": "P"}
        _seed_devis(devis_store, USER_ID)
        # Simulate clicking the Edit button
        handle_inbound(
            _inbound(
                "interactive",
                {"type": "button_reply", "button_reply": {"id": BTN_EDIT, "title": "Modifier"}},
            ),
            deps=deps,
        )
        state = deps.store.get(USER_ID)
        assert state is not None
        assert state.data["editing_devis"] == "DEVIS-TEST-001"

    def test_tva_zero_shortcut_skips_llm(self, deps, client_store, devis_store, recorder):
        """Per core.edit_flow, a TVA=0 modification uses the deterministic path
        and does NOT call the LLM."""
        _rec, calls = recorder
        client_store.profiles[USER_ID] = {"raison_sociale": "P"}
        _seed_devis(devis_store, USER_ID)
        deps.store.update(
            USER_ID, CHANNEL_WHATSAPP,
            lambda s: s.data.update({"editing_devis": "DEVIS-TEST-001"}),
        )
        deps.llm_client = FakeLLM([])  # empty — any LLM call raises
        handle_inbound(_inbound("text", {"body": "Je ne suis pas assujetti à la TVA"}), deps=deps)
        # Document was sent → TVA path ran
        assert any(c[0] == "send_document" for c in calls)
        # No LLM calls since FakeLLM had empty script; it would have raised
        assert deps.llm_client.calls == []

    def test_edit_with_patch_changes_devis(self, deps, client_store, devis_store, recorder):
        import json
        _rec, _calls = recorder
        client_store.profiles[USER_ID] = {"raison_sociale": "P"}
        _seed_devis(devis_store, USER_ID, total_ttc=1000.0)
        deps.store.update(
            USER_ID, CHANNEL_WHATSAPP,
            lambda s: s.data.update({"editing_devis": "DEVIS-TEST-001"}),
        )
        patch_json = json.dumps({
            "modifications": [
                {"type": "prix_unitaire", "poste_index": 0, "nouvelle_valeur": 500.0}
            ]
        })
        deps.llm_client = FakeLLM([patch_json])
        handle_inbound(_inbound("text", {"body": "Change le prix de la MO à 500"}), deps=deps)
        new_devis = devis_store.load_devis(USER_ID, "DEVIS-TEST-001")
        assert new_devis["lignes"][0]["prix_unitaire_ht"] == 500.0


# ─────────────────────────────────────────────────────────────────────────────
# Facture flow (QA item 9)
# ─────────────────────────────────────────────────────────────────────────────


class TestFactureFlow:
    def test_acompte_solde_sums_to_total(self, deps, client_store, devis_store, recorder):
        _rec, calls = recorder
        client_store.profiles[USER_ID] = {"raison_sociale": "P"}
        _seed_devis(devis_store, USER_ID, total_ttc=1000.0)
        # Select the devis as the facture target
        deps.store.update(
            USER_ID, CHANNEL_WHATSAPP,
            lambda s: s.data.update({"facture_target_numero": "DEVIS-TEST-001"}),
        )
        # Acompte (30% of 1000 = 300)
        handle_inbound(
            _inbound("interactive", {"type": "button_reply", "button_reply": {"id": BTN_FACT_ACOMPTE, "title": "Acompte"}}),
            deps=deps,
        )
        # Solde (700 remaining)
        deps.store.update(
            USER_ID, CHANNEL_WHATSAPP,
            lambda s: s.data.update({"facture_target_numero": "DEVIS-TEST-001"}),
        )
        handle_inbound(
            _inbound("interactive", {"type": "button_reply", "button_reply": {"id": BTN_FACT_SOLDE, "title": "Solde"}}),
            deps=deps,
        )
        total_factured = sum(
            f.get("facturation", {}).get("montant_cette_facture_ttc", 0)
            for f in devis_store.factures.values()
        )
        assert round(total_factured, 2) == 1000.0


# ─────────────────────────────────────────────────────────────────────────────
# Email flow
# ─────────────────────────────────────────────────────────────────────────────


class TestEmailFlow:
    def test_missing_email_asks_then_sends(self, deps, client_store, devis_store, recorder):
        _rec, calls = recorder
        client_store.profiles[USER_ID] = {
            "raison_sociale": "P",
            "gmail_address": "me@gmail.com",
            "gmail_app_password": "x",
        }
        _seed_devis(devis_store, USER_ID)
        # Click Send button (client email unknown) → asks
        handle_inbound(
            _inbound("interactive", {"type": "button_reply", "button_reply": {"id": BTN_SEND, "title": "Envoyer"}}),
            deps=deps,
        )
        state = deps.store.get(USER_ID)
        assert state.data.get("waiting_for_email") == "devis"
        # Send the email as text
        handle_inbound(_inbound("text", {"body": "client@example.com"}), deps=deps)
        # send_devis_email was called (FlowDeps stub returns True) → success message
        assert any("envoyé" in t for t in _texts_sent(calls))

    def test_invalid_email_reprompts(self, deps, client_store, devis_store, recorder):
        _rec, calls = recorder
        client_store.profiles[USER_ID] = {
            "raison_sociale": "P", "gmail_address": "x", "gmail_app_password": "x",
        }
        _seed_devis(devis_store, USER_ID)
        handle_inbound(
            _inbound("interactive", {"type": "button_reply", "button_reply": {"id": BTN_SEND, "title": "Envoyer"}}),
            deps=deps,
        )
        handle_inbound(_inbound("text", {"body": "not-an-email"}), deps=deps)
        assert any("Email invalide" in t for t in _texts_sent(calls))


# ─────────────────────────────────────────────────────────────────────────────
# Regression: "Oui, ajouter" button then free-text client details
# ─────────────────────────────────────────────────────────────────────────────


class TestClickYesClientThenTypeDetails:
    """Reproduces the live bug (2026-04-14): after clicking 'Oui, ajouter',
    the next text message was dropped into the menu-hint fallback because
    ``data["pre_step"]`` wasn't set alongside ``state.step``.
    """

    def test_button_yes_then_text_is_captured_as_client(self, deps, client_store, recorder):
        from contextlib import contextmanager
        _rec, calls = recorder
        client_store.profiles[USER_ID] = {"raison_sociale": "P"}

        @contextmanager
        def fake_download(_mid, **_kw):
            yield "/tmp/fake.ogg"

        deps.download_media = fake_download
        # Voice with 1 line whose price is unknown → the flow lingers in the
        # prix step after capturing the client, so we can inspect the state.
        deps.llm_client = FakeLLM([
            _quick_extract_script(
                client=None,
                lines=[{"description": "MO", "unite": "jour", "quantite": 1}],
            ),
        ])
        handle_inbound(_inbound("audio", {"id": "mid-1"}), deps=deps)

        # User clicks "Oui, ajouter"
        handle_inbound(
            _inbound(
                "interactive",
                {"type": "button_reply", "button_reply": {"id": BTN_YES_CLIENT, "title": "Oui"}},
            ),
            deps=deps,
        )

        # User sends client details as free text
        handle_inbound(
            _inbound("text", {"body": "Mme Carte Magalie\n25 rue De Gaulle, 75004 Paris\n0678899000"}),
            deps=deps,
        )

        # Asserts: client captured, moved into prix step, no menu-hint fallback
        state = deps.store.get(USER_ID)
        assert state is not None, "state must exist after the sequence"
        assert state.data.get("pre_extra", {}).get("client", "").startswith("Mme Carte"), (
            f"client not captured — state.data={state.data!r}"
        )
        menu_hints = [t for t in _texts_sent(calls) if "tapez *menu*" in t.lower()]
        assert not menu_hints, f"menu hint fired when it shouldn't: {menu_hints!r}"
