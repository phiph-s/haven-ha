"""Ablage der Bildsätze für Widgets und Displays (ohne Home Assistant).

Die App lädt nach einer Kartenänderung alle Bildsätze in Stücken hoch (siehe
``render_ws.py``)::

    begin(key)                     → Upload-ID, leerer Ordner ``upload/``
    chunk(upload, path, offset, data)  path = "<satz>/<datei>", angehängt an der Stelle offset
    commit(upload)                 → prüft jeden Satz, tauscht ``sets/`` atomar aus

``key`` ist der Render-Schlüssel der App (Hash aus Plan, Perspektiven und Format): an
ihm sieht die App über ``status()``, ob HA schon die Bilder zum aktuellen Plan hat.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import re
import secrets
import shutil
from typing import Any

# Namen von Sätzen (Ordner) und Dateien darin; nie mit Punkt am Anfang („..“)
NAME_RE = re.compile(r"^[A-Za-z0-9_\-][A-Za-z0-9_.\-]{0,95}$")
FILE_RE = re.compile(r"^(meta\.json|base\.png|(room|lamp)_[A-Za-z0-9_.\-]{1,96}\.png)$")

MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_SETS = 64
MAX_KEY = 128


class RenderStoreError(ValueError):
    """Ungültiger Upload (Name, Größe, Reihenfolge, Inhalt)."""


class RenderStore:
    """Ordner ``root`` mit ``sets/<satz>/…``, ``upload/<satz>/…`` und ``index.json``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sets_dir = root / "sets"
        self.upload_dir = root / "upload"
        self.index_file = root / "index.json"
        self._upload: str | None = None
        self._key: str | None = None
        self._total = 0

    # --- Lesen ------------------------------------------------------------------------

    def index(self) -> dict[str, Any]:
        try:
            data = json.loads(self.index_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def set_names(self) -> list[str]:
        if not self.sets_dir.is_dir():
            return []
        return sorted(p.name for p in self.sets_dir.iterdir() if p.is_dir() and NAME_RE.match(p.name))

    def status(self) -> dict[str, Any]:
        """Stand für die App: Schlüssel, Zeitpunkt und Sätze (Ansicht, Design, Name)."""
        idx = self.index()
        return {
            "key": idx.get("key"),
            "updated_at": idx.get("updated_at"),
            "sets": idx.get("sets", {}),
        }

    def read(self, name: str, file: str, offset: int = 0, length: int = 512 * 1024) -> tuple[bytes, int]:
        """Stück ``length`` von Datei ``file`` im Satz ``name`` ab ``offset``; mit Gesamtgröße."""
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise RenderStoreError(f"Ungültiger Satz: {name!r}")
        if not isinstance(file, str) or not FILE_RE.match(file):
            raise RenderStoreError(f"Ungültige Datei: {file!r}")
        if not isinstance(offset, int) or offset < 0 or not isinstance(length, int) or length <= 0:
            raise RenderStoreError("Ungültiger Ausschnitt")
        path = self.sets_dir / name / file
        if not path.is_file():
            raise RenderStoreError(f"{name}/{file} gibt es nicht")
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(min(offset, size))
            return f.read(length), size

    # --- Upload -----------------------------------------------------------------------

    def begin(self, key: str) -> str:
        """Neuer Upload; ein laufender wird verworfen."""
        if not isinstance(key, str) or not 0 < len(key) <= MAX_KEY:
            raise RenderStoreError("Ungültiger Schlüssel")
        shutil.rmtree(self.upload_dir, ignore_errors=True)
        self.upload_dir.mkdir(parents=True)
        self._upload = secrets.token_hex(8)
        self._key = key
        self._total = 0
        return self._upload

    def _check_upload(self, upload: str) -> None:
        if self._upload is None or upload != self._upload:
            raise RenderStoreError("Kein laufender Upload (neu beginnen)")

    def chunk(self, upload: str, path: str, offset: int, data: bytes) -> int:
        """Stück ``data`` an ``path`` ab ``offset`` schreiben; gibt die neue Dateigröße zurück."""
        self._check_upload(upload)
        parts = path.split("/") if isinstance(path, str) else []
        if len(parts) != 2 or not NAME_RE.match(parts[0]) or not FILE_RE.match(parts[1]):
            raise RenderStoreError(f"Ungültiger Pfad: {path!r}")
        folder = self.upload_dir / parts[0]
        if not folder.is_dir() and sum(1 for _ in self.upload_dir.iterdir()) >= MAX_SETS:
            raise RenderStoreError("Zu viele Bildsätze")
        folder.mkdir(exist_ok=True)
        target = folder / parts[1]
        size = target.stat().st_size if target.exists() else 0
        if offset != size:
            raise RenderStoreError(f"{path}: Stück an Stelle {offset} erwartet {size}")
        if size + len(data) > MAX_FILE:
            raise RenderStoreError(f"{path} ist zu groß")
        if self._total + len(data) > MAX_TOTAL:
            raise RenderStoreError("Upload ist zu groß")
        with target.open("ab") as f:
            f.write(data)
        self._total += len(data)
        return size + len(data)

    def commit(self, upload: str) -> dict[str, dict[str, Any]]:
        """Alle Sätze prüfen und den Bestand ersetzen. Gibt die Sätze (Name → Angaben) zurück."""
        from .render_compose import LayerSet, LayerSetError  # noqa: PLC0415 - numpy erst hier

        self._check_upload(upload)
        names = sorted(p.name for p in self.upload_dir.iterdir() if p.is_dir())
        if not names:
            raise RenderStoreError("Keine Bildsätze hochgeladen")
        sets: dict[str, dict[str, Any]] = {}
        for name in names:
            try:
                s = LayerSet(self.upload_dir / name)
            except LayerSetError as err:
                raise RenderStoreError(f"{name}: {err}") from err
            view = s.view
            sets[name] = {
                "view": str(view.get("id", name)),
                "name": str(view.get("name") or view.get("id") or name),
                "room": view.get("room") if isinstance(view.get("room"), str) else None,
                "theme": str(s.meta.get("theme", "")),
                "width": s.width,
                "height": s.height,
                "lamps": sorted(s.lamp_ids),
            }
        old = self.root / "sets.old"
        shutil.rmtree(old, ignore_errors=True)
        if self.sets_dir.exists():
            self.sets_dir.rename(old)
        self.upload_dir.rename(self.sets_dir)
        shutil.rmtree(old, ignore_errors=True)
        index = {
            "key": self._key,
            "updated_at": datetime.now(UTC).isoformat(),
            "sets": sets,
        }
        tmp = self.index_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(index), encoding="utf-8")
        tmp.replace(self.index_file)
        self._upload = None
        self._key = None
        return sets

    def abort(self) -> None:
        shutil.rmtree(self.upload_dir, ignore_errors=True)
        self._upload = None
        self._key = None

    def clear(self) -> None:
        """Alle Bilder löschen (z. B. Grundriss entfernt)."""
        self.abort()
        shutil.rmtree(self.sets_dir, ignore_errors=True)
        self.index_file.unlink(missing_ok=True)
