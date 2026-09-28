"""Inspect the sample survey form: where are the date labels, and what does the text layer hold?"""
import re

import fitz

with open("sample.pdf", "rb") as fh:
    data = fh.read()

doc = fitz.open(stream=data, filetype="pdf")
print(f"pages: {len(doc)}")
page = doc[0]
print(f"page 1 size: {page.rect.width:.1f} x {page.rect.height:.1f} pt")
print()

text = page.get_text("text")
print("=== text layer, first 1200 chars ===")
print(text[:1200])
print("=== end of text layer ===")
print()

print("=== search_for label hits (rect = x0,y0,x1,y1) ===")
for needle in (
    "End Date", "end date", "To Date", "Start Date", "From Date",
    "Date", "Survey End", "Survey Start", "To", "From",
):
    hits = page.search_for(needle)
    for r in hits:
        yfrac = r.y0 / page.rect.height
        print(f"  {needle!r:16} -> x0={r.x0:6.1f} y0={r.y0:6.1f} "
              f"x1={r.x1:6.1f} y1={r.y1:6.1f}   (y at {yfrac:.1%} of page height)")
