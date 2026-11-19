"""Bilder der Wohnung: Zusammensetzen aus Lichtebenen (``render_compose``) und Ablage (``render_store``)."""

from __future__ import annotations

import importlib
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

rc = importlib.import_module("haven_nohass.render_compose")
rs = importlib.import_module("haven_nohass.render_store")


# --- Mathematik ----------------------------------------------------------------------


def test_tone_untone_roundtrip():
    rng = np.random.default_rng(1)
    x = (rng.random((5000, 3)) * 3).astype(np.float32)
    y = rc.tone(x)
    ok = y.max(axis=-1) < 0.99
    assert np.abs(rc.untone(y) - x)[ok].max() < 1e-3


def test_tone_keeps_low_values_and_rolls_off_highlights():
    out = rc.tone(np.array([[0.5, 0.5, 0.5], [4.0, 4.0, 4.0]], dtype=np.float32))
    assert out[0] == pytest.approx([0.46, 0.46, 0.46], abs=1e-4)
    assert 0.95 < out[1, 0] < 1.0


def test_srgb_roundtrip():
    u8 = np.arange(256, dtype=np.uint8)
    assert np.array_equal(rc.linear_to_srgb(rc.srgb_to_linear(u8)), u8)


# --- Lampen wie in der App -----------------------------------------------------------


def test_lamp_light_off_and_unavailable():
    assert rc.lamp_light("off", {"brightness": 255}) is None
    assert rc.lamp_light("unavailable", {}) is None
    assert rc.lamp_light(None, None) is None


def test_lamp_light_brightness_and_color():
    full = rc.lamp_light("on", {})
    assert full.brightness == 1.0
    assert full.rgb == rc.kelvin_to_rgb(2700)
    half = rc.lamp_light("on", {"brightness": 128, "rgb_color": [255, 0, 10]})
    assert half.brightness == pytest.approx(0.5)
    assert half.rgb == (255, 0, 10)
    # Sehr dunkel: wie die App mindestens 5 %
    assert rc.lamp_light("on", {"brightness": 1}).brightness == pytest.approx(0.05)
    warm = rc.lamp_light("on", {"color_temp_kelvin": 2000})
    cold = rc.lamp_light("on", {"color_temp_kelvin": 6500})
    assert warm.rgb[2] < cold.rgb[2]


def test_kelvin_to_rgb_matches_app_values():
    # Gleiche Formel wie kelvinToRgb der App (Tanner Helland)
    assert rc.kelvin_to_rgb(6600) == (255, 255, 255)
    r, g, b = rc.kelvin_to_rgb(2700)
    assert r == 255 and 160 < g < 175 and 80 < b < 95


# --- Synthetischer Bildsatz ----------------------------------------------------------


def _png(rgba: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgba.astype(np.uint8), "RGBA").save(buf, "PNG")
    return buf.getvalue()


def _rgba(hdr: np.ndarray, exposure: float, alpha: int = 255) -> np.ndarray:
    """HDR (linear) → gespeichertes Bild wie aus der App (Belichtung, PBR Neutral, sRGB)."""
    out = np.empty((*hdr.shape[:2], 4), dtype=np.uint8)
    out[..., :3] = rc.linear_to_srgb(rc.tone(hdr * exposure))
    out[..., 3] = alpha
    return out


W, H = 24, 16
EXPOSURE = 0.5
GAIN = 0.5


