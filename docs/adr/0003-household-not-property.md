# 3. The engine simulates a household, not a property

**Status:** accepted

## Context

The obvious shape for a real-estate tax engine is a function from a property to
a tax figure. Every spreadsheet an advisor uses today has that shape.

## Decision

The unit of computation is a **household tax year**. Properties contribute
`RentalYear` records and `GainCharacter` slices; the tax is computed once for the
whole return.

## Why

Three effects are invisible to a per-property model, and all three routinely
change the answer:

1. **§469 passive losses are a shared pool.** A fully taxable disposition
   releases that activity's entire suspended stack, and the freed losses are no
   longer passive — they shelter wage income and gain on other properties. Worth
   $78,915 on a single sale in the demo household.
2. **Brackets are shared.** Capital gain stacks on ordinary income, so two sales
   in one year push each other up the table. Splitting them across two years
   saves $7,528 on identical properties at identical prices.
3. **The §1411 NIIT threshold is a household cliff**, not a property one, and it
   has never been indexed.

## Consequences

- `compute_tax_year()` takes the whole household and an opening passive-activity
  ledger, and returns a fully broken-out result so every line can be defended
  separately.
- Order of operations follows the return itself, because that is the order that
  is defensible when a CPA checks it.
- A single-property answer is now a special case of the general one, rather than
  the other way round.
