#!/usr/bin/env python3
"""Export the demo scenario as a JSON bundle for the web viewer.

The viewer never computes tax. It renders what the engine produced, including
the provenance chain behind each figure, so the number on screen and the
number in the ledger cannot drift apart.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "engine"))

from standbehind.dispositions import SaleTerms, compute_sale
from standbehind.fixtures import (
    MARKET,
    build_household,
    build_strategies,
    facts_for_demo,
)
from standbehind.household import HouseholdYear
from standbehind.money import Money
from standbehind.provenance import Derived, Fact
from standbehind.recommend import assess_exit
from standbehind.rules import load
from standbehind.rules.schema import FilingStatus as FS
from standbehind.simulate import simulate

M = Money.from_dollars
HORIZON = 30
START = 2026


def d(m: Money) -> float:
    """Dollars as a float, for the chart layer only.

    Every figure also ships as integer cents. The float is for pixel maths;
    anything displayed as a number comes from the cents.
    """
    return float(m.dollars)


def money(m: Money) -> dict:
    return {"cents": m.cents, "dollars": d(m), "text": str(m)}


def provenance_node(fact: Fact) -> dict:
    p = fact.provenance
    node = {
        "id": fact.id,
        "name": fact.name,
        "value": money(fact.value) if isinstance(fact.value, Money) else str(fact.value),
        "standing": fact.standing.name.lower(),
        "standingLabel": fact.standing.label,
        "kind": type(p).__name__.lower(),
        "describe": p.describe(),
        "children": [],
    }
    if isinstance(p, Derived):
        node["ruleId"] = p.rule_id
        node["explanation"] = p.explanation
        node["children"] = [provenance_node(c) for c in p.inputs]
    else:
        for field in ("document_id", "page", "cited_text", "confidence", "basis",
                      "by", "note"):
            value = getattr(p, field, None)
            if value is not None:
                node[field] = str(value) if isinstance(value, Decimal) else value
        if hasattr(p, "at"):
            node["at"] = p.at.isoformat(timespec="seconds")
    return node


def main() -> int:
    portfolio = build_household()
    facts = facts_for_demo()
    rules = load(START)
    today = date(2026, 12, 31)

    bundle: dict = {
        "generated": datetime(2026, 9, 23, 17, 0).isoformat(),
        "engineVersion": rules.version,
        "ruleSource": rules.source,
        "household": {
            "name": portfolio.household_name,
            "filingStatus": "Married filing jointly",
            "state": portfolio.state,
            "wages": money(portfolio.wages),
            "portfolioIncome": money(portfolio.portfolio_income),
            "suspendedPassiveLosses": money(portfolio.opening_ledger.total()),
        },
        "properties": [],
        "strategies": [],
        "assessment": {},
        "waterfall": {},
        "timeline": [],
    }

    hard = {
        "pasadena": "Interest-only note converts to amortising in 2027 - the payment jumps 75%.",
        "austin": "Cost segregation with 100% bonus. 40% of the exit gain is ordinary income, not 25% gain.",
        "long_beach": "Inherited at a stepped-up basis. Four paid-off liens still on title. HELOC in its draw period.",
        "silver_lake": "A former home. The section 121 clock is running out and depreciation is never excludable.",
    }

    for p in portfolio.properties:
        a = portfolio.assumption_for(p.id)
        basis = p.basis_on(START - 1)
        value = a.market_value
        bundle["properties"].append({
            "id": p.id,
            "address": p.address,
            "state": p.state,
            "acquired": p.acquired.isoformat(),
            "placedInService": p.placed_in_service.isoformat(),
            "acquisitionKind": p.acquisition_kind.value,
            "marketValue": money(value),
            "originalBasis": money(p.original_basis),
            "landAllocation": money(p.land_allocation),
            "landRatio": float(p.land_ratio),
            "adjustedBasis": money(basis.adjusted_basis),
            "accumulated1250": money(basis.accumulated_1250),
            "accumulated1245": money(basis.accumulated_1245),
            "additional1250": money(basis.additional_1250),
            "debt": money(p.liens.total_debt(today)),
            "equity": money(value - p.liens.total_debt(today)),
            "lendableEquity": money(p.lendable_equity_on(value, today)),
            "annualRents": money(a.annual_rents),
            "annualExpenses": money(a.annual_operating_expenses + a.annual_insurance),
            "depreciationThisYear": money(p.depreciation_in_year(START)),
            "depreciationExhausted": p.depreciation_exhausted_in(),
            "hasCostSeg": p.cost_segregation is not None,
            "whatMakesItHard": hard[p.id],
            "liens": [
                {
                    "position": lien.position,
                    "description": lien.description,
                    "balance": money(lien.balance_on(today)),
                    "released": lien.released,
                    "releaseCost": money(lien.release_cost),
                }
                for lien in p.liens.ordered()
            ],
            "schedules": [
                {
                    "label": label,
                    "basis": money(s.depreciable_basis),
                    "recapture": s.recovery_class.recapture.value,
                    "method": s.recovery_class.method.value,
                    "years": s.recovery_class.name,
                    "citation": s.recovery_class.citation,
                    "byYear": [{"year": y, "amount": d(amt)} for y, amt in s.by_year],
                }
                for label, s in p.schedules()
            ],
        })

    # ---- strategies ---------------------------------------------------
    for strategy in build_strategies():
        result = simulate(portfolio, strategy, MARKET, start_year=START, horizon=HORIZON)
        bundle["strategies"].append({
            "name": strategy.name,
            "description": strategy.description,
            "terminalNetWorth": money(result.terminal_net_worth),
            "cumulativeTax": money(result.cumulative_tax),
            "npv": money(result.npv(MARKET.discount_rate)),
            "ledgerHash": result.ledger_hash,
            "years": [
                {
                    "year": y.year,
                    "value": d(y.total_value),
                    "debt": d(y.total_debt),
                    "equity": d(y.equity),
                    "netWorth": d(y.net_worth),
                    "cashFlow": d(y.rental_cash_flow),
                    "afterTaxCashFlow": d(y.after_tax_cash_flow),
                    "tax": d(y.tax.total_tax),
                    "federalTax": d(y.tax.federal_tax),
                    "stateTax": d(y.tax.state_tax),
                    "niit": d(y.tax.niit),
                    "suspendedLosses": d(y.tax.ledger.total()),
                    "releasedLosses": d(y.tax.passive_loss_released),
                    "events": list(y.events),
                }
                for y in result.years
            ],
        })

    # ---- the refusal ---------------------------------------------------
    lb = next(p for p in portfolio.properties if p.id == "long_beach")
    household = HouseholdYear(
        year=2027, filing_status=FS.MARRIED_FILING_JOINTLY,
        wages=M("310000"), portfolio_income=M("22000"), state="CA")
    terms = SaleTerms(date(2027, 9, 15), M("1697400"))
    before = assess_exit(lb, facts, terms, rules, household,
                         ledger=portfolio.opening_ledger)

    attested = dict(facts)
    attested["long_beach.land_allocation"] = Fact.attested(
        "long_beach.land_allocation", M("224000"),
        by="advisor:dana.reyes, CFP", at=datetime(2026, 9, 23, 16, 5),
        note="Client confirmed the 2019 date-of-death appraisal allocated 20% to "
             "land. The 2022 return was prepared on a 30% assumption in error.")
    resolved = dataclasses.replace(lb, land_allocation=M("224000"))
    after = assess_exit(resolved, attested, terms, rules, household,
                        ledger=portfolio.opening_ledger)

    bundle["assessment"] = {
        "propertyId": before.property_id,
        "salePrice": money(terms.price),
        "saleDate": terms.on.isoformat(),
        "before": {
            "canRecommend": before.can_recommend,
            "standing": before.standing.name.lower(),
            "narrative": before.narrative,
            "uncertainty": money(before.uncertainty),
            "questions": [
                {
                    "field": q.field,
                    "prompt": q.prompt,
                    "whyItMatters": q.why_it_matters,
                    "dollarImpact": money(q.dollar_impact),
                    "standing": q.current_standing.name.lower(),
                    "candidates": [
                        {"value": money(c.value), "source": c.source,
                         "impliedBy": c.implied_by}
                        for c in q.candidates
                    ],
                }
                for q in before.questions
            ],
            "provenance": provenance_node(before.derivation),
        },
        "after": {
            "canRecommend": after.can_recommend,
            "standing": after.standing.name.lower(),
            "narrative": after.narrative,
            "exitTax": money(after.exit_tax) if after.exit_tax else None,
            "netAfterTax": money(after.net_after_tax) if after.net_after_tax else None,
            "provenance": provenance_node(after.derivation),
        },
    }

    # ---- exit tax waterfalls -------------------------------------------
    for p in portfolio.properties:
        a = portfolio.assumption_for(p.id)
        price = a.market_value.apply_rate(Decimal("1.035"))
        sale = compute_sale(p, SaleTerms(date(2027, 6, 15), price), rules)
        bundle["waterfall"][p.id] = {
            "price": money(sale.gross_price),
            "sellingCosts": money(sale.selling_costs),
            "amountRealized": money(sale.amount_realized),
            "adjustedBasis": money(sale.adjusted_basis),
            "totalGain": money(sale.total_gain),
            "section1245Ordinary": money(sale.character.section_1245_ordinary),
            "section1250Ordinary": money(sale.character.section_1250_ordinary),
            "unrecaptured1250": money(sale.character.unrecaptured_1250),
            "capitalGain": money(sale.character.adjusted_net_capital_gain),
            "debtPayoff": money(sale.debt_payoff),
            "lienClearingCosts": money(sale.lien_clearing_costs),
            "netCashBeforeTax": money(sale.net_cash_before_tax),
            "suspendedLossReleased": money(portfolio.opening_ledger.for_activity(p.id)),
        }

    hold = bundle["strategies"][0]
    bundle["timeline"] = [
        {"year": y["year"], "event": e}
        for y in hold["years"] for e in y["events"]
    ]

    out = ROOT / "packages" / "web" / "public" / "scenario.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, indent=1))
    size = out.stat().st_size
    print(f"wrote {out.relative_to(ROOT)}  ({size / 1024:.0f} KB)")
    print(f"  {len(bundle['properties'])} properties, "
          f"{len(bundle['strategies'])} strategies x {HORIZON} years, "
          f"{len(bundle['timeline'])} timeline events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
