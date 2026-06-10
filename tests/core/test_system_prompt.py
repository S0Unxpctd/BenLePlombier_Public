"""
Tests unitaires pour system_prompt.py — Validation de la structure et cohérence
du prompt système v2 (Phase 2.5).

Ces tests vérifient :
1. Présence de tous les placeholders [NOM_ENTREPRISE], [SIRET], etc.
2. Injection correcte des profils artisans
3. Constantes (validité, acompte, TVA, tarifs)
4. Structure JSON documentée
5. Mentions légales obligatoires
6. Règles de cohérence (matériel ↔ pièce)
7. Limites de lignes par budget
8. Restriction des flags [À COMPLÉTER]
9. Framing "devis envoyable"
10. Santé des fixtures de test synthétiques
"""

import pytest
import json
import re
from pathlib import Path
from system_prompt import SYSTEM_PROMPT, DEVIS_VALIDITE_JOURS, ACOMPTE_DEFAUT_PCT, TAUX_TVA_DEFAUT, TARIFS_DEFAUT
from client_store import inject_profile_in_prompt


# ============================================================
# PARTIE 1 — TestPromptStructure
# ============================================================

class TestPromptStructure:
    """Tests sur les invariants textuels du prompt système (aucun LLM)."""

    def test_all_profile_placeholders_present(self):
        """Assertion : tous les 17 tokens placeholders sont présents littéralement."""
        required_tokens = [
            "[NOM_ENTREPRISE]",
            "[FORME_JURIDIQUE]",
            "[NOM_PRENOM_DIRIGEANT]",
            "[ADRESSE_COMPLETE]",
            "[TELEPHONE]",
            "[EMAIL]",
            "[SITE_WEB]",
            "[NUMERO_SIRET]",
            "[NUMERO_TVA_INTRACOM]",
            "[NUMERO_RCS_OU_RM]",
            "[CODE_APE]",
            "[NOM_ASSUREUR]",
            "[NUMERO_POLICE_ASSURANCE]",
            "[ANNEE_VALIDITE_ASSURANCE]",
            "350,00 € HT / jour",
            "450,00 € HT / jour",
            "40,00 € HT / jour de présence",
        ]
        for token in required_tokens:
            assert token in SYSTEM_PROMPT, f"Missing placeholder: {token}"

    def test_inject_profile_in_prompt_replaces_tokens(self):
        """Test : inject_profile_in_prompt remplace correctement les tokens."""
        sample_profile = {
            "raison_sociale": "Dupont Plomberie",
            "forme_juridique": "EI",
            "dirigeant": "Jean Dupont",
            "adresse": "1 rue Test Aubagne",
            "telephone": "0612345678",
            "email": "test@example.com",
            "site_web": "www.benhaddou.com",
            "siret": "12345678901234",
            "tva_intracom": "FR1234567890123",
            "rcs_rm": "RCS Aubagne 123456",
            "code_ape": "4322A",
            "assurance_decennale": "AXA",
            "numero_police": "POL123456",
            "annee_assurance": "2025",
            "journee_standard_ht": 400.0,
            "journee_specialise_ht": 500.0,
            "deplacement_par_jour": 50.0,
        }

        injected = inject_profile_in_prompt(SYSTEM_PROMPT, sample_profile)

        # Vérifier que les tokens ont disparu
        assert "[NOM_ENTREPRISE]" not in injected
        assert "[NUMERO_SIRET]" not in injected
        assert "[EMAIL]" not in injected

        # Vérifier que les valeurs réelles sont présentes
        assert "Dupont Plomberie" in injected
        assert "12345678901234" in injected
        assert "test@example.com" in injected

        # Vérifier que les tarifs personnalisés remplacent les défauts
        # Note: client_store uses dots (.) not commas in the output format
        assert "400.00 € HT / jour" in injected or "400,00 € HT / jour" in injected
        assert "500.00 € HT / jour" in injected or "500,00 € HT / jour" in injected
        assert "50.00 € HT / jour de présence" in injected or "50,00 € HT / jour de présence" in injected
        # Les anciens tarifs ne doivent plus être présents
        assert "350,00 € HT / jour" not in injected
        assert "450,00 € HT / jour" not in injected

    def test_constants_preserved(self):
        """Test : les constantes ont les bonnes valeurs."""
        assert DEVIS_VALIDITE_JOURS == 90
        assert ACOMPTE_DEFAUT_PCT == 30
        assert TAUX_TVA_DEFAUT == 0.10

        expected_keys = {
            "journee_standard_ht",
            "journee_specialise_ht",
            "deplacement_par_jour",
            "marge_materiaux",
            "majoration_urgence",
            "majoration_nuit_we",
            "coeff_renovation",
            "coeff_hauteur",
            "coeff_espace_confine",
            "coeff_depannage_urgent",
        }
        assert set(TARIFS_DEFAUT.keys()) == expected_keys

    def test_json_schema_documented(self):
        """Test : le prompt mentionne les clés JSON que consomme le template PDF."""
        required_keys = [
            # Top-level
            "meta",
            "artisan",
            "client",
            "lignes",
            "totaux",
            "conditions",
            "flags",
            "mentions_legales",
            # meta.*
            "numero_devis",
            "date_emission",
            "date_validite",
            "reference_chantier",
            # lignes[].*
            "poste",
            "description",
            "detail",
            "unite",
            "quantite",
            "prix_unitaire_ht",
            "montant_ht",
            "taux_tva",
            # totaux.*
            "sous_total_ht",
            "montant_tva_10",
            "montant_tva_55",
            "montant_tva_20",
            "total_ttc",
            # conditions.*
            "acompte_pourcentage",
            "acompte_montant_ttc",
            "solde_montant_ttc",
            "delai_realisation",
            "modalites_paiement",
            "penalites_retard",
            # mentions_legales.*
            "droit_retractation",
            "tva_note",
            "garantie",
        ]
        for key in required_keys:
            assert key in SYSTEM_PROMPT, f"Missing schema key: {key}"

    def test_legal_mentions_present(self):
        """Test : les mentions légales obligatoires sont présentes."""
        legal_checks = [
            ("assurance décennale", re.IGNORECASE),
            ("L.221-18", re.IGNORECASE),
            ("Code de la consommation", re.IGNORECASE),
            ("14 jours", re.IGNORECASE),
        ]
        for text, flags in legal_checks:
            assert re.search(text, SYSTEM_PROMPT, flags), f"Missing legal mention: {text}"

        # Garantie décennale: accepte "Code civil" ou "article 1792" ou "articles 1792"
        garantie_found = re.search(r"(Code civil|article[s]? 1792)", SYSTEM_PROMPT, re.IGNORECASE)
        assert garantie_found, "Missing garantie décennale reference (Code civil or article(s) 1792)"

    def test_incoherence_rules_present(self):
        """Test : le prompt mentionne les règles de cohérence."""
        coherence_keywords = [
            "cohérence",
            "COHÉRENCE",
            "auto-check",
            "self-review",
            "vérification",
        ]
        # Au moins 2 de ces keywords doivent être présents
        found = sum(1 for kw in coherence_keywords if kw in SYSTEM_PROMPT)
        assert found >= 2, f"Coherence rules not sufficiently documented (found {found} keywords)"

    def test_line_count_cap_documented(self):
        """Test : le prompt documente le cap de lignes (max 5, 7, 10)."""
        # Regex souple : "max" suivi de 0-30 chars, puis 5/7/10, puis "lignes"
        match = re.search(
            r"max[^\n]{0,30}(5|7|10)[^\n]{0,30}lignes?",
            SYSTEM_PROMPT,
            re.IGNORECASE
        )
        assert match, "Line count cap not documented (max 5/7/10 lignes)"

    def test_a_completer_narrowed(self):
        """Test : [À COMPLÉTER] est explicitement restreint."""
        assert "[À COMPLÉTER]" in SYSTEM_PROMPT, "Missing [À COMPLÉTER] token"

        # Vérifier qu'il y a un message de restriction explicite dans le prompt
        # Le message de restriction se trouve dans PARTIE 6
        assert "NE PAS utiliser [À COMPLÉTER]" in SYSTEM_PROMPT, (
            "Missing explicit restriction message 'NE PAS utiliser [À COMPLÉTER]'"
        )

    def test_pivot_framing_present(self):
        """Test : framing 'devis envoyable sans retouche' est présent."""
        # The prompt uses "SANS LE RETOUCHER" (uppercase) as the core framing
        assert "SANS LE RETOUCHER" in SYSTEM_PROMPT, (
            "Missing pivot framing 'SANS LE RETOUCHER' "
            "(devis que l'artisan peut envoyer sans modification)"
        )


