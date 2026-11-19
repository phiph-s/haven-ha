"""Bilder der Wohnung aus Lichtebenen zusammensetzen (ohne Home Assistant).

Die App rendert je Perspektive und Design einen Satz Bilder (siehe ``LayerSet`` in
``haven_3d``): ein Grundbild mit allen Lampen aus, je Raum einen Ausschnitt „Raum an,
ohne Licht“ (Möbellinien blenden in hellen Räumen aus) und je Lampe einen Ausschnitt
mit nur dieser Lampe in gedämpftem Weiß. Licht ist additiv, deshalb entsteht jeder
Lichtzustand ohne GPU::

    start = Grundbild, darin die Raumausschnitte der hellen Räume
    hdr   = T⁻¹(start)/e + Σ farbe·helligkeit/gain · (T⁻¹(ebene) − T⁻¹(raum))/e
    bild  = T(hdr)

mit T = Tone-Mapping „PBR Neutral“ (wie flutter_scene) auf linearem Licht und
e = Belichtung der Ebenen. Bilder liegen als PNG (sRGB, RGBA ohne Vormultiplikation) vor.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

FORMAT = 1

# PBR Neutral (Khronos), wie in flutter_scene
_KS = 0.8 - 0.04
_D = 1.0 - _KS
_DESAT = 0.15


def srgb_to_linear(u8: np.ndarray) -> np.ndarray:
    c = u8.astype(np.float32) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(lin: np.ndarray) -> np.ndarray:
    lin = np.clip(lin, 0.0, 1.0)
    c = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - 0.055)
    return np.clip(np.round(c * 255.0), 0, 255).astype(np.uint8)


def tone(c: np.ndarray) -> np.ndarray:
    """PBR Neutral auf ``(..., 3)`` linearem Licht."""
    x = c.min(axis=-1, keepdims=True)
    off = np.where(x < 0.08, x - 6.25 * x * x, 0.04)
    c = c - off
    peak = c.max(axis=-1, keepdims=True)
    safe = np.maximum(peak, 1e-6)
    new_peak = 1.0 - _D * _D / (peak + _D - _KS)
    g = 1.0 - 1.0 / (_DESAT * (peak - new_peak) + 1.0)
    rolled = (c * new_peak / safe) * (1.0 - g) + new_peak * g
    return np.where(peak < _KS, c, rolled).astype(np.float32)


def untone(y: np.ndarray) -> np.ndarray:
    """Umkehrung von :func:`tone` (bis auf Sättigung bei 1)."""
    y = y.astype(np.float32)
    p = y.max(axis=-1, keepdims=True)
    pc = np.minimum(p, 0.995)
    peak = _D * _D / (1.0 - pc) - _D + _KS
    g = 1.0 - 1.0 / (_DESAT * (peak - pc) + 1.0)
    rolled = (y - g * pc) * peak / np.maximum((1.0 - g) * pc, 1e-6)
    col = np.where(p >= _KS, rolled, y)
    m = np.maximum(col.min(axis=-1, keepdims=True), 0.0)
    x = np.where(m >= 0.04, m + 0.04, np.sqrt(m / 6.25))
    off = np.where(x < 0.08, x - 6.25 * x * x, 0.04)
    return (col + off).astype(np.float32)


def _hdr(rgb_u8: np.ndarray, exposure: float) -> np.ndarray:
    return untone(srgb_to_linear(rgb_u8)) / exposure


# --- Lampen aus Home Assistant (wie ``lampStateOf`` in der App) -----------------------


def kelvin_to_rgb(kelvin: float) -> tuple[int, int, int]:
    """Wie ``kelvinToRgb`` der App."""
    k = min(max(float(kelvin), 1000.0), 40000.0) / 100.0
    if k <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(k) - 161.1195681661
    else:
        r = 329.698727446 * math.pow(k - 60, -0.1332047592)
        g = 288.1221695283 * math.pow(k - 60, -0.0755148492)
    if k >= 66:
        b = 255.0
    elif k <= 19:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(k - 10) - 305.0447927307

    def clamp(v: float) -> int:
        return int(round(min(max(v, 0.0), 255.0)))

    return clamp(r), clamp(g), clamp(b)


@dataclass(frozen=True)
class LampLight:
    """Licht einer Lampe: Helligkeit 0…1 und Farbe (sRGB)."""

    brightness: float
    rgb: tuple[int, int, int]


def lamp_light(state: str | None, attributes: dict[str, Any] | None) -> LampLight | None:
    """Licht einer ``light.*``-Entity oder ``None`` (aus, nicht erreichbar)."""
    if state != "on":
        return None
    a = attributes or {}
    b255 = a.get("brightness")
    pct = 100 if not isinstance(b255, (int, float)) else min(max(round(b255 / 255 * 100), 1), 100)
    rgb = a.get("rgb_color")
    if isinstance(rgb, (list, tuple)) and len(rgb) >= 3:
        color = (int(round(rgb[0])), int(round(rgb[1])), int(round(rgb[2])))
    else:
        k = a.get("color_temp_kelvin")
        color = kelvin_to_rgb(k if isinstance(k, (int, float)) else 2700)
    return LampLight(min(max(pct / 100, 0.05), 1.0), color)


# --- Ein Bildsatz ---------------------------------------------------------------------


class LayerSetError(ValueError):
    """Bildsatz unvollständig oder ungültig."""


def _load_rgba(data: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(data)) as img:
        return np.asarray(img.convert("RGBA"), dtype=np.uint8).copy()


def _box(entry: Any, width: int, height: int) -> tuple[int, int, int, int]:
    if not isinstance(entry, dict):
        raise LayerSetError("Ausschnitt ohne Angaben")
    try:
        x, y, w, h = (int(entry[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError) as err:
        raise LayerSetError("Ausschnitt ohne x, y, w, h") from err
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
        raise LayerSetError("Ausschnitt außerhalb des Bilds")
    return x, y, w, h


class LayerSet:
    """Bildsatz in einem Ordner: ``meta.json``, ``base.png``, ``room_<id>.png``, ``lamp_<id>.png``."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise LayerSetError(f"meta.json fehlt oder ist kaputt: {err}") from err
        if not isinstance(meta, dict) or meta.get("format") != FORMAT:
            raise LayerSetError("Unbekanntes Format")
        try:
            self.width = int(meta["width"])
            self.height = int(meta["height"])
            self.exposure = float(meta.get("exposure", 1.0))
            self.gain = float(meta.get("gain", 0.25))
        except (KeyError, TypeError, ValueError) as err:
            raise LayerSetError("Größe, Belichtung oder gain fehlen") from err
        if not (0 < self.width <= 4096 and 0 < self.height <= 4096):
            raise LayerSetError("Bild zu groß")
        if not (0 < self.exposure <= 4 and 0 < self.gain <= 1):
            raise LayerSetError("Belichtung oder gain ungültig")
        self.meta = meta
        self.view = meta.get("view") if isinstance(meta.get("view"), dict) else {}
        rooms = meta.get("rooms") or {}
        lamps = meta.get("lamps") or {}
        if not isinstance(rooms, dict) or not isinstance(lamps, dict):
            raise LayerSetError("rooms und lamps müssen Objekte sein")
        self.rooms = {str(k): _box(v, self.width, self.height) for k, v in rooms.items()}
        self.lamp_rooms: dict[str, str | None] = {}
        self.lamps: dict[str, tuple[int, int, int, int]] = {}
        for k, v in lamps.items():
            if not isinstance(v, dict):
                raise LayerSetError("Lampe ohne Angaben")
            room = v.get("room")
            self.lamp_rooms[str(k)] = str(room) if isinstance(room, str) else None
            if "x" in v:
                self.lamps[str(k)] = _box(v, self.width, self.height)
        for name in ["base.png", *(f"room_{k}.png" for k in self.rooms), *(f"lamp_{k}.png" for k in self.lamps)]:
            if not (folder / name).is_file():
                raise LayerSetError(f"{name} fehlt")

    @property
    def lamp_ids(self) -> set[str]:
        """Alle Lampen der Perspektive (auch ohne sichtbare Wirkung)."""
        return set(self.lamp_rooms)

    def _png(self, name: str) -> np.ndarray:
        return _load_rgba((self.folder / name).read_bytes())

    def compose(self, lights: dict[str, LampLight | None]) -> np.ndarray:
        """Bild ``(h, w, 4)`` uint8 für die Lichter ``lights`` (Entity-ID → Licht oder aus).

        Immer mit Belichtung 1 wie in der App, auch wenn die Ebenen dunkler aufgenommen sind.
        """
        base = self._png("base.png")
        if base.shape != (self.height, self.width, 4):
            raise LayerSetError("Grundbild hat die falsche Größe")
        active = {
            k: v
            for k, v in lights.items()
            if v is not None and v.brightness > 0 and k in self.lamp_rooms
        }
        if not active and self.exposure == 1.0:
            return base

        def room_patch(room: str) -> tuple[tuple[int, int, int, int], np.ndarray] | None:
            box = self.rooms.get(room)
            if box is None:
                return None
            img = self._png(f"room_{room}.png")
            if img.shape != (box[3], box[2], 4):
                raise LayerSetError(f"room_{room}.png hat die falsche Größe")
            return box, img

        def overlay(dst: np.ndarray, dx: int, dy: int, patch: tuple[tuple[int, int, int, int], np.ndarray]) -> None:
            """Raumausschnitt dort einsetzen, wo er sich vom Grundbild unterscheidet.

            ``dst`` ist ein Ausschnitt des Grundbilds ab ``(dx, dy)``.
            """
            (x, y, w, h), img = patch
            x0, y0 = max(x, dx), max(y, dy)
            x1, y1 = min(x + w, dx + dst.shape[1]), min(y + h, dy + dst.shape[0])
            if x0 >= x1 or y0 >= y1:
                return
            src = img[y0 - y : y1 - y, x0 - x : x1 - x]
            ref = base[y0:y1, x0:x1]
            mask = np.any(src != ref, axis=-1)
            view = dst[y0 - dy : y1 - dy, x0 - dx : x1 - dx]
            view[mask] = src[mask]

        patches = {}
        for room in {self.lamp_rooms[k] for k in active} - {None}:
            p = room_patch(room)
            if p is not None:
                patches[room] = p

        start = base.copy()
        for p in patches.values():
            overlay(start, 0, 0, p)
        hdr = _hdr(start[..., :3], self.exposure)

        for lamp, light in active.items():
            box = self.lamps.get(lamp)
            if box is None:
                continue
            x, y, w, h = box
            layer = self._png(f"lamp_{lamp}.png")
            if layer.shape != (h, w, 4):
                raise LayerSetError(f"lamp_{lamp}.png hat die falsche Größe")
            ref = base[y : y + h, x : x + w].copy()
            room = self.lamp_rooms.get(lamp)
            if room in patches:
                overlay(ref, x, y, patches[room])
            factor = srgb_to_linear(np.asarray(light.rgb, dtype=np.uint8)) * (light.brightness / self.gain)
            delta = _hdr(layer[..., :3], self.exposure) - _hdr(ref[..., :3], self.exposure)
            hdr[y : y + h, x : x + w] += delta * factor

        out = np.empty_like(start)
        out[..., :3] = linear_to_srgb(tone(np.maximum(hdr, 0.0)))
        out[..., 3] = start[..., 3]
        return out


def encode_png(rgba: np.ndarray) -> bytes:
    """``(h, w, 4)`` uint8 → PNG (schnell komprimiert)."""
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", compress_level=3)
    return buf.getvalue()
