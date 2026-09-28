"""
Analyse survey_964_top_right.png: find where the printed labels and the
handwriting sit, by measuring dark-ink density row by row and column by column.
Used to sanity-check the fixed 30% / 55% / 45% crop bands.
"""
import io

import fitz
from PIL import Image

PNG = "survey_964_top_right.png"
with open(PNG, "rb") as fh:
    raw = fh.read()

# What is the native size of this crop, and what page fraction does it cover?
pix = fitz.Pixmap(PNG)
print(f"image: {pix.width} x {pix.height} px, {pix.n} channel(s)")
print()

img = Image.open(io.BytesIO(raw)).convert("L")
w, h = img.size
px = img.load()

print("=== dark-ink density per horizontal band (rows) ===")
band = max(1, h // 20)
for top in range(0, h, band):
    bottom = min(h, top + band)
    dark = 0
    total = 0
    for y in range(top, bottom):
        for x in range(0, w, 2):
            total += 1
            if px[x, y] < 128:
                dark += 1
    if total:
        pct = 100.0 * dark / total
        bar = "#" * int(pct / 2)
        print(f"  y {top:>4}-{bottom:<4} ({100.0*top/h:5.1f}%-{100.0*bottom/h:5.1f}%)"
              f"  ink {pct:5.1f}%  {bar}")

print()
print("=== dark-ink density per vertical band (columns) ===")
band = max(1, w // 10)
for left in range(0, w, band):
    right = min(w, left + band)
    dark = 0
    total = 0
    for y in range(0, h, 2):
        for x in range(left, right):
            total += 1
            if px[x, y] < 128:
                dark += 1
    if total:
        pct = 100.0 * dark / total
        print(f"  x {left:>4}-{right:<4} ({100.0*left/w:5.1f}%-{100.0*right/w:5.1f}%)"
              f"  ink {pct:5.1f}%  {'#' * int(pct / 2)}")
