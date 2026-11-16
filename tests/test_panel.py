"""Seitenleisten-Eintrag und Möbel im Grundriss (ohne Home Assistant)."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

panel = importlib.import_module("haven_nohass.panel")
appdata = importlib.import_module("haven_nohass.appdata")

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "haven"


def test_panel_registration_args():
    args = panel.panel_registration("0.4.0")
    assert args == {
        "frontend_url_path": "haven",
        "webcomponent_name": "haven-panel",
        "sidebar_title": "Haven",
        "sidebar_icon": "mdi:home-heart",
        "module_url": "/haven_static/0.4.0/haven-panel.js",
        "embed_iframe": False,
        "trust_external": False,
        "require_admin": False,
        "config": {"base_url": "/haven_static/0.4.0/", "version": "0.4.0"},
    }
    assert panel.panel_registration("1.0.0", require_admin=True)["require_admin"] is True


def test_static_path_uses_version_for_cache_busting(tmp_path):
    cfg = panel.static_path_config("0.4.0", tmp_path)
    assert cfg == {"url_path": "/haven_static/0.4.0", "path": str(tmp_path), "cache_headers": True}
    assert panel.static_path_config("0.4.1", tmp_path)["url_path"] != cfg["url_path"]
    # Keine Pfadtricks über die Version.
    assert panel.static_url("../../etc") == "/haven_static/.._.._etc"
    assert panel.static_url(None) == "/haven_static/dev"
    assert panel.static_url(" 0.4.0+5 ") == "/haven_static/0.4.0_5"


def test_frontend_available(tmp_path):
    assert not panel.frontend_available(tmp_path / "fehlt")
    assert not panel.frontend_available(tmp_path)
    for f in panel.REQUIRED_FILES:
        (tmp_path / f).write_text("x", encoding="utf-8")
    assert panel.frontend_available(tmp_path)
    assert panel.frontend_dir(ROOT) == ROOT / "frontend"


def test_panel_module_in_repo_matches_config():
    """Das Panel-Modul liegt im Repo und definiert das angemeldete Element."""
    module = ROOT / "frontend" / panel.PANEL_MODULE
    if not module.exists():
        module = Path(__file__).resolve().parents[1] / "panel" / panel.PANEL_MODULE
    text = module.read_text(encoding="utf-8")
    assert f'customElements.define("{panel.PANEL_COMPONENT}"' in text or (
        f"customElements.define('{panel.PANEL_COMPONENT}'" in text
    )
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    for dep in ("frontend", "http", "panel_custom"):
        assert dep in manifest["dependencies"]


PLAN = {"version": 2, "grid": {"cols": 10, "rows": 10}, "rooms": [], "placements": []}
SOFA = {"id": "s", "kind": "sofa", "x": 4, "y": 4, "w": 4.4, "h": 1.8, "rotation": 90}


def test_furniture_validation():
    assert appdata.validate_floorplan({**PLAN, "furniture": [SOFA]}) == []
    assert appdata.validate_floorplan({**PLAN, "furniture": {}}) == ["furniture muss eine Liste sein"]
    assert appdata.validate_floorplan({**PLAN, "furniture": [{"kind": "sofa"}]}) == [
        "furniture: jeder Eintrag braucht ein Feld id"
    ]


def test_old_client_keeps_furniture():
    previous = appdata.build_plan_doc({**PLAN, "furniture": [SOFA]}, 1)
    # Alter Client (ohne Feld): Möbel bleiben.
    doc = appdata.build_plan_doc(dict(PLAN), 2, previous=previous)
    assert doc["plan"]["furniture"] == [SOFA]
    assert doc["updated_at"] == 2
    # Neuer Client löscht bewusst.
    doc = appdata.build_plan_doc({**PLAN, "furniture": []}, 3, previous=previous)
    assert doc["plan"]["furniture"] == []
    # Nichts gespeichert: unverändert, kein leeres Feld erfunden.
    assert "furniture" not in appdata.build_plan_doc(dict(PLAN), previous=None)["plan"]
    assert "furniture" not in appdata.merge_plan_furniture(dict(PLAN), {"plan": None})
    # Das übergebene Dokument wird nicht verändert.
    plan = dict(PLAN)
    appdata.build_plan_doc(plan, previous=previous)
    assert "furniture" not in plan
