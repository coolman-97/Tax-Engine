# 2. Provenance is a type, and it propagates by weakest link

**Status:** accepted

## Context

A household's real-estate data arrives across several documents and several
years. Some of it is on a tax return, some on a closing statement, some in no
system at all. If the data is wrong the recommendation is wrong, so nothing
downstream should run on a number nobody has stood behind.

Attaching provenance as a logging concern does not achieve that: logs are read
after the fact, and nothing stops a number with no pedigree from becoming advice.

## Decision

Every engine input is a `Fact`: a value plus a `Provenance`, which is one of
`Assumed`, `Extracted`, `Attested` or `Derived`. These are ordered by how well
they would survive being questioned (`Standing`).

The whole mechanism is one rule:

```python
@property
def standing(self) -> Standing:
    return min(fact.standing for fact in self.inputs)
```

A derived number is exactly as defensible as its weakest input. `Fact.require()`
is the gate: it returns the value or raises `Unattested`, carrying the blocking
leaf facts.

## Alternatives considered

**Provenance on every `Money`.** Rejected: it would make arithmetic allocate on
every operation and would smear bookkeeping through the hot path for no gain.
The derivation graph is what an advisor defends, not each individual addition.

**A confidence score on the output.** Rejected: a single scalar cannot tell you
*which* input to go and ask about, and a number with a confidence attached still
gets read as a number.

## Consequences

- Nothing in the engine except `provenance.py` needs to know about
  defensibility — it propagates from the leaves for free.
- A refusal is actionable: `blocking_facts()` returns the deduplicated,
  worst-first leaf set, which `recommend.py` turns into a priced question list.
- The auto-accept threshold for extracted values is a single calibrated constant
  (`AUTO_ACCEPT_CONFIDENCE`), chosen from an operating-point curve rather than
  guessed.
