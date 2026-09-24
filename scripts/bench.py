#!/usr/bin/env python3
"""Simulation throughput. Exact integer-cent arithmetic is not free; this says
how much it costs."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "engine"))
from standbehind.fixtures import MARKET, build_household, build_strategies
from standbehind.simulate import simulate

portfolio, strategies = build_household(), build_strategies()
for horizon in (10, 30):
    t = time.perf_counter()
    runs = 0
    for s in strategies:
        simulate(portfolio, s, MARKET, start_year=2026, horizon=horizon)
        runs += 1
    dt = time.perf_counter() - t
    print(f"  {runs} strategies x {horizon}y x 4 properties: "
          f"{dt * 1000:6.0f} ms  ({dt / runs * 1000:.0f} ms per scenario)")
