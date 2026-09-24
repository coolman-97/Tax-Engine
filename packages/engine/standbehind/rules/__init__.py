"""Loading rule sets, and projecting them forward honestly.

Projecting tax rules over a 30-year horizon is where long-horizon models
quietly go wrong, and the error is always in the same direction.

Most parameters are indexed for inflation, so a naive projection inflates
everything and produces a household whose tax picture looks roughly stable
forever. But several of the parameters that matter most to real estate were
fixed by statute and **never indexed**:

- The NIIT thresholds ($250,000 joint) have been frozen since 2013.
- The section 469(i) $25,000 allowance and its $100,000 phaseout have been
  frozen since 1986.
- The section 121 exclusion ($500,000 joint) has been frozen since 1997.

Those thresholds therefore fall in real terms every single year. A household
comfortably under the NIIT threshold today crosses it on a fixed date, without
earning a dollar more in real terms - and an advisor should hear about that
date from the software, not from the client's CPA. Getting this right is a
one-line distinction in the rule schema (``inflation_indexed``) and a
materially different answer thirty years out.
"""

from __future__ import annotations

import functools
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

from .schema import (
    Bracket,
    BracketTable,
    FilingStatus,
    RuleSet,
    StateRule,
)

__all__ = ["load", "available_years", "available_states", "RuleSet", "FilingStatus"]

_HERE = Path(__file__).parent
_DEFAULT_CPI = Decimal("0.025")


def available_years() -> list[int]:
    return sorted(int(p.stem) for p in (_HERE / "federal").glob("*.yaml"))


def available_states() -> list[str]:
    return sorted(p.stem for p in (_HERE / "states").glob("*.yaml"))


def _load_states() -> dict[str, StateRule]:
    states: dict[str, StateRule] = {}
    for path in sorted((_HERE / "states").glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        rule = StateRule.model_validate(data)
        states[rule.code] = rule
    return states


@functools.cache
def _load_base(tax_year: int) -> RuleSet:
    path = _HERE / "federal" / f"{tax_year}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"no federal rule data for {tax_year}; have {available_years()}. "
            "Use load(year, project_from=...) to project a later year forward."
        )
    data = yaml.safe_load(path.read_text())
    data["states"] = _load_states()
    return RuleSet.model_validate(data)


def _index_table(table: BracketTable, factor: Decimal) -> BracketTable:
    """Inflate bracket thresholds, rounding to the nearest $50 as the IRS does."""
    indexed = []
    for bracket in table.brackets:
        if bracket.up_to is None:
            indexed.append(Bracket(up_to=None, rate=bracket.rate))
        else:
            scaled = Decimal(bracket.up_to) * factor / Decimal(50)
            rounded = int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP)) * 50
            indexed.append(Bracket(up_to=rounded, rate=bracket.rate))
    return BracketTable(brackets=tuple(indexed))


def load(
    tax_year: int,
    *,
    cpi: Decimal | None = None,
    project_from: int | None = None,
) -> RuleSet:
    """Load the rules for ``tax_year``.

    If there is no published data for that year, project forward from the
    latest year there is - indexing only the parameters that are actually
    indexed, and holding the statutory ones fixed.
    """
    years = available_years()
    if tax_year in years and project_from is None:
        return _load_base(tax_year)

    base_year = project_from or max(y for y in years if y <= tax_year) if any(
        y <= tax_year for y in years
    ) else min(years)
    base = _load_base(base_year)
    elapsed = tax_year - base_year
    if elapsed <= 0:
        return base

    inflation = cpi if cpi is not None else _DEFAULT_CPI
    factor = (Decimal(1) + inflation) ** elapsed

    # Indexed: brackets, standard deduction, QBI threshold.
    ordinary = {k: _index_table(v, factor) for k, v in base.ordinary.items()}
    capital = {k: _index_table(v, factor) for k, v in base.capital_gains.items()}
    standard = {
        k: int((Decimal(v) * factor / Decimal(50)).quantize(
            Decimal(1), rounding=ROUND_HALF_UP)) * 50
        for k, v in base.standard_deduction.items()
    }

    qbi = base.qbi
    if qbi is not None:
        qbi = qbi.model_copy(update={
            "threshold": {
                k: int((Decimal(v) * factor / Decimal(50)).quantize(
                    Decimal(1), rounding=ROUND_HALF_UP)) * 50
                for k, v in qbi.threshold.items()
            }
        })

    # NOT indexed, by statute: NIIT thresholds, 469(i) allowance and phaseout,
    # 121 exclusion. These are deliberately carried forward unchanged.
    return base.model_copy(update={
        "version": f"{base.version}+projected{elapsed}y@cpi{inflation}",
        "tax_year": tax_year,
        "source": (
            f"{base.source}; projected {elapsed}y at {inflation:.1%} CPI. "
            "Indexed: brackets, standard deduction, 199A threshold. "
            "Held fixed by statute: 1411 thresholds, 469(i) allowance and "
            "phaseout, 121 exclusion."
        ),
        "ordinary": ordinary,
        "capital_gains": capital,
        "standard_deduction": standard,
        "qbi": qbi,
    })
