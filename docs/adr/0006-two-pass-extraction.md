# 6. Document extraction is two-pass: quote first, structure second

**Status:** accepted (designed; not built in this repo)

## Context

The pipeline has to turn tax returns into the data everything else runs on, and
the model must not be able to invent a number. "AI reads the documents and writes
the narrative; it doesn't touch the math" has to be structural, not a prompt
instruction.

There is also a hard API constraint: Anthropic's citations feature is
incompatible with `output_config.format` — sending both returns a 400. You can
have grounded quotes with page anchors, or a schema-validated object, but not
from the same call.

## Decision

Two passes, and the constraint turns out to point at the better design anyway.

**Pass A — evidence.** The document goes in as a `document` block with
`citations: {enabled: true}`. The model's only job is to *find and quote*. What
comes back is spans of verbatim text with `page_location` anchors.

**Pass B — structure.** `client.messages.parse(output_format=PropertyFacts)` runs
over **only Pass A's quoted spans**. It never sees the raw document.

Pass B cannot invent a value because it has nothing to invent from. Grounding
becomes a property of the pipeline's shape rather than something the prompt asks
for.

Each extracted field becomes a `Fact` with `Extracted` provenance carrying the
document id, page, the exact cited text and a calibrated confidence — which is
what the UI shows when an advisor clicks a number, and what the standing rule
in ADR 2 consumes.

## Also decided

- **Reconciliation never averages.** The same property reads differently across
  years; disagreements become first-class `Conflict` objects surfaced for
  resolution. The demo's blocked recommendation is exactly this case.
- **The narrative is guarded.** Any number in generated prose must appear in the
  engine's output set, checked after generation. A number the model produced that
  the engine did not is a bug, not a phrasing choice.
- **Measurement is the deliverable.** Per-field precision and recall on a golden
  set, a confidence calibration curve, and the auto-accept threshold chosen from
  the operating-point curve rather than picked — that threshold is
  `AUTO_ACCEPT_CONFIDENCE` in `provenance.py`.
