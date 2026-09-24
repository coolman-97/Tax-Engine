# What I'd build next, and in what order

This repo is an engine and a viewer. The parts that would make it a product are
below, in the order I'd actually do them, with what I'd measure for each.

It is written against the roadmap Leveridge described as open: the document
pipeline, a conversational layer over a client's whole portfolio, and monitoring
that tells an advisor something changed before the client calls them.

---

## First: the document pipeline, end to end

*This is the piece I'd want to own, and it's the one this repo deliberately
stops short of.* The design is in [ADR 6](adr/0006-two-pass-extraction.md); what
follows is how I'd build and, more importantly, how I'd know whether it works.

**The shape.** Two passes, forced by a real constraint and better for it.
Anthropic's citations feature returns a 400 alongside `output_config.format`, so
you cannot get grounded page-anchored quotes and a schema-validated object from
one call. Pass A takes the document with `citations: {enabled: true}` and does
nothing but find and quote. Pass B runs `messages.parse()` over **only those
quotes** and never sees the raw document. Pass B cannot invent a value because it
has nothing to invent from — grounding becomes a property of the pipeline's shape
instead of something the prompt asks for.

**Measurement is the deliverable, not a follow-up.** An extraction pipeline
without an eval is a demo. What I'd build alongside it:

- A **synthetic corpus generator** producing realistic multi-year returns
  (Schedule E, Form 4562, 1040 pages, closing disclosures, lender statements)
  with deliberate real-world mess: a missing tax year, the same property named
  three ways, a depreciation figure that disagrees year over year. Because the
  documents are generated, ground-truth labels come free — which is what makes
  the eval honest rather than hand-labelled and small.
- **Per-field precision and recall**, not a document-level accuracy number. The
  fields are not equally important: getting `date_placed_in_service` wrong breaks
  the whole depreciation schedule; getting a management fee wrong moves cash flow
  by a little.
- **A confidence calibration curve** (reliability diagram + expected calibration
  error). A model that says 0.95 should be right 95% of the time. If it isn't,
  the auto-accept threshold in `provenance.py` is meaningless.
- **An operating-point curve**: auto-accept threshold against human-review
  burden. The threshold is then *chosen* from a measured tradeoff rather than
  picked — target field-level precision at or above 0.99, and report what
  fraction of fields that leaves for review.
- **Cost and latency per document**, with model routing measured rather than
  assumed. Opus for hard pages, Haiku for routine structuring, and a number
  attached to the decision.
- **A CI regression gate.** Recorded response cassettes committed so the eval
  runs offline with no API key, and the build fails on regression.

**Reconciliation never averages.** The same property reads differently across
years. Disagreements become first-class `Conflict` objects surfaced for
resolution — the demo's blocked recommendation is exactly this case, and it's
the behaviour that makes the extraction trustworthy rather than merely fast.

**Why this first:** everything else runs on this data. It is also the piece where
"applied AI inside a pipeline that has to be right" is a real engineering problem
rather than a prompt.

---

## Second: monitoring, because it is nearly free here

The engine already computes every trigger. `simulate()` emits them today:

```
2026  austin: PAYMENT SHOCK on 2026-04-01 — $1,997.60/mo becomes $2,456.43/mo (+23%)
2027  pasadena: PAYMENT SHOCK on 2027-07-01 — $1,551.25/mo becomes $2,712.25/mo (+75%)
2043  pasadena: depreciation schedule exhausted — the shelter ends and taxable
      rental income steps up with no change to the property
```

Turning that into an advisor-facing feed is a scheduler, a diff against last
run, and a notification. The triggers worth shipping first:

- **Interest-only conversions and ARM resets**, dated, with the new payment.
- **Depreciation runway ending.** Nothing about the property changes; the
  client's taxable income steps up anyway. Worth a conversation three years out.
- **A §1031 identification deadline inside 45 days**, and the 180-day close —
  including the earlier-of-return-due-date rule that catches Q4 closings.
- **FTB 3840 filing due** for any client with a live California clawback. Miss
  it and the FTB may assess the deferred gain outright.
- **The §121 clock expiring** on a converted residence.
- **A household about to cross the NIIT threshold**, which is computable years
  ahead precisely because the threshold never moves.
- **DSCR breach or a negative-cash-flow crossover**, from the rent and expense
  growth paths.

The valuable framing is not "here is an alert". It is *the advisor calls the
client first.*

---

## Third: the conversational layer, with the model kept away from arithmetic

The rule this repo already enforces — AI reads and writes, it does not compute —
extends cleanly:

- The model's **only** output is a **typed scenario DSL**, validated by Pydantic
  before anything runs. "What if we sell the Pasadena duplex in 2027 and exchange
  into two properties?" becomes a `Strategy` object, not a number.
- The engine executes it deterministically.
- The model then narrates the engine's output — and a **numeric guard** checks
  that every number appearing in the prose also appears in the engine's output
  set. A number the model produced that the engine did not is a bug, not a
  phrasing choice, and it fails loudly.

That is measurable too: NL→DSL accuracy on a labelled set of advisor questions,
and the guard's catch rate on deliberately perturbed narratives.

---

## Then: the things that make the numbers more right

Roughly in order of how often they'd bite:

1. **Reinvestment of sale proceeds.** Today proceeds sit as cash at 0%, which
   flatters holding and exchanging. Every comparison improves once proceeds are
   reinvested at a stated portfolio return.
2. **Installment sales (§453).** Spreading gain across years interacts directly
   with the bracket and NIIT-threshold effects the engine already models — but
   recapture must be recognised in year one, which is the part people get wrong.
3. **More states.** The schema already handles non-conformity; only California
   is filled in. Pennsylvania's §1031 history and Massachusetts are the next
   interesting ones.
4. **C corporations.** §291(a)(1) gives C corps ordinary recapture on
   straight-line real property and no unrecaptured §1250 concept at all.
5. **Monte Carlo over the assumptions**, with the tax math staying exactly
   deterministic conditional on each path — distributional outcomes, exact
   arithmetic.
6. **Disposition sequencing as a search.** The engine can already price any
   (property × year × strategy) combination; finding the cheapest way to raise a
   given amount of cash is a beam search over the year axis scored by the real
   engine. The demo shows a $7,528 difference from moving one closing date;
   across a six-property portfolio there is more.
7. **Emit the forms.** Filling Form 4797, Form 8824, Schedule E and FTB 3840
   line by line is both the proof of correctness in the language a CPA checks,
   and an independent cross-check on the engine — the form's own internal
   arithmetic has to agree.

---

## What I'd want to be held to

- No number reaches an advisor without a provenance chain. The gate is the
  product, not a feature of it.
- Every tax rule carries a citation to a primary source, and anything unverified
  says so rather than implying otherwise.
- The eval exists before the pipeline is called done, and its threshold is
  chosen from a measured curve.
- When I break something, it goes in `EDGE_CASES.md` with the commit that caught
  it. That document is more useful than a changelog.
