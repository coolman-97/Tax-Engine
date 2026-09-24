"""Whether the engine is willing to make a recommendation at all.

This is the module the project exists to write.

An advisor is about to tell a client what to do with a property that is a
third of their net worth. The engine can always produce *a* number. The
question is whether that number is one anybody has stood behind, and if not,
what the shortest path to a defensible one is.

So the output of an assessment is one of two things:

- a recommendation, with the derivation attached; or
- a refusal, with the specific questions that would unblock it, **ranked by
  how much money each one actually moves**.

That ranking is what makes the refusal useful rather than annoying. "We need
more information" is a shrug. "Three questions, and the first one moves the
exit tax by $47,000" is a task an advisor can do before Thursday's meeting.

The dollar figure is computed, not estimated: the engine re-runs the exit tax
under each candidate reading of the disputed field and reports the spread.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal

from .dispositions.sale import SaleTerms, compute_sale
from .household import (
    EMPTY_LEDGER,
    HouseholdYear,
    PassiveActivityLedger,
    compute_tax_year,
)
from .money import Money
from .property import Property
from .provenance import Fact, Standing, blocking_facts
from .rules.schema import RuleSet

__all__ = ["Candidate", "Question", "Assessment", "assess_exit"]


@dataclass(frozen=True)
class Candidate:
    """One defensible reading of a disputed field, and where it came from."""

    value: Money
    source: str
    implied_by: str = ""


@dataclass(frozen=True)
class Question:
    """Something to ask the owner, priced."""

    field: str
    prompt: str
    why_it_matters: str
    dollar_impact: Money
    candidates: tuple[Candidate, ...] = ()
    current_standing: Standing = Standing.ASSUMED

    def __str__(self) -> str:
        return f"{self.prompt} (moves the answer by {self.dollar_impact})"


@dataclass(frozen=True)
class Assessment:
    """The engine's answer, or its reason for declining to give one."""

    property_id: str
    can_recommend: bool
    exit_tax: Money | None
    net_after_tax: Money | None
    standing: Standing
    questions: tuple[Question, ...]
    uncertainty: Money
    derivation: Fact | None = None
    narrative: str = ""

    @property
    def refused(self) -> bool:
        return not self.can_recommend


def _exit_tax_for(
    property_: Property,
    land_allocation: Money,
    terms: SaleTerms,
    rules: RuleSet,
    household: HouseholdYear,
    ledger: PassiveActivityLedger,
) -> tuple[Money, Money]:
    """Exit tax and net-after-tax proceeds under one land allocation.

    Land allocation is not a cosmetic input. It sets the depreciable basis,
    which sets the whole depreciation schedule, which sets accumulated
    depreciation, which sets both adjusted basis *and* how much of the gain is
    unrecaptured section 1250 taxed at 25% rather than capital gain taxed at
    20%. One field, three separate effects, all pulling the same direction.
    """
    variant = replace(property_, land_allocation=land_allocation)
    sale = compute_sale(variant, terms, rules, filing_status=household.filing_status)
    year = compute_tax_year(
        replace(
            household,
            gains=household.gains + sale.character,
            fully_disposed_activities=tuple(
                set(household.fully_disposed_activities) | {property_.id}
            ),
        ),
        rules,
        ledger,
    )
    baseline = compute_tax_year(household, rules, ledger)
    attributable = year.total_tax - baseline.total_tax
    return attributable, sale.net_cash_before_tax - attributable