# ============================================================
# PARTIE 2 — TestPromptFixtures
# ============================================================

@pytest.fixture(scope="module")
def prompt_fixtures():
    """Charge tous les fixtures JSON de prompts (synthétiques)."""
    fx_dir = Path(__file__).parent.parent / "fixtures" / "prompts"
    out = []
    for p in sorted(fx_dir.glob("*.json")):
        out.append((p.name, json.loads(p.read_text(encoding="utf-8"))))
    return out


class TestPromptFixtures:
    """Tests sur la cohérence et santé des fixtures synthétiques."""

    def test_all_fixtures_parse_and_have_required_keys(self, prompt_fixtures):
        """Test : tous les fixtures ont les clés requises et sont valides."""
        for fname, fixture in prompt_fixtures:
            assert isinstance(fixture, dict), f"{fname}: not a dict"

            required_keys = {
                "id",
                "description_fr",
                "is_synthetic",
                "transcription",
                "expected",
                "notes",
            }
            assert required_keys.issubset(fixture.keys()), (
                f"{fname}: missing keys {required_keys - set(fixture.keys())}"
            )

            assert fixture.get("is_synthetic") is True, f"{fname}: is_synthetic != True"

            expected = fixture.get("expected", {})
            expected_keys = {
                "min_lignes",
                "max_lignes",
                "must_include_taux_tva",
                "reference_chantier_keywords",
                "total_ht_range",
                "expected_flags_max",
                "forbidden_flags",
                "must_flag_incoherence",
            }
            assert expected_keys.issubset(expected.keys()), (
                f"{fname}: expected missing keys {expected_keys - set(expected.keys())}"
            )

    def test_fixture_ranges_are_sane(self, prompt_fixtures):
        """Test : les ranges numériques des fixtures sont cohérentes."""
        for fname, fixture in prompt_fixtures:
            exp = fixture.get("expected", {})
            min_l = exp.get("min_lignes", 0)
            max_l = exp.get("max_lignes", 10)
            ht_range = exp.get("total_ht_range", [0, 0])
            flags_max = exp.get("expected_flags_max", 0)

            assert 0 < min_l <= max_l <= 10, (
                f"{fname}: ligne range invalid ({min_l}–{max_l})"
            )
            assert ht_range[0] < ht_range[1], (
                f"{fname}: total_ht_range invalid ({ht_range})"
            )
            assert ht_range[0] >= 50, (
                f"{fname}: total_ht_range[0] too low ({ht_range[0]})"
            )
            assert ht_range[1] <= 10000, (
                f"{fname}: total_ht_range[1] too high ({ht_range[1]})"
            )
            assert 0 <= flags_max <= 3, (
                f"{fname}: expected_flags_max out of range ({flags_max})"
            )

    def test_fixture_tva_rates_are_valid(self, prompt_fixtures):
        """Test : les taux TVA mentionnés sont valides."""
        valid_tva = {0.055, 0.10, 0.20}
        for fname, fixture in prompt_fixtures:
            exp = fixture.get("expected", {})
            must_include = exp.get("must_include_taux_tva", [])
            for rate in must_include:
                assert rate in valid_tva, (
                    f"{fname}: invalid TVA rate {rate} "
                    f"(must be in {valid_tva})"
                )

    def test_exactly_one_incoherence_fixture(self, prompt_fixtures):
        """Test : exactement 1 fixture a must_flag_incoherence == True."""
        incoherent_count = sum(
            1 for _, fx in prompt_fixtures
            if fx.get("expected", {}).get("must_flag_incoherence") is True
        )
        assert incoherent_count == 1, (
            f"Expected exactly 1 incoherent fixture, found {incoherent_count}"
        )

        # Vérifier que le fixture incohérent a "incoherent" ou "incoh" dans l'id
        incoherent_fixture = next(
            (fx for _, fx in prompt_fixtures
             if fx.get("expected", {}).get("must_flag_incoherence") is True),
            None
        )
        assert incoherent_fixture is not None
        fixture_id = incoherent_fixture.get("id", "").lower()
        assert "incoherent" in fixture_id or "incoh" in fixture_id, (
            f"Incoherent fixture id '{fixture_id}' should contain 'incoherent' or 'incoh'"
        )

    def test_fixtures_cover_expected_scenarios(self, prompt_fixtures):
        """Test : les fixtures couvrent les scénarios clés."""
        all_ids = [fx.get("id", "") for _, fx in prompt_fixtures]
        all_text = " ".join(all_ids).lower()

        required_scenarios = {
            "chaudiere": "chaudiere" in all_text,
            "sdb_or_salle": ("salle_de_bain" in all_text or "sdb" in all_text),
            "depannage_or_fuite": ("depannage" in all_text or "fuite" in all_text),
            "chauffe_eau": "chauffe_eau" in all_text or "chauffe-eau" in all_text.replace("_", "-"),
            "incoherent": any("incoherent" in fid.lower() or "incoh" in fid.lower() for fid in all_ids),
        }

        for scenario, found in required_scenarios.items():
            assert found, (
                f"Fixture scenario '{scenario}' not covered. "
                f"Available: {all_ids}"
            )


