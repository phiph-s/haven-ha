"""Drosselung von Zustandsmeldungen je Schlüssel (ohne Home Assistant)."""

from __future__ import annotations

from typing import Any


class Throttle:
    """Höchstens ``max_per_second`` Meldungen je Schlüssel, ohne Werte zu verlieren.

    Die erste Meldung geht sofort raus. Kommt innerhalb des Mindestabstands
    mehr, werden die Werte zusammengeführt (neuere gewinnen) und beim nächsten
    :meth:`flush` gemeinsam geliefert. Der Aufrufer plant das Flush anhand von
    :meth:`delay`. ``now`` ist eine monotone Zeit in Sekunden.
    """

    def __init__(self, max_per_second: float = 4) -> None:
        self.interval = 1.0 / max_per_second
        self._last: dict[str, float] = {}
        self._pending: dict[str, dict[str, Any]] = {}

    def push(self, key: str, values: dict[str, Any], now: float) -> dict[str, Any] | None:
        """Gibt die sofort zu sendenden Werte zurück oder ``None`` (zurückgestellt)."""
        last = self._last.get(key)
        if key not in self._pending and (last is None or now - last >= self.interval):
            self._last[key] = now
            return dict(values)
        self._pending.setdefault(key, {}).update(values)
        return None

    def delay(self, key: str, now: float) -> float | None:
        """Sekunden bis zum Flush, ``None`` wenn nichts wartet."""
        if key not in self._pending:
            return None
        return max(0.0, self._last[key] + self.interval - now)

    def flush(self, key: str, now: float) -> dict[str, Any] | None:
        """Liefert die zurückgestellten Werte, wenn der Mindestabstand erreicht ist."""
        if key not in self._pending or now - self._last[key] < self.interval - 1e-9:
            return None
        self._last[key] = now
        return self._pending.pop(key)

    def clear(self) -> None:
        self._last.clear()
        self._pending.clear()
