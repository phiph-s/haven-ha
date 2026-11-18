"""Letzter bekannter Gerätezustand aus ``<base>/<friendly_name>`` (ohne Home Assistant)."""

from __future__ import annotations

import json
from typing import Any


class StateCache:
    """Hält Zustände je IEEE und ordnet Topics den Geräten zu.

    ``<base>/+`` deckt nur Namen ohne ``/`` ab. Für Namen mit ``/`` liefert
    :meth:`slash_topics` die exakten Topics, die zusätzlich abonniert werden.
    """

    def __init__(self, base_topic: str) -> None:
        self.base = base_topic.strip("/")
        self.states: dict[str, dict[str, Any]] = {}
        self._names: dict[str, str] = {}  # friendly_name -> ieee

    def set_names(self, names: dict[str, str]) -> None:
        self._names = dict(names)
        known = set(names.values())
        for ieee in [i for i in self.states if i not in known]:
            del self.states[ieee]

    @property
    def wildcard_topic(self) -> str:
        return f"{self.base}/+"

    def slash_topics(self) -> dict[str, str]:
        """Friendly-Name -> exaktes Topic, für Namen mit ``/``."""
        return {n: f"{self.base}/{n}" for n in sorted(self._names) if "/" in n}

    def ingest(self, topic: str, payload: str | bytes) -> tuple[str, dict[str, Any]] | None:
        """Verarbeitet eine Meldung. ``(ieee, values)`` oder ``None`` (fremd/ungültig)."""
        prefix = self.base + "/"
        if not topic.startswith(prefix):
            return None
        name = topic[len(prefix) :]
        if name == "bridge" or name.startswith("bridge/"):
            return None
        ieee = self._names.get(name)
        if ieee is None:  # Gruppe, unbekanntes Gerät, /set, /availability ...
            return None
        try:
            values = json.loads(payload)
        except (TypeError, ValueError):
            return None
        if not isinstance(values, dict):
            return None
        self.states.setdefault(ieee, {}).update(values)
        return ieee, values

    # --- Verfügbarkeit: ``<base>/<friendly_name>/availability`` -------------------------

    @property
    def availability_wildcard(self) -> str:
        return f"{self.base}/+/availability"

    def slash_availability_topics(self) -> dict[str, str]:
        """Friendly-Name -> exaktes Verfügbarkeits-Topic, für Namen mit ``/``."""
        return {n: f"{self.base}/{n}/availability" for n in sorted(self._names) if "/" in n}

    def availability_name(self, topic: str) -> str | None:
        """Friendly-Name aus einem Verfügbarkeits-Topic (auch noch unbekannte), ``None`` bei Fremdem."""
        prefix, suffix = self.base + "/", "/availability"
        if not (topic.startswith(prefix) and topic.endswith(suffix)):
            return None
        name = topic[len(prefix) : -len(suffix)]
        return None if not name or name == "bridge" or name.startswith("bridge/") else name

    def availability_ieee(self, topic: str) -> str | None:
        """IEEE zu einem Verfügbarkeits-Topic, ``None`` bei Gruppen, Bridge oder Fremdem."""
        name = self.availability_name(topic)
        return self._names.get(name) if name is not None else None

    def ieee_for(self, name: str) -> str | None:
        return self._names.get(name)
