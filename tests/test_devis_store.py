"""
Tests unitaires pour devis_store.py — Persistance des devis et factures
"""

import pytest
import json
from pathlib import Path
import tempfile
import sys
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestDevisStore:
    """Tests pour les fonctions de devis_store"""

    @pytest.fixture
    def temp_devis_dir(self):
        """Fixture pour utiliser un répertoire devis temporaire."""
        with tempfile.TemporaryDirectory() as tmpdir:
            devis_dir = Path(tmpdir) / "devis"
            devis_dir.mkdir(parents=True, exist_ok=True)

            # Patcher devis_store pour utiliser le répertoire temporaire
            import devis_store
            original_data_dir = devis_store.DATA_DIR
            devis_store.DATA_DIR = devis_dir

            yield devis_dir

            # Restaurer
            devis_store.DATA_DIR = original_data_dir

    def test_save_devis(self, temp_devis_dir):
        """Test save_devis()."""
        import devis_store

        user_id = 12345
        devis = {
            "meta": {
                "numero_devis": "DEVIS-20260331-001",
                "date_emission": "31/03/2026",
            },
            "client": {"nom": "Client Test"},
            "totaux": {"total_ttc": 1000.0},
        }

        path = devis_store.save_devis(user_id, devis)

        assert Path(path).exists()
        assert "DEVIS-20260331-001.json" in path

        # Vérifier le contenu
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
        assert saved["meta"]["numero_devis"] == "DEVIS-20260331-001"
        assert saved["client"]["nom"] == "Client Test"

    def test_load_devis(self, temp_devis_dir):
        """Test load_devis()."""
        import devis_store

        user_id = 12345
        devis = {
            "meta": {
                "numero_devis": "DEVIS-20260331-001",
            },
            "client": {"nom": "Test"},
        }

        # Sauvegarder
        devis_store.save_devis(user_id, devis)

        # Charger
        loaded = devis_store.load_devis(user_id, "DEVIS-20260331-001")

        assert loaded is not None
        assert loaded["meta"]["numero_devis"] == "DEVIS-20260331-001"
        assert loaded["client"]["nom"] == "Test"

    def test_load_devis_not_found(self, temp_devis_dir):
        """Test load_devis() avec un devis inexistant."""
        import devis_store

        user_id = 12345
        loaded = devis_store.load_devis(user_id, "NONEXISTENT")

        assert loaded is None

    def test_list_devis(self, temp_devis_dir):
        """Test list_devis()."""
        import devis_store

        user_id = 12345

        # Créer plusieurs devis
        for i in range(5):
            devis = {
                "meta": {
                    "numero_devis": f"DEVIS-20260331-{i:03d}",
                    "date_emission": f"31/03/2026",
                    "reference_chantier": f"Chantier {i}",
                },
                "client": {"nom": f"Client {i}"},
                "totaux": {"total_ttc": 1000.0 + i * 100},
            }
            devis_store.save_devis(user_id, devis)

        # Lister
        devis_list = devis_store.list_devis(user_id, limit=3)

        assert len(devis_list) == 3
        for item in devis_list:
            assert "numero" in item
            assert "date" in item
            assert "client" in item
            assert "total_ttc" in item

    def test_list_devis_order(self, temp_devis_dir):
        """Test que list_devis() retourne les plus récents en premier."""
        import devis_store
        import time

        user_id = 12345

        # Créer deux devis avec un délai
        devis1 = {
            "meta": {
                "numero_devis": "DEVIS-001",
                "date_emission": "31/03/2026",
            },
            "client": {"nom": "Client 1"},
            "totaux": {"total_ttc": 1000.0},
        }
        path1 = devis_store.save_devis(user_id, devis1)

        time.sleep(0.1)  # Petit délai pour différencier les temps de modification

        devis2 = {
            "meta": {
                "numero_devis": "DEVIS-002",
                "date_emission": "31/03/2026",
            },
            "client": {"nom": "Client 2"},
            "totaux": {"total_ttc": 2000.0},
        }
        path2 = devis_store.save_devis(user_id, devis2)

        # Lister
        devis_list = devis_store.list_devis(user_id, limit=2)

        # Le plus récent (DEVIS-002) doit être en premier
        assert devis_list[0]["numero"] == "DEVIS-002"
        assert devis_list[1]["numero"] == "DEVIS-001"

    def test_get_last_devis(self, temp_devis_dir):
        """Test get_last_devis()."""
        import devis_store
        import time

        user_id = 12345

        devis1 = {
            "meta": {"numero_devis": "DEVIS-001"},
            "client": {"nom": "Client 1"},
        }
        devis_store.save_devis(user_id, devis1)

        time.sleep(0.1)

        devis2 = {
            "meta": {"numero_devis": "DEVIS-002"},
            "client": {"nom": "Client 2"},
        }
        devis_store.save_devis(user_id, devis2)

        # Récupérer le dernier
        last = devis_store.get_last_devis(user_id)

        assert last is not None
        assert last["meta"]["numero_devis"] == "DEVIS-002"

    def test_get_last_devis_empty(self, temp_devis_dir):
        """Test get_last_devis() avec aucun devis."""
        import devis_store

        user_id = 12345
        last = devis_store.get_last_devis(user_id)

        assert last is None

    def test_numero_with_special_characters(self, temp_devis_dir):
        """Test que les numéros avec caractères spéciaux sont sécurisés."""
        import devis_store

        user_id = 12345
        devis = {
            "meta": {
                "numero_devis": "DEVIS-2026/03-31/001",  # Contient / et -
            },
            "client": {"nom": "Test"},
        }

        path = devis_store.save_devis(user_id, devis)

        # Le fichier doit être créé avec les caractères spéciaux remplacés
        assert Path(path).exists()

        # Charger en utilisant le numéro original
        loaded = devis_store.load_devis(user_id, "DEVIS-2026/03-31/001")
        assert loaded is not None