def assess_exit(
    property_: Property,
    facts: Mapping[str, Fact],
    terms: SaleTerms,
    rules: RuleSet,
    household: HouseholdYear,
    *,
    ledger: PassiveActivityLedger | None = None,
    minimum_standing: Standing = Standing.EXTRACTED_HIGH,
) -> Assessment:
    """Assess whether an exit recommendation can be defended, and price the gaps."""

    ledger = ledger if ledger is not None else EMPTY_LEDGER
    relevant = {
        name: fact for name, fact in facts.items()
        if name.startswith(f"{property_.id}.")
    }

    # Build the derivation so the UI can show its work either way.
    derivation = Fact.derived(
        f"{property_.id}.exit_tax",
        Money(0),
        rule_id=f"exit-tax@{rules.version}",
        inputs=tuple(relevant.values()),
        explanation=(
            "amount realised less adjusted basis, split into unrecaptured "
            "section 1250, section 1245 ordinary recapture and capital gain, "
            "then taxed in the context of the whole household return"
        ),
    )

    blocking = blocking_facts(derivation, minimum_standing)

    # ---- Price each blocking field by re-running the engine ---------------
    questions: list[Question] = []
    total_uncertainty = Money(0)

    for fact in blocking:
        candidates = _candidates_for(fact, relevant, property_)
        if len(candidates) < 2:
            impact = Money(0)
        else:
            outcomes = [
                _exit_tax_for(property_, c.value, terms, rules, household, ledger)[0]
                for c in candidates
            ]
            impact = max(outcomes) - min(outcomes)
        total_uncertainty = total_uncertainty + impact
        questions.append(Question(
            field=fact.name,
            prompt=_prompt_for(fact, property_),
            why_it_matters=_why_for(fact),
            dollar_impact=impact,
            candidates=tuple(candidates),
            current_standing=fact.standing,
        ))

    questions.sort(key=lambda q: (-q.dollar_impact.cents, q.field))

    if blocking:
        return Assessment(
            property_id=property_.id, can_recommend=False, exit_tax=None,
            net_after_tax=None, standing=derivation.standing,
            questions=tuple(questions), uncertainty=total_uncertainty,
            derivation=derivation,
            narrative=(
                f"Cannot recommend. {len(questions)} input"
                f"{'s' if len(questions) != 1 else ''} to the exit-tax calculation "
                f"{'are' if len(questions) != 1 else 'is'} not attested, and the "
                f"range between defensible readings is {total_uncertainty}. "
                f"Ask the owner, or attest an assumption."
            ),
        )

    tax, net = _exit_tax_for(
        property_, property_.land_allocation, terms, rules, household, ledger
    )
    return Assessment(
        property_id=property_.id, can_recommend=True, exit_tax=tax,
        net_after_tax=net, standing=derivation.standing, questions=(),
        uncertainty=Money(0), derivation=derivation,
        narrative=(
            f"Exit tax of {tax} on a sale at {terms.price}, leaving {net} after "
            f"tax and debt payoff. Every input traces to a document or an "
            f"attestation; the derivation is attached."
        ),
    )


def _candidates_for(
    fact: Fact, facts: Mapping[str, Fact], property_: Property
) -> list[Candidate]:
    """Back out the defensible readings of a disputed field.

    For a land allocation, each year's filed depreciation figure implies an
    allocation: annual depreciation times 27.5 is the depreciable basis, and
    the rest is land. Two tax years that disagree therefore imply two
    different allocations, and the spread between them is real money.
    """
    if not fact.name.endswith("land_allocation"):
        return []

    total = property_.purchase_price + property_.capitalized_closing_costs
    candidates: list[Candidate] = []
    for name, other in sorted(facts.items()):
        if "depreciation_" not in name:
            continue
        annual = other.value
        if not isinstance(annual, Money) or not annual.is_positive():
            continue
        # Back out the depreciable basis: annual straight-line deduction times
        # the 27.5-year recovery period. Done with an exact Decimal rate rather
        # than integer arithmetic, so the implied land figure lands on a round
        # number instead of $336,000.03 - which would make an advisor doubt the
        # whole screen.
        depreciable = annual.apply_rate(Decimal("27.5"))
        implied_land = (total - depreciable).clamp_at_zero()
        # Round to the dollar. The source figure is a whole-dollar entry on a
        # tax return, so quoting the back-solved allocation to the cent would
        # be false precision - and false precision is exactly what makes an
        # advisor stop trusting a screen.
        implied_land = Money(round(implied_land.cents / 100) * 100)
        source = getattr(other.provenance, "document_id", name)
        candidates.append(Candidate(
            value=implied_land,
            source=source,
            implied_by=(
                f"{annual}/yr of depreciation over 27.5 years implies "
                f"{depreciable} of depreciable basis, leaving {implied_land} as land "
                f"({implied_land.ratio_to(total) * 100:.0f}% of basis)"
            ),
        ))
    # The engine's own assumption is only worth showing if it is genuinely a
    # third reading - within a dollar of a document-implied figure it is the
    # same answer with a worse pedigree.
    if all(abs((fact.value - c.value).cents) > 100 for c in candidates):
        candidates.append(Candidate(
            value=fact.value, source="engine assumption",
            implied_by=getattr(fact.provenance, "basis", ""),
        ))
    return sorted(candidates, key=lambda c: c.value.cents)


def _prompt_for(fact: Fact, property_: Property) -> str:
    short = property_.address.split(",")[0]
    if fact.name.endswith("land_allocation"):
        return (
            f"What land/improvement split was used for {short}? "
            "The original appraisal or the first year's depreciation schedule "
            "would settle it."
        )
    field = fact.name.split(".")[-1].replace("_", " ")
    return f"Confirm the {field} for {short}."


def _why_for(fact: Fact) -> str:
    if fact.name.endswith("land_allocation"):
        return (
            "Land is not depreciable, so this one number sets the depreciable "
            "basis, the entire depreciation schedule, the adjusted basis at "
            "sale, and how much of the gain is unrecaptured section 1250 taxed "
            "at 25% instead of capital gain taxed at 20%."
        )
    return "This value feeds the adjusted basis and therefore the gain on sale."
