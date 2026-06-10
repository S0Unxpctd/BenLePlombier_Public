"""
Tests unitaires pour client_store.py — Gestion des profils artisans
"""

import pytest
import json
from pathlib import Path
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestClientStore:
    """Tests pour les fonctions de client_store"""

    @pytest.fixture
    def temp_client_file(self):
        """Fixture pour utiliser un fichier clients.json temporaire."""
        with tempfile.TemporaryDirectory() as tmpdir:
            clients_file = Path(tmpdir) / "clients.json"
            clients_file.write_text("{}", encoding="utf-8")

            # Patcher client_store pour utiliser le fichier temporaire
            import client_store
            original_clients_file = client_store.CLIENTS_FILE
            client_store.CLIENTS_FILE = clients_file

            yield clients_file

            # Restaurer
            client_store.CLIENTS_FILE = original_clients_file

    def test_save_and_get_client(self, temp_client_file):
        """Test save_client et get_client."""
        import client_store

        user_id = 12345
        profile = {
            "raison_sociale": "Plomberie Martin",
            "dirigeant": "Jean Martin",
            "email": "jean@example.com",
            "telephone": "06 12 34 56 78",
        }

        # Sauvegarder
        client_store.save_client(user_id, profile)

        # Charger
        retrieved = client_store.get_client(user_id)

        assert retrieved is not None
        assert retrieved["raison_sociale"] == "Plomberie Martin"
        assert retrieved["dirigeant"] == "Jean Martin"
        assert retrieved["email"] == "jean@example.com"

    def test_client_exists(self, temp_client_file):
        """Test client_exists()."""
        import client_store

        user_id = 12345
        profile = {"raison_sociale": "Test"}

        # Vérifier que le client n'existe pas
        assert not client_store.client_exists(user_id)

        # Sauvegarder
        client_store.save_client(user_id, profile)

        # Vérifier que le client existe
        assert client_store.client_exists(user_id)

    def test_delete_client(self, temp_client_file):
        """Test delete_client()."""
        import client_store

        user_id = 12345
        profile = {"raison_sociale": "Test"}

        # Sauvegarder
        client_store.save_client(user_id, profile)
        assert client_store.client_exists(user_id)

        # Supprimer
        client_store.delete_client(user_id)

        # Vérifier que le client n'existe plus
        assert not client_store.client_exists(user_id)
        assert client_store.get_client(user_id) is None

    def test_multiple_clients(self, temp_client_file):
        """Test avec plusieurs clients."""
        import client_store

        profiles = {
            111: {"raison_sociale": "Artisan 1"},
            222: {"raison_sociale": "Artisan 2"},
            333: {"raison_sociale": "Artisan 3"},
        }

        # Sauvegarder tous les clients
        for user_id, profile in profiles.items():
            client_store.save_client(user_id, profile)

        # Vérifier que tous existent
        for user_id in profiles:
            assert client_store.client_exists(user_id)

        # Supprimer un client
        client_store.delete_client(222)

        # Vérifier que les autres existent toujours
        assert client_store.client_exists(111)
        assert not client_store.client_exists(222)
        assert client_store.client_exists(333)

    def test_empty_profile(self, temp_client_file):
        """Test avec un profil vide."""
        import client_store

        user_id = 12345
        profile = {}

        client_store.save_client(user_id, profile)
        retrieved = client_store.get_client(user_id)

        assert retrieved == {}

    def test_profile_with_missing_fields(self, temp_client_file):
        """Test avec un profil partiel."""
        import client_store

        user_id = 12345
        profile = {
            "raison_sociale": "Test",
            # Autres champs absents
        }

        client_store.save_client(user_id, profile)
        retrieved = client_store.get_client(user_id)

        assert retrieved["raison_sociale"] == "Test"
        assert "dirigeant" not in retrieved

    def test_list_clients(self, temp_client_file):
        """Test list_clients()."""
        import client_store

        profiles = {
            111: {"raison_sociale": "Artisan 1"},
            222: {"raison_sociale": "Artisan 2"},
        }

        for user_id, profile in profiles.items():
            client_store.save_client(user_id, profile)

        all_clients = client_store.list_clients()

        assert "111" in all_clients
        assert "222" in all_clients
        assert all_clients["111"]["raison_sociale"] == "Artisan 1"
        assert all_clients["222"]["raison_sociale"] == "Artisan 2"

    def test_nonexistent_client_returns_none(self, temp_client_file):
        """Test que get_client retourne None pour un client inexistant."""
        import client_store

        result = client_store.get_client(99999)
        assert result is None

    def test_profile_persistence(self, temp_client_file):
        """Test que les données sont bien persistées dans le fichier JSON."""
        import client_store

        user_id = 12345
        profile = {"raison_sociale": "Test", "email": "test@example.com"}

        client_store.save_client(user_id, profile)

        # Vérifier directement le fichier
        file_content = json.loads(temp_client_file.read_text(encoding="utf-8"))
        assert "12345" in file_content
        assert file_content["12345"]["raison_sociale"] == "Test"

    def test_special_characters_in_profile(self, temp_client_file):
        """Test avec des caractères spéciaux (accents, symboles)."""
        import client_store

        user_id = 12345
        profile = {
            "raison_sociale": "Plomberie & Chauffage Côte d'Azur",
            "adresse": "123 Rue de l'École, 75000 Paris",
            "dirigeant": "François Müller",
        }

        client_store.save_client(user_id, profile)
        retrieved = client_store.get_client(user_id)

        assert retrieved["raison_sociale"] == "Plomberie & Chauffage Côte d'Azur"
        assert retrieved["adresse"] == "123 Rue de l'École, 75000 Paris"
        assert retrieved["dirigeant"] == "François Müller"


