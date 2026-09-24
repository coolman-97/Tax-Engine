#!/usr/bin/env python3
"""The demo: one household, four properties, and the moment the engine refuses.

    make demo

Runs entirely offline. No API key, no database, no network.
"""
from __future__ import annotations

import dataclasses
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "engine"))

from standbehind.dispositions.sale import SaleTerms
from standbehind.fixtures import (
    MARKET,
    build_household,
    build_strategies,
    facts_for_demo,
)
from standbehind.household import HouseholdYear
from standbehind.money import Money
from standbehind.provenance import Fact, explain
from standbehind.recommend import assess_exit
from standbehind.rules import load
from standbehind.rules.schema import FilingStatus as FS
from standbehind.simulate import simulate

M = Money.from_dollars
W = 84


def rule(title: str = "", char: str = "=") -> None:
    if title:
        print(f"\n{char * 3} {title} {char * max(0, W - len(title) - 5)}")
    else:
        print(char * W)


def main() -> int:
    portfolio = build_household()
    facts = facts_for_demo()
    rules = load(2026)

    rule("THE HOUSEHOLD")
    print(f"{portfolio.household_name} - married filing jointly, California")
    print(f"W-2 income {portfolio.wages}, portfolio income {portfolio.portfolio_income}")
    print(f"Suspended passive losses carried in: {portfolio.opening_ledger.total()}\n")
    print(f"  {'property':<13}{'value':>14}{'basis':>14}{'debt':>13}  what makes it hard")
    notes = {
        "pasadena": "interest-only note converts 2027 (+75% payment)",
        "austin": "cost seg + 100% bonus; 5/1 ARM resetting; 1245 recapture",
        "long_beach": "inherited (1014 step-up); 4 paid-off liens; HELOC",
        "silver_lake": "former home; section 121 clock; depreciation never excludable",
    }
    today = date(2026, 12, 31)
    for p in portfolio.properties:
        a = portfolio.assumption_for(p.id)
        print(f"  {p.id:<13}{a.market_value!s:>14}{p.original_basis!s:>14}"
              f"{p.liens.total_debt(today)!s:>13}  {notes[p.id]}")

    # ------------------------------------------------------------------
    rule("THE MOMENT THE ENGINE REFUSES")
    lb = next(p for p in portfolio.properties if p.id == "long_beach")
    household = HouseholdYear(
        year=2027, filing_status=FS.MARRIED_FILING_JOINTLY,
        wages=M("310000"), portfolio_income=M("22000"), state="CA")
    terms = SaleTerms(date(2027, 9, 15), M("1697400"))

    before = assess_exit(lb, facts, terms, rules, household,
                         ledger=portfolio.opening_ledger)
    print("\n  Advisor asks: should the client sell 1247 Ocean Blvd in 2027?\n")
    print(f"  >>> {before.narrative}\n")
    for i, q in enumerate(before.questions, 1):
        print(f"  {i}. {q.prompt}")
        print(f"     This moves the exit tax by {q.dollar_impact}.")
        print(f"     {q.why_it_matters}")
        print("     Defensible readings found in the documents:")
        for c in q.candidates:
            print(f"       {c.value}  from {c.source}")
            if c.implied_by:
                print(f"         {c.implied_by}")
    print("\n  Note what did NOT happen: the engine did not average the two "
          "\n  readings, and it did not return a number with an asterisk.")

    rule("THE ADVISOR ATTESTS", "-")
    facts["long_beach.land_allocation"] = Fact.attested(
        "long_beach.land_allocation", M("224000"),
        by="advisor:dana.reyes, CFP", at=datetime(2026, 9, 23, 16, 5),
        note="Client confirmed the 2019 date-of-death appraisal allocated 20% to "
             "land. The 2022 return was prepared on a 30% assumption in error.")
    resolved = dataclasses.replace(lb, land_allocation=M("224000"))
    after = assess_exit(resolved, facts, terms, rules, household,
                        ledger=portfolio.opening_ledger)
    print(f"\n  >>> {after.narrative}\n")
    print("  The attestation is now permanently part of the provenance chain:")
    print("  " + "\n  ".join(
        explain(facts["long_beach.land_allocation"]).splitlines()))

    # ------------------------------------------------------------------
    rule("HOLD vs SELL vs EXCHANGE")
    strategies = build_strategies()
    print(f"\n  30-year horizon, {MARKET.appreciation:.1%} appreciation, "
          f"{MARKET.discount_rate:.1%} discount rate\n")
    print(f"  {'strategy':<38}{'terminal NW':>15}{'total tax':>14}{'NPV':>15}")
    print("  " + "-" * (W - 4))
    results = []
    for s in strategies:
        r = simulate(portfolio, s, MARKET, start_year=2026, horizon=30)
        results.append((s, r))
        print(f"  {s.name:<38}{r.terminal_net_worth!s:>15}"
              f"{r.cumulative_tax!s:>14}{r.npv(MARKET.discount_rate)!s:>15}")

    best = max(results, key=lambda pair: pair[1].npv(MARKET.discount_rate).cents)
    print(f"\n  Best on NPV: {best[0].name}")
    print(f"  {best[0].description}")

    pair = {s.name: r for s, r in results}
    a, b = pair["Sell both in 2027"], pair["Sell Pasadena 2027, Long Beach 2028"]
    delta = a.cumulative_tax - b.cumulative_tax
    print("\n  Sequencing effect: the same two properties at the same prices cost")
    print(f"  {a.cumulative_tax} in tax sold together, {b.cumulative_tax} split across")
    print(f"  two tax years - a difference of {delta} for changing one closing date.")

    # ------------------------------------------------------------------
    rule("WHAT THE ENGINE SAW COMING")
    hold = pair["Hold"]
    for year, event in hold.all_events()[:9]:
        print(f"  {year}  {event[:W - 8]}")

    rule("DETERMINISM")
    again = simulate(portfolio, strategies[0], MARKET, start_year=2026, horizon=30)
    print(f"  ledger hash:  {hold.ledger_hash}")
    print(f"  replayed:     {again.ledger_hash}")
    print(f"  identical:    {hold.ledger_hash == again.ledger_hash}")
    print("\n  Same scenario, same bytes. 'Is this the number I showed the client")
    print("  in March?' is a question with an answer.")
    rule()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
