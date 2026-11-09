"""Render the app icon (assets/icon.svg; assets/icon-small.svg for 32 px and below) at every size macOS wants."""
import sys
from pathlib import Path

import pymupdf

root = Path(__file__).resolve().parents[1]
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
big = (root / "assets" / "icon.svg").read_bytes()
small = (root / "assets" / "icon-small.svg").read_bytes()


def render(svg: bytes, px: int, dest: Path) -> None:
    doc = pymupdf.open(stream=svg, filetype="svg")
    page = doc[0]
    pix = page.get_pixmap(matrix=pymupdf.Matrix(px / page.rect.width, px / page.rect.height), alpha=True)
    pix.save(dest)
    doc.close()


for size in (16, 32, 64, 128, 256, 512):
    for scale, suffix in ((1, ""), (2, "@2x")):
        px = size * scale
        render(small if px <= 32 else big, px, out / f"icon_{size}x{size}{suffix}.png")
