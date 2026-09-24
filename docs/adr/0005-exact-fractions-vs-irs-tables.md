# 5. Compute the exact statutory fraction; treat the IRS tables as a rounded
# presentation of it

**Status:** accepted

## Context

IRS Pub 946 publishes MACRS percentage tables, and practitioners generally use
them. But the tables are rounded to two or three decimals, and the IRS then
nudges individual years so each column sums to exactly 100.00%. Pub 946 itself
warns that figuring deductions without the tables "will generally result in a
slightly different amount".

## Decision

The engine computes the exact fraction the statute describes. The published
tables are treated as a validation target, not as the source of truth.

## Consequences

- Schedules sum to exactly the depreciable basis, for every input. A table-driven
  implementation does not, because the published percentages do not sum to 100
  before adjustment.
- The engine reproduces **Table A-6 exactly** (all twelve mid-month first-year
  percentages, three decimals) and **Table A-1 to published precision** for 5-,
  7- and 15-year property.
- There are known one-unit divergences. 7-year property year 6 is exactly
  8.925%; Pub 946 prints 8.92 so the column lands on 100.00. The test asserts
  "within half a published unit, and sums to exactly basis" — asserting byte
  equality against a rounded presentation would be testing the rounding.
- The declining-balance-to-straight-line switch year is computed (where straight
  line over the remaining period first beats declining balance), not hardcoded.
  Reproducing the published columns is the evidence it is found correctly.

If a client's prior returns were prepared with the tables, a future option
should let a property pin to table percentages for continuity — Pub 946 notes
that once you start using the tables you generally must continue.
