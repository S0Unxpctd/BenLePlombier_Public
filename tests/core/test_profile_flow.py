"""Unit tests for V3/core/profile_flow.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

V3_ROOT = Path(__file__).resolve().parents[2]
if str(V3_ROOT) not in sys.path:
    sys.path.insert(0, str(V3_ROOT))

from core import profile_flow  # noqa: E402


class TestApplyFieldUpdate:
    def test_string_field(self):
        out = profile_flow.apply_field_update({}, "raison_sociale", "  Plomberie Dupont  ")
        assert out["raison_sociale"] == "Plomberie Dupont"

    def test_siret_strips_spaces(self):
        out = profile_flow.apply_field_update({}, "siret", "123 456 789 01234")
        assert out["siret"] == "12345678901234"

    def test_iban_strips_spaces(self):
        out = profile_flow.apply_field_update({}, "iban", "FR76 1234 5678 9012")
        assert out["iban"] == "FR7612345678 9012".replace(" ", "")

    def test_gmail_app_password_strips_spaces(self):
        out = profile_flow.apply_field_update({}, "gmail_app_password", "abcd efgh ijkl mnop")
        assert out["gmail_app_password"] == "abcdefghijklmnop"

    def test_tarif_numeric(self):
        out = profile_flow.apply_field_update({}, "journee_standard_ht", "350,00 €")
        assert out["journee_standard_ht"] == 350.0

    def test_tarif_invalid_raises(self):
        with pytest.raises(ValueError):
            profile_flow.apply_field_update({}, "journee_standard_ht", "abc")

    def test_deepcopy_no_mutation(self):
        original = {"raison_sociale": "X"}
        out = profile_flow.apply_field_update(original, "telephone", "0601020304")
        assert original == {"raison_sociale": "X"}
        assert out["telephone"] == "0601020304"


class TestOnboardingSteps:
    def test_has_all_critical_fields(self):
        fields = {step.field for step in profile_flow.ONBOARDING_STEPS}
        required = {
            "raison_sociale", "siret", "adresse", "telephone", "email",
            "assurance_decennale", "numero_police", "iban", "tva_intracom",
            "journee_standard_ht", "gmail_address", "gmail_app_password",
        }
        missing = required - fields
        assert not missing, f"Missing onboarding steps: {missing}"

    def test_prompts_are_french(self):
        for step in profile_flow.ONBOARDING_STEPS:
            # Rough heuristic: must contain at least one common French word/char.
            assert any(ch in step.prompt_fr for ch in "àâéèêîôùûçÀÂÉÈÊÎÔÙÛÇ?") or \
                   any(w in step.prompt_fr.lower() for w in ("votre", "vos", "entreprise", "tarif"))


class TestSummarizeProfile:
    def test_renders_known_fields(self):
        summary = profile_flow.summarize_profile(
            {
                "raison_sociale": "Plomberie Dupont",
                "siret": "12345678901234",
                "adresse": "Paris",
                "telephone": "0601020304",
                "email": "pro@artisan.fr",
                "assurance_decennale": "AXA",
                "numero_police": "POL123",
                "iban": "FR76...",
                "tva_intracom": "Non assujetti",
                "journee_standard_ht": 400,
            }
        )
        assert "Plomberie Dupont" in summary
        assert "AXA" in summary
        assert "POL123" in summary
        assert "400" in summary

    def test_handles_missing_fields_gracefully(self):
        summary = profile_flow.summarize_profile({})
        assert "—" in summary


# ─────────────────────────────────────────────────────────────────────────────
# CRUD thin wrappers — patched with monkeypatch so they don't need Postgres
# ─────────────────────────────────────────────────────────────────────────────


class TestCRUDDelegation:
    def test_load_profile_delegates(self, monkeypatch):
        calls = []

        def fake_get(user_id):
            calls.append(user_id)
            return {"raison_sociale": "X"}

        # client_store is imported lazily inside the function
        import client_store

        monkeypatch.setattr(client_store, "get_client", fake_get)
        assert profile_flow.load_profile(42) == {"raison_sociale": "X"}
        assert calls == [42]

    def test_save_profile_delegates(self, monkeypatch):
        captured = {}

        def fake_save(user_id, profile):
            captured["args"] = (user_id, profile)

        import client_store

        monkeypatch.setattr(client_store, "save_client", fake_save)
        profile_flow.save_profile(7, {"k": "v"})
        assert captured["args"] == (7, {"k": "v"})

    def test_profile_exists_delegates(self, monkeypatch):
        import client_store

        monkeypatch.setattr(client_store, "client_exists", lambda uid: uid == 1)
        assert profile_flow.profile_exists(1) is True
        assert profile_flow.profile_exists(2) is False

    def test_new_empty_profile_delegates(self):
        prof = profile_flow.new_empty_profile()
        # client_store.empty_profile() returns a dict with these keys
        for k in ("raison_sociale", "siret", "adresse", "telephone", "email"):
            assert k in prof
