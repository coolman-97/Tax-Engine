"""The refusal mechanic. If this is wrong, the whole premise is wrong."""
from datetime import datetime
from decimal import Decimal

import pytest
from standbehind.money import Money
from standbehind.provenance import (
    AUTO_ACCEPT_CONFIDENCE,
    Fact,
    Standing,
    Unattested,
    blocking_facts,
    explain,
    walk,
)


def _attested(name="a", value=None):
    value = value if value is not None else Money.from_dollars("100")
    return Fact.attested(name, value, by="advisor:dana", at=datetime(2026, 9, 20))


def _assumed(name="b", value=None):
    value = value if value is not None else Money.from_dollars("50")
    return Fact.assumed(name, value, basis="county median")


def _extracted(name="c", confidence="0.97"):
    return Fact.extracted(
        name, Money.from_dollars("75"), document_id="d1", page=3,
        cited_text="Depreciation 61,200", confidence=confidence, extractor="test",
    )


def test_standing_is_ordered():
    assert Standing.ASSUMED < Standing.EXTRACTED_LOW < Standing.EXTRACTED_HIGH < Standing.ATTESTED


def test_extraction_standing_turns_on_the_calibrated_threshold():
    assert _extracted(confidence=str(AUTO_ACCEPT_CONFIDENCE)).standing is Standing.EXTRACTED_HIGH
    below = Decimal(AUTO_ACCEPT_CONFIDENCE) - Decimal("0.01")
    assert _extracted(confidence=str(below)).standing is Standing.EXTRACTED_LOW


def test_derived_takes_the_weakest_link():
    """The entire mechanism is this one rule."""
    derived = Fact.derived(
        "basis", Money.from_dollars("225"), rule_id="irc-1016",
        inputs=[_attested(), _assumed(), _extracted()],
    )
    assert derived.standing is Standing.ASSUMED


def test_derived_from_all_attested_is_attested():
    derived = Fact.derived(
        "basis", Money.from_dollars("200"), rule_id="irc-1016",
        inputs=[_attested("a"), _attested("b")],
    )
    assert derived.standing is Standing.ATTESTED
    assert derived.require(Standing.ATTESTED) == Money.from_dollars("200")


def test_weakest_link_propagates_through_depth():
    deep = _assumed("root")
    for i in range(6):
        deep = Fact.derived(f"level{i}", Money.from_dollars("1"), rule_id="r", inputs=[deep, _attested()])
    assert deep.standing is Standing.ASSUMED
    assert [f.name for f in blocking_facts(deep)] == ["root"]


def test_require_refuses_and_names_the_questions():
    derived = Fact.derived(
        "exit_tax", Money.from_dollars("47310"), rule_id="irc-1250",
        inputs=[_attested(), _assumed("land_allocation"), _extracted("depr", "0.72")],
    )
    with pytest.raises(Unattested) as caught:
        derived.require()
    names = {f.name for f in caught.value.blocking}
    assert names == {"land_allocation", "depr"}
    assert "land_allocation" in str(caught.value)


def test_blocking_facts_are_deduplicated_and_worst_first():
    shared = _assumed("shared")
    derived = Fact.derived(
        "top", Money.from_dollars("1"), rule_id="r",
        inputs=[shared, shared, _extracted("mid", "0.5"), _attested("fine")],
    )
    blocking = blocking_facts(derived)
    assert [f.name for f in blocking] == ["shared", "mid"]
    assert blocking[0].standing < blocking[1].standing


def test_a_constant_from_the_rule_data_counts_as_attested():
    """A rule with no inputs is a figure the firm adopted deliberately."""
    assert Fact.derived("rate", Decimal("0.25"), rule_id="irc-1h1E").standing is Standing.ATTESTED


def test_fact_ids_are_stable_and_content_addressed():
    a = Fact.derived("x", Money.from_dollars("1"), rule_id="r", inputs=[_attested()])
    b = Fact.derived("x", Money.from_dollars("1"), rule_id="r", inputs=[_attested()])
    assert a.id == b.id
    c = Fact.derived("x", Money.from_dollars("2"), rule_id="r", inputs=[_attested()])
    assert a.id != c.id


def test_walk_visits_the_whole_tree():
    derived = Fact.derived("top", Money.from_dollars("1"), rule_id="r",
                           inputs=[_attested("a"), _assumed("b")])
    assert {f.name for f in walk(derived)} == {"top", "a", "b"}


def test_explain_shows_the_receipts():
    derived = Fact.derived("basis", Money.from_dollars("225"), rule_id="irc-1016",
                           inputs=[_attested("price"), _extracted("depr")])
    text = explain(derived)
    assert "irc-1016" in text or "basis" in text
    assert "attested by advisor:dana" in text
    assert "Depreciation 61,200" in text, "the cited span must be visible to a human"
    assert "p.3" in text
