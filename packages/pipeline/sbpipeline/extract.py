"""Two-pass extraction: quote first, structure second.

Pass A hands the model the document with citations enabled and asks it to do
exactly one thing: find the figures and **quote them**. What comes back is
spans of verbatim text with the page they appear on.

Pass B runs over **only those quotes**. It never sees the document. It cannot
invent a value because it has nothing to invent from - grounding stops being
something the prompt asks for and becomes a property of the pipeline's shape.

The split was forced by an API constraint (citations return a 400 alongside
``output_config.format``, so you cannot have grounded quotes and a
schema-validated object from one call) and it turned out to be the better
design. See docs/adr/0006-two-pass-extraction.md.

Everything a field needs to become a `Fact` with `Extracted` provenance -
document id, page, the exact cited text, a confidence - comes out of here.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .cassettes import Cassette, CassetteMiss
from .corpus import Document
from .schemas import Evidence, PropertyFacts

__all__ = ["ExtractionResult", "Extractor", "EVIDENCE_MODEL", "STRUCTURE_MODEL"]

#: Pass A reads a form layout and has to not misread a column. That is the
#: harder of the two jobs, so it gets the stronger model.
EVIDENCE_MODEL = "claude-opus-5"
#: Pass B transcribes already-quoted spans into a schema. That is mechanical,
#: so it runs on the cheap model - and the eval measures whether that holds.
STRUCTURE_MODEL = "claude-haiku-4-5"

# Per million tokens, for the cost column in the eval report.
PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

def _blocks_and_pages(doc: Document) -> tuple[list[dict], list[int]]:
    """Chunk the document into one content block per line.

    Citation granularity equals content-block granularity. Send a page as one
    block and every citation comes back quoting the entire page - 2,000
    characters of form header, useless as evidence a human can check in two
    seconds. Send it a line at a time and a citation quotes the actual form
    line, which is exactly the span an advisor needs to see.

    Returns the blocks and a parallel list mapping block index to page number,
    so `content_block_location.start_block_index` resolves to a page.
    """
    blocks: list[dict] = []
    page_of: list[int] = []
    for page_no, page in enumerate(doc.pages, start=1):
        for line in page.split("\n"):
            if not line.strip():
                continue
            blocks.append({"type": "text", "text": line})
            page_of.append(page_no)
    return blocks, page_of


EVIDENCE_SYSTEM = """\
You are reading a US federal tax document for a financial advisor who will rely \
on these figures to advise a client on a property worth more than a million \
dollars. Being wrong is worse than being incomplete.

Your ONLY job in this step is to FIND and QUOTE. Do not compute anything. Do \
not sum columns. Do not convert or reformat numbers. Do not infer a value that \
is not written down.

Output ONLY data lines, in exactly this format, one per line:

  <property letter> :: <field_name> :: <the figure exactly as written>

The property letter, the field name and the value must all be on the SAME \
line. Write no prose, no headings, no commentary, and no summary - not before \
the lines, not after them.

Cover EVERY property on the form. Schedule E has columns A, B and C; if all \
three are filled in, all three must appear in your output. Skipping a property \
is a failure, not a safe omission.

Use these field names only:
  property_label, rents_received, total_expenses, mortgage_interest,
  depreciation, insurance, property_taxes, date_placed_in_service,
  cost_or_basis, recovery_period_years

Rules:
- Quote the figure exactly as it appears, including commas. Do not add or \
remove a currency symbol.
- Schedule E is a COLUMNAR form: column A, B and C are different properties. \
Read down the correct column. Mixing columns is the most damaging error you \
can make here.
- The line numbers are your strongest signal: rents received is line 3, \
insurance line 9, mortgage interest line 12, taxes line 16, depreciation \
line 18, total expenses line 20.
- If a figure is absent, illegible, or you are not sure which column it \
belongs to, omit the line entirely. An omission is recoverable; a wrong number \
that looks confident is not.
- If the document is a scan with character errors, prefer the reading that is \
consistent with the form's arithmetic, and omit anything still ambiguous."""

STRUCTURE_SYSTEM = """\
You will be given quoted spans extracted from a tax document, and nothing else. \
Transcribe them into the schema.

You do not have the document. Do not infer, compute, or supply any value that \
is not present in the spans you were given. If a field has no span, leave it \
null.

Money fields are whole dollars as integers: strip commas and any currency \
symbol, and do not round. "28,509" becomes 28509.

Dates are ISO, YYYY-MM-DD. A form that writes only a month and year, such as \
"04/2021", becomes "2021-04-01". Reformatting a date you were given is \
transcription, not inference; supplying a date you were NOT given is not \
allowed."""


