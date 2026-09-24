# Edge cases

A register of what breaks, kept because a list of what works is not evidence of
much. It is in two parts, and the first part is the one worth reading.

---

## Part 1 — where this engine was wrong first

Five defects that a plausible-looking implementation shipped with, how each was
caught, and what the fix was. Each has a regression test named below.

### 1. A 30-year note that never quite gets paid off

**What broke.** `Loan._level_payment` rounded the annuity payment to the nearest
cent. A $400,000 30-year note at 6.5% then still owed **$2.61** in month 360.

The true payment is $2,528.2673. Rounded half-up it is $2,528.27, which is
$0.0073/month short — trivially small, and over 360 months it compounds into a
stub balance that never amortises away.

**Why it matters beyond the $2.61.** The balance feeds debt payoff at sale, which
feeds net proceeds, which feeds the whole hold/sell comparison. More importantly
a loan that does not retire means `balance_on(maturity)` is non-zero, and any
downstream code that tests "is this loan paid off" silently says no, forever.

**How it was caught.** Printing the last period of a schedule during a smoke test
of the five loan constructors.

**Fix.** Round the level payment **up** (`ROUND_CEILING`), which is what servicers
actually do and for exactly this reason. Regression:
`tests/test_loans.py::test_every_amortizing_loan_retires_to_zero`.

### 2. …and a $0.01 stub that survived the fix

**What broke.** Hypothesis, exploring the same invariant across 120 generated
loans, found that a **$13,164.73** note at **3.25%** still owed **$0.01** at
maturity even with the payment rounded up.

Rounding the payment up buys a small monthly surplus, but interest is *also*
rounded to the cent each month, and on a small balance those roundings can eat
the surplus entirely.

**How it was caught.** Property-based testing. No hand-picked example would have
found this — I had tried five, and all five passed.

**Fix.** The final scheduled payment of an amortising phase is a **payoff
payment** (`balance + interest`), not the level amount. Again this is what really
happens: a servicer sends a payoff quote for the last payment. The invariant now
holds for every generated input rather than for the examples I thought of.

### 3. Mortgage boot overstated on every trade-down

**What broke.** In a 1031 exchange, debt relief not matched by new debt is
taxable boot. The engine computed it as `old_debt − new_debt` and only offset it
by an `additional_cash_invested` field the caller had to pass explicitly.

But cash brought into the exchange is boot *given*, and it nets against debt
relief — Reg. §1.1031(b)-1(c). That cash is almost never stated anywhere. It is
**implied by the deal structure**: whenever the replacement costs more than the
relinquished equity covers, the shortfall is cash the client has to write a
cheque for.

On the demo property: debt relief of $254,443.85, replacement requiring $19,643.85
of new cash. The engine reported **$254,443.85** of taxable boot. The right answer
is **$234,800.00** — an overstatement of nearly twenty thousand dollars of
recognised gain, on a transaction the client was told was tax-deferred.

**How it was caught.** Reading the engine's own output for the trade-down
scenario and noticing that the client was obviously contributing cash that the
boot calculation ignored.

**Fix.** Derive the cash from the structure instead of asking for it.
Regression: `tests/test_exchange_1031.py::test_cash_brought_into_the_exchange_offsets_mortgage_boot`.

### 4. 15-year land improvements classified as §1245 property

**What broke.** The engine treated all three cost-segregation buckets — 5-year,
7-year and 15-year — as §1245 property, recapturing every dollar as ordinary
income. It also cited `IRC 168(e)(3)(E)` for the 15-year class.

Both are wrong, and they are wrong in opposite directions:

- **The citation.** §168(e)(3)(E) covers municipal wastewater plant, telephone
  distribution plant, retail motor fuels outlets, gas-utility clearing and
  grading, 69kV+ electric transmission, natural gas distribution lines, and QIP.
  It does not cover a parking lot. Ordinary land improvements reach a 15-year
  recovery period through **§168(e)(1)** and Rev. Proc. 87-56 asset class 00.3.
- **The classification.** Reg. §1.48-1(c) defines tangible personal property as
  "any tangible property except land and improvements thereto", and an apartment
  building is not an integral part of manufacturing under §1245(a)(3)(B). Land
  improvements therefore fall to **§1250(c)**, not §1245.

**Why it matters.** §1250 recaptures only *additional* depreciation — the excess
over hypothetical straight line. For a building that excess is zero, which is why
individuals have no ordinary recapture on buildings. But 15-year land
improvements run on **150% declining balance** and are **bonus-eligible**, so the
excess is large. The engine had no concept of additional depreciation at all.

**How it was caught.** Cross-checking the engine's citations against primary
sources (IRC, Reg., Pub 946, Form 4797 instructions) rather than trusting them.

