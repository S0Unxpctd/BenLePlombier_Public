"""Phase 1 QA contract item 3: `core/` modules must be importable without
the transport libraries.

We check this by scanning `core/` sources for forbidden top-level imports.
That's more reliable than doing sys.modules gymnastics, because pytest and
the dev environment already import the transport libs for other tests.

Forbidden imports at module-import time (not inside functions):
- telegram / telegram.ext
- fastapi, flask
- whatsapp adapters (will live under V3/whatsapp/ in Phase 2)

Lazy imports (inside functions) are allowed — that's the whole point of
making the module import-safe.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

V3_ROOT = Path(__file__).resolve().parents[2]
CORE_DIR = V3_ROOT / "core"


FORBIDDEN_TOPLEVEL_PREFIXES = (
    "telegram",
    "fastapi",
    "flask",
    "whatsapp",
)


def _collect_toplevel_imports(source: str) -> list[str]:
    """Return a list of module names imported at module top level."""
    tree = ast.parse(source)
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.append(node.module or "")
    return names


class TestTransportIsolation:
    def test_core_has_no_forbidden_toplevel_imports(self):
        offenders: list[tuple[str, str]] = []
        for py in CORE_DIR.glob("*.py"):
            source = py.read_text(encoding="utf-8")
            for name in _collect_toplevel_imports(source):
                for forbidden in FORBIDDEN_TOPLEVEL_PREFIXES:
                    if name == forbidden or name.startswith(forbidden + "."):
                        offenders.append((py.name, name))
        assert not offenders, (
            "V3/core/ modules must not import transport libraries at top level. "
            f"Offenders: {offenders}"
        )

    def test_core_modules_are_importable_cold(self):
        """Import each core/*.py freshly and assert it succeeds."""
        if str(V3_ROOT) not in sys.path:
            sys.path.insert(0, str(V3_ROOT))
        import importlib

        for mod_name in ("core.state_store", "core.quote_flow",
                         "core.edit_flow", "core.profile_flow"):
            mod = importlib.import_module(mod_name)
            assert mod is not None
