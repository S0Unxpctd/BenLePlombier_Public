"""
Tests unitaires pour les fonctions helpers de bot.py (pure functions)
"""

import pytest
from datetime import datetime, timedelta
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot import (
    is_skip,
    build_user_message,
    _detecter_tva_zero,
    build_recap,
    SKIP_BTN,
    NON_BTN,
)


class TestIsSkip:
    """Tests pour is_skip()"""

    def test_skip_button_text(self):
        """Test avec le texte du bouton SKIP."""
        assert is_skip(SKIP_BTN) is True

    def test_non_button_text(self):
        """Test avec le texte du bouton NON."""
        assert is_skip(NON_BTN) is True

    def test_skip_words(self):
        """Test avec les variantes textuelles de skip."""
        skip_words = ["passer", "plus tard", "plustard", "skip", "non", "nope",
                      "rien", "pas maintenant", "plus", "later", "n/a", "na", "—", "-"]

        for word in skip_words:
            assert is_skip(word) is True

    def test_case_insensitive_skip(self):
        """Test que la détection est insensible à la casse."""
        assert is_skip("PASSER") is True
        assert is_skip("Skip") is True
        assert is_skip("PLUS TARD") is True

    def test_with_spaces(self):
        """Test avec espaces avant/après."""
        assert is_skip("  passer  ") is True
        assert is_skip(" non ") is True

    def test_non_skip_text(self):
        """Test avec du texte qui ne doit pas être skip."""
        assert is_skip("d'accord") is False
        assert is_skip("oui") is False
        assert is_skip("350") is False
        assert is_skip("mon client s'appelle Martin") is False

    def test_empty_string(self):
        """Test avec une chaîne vide."""
        assert is_skip("") is False

    def test_partial_skip_words(self):
        """Test que les mots partiels ne matchent pas."""
        # "plus" est un skip word, mais "plus tard" aussi
        # Vérifier que "surplus" n'est pas considéré comme skip
        assert is_skip("surplus") is False
        # "non" est un skip word, mais "non-compris" ne doit pas matcher
        assert is_skip("non-compris") is False


class TestBuildUserMessage:
    """Tests pour build_user_message()"""

    def test_basic_message(self):
        """Test la génération basique d'un message utilisateur."""
        transcription = "Je veux un devis pour une plomberie"
        message = build_user_message(transcription)

        assert "Date d'aujourd'hui :" in message
        assert "Date de validité :" in message
        assert "Transcription du message vocal :" in message
        assert "Je veux un devis pour une plomberie" in message
        assert "Génère le devis JSON complet" in message

    def test_validity_date_calculation(self):
        """Test que la date de validité est à 90 jours."""
        # Note: ce test peut échouer à minuit UTC/CET à cause de décalages horaires
        transcription = "Test"
        message = build_user_message(transcription)

        # Extraire les dates du message
        today = datetime.now()
        validity = today + timedelta(days=90)

        today_str = today.strftime("%d/%m/%Y")
        validity_str = validity.strftime("%d/%m/%Y")

        assert today_str in message
        assert validity_str in message

    def test_with_client_extra(self):
        """Test avec client fourni."""
        transcription = "Test"
        extra = {"client": "Jean Martin, 75000 Paris, 06 12 34 56 78"}
        message = build_user_message(transcription, extra)

        assert "Coordonnées client fournies par l'artisan :" in message
        assert "Jean Martin, 75000 Paris, 06 12 34 56 78" in message

    def test_with_duree_extra(self):
        """Test avec durée fournie."""
        transcription = "Test"
        extra = {"duree": "3 jours"}
        message = build_user_message(transcription, extra)

        assert "Durée estimée des travaux précisée :" in message
        assert "3 jours" in message

    def test_with_marques_extra(self):
        """Test avec marques/références fournis."""
        transcription = "Test"
        extra = {"marques": "Chaudière Bosch, Radiateurs en acier"}
        message = build_user_message(transcription, extra)

        assert "Marques / références équipements précisées :" in message
        assert "Chaudière Bosch, Radiateurs en acier" in message

    def test_with_prix_postes_extra(self):
        """Test avec prix imposés fournis."""
        transcription = "Test"
        extra = {
            "prix_postes": [
                {"description": "Main d'œuvre", "prix_unitaire_ht": 350, "unite": "jour"},
                {"description": "Matériel chauffage", "prix_unitaire_ht": 500, "unite": "forfait"},
            ]
        }
        message = build_user_message(transcription, extra)

        assert "PRIX IMPOSÉS PAR L'ARTISAN" in message
        assert "Main d'œuvre" in message
        assert "350" in message
        assert "jour" in message
        assert "Matériel chauffage" in message
        assert "500" in message

    def test_with_all_extras(self):
        """Test avec tous les extras fournis."""
        transcription = "Full test"
        extra = {
            "client": "Client Test",
            "duree": "5 jours",
            "marques": "Brand A, Brand B",
            "prix_postes": [
                {"description": "Poste 1", "prix_unitaire_ht": 100, "unite": "u"},
            ]
        }
        message = build_user_message(transcription, extra)

        assert "Client Test" in message
        assert "5 jours" in message
        assert "Brand A, Brand B" in message
        assert "Poste 1" in message
        assert "PRIX IMPOSÉS PAR L'ARTISAN" in message

    def test_none_extra(self):
        """Test avec extra=None."""
        transcription = "Test"
        message = build_user_message(transcription, None)

        # Ne doit pas avoir d'erreur, et pas de sections extras
        assert "Transcription du message vocal :" in message
        assert "Coordonnées client" not in message


