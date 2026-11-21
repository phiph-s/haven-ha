"""Revisionen der App-Dokumente: zwei Geräte, ein veralteter Stand wird abgelehnt."""

from __future__ import annotations

import asyncio
import copy
import importlib

import pytest

appdata = importlib.import_module("haven_nohass.appdata")
commands = importlib.import_module("haven_nohass.commands")

PLAN = {"version": 2, "rooms": [{"id": "kueche", "name": "Küche"}], "placements": [], "furniture": []}


class FakeStore:
    """Nachbau von ``homeassistant.helpers.storage.Store`` (nur Laden und Speichern)."""

    def __init__(self, data=None) -> None:
        self.data = copy.deepcopy(data)
        self.saves = 0

    async def async_load(self):
        await asyncio.sleep(0)  # wie die Datei: gibt die Schleife frei
        return copy.deepcopy(self.data)

    async def async_save(self, data) -> None:
        await asyncio.sleep(0)
        self.data = copy.deepcopy(data)
        self.saves += 1


def _doc(store: FakeStore) -> appdata.VersionedDoc:
    return appdata.VersionedDoc(store.async_load, store.async_save, "Der Grundriss")


def _set_plan(doc, plan, updated_at, base_revision=None):
    """Wie ``HavenStorage.async_set_plan``."""
    return doc.async_update(lambda prev: appdata.build_plan_doc(plan, updated_at, previous=prev), base_revision)


def test_revision_reading_and_validation():
    assert appdata.read_revision(None) == 0
    assert appdata.read_revision({"plan": PLAN}) == 0
    assert appdata.read_revision({"revision": 7}) == 7
    assert appdata.read_revision({"revision": True}) == 0
    assert appdata.read_revision({"revision": -1}) == 0
    assert appdata.validate_base_revision(None) == []
    assert appdata.validate_base_revision(0) == [] and appdata.validate_base_revision(3) == []
    assert appdata.validate_base_revision(-1) != [] and appdata.validate_base_revision(True) != []
    assert appdata.validate_base_revision("3") != []
    appdata.check_base_revision(3, None)
    appdata.check_base_revision(3, 3)
    with pytest.raises(commands.Z2MError) as err:
        appdata.check_base_revision(4, 3, "Der Grundriss")
    assert err.value.code == "conflict" and "anderen Gerät" in err.value.message


def test_event_data_with_revision():
    assert appdata.build_event_data("floorplan", 5, 3) == {"kind": "floorplan", "updated_at": 5, "revision": 3}
    assert appdata.build_event_data("prefs", None, 1) == {"kind": "prefs", "revision": 1}


def test_two_devices_stale_write_is_rejected():
    """Telefon und Desktop laden denselben Stand. Der Desktop speichert, danach will das
    Telefon seine ältere Fassung speichern: abgelehnt, der Stand des Desktops bleibt."""

    async def run():
        store = FakeStore({"plan": PLAN, "updated_at": 100})  # Speicherstand vor den Revisionen
        doc = _doc(store)
        start = appdata.read_plan_doc(await doc.async_load())
        assert start["revision"] == 0
        desktop_base = phone_base = start["revision"]

        desktop_plan = {**PLAN, "rooms": [*PLAN["rooms"], {"id": "bad", "name": "Bad"}]}
        saved = await _set_plan(doc, desktop_plan, 200, desktop_base)
        assert saved["revision"] == 1

        # Das Telefon (Uhr geht vor: 999) kennt noch Revision 0
        phone_plan = {**PLAN, "rooms": [{"id": "kueche", "name": "Küche alt"}]}
        with pytest.raises(commands.Z2MError) as err:
            await _set_plan(doc, phone_plan, 999, phone_base)
        assert err.value.code == "conflict"
        stored = appdata.read_plan_doc(store.data)
        assert stored["plan"] == desktop_plan and stored["revision"] == 1 and stored["updated_at"] == 200

        # Nach dem Neuladen (Revision 1) geht es durch
        again = await _set_plan(doc, {**desktop_plan, "placements": []}, 300, stored["revision"])
        assert again["revision"] == 2 and store.saves == 2

    asyncio.run(run())


def test_parallel_writes_with_same_base_only_one_wins():
    """Zwei ``set`` gleichzeitig auf derselben Revision: genau eines gewinnt."""

    async def run():
        store = FakeStore()
        doc = _doc(store)
        a = {**PLAN, "rooms": [{"id": "a", "name": "A"}]}
        b = {**PLAN, "rooms": [{"id": "b", "name": "B"}]}
        results = await asyncio.gather(
            _set_plan(doc, a, 1, 0), _set_plan(doc, b, 2, 0), return_exceptions=True
        )
        ok = [r for r in results if isinstance(r, dict)]
        failed = [r for r in results if isinstance(r, commands.Z2MError)]
        assert len(ok) == 1 and len(failed) == 1 and failed[0].code == "conflict"
        assert store.data["plan"] == ok[0]["plan"] and store.data["revision"] == 1

    asyncio.run(run())


def test_old_client_without_base_revision_still_saves_and_bumps_revision():
    """Ältere Apps kennen ``base_revision`` nicht: sie speichern wie bisher, die Revision
    steigt, damit neuere Geräte die Änderung bemerken (und selbst abgelehnt werden)."""

    async def run():
        store = FakeStore()
        doc = _doc(store)
        new_app = await _set_plan(doc, PLAN, 1, 0)
        assert new_app["revision"] == 1
        old_app = await _set_plan(doc, {**PLAN, "placements": []}, 2)  # ohne base_revision
        assert old_app["revision"] == 2
        with pytest.raises(commands.Z2MError):
            await _set_plan(doc, PLAN, 3, new_app["revision"])

    asyncio.run(run())


def test_widgets_and_prefs_carry_revision():
    async def run():
        widgets = FakeStore({"widgets": [], "layout": {"room_order": ["a"]}})
        wdoc = appdata.VersionedDoc(widgets.async_load, widgets.async_save, "Die Übersicht")
        saved = await wdoc.async_update(
            lambda prev: appdata.build_widgets_doc([{"id": "w", "type": "power"}], None, 5, previous=prev), 0
        )
        # Alter Client ohne layout: Layout bleibt, Revision steigt
        assert appdata.read_widgets_doc(saved) == {
            "widgets": [{"id": "w", "type": "power"}],
            "layout": {"room_order": ["a"]},
            "updated_at": 5,
            "revision": 1,
        }
        prefs = FakeStore({"prefs": {"excluded_lights": ["light.a"]}})
        pdoc = appdata.VersionedDoc(prefs.async_load, prefs.async_save, "Die Einstellungen")
        assert appdata.read_prefs_doc(await pdoc.async_load()) == {
            "prefs": {"excluded_lights": ["light.a"], "locked": []},
            "revision": 0,
        }
        await pdoc.async_update(lambda _p: {"prefs": {"excluded_lights": []}}, 0)
        with pytest.raises(commands.Z2MError):
            await pdoc.async_update(lambda _p: {"prefs": {"excluded_lights": ["light.b"]}}, 0)
        assert appdata.read_prefs_doc(prefs.data)["revision"] == 1

    asyncio.run(run())
