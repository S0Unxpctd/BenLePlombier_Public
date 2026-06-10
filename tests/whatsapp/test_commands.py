"""QA contract item 5 — text command parser covers all 7 commands + variants."""

from __future__ import annotations

import pytest

from whatsapp.commands import Command, parse_command


# All 7 canonical commands must resolve from at least their canonical spelling
# plus at least two variants (case / accents / alternative phrasing).
ALL_COMMANDS = [
    Command.PROFIL,
    Command.DEVIS,
    Command.FACTURE,
    Command.AIDE,
    Command.MENU,
    Command.RECOMMENCER,
    Command.ANNULER,
]


class TestAllSevenCommands:
    @pytest.mark.parametrize("command", ALL_COMMANDS)
    def test_canonical_spelling(self, command):
        assert parse_command(command.value) is command

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("PROFIL", Command.PROFIL),
            ("Mon Profil", Command.PROFIL),
            ("profile", Command.PROFIL),
            ("mes devis", Command.DEVIS),
            ("les devis", Command.DEVIS),
            ("voir devis", Command.DEVIS),
            ("liste devis", Command.DEVIS),
            ("historique", Command.DEVIS),
            ("nouvelle facture", Command.FACTURE),
            ("Factures", Command.FACTURE),
            ("help", Command.AIDE),
            ("aide", Command.AIDE),
            ("?", Command.AIDE),
            ("menu principal", Command.MENU),
            ("OPTIONS", Command.MENU),
            ("reset", Command.RECOMMENCER),
            ("nouveau", Command.RECOMMENCER),
            ("restart", Command.RECOMMENCER),
            ("cancel", Command.ANNULER),
            ("stop", Command.ANNULER),
            ("annuler.", Command.ANNULER),  # trailing punct stripped
            ("/profil", Command.PROFIL),     # slash-prefix variant
            ("/aide", Command.AIDE),
        ],
    )
    def test_recognised_variants(self, text, expected):
        assert parse_command(text) is expected


class TestFreeText:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "bonjour !",
            "Je voudrais un devis pour un remplacement de robinet cuisine",
            # Long messages (>40 chars) containing the keyword are still free text
            "aide moi a creer une facture pour mon client avec les reglages",
        ],
    )
    def test_returns_none(self, text):
        assert parse_command(text) is None

    def test_long_message_with_command_keyword_is_free_text(self):
        text = "j'ai besoin d'aide pour creer un devis pour le chantier de M. Dupont"
        # Exactly > 40 chars → treated as free text
        assert len(text) > 40
        assert parse_command(text) is None


class TestEdgeCases:
    def test_none(self):
        assert parse_command(None) is None  # type: ignore[arg-type]

    def test_whitespace_only(self):
        assert parse_command("   \n\t  ") is None

    def test_only_punct(self):
        assert parse_command("...") is None
        assert parse_command("??") is None