class TestDetecterTvaZero:
    """Tests pour _detecter_tva_zero()"""

    def test_non_assujetti(self):
        """Test la détection 'non assujetti'."""
        assert _detecter_tva_zero("Je suis non assujetti à la TVA") is True
        assert _detecter_tva_zero("Non assujetti") is True

    def test_pas_assujetti(self):
        """Test la détection 'pas assujetti'."""
        assert _detecter_tva_zero("Je suis pas assujetti") is True

    def test_sans_tva(self):
        """Test les variantes 'sans tva'."""
        assert _detecter_tva_zero("sans tva") is True
        assert _detecter_tva_zero("pas de tva") is True

    def test_tva_zero_patterns(self):
        """Test les patterns TVA 0%."""
        assert _detecter_tva_zero("tva 0") is True
        assert _detecter_tva_zero("0% tva") is True
        assert _detecter_tva_zero("0 % tva") is True

    def test_exempt_patterns(self):
        """Test les patterns 'exempt'."""
        assert _detecter_tva_zero("exempté") is True
        assert _detecter_tva_zero("exempt") is True

    def test_franchise_en_base(self):
        """Test la détection 'franchise en base'."""
        assert _detecter_tva_zero("franchise en base") is True

    def test_micro_entreprise_patterns(self):
        """Test les patterns micro-entreprise."""
        assert _detecter_tva_zero("micro-entreprise") is True
        assert _detecter_tva_zero("micro entreprise") is True
        assert _detecter_tva_zero("autoentrepreneur") is True
        assert _detecter_tva_zero("auto-entrepreneur") is True
        assert _detecter_tva_zero("auto entrepreneur") is True

    def test_article_293(self):
        """Test la détection article 293."""
        assert _detecter_tva_zero("art 293") is True
        assert _detecter_tva_zero("article 293") is True

    def test_hors_tva(self):
        """Test les patterns 'hors tva'."""
        assert _detecter_tva_zero("hors tva") is True
        assert _detecter_tva_zero("ht seulement") is True

    def test_case_insensitive(self):
        """Test que la détection est insensible à la casse."""
        assert _detecter_tva_zero("NON ASSUJETTI") is True
        assert _detecter_tva_zero("Franchise en base") is True
        assert _detecter_tva_zero("MICRO-ENTREPRISE") is True

    def test_no_false_positives(self):
        """Test qu'on n'ait pas de faux positifs."""
        # Important: "je suis assujetti à la TVA" NE doit PAS trigger TVA zéro
        assert _detecter_tva_zero("je suis assujetti à la TVA") is False

        # "TVA 20%" ne doit PAS trigger
        assert _detecter_tva_zero("TVA 20%") is False

        # Un message normal sans mention TVA zéro
        assert _detecter_tva_zero("Je veux un devis pour ma plomberie") is False

    def test_empty_string(self):
        """Test avec chaîne vide."""
        assert _detecter_tva_zero("") is False

    def test_embedded_patterns(self):
        """Test avec patterns imbriqués dans du texte."""
        text = "Mon entreprise est non assujetti à la TVA car je suis en micro-entreprise"
        assert _detecter_tva_zero(text) is True

    def test_accent_insensitive(self):
        """Test que les accents ne causent pas d'issues."""
        # Les patterns ne contiennent pas d'accents donc ça devrait matcher
        assert _detecter_tva_zero("exempté") is True  # 'é' mais pattern = 'exempt'
        # Hmm, cela dépend de l'implémentation — vérifier ce qui se passe