def _make_set(folder: Path) -> np.ndarray:
    """Grund 0,3; Lampe ``light.a`` (Raum ``r``) mit Licht L in einem Ausschnitt; Raumausschnitt: Linie."""
    folder.mkdir(parents=True)
    base_hdr = np.full((H, W, 3), 0.3, dtype=np.float32)
    base = _rgba(base_hdr, EXPOSURE)
    # Raum an: eine „Möbellinie“ verschwindet (Pixel heller)
    room = base.copy()
    room[4, 2:10, :3] = 200
    # Licht der Lampe (bei gain): Verlauf in x
    light = np.zeros((H, W, 3), dtype=np.float32)
    light[:, 8:20] = np.linspace(0.05, 0.6, 12, dtype=np.float32)[None, :, None]
    room_hdr = rc.untone(rc.srgb_to_linear(room[..., :3])) / EXPOSURE
    lamp = _rgba(room_hdr + light * GAIN, EXPOSURE)
    meta = {
        "format": 1,
        "view": {"id": "uebersicht", "name": "Übersicht"},
        "theme": "dark",
        "width": W,
        "height": H,
        "exposure": EXPOSURE,
        "gain": GAIN,
        "rooms": {"r": {"x": 2, "y": 4, "w": 8, "h": 1}},
        "lamps": {
            "light.a": {"x": 8, "y": 0, "w": 12, "h": H, "room": "r"},
            "light.dark": {"room": "r"},
        },
    }
    (folder / "meta.json").write_text(json.dumps(meta))
    (folder / "base.png").write_bytes(_png(base))
    (folder / "room_r.png").write_bytes(_png(room[4:5, 2:10]))
    (folder / "lamp_light.a.png").write_bytes(_png(lamp[:, 8:20]))
    return light


def _shown(rgba: np.ndarray) -> np.ndarray:
    """Gespeichertes Bild (Belichtung EXPOSURE) so, wie es mit Belichtung 1 aussieht."""
    return rc.linear_to_srgb(rc.tone(rc.untone(rc.srgb_to_linear(rgba[..., :3])) / EXPOSURE))


def test_compose_all_off_is_base_at_full_exposure(tmp_path):
    _make_set(tmp_path / "s")
    s = rc.LayerSet(tmp_path / "s")
    base = np.asarray(Image.open(tmp_path / "s" / "base.png").convert("RGBA"))
    out = s.compose({"light.a": None})
    assert np.abs(out[..., :3].astype(int) - _shown(base).astype(int)).max() <= 1
    assert out[..., :3].mean() > base[..., :3].mean() + 20  # heller als gespeichert
    assert np.array_equal(out[..., 3], base[..., 3])
    assert s.lamp_ids == {"light.a", "light.dark"}
    # Mit Belichtung 1 ist das Grundbild schon das Ergebnis
    meta = json.loads((tmp_path / "s" / "meta.json").read_text())
    meta["exposure"] = 1.0
    (tmp_path / "s" / "meta.json").write_text(json.dumps(meta))
    assert np.array_equal(rc.LayerSet(tmp_path / "s").compose({}), base)


def test_compose_scales_light_by_brightness_and_color(tmp_path):
    light = _make_set(tmp_path / "s")
    s = rc.LayerSet(tmp_path / "s")
    out = s.compose({"light.a": rc.LampLight(0.8, (255, 255, 255))})
    # Erwartung: Raum hell (Linie weg) plus 0,8 × Licht
    room = np.asarray(Image.open(tmp_path / "s" / "base.png").convert("RGBA")).copy()
    room[4, 2:10, :3] = 200
    want_hdr = rc.untone(rc.srgb_to_linear(room[..., :3])) / EXPOSURE + 0.8 * light
    want = rc.linear_to_srgb(rc.tone(want_hdr * 1.0))
    assert np.abs(out[..., :3].astype(int) - want.astype(int)).max() <= 2
    # Linie ist ersetzt, außerhalb des Lichts bleibt alles wie im Grundbild
    assert np.abs(out[4, 3, :3].astype(int) - _shown(room)[4, 3].astype(int)).max() <= 1
    assert np.abs(out[:, :2, :3].astype(int) - _shown(room)[:, :2].astype(int)).max() <= 1
    # Rotes Licht färbt nur Rot
    red = s.compose({"light.a": rc.LampLight(1.0, (255, 0, 0))})
    px = red[10, 18].astype(int)
    assert px[0] > px[1] + 20 and px[1] == px[2]


