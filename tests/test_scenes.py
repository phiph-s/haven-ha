"""Szenen der Z2M-Gruppen: IDs, Namen, Payloads, Looks."""

from __future__ import annotations

import importlib

import pytest

scenes = importlib.import_module("haven_nohass.scenes")
commands = importlib.import_module("haven_nohass.commands")
Z2MError = commands.Z2MError

GROUP = {
    "id": 3,
    "friendly_name": "ZGSalon",
    "members": [],
    "scenes": [{"id": 0, "name": "Hell"}, {"id": 1, "name": "Kinoabend"}, {"id": 3, "name": "Fokus"}],
}


def _code(fn, *args, **kwargs) -> str:
    with pytest.raises(Z2MError) as err:
        fn(*args, **kwargs)
    return err.value.code


def test_new_scene_takes_smallest_free_id():
    scene_id, name, payload = scenes.plan_store(GROUP, "  Abend   rot ")
    assert scene_id == 2
    assert name == "Abend rot"
    assert payload == {"scene_store": {"ID": 2, "name": "Abend rot"}}


def test_new_scene_in_empty_group_starts_at_zero():
    assert scenes.plan_store({"id": 1, "scenes": []}, "Erste")[0] == 0


def test_new_scene_with_taken_name_is_rejected_case_insensitive():
    assert _code(scenes.plan_store, GROUP, "kinoabend") == "name_taken"


def test_overwrite_keeps_id_and_name():
    scene_id, name, payload = scenes.plan_store(GROUP, None, 1)
    assert (scene_id, name) == (1, "Kinoabend")
    assert payload == {"scene_store": {"ID": 1, "name": "Kinoabend"}}


def test_overwrite_with_new_name_and_conflicts():
    assert scenes.plan_store(GROUP, "Kino", 1)[1] == "Kino"
    assert scenes.plan_store(GROUP, "KINOABEND", 1)[1] == "KINOABEND"  # eigener Name zählt nicht
    assert _code(scenes.plan_store, GROUP, "Fokus", 1) == "name_taken"
    assert _code(scenes.plan_store, GROUP, None, 9) == "not_found"


def test_names_are_validated():
    assert _code(scenes.plan_store, GROUP, "   ") == "invalid_format"
    assert _code(scenes.plan_store, GROUP, "x" * 41) == "invalid_format"


def test_full_group():
    full = {"id": 1, "scenes": [{"id": i, "name": f"S{i}"} for i in range(256)]}
    assert _code(scenes.plan_store, full, "Noch eine") == "full"


def test_rename_remove_recall():
    assert scenes.plan_rename(GROUP, 3, "Arbeiten") == ("Arbeiten", {"scene_rename": {"ID": 3, "name": "Arbeiten"}})
    assert _code(scenes.plan_rename, GROUP, 3, "Hell") == "name_taken"
    assert _code(scenes.plan_rename, GROUP, 7, "Neu") == "not_found"
    assert scenes.plan_remove(GROUP, 0) == {"scene_remove": 0}
    assert _code(scenes.plan_remove, GROUP, 7) == "not_found"
    assert scenes.plan_recall(GROUP, 1) == {"scene_recall": 1}


def test_find_group():
    assert scenes.find_group([GROUP], 3) is GROUP
    assert _code(scenes.find_group, [GROUP], 4) == "not_found"


def test_confirmation_predicates():
    after_store = [{**GROUP, "scenes": [*GROUP["scenes"], {"id": 2, "name": "Abend"}]}]
    assert scenes.stored(3, 2, "Abend")(after_store)
    assert not scenes.stored(3, 2, "Abend")([GROUP])
    assert not scenes.stored(3, 1, "Kino")([GROUP])
    assert scenes.removed(3, 7)([GROUP])
    assert not scenes.removed(3, 1)([GROUP])
    assert scenes.removed(9, 1)([GROUP])


def test_validate_look():
    look = {"colors": ["#FFAA00", "#112233"], "brightness": 40, "kelvin": None}
    assert scenes.validate_look(look) == {"colors": ["#ffaa00", "#112233"], "brightness": 40, "kelvin": None}
    assert scenes.validate_look({"colors": ["#ffffff"], "brightness": 100, "kelvin": 2700})["kelvin"] == 2700
    for bad in (
        None,
        {"colors": [], "brightness": 10},
        {"colors": ["red"], "brightness": 10},
        {"colors": ["#ffffff"] * 4, "brightness": 10},
        {"colors": ["#ffffff"], "brightness": 101},
        {"colors": ["#ffffff"], "brightness": True},
        {"colors": ["#ffffff"], "brightness": 10, "kelvin": 50},
    ):
        assert _code(scenes.validate_look, bad) == "invalid_format"


def test_prune_and_attach_looks():
    look = {"colors": ["#ffaa00"], "brightness": 40, "kelvin": None, "stored_at": "2026-11-16T20:00:00+00:00"}
    looks = {"3:1": look, "3:9": look, "8:0": look}
    assert scenes.prune_looks(looks, [GROUP]) == {"3:1": look}

    summary = [{"id": 3, "friendly_name": "ZGSalon", "members": [], "scenes": GROUP["scenes"]}]
    out = scenes.attach_looks(summary, {"3:1": look})
    assert out[0]["scenes"][1] == {"id": 1, "name": "Kinoabend", "look": look}
    assert out[0]["scenes"][0]["look"] is None
    assert "look" not in summary[0]["scenes"][0]  # Eingabe bleibt unverändert