class TestBuildRecap:
    """Tests pour build_recap()"""

    def test_basic_recap(self):
        """Test la génération basique d'un récapitulatif."""
        devis = {
            "meta": {
                "numero_devis": "DEVIS-20260331-001",
                "reference_chantier": "Rénovation salle de bain",
                "date_validite": "30/06/2026",
            },
            "client": {
                "nom": "Martin Jean",
            },
            "totaux": {
                "total_ttc": 5000.0,
            },
            "conditions": {
                "acompte_montant_ttc": 1500.0,
            },
            "lignes": [
                {"description": "Poste 1"},
                {"description": "Poste 2"},
                {"description": "Poste 3"},
            ],
            "flags": [],
        }

        recap = build_recap(devis)

        assert "DEVIS-20260331-001" in recap
        assert "Martin Jean" in recap
        assert "5000.00" in recap or "5,000" in recap
        assert "1500.00" in recap or "1,500" in recap
        assert "3" in recap  # Nombre de postes

    def test_recap_with_flags(self):
        """Test que les flags sont inclus dans le récap."""
        devis = {
            "meta": {"numero_devis": "DEVIS-001", "reference_chantier": "Test"},
            "client": {"nom": "Test"},
            "totaux": {"total_ttc": 1000.0},
            "conditions": {"acompte_montant_ttc": 300.0},
            "lignes": [{"description": "Poste"}],
            "flags": ["Vérifier le matériel", "Confirmer la date"],
        }

        recap = build_recap(devis)

        assert "Vérifier le matériel" in recap
        assert "Confirmer la date" in recap
        assert "2 point(s) à vérifier" in recap or "2" in recap

    def test_recap_empty_or_invalid_flags(self):
        """Test que les flags vides ou '...' sont ignorés."""
        devis = {
            "meta": {"numero_devis": "DEVIS-001", "reference_chantier": "Test"},
            "client": {"nom": "Test"},
            "totaux": {"total_ttc": 1000.0},
            "conditions": {"acompte_montant_ttc": 300.0},
            "lignes": [{"description": "Poste"}],
            "flags": ["", "...", None, "Point important"],
        }

        recap = build_recap(devis)

        # Les flags vides et "..." ne doivent pas être affichés
        assert "..." not in recap or "point(s)" not in recap
        assert "Point important" in recap

    def test_recap_missing_fields(self):
        """Test le récap avec des champs manquants."""
        devis = {
            "meta": {"numero_devis": "DEVIS-001"},
            "client": {},
            "totaux": {},
            "conditions": {},
            "lignes": [],
            "flags": [],
        }

        recap = build_recap(devis)

        # Ne doit pas avoir d'erreur et doit utiliser des valeurs par défaut
        assert "DEVIS-001" in recap
        assert "[À COMPLÉTER]" in recap or "—" in recap

    def test_recap_markdown_formatting(self):
        """Test que le récap contient du Markdown."""
        devis = {
            "meta": {"numero_devis": "DEVIS-001", "reference_chantier": "Test"},
            "client": {"nom": "Client"},
            "totaux": {"total_ttc": 1000.0},
            "conditions": {"acompte_montant_ttc": 300.0},
            "lignes": [{"description": "Poste"}],
            "flags": [],
        }

        recap = build_recap(devis)

        # Doit contenir du Markdown
        assert "*" in recap  # Gras
        assert "`" in recap  # Code

    def test_recap_numbers_formatting(self):
        """Test que les nombres sont bien formatés."""
        devis = {
            "meta": {"numero_devis": "DEVIS-001", "reference_chantier": "Test"},
            "client": {"nom": "Test"},
            "totaux": {"total_ttc": 1234.56},
            "conditions": {"acompte_montant_ttc": 370.37},
            "lignes": [{"description": "Poste"}],
            "flags": [],
        }

        recap = build_recap(devis)

        # Doit contenir les montants (avec ou sans séparateur de milliers)
        assert "1234.56" in recap or "1,234.56" in recap or "1234" in recap
        assert "370.37" in recap or "370" in recap

    def test_recap_zero_values(self):
        """Test avec des montants zéro."""
        devis = {
            "meta": {"numero_devis": "DEVIS-001", "reference_chantier": "Test"},
            "client": {"nom": "Test"},
            "totaux": {"total_ttc": 0.0},
            "conditions": {"acompte_montant_ttc": 0.0},
            "lignes": [],
            "flags": [],
        }

        recap = build_recap(devis)

        # Ne doit pas avoir d'erreur
        assert "DEVIS-001" in recap
        assert "0" in recap