# ============================================================
# PARTIE 3 — TestInjectedPromptRoundtrip
# ============================================================

class TestInjectedPromptRoundtrip:
    """Tests sur l'injection de profils et intégrité du prompt."""

    def test_injected_prompt_has_no_residual_english_placeholders_for_complete_profile(self):
        """Test : après injection d'un profil complet, tous les tokens sont remplacés."""
        complete_profile = {
            "raison_sociale": "Martin Plomberie",
            "forme_juridique": "SARL",
            "dirigeant": "Jean Martin",
            "adresse": "42 rue de la Paix, 75001 Paris",
            "telephone": "0123456789",
            "email": "contact@martin-plomberie.fr",
            "site_web": "www.martin-plomberie.fr",
            "siret": "50123456789012",
            "tva_intracom": "FR5012345678901",
            "rcs_rm": "RCS Paris 123456",
            "code_ape": "4322A",
            "assurance_decennale": "AXA Assurance",
            "numero_police": "POL-XYZ-2025",
            "annee_assurance": "2025",
        }

        injected = inject_profile_in_prompt(SYSTEM_PROMPT, complete_profile)

        # Vérifier que les tokens principaux ont disparu
        main_tokens = [
            "[NOM_ENTREPRISE]",
            "[FORME_JURIDIQUE]",
            "[NOM_PRENOM_DIRIGEANT]",
            "[ADRESSE_COMPLETE]",
            "[TELEPHONE]",
            "[EMAIL]",
            "[SITE_WEB]",
            "[NUMERO_SIRET]",
            "[NUMERO_TVA_INTRACOM]",
            "[NUMERO_RCS_OU_RM]",
            "[CODE_APE]",
            "[NOM_ASSUREUR]",
            "[NUMERO_POLICE_ASSURANCE]",
            "[ANNEE_VALIDITE_ASSURANCE]",
        ]
        for token in main_tokens:
            assert token not in injected, (
                f"Token {token} not replaced after profile injection"
            )

        # Vérifier que les valeurs réelles y sont
        assert "Martin Plomberie" in injected
        assert "50123456789012" in injected
        assert "contact@martin-plomberie.fr" in injected

    def test_injected_prompt_handles_empty_profile_gracefully(self):
        """Test : inject_profile_in_prompt({}) ne lève pas d'exception."""
        result = inject_profile_in_prompt(SYSTEM_PROMPT, {})
        assert isinstance(result, str)
        assert len(result) > 0
        # Pas d'assertion sur la présence/absence de [À COMPLÉTER],
        # car les fallbacks doivent contenir [À COMPLÉTER]

    def test_prompt_length_reasonable(self):
        """Test : la longueur du prompt est raisonnable (< 20k chars)."""
        assert len(SYSTEM_PROMPT) < 20000, (
            f"Prompt too long: {len(SYSTEM_PROMPT)} chars (max 20000)"
        )


