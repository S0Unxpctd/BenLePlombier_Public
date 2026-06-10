"""
Tests unitaires pour calculs.py — Fonctions de recalcul TVA et montants
"""

import pytest
import copy


# Les fonctions à tester sont importées depuis le module parent
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from calculs import recalculer_totaux, appliquer_taux_tva_uniforme


class TestRecalculerTotaux:
    """Tests pour recalculer_totaux()"""

    def test_empty_devis(self):
        """Test avec un devis vide."""
        devis = {"lignes": []}
        result = recalculer_totaux(devis)

        assert result["totaux"]["sous_total_ht"] == 0.0
        assert result["totaux"]["montant_tva_10"] == 0.0
        assert result["totaux"]["montant_tva_55"] == 0.0
        assert result["totaux"]["montant_tva_20"] == 0.0
        assert result["totaux"]["total_ttc"] == 0.0
        assert result["conditions"]["acompte_montant_ttc"] == 0.0
        assert result["conditions"]["solde_montant_ttc"] == 0.0

    def test_single_line_tva_20(self):
        """Test avec une seule ligne à TVA 20%."""
        devis = {
            "lignes": [
                {
                    "description": "Main d'œuvre",
                    "quantite": 1,
                    "prix_unitaire_ht": 100.0,
                    "taux_tva": 0.20,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = recalculer_totaux(devis)

        assert result["totaux"]["sous_total_ht"] == 100.0
        assert result["totaux"]["montant_tva_20"] == 20.0
        assert result["totaux"]["montant_tva_10"] == 0.0
        assert result["totaux"]["montant_tva_55"] == 0.0
        assert result["totaux"]["total_ttc"] == 120.0
        assert result["conditions"]["acompte_montant_ttc"] == 36.0
        assert result["conditions"]["solde_montant_ttc"] == 84.0

    def test_single_line_tva_10(self):
        """Test avec une seule ligne à TVA 10%."""
        devis = {
            "lignes": [
                {
                    "description": "Travaux de rénovation",
                    "quantite": 1,
                    "prix_unitaire_ht": 500.0,
                    "taux_tva": 0.10,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = recalculer_totaux(devis)

        assert result["totaux"]["sous_total_ht"] == 500.0
        assert result["totaux"]["montant_tva_10"] == 50.0
        assert result["totaux"]["total_ttc"] == 550.0

    def test_single_line_tva_55(self):
        """Test avec une seule ligne à TVA 5.5%."""
        devis = {
            "lignes": [
                {
                    "description": "Travaux de rénovation énergétique",
                    "quantite": 1,
                    "prix_unitaire_ht": 1000.0,
                    "taux_tva": 0.055,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = recalculer_totaux(devis)

        assert result["totaux"]["sous_total_ht"] == 1000.0
        assert result["totaux"]["montant_tva_55"] == 55.0
        assert result["totaux"]["total_ttc"] == 1055.0

    def test_single_line_tva_zero(self):
        """Test avec TVA 0%."""
        devis = {
            "lignes": [
                {
                    "description": "Micro-entreprise non assujettie",
                    "quantite": 1,
                    "prix_unitaire_ht": 100.0,
                    "taux_tva": 0.0,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = recalculer_totaux(devis)

        assert result["totaux"]["sous_total_ht"] == 100.0
        assert result["totaux"]["montant_tva_10"] == 0.0
        assert result["totaux"]["montant_tva_55"] == 0.0
        assert result["totaux"]["montant_tva_20"] == 0.0
        assert result["totaux"]["total_ttc"] == 100.0

    def test_multiple_lines_mixed_tva(self):
        """Test avec plusieurs lignes à différents taux de TVA."""
        devis = {
            "lignes": [
                {
                    "description": "Main d'œuvre",
                    "quantite": 2,
                    "prix_unitaire_ht": 350.0,
                    "taux_tva": 0.20,
                },
                {
                    "description": "Matériel chauffage",
                    "quantite": 1,
                    "prix_unitaire_ht": 500.0,
                    "taux_tva": 0.10,
                },
                {
                    "description": "Isolation thermique",
                    "quantite": 1,
                    "prix_unitaire_ht": 1000.0,
                    "taux_tva": 0.055,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = recalculer_totaux(devis)

        # Recalcul manuel
        assert result["lignes"][0]["montant_ht"] == 700.0  # 2 * 350
        assert result["lignes"][1]["montant_ht"] == 500.0
        assert result["lignes"][2]["montant_ht"] == 1000.0

        sous_total = 700.0 + 500.0 + 1000.0
        assert result["totaux"]["sous_total_ht"] == 2200.0

        assert result["totaux"]["montant_tva_20"] == 140.0  # 700 * 0.20
        assert result["totaux"]["montant_tva_10"] == 50.0   # 500 * 0.10
        assert result["totaux"]["montant_tva_55"] == 55.0   # 1000 * 0.055

        total_ttc = 2200.0 + 140.0 + 50.0 + 55.0
        assert result["totaux"]["total_ttc"] == 2445.0
        assert result["conditions"]["acompte_montant_ttc"] == 733.5  # 2445 * 0.30
        assert result["conditions"]["solde_montant_ttc"] == 1711.5

    def test_quantity_calculation(self):
        """Test que montant_ht = quantite * prix_unitaire_ht."""
        devis = {
            "lignes": [
                {
                    "description": "Travaux",
                    "quantite": 3.5,
                    "prix_unitaire_ht": 150.0,
                    "taux_tva": 0.20,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = recalculer_totaux(devis)

        expected_montant = round(3.5 * 150.0, 2)
        assert result["lignes"][0]["montant_ht"] == 525.0
        assert result["totaux"]["sous_total_ht"] == 525.0

    def test_rounding_precision(self):
        """Test la précision du rounding à 2 décimales."""
        devis = {
            "lignes": [
                {
                    "description": "Montant causant arrondi",
                    "quantite": 1,
                    "prix_unitaire_ht": 33.33,
                    "taux_tva": 0.10,
                },
                {
                    "description": "Autre montant",
                    "quantite": 1,
                    "prix_unitaire_ht": 33.34,
                    "taux_tva": 0.10,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = recalculer_totaux(devis)

        # Vérifier que tous les montants sont arrondis à 2 décimales
        assert result["totaux"]["sous_total_ht"] == 66.67
        tva_attendue = round(66.67 * 0.10, 2)
        assert result["totaux"]["montant_tva_10"] == tva_attendue

        # Acompte doit aussi être arrondi
        acompte = result["conditions"]["acompte_montant_ttc"]
        assert acompte == round(acompte, 2)

    def test_deepcopy_no_mutation(self):
        """Vérifier que recalculer_totaux() ne modifie pas l'original."""
        original = {
            "lignes": [
                {
                    "description": "Test",
                    "quantite": 1,
                    "prix_unitaire_ht": 100.0,
                    "taux_tva": 0.20,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        original_copy = copy.deepcopy(original)

        result = recalculer_totaux(original)

        # L'original ne doit pas être modifié
        assert original == original_copy
        # Le résultat doit contenir les calculs
        assert "totaux" in result
        assert result["totaux"]["total_ttc"] == 120.0

    def test_default_acompte_pourcentage(self):
        """Test avec acompte_pourcentage par défaut (30)."""
        devis = {
            "lignes": [
                {
                    "description": "Test",
                    "quantite": 1,
                    "prix_unitaire_ht": 100.0,
                    "taux_tva": 0.20,
                }
            ]
            # conditions vide ou absent
        }
        result = recalculer_totaux(devis)

        # Doit utiliser 30% par défaut
        assert result["conditions"]["acompte_montant_ttc"] == 36.0  # 120 * 0.30

    def test_zero_quantity(self):
        """Test avec quantité = 0."""
        devis = {
            "lignes": [
                {
                    "description": "Ligne avec quantité 0",
                    "quantite": 0,
                    "prix_unitaire_ht": 100.0,
                    "taux_tva": 0.20,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = recalculer_totaux(devis)

        assert result["lignes"][0]["montant_ht"] == 0.0
        assert result["totaux"]["total_ttc"] == 0.0

    def test_zero_price(self):
        """Test avec prix unitaire = 0."""
        devis = {
            "lignes": [
                {
                    "description": "Ligne avec prix 0",
                    "quantite": 5,
                    "prix_unitaire_ht": 0.0,
                    "taux_tva": 0.20,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = recalculer_totaux(devis)

        assert result["lignes"][0]["montant_ht"] == 0.0
        assert result["totaux"]["total_ttc"] == 0.0

    def test_custom_acompte_pourcentage(self):
        """Test avec un acompte_pourcentage personnalisé."""
        devis = {
            "lignes": [
                {
                    "description": "Test",
                    "quantite": 1,
                    "prix_unitaire_ht": 1000.0,
                    "taux_tva": 0.20,
                }
            ],
            "conditions": {"acompte_pourcentage": 50}
        }
        result = recalculer_totaux(devis)

        total_ttc = 1200.0
        assert result["conditions"]["acompte_montant_ttc"] == 600.0  # 1200 * 0.50
        assert result["conditions"]["solde_montant_ttc"] == 600.0


class TestAppliquerTauxTvaUniforme:
    """Tests pour appliquer_taux_tva_uniforme()"""

    def test_tva_zero_mentions_legales(self):
        """Test que TVA=0 ajoute les mentions légales correctes."""
        devis = {
            "lignes": [
                {
                    "description": "Test",
                    "quantite": 1,
                    "prix_unitaire_ht": 100.0,
                    "taux_tva": 0.20,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = appliquer_taux_tva_uniforme(devis, 0.0)

        # Vérifier que le taux TVA a été changé
        assert result["lignes"][0]["taux_tva"] == 0.0

        # Vérifier les mentions légales
        assert "artisan" in result
        assert result["artisan"]["tva_intracom"] == "Non assujetti à la TVA — Art. 293 B du CGI"

        assert "mentions_legales" in result
        assert result["mentions_legales"]["tva_note"] == "Non assujetti à la TVA — Art. 293 B du CGI"

        # TVA doit être 0
        assert result["totaux"]["total_ttc"] == 100.0

    def test_tva_20_applied_uniformly(self):
        """Test que TVA=0.20 s'applique à toutes les lignes."""
        devis = {
            "lignes": [
                {
                    "description": "Ligne 1",
                    "quantite": 1,
                    "prix_unitaire_ht": 100.0,
                    "taux_tva": 0.10,
                },
                {
                    "description": "Ligne 2",
                    "quantite": 1,
                    "prix_unitaire_ht": 100.0,
                    "taux_tva": 0.055,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = appliquer_taux_tva_uniforme(devis, 0.20)

        # Tous les taux doivent être 0.20
        for ligne in result["lignes"]:
            assert ligne["taux_tva"] == 0.20

        # Vérifier le calcul
        assert result["totaux"]["sous_total_ht"] == 200.0
        assert result["totaux"]["montant_tva_20"] == 40.0
        assert result["totaux"]["total_ttc"] == 240.0

    def test_tva_55_applied(self):
        """Test avec TVA=5.5%."""
        devis = {
            "lignes": [
                {
                    "description": "Test",
                    "quantite": 1,
                    "prix_unitaire_ht": 100.0,
                    "taux_tva": 0.20,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = appliquer_taux_tva_uniforme(devis, 0.055)

        assert result["lignes"][0]["taux_tva"] == 0.055
        assert result["totaux"]["montant_tva_55"] == 5.5
        assert result["totaux"]["total_ttc"] == 105.5

    def test_tva_10_applied(self):
        """Test avec TVA=10%."""
        devis = {
            "lignes": [
                {
                    "description": "Test",
                    "quantite": 1,
                    "prix_unitaire_ht": 100.0,
                    "taux_tva": 0.055,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = appliquer_taux_tva_uniforme(devis, 0.10)

        assert result["lignes"][0]["taux_tva"] == 0.10
        assert result["totaux"]["montant_tva_10"] == 10.0
        assert result["totaux"]["total_ttc"] == 110.0

    def test_deepcopy_no_mutation_uniform(self):
        """Vérifier que appliquer_taux_tva_uniforme() ne modifie pas l'original."""
        original = {
            "lignes": [
                {
                    "description": "Test",
                    "quantite": 1,
                    "prix_unitaire_ht": 100.0,
                    "taux_tva": 0.20,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        original_copy = copy.deepcopy(original)

        result = appliquer_taux_tva_uniforme(original, 0.0)

        # L'original ne doit pas être modifié
        assert original == original_copy
        # Le résultat doit avoir TVA=0
        assert result["lignes"][0]["taux_tva"] == 0.0

    def test_empty_lines_uniform_tva(self):
        """Test avec devis vide."""
        devis = {
            "lignes": [],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = appliquer_taux_tva_uniforme(devis, 0.0)

        assert result["totaux"]["total_ttc"] == 0.0
        assert result["artisan"]["tva_intracom"] == "Non assujetti à la TVA — Art. 293 B du CGI"

    def test_tva_zero_no_mention_for_nonzero(self):
        """Test qu'avec TVA non-zéro, les mentions TVA zéro ne sont PAS ajoutées."""
        devis = {
            "lignes": [
                {
                    "description": "Test",
                    "quantite": 1,
                    "prix_unitaire_ht": 100.0,
                    "taux_tva": 0.0,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        # Commencer avec TVA=0, puis passer à TVA=20
        result = appliquer_taux_tva_uniforme(devis, 0.20)

        # Les mentions TVA zéro ne doivent PAS être présentes
        # (elles ne sont ajoutées que si taux == 0.0)
        artisan_mention = result.get("artisan", {}).get("tva_intracom", "")
        assert "Non assujetti à la TVA" not in artisan_mention or "Art. 293" not in artisan_mention


class TestEdgeCases:
    """Tests pour les cas limites."""

    def test_very_small_values(self):
        """Test avec des très petites valeurs."""
        devis = {
            "lignes": [
                {
                    "description": "Très petit montant",
                    "quantite": 0.01,
                    "prix_unitaire_ht": 0.01,
                    "taux_tva": 0.20,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = recalculer_totaux(devis)

        # Doit être arrondi à 0.00 (0.0001 -> 0.00 à 2 décimales)
        assert result["lignes"][0]["montant_ht"] == 0.0

    def test_large_values(self):
        """Test avec des très grandes valeurs."""
        devis = {
            "lignes": [
                {
                    "description": "Très gros montant",
                    "quantite": 1000,
                    "prix_unitaire_ht": 10000.0,
                    "taux_tva": 0.20,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = recalculer_totaux(devis)

        assert result["lignes"][0]["montant_ht"] == 10000000.0
        assert result["totaux"]["sous_total_ht"] == 10000000.0
        assert result["totaux"]["montant_tva_20"] == 2000000.0

    def test_floating_point_precision(self):
        """Test la précision des calculs en virgule flottante."""
        devis = {
            "lignes": [
                {
                    "description": "Test virgule flottante",
                    "quantite": 3,
                    "prix_unitaire_ht": 10.99,
                    "taux_tva": 0.10,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }
        result = recalculer_totaux(devis)

        # 3 * 10.99 = 32.97
        assert result["lignes"][0]["montant_ht"] == 32.97
        assert result["totaux"]["sous_total_ht"] == 32.97
        # 32.97 * 0.10 = 3.297 -> 3.30
        assert result["totaux"]["montant_tva_10"] == 3.3
