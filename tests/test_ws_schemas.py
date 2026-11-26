"""WebSocket-Befehle dürfen kein eigenes Feld `id` haben.

`id` ist bei HA die Nummer der Nachricht. Ein Befehlsfeld gleichen Namens
wird vom Client überschrieben und schlägt mit „expected str at 'id'“ fehl.
"""

from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "haven"


def test_no_command_field_named_id():
    bad = [
        f"{path.name}:{n}"
        for path in sorted(SRC.glob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"vol\.(Required|Optional)\(\s*[\"']id[\"']", line)
    ]
    assert bad == []