**Fix.** A `Recapture` classification on every recovery class, and
`DepreciationSchedule.additional_depreciation_through()` implementing
§1250(b)(1). On the demo's cost-segregated property this moves **$31,333** from
the 25% bucket to ordinary rates. Regressions: `tests/test_recapture.py`.

### 5. A strategy comparison that compared different things

**What broke.** Comparing "sell both properties in 2027" against "sell one in
2027 and one in 2028", the engine reported that splitting the sales *cost*
$58,448 more — the opposite of what bracket management predicts.

The scenarios were not comparable. The combined case covered one tax year; the
split case covered two, and therefore counted an extra year of the household's
$310,000 of wages.

**How it was caught.** The result contradicted the mechanism it was supposed to
demonstrate, which is the cheapest kind of bug to catch and the easiest to
rationalise away.

**Fix.** Both scenarios now span the identical two-year window. Splitting the
sales saves **$7,528** by keeping the second sale under the 20% capital-gains
threshold — the direction the mechanism predicts, and a defensible number.

**The general lesson.** The engine was right in all five of these; the *analysis*
was wrong. A result that supports your thesis is exactly when to check the
denominator.

---

## Part 2 — hunted, handled, and under test

Cases the engine was built to handle, each with a test. The four in **bold** are
the instruments that most reliably break a naive implementation.

### Debt instruments

| Case | What breaks naively | Test |
|---|---|---|
| **Interest-only → amortising** | Payment is sized over the full term, not the remaining term, so the conversion shock disappears. Real shock on the demo note: **+75%**, on 1 July 2027. | `test_interest_only_conversion_is_a_payment_shock` |
| **ARM caps** | "2/2/5" is three *different* reference points — first adjustment against the initial rate, later ones against the *previous* rate, everything against initial+lifetime. Using the initial rate for all three understates the reachable rate. | `test_arm_respects_all_three_caps` |
| ARM floor | The floor is usually the margin, not zero. | `test_arm_floor_holds_when_the_index_collapses` |
| **HELOC** | Has no opening principal — the balance starts at zero and grows with draws, so it cannot be modelled as a loan with a principal. | `test_heloc_starts_empty_and_tracks_draws` |
| HELOC interest tracing | Interest follows the *use of the proceeds*, not the collateral (Temp. Reg. §1.163-8T). A HELOC on a rental spent on a boat is not rental interest. | `test_heloc_interest_is_only_deductible_to_the_traced_extent` |
| **Four paid-off liens** | Filtering the stack on `balance > 0` drops them. They still cloud title, still cost **$1,060** to release here, and still push new borrowing to position 6 rather than 2. | `test_paid_off_liens_still_have_to_be_cleared`, `test_paid_off_liens_push_new_borrowing_down_the_stack` |
| Negative amortisation | A payment below accrued interest must grow the balance, not floor at zero. | `test_negative_amortization_is_representable` |
| Balloon | Final payment is the whole balance. | `test_balloon_pays_the_whole_balance_at_the_end` |
| PMI termination | Terminates at 78% of **original** value on the **scheduled** balance (Homeowners Protection Act). Appreciation does not cancel PMI. | `test_pmi_terminates_on_the_scheduled_balance_not_market_value` |
| Prepayment penalty | Applies only inside its window. | `test_prepayment_penalty_applies_only_inside_the_window` |
| Payoff waterfall | Must conserve every cent and respect seniority. | `test_payoff_waterfall_conserves_every_cent` |

### Depreciation

| Case | What breaks naively | Test |
|---|---|---|
| Mid-month convention | January placed-in-service is 23/24 of a year (3.485%); December is 1/24 (0.152%). Using a half-year convention on real property is wrong by up to 11 months. | `test_matches_irs_table_a6_residential_27_5_year` |
| DB→SL switch | The switch year is *computed* (where straight line over the remaining period first beats declining balance), not hardcoded. | `test_matches_irs_table_a1_five_year_200db_half_year` |
| Schedule must sum to basis | Rounding each year independently accumulates drift. Cumulative differencing makes the total exact for every input. | `test_schedule_sums_to_exactly_basis` (property-based) |
| Bonus never applies to the building | §168(k) needs a recovery period ≤ 20 years. The asymmetry between the building and its carved-out components is the entire financial point of a cost seg study. | `test_bonus_never_applies_to_the_building` |
| Disposition-year proration | Mid-month gives `(month − 0.5)/12` of a year in the year of sale. | `test_disposition_year_uses_mid_month_proration` |
| "Allowed **or allowable**" | Basis is reduced by depreciation the taxpayer *could* have taken even if they did not (§1016(a)(2)). Counting only what was claimed overstates basis and understates gain. | documented in `depreciation.py` |

### Tax character and the household

