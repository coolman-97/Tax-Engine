"""Where every number came from, and whether anyone stood behind it.

An advisor sits across from a client and says what to do with the largest
asset that client owns. They defend that number out loud, with their license
behind it. So the engine treats "where did this come from" as a load-bearing
property of the number itself, not as logging.

Every input is a :class:`Fact`: a value plus a :class:`Provenance`. There are
four kinds of provenance and they are not equally defensible:

===============  =========================================================
``Assumed``      The engine picked a default. Nobody stood behind it.
``Extracted``    A model read it off a document. Carries the page, the
                 quoted text and a calibrated confidence.
``Attested``     A named human said "this is right", at a recorded time.
``Derived``      Computed from other facts by a named, versioned rule.
===============  =========================================================

The rule that makes this useful is one line in :meth:`Derived.standing`:

    **A derived number is exactly as defensible as its weakest input.**

That single propagation rule is what lets the engine refuse. Ask it for an
exit-tax recommendation whose basis traces back to an assumed land allocation
and it will not hand you a number with a caveat attached - it raises
:class:`Unattested`, and :func:`blocking_facts` tells you the shortest list of
questions that would unblock it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import IntEnum
from typing import Generic, TypeVar

__all__ = [
    "Standing",
    "Assumed",
    "Extracted",
    "Attested",
    "Derived",
    "Provenance",
    "Fact",
    "Unattested",
    "blocking_facts",
    "walk",
    "explain",
    "AUTO_ACCEPT_CONFIDENCE",
]

T = TypeVar("T")

#: Confidence at or above which an extracted value is treated as
#: policy-acceptable without a human touching it. This is not a guess - it is
#: the operating point chosen from the calibration curve in
#: ``packages/pipeline/sbpipeline/evals``, picked to hold field-level precision
#: at or above 0.99 on the golden set. Change it there, not here.
AUTO_ACCEPT_CONFIDENCE = Decimal("0.90")


class Standing(IntEnum):
    """How well a number would survive being questioned. Ordered, low to high."""

    ASSUMED = 0
    """A default the engine chose. Fine for illustration, never for advice."""

    EXTRACTED_LOW = 1
    """A model read it, below the auto-accept threshold. Needs human review."""

    EXTRACTED_HIGH = 2
    """A model read it, at or above the calibrated threshold. Policy-acceptable."""

    ATTESTED = 3
    """A named human stood behind it at a recorded moment."""

    @property
    def label(self) -> str:
        return {
            Standing.ASSUMED: "assumed",
            Standing.EXTRACTED_LOW: "needs review",
            Standing.EXTRACTED_HIGH: "read from document",
            Standing.ATTESTED: "attested",
        }[self]


# ----------------------------------------------------------------------
# The four kinds of provenance
# ----------------------------------------------------------------------
@dataclass(frozen=True, eq=True)
class Assumed:
    """A default the engine supplied because nothing better was available."""

    basis: str
    """Why this default is defensible as a default, e.g. 'county assessor
    median land ratio for zip 90803'."""

    set_by: str = "engine"

    @property
    def standing(self) -> Standing:
        return Standing.ASSUMED

    def describe(self) -> str:
        return f"assumed ({self.basis})"


@dataclass(frozen=True, eq=True)
class Extracted:
    """A value a model read off a document, with the receipt attached."""

    document_id: str
    page: int
    cited_text: str
    """The exact span the model quoted. Not a paraphrase - this is what makes
    the extraction checkable by a human in two seconds."""

    confidence: Decimal
    extractor: str
    """Model id plus pipeline version, e.g. 'claude-opus-5/evidence@3'."""

    bbox: tuple | None = None

    @property
    def standing(self) -> Standing:
        return (
            Standing.EXTRACTED_HIGH
            if self.confidence >= AUTO_ACCEPT_CONFIDENCE
            else Standing.EXTRACTED_LOW
        )

    def describe(self) -> str:
        return (
            f"read from {self.document_id} p.{self.page} "
            f"(confidence {self.confidence:.2f}): {self.cited_text!r}"
        )


@dataclass(frozen=True, eq=True)
class Attested:
    """A named human said this is right, and when."""

    by: str
    at: datetime
    note: str = ""
    supersedes: str | None = None
    """Fact id this attestation overrode, if it resolved a conflict."""

    @property
    def standing(self) -> Standing:
        return Standing.ATTESTED

    def describe(self) -> str:
        stamp = self.at.isoformat(timespec="seconds")
        tail = f": {self.note}" if self.note else ""
        return f"attested by {self.by} at {stamp}{tail}"


@dataclass(frozen=True, eq=False)
class Derived:
    """Computed from other facts by a named, versioned rule."""

    rule_id: str
    """Stable identifier for the rule that produced this, e.g.
    'irc-168-macrs-residential@2026.1'. Versioned, because the answer to
    'why is this number different from last quarter' is usually 'the rule
    changed', and that should be answerable."""

    inputs: tuple[Fact, ...]
    explanation: str = ""

    @property
    def standing(self) -> Standing:
        """A derived number is exactly as defensible as its weakest input.

        This is the whole mechanism. Nothing else in the engine needs to know
        about defensibility - it propagates from the leaves for free.
        """
        if not self.inputs:
            # A rule with no inputs is a constant from the rule data, which the
            # firm adopted deliberately. That counts as standing behind it.
            return Standing.ATTESTED
        return min(fact.standing for fact in self.inputs)

    def describe(self) -> str:
        return self.explanation or f"derived by {self.rule_id}"


Provenance = Assumed | Extracted | Attested | Derived