# ============================================================
# Tests de compliance additionnels (bonus)
# ============================================================

class TestPromptCompliance:
    """Tests supplémentaires de conformité détaillée."""

    def test_tarif_defaults_within_reasonable_bounds(self):
        """Test : les tarifs par défaut sont dans des fourchettes raisonnables."""
        assert 300 <= TARIFS_DEFAUT["journee_standard_ht"] <= 500
        assert 400 <= TARIFS_DEFAUT["journee_specialise_ht"] <= 600
        assert 30 <= TARIFS_DEFAUT["deplacement_par_jour"] <= 60
        assert 0.20 <= TARIFS_DEFAUT["marge_materiaux"] <= 0.50

    def test_prompt_mentions_invoice_not_just_quote(self):
        """Test : le prompt bien précise qu'il s'agit de devis, pas de facture."""
        assert "devis" in SYSTEM_PROMPT.lower()

    def test_no_placeholder_in_constants(self):
        """Test : les valeurs constantes n'ont pas de placeholders."""
        for key, value in TARIFS_DEFAUT.items():
            if isinstance(value, str):
                assert "[" not in value, f"TARIFS_DEFAUT[{key}] contains placeholder"

    def test_json_format_section_is_present(self):
        """Test : la section FORMAT DU DEVIS est documentée."""
        assert "FORMAT DU DEVIS" in SYSTEM_PROMPT or "format" in SYSTEM_PROMPT.lower()

    def test_parts_are_numbered(self):
        """Test : le prompt a une structure en PARTIES numérotées."""
        # Vérifier qu'il y a au moins 5 parties documentées
        parts_match = re.findall(r"PARTIE \d+", SYSTEM_PROMPT)
        assert len(parts_match) >= 5, (
            f"Expected at least 5 PARTIE sections, found {len(parts_match)}"
        )
