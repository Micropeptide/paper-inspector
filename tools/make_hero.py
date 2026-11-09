"""Hero images for the web app: one synthetic micrograph and its re-contrasted copy, already aligned."""
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from paperinspector import synth  # noqa: E402

rng = np.random.default_rng(7)
a = synth.fluorescence(rng, 420, 420)
b = synth.adjust(a, 1.25, -14)
out = Path(__file__).resolve().parents[1] / "web" / "public"
for name, img in (("hero-a.png", a), ("hero-b.png", b)):
    cv2.imwrite(str(out / name), img)
print("wrote", out)
