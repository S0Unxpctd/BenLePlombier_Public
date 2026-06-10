"""Souffl.AI V3 — French-language text-command parser for WhatsApp.

WhatsApp doesn't have the slash-command affordance Telegram does. Artisans
type natural French phrases; this module normalises those phrases into a
canonical ``Command`` enum so the router can dispatch without caring about
typography / capitalisation / accents.

Mapping (see Phase 2 spec § 2.11)
---------------------------------
- ``profil``     → show profile menu             (variants: "mon profil", "profile")
- ``devis``      → list recent quotes            (variants: "mes devis", "liste devis", "voir devis")
- ``facture``    → start invoice flow            (variants: "factures", "nouvelle facture")
- ``aide``       → show help                     (variants: "help", "?")
- ``menu``       → open the main menu list       (quick win from § 2.11)
- ``recommencer``→ wipe conversational state     (variants: "reset", "restart", "nouveau")
- ``annuler``    → cancel current flow only      (variants: "cancel", "stop", "abort")

Philosophy: match **whole-message intent** only. A long voice-dictated
message that *contains* the word "facture" is NOT a command — we only
trigger when the artisan sends a short phrase that is basically the command
itself. Heuristic: message ≤ 40 chars and strips to a known keyword.
"""

from __future__ import annotations

import enum
import unicodedata
from typing import Optional


class Command(enum.Enum):
    PROFIL = "profil"
    DEVIS = "devis"
    FACTURE = "facture"
    AIDE = "aide"
    MENU = "menu"
    RECOMMENCER = "recommencer"
    ANNULER = "annuler"
    EXPORT = "export"


# Canonical keyword → variants we'll accept (already lower-case, accent-stripped).
# Each variant is the full stripped text of the message — we match exactly.
_VARIANTS: dict[Command, frozenset[str]] = {
    Command.PROFIL: frozenset({
        "profil", "profile", "mon profil", "mon profile",
        "/profil", "profil ?",
    }),
    Command.DEVIS: frozenset({
        "devis", "mes devis", "les devis", "liste devis",
        "liste des devis", "voir devis", "voir mes devis",
        "/devis", "historique", "historique devis",
    }),
    Command.FACTURE: frozenset({
        "facture", "factures", "nouvelle facture", "creer facture",
        "creer une facture", "/facture",
    }),
    Command.AIDE: frozenset({
        "aide", "help", "?", "besoin d'aide", "besoin daide",
        "/aide", "/help", "comment ca marche", "ca marche comment",
    }),
    Command.MENU: frozenset({
        "menu", "/menu", "menu principal", "options",
        "actions", "choix",
    }),
    Command.RECOMMENCER: frozenset({
        "recommencer", "reset", "restart", "nouveau",
        "nouveau devis", "/reset", "/recommencer",
        "recommence", "redemarrer", "redemarre",
    }),
    Command.ANNULER: frozenset({
        "annuler", "cancel", "stop", "abort",
        "/annuler", "/cancel", "arreter", "arrete",
    }),
    Command.EXPORT: frozenset({
        "export", "exporter", "archiver", "archive",
        "mes documents", "/export", "/exporter",
    }),
}


# Precompute the reverse map once.
_REVERSE: dict[str, Command] = {v: cmd for cmd, vs in _VARIANTS.items() for v in vs}

_MAX_COMMAND_LEN = 40


def _normalise(text: str) -> str:
    """Lower-case, strip accents, collapse whitespace, drop trailing punct."""
    if not text:
        return ""
    # Strip accents.
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower().strip()
    # Collapse whitespace.
    t = " ".join(t.split())
    # Drop trailing punctuation.
    t = t.rstrip(".!,;:")
    return t


def parse_command(text: str) -> Optional[Command]:
    """Return the ``Command`` this text maps to, or ``None`` if it's free text.

    Long messages (> 40 chars) are always free text — they likely contain
    content the artisan wants the bot to process, not a command.
    """
    if not text:
        return None
    stripped = text.strip()
    if len(stripped) > _MAX_COMMAND_LEN:
        return None
    n = _normalise(stripped)
    if not n:
        return None
    return _REVERSE.get(n)


__all__ = ["Command", "parse_command"]
