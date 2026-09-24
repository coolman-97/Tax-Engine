# 1. Money is integer cents, and floats are rejected at the type boundary

**Status:** accepted

## Context

The engine's outputs get defended out loud by an advisor with their license
behind them. A depreciation schedule runs for 27.5 years, so a rounding error in
year one is still there in year twenty-seven — and accumulated depreciation
drives adjusted basis, which drives gain, which drives unrecaptured §1250 tax at
exit.

## Decision

`Money` stores a signed integer number of cents. It **cannot be constructed from
a float** — `Money.from_dollars(0.1)` raises `TypeError`. Rates are `Decimal` and
`rate()` refuses floats for the same reason.

Multiplying money by a rate is not an operator. It is `Money.apply_rate(r,
rounding=...)`, which requires the caller to have already decided the rounding
mode. There is therefore exactly one rounding point in the engine and it is
greppable.

Splitting uses `Money.allocate()`, largest-remainder, so the parts always sum to
exactly the whole.

## Consequences

- `sum(m.allocate(w)) == m` holds for every input, asserted by Hypothesis.
- Straight-line depreciation uses cumulative differencing rather than rounding
  each year, so a schedule sums to exactly the depreciable basis.
- Exact arithmetic is slower than float. The 30-year, 4-property, 5-strategy
  demo runs in 37 ms, which is fast enough that it never became a question.
- Callers must write `Money.from_dollars("612000")`, not `612000.0`. This is
  deliberate friction at the one place it matters.