class TestFactureStore:
    """Tests pour les fonctions de facture"""

    @pytest.fixture
    def temp_devis_dir(self):
        """Fixture pour utiliser un répertoire devis temporaire."""
        with tempfile.TemporaryDirectory() as tmpdir:
            devis_dir = Path(tmpdir) / "devis"
            devis_dir.mkdir(parents=True, exist_ok=True)

            import devis_store
            original_data_dir = devis_store.DATA_DIR
            devis_store.DATA_DIR = devis_dir

            yield devis_dir

            devis_store.DATA_DIR = original_data_dir

    def test_save_facture(self, temp_devis_dir):
        """Test save_facture()."""
        import devis_store

        user_id = 12345
        facture = {
            "meta": {
                "numero_facture": "FAC-20260331-001-ACOMPTE",
                "numero_devis_origine": "DEVIS-20260331-001",
                "date_emission": "31/03/2026",
            },
            "client": {"nom": "Client Test"},
        }

        path = devis_store.save_facture(user_id, facture)

        assert Path(path).exists()
        assert "FAC-20260331-001-ACOMPTE.json" in path

    def test_load_facture(self, temp_devis_dir):
        """Test load_facture()."""
        import devis_store

        user_id = 12345
        facture = {
            "meta": {
                "numero_facture": "FAC-20260331-001-ACOMPTE",
                "numero_devis_origine": "DEVIS-20260331-001",
            },
            "client": {"nom": "Test"},
        }

        # Sauvegarder
        devis_store.save_facture(user_id, facture)

        # Charger
        loaded = devis_store.load_facture(user_id, "FAC-20260331-001-ACOMPTE")

        assert loaded is not None
        assert loaded["meta"]["numero_facture"] == "FAC-20260331-001-ACOMPTE"

    def test_load_facture_not_found(self, temp_devis_dir):
        """Test load_facture() avec une facture inexistante."""
        import devis_store

        user_id = 12345
        loaded = devis_store.load_facture(user_id, "NONEXISTENT")

        assert loaded is None

    def test_get_factures_for_devis(self, temp_devis_dir):
        """Test get_factures_for_devis()."""
        import devis_store

        user_id = 12345
        numero_devis = "DEVIS-20260331-001"

        # Créer plusieurs factures pour le même devis
        for type_fac in ["ACOMPTE", "INTER-01", "SOLDE"]:
            facture = {
                "meta": {
                    "numero_facture": f"FAC-20260331-001-{type_fac}",
                    "numero_devis_origine": numero_devis,
                    "date_emission": "31/03/2026",
                    "type_facture": type_fac,
                },
                "facturation": {
                    "montant_cette_facture_ttc": 1000.0,
                },
            }
            devis_store.save_facture(user_id, facture)

        # Créer une facture pour un autre devis
        autre_facture = {
            "meta": {
                "numero_facture": "FAC-20260331-002-ACOMPTE",
                "numero_devis_origine": "DEVIS-20260331-002",
                "date_emission": "31/03/2026",
                "type_facture": "ACOMPTE",
            },
            "facturation": {
                "montant_cette_facture_ttc": 500.0,
            },
        }
        devis_store.save_facture(user_id, autre_facture)

        # Récupérer les factures du premier devis
        factures = devis_store.get_factures_for_devis(user_id, numero_devis)

        assert len(factures) == 3
        for fac in factures:
            assert fac["meta"]["numero_devis_origine"] == numero_devis

    def test_get_factures_for_devis_empty(self, temp_devis_dir):
        """Test get_factures_for_devis() sans aucune facture."""
        import devis_store

        user_id = 12345
        factures = devis_store.get_factures_for_devis(user_id, "DEVIS-NONEXISTENT")

        assert factures == []

    def test_get_total_deja_facture(self, temp_devis_dir):
        """Test get_total_deja_facture()."""
        import devis_store

        user_id = 12345
        numero_devis = "DEVIS-20260331-001"

        # Créer plusieurs factures avec différents montants
        montants = [500.0, 750.0, 250.0]
        for i, montant in enumerate(montants):
            facture = {
                "meta": {
                    "numero_facture": f"FAC-20260331-001-{i}",
                    "numero_devis_origine": numero_devis,
                    "date_emission": "31/03/2026",
                    "type_facture": "ACOMPTE",
                },
                "facturation": {
                    "montant_cette_facture_ttc": montant,
                },
            }
            devis_store.save_facture(user_id, facture)

        # Calculer le total
        total = devis_store.get_total_deja_facture(user_id, numero_devis)

        assert total == 1500.0  # 500 + 750 + 250

    def test_get_total_deja_facture_empty(self, temp_devis_dir):
        """Test get_total_deja_facture() sans factures."""
        import devis_store

        user_id = 12345
        total = devis_store.get_total_deja_facture(user_id, "DEVIS-NONEXISTENT")

        assert total == 0.0

    def test_get_total_deja_facture_rounding(self, temp_devis_dir):
        """Test que get_total_deja_facture() arrondit correctement."""
        import devis_store

        user_id = 12345
        numero_devis = "DEVIS-20260331-001"

        # Créer des factures avec montants qui causent arrondissage
        montants = [100.11, 200.22, 300.33]
        for i, montant in enumerate(montants):
            facture = {
                "meta": {
                    "numero_facture": f"FAC-20260331-001-{i}",
                    "numero_devis_origine": numero_devis,
                    "date_emission": "31/03/2026",
                    "type_facture": "ACOMPTE",
                },
                "facturation": {
                    "montant_cette_facture_ttc": montant,
                },
            }
            devis_store.save_facture(user_id, facture)

        total = devis_store.get_total_deja_facture(user_id, numero_devis)

        # Somme = 600.66, doit être arrondi à 2 décimales
        assert total == 600.66
        assert isinstance(total, float)

    def test_list_devis_with_missing_files(self, temp_devis_dir):
        """Test list_devis() avec des fichiers corrompus."""
        import devis_store

        user_id = 12345

        # Créer un devis valide
        devis = {
            "meta": {
                "numero_devis": "DEVIS-20260331-001",
                "date_emission": "31/03/2026",
            },
            "client": {"nom": "Client 1"},
            "totaux": {"total_ttc": 1000.0},
        }
        devis_store.save_devis(user_id, devis)

        # Créer un fichier JSON corrompu
        user_dir = Path(temp_devis_dir) / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        corrupted_file = user_dir / "DEVIS-20260331-999.json"
        corrupted_file.write_text("{ invalid json }", encoding="utf-8")

        # list_devis() doit ignorer le fichier corrompu
        devis_list = devis_store.list_devis(user_id)

        assert len(devis_list) == 1
        assert devis_list[0]["numero"] == "DEVIS-20260331-001"
