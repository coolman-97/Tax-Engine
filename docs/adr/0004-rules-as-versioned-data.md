# 4. Tax rules are versioned data with citations, not code

**Status:** accepted

## Context

Too much of what a firm knows about real estate lives in someone's head. Rules
also change: OBBBA made the TCJA rate structure permanent and restored 100% bonus
depreciation with an *acquisition-date* cliff, and thresholds move every year.

"Why is this number different from last quarter" should have a better answer than
"someone changed the code".

## Decision

Rules live in YAML keyed by tax year and jurisdiction, loaded into frozen
Pydantic models. Each file carries a `source` and a per-field `verified` mapping
naming the primary source for each figure. Every computed result records the
`RuleSet.version` that produced it.

## The projection rule

Projecting forward indexes **only what statute indexes**. Held fixed:

| Parameter | Frozen since |
|---|---|
| §1411 NIIT thresholds ($250k joint) | 2013 |
| §469(i) allowance ($25k) and its $100k phaseout | 1986 |
| §121 exclusion ($250k / $500k) | 1997 |

Inflating these is a real modelling bug. Over a thirty-year horizon it is the
difference between "this household never pays NIIT" and "this household pays NIIT
every year from a date the engine can name".

## Consequences

- Adding a state is a YAML file, not a code change.
- A rule set from March 2026 can be reproduced in March 2027.
- The schema encodes non-conformity as first-class (California does not conform
  to bonus depreciation, taxes capital gain as ordinary income, and claws back
  deferred §1031 gain on FTB 3840).
