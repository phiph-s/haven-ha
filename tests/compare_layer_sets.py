"""Werkzeug (kein pytest): Bildsätze aus dem GPU-Test der App zusammensetzen und mit echten Renders vergleichen.

    cd app/packages/haven_3d
    flutter test --enable-flutter-gpu --enable-impeller test_gpu/layer_renderer_test.dart
    python3 ../../../integration/tests/compare_layer_sets.py build/layer_sets
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import time

import numpy as np
from PIL import Image

_SPEC = importlib.util.spec_from_file_location(
    "render_compose", Path(__file__).resolve().parents[1] / "custom_components" / "haven" / "render_compose.py"
)
rc = importlib.util.module_from_spec(_SPEC)
sys.modules["render_compose"] = rc
_SPEC.loader.exec_module(rc)


def main(root: Path) -> int:
    worst = 0.0
    for folder in sorted(p for p in root.iterdir() if (p / "meta.json").is_file()):
        s = rc.LayerSet(folder)
        for ref in sorted(folder.glob("ref_*.json")):
            states = json.loads(ref.read_text())
            lights = {
                k: rc.LampLight(v["brightness"], tuple(int(v["color"][i : i + 2], 16) for i in (1, 3, 5)))
                if v["on"]
                else None
                for k, v in states.items()
            }
            t = time.perf_counter()
            out = s.compose(lights)
            ms = (time.perf_counter() - t) * 1000
            real = np.asarray(Image.open(ref.with_suffix(".png")).convert("RGBA"), dtype=np.int16)
            vis = real[..., 3] > 8
            err = np.abs(real[..., :3] - out[..., :3].astype(np.int16)).max(axis=-1)[vis]
            mean = float(err.mean())
            worst = max(worst, mean)
            print(
                f"{folder.name:16} {ref.stem[4:]:7} Ø {mean:5.2f}  p99 {int(np.percentile(err, 99)):3d}  "
                f">6: {100 * float((err > 6).mean()):5.2f} %  ΔAlpha {int(np.abs(real[..., 3] - out[..., 3]).max()):3d}"
                f"  ({ms:.0f} ms)"
            )
            Image.fromarray(out, "RGBA").save(folder / f"out_{ref.stem[4:]}.png")
    return 0 if worst < 3 else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