class TestInjectProfileInPrompt:
    """Tests pour inject_profile_in_prompt()"""

    def test_basic_replacement(self):
        """Test le remplacement de placeholders basiques."""
        from client_store import inject_profile_in_prompt

        prompt = (
            "Entreprise: [NOM_ENTREPRISE]\n"
            "Contact: [EMAIL]\n"
            "Téléphone: [TELEPHONE]"
        )
        profile = {
            "raison_sociale": "Plomberie Martin",
            "email": "contact@martin.fr",
            "telephone": "06 12 34 56 78",
        }

        result = inject_profile_in_prompt(prompt, profile)

        assert "Plomberie Martin" in result
        assert "contact@martin.fr" in result
        assert "06 12 34 56 78" in result
        assert "[NOM_ENTREPRISE]" not in result
        assert "[EMAIL]" not in result

    def test_missing_profile_fields(self):
        """Test avec des champs de profil manquants."""
        from client_store import inject_profile_in_prompt

        prompt = (
            "Entreprise: [NOM_ENTREPRISE]\n"
            "Email: [EMAIL]\n"
            "Téléphone: [TELEPHONE]"
        )
        profile = {
            "raison_sociale": "Plomberie Martin",
            # email et telephone manquent
        }

        result = inject_profile_in_prompt(prompt, profile)

        assert "Plomberie Martin" in result
        # Les placeholders manquants doivent être remplacés par [À COMPLÉTER]
        assert "[À COMPLÉTER]" in result

    def test_custom_tarifs_replacement(self):
        """Test le remplacement des tarifs personnalisés."""
        from client_store import inject_profile_in_prompt

        prompt = "Tarif journée standard: 350,00 € HT / jour\nTarif journée spécialisée: 450,00 € HT / jour"
        profile = {
            "journee_standard_ht": 400.0,
            "journee_specialise_ht": 500.0,
        }

        result = inject_profile_in_prompt(prompt, profile)

        assert "400.00 € HT / jour" in result
        assert "500.00 € HT / jour" in result
        assert "350,00 € HT / jour" not in result
        assert "450,00 € HT / jour" not in result

    def test_deplacement_replacement(self):
        """Test le remplacement du tarif de déplacement."""
        from client_store import inject_profile_in_prompt

        prompt = "Déplacement: 40,00 € HT / jour de présence"
        profile = {
            "deplacement_par_jour": 60.0,
        }

        result = inject_profile_in_prompt(prompt, profile)

        assert "60.00 € HT / jour de présence" in result
        assert "40,00 € HT / jour" not in result

    def test_empty_profile(self):
        """Test avec un profil vide."""
        from client_store import inject_profile_in_prompt

        prompt = "Entreprise: [NOM_ENTREPRISE]"
        profile = {}

        result = inject_profile_in_prompt(prompt, profile)

        assert "[À COMPLÉTER]" in result

    def test_none_values_not_replaced(self):
        """Test que les valeurs None ne remplacent pas les placeholders."""
        from client_store import inject_profile_in_prompt

        prompt = "Tarif: 350,00 € HT / jour"
        profile = {
            "journee_standard_ht": None,
        }

        result = inject_profile_in_prompt(prompt, profile)

        # Le placeholder ne doit PAS être remplacé puisque la valeur est None
        assert "350,00 € HT / jour" in result

    def test_all_placeholders(self):
        """Test tous les placeholders connus."""
        from client_store import inject_profile_in_prompt

        prompt = (
            "[NOM_ENTREPRISE] [FORME_JURIDIQUE] [NOM_PRENOM_DIRIGEANT] "
            "[ADRESSE_COMPLETE] [TELEPHONE] [EMAIL] [SITE_WEB] "
            "[NUMERO_SIRET] [NUMERO_TVA_INTRACOM] [NUMERO_RCS_OU_RM] "
            "[CODE_APE] [NOM_ASSUREUR] [NUMERO_POLICE_ASSURANCE] "
            "[ANNEE_VALIDITE_ASSURANCE]"
        )
        profile = {
            "raison_sociale": "Test SARL",
            "forme_juridique": "SARL",
            "dirigeant": "John Doe",
            "adresse": "123 Rue Test",
            "telephone": "01 23 45 67 89",
            "email": "test@example.com",
            "site_web": "www.test.com",
            "siret": "12345678901234",
            "tva_intracom": "FR12345678901",
            "rcs_rm": "RCS Paris 123456",
            "code_ape": "4322A",
            "assurance_decennale": "Assurance Test",
            "numero_police": "POL123456",
            "annee_assurance": "2026",
        }

        result = inject_profile_in_prompt(prompt, profile)

        assert "Test SARL" in result
        assert "SARL" in result
        assert "John Doe" in result
        assert "123 Rue Test" in result
        assert "01 23 45 67 89" in result
        assert "test@example.com" in result
        assert "www.test.com" in result
        assert "12345678901234" in result
        assert "FR12345678901" in result
        assert "RCS Paris 123456" in result
        assert "4322A" in result
        assert "Assurance Test" in result
        assert "POL123456" in result
        assert "2026" in result

    def test_empty_string_values_not_replaced(self):
        """Test que les valeurs vides ("") ne remplacent pas."""
        from client_store import inject_profile_in_prompt

        prompt = "Email: [EMAIL] | Site: [SITE_WEB]"
        profile = {
            "email": "test@example.com",
            "site_web": "",  # Valeur vide
        }

        result = inject_profile_in_prompt(prompt, profile)

        assert "test@example.com" in result
        # site_web étant vide, le placeholder doit rester
        assert "[SITE_WEB]" in result


class TestEmptyProfile:
    """Tests pour empty_profile()"""

    def test_empty_profile_structure(self):
        """Test que empty_profile() retourne un dictionnaire valide."""
        from client_store import empty_profile

        profile = empty_profile()

        assert isinstance(profile, dict)
        assert "raison_sociale" in profile
        assert "dirigeant" in profile
        assert "adresse" in profile
        assert "email" in profile
        assert "siret" in profile
        assert "iban" in profile
        assert "gmail_address" in profile
        assert "gmail_app_password" in profile

    def test_empty_profile_has_defaults(self):
        """Test que les champs ont des valeurs par défaut."""
        from client_store import empty_profile

        profile = empty_profile()

        assert profile["raison_sociale"] == ""
        assert profile["tva_intracom"] == "Non assujetti à la TVA"
        assert profile["rcs_rm"] == "[À COMPLÉTER]"
        assert profile["journee_standard_ht"] is None
        assert profile["journee_specialise_ht"] is None