def test_lamp_without_effect_only_switches_room(tmp_path):
    _make_set(tmp_path / "s")
    s = rc.LayerSet(tmp_path / "s")
    out = s.compose({"light.dark": rc.LampLight(1.0, (255, 255, 255))})
    room = np.asarray(Image.open(tmp_path / "s" / "base.png").convert("RGBA")).copy()
    room[4, 2:10, :3] = 200
    assert np.abs(out[..., :3].astype(int) - _shown(room).astype(int)).max() <= 1


def test_layer_set_rejects_broken_sets(tmp_path):
    _make_set(tmp_path / "s")
    (tmp_path / "s" / "lamp_light.a.png").unlink()
    with pytest.raises(rc.LayerSetError):
        rc.LayerSet(tmp_path / "s")
    _make_set(tmp_path / "t")
    meta = json.loads((tmp_path / "t" / "meta.json").read_text())
    meta["rooms"]["r"]["x"] = W  # außerhalb
    (tmp_path / "t" / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(rc.LayerSetError):
        rc.LayerSet(tmp_path / "t")


# --- Ablage und Upload ---------------------------------------------------------------


def _upload_set(store, upload: str, name: str, src: Path, chunk: int = 50) -> None:
    for f in sorted(src.iterdir()):
        data = f.read_bytes()
        for off in range(0, len(data), chunk):
            store.chunk(upload, f"{name}/{f.name}", off, data[off : off + chunk])


def test_store_upload_commit_and_status(tmp_path):
    _make_set(tmp_path / "src")
    store = rs.RenderStore(tmp_path / "render")
    assert store.status() == {"key": None, "updated_at": None, "sets": {}}
    up = store.begin("schluessel-1")
    _upload_set(store, up, "uebersicht_dark", tmp_path / "src")
    sets = store.commit(up)
    assert list(sets) == ["uebersicht_dark"]
    assert sets["uebersicht_dark"]["name"] == "Übersicht"
    assert sets["uebersicht_dark"]["lamps"] == ["light.a", "light.dark"]
    st = store.status()
    assert st["key"] == "schluessel-1" and st["updated_at"]
    assert store.set_names() == ["uebersicht_dark"]
    # Neuer Upload ersetzt den Bestand vollständig
    up2 = store.begin("schluessel-2")
    _upload_set(store, up2, "andere_light", tmp_path / "src")
    store.commit(up2)
    assert store.set_names() == ["andere_light"]
    assert store.status()["key"] == "schluessel-2"
    assert not (tmp_path / "render" / "upload").exists()


def test_store_rejects_bad_uploads(tmp_path):
    _make_set(tmp_path / "src")
    store = rs.RenderStore(tmp_path / "render")
    with pytest.raises(rs.RenderStoreError):
        store.chunk("x", "a/base.png", 0, b"1")  # ohne begin
    up = store.begin("k")
    for bad in ("../base.png", "a/b/base.png", "a/evil.sh", "a/room_..png/x", "a b/base.png"):
        with pytest.raises(rs.RenderStoreError):
            store.chunk(up, bad, 0, b"1")
    store.chunk(up, "a/base.png", 0, b"12")
    with pytest.raises(rs.RenderStoreError):
        store.chunk(up, "a/base.png", 5, b"3")  # Lücke
    # Unvollständiger Satz: Commit lehnt ab, Bestand bleibt
    with pytest.raises(rs.RenderStoreError):
        store.commit(up)
    assert store.set_names() == []
    with pytest.raises(rs.RenderStoreError):
        store.begin("")


def test_store_new_begin_invalidates_old_upload(tmp_path):
    store = rs.RenderStore(tmp_path / "render")
    old = store.begin("a")
    store.begin("b")
    with pytest.raises(rs.RenderStoreError):
        store.chunk(old, "s/base.png", 0, b"1")
