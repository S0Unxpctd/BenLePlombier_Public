"""
Tests d'intégration pour Souffl.AI
Tests les flux complets entre modules
"""

import pytest
import json
from pathlib import Path
import tempfile
import sys
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

from calculs import recalculer_totaux, appliquer_taux_tva_uniforme
from facture_generator import devis_to_facture, devis_to_facture_typed


class TestIntegrationDevisWorkflow:
    """Tests d'intégration du flux complet devis"""

    def test_devis_recalculation_workflow(self):
        """Test le flux : création devis → recalcul totaux → vérification."""
        # Créer un devis minimal
        devis = {
            "meta": {
                "numero_devis": "DEVIS-20260331-TEST",
                "date_emission": "31/03/2026",
            },
            "client": {
                "nom": "Client Test",
            },
            "lignes": [
                {
                    "description": "Main d'œuvre",
                    "quantite": 2,
                    "prix_unitaire_ht": 350.0,
                    "taux_tva": 0.20,
                },
                {
                    "description": "Matériel",
                    "quantite": 1,
                    "prix_unitaire_ht": 500.0,
                    "taux_tva": 0.10,
                },
            ],
            "conditions": {
                "acompte_pourcentage": 30,
            }
        }

        # Recalculer
        result = recalculer_totaux(devis)

        # Vérifications
        assert result["lignes"][0]["montant_ht"] == 700.0
        assert result["lignes"][1]["montant_ht"] == 500.0

        sous_total = 700.0 + 500.0
        assert result["totaux"]["sous_total_ht"] == 1200.0

        tva_20 = 700.0 * 0.20
        tva_10 = 500.0 * 0.10
        assert result["totaux"]["montant_tva_20"] == tva_20
        assert result["totaux"]["montant_tva_10"] == tva_10

        total_ttc = 1200.0 + tva_20 + tva_10
        assert result["totaux"]["total_ttc"] == total_ttc

        acompte = total_ttc * 0.30
        solde = total_ttc - acompte
        assert result["conditions"]["acompte_montant_ttc"] == acompte
        assert result["conditions"]["solde_montant_ttc"] == solde

    def test_devis_tva_zero_workflow(self):
        """Test le flux : devis normal → appliquer TVA 0 → vérifier mentions."""
        devis = {
            "meta": {"numero_devis": "DEVIS-001"},
            "lignes": [
                {
                    "description": "Travaux",
                    "quantite": 1,
                    "prix_unitaire_ht": 1000.0,
                    "taux_tva": 0.20,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }

        # Appliquer TVA 0
        result = appliquer_taux_tva_uniforme(devis, 0.0)

        # Vérifier
        assert result["lignes"][0]["taux_tva"] == 0.0
        assert result["totaux"]["total_ttc"] == 1000.0
        assert result["artisan"]["tva_intracom"] == "Non assujetti à la TVA — Art. 293 B du CGI"
        assert result["mentions_legales"]["tva_note"] == "Non assujetti à la TVA — Art. 293 B du CGI"

    def test_complex_multi_tva_devis(self):
        """Test un devis complexe avec plusieurs taux de TVA."""
        devis = {
            "meta": {"numero_devis": "DEVIS-COMPLEX"},
            "lignes": [
                {
                    "description": "Main d'œuvre 20%",
                    "quantite": 3,
                    "prix_unitaire_ht": 350.0,
                    "taux_tva": 0.20,
                },
                {
                    "description": "Matériel chauffage 10%",
                    "quantite": 2,
                    "prix_unitaire_ht": 500.0,
                    "taux_tva": 0.10,
                },
                {
                    "description": "Isolant thermique 5.5%",
                    "quantite": 50,
                    "prix_unitaire_ht": 20.0,
                    "taux_tva": 0.055,
                },
                {
                    "description": "Service non assujetti 0%",
                    "quantite": 1,
                    "prix_unitaire_ht": 100.0,
                    "taux_tva": 0.0,
                }
            ],
            "conditions": {"acompte_pourcentage": 40}
        }

        result = recalculer_totaux(devis)

        # Montants HT par ligne
        assert result["lignes"][0]["montant_ht"] == 1050.0  # 3 * 350
        assert result["lignes"][1]["montant_ht"] == 1000.0  # 2 * 500
        assert result["lignes"][2]["montant_ht"] == 1000.0  # 50 * 20
        assert result["lignes"][3]["montant_ht"] == 100.0   # 1 * 100

        sous_total = 1050.0 + 1000.0 + 1000.0 + 100.0
        assert result["totaux"]["sous_total_ht"] == 3150.0

        # TVA par taux
        assert result["totaux"]["montant_tva_20"] == 210.0  # 1050 * 0.20
        assert result["totaux"]["montant_tva_10"] == 100.0  # 1000 * 0.10
        assert result["totaux"]["montant_tva_55"] == 55.0   # 1000 * 0.055
        assert result["totaux"]["montant_tva_zero"] == 0.0 if "montant_tva_zero" in result["totaux"] else True

        # Total TTC
        total_ttc = 3150.0 + 210.0 + 100.0 + 55.0
        assert result["totaux"]["total_ttc"] == total_ttc

        # Acompte 40%
        acompte = total_ttc * 0.40
        assert result["conditions"]["acompte_montant_ttc"] == acompte


class TestIntegrationFactureWorkflow:
    """Tests d'intégration du flux devis → facture"""

    def test_devis_to_simple_facture(self):
        """Test la conversion simple devis → facture."""
        devis = {
            "meta": {
                "numero_devis": "DEVIS-20260331-001",
                "date_emission": "31/03/2026",
            },
            "client": {"nom": "Client Test"},
            "lignes": [
                {
                    "description": "Travaux",
                    "quantite": 1,
                    "prix_unitaire_ht": 1000.0,
                    "taux_tva": 0.20,
                }
            ],
            "conditions": {"acompte_pourcentage": 30},
            "artisan": {"iban": "FR1234567890"},
        }

        # Convertir en facture
        facture = devis_to_facture(devis)

        # Vérifications
        assert facture["meta"]["numero_facture"] == "FAC-20260331-001"
        assert facture["meta"]["numero_devis_origine"] == "DEVIS-20260331-001"
        assert facture["meta"]["date_devis_origine"] == "31/03/2026"
        assert "date_validite" not in facture["meta"]
        assert facture["artisan"]["iban"] == "FR1234567890"

    def test_devis_to_facture_typed_acompte(self):
        """Test la création d'une facture d'acompte."""
        devis = {
            "meta": {
                "numero_devis": "DEVIS-20260331-001",
                "date_emission": "31/03/2026",
            },
            "client": {"nom": "Client"},
            "lignes": [
                {
                    "description": "Travaux",
                    "quantite": 1,
                    "prix_unitaire_ht": 1000.0,
                    "taux_tva": 0.20,
                }
            ],
            "totaux": {"total_ttc": 1200.0},
            "conditions": {"acompte_pourcentage": 30},
            "artisan": {"iban": "FR1234567890"},
        }

        # Créer facture d'acompte (30%)
        montant_acompte = 1200.0 * 0.30  # 360
        facture = devis_to_facture_typed(
            devis,
            type_facture="acompte",
            montant_ttc=montant_acompte,
            deja_facture_ttc=0.0,
        )

        # Vérifications
        assert facture["meta"]["type_facture"] == "acompte"
        assert "ACOMPTE" in facture["meta"]["numero_facture"]
        assert facture["facturation"]["montant_cette_facture_ttc"] == montant_acompte
        assert facture["facturation"]["deja_facture_ttc"] == 0.0
        assert facture["facturation"]["reste_apres_ttc"] == 840.0  # 1200 - 360

    def test_devis_to_facture_typed_solde(self):
        """Test la création d'une facture de solde."""
        devis = {
            "meta": {
                "numero_devis": "DEVIS-20260331-001",
                "date_emission": "31/03/2026",
            },
            "client": {"nom": "Client"},
            "lignes": [
                {
                    "description": "Travaux",
                    "quantite": 1,
                    "prix_unitaire_ht": 1000.0,
                    "taux_tva": 0.20,
                }
            ],
            "totaux": {"total_ttc": 1200.0},
            "conditions": {"acompte_pourcentage": 30},
            "artisan": {"iban": "FR1234567890"},
        }

        # Acompte déjà facturé
        deja_facture = 360.0
        solde_restant = 1200.0 - deja_facture

        facture = devis_to_facture_typed(
            devis,
            type_facture="solde",
            montant_ttc=solde_restant,
            deja_facture_ttc=deja_facture,
        )

        # Vérifications
        assert facture["meta"]["type_facture"] == "solde"
        assert "SOLDE" in facture["meta"]["numero_facture"]
        assert facture["facturation"]["montant_cette_facture_ttc"] == solde_restant
        assert facture["facturation"]["deja_facture_ttc"] == deja_facture
        assert facture["facturation"]["reste_apres_ttc"] == 0.0

    def test_devis_to_facture_typed_intermediaire(self):
        """Test la création d'une situation intermédiaire."""
        devis = {
            "meta": {
                "numero_devis": "DEVIS-20260331-001",
                "date_emission": "31/03/2026",
            },
            "client": {"nom": "Client"},
            "lignes": [
                {
                    "description": "Travaux",
                    "quantite": 1,
                    "prix_unitaire_ht": 1000.0,
                    "taux_tva": 0.20,
                }
            ],
            "totaux": {"total_ttc": 1200.0},
            "conditions": {"acompte_pourcentage": 30},
            "artisan": {"iban": "FR1234567890"},
        }

        # Acompte déjà facturé
        facture_acompte = {
            "meta": {
                "numero_facture": "FAC-20260331-001-ACOMPTE",
                "type_facture": "acompte",
            },
            "facturation": {
                "montant_cette_facture_ttc": 360.0,
            }
        }

        # Situation intermédiaire : 600 €
        deja_facture = 360.0
        montant_inter = 600.0

        facture = devis_to_facture_typed(
            devis,
            type_facture="intermediaire",
            montant_ttc=montant_inter,
            deja_facture_ttc=deja_facture,
            factures_precedentes=[facture_acompte],
        )

        # Vérifications
        assert facture["meta"]["type_facture"] == "intermediaire"
        assert "INTER-01" in facture["meta"]["numero_facture"]
        assert facture["facturation"]["montant_cette_facture_ttc"] == montant_inter
        assert facture["facturation"]["deja_facture_ttc"] == deja_facture
        assert facture["facturation"]["reste_apres_ttc"] == 240.0  # 1200 - 360 - 600
        assert len(facture["facturation"]["historique"]) == 1

    def test_facture_sequence_acompte_inter_solde(self):
        """Test la séquence complète : acompte → intermédiaire → solde."""
        devis = {
            "meta": {
                "numero_devis": "DEVIS-20260331-001",
                "date_emission": "31/03/2026",
            },
            "client": {"nom": "Client"},
            "lignes": [
                {
                    "description": "Travaux",
                    "quantite": 1,
                    "prix_unitaire_ht": 1000.0,
                    "taux_tva": 0.20,
                }
            ],
            "totaux": {"total_ttc": 1200.0},
            "conditions": {"acompte_pourcentage": 30},
            "artisan": {"iban": "FR1234567890"},
        }

        # Étape 1: Acompte 30% = 360€
        facture_acompte = devis_to_facture_typed(
            devis,
            type_facture="acompte",
            montant_ttc=360.0,
            deja_facture_ttc=0.0,
        )
        assert facture_acompte["facturation"]["montant_cette_facture_ttc"] == 360.0
        assert facture_acompte["facturation"]["reste_apres_ttc"] == 840.0

        # Étape 2: Situation intermédiaire 50% = 600€
        facture_inter = devis_to_facture_typed(
            devis,
            type_facture="intermediaire",
            montant_ttc=600.0,
            deja_facture_ttc=360.0,
            factures_precedentes=[facture_acompte],
        )
        assert facture_inter["facturation"]["montant_cette_facture_ttc"] == 600.0
        assert facture_inter["facturation"]["reste_apres_ttc"] == 240.0

        # Étape 3: Solde = 240€
        facture_solde = devis_to_facture_typed(
            devis,
            type_facture="solde",
            montant_ttc=240.0,
            deja_facture_ttc=960.0,  # 360 + 600
            factures_precedentes=[facture_acompte, facture_inter],
        )
        assert facture_solde["facturation"]["montant_cette_facture_ttc"] == 240.0
        assert facture_solde["facturation"]["reste_apres_ttc"] == 0.0
        assert len(facture_solde["facturation"]["historique"]) == 2

        # Vérifier que le total facturé = total du devis
        total_facture = 360.0 + 600.0 + 240.0
        assert total_facture == 1200.0

    def test_facture_missing_iban_placeholder(self):
        """Test que les factures sans IBAN reçoivent un placeholder."""
        devis = {
            "meta": {"numero_devis": "DEVIS-001"},
            "lignes": [
                {
                    "description": "Travaux",
                    "quantite": 1,
                    "prix_unitaire_ht": 100.0,
                    "taux_tva": 0.20,
                }
            ],
            "totaux": {"total_ttc": 120.0},
            "artisan": {},  # Pas d'IBAN
        }

        facture = devis_to_facture(devis)

        assert facture["artisan"]["iban"] == "[À COMPLÉTER — IBAN requis pour le paiement]"


class TestIntegrationCrossModuleConsistency:
    """Tests d'intégrité entre modules"""

    def test_calculs_consistency_with_pdf_data(self):
        """Test que calculs.py produit les mêmes résultats que les attentes du PDF."""
        devis = {
            "meta": {"numero_devis": "DEVIS-001"},
            "lignes": [
                {
                    "description": "Service A",
                    "quantite": 2,
                    "prix_unitaire_ht": 250.0,
                    "taux_tva": 0.20,
                },
                {
                    "description": "Service B",
                    "quantite": 1,
                    "prix_unitaire_ht": 500.0,
                    "taux_tva": 0.10,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }

        result = recalculer_totaux(devis)

        # Les montants doivent être disponibles pour le PDF
        assert result["lignes"][0]["montant_ht"] is not None
        assert result["lignes"][1]["montant_ht"] is not None
        assert result["totaux"]["sous_total_ht"] is not None
        assert result["totaux"]["total_ttc"] is not None

        # Les montants TVA séparés doivent être disponibles
        assert "montant_tva_20" in result["totaux"]
        assert "montant_tva_10" in result["totaux"]
        assert "montant_tva_55" in result["totaux"]

    def test_rounding_consistency(self):
        """Test que le rounding est cohérent partout."""
        devis = {
            "meta": {"numero_devis": "DEVIS-001"},
            "lignes": [
                {
                    "description": "Montant causant arrondissage",
                    "quantite": 3,
                    "prix_unitaire_ht": 33.33,
                    "taux_tva": 0.10,
                }
            ],
            "conditions": {"acompte_pourcentage": 30}
        }

        result = recalculer_totaux(devis)

        # Tous les montants doivent être arrondis à 2 décimales
        def check_2_decimals(value):
            """Vérifier qu'une valeur est arrondie à 2 décimales."""
            return value == round(value, 2)

        assert check_2_decimals(result["lignes"][0]["montant_ht"])
        assert check_2_decimals(result["totaux"]["sous_total_ht"])
        assert check_2_decimals(result["totaux"]["montant_tva_10"])
        assert check_2_decimals(result["totaux"]["total_ttc"])
        assert check_2_decimals(result["conditions"]["acompte_montant_ttc"])
        assert check_2_decimals(result["conditions"]["solde_montant_ttc"])

    def test_devis_facture_totaux_preserved(self):
        """Test que les totaux sont préservés lors de la conversion devis → facture."""
        devis = {
            "meta": {
                "numero_devis": "DEVIS-20260331-001",
                "date_emission": "31/03/2026",
            },
            "lignes": [
                {
                    "description": "Travaux",
                    "quantite": 1,
                    "prix_unitaire_ht": 1000.0,
                    "taux_tva": 0.20,
                }
            ],
            "totaux": {"total_ttc": 1200.0, "sous_total_ht": 1000.0},
            "conditions": {"acompte_pourcentage": 30},
            "artisan": {},
        }

        facture = devis_to_facture(devis)

        # Les totaux doivent être identiques
        assert facture["totaux"]["total_ttc"] == devis["totaux"]["total_ttc"]
        assert facture["totaux"]["sous_total_ht"] == devis["totaux"]["sous_total_ht"]
        assert facture["lignes"] == devis["lignes"]
