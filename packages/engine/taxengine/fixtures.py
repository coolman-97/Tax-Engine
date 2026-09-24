"""The demo household.

Four properties, chosen so that between them they exercise every instrument
reliably breaks an engine - interest-only loans, ARMs, HELOCs, and a property
carrying four paid-off liens - plus several less obvious ones: a stepped-up basis from an inheritance, a cost segregation study
with bonus depreciation, a former principal residence with a section 121 clock
running out, and a California property whose exchange would trigger the FTB
3840 clawback.

Every figure carries provenance. Some of it is attested, some was read off a
document, and one field - the land allocation on the Long Beach fourplex - is
deliberately left assumed, because that is the state real portfolios actually
arrive in and it is what makes the engine refuse.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from .household import PassiveActivityLedger
from .loans import (
    FixedRate,
    Lien,
    LienStack,
    adjustable_rate_mortgage,
    fixed_rate_mortgage,
    heloc,
    interest_only_then_amortizing,
)
from .money import Money, rate
from .property import (
    AcquisitionKind,
    CapitalImprovement,
    CostSegregation,
    PrimaryResidenceHistory,
    Property,
    PropertyKind,
)
from .provenance import Fact
from .rules.schema import FilingStatus
from .simulate import (
    Action,
    ActionKind,
    MarketAssumptions,
    Portfolio,
    PropertyAssumptions,
    Strategy,
)

__all__ = ["build_household", "build_strategies", "MARKET", "facts_for_demo"]

M = Money.from_dollars
ADVISOR = "advisor:dana.reyes, CFP"
ATTESTED_AT = datetime(2026, 9, 18, 10, 30)

MARKET = MarketAssumptions(
    appreciation=Decimal("0.035"),
    rent_growth=Decimal("0.03"),
    expense_growth=Decimal("0.035"),
    insurance_growth=Decimal("0.09"),
    vacancy=Decimal("0.05"),
    discount_rate=Decimal("0.06"),
)


def build_household() -> Portfolio:
    # ---------------------------------------------------------------- 1 ----
    # Pasadena duplex. Held since 2009, so the 27.5-year clock runs out in
    # 2037. Financed interest-only for ten years; the note converts to
    # amortising in 2027 and the payment roughly doubles on a date the client
    # has forgotten about.
    pasadena = Property(
        id="pasadena",
        address="1842 Casitas Ave, Pasadena CA 91103",
        kind=PropertyKind.RESIDENTIAL_RENTAL,
        acquired=date(2009, 6, 1),
        placed_in_service=date(2009, 6, 15),
        purchase_price=M("585000"),
        land_allocation=M("205000"),
        capitalized_closing_costs=M("11800"),
        capital_improvements=(
            CapitalImprovement(date(2016, 4, 1), M("84000"), "seismic retrofit + roof"),
        ),
        liens=LienStack((
            Lien(1, "purchase-money first, interest-only to 2027",
                 interest_only_then_amortizing(
                     "pasadena-1st", M("438000"), date(2017, 7, 1),
                     rate("0.0425"), io_years=10, term_years=30)),
        )),
        state="CA",
    )

    # ---------------------------------------------------------------- 2 ----
    # Austin single-family. Bought in 2021 with a cost segregation study and
    # 100% bonus depreciation, which produced a very large paper loss in year
    # one - and a section 1245 ordinary recapture liability at exit that the
    # client has never been shown. Financed with a 5/1 ARM that reset in 2026.
    austin = Property(
        id="austin",
        address="4412 Ramsey Ave, Austin TX 78756",
        kind=PropertyKind.RESIDENTIAL_RENTAL,
        acquired=date(2021, 3, 12),
        placed_in_service=date(2021, 4, 1),
        purchase_price=M("612000"),
        land_allocation=M("122400"),
        capitalized_closing_costs=M("14300"),
        cost_segregation=CostSegregation(
            five_year=M("58000"), seven_year=M("21000"),
            fifteen_year=M("47000"), study_cost=M("6500"),
            performed_on=date(2021, 9, 1),
        ),
        bonus_rate_at_acquisition=rate("1.00"),
        liens=LienStack((
            Lien(1, "5/1 ARM, 2/2/5 caps, reset April 2026",
                 adjustable_rate_mortgage(
                     "austin-arm", M("459000"), date(2021, 4, 1),
                     rate("0.0325"), rate("0.0275"),
                     [Decimal("0.015")] * 60 + [Decimal("0.0475")] * 300)),
        )),
        state="TX",
    )

    # ---------------------------------------------------------------- 3 ----
    # Long Beach fourplex, inherited in 2018 at a stepped-up basis. Title
    # carries four paid-off liens that were never reconveyed, plus a HELOC in
    # its draw period whose proceeds were only partly spent on the property.
    #
    # The land allocation here is ASSUMED, not attested. It is the field that
    # stops the engine from recommending.
    long_beach = Property(
        id="long_beach",
        address="1247 Ocean Blvd, Long Beach CA 90802",
        kind=PropertyKind.RESIDENTIAL_RENTAL,
        acquired=date(2018, 2, 14),
        placed_in_service=date(2018, 3, 1),
        purchase_price=M("1120000"),          # FMV at date of death, IRC 1014
        land_allocation=M("336000"),          # <- assumed: 30% assessor median
        acquisition_kind=AcquisitionKind.INHERITANCE,
        liens=LienStack((
            Lien(1, "HELOC, draw period through 2032",
                 heloc("lb-heloc", M("250000"), date(2022, 1, 1),
                       FixedRate(rate("0.0825")),
                       draws=[(0, M("120000")), (30, M("60000"))],
                       interest_deductible_fraction=rate("0.62"))),
            Lien(2, "paid-off second from 2019, never reconveyed",
                 None, release_cost=M("175")),
            Lien(3, "satisfied mechanics lien (2020 roof contractor)",
                 None, release_cost=M("450")),
            Lien(4, "solar UCC-1 fixture filing, paid off 2023",
                 None, release_cost=M("125")),
            Lien(5, "county nuisance abatement lien, satisfied 2021",
                 None, release_cost=M("310")),
        )),
        state="CA",
    )

    # ---------------------------------------------------------------- 4 ----
    # The Silver Lake house: lived in until 2019, rented since. The section 121
    # two-of-five-year clock expires partway through the horizon, and the
    # depreciation taken since conversion is never excludable no matter what.
    silver_lake = Property(
        id="silver_lake",
        address="2907 Rowena Ave, Los Angeles CA 90039",
        kind=PropertyKind.RESIDENTIAL_RENTAL,
        acquired=date(2011, 8, 1),
        placed_in_service=date(2019, 9, 1),   # converted to rental
        purchase_price=M("540000"),
        land_allocation=M("243000"),
        capitalized_closing_costs=M("9200"),
        primary_residence=PrimaryResidenceHistory(
            occupied_from=date(2011, 8, 1), occupied_until=date(2019, 8, 31)),
        liens=LienStack((
            Lien(1, "30-year fixed, refinanced 2020",
                 fixed_rate_mortgage("sl-1st", M("396000"), date(2020, 6, 1),
                                     rate("0.0325"), 30)),
        )),
        state="CA",
    )

    return Portfolio(
        household_name="The Okafor Household",
        filing_status=FilingStatus.MARRIED_FILING_JOINTLY,
        properties=(pasadena, austin, long_beach, silver_lake),
        assumptions=(
            PropertyAssumptions("pasadena", M("1420000"), M("86400"), M("21600"), M("7400")),
            PropertyAssumptions("austin", M("742000"), M("43200"), M("14100"), M("4900"),
                                appreciation=Decimal("0.042")),
            PropertyAssumptions("long_beach", M("1640000"), M("122400"), M("38900"), M("14800")),
            PropertyAssumptions("silver_lake", M("1185000"), M("60000"), M("16200"), M("6100")),
        ),
        wages=M("310000"),
        portfolio_income=M("22000"),
        state="CA",
        opening_ledger=PassiveActivityLedger({
            "pasadena": M("184000"),
            "austin": M("96000"),
            "long_beach": M("41500"),
            "silver_lake": M("28700"),
        }),
    )


def build_strategies() -> list[Strategy]:
    """The three paths an advisor has to compare, plus the one nobody models."""
    return [
        Strategy("Hold", (), "Keep every property for the full horizon."),
        Strategy(
            "Sell Pasadena 2027",
            (Action(ActionKind.SELL, "pasadena", 2027, month=6),),
            "Sell the duplex outright. Releases its suspended passive losses in "
            "full, but realises 17 years of depreciation recapture at once.",
        ),
        Strategy(
            "Exchange Pasadena 2027 into Texas",
            (Action(ActionKind.EXCHANGE, "pasadena", 2027, month=6,
                    replacement_price=M("1950000"), replacement_debt=M("780000"),
                    replacement_state="TX"),),
            "Defer the gain into a larger Texas replacement. Triggers the "
            "California clawback and does NOT release the suspended losses.",
        ),
        Strategy(
            "Sell Pasadena 2027, Long Beach 2028",
            (Action(ActionKind.SELL, "pasadena", 2027, month=6),
             Action(ActionKind.SELL, "long_beach", 2028, month=9)),
            "Stagger two sales across tax years to keep the second out of the "
            "top capital gains bracket.",
        ),
        Strategy(
            "Sell both in 2027",
            (Action(ActionKind.SELL, "pasadena", 2027, month=6),
             Action(ActionKind.SELL, "long_beach", 2027, month=9)),
            "The same two sales, same prices, one tax year.",
        ),
    ]


def facts_for_demo() -> dict[str, Fact]:
    """The provenance behind the Long Beach numbers.

    This is the set the attestation gate operates on. Note that the land
    allocation is ``Assumed`` and the two depreciation figures are
    ``Extracted`` from different tax years - and they disagree.
    """
    return {
        "long_beach.fmv_at_death": Fact.attested(
            "long_beach.fmv_at_death", M("1120000"), by=ADVISOR, at=ATTESTED_AT,
            note="date-of-death appraisal in the estate file, confirmed with client",
        ),
        "long_beach.land_allocation": Fact.assumed(
            "long_beach.land_allocation", M("336000"),
            basis="30% of value - Los Angeles County assessor median land ratio "
                  "for zip 90802. No appraisal allocation is in any document provided.",
        ),
        # These two disagree, and the disagreement is the point. $28,509/yr
        # implies the preparer used a 30% land allocation; $32,582/yr implies
        # 20%. Both were filed. Neither document states the allocation
        # directly - it has to be backed out, and only the owner knows which
        # is right. Averaging them would be the worst available answer.
        "long_beach.depreciation_2022": Fact.extracted(
            "long_beach.depreciation_2022", M("28509.09"),
            document_id="2022-form-1040-schedule-e", page=2,
            cited_text="18  Depreciation expense or depletion . . . 28,509",
            confidence="0.97", extractor="claude-opus-5/evidence@1",
        ),
        "long_beach.depreciation_2024": Fact.extracted(
            "long_beach.depreciation_2024", M("32581.82"),
            document_id="2024-form-1040-schedule-e", page=2,
            cited_text="18  Depreciation expense or depletion . . . 32,582",
            confidence="0.96", extractor="claude-opus-5/evidence@1",
        ),
        "pasadena.purchase_price": Fact.attested(
            "pasadena.purchase_price", M("585000"), by=ADVISOR, at=ATTESTED_AT,
            note="2009 HUD-1 settlement statement line 101",
        ),
        "austin.cost_seg_five_year": Fact.extracted(
            "austin.cost_seg_five_year", M("58000"),
            document_id="2021-cost-segregation-study", page=11,
            cited_text="5-year personal property (appliances, carpet, "
                       "window treatments) . . . $58,000",
            confidence="0.93", extractor="claude-opus-5/evidence@1",
        ),
    }
