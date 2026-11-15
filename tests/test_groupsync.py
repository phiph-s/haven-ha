"""Gruppensync-Planung mit den echten Gruppen aus dem Inventar."""

from __future__ import annotations

import importlib
import json

groupsync = importlib.import_module("haven_nohass.groupsync")
from test_z2m import _bridge  # noqa: E402


def _setup(inventory):
    bridge = _bridge(inventory)
    lights = {i for i in bridge.devices if bridge.device_type(i) == "light"}
    return bridge, lights, bridge.ieee_for


def _plan(bridge, lights, ieee, old, new):
    return groupsync.plan_group_sync(ieee, True, bridge.groups, lights, old, new)


def test_move_light_between_rooms(inventory):
    bridge, lights, ref = _setup(inventory)
    lampe_waschraum = ref("Lampe Waschraum")
    kochbereich = {ref("Kochbereich Arbeitslicht 2"), ref("Kochbereich Arbeitslicht 1")}
    plan = _plan(bridge, lights, lampe_waschraum, {lampe_waschraum}, kochbereich)
    # Lampe Waschraum liegt nur in ZGWaschraum; der Bereich "Bad" hat außer ihr kein Licht,
    # also ist die alte Gruppe über das Licht selbst nicht bestimmbar: nur Aufnahme.
    assert plan["new_group"] == "ZGKochbereich"
    assert [a["action"] for a in plan["actions"]] == ["add"]
    add = plan["actions"][0]
    assert add["topic"] == "group/members/add"
    assert add["payload"] == {"group": "ZGKochbereich", "device": lampe_waschraum, "endpoint": 11}


def test_remove_and_add(inventory):
    bridge, lights, ref = _setup(inventory)
    bett = ref("Lampe Ruhebett")  # nur in ZGSalon
    wohn = {ref("Tischlampe Salon"), ref("Lampe Salon"), bett}
    kochbereich = {ref("Kochbereich Arbeitslicht 2"), ref("Kochbereich Arbeitslicht 1")}
    plan = _plan(bridge, lights, bett, wohn, kochbereich)
    assert [(a["action"], a["group"]) for a in plan["actions"]] == [
        ("remove", "ZGSalon"),
        ("add", "ZGKochbereich"),
    ]
    assert plan["old_group"] == "ZGSalon" and plan["skipped"] is None
    assert plan["actions"][0]["payload"]["device"] == bett


def test_same_group_and_non_light_and_empty_areas(inventory):
    bridge, lights, ref = _setup(inventory)
    spuele, kochen = ref("Kochbereich Arbeitslicht 2"), ref("Kochbereich Arbeitslicht 1")
    plan = _plan(bridge, lights, spuele, {spuele, kochen}, {kochen})
    assert plan["actions"] == [] and plan["skipped"] == "same_group"

    knob = ref("Drehknopf Kochbereich")
    assert groupsync.plan_group_sync(knob, False, bridge.groups, lights, None, {kochen})["skipped"] == "not_a_light"

    # Zielbereich ohne Lichter: nur raus aus der alten Gruppe
    plan = _plan(bridge, lights, spuele, {spuele, kochen}, set())
    assert [a["action"] for a in plan["actions"]] == ["remove"]
    assert plan["skipped"] == "no_group_for_area"

    # Bereich entfernt (None)
    plan = _plan(bridge, lights, spuele, {spuele, kochen}, None)
    assert [a["action"] for a in plan["actions"]] == ["remove"] and plan["skipped"] == "no_new_area"

    # kein alter Bereich: nur aufnehmen
    plan = _plan(bridge, lights, spuele, None, {ref("Lichtleiste Links")})
    assert [(a["action"], a["group"]) for a in plan["actions"]] == [("add", "ZGRuheraum")]


def test_already_member_not_added(inventory):
    bridge, lights, ref = _setup(inventory)
    spuele, kochen = ref("Kochbereich Arbeitslicht 2"), ref("Kochbereich Arbeitslicht 1")
    # Gerät ist schon in der Zielgruppe, alte Gruppe unbekannt: nichts zu tun
    plan = _plan(bridge, lights, spuele, None, {kochen})
    assert plan["actions"] == [] and plan["new_group"] == "ZGKochbereich"


def test_find_room_group_ignores_non_lights(inventory):
    bridge, lights, ref = _setup(inventory)
    # ein Knopf allein bestimmt keine Raumgruppe
    assert groupsync.find_room_group(bridge.groups, {ref("Drehknopf Kochbereich")}, lights) is None
    assert json.dumps(bridge.groups)  # Rohdaten bleiben serialisierbar