# ----------------------------------------------------------------------
# Facts
# ----------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class Fact(Generic[T]):
    """A value that knows where it came from."""

    name: str
    """Dotted field path, e.g. 'pasadena_duplex.land_allocation'."""

    value: T
    provenance: Provenance
    unit: str = ""
    as_of: date | None = None

    # ---------------- constructors ----------------
    @classmethod
    def attested(
        cls,
        name: str,
        value: T,
        *,
        by: str,
        at: datetime | None = None,
        note: str = "",
        supersedes: str | None = None,
        unit: str = "",
        as_of: date | None = None,
    ) -> Fact[T]:
        return cls(
            name,
            value,
            Attested(by=by, at=at or datetime.now(), note=note, supersedes=supersedes),
            unit,
            as_of,
        )

    @classmethod
    def extracted(
        cls,
        name: str,
        value: T,
        *,
        document_id: str,
        page: int,
        cited_text: str,
        confidence: str | Decimal,
        extractor: str,
        bbox: tuple | None = None,
        unit: str = "",
        as_of: date | None = None,
    ) -> Fact[T]:
        return cls(
            name,
            value,
            Extracted(
                document_id=document_id,
                page=page,
                cited_text=cited_text,
                confidence=Decimal(confidence),
                extractor=extractor,
                bbox=bbox,
            ),
            unit,
            as_of,
        )

    @classmethod
    def assumed(
        cls,
        name: str,
        value: T,
        *,
        basis: str,
        set_by: str = "engine",
        unit: str = "",
        as_of: date | None = None,
    ) -> Fact[T]:
        return cls(name, value, Assumed(basis=basis, set_by=set_by), unit, as_of)

    @classmethod
    def derived(
        cls,
        name: str,
        value: T,
        *,
        rule_id: str,
        inputs: Sequence[Fact] = (),
        explanation: str = "",
        unit: str = "",
        as_of: date | None = None,
    ) -> Fact[T]:
        return cls(
            name,
            value,
            Derived(rule_id=rule_id, inputs=tuple(inputs), explanation=explanation),
            unit,
            as_of,
        )

    # ---------------- properties ----------------
    @property
    def standing(self) -> Standing:
        return self.provenance.standing

    @property
    def id(self) -> str:
        """Stable content-addressed id, so the UI can key on it and a replay
        of the same scenario produces the same node ids."""
        digest = hashlib.blake2b(digest_size=8)
        digest.update(self.name.encode())
        digest.update(repr(self.value).encode())
        digest.update(type(self.provenance).__name__.encode())
        if isinstance(self.provenance, Derived):
            digest.update(self.provenance.rule_id.encode())
            for child in self.provenance.inputs:
                digest.update(child.id.encode())
        else:
            digest.update(self.provenance.describe().encode())
        return digest.hexdigest()

    @property
    def is_leaf(self) -> bool:
        return not isinstance(self.provenance, Derived)

    def require(self, minimum: Standing = Standing.EXTRACTED_HIGH) -> T:
        """Return the value, or refuse.

        This is the gate. Call it anywhere a number is about to become advice.
        """
        if self.standing < minimum:
            raise Unattested(self, minimum)
        return self.value

    def __repr__(self) -> str:
        return f"Fact({self.name}={self.value!r}, {self.standing.label})"


class Unattested(Exception):
    """Raised when a number would become advice without anyone behind it.

    Carries the blocking leaves, so the caller can turn the refusal into a
    question list rather than an error message.
    """

    def __init__(self, fact: Fact, minimum: Standing) -> None:
        self.fact = fact
        self.minimum = minimum
        self.blocking = blocking_facts(fact, minimum)
        names = ", ".join(sorted({b.name for b in self.blocking})) or fact.name
        super().__init__(
            f"{fact.name} is {fact.standing.label}, below the {minimum.label} bar "
            f"required to recommend. Unblocked by: {names}"
        )


# ----------------------------------------------------------------------
# Graph walking
# ----------------------------------------------------------------------
def walk(fact: Fact) -> Iterator[Fact]:
    """Yield ``fact`` and every fact it was derived from, depth first."""
    yield fact
    if isinstance(fact.provenance, Derived):
        for child in fact.provenance.inputs:
            yield from walk(child)


def blocking_facts(
    fact: Fact, minimum: Standing = Standing.EXTRACTED_HIGH
) -> tuple[Fact, ...]:
    """The leaf facts that hold ``fact`` below ``minimum``.

    Deduplicated by name and ordered by standing then name, so the question
    list an advisor sees is short, stable, and worst-first.
    """
    found: dict[str, Fact] = {}
    for node in walk(fact):
        if node.is_leaf and node.standing < minimum:
            existing = found.get(node.name)
            if existing is None or node.standing < existing.standing:
                found[node.name] = node
    return tuple(sorted(found.values(), key=lambda f: (f.standing, f.name)))


def explain(fact: Fact, *, indent: int = 0, _seen: set | None = None) -> str:
    """Render the derivation as a readable tree.

    This is what the "show your work" drawer prints, and what a reviewer reads
    when they want to know whether the engine is bluffing.
    """
    _seen = _seen if _seen is not None else set()
    pad = "  " * indent
    marker = {
        Standing.ATTESTED: "+",
        Standing.EXTRACTED_HIGH: ".",
        Standing.EXTRACTED_LOW: "?",
        Standing.ASSUMED: "!",
    }[fact.standing]
    unit = f" {fact.unit}" if fact.unit else ""
    lines = [f"{pad}{marker} {fact.name} = {fact.value}{unit}"]
    lines.append(f"{pad}    {fact.provenance.describe()}")
    if isinstance(fact.provenance, Derived):
        if fact.id in _seen:
            lines.append(f"{pad}    (subtree shown above)")
            return "\n".join(lines)
        _seen.add(fact.id)
        for child in fact.provenance.inputs:
            lines.append(explain(child, indent=indent + 1, _seen=_seen))
    return "\n".join(lines)
