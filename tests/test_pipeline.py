"""The document pipeline: corpus, reconciliation, and the cassette layer.

These run offline. The extraction eval itself needs either recorded cassettes
or a live key; see tests/test_extraction_eval.py.
"""
from sbpipeline.cassettes import Cassette
from sbpipeline.corpus import GOLDEN, build_corpus
from sbpipeline.corpus.generate import LONG_BEACH_ALIASES
from sbpipeline.reconcile import normalize_address, reconcile
from sbpipeline.schemas import FIELD_SPECS, PropertyFacts


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------
def test_corpus_is_deterministic():
    """Eval numbers must move only when the pipeline moves, never because the
    corpus regenerated differently."""
    a, b = build_corpus(), build_corpus()
    assert [d.pages for d in a] == [d.pages for d in b]
    assert GOLDEN() == GOLDEN()


def test_corpus_carries_its_own_ground_truth():
    golden = GOLDEN()
    assert len(golden) >= 10
    values = sum(len(v) - 2 for v in golden.values())  # minus label and year
    assert values >= 50, "too few labelled field values to measure anything"


def test_the_2023_return_is_deliberately_missing():
    """Clients lose returns. An eval built only on complete records does not
    test the case that actually shows up."""
    years = {d.tax_year for d in build_corpus() if d.kind == "schedule_e"}
    assert years == {2021, 2022, 2024}
    assert 2023 not in years


def test_one_document_is_a_scan_with_ocr_artefacts():
    scanned = [d for d in build_corpus() if d.note and "OCR" in d.note]
    assert len(scanned) == 1
    clean = next(d for d in build_corpus() if d.tax_year == 2022 and d.kind == "schedule_e")
    assert scanned[0].pages[0] != clean.pages[0]


def test_documents_look_like_the_real_form():
    """Line numbers are the extractor's strongest signal. An eval built on
    prose rather than form layout would not test that."""
    sch_e = next(d for d in build_corpus() if d.kind == "schedule_e")
    page = sch_e.pages[0]
    for marker in ("SCHEDULE E", "Rents received", "Depreciation expense or depletion",
                   "Total expenses. Add lines 5 through 19", "Properties:"):
        assert marker in page, marker


def test_the_disputed_depreciation_is_actually_inconsistent():
    """The corpus's central case: two filed years implying different land
    allocations. If these ever agree, the demo's refusal is fiction."""
    golden = GOLDEN()
    by_year = {}
    for (_doc, label), truth in golden.items():
        if "ocean" in label.lower() and "depreciation" in truth:
            by_year[truth["tax_year"]] = truth["depreciation"]
    assert by_year[2022] != by_year[2024]
    assert by_year[2022] == 28_509 and by_year[2024] == 32_582


# --------------------------------------------------------------------------
# Entity resolution
# --------------------------------------------------------------------------
def test_the_three_aliases_normalize_to_one_property():
    keys = {normalize_address(a) for a in LONG_BEACH_ALIASES.values()}
    assert len(keys) == 1, f"aliases did not collapse: {keys}"


def test_normalization_does_not_merge_different_properties():
    """A false merge silently combines two basis histories, which is far worse
    than a false split."""
    a = normalize_address("1247 Ocean Blvd, Long Beach, CA 90802")
    b = normalize_address("1245 Ocean Blvd, Long Beach, CA 90802")
    c = normalize_address("1247 Ocean Blvd, Long Beach, CA 90803")
    assert a != b, "different street numbers merged"
    assert a != c, "different ZIPs merged"


class _Result:
    def __init__(self, document_id, properties):
        self.document_id, self.properties = document_id, properties

    def evidence_for(self, label, field):
        return None


def _long_beach_years():
    return [
        _Result("2021-sch-e", [PropertyFacts(
            property_label=LONG_BEACH_ALIASES[2021], tax_year=2021,
            depreciation=28_509, cost_or_basis=784_000)]),
        _Result("2022-sch-e", [PropertyFacts(
            property_label=LONG_BEACH_ALIASES[2022], tax_year=2022,
            depreciation=28_509, cost_or_basis=784_000)]),
        _Result("2024-sch-e", [PropertyFacts(
            property_label=LONG_BEACH_ALIASES[2024], tax_year=2024,
            depreciation=32_582, cost_or_basis=896_000)]),
    ]


def test_reconciliation_groups_the_years_into_one_property():
    props = reconcile(_long_beach_years())
    assert len(props) == 1
    assert props[0].years == [2021, 2022, 2024]
    assert len(props[0].labels) == 3


def test_reconciliation_reports_the_missing_year():
    assert reconcile(_long_beach_years())[0].missing_years() == [2023]


def test_a_changed_basis_is_a_conflict_not_a_timeline():
    """Operating figures move year to year. Basis does not."""
    conflicts = {c.field for c in reconcile(_long_beach_years())[0].conflicts}
    assert "cost_or_basis" in conflicts
    assert "depreciation" in conflicts