| Case | What breaks naively | Test |
|---|---|---|
| §469(g) release | A fully taxable disposition releases the entire suspended stack **for that activity**, and the freed losses are no longer passive — they shelter wages and gain on other properties. Worth **$78,915** on a single sale in the demo. | `test_full_disposition_releases_the_entire_suspended_stack` |
| §1031 does **not** release | An exchange is not a fully taxable disposition. The losses transfer to the replacement activity. Getting this backwards invents a large deduction that does not exist. | `test_an_exchange_does_not_release_suspended_losses` |
| Capital gain stacking | Gain is taxed at the rate for *combined* income, not in isolation. On a $200k gain the difference between a $40k and a $700k household is the whole 0%-vs-20% spread. | `test_capital_gain_stacks_on_ordinary_income` |
| 25% is a **ceiling** | §1(h)(1)(E) caps unrecaptured §1250 at 25%; a taxpayer below that bracket pays less. | `test_unrecaptured_1250_is_capped_at_25_percent_but_can_be_lower` |
| §469(i) phaseout | $1 of allowance lost per $2 of MAGI over $100,000; gone at $150,000. | `test_the_allowance_phases_out_at_50_cents_on_the_dollar` |
| §1231(c) lookback | Five-year nonrecaptured losses recharacterise this year's gain as ordinary. Most models ignore it. | `test_section_1231_lookback_recharacterizes_gain_as_ordinary` |
| NIIT is the **lesser** of | 3.8% of the lesser of NII and MAGI-over-threshold — not of whichever you looked at first. | `test_niit_is_the_lesser_of_nii_and_magi_excess` |
| Real estate professional | §469(c)(7) makes rentals non-passive *and* generally takes the income outside the NIIT. | `test_real_estate_professional_deducts_losses_currently` |

### Statutory drift over a long horizon

| Case | What breaks naively | Test |
|---|---|---|
| Unindexed thresholds | Most parameters are inflation-indexed. The NIIT thresholds (frozen 2013), the §469(i) allowance (frozen 1986) and the §121 exclusion (frozen 1997) are **not**. Projecting everything forward with inflation produces a household whose tax picture looks stable forever. It isn't — those thresholds fall in real terms every year, and the engine can name the date a household crosses one. | `test_niit_thresholds_are_not_indexed` |

### §1031 specifics

| Case | What breaks naively | Test |
|---|---|---|
| Boot pulls the *worst* slice first | Not a proportional share. Small boot on a cost-segregated property can be taxed entirely at ordinary rates. | `test_recapture_is_recognized_before_capital_gain` |
| A sliver of equity is still boot | $356 of unreinvested equity is taxable. Exchanges are all-or-nothing at the margin. | `test_a_sliver_of_unreinvested_equity_is_still_boot` |
| California clawback | CA conforms to §1031 but does not let go: exchange out of state and **FTB 3840** must be filed every year until the gain is recognised, and CA taxes it regardless of where the client lives by then. | `test_california_clawback_fires_on_an_out_of_state_replacement` |
| Related-party two-year rule | §1031(f): either party disposing within two years retroactively disallows the deferral. | `test_related_party_two_year_rule_is_flagged` |
| 45/180-day deadlines | Missing the 180-day close fails the exchange **entirely** — the whole gain is taxable. | `test_missing_the_180_day_deadline_is_flagged_loudly` |
| Basis carries the deferred gain | The tax is not forgiven, it is baked into a lower replacement basis. | `test_replacement_basis_carries_the_deferred_gain_forward` |

### §121, for a converted residence

| Case | What breaks naively |
|---|---|
| Depreciation is **never** excludable | §121(d)(6). A converted rental always has a taxable unrecaptured §1250 slice no matter how long it was a home. The most common §121 error there is. |
| Nonqualified use | §121(b)(5) prorates the exclusion by post-2008 non-residence use — but periods *after* the last date of residence do not count, which is the carve-out that makes convert-then-sell work at all. |
| Not indexed | $250k/$500k, unchanged since 1997. |

---

## Known limitations

Stated because a register that only lists wins is not a register.

- **Sale proceeds are held as cash and do not appreciate.** This flatters holding
  and exchanging in the net-worth chart. The present-value ranking is the fairer
  comparison and the chart's caption says so; modelling reinvestment at a stated
  portfolio return is the right fix and is not done.
- **C corporations are not modelled.** §291(a)(1) gives C corps ordinary
  recapture on straight-line real property and no unrecaptured §1250 concept at
  all. The engine assumes an individual return throughout.
- **§199A is in the rule data but not applied** in the tax computation.
- **State coverage is CA and TX only.** The schema supports non-conformity
  generally; only California's is filled in.
- **Mid-quarter convention** is implemented but not triggered automatically by
  the >40%-in-Q4 test.
- **The 180-day deadline** does not yet apply the earlier-of-return-due-date
  rule, which catches Q4 closings.