@dataclass
class ExtractionResult:
    document_id: str
    properties: list[PropertyFacts] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: float = 0.0
    models: tuple[str, ...] = ()
    error: str | None = None

    @property
    def cost_usd(self) -> float:
        total = 0.0
        for model in self.models:
            inp, out = PRICES.get(model, (0.0, 0.0))
            total += (self.input_tokens / 1e6) * inp + (self.output_tokens / 1e6) * out
        return total / max(1, len(self.models))

    def evidence_for(self, label: str, field_name: str) -> Evidence | None:
        for e in self.evidence:
            if e.field == field_name and label.lower() in e.quote.lower():
                return e
        for e in self.evidence:
            if e.field == field_name:
                return e
        return None


class Extractor:
    """Runs the two passes, through a cassette layer."""

    def __init__(
        self,
        *,
        live: bool = False,
        record: bool = False,
        cassette: Cassette | None = None,
        evidence_model: str = EVIDENCE_MODEL,
        structure_model: str = STRUCTURE_MODEL,
    ) -> None:
        self.live = live
        self.cassette = cassette or Cassette(record=record)
        self.evidence_model = evidence_model
        self.structure_model = structure_model
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    # ------------------------------------------------------------------
    def _call(self, request: dict[str, Any], kind: str, document_id: str) -> dict:
        """One API call, cassette-first."""
        keyed = dict(request)
        keyed["_pass"] = kind
        keyed["_document"] = document_id
        cached = self.cassette.get(keyed)
        if cached is not None:
            return cached
        if not self.live:
            raise CassetteMiss(
                f"no cassette for {kind} on {document_id}. Run with --live to "
                f"record one (requires ANTHROPIC_API_KEY)."
            )
        response = self.client.messages.create(**request)
        payload = response.model_dump(mode="json")
        self.cassette.put(keyed, payload)
        return payload

    # ------------------------------------------------------------------
    def pass_a_evidence(self, doc: Document) -> tuple[list[Evidence], dict]:
        """Find and quote. Citations give us the page for free."""
        blocks, page_of = _blocks_and_pages(doc)
        request = {
            "model": self.evidence_model,
            "max_tokens": 8000,
            "system": [{
                "type": "text", "text": EVIDENCE_SYSTEM,
                # The instruction is identical for every document, so it is
                # worth caching: on a 15-document run it is read 15 times.
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        # One content block per page, so a citation's block
                        # index IS the page number. That is where the page in
                        # the provenance chain comes from.
                        "source": {"type": "content", "content": blocks},
                        "title": doc.id,
                        "citations": {"enabled": True},
                    },
                    {"type": "text",
                     "text": f"Extract the figures from this {doc.kind.replace('_', ' ')} "
                             f"for tax year {doc.tax_year}. Quote only."},
                ],
            }],
        }
        payload = self._call(request, "evidence", doc.id)
        return self._parse_evidence(payload, doc, page_of), payload

    @staticmethod
    def _parse_evidence(
        payload: dict, doc: Document, page_of: list[int]
    ) -> list[Evidence]:
        """Turn cited text blocks into Evidence.

        The API splits the response **at citation boundaries**, which is the
        thing that makes a naive parser return nothing. A single logical line
        of output arrives as two blocks:

            block n   : 'A :: rents_received :: '      (no citation)
            block n+1 : '82,100'                       (carries the citation)

        So neither block parses on its own - the first has no value, the second
        has no field name. The parser therefore works over the *joined* text
        with a character-to-block map, matches the whole
        ``label :: field :: value`` pattern across the seam, and then looks up
        which block contributed the value to get its citation.

        That citation is the whole point: its ``cited_text`` is the exact form
        line the figure was read from, and its ``start_block_index`` resolves
        through ``page_of`` to a page. Neither is the model's claim about
        itself - both come from the API.
        """
        text_blocks = [b for b in payload.get("content", []) if b.get("type") == "text"]

        joined: list[str] = []
        owner: list[int] = []          # character index -> index into text_blocks
        for i, block in enumerate(text_blocks):
            chunk = block.get("text", "")
            joined.append(chunk)
            owner.extend([i] * len(chunk))
        stream = "".join(joined)

        def citation_for(char_index: int) -> tuple[int, str]:
            """Page and quote for the block covering this character."""
            if char_index >= len(owner):
                return 1, ""
            block = text_blocks[owner[char_index]]
            best_page, best_quote = 1, ""
            for c in block.get("citations") or []:
                quote = c.get("cited_text", "") or ""
                if c.get("type") == "content_block_location":
                    idx = int(c.get("start_block_index", 0))
                    page = page_of[idx] if idx < len(page_of) else 1
                else:
                    page = 1
                # Prefer a quote that looks like a form line over a header.
                if not best_quote or (
                    re.search(r"\d", quote) and len(quote) < len(best_quote)
                ):
                    best_quote, best_page = quote, page
            return best_page, best_quote

        out: list[Evidence] = []
        pattern = re.compile(
            r"([A-Za-z0-9 ,.'\-]{1,60}?)\s*::\s*([a-z_ ]{3,40}?)\s*::\s*([^\n]{1,80})"
        )
        for match in pattern.finditer(stream):
            label = match.group(1).strip().lstrip("*# ").strip()
            name = match.group(2).strip().lower().replace(" ", "_")
            value = match.group(3).strip()
            if not value or not name:
                continue
            page, cited = citation_for(match.start(3))

            # Confidence prior: a cited span that begins with an explicit form
            # line number is the strongest evidence available; a span with any
            # digits is next; an uncited value is weakest. This is only a
            # prior - the calibration curve in evals/run.py is what says
            # whether it means anything.
            if re.match(r"\s*\d{1,2}[a-c]?\s+\S", cited or ""):
                confidence = 0.97
            elif cited and re.search(r"\d", cited):
                confidence = 0.93
            elif cited:
                confidence = 0.85
            else:
                confidence = 0.70
            if doc.note:
                confidence -= 0.06  # scanned document

            evidence = Evidence(
                field=name, quote=(cited or value).strip()[:300],
                page=page, confidence=max(0.05, min(0.99, confidence)),
            )
            evidence.__dict__["_label"] = label
            evidence.__dict__["_value"] = value
            out.append(evidence)
        return out

    # ------------------------------------------------------------------
    def pass_b_structure(
        self, doc: Document, evidence: list[Evidence]
    ) -> tuple[list[PropertyFacts], dict]:
        """Structure the quotes. The document is deliberately not supplied."""
        rendered = "\n".join(
            f"[page {e.page}] {e.__dict__.get('_label', '?')} :: {e.field} :: "
            f"{e.__dict__.get('_value', '')}    (quoted: {e.quote[:120]!r})"
            for e in evidence
        )
        schema = {
            "type": "object",
            "properties": {
                "properties": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "property_label": {"type": "string"},
                            "tax_year": {"type": "integer"},
                            "rents_received": {"type": ["integer", "null"]},
                            "total_expenses": {"type": ["integer", "null"]},
                            "mortgage_interest": {"type": ["integer", "null"]},
                            "depreciation": {"type": ["integer", "null"]},
                            "insurance": {"type": ["integer", "null"]},
                            "property_taxes": {"type": ["integer", "null"]},
                            "date_placed_in_service": {"type": ["string", "null"]},
                            "cost_or_basis": {"type": ["integer", "null"]},
                            "recovery_period_years": {"type": ["number", "null"]},
                        },
                        "required": [
                            "property_label", "tax_year", "rents_received",
                            "total_expenses", "mortgage_interest", "depreciation",
                            "insurance", "property_taxes", "date_placed_in_service",
                            "cost_or_basis", "recovery_period_years",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["properties"],
            "additionalProperties": False,
        }
        request = {
            "model": self.structure_model,
            "max_tokens": 4000,
            "system": STRUCTURE_SYSTEM,
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
            "messages": [{
                "role": "user",
                "content": f"Tax year {doc.tax_year}. Quoted spans:\n\n{rendered}",
            }],
        }
        payload = self._call(request, "structure", doc.id)
        text = next(
            (b["text"] for b in payload.get("content", []) if b.get("type") == "text"),
            "{}",
        )
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return [], payload
        facts = []
        for row in data.get("properties", []):
            try:
                facts.append(PropertyFacts.model_validate(row))
            except Exception:
                continue
        return facts, payload

    # ------------------------------------------------------------------
    def extract(self, doc: Document) -> ExtractionResult:
        started = time.perf_counter()
        result = ExtractionResult(document_id=doc.id,
                                  models=(self.evidence_model, self.structure_model))
        try:
            evidence, pa = self.pass_a_evidence(doc)
            facts, pb = self.pass_b_structure(doc, evidence)
        except CassetteMiss as exc:
            result.error = str(exc)
            result.latency_ms = (time.perf_counter() - started) * 1000
            return result

        result.evidence = evidence
        result.properties = facts
        for payload in (pa, pb):
            usage = payload.get("usage") or {}
            result.input_tokens += usage.get("input_tokens", 0) or 0
            result.output_tokens += usage.get("output_tokens", 0) or 0
            result.cache_read_tokens += usage.get("cache_read_input_tokens", 0) or 0
        result.latency_ms = (time.perf_counter() - started) * 1000
        return result
