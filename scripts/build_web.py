#!/usr/bin/env python3
"""Inline the scenario bundle into the single-file viewer.

The viewer ships as one HTML file with the data embedded, so a reviewer can
open it from disk with no server, no build step and no network. The engine
remains the only thing that computes tax; this just carries its output.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
web = ROOT / "packages" / "web"
data = json.loads((web / "public" / "scenario.json").read_text())
html = (web / "template.html").read_text()
compact = json.dumps(data, separators=(",", ":"))
assert "__SCENARIO__" in html, "template lost its data placeholder"
out = web / "index.html"
out.write_text(html.replace("__SCENARIO__", compact))
print(f"wrote {out.relative_to(ROOT)}  ({out.stat().st_size / 1024:.0f} KB)")
