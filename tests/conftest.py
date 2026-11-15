"""Lädt exposes.py und z2m.py ohne Home Assistant.

Das Paket ``custom_components.haven`` importiert in ``__init__`` Home Assistant.
Hier wird stattdessen ein leeres Platzhalter-Paket mit demselben Pfad registriert,
sodass nur die HA-freien Module geladen werden.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "custom_components" / "haven"
INVENTORY = ROOT / "tests" / "fixtures" / "inventory.json"

_pkg = types.ModuleType("haven_nohass")
_pkg.__path__ = [str(PKG_DIR)]
sys.modules.setdefault("haven_nohass", _pkg)


@pytest.fixture(scope="session")
def inventory() -> dict:
    return json.loads(INVENTORY.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def devices(inventory) -> dict[str, dict]:
    """Inventar-Geräte nach Name."""
    return {d["name"]: d for d in inventory["z2m_devices"]}
