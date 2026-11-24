#!/usr/bin/env python3
import sys, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from pathlib import Path

# Load all functions from build.py without running main
src = Path("build.py").read_text()
exec(src.split("if __name__")[0], globals())

genres, relations = parse_ejemplo()
genres = assign_positions(genres, relations)
out    = generate_html(genres, relations)
Path("ejemplo_td.html").write_text(out, encoding="utf-8")
print(f"Written ejemplo_td.html ({len(out)//1024} KB, {len(genres)} nodes)")

# Overlap check
NW, NH = 210, 55; PAD, LBL = 22, 20
fam_bounds = {}
for g in genres:
    key = (g["cat"], g["fam"])
    if key not in fam_bounds:
        fam_bounds[key] = dict(cat=g["cat"], fam=g["fam"],
            minX=1e9, minY=1e9, maxX=-1e9, maxY=-1e9)
    b = fam_bounds[key]
    b["minX"] = min(b["minX"], g["x"]-PAD);   b["minY"] = min(b["minY"], g["y"]-LBL-PAD)
    b["maxX"] = max(b["maxX"], g["x"]+NW+PAD); b["maxY"] = max(b["maxY"], g["y"]+NH+PAD)

boxes = list(fam_bounds.values())
overlaps = [
    (a["cat"], b["cat"]) for i, a in enumerate(boxes) for b in boxes[i+1:]
    if a["minX"]<b["maxX"] and b["minX"]<a["maxX"]
    and a["minY"]<b["maxY"] and b["minY"]<a["maxY"]
]
print(f"Box overlaps: {len(overlaps)}")
for a, b in overlaps[:5]:
    print(f"  {a!r}  /  {b!r}")

by_num = {g["num"]: g for g in genres}
viol = sum(1 for g in genres for to in g.get("rel_out", [])
           if (ch := by_num.get(to)) and g["y"] >= ch["y"])
print(f"Parent-above-child violations: {viol}")
