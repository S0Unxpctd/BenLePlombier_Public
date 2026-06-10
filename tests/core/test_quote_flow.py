"""Unit tests for V3/core/quote_flow.py.

Covers every public helper + the generate_quote end-to-end path, using a
fake LLM client so no network calls are made.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

V3_ROOT = Path(__file__).resolve().parents[2]
if str(V3_ROOT) not in sys.path:
    sys.path.insert(0, str(V3_ROOT))

from core import quote_flow  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fake LLM client
# ─────────────────────────────────────────────────────────────────────────────


def _mock_llm(response_text: str):
    """Build a MagicMock llm_client.chat.completions.create() that returns
    an object with `.choices[0].message.content == response_text`.
    """
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=response_text))]
    )
    return client


# ─────────────────────────────────────────────────────────────────────────────
# parse_llm_json
# ─────────────────────────────────────────────────────────────────────────────


class TestParseLlmJson:
    def test_raw_json(self):
        assert quote_flow.parse_llm_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        raw = "```json\n{\"a\": 1}\n```"
        assert quote_flow.parse_llm_json(raw) == {"a": 1}

    def test_fenced_plain(self):
        raw = "```\n{\"a\": 1}\n```"
        assert quote_flow.parse_llm_json(raw) == {"a": 1}

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            quote_flow.parse_llm_json("")
        with pytest.raises(ValueError):
            quote_flow.parse_llm_json(None)

    def test_fence_only_raises(self):
        with pytest.raises(ValueError):
            quote_flow.parse_llm_json("```\n\n```")


# ─────────────────────────────────────────────────────────────────────────────
# build_user_message
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildUserMessage:
    def test_minimal(self):
        now = datetime(2026, 4, 14)
        msg = quote_flow.build_user_message("bonjour", now=now)
        assert "14/04/2026" in msg
        # Validity = now + 90 days → 13/07/2026
        assert "13/07/2026" in msg
        assert "bonjour" in msg
        assert "Réponds uniquement avec le JSON" in msg

    def test_with_extra_client_and_prix_postes(self):
        extra = {
            "client": "Mme Dupont, 12 rue X, Paris",
            "prix_postes": [
                {"description": "Robinet cuisine", "prix_unitaire_ht": 120, "unite": "u"},
            ],
        }
        msg = quote_flow.build_user_message("bonjour", extra, now=datetime(2026, 4, 14))
        assert "Mme Dupont" in msg
        assert "Robinet cuisine" in msg
        assert "PRIX IMPOSÉS" in msg

    def test_ignores_missing_extra_keys(self):
        msg = quote_flow.build_user_message("x", {}, now=datetime(2026, 4, 14))
        assert "Coordonnées client" not in msg
        assert "PRIX IMPOSÉS" not in msg


# ─────────────────────────────────────────────────────────────────────────────
# TVA-zero detection + application
# ─────────────────────────────────────────────────────────────────────────────


class TestTvaZero:
    @pytest.mark.parametrize(
        "text",
        [
            "je ne suis pas assujetti à la TVA",
            "micro-entreprise",
            "auto-entrepreneur",
            "sans TVA",
            "art 293 B",
        ],
    )
    def test_detect_positive(self, text):
        assert quote_flow.detect_tva_zero(text) is True

    def test_detect_negative(self):
        assert quote_flow.detect_tva_zero("TVA 10% applicable") is False

    def test_apply_wipes_tva(self):
        devis = {
            "lignes": [
                {"description": "MO", "quantite": 1, "prix_unitaire_ht": 100, "taux_tva": 0.10}
            ],
            "conditions": {"acompte_pourcentage": 30},
        }
        result = quote_flow.apply_tva_zero(devis)
        assert result["lignes"][0]["taux_tva"] == 0.0
        assert result["totaux"]["total_ttc"] == 100.0
        assert result["artisan"]["tva_intracom"].startswith("Non assujetti")


# ─────────────────────────────────────────────────────────────────────────────
# enrich_artisan
# ─────────────────────────────────────────────────────────────────────────────


class TestEnrichArtisan:
    def test_fills_placeholders(self):
        devis = {
            "artisan": {
                "raison_sociale": "[À COMPLÉTER]",
                "adresse": "[À COMPLÉTER]",
                "siret": "",
            }
        }
        profile = {
            "raison_sociale": "Plomberie Dupont",
            "adresse": "12 rue X, Paris",
            "siret": "12345678901234",
        }
        out = quote_flow.enrich_artisan(devis, profile)
        assert out["artisan"]["raison_sociale"] == "Plomberie Dupont"
        assert out["artisan"]["adresse"] == "12 rue X, Paris"
        assert out["artisan"]["siret"] == "12345678901234"

    def test_preserves_existing_real_values(self):
        devis = {"artisan": {"raison_sociale": "Already set"}}
        out = quote_flow.enrich_artisan(devis, {"raison_sociale": "Different"})
        assert out["artisan"]["raison_sociale"] == "Already set"

    def test_dirigeant_falls_back_to_raison_sociale(self):
        devis = {"artisan": {"dirigeant": "[À COMPLÉTER]"}}
        profile = {"raison_sociale": "Dupont SARL"}
        out = quote_flow.enrich_artisan(devis, profile)
        assert out["artisan"]["dirigeant"] == "Dupont SARL"


# ─────────────────────────────────────────────────────────────────────────────
# remove_empty_lines
# ─────────────────────────────────────────────────────────────────────────────


class TestRemoveEmptyLines:
    def test_drops_zero_priced(self):
        devis = {
            "lignes": [
                {"description": "A", "quantite": 1, "prix_unitaire_ht": 100, "taux_tva": 0.10},
                {"description": "B", "quantite": 1, "prix_unitaire_ht": 0, "taux_tva": 0.10},
                {"description": "C", "quantite": 1, "prix_unitaire_ht": 50, "taux_tva": 0.10},
            ],
            "conditions": {"acompte_pourcentage": 30},
        }
        out = quote_flow.remove_empty_lines(devis)
        descriptions = [l["description"] for l in out["lignes"]]
        assert descriptions == ["A", "C"]
        # Postes renumbered
        assert [l["poste"] for l in out["lignes"]] == [1, 2]

    def test_handles_unparseable_price(self):
        devis = {
            "lignes": [
                {"description": "A", "quantite": 1, "prix_unitaire_ht": "[À COMPLÉTER]", "taux_tva": 0.10},
                {"description": "B", "quantite": 1, "prix_unitaire_ht": 50, "taux_tva": 0.10},
            ]
        }
        out = quote_flow.remove_empty_lines(devis)
        # "[À COMPLÉTER]" → 0 → dropped
        assert len(out["lignes"]) == 1
        assert out["lignes"][0]["description"] == "B"


# ─────────────────────────────────────────────────────────────────────────────
# filter_flags
# ─────────────────────────────────────────────────────────────────────────────


class TestFilterFlags:
    def test_always_drops_legal_flags(self):
        devis = {
            "artisan": {},
            "flags": ["Préciser le médiateur", "Compléter le tribunal compétent"],
        }
        out = quote_flow.filter_flags(devis)
        assert out["flags"] == []

    def test_drops_flag_when_field_is_filled(self):
        devis = {
            "artisan": {"email": "pro@artisan.fr"},
            "flags": ["Ajouter votre e-mail", "Compléter votre téléphone"],
        }
        out = quote_flow.filter_flags(devis)
        # email flag is dropped, téléphone flag stays (téléphone empty on the artisan)
        assert "Ajouter votre e-mail" not in out["flags"]
        assert "Compléter votre téléphone" in out["flags"]

    def test_empty_flags_is_noop(self):
        devis = {"flags": []}
        assert quote_flow.filter_flags(devis) == devis


# ─────────────────────────────────────────────────────────────────────────────
# quick_extract
# ─────────────────────────────────────────────────────────────────────────────


class TestQuickExtract:
    def test_happy_path(self):
        payload = {
            "client": "Mme Leroy",
            "lignes": [
                {"description": "Robinet", "unite": "u", "quantite": 1, "prix_unitaire_ht": 120},
                {"description": "Main d'œuvre", "unite": "forfait", "quantite": 1},
            ],
        }
        client = _mock_llm(json.dumps(payload))
        result = quote_flow.quick_extract("bonjour", client, "mock-model")
        assert result["client"] == "Mme Leroy"
        assert len(result["lignes"]) == 2
        assert result["lignes"][0]["prix_unitaire_ht"] == 120.0
        # Second line has no price
        assert "prix_unitaire_ht" not in result["lignes"][1]

    def test_bad_json_returns_empty(self):
        client = _mock_llm("not json")
        result = quote_flow.quick_extract("x", client, "m")
        assert result == {"client": None, "lignes": []}

    def test_null_client_string(self):
        client = _mock_llm('{"client": "null", "lignes": []}')
        result = quote_flow.quick_extract("x", client, "m")
        assert result["client"] is None

    def test_llm_exception_returns_empty(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("boom")
        result = quote_flow.quick_extract("x", client, "m")
        assert result == {"client": None, "lignes": []}

    def test_skips_lines_without_description(self):
        payload = {
            "client": None,
            "lignes": [
                {"unite": "u", "quantite": 1},  # no description → dropped
                {"description": "Valid", "unite": "forfait", "quantite": 1},
            ],
        }
        client = _mock_llm(json.dumps(payload))
        result = quote_flow.quick_extract("x", client, "m")
        assert len(result["lignes"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# generate_quote — end-to-end with mocked LLM
# ─────────────────────────────────────────────────────────────────────────────


_SAMPLE_DEVIS_JSON = json.dumps({
    "meta": {"reference_chantier": "Dépannage robinet cuisine"},
    "artisan": {
        "raison_sociale": "[À COMPLÉTER]",
        "adresse": "[À COMPLÉTER]",
        "siret": "[À COMPLÉTER]",
    },
    "client": {"nom": "Mme Durand"},
    "lignes": [
        {"poste": 1, "description": "MO", "unite": "forfait", "quantite": 1,
         "prix_unitaire_ht": 150.0, "taux_tva": 0.10},
        {"poste": 2, "description": "Déplacement", "unite": "u", "quantite": 1,
         "prix_unitaire_ht": 40.0, "taux_tva": 0.10},
    ],
    "conditions": {"acompte_pourcentage": 30},
    "flags": ["Préciser le médiateur"],  # will be filtered out
})


class TestGenerateQuote:
    def test_happy_path(self):
        client = _mock_llm(_SAMPLE_DEVIS_JSON)
        profile = {
            "raison_sociale": "Plomberie Dupont",
            "adresse": "Paris",
            "siret": "12345678901234",
            "iban": "FR7612345",
        }
        devis = quote_flow.generate_quote(
            transcription="remplacement robinet cuisine",
            profile=profile,
            extra=None,
            llm_client=client,
            model="mock",
            system_prompt="SYS",
            now=datetime(2026, 4, 14),
        )
        assert devis["meta"]["numero_devis"].startswith("DEVIS-20260414-")
        assert devis["artisan"]["raison_sociale"] == "Plomberie Dupont"
        assert devis["artisan"]["iban"] == "FR7612345"
        assert devis["totaux"]["sous_total_ht"] == 190.0
        # médiateur flag got filtered out.
        assert all("médiateur" not in f.lower() for f in devis.get("flags", []))

    def test_tva_zero_is_applied_deterministically(self):
        client = _mock_llm(_SAMPLE_DEVIS_JSON)
        devis = quote_flow.generate_quote(
            transcription="je suis auto-entrepreneur, pas assujetti",
            profile={"raison_sociale": "X", "adresse": "Y", "siret": "Z"},
            extra=None,
            llm_client=client,
            model="mock",
            system_prompt="SYS",
            now=datetime(2026, 4, 14),
        )
        for ligne in devis["lignes"]:
            assert ligne["taux_tva"] == 0.0
        assert devis["totaux"]["total_ttc"] == 190.0

    def test_error_passthrough(self):
        err_payload = json.dumps({"erreur": "TRANSCRIPTION_INSUFFISANTE", "message": "..."})
        client = _mock_llm(err_payload)
        result = quote_flow.generate_quote(
            transcription="x",
            profile={},
            extra=None,
            llm_client=client,
            model="mock",
            system_prompt="SYS",
        )
        assert "erreur" in result
