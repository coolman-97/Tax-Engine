"""The extraction eval.

A pipeline without an eval is a demo. This is what turns "the model reads tax
returns" into a claim with a number attached, and it is deliberately harsher
than a single accuracy figure in four ways.

**Per field, not per document.** A document-level score hides that the fields
are not equally consequential. Getting `date_placed_in_service` wrong breaks
the whole depreciation schedule and therefore the exit tax; getting `insurance`
wrong moves cash flow slightly. They are reported separately and weighted.

**Precision separated from recall.** In this domain they are not
interchangeable. A missing field routes to a human and costs a few minutes. A
*wrong* field flows silently into a recommendation an advisor defends with
their license. The pipeline is tuned for precision and the eval reports both.

**Calibration, not just accuracy.** A model that says 0.95 should be right 95%
of the time. If it is not, the auto-accept threshold in `provenance.py` is
meaningless - it would be gating on a number that does not mean what it says.
Reported as a reliability table plus expected calibration error.

**An operating point, chosen rather than picked.** The auto-accept threshold is
read off the precision/review-burden curve at a target precision, and the
report says what fraction of fields that leaves for a human.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "packages" / "pipeline"))
sys.path.insert(0, str(ROOT / "packages" / "engine"))

from sbpipeline.cassettes import Cassette  # noqa: E402
from sbpipeline.corpus import GOLDEN, build_corpus  # noqa: E402
from sbpipeline.extract import Extractor  # noqa: E402
from sbpipeline.schemas import FIELD_SPECS  # noqa: E402

WEIGHTS = {"critical": 4.0, "high": 2.0, "medium": 1.0, "low": 0.5}
TARGET_PRECISION = 0.99


@dataclass
class Judgement:
    document: str
    label: str
    field: str
    expected: Any
    got: Any
    confidence: float
    outcome: str  # correct | wrong | missed | spurious

    @property
    def attempted(self) -> bool:
        return self.outcome in ("correct", "wrong", "spurious")


def _norm_label(s: str) -> str:
    """Address normalisation, for matching an extracted property to truth.

    The same property is written three ways across the corpus on purpose.
    Matching has to survive that without being so loose it merges two
    genuinely different properties.
    """
    s = s.lower()
    s = re.sub(r"[.,]", " ", s)
    for long, short in (("boulevard", "blvd"), ("blv", "blvd"), ("avenue", "ave"),
                        ("street", "st"), ("california", "ca"), ("texas", "tx")):
        s = re.sub(rf"\b{long}\b", short, s)
    return re.sub(r"\s+", " ", s).strip()


def _month_year(raw: Any) -> tuple[int, int] | None:
    """Parse the month and year out of whatever a date arrived as.

    Forms write "04/2021"; the schema asks for ISO. Scoring these as different
    values would be testing the string format rather than whether the pipeline
    read the right month - and the month is all that matters, because the
    mid-month convention only needs the month.
    """
    text = str(raw).strip()
    for pattern, order in (
        (r"^(\d{4})-(\d{1,2})", "ym"),
        (r"^(\d{1,2})/(\d{4})$", "my"),
        (r"^(\d{1,2})/\d{1,2}/(\d{4})$", "my"),
        (r"^(\d{1,2})-(\d{4})$", "my"),
    ):
        m = re.match(pattern, text)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            return (a, b) if order == "ym" else (b, a)
    return None


def _match(expected: Any, got: Any, kind: str) -> bool:
    if got is None or expected is None:
        return False
    if kind == "text":
        return _norm_label(str(expected)) == _norm_label(str(got))
    if kind == "date":
        e, g = _month_year(expected), _month_year(got)
        return e is not None and e == g
    if kind == "number":
        return abs(float(expected) - float(got)) < 0.01
    # money: exact to the dollar. No tolerance - the engine is exact, and a
    # tolerance here would hide the errors that matter most.
    return int(expected) == int(got)


def judge(results: list, golden: dict) -> list[Judgement]:
    out: list[Judgement] = []
    spec_by_name = {s.name: s for s in FIELD_SPECS}

    # Index the ground truth by (document, normalised label).
    truth_index = {
        (doc_id, _norm_label(label)): v
        for (doc_id, label), v in golden.items()
    }
    seen: set[tuple[str, str]] = set()

    for result in results:
        for prop in result.properties:
            key = (result.document_id, _norm_label(prop.property_label))
            truth = truth_index.get(key)
            if truth is None:
                out.append(Judgement(result.document_id, prop.property_label,
                                     "property_label", None, prop.property_label,
                                     0.0, "spurious"))
                continue
            seen.add(key)
            for name, spec in spec_by_name.items():
                expected = truth.get(name)
                got = getattr(prop, name, None)
                if expected is None and got is None:
                    continue
                if expected is None:
                    out.append(Judgement(result.document_id, prop.property_label,
                                         name, None, got, 0.0, "spurious"))
                    continue
                ev = result.evidence_for(prop.property_label, name)
                conf = ev.confidence if ev else 0.5
                if got is None:
                    out.append(Judgement(result.document_id, prop.property_label,
                                         name, expected, None, conf, "missed"))
                else:
                    ok = _match(expected, got, spec.kind)
                    out.append(Judgement(result.document_id, prop.property_label,
                                         name, expected, got, conf,
                                         "correct" if ok else "wrong"))

    # Properties in the truth the pipeline never produced at all.
    for key, truth in truth_index.items():
        if key in seen:
            continue
        for name in spec_by_name:
            if truth.get(name) is not None:
                out.append(Judgement(key[0], truth["property_label"], name,
                                     truth[name], None, 0.0, "missed"))
    return out


def per_field(judgements: list[Judgement]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for spec in FIELD_SPECS:
        js = [j for j in judgements if j.field == spec.name]
        correct = sum(1 for j in js if j.outcome == "correct")
        wrong = sum(1 for j in js if j.outcome in ("wrong", "spurious"))
        missed = sum(1 for j in js if j.outcome == "missed")
        attempted = correct + wrong
        supported = correct + wrong + missed
        stats[spec.name] = {
            "weight": spec.weight,
            "n": supported,
            "correct": correct, "wrong": wrong, "missed": missed,
            "precision": (correct / attempted) if attempted else None,
            "recall": (correct / supported) if supported else None,
            "why": spec.why,
        }
    return stats


def calibration(judgements: list[Judgement], bins: int = 5) -> tuple[list[dict], float]:
    """Reliability table and expected calibration error."""
    attempted = [j for j in judgements if j.attempted]
    rows: list[dict] = []
    ece = 0.0
    total = len(attempted) or 1
    edges = [0.0, 0.5, 0.7, 0.85, 0.95, 1.01]
    # Pairwise over the bin edges: zip the list against its own tail.
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        bucket = [j for j in attempted if lo <= j.confidence < hi]
        if not bucket:
            continue
        acc = sum(1 for j in bucket if j.outcome == "correct") / len(bucket)
        conf = sum(j.confidence for j in bucket) / len(bucket)
        ece += (len(bucket) / total) * abs(acc - conf)
        rows.append({"range": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": len(bucket),
                     "mean_confidence": conf, "accuracy": acc, "gap": acc - conf})
    return rows, ece


def operating_points(judgements: list[Judgement]) -> list[dict]:
    """Auto-accept threshold vs precision vs how much a human still reviews."""
    attempted = [j for j in judgements if j.attempted]
    out = []
    for threshold in (0.0, 0.50, 0.70, 0.80, 0.85, 0.90, 0.95, 0.98):
        auto = [j for j in attempted if j.confidence >= threshold]
        if not auto:
            out.append({"threshold": threshold, "auto_accepted": 0,
                        "precision": None, "review_share": 1.0})
            continue
        correct = sum(1 for j in auto if j.outcome == "correct")
        out.append({
            "threshold": threshold,
            "auto_accepted": len(auto),
            "precision": correct / len(auto),
            "review_share": 1 - len(auto) / len(attempted),
        })
    return out


def run(live: bool, record: bool, limit: int | None = None) -> dict:
    docs = build_corpus()
    if limit:
        docs = docs[:limit]
    cassette = Cassette(record=record)
    extractor = Extractor(live=live, record=record, cassette=cassette)

    results, errors = [], []
    started = time.perf_counter()
    for doc in docs:
        r = extractor.extract(doc)
        if r.error:
            errors.append((doc.id, r.error))
        results.append(r)
    wall = time.perf_counter() - started

    judgements = judge(results, GOLDEN())
    fields = per_field(judgements)
    rows, ece = calibration(judgements)
    ops = operating_points(judgements)

    chosen = next(
        (o for o in ops
         if o["precision"] is not None and o["precision"] >= TARGET_PRECISION),
        None)

    weighted_num = weighted_den = 0.0
    for s in fields.values():
        if s["recall"] is None:
            continue
        w = WEIGHTS[s["weight"]]
        weighted_num += w * s["recall"] * s["n"]
        weighted_den += w * s["n"]

    return {
        "documents": len(docs),
        "pages": sum(d.page_count for d in docs),
        "errors": errors,
        "cassette": cassette.stats(),
        "wall_seconds": wall,
        "latency_ms_per_doc": [round(r.latency_ms) for r in results],
        "cost_usd": sum(r.cost_usd for r in results),
        "input_tokens": sum(r.input_tokens for r in results),
        "output_tokens": sum(r.output_tokens for r in results),
        "cache_read_tokens": sum(r.cache_read_tokens for r in results),
        "fields": fields,
        "calibration": rows,
        "ece": ece,
        "operating_points": ops,
        "chosen_threshold": chosen,
        "weighted_recall": (weighted_num / weighted_den) if weighted_den else None,
        "judgements": len(judgements),
        "evidence_spans": sum(len(r.evidence) for r in results),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the extraction eval.")
    ap.add_argument("--live", action="store_true",
                    help="call the API (requires ANTHROPIC_API_KEY)")
    ap.add_argument("--record", action="store_true", help="write cassettes")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = run(live=args.live, record=args.record, limit=args.limit)
    if args.json:
        print(json.dumps(report, indent=1, default=str))
        return 0

    if report["errors"]:
        print(f"{len(report['errors'])} documents could not be extracted:")
        for doc, err in report["errors"][:3]:
            print(f"  {doc}: {err}")
        print()

    print(f"documents {report['documents']} · pages {report['pages']} · "
          f"{report['cassette']}")
    print(f"evidence spans {report['evidence_spans']} · "
          f"judgements {report['judgements']}")
    print(f"cost ${report['cost_usd']:.4f} · "
          f"{report['input_tokens']:,} in / {report['output_tokens']:,} out tokens")
    print()
    print(f"{'field':<26}{'weight':<10}{'n':>4}{'prec':>8}{'recall':>8}"
          f"{'wrong':>7}{'missed':>8}")
    for name, s in report["fields"].items():
        p = f"{s['precision']:.3f}" if s["precision"] is not None else "  -  "
        r = f"{s['recall']:.3f}" if s["recall"] is not None else "  -  "
        print(f"{name:<26}{s['weight']:<10}{s['n']:>4}{p:>8}{r:>8}"
              f"{s['wrong']:>7}{s['missed']:>8}")
    print()
    print(f"weighted recall (critical fields x4): "
          f"{report['weighted_recall']:.3f}" if report["weighted_recall"]
          else "weighted recall: n/a")
    print(f"expected calibration error: {report['ece']:.3f}")
    if report["chosen_threshold"]:
        c = report["chosen_threshold"]
        print(f"operating point for >={TARGET_PRECISION:.0%} precision: "
              f"threshold {c['threshold']:.2f}, "
              f"{c['review_share']:.0%} of fields routed to review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