def test_reconciliation_never_averages():
    """The load-bearing behaviour. Two filed returns implying 30% and 20% land
    do not average to 25% - one of them is right and the owner knows which."""
    prop = reconcile(_long_beach_years())[0]
    series = dict(prop.series("depreciation"))
    assert series[2022] == 28_509
    assert series[2024] == 32_582
    assert 30_545 not in series.values(), "a mean appeared in the timeline"
    assert prop.has_blocking_conflict()

    import inspect

    import sbpipeline.reconcile as module
    source = inspect.getsource(module)
    for smell in ("mean(", "statistics.", "/ len(", "sum(values)"):
        assert smell not in source, f"an averaging path appeared: {smell}"


def test_operating_figures_alone_are_not_a_conflict():
    results = [
        _Result("2022", [PropertyFacts(property_label="1 A St, Austin, TX 78756",
                                       tax_year=2022, rents_received=40_000)]),
        _Result("2024", [PropertyFacts(property_label="1 A St, Austin, TX 78756",
                                       tax_year=2024, rents_received=44_000)]),
    ]
    prop = reconcile(results)[0]
    assert not prop.conflicts, "rent growth was reported as a conflict"
    assert prop.series("rents_received") == [(2022, 40_000), (2024, 44_000)]


# --------------------------------------------------------------------------
# Scoring and cassettes
# --------------------------------------------------------------------------
def test_fields_are_weighted_by_consequence():
    """A document-level accuracy number would hide that these differ wildly in
    what they break."""
    weights = {s.name: s.weight for s in FIELD_SPECS}
    assert weights["date_placed_in_service"] == "critical"
    assert weights["cost_or_basis"] == "critical"
    assert weights["insurance"] == "low"
    assert all(s.why for s in FIELD_SPECS), "every field must justify its weight"


def test_cassette_key_changes_when_the_request_changes(tmp_path):
    """A cassette that served a stale response for a changed prompt would make
    the eval a lie."""
    c = Cassette(tmp_path)
    base = {"model": "claude-opus-5", "messages": [{"role": "user", "content": "a"}]}
    changed_prompt = {**base, "messages": [{"role": "user", "content": "b"}]}
    changed_model = {**base, "model": "claude-haiku-4-5"}
    assert c.key(base) != c.key(changed_prompt)
    assert c.key(base) != c.key(changed_model)
    assert c.key(base) == c.key(dict(base))


def test_cassette_round_trips(tmp_path):
    c = Cassette(tmp_path)
    req = {"model": "claude-opus-5", "_pass": "evidence", "_document": "d1"}
    assert c.get(req) is None
    c.put(req, {"content": [{"type": "text", "text": "hello"}]})
    assert c.get(req)["content"][0]["text"] == "hello"


# --------------------------------------------------------------------------
# The eval itself, replayed offline
# --------------------------------------------------------------------------
def test_the_eval_replays_entirely_from_cassettes():
    """No key, no network, and every call must hit a recording.

    A miss here means the corpus or a prompt changed without the cassettes
    being re-recorded - at which point docs/EVALS.md is reporting numbers for
    a pipeline that no longer exists.
    """
    from sbpipeline.evals.run import run

    report = run(live=False, record=False)
    assert not report["errors"], f"cassette misses: {report['errors'][:2]}"
    assert report["cassette"].startswith("10/10"), report["cassette"]
    assert report["evidence_spans"] > 50


def test_extraction_holds_its_measured_quality():
    """A regression gate on the numbers in docs/EVALS.md.

    Deliberately a floor rather than an equality: the point is to catch a
    regression, not to pin the pipeline to one run.
    """
    from sbpipeline.evals.run import run

    report = run(live=False, record=False)
    assert report["weighted_recall"] >= 0.95, report["weighted_recall"]
    for name, s in report["fields"].items():
        if s["weight"] == "critical" and s["precision"] is not None:
            assert s["precision"] >= 0.95, f"{name} precision {s['precision']}"


def test_the_degraded_scan_did_not_silently_lose_a_corrupted_figure():
    """On the 2021 scan, $18,615 of mortgage interest is corrupted to
    'l8,615'. The pipeline recovers it from the form's arithmetic. If that ever
    regresses to a confident wrong number, this fails."""
    from sbpipeline.evals.run import judge
    from sbpipeline.extract import Extractor

    scan = next(d for d in build_corpus()
                if d.tax_year == 2021 and d.kind == "schedule_e")
    assert "l8,615" in scan.pages[0], "the corpus no longer corrupts that figure"

    ex = Extractor(live=False)
    judged = judge([ex.extract(scan)], GOLDEN())
    interest = [j for j in judged if j.field == "mortgage_interest"]
    assert interest, "mortgage interest was not scored at all"
    assert all(j.outcome != "wrong" for j in interest), (
        "a corrupted figure was reported as a confident wrong value: "
        f"{[(j.expected, j.got) for j in interest if j.outcome == 'wrong']}"
    )
