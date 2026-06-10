"""Unit tests for V3/core/edit_flow.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

V3_ROOT = Path(__file__).resolve().parents[2]
if str(V3_ROOT) not in sys.path:
    sys.path.insert(0, str(V3_ROOT))

from core import edit_flow  # noqa: E402


def _mock_llm(content: str):
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=content))]
    )
    return client


def _sample_devis():
    return {
        "meta": {"numero_devis": "DEVIS-20260414-ABCDEF"},
        "artisan": {"raison_sociale": "Plomberie X"},
        "client": {"nom": "Mme Y"},
        "lignes": [
            {"poste": 1, "description": "Main d'œuvre", "quantite": 1,
             "prix_unitaire_ht": 350.0, "unite": "jour", "taux_tva": 0.10},
            {"poste": 2, "description": "Déplacement", "quantite": 1,
             "prix_unitaire_ht": 40.0, "unite": "jour", "taux_tva": 0.10},
        ],
        "conditions": {"acompte_pourcentage": 30},
        "flags": [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# apply_patch — one test per mutation type
# ─────────────────────────────────────────────────────────────────────────────


class TestApplyPatch:
    def test_prix_unitaire(self):
        out = edit_flow.apply_patch(
            _sample_devis(),
            [{"type": "prix_unitaire", "poste_index": 0, "nouvelle_valeur": 500}],
        )
        assert out["lignes"][0]["prix_unitaire_ht"] == 500.0

    def test_quantite(self):
        out = edit_flow.apply_patch(
            _sample_devis(),
            [{"type": "quantite", "poste_index": 0, "nouvelle_valeur": 2.5}],
        )
        assert out["lignes"][0]["quantite"] == 2.5

    def test_description(self):
        out = edit_flow.apply_patch(
            _sample_devis(),
            [{"type": "description", "poste_index": 0, "nouvelle_valeur": "New desc"}],
        )
        assert out["lignes"][0]["description"] == "New desc"

    def test_detail_ligne(self):
        out = edit_flow.apply_patch(
            _sample_devis(),
            [{"type": "detail_ligne", "poste_index": 0, "nouvelle_valeur": "Techn."}],
        )
        assert out["lignes"][0]["detail"] == "Techn."

    def test_unite_ligne(self):
        out = edit_flow.apply_patch(
            _sample_devis(),
            [{"type": "unite_ligne", "poste_index": 0, "nouvelle_valeur": "heure"}],
        )
        assert out["lignes"][0]["unite"] == "heure"

    def test_taux_tva_global(self):
        out = edit_flow.apply_patch(
            _sample_devis(), [{"type": "taux_tva_global", "nouvelle_valeur": 0.20}]
        )
        assert all(l["taux_tva"] == 0.20 for l in out["lignes"])

    def test_taux_tva_global_zero_sets_tva_note(self):
        out = edit_flow.apply_patch(
            _sample_devis(), [{"type": "taux_tva_global", "nouvelle_valeur": 0.0}]
        )
        assert out["artisan"]["tva_intracom"].startswith("Non assujetti")
        assert out["mentions_legales"]["tva_note"].startswith("Non assujetti")

    def test_taux_tva_ligne(self):
        out = edit_flow.apply_patch(
            _sample_devis(),
            [{"type": "taux_tva_ligne", "poste_index": 1, "nouvelle_valeur": 0.055}],
        )
        assert out["lignes"][1]["taux_tva"] == 0.055
        assert out["lignes"][0]["taux_tva"] == 0.10

    def test_acompte_pourcentage(self):
        out = edit_flow.apply_patch(
            _sample_devis(), [{"type": "acompte_pourcentage", "nouvelle_valeur": 50}]
        )
        assert out["conditions"]["acompte_pourcentage"] == 50

    def test_ajouter_ligne(self):
        out = edit_flow.apply_patch(
            _sample_devis(),
            [
                {
                    "type": "ajouter_ligne",
                    "ligne": {
                        "description": "Nettoyage",
                        "quantite": 1,
                        "prix_unitaire_ht": 80,
                        "taux_tva": 0.10,
                    },
                }
            ],
        )
        assert len(out["lignes"]) == 3
        assert out["lignes"][-1]["description"] == "Nettoyage"
        assert out["lignes"][-1]["montant_ht"] == 80.0

    def test_supprimer_ligne(self):
        out = edit_flow.apply_patch(
            _sample_devis(), [{"type": "supprimer_ligne", "poste_index": 1}]
        )
        assert len(out["lignes"]) == 1
        assert out["lignes"][0]["description"] == "Main d'œuvre"

    def test_client_fields(self):
        out = edit_flow.apply_patch(
            _sample_devis(),
            [
                {
                    "type": "client",
                    "champs": {"nom": "Dupont", "telephone": "0601020304", "email": "null"},
                }
            ],
        )
        assert out["client"]["nom"] == "Dupont"
        assert out["client"]["telephone"] == "0601020304"
        # "null" string is ignored
        assert "email" not in out["client"]

    def test_delai_realisation(self):
        out = edit_flow.apply_patch(
            _sample_devis(),
            [{"type": "delai_realisation", "nouvelle_valeur": "5 jours"}],
        )
        assert out["conditions"]["delai_realisation"] == "5 jours"

    def test_reference_chantier(self):
        out = edit_flow.apply_patch(
            _sample_devis(),
            [{"type": "reference_chantier", "nouvelle_valeur": "Salle de bain"}],
        )
        assert out["meta"]["reference_chantier"] == "Salle de bain"

    def test_modalites_paiement(self):
        out = edit_flow.apply_patch(
            _sample_devis(),
            [{"type": "modalites_paiement", "nouvelle_valeur": "Virement seulement"}],
        )
        assert out["conditions"]["modalites_paiement"] == "Virement seulement"

    def test_unknown_mutation_is_logged_not_crashed(self, caplog):
        with caplog.at_level("WARNING"):
            out = edit_flow.apply_patch(
                _sample_devis(),
                [{"type": "do_the_hokey_pokey", "nouvelle_valeur": 42}],
            )
        assert "Unknown patch type" in caplog.text
        # devis unchanged apart from deepcopy
        assert len(out["lignes"]) == 2

    def test_out_of_bounds_index_is_ignored(self):
        out = edit_flow.apply_patch(
            _sample_devis(),
            [{"type": "prix_unitaire", "poste_index": 99, "nouvelle_valeur": 1}],
        )
        assert out["lignes"][0]["prix_unitaire_ht"] == 350.0

    def test_empty_patch_list_returns_deepcopy(self):
        original = _sample_devis()
        out = edit_flow.apply_patch(original, [])
        assert out == original
        assert out is not original


# ─────────────────────────────────────────────────────────────────────────────
# edit_quote — TVA-zero shortcut
# ─────────────────────────────────────────────────────────────────────────────


class TestEditQuoteTvaZero:
    def test_shortcut_skips_llm(self):
        devis = _sample_devis()
        client = MagicMock()  # should NOT be called
        out = edit_flow.edit_quote(
            devis,
            "je ne suis pas assujetti à la TVA",
            profile={},
            llm_client=client,
            model="m",
        )
        assert all(l["taux_tva"] == 0.0 for l in out["lignes"])
        assert out["meta"]["numero_devis"] == "DEVIS-20260414-ABCDEF"
        client.chat.completions.create.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# edit_quote — LLM patch path
# ─────────────────────────────────────────────────────────────────────────────


class TestEditQuoteLlmPath:
    def test_applies_patch_and_recomputes(self):
        patch_json = json.dumps({
            "modifications": [
                {"type": "quantite", "poste_index": 0, "nouvelle_valeur": 2}
            ]
        })
        client = _mock_llm(patch_json)
        devis = _sample_devis()
        out = edit_flow.edit_quote(
            devis,
            "change main d'œuvre à 2 jours",
            profile={},
            llm_client=client,
            model="m",
        )
        assert out["lignes"][0]["quantite"] == 2.0
        assert out["lignes"][0]["montant_ht"] == 700.0
        # totals recomputed
        assert out["totaux"]["sous_total_ht"] == 700 + 40
        assert out["meta"]["numero_devis"] == "DEVIS-20260414-ABCDEF"

    def test_bad_json_produces_no_op_patch(self):
        client = _mock_llm("not a json")
        devis = _sample_devis()
        out = edit_flow.edit_quote(
            devis, "change something", profile={}, llm_client=client, model="m"
        )
        # Totals still recomputed (from the unchanged devis)
        assert out["totaux"]["sous_total_ht"] == 390.0
        assert out["lignes"][0]["quantite"] == 1
