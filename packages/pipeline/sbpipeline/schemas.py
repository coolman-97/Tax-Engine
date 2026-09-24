"""What the pipeline is allowed to extract, and what it must carry with it.

Every extracted field arrives with the span of document text that supports it.
A value with no supporting quote is not an extraction, it is a guess, and the
schema makes that impossible to express.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Evidence",
    "PropertyFacts",
    "ScheduleEExtraction",
    "Form4562Extraction",
    "FIELD_SPECS",
    "FieldSpec",
]


class Evidence(BaseModel):
    """A quoted span, and where it came from."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(description="Dotted field name this span supports.")
    quote: str = Field(
        description="The exact text from the document, copied verbatim. Never "
                    "paraphrased, never reformatted, never a computed value."
    )
    page: int = Field(description="1-indexed page the quote appears on.")
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="How confident you are that this span means this field. "
                    "Use the full range: 0.5 when the label is ambiguous, 0.99 "
                    "when the form line number is explicit.",
    )


class PropertyFacts(BaseModel):
    """The structured result of pass B.

    Note what is absent: there is no field for a value the model computed.
    Every number here is transcribed from a quote pass A already captured.
    """

    model_config = ConfigDict(extra="forbid")

    property_label: str = Field(
        description="The property address or label exactly as written on the form."
    )
    tax_year: int
    rents_received: int | None = Field(
        default=None, description="Whole dollars. Schedule E line 3."
    )
    total_expenses: int | None = Field(
        default=None, description="Whole dollars. Schedule E line 20."
    )
    mortgage_interest: int | None = Field(
        default=None, description="Whole dollars. Schedule E line 12."
    )
    depreciation: int | None = Field(
        default=None, description="Whole dollars. Schedule E line 18."
    )
    insurance: int | None = Field(
        default=None, description="Whole dollars. Schedule E line 9."
    )
    property_taxes: int | None = Field(
        default=None, description="Whole dollars. Schedule E line 16."
    )
    date_placed_in_service: str | None = Field(
        default=None, description="ISO date, from Form 4562 if present."
    )
    cost_or_basis: int | None = Field(
        default=None, description="Whole dollars. Form 4562 cost or other basis."
    )
    recovery_period_years: float | None = Field(
        default=None, description="e.g. 27.5 or 39."
    )


class ScheduleEExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    properties: list[PropertyFacts]


class Form4562Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    properties: list[PropertyFacts]


class FieldSpec:
    """How a field is scored, because they are not equally important."""

    def __init__(self, name: str, kind: str, weight: str, why: str) -> None:
        self.name, self.kind, self.weight, self.why = name, kind, weight, why


#: Scoring is per field, not per document. A document-level accuracy number
#: hides the fact that the fields differ wildly in consequence: getting
#: `date_placed_in_service` wrong breaks the entire depreciation schedule and
#: therefore the exit tax; getting `insurance` wrong moves cash flow slightly.
FIELD_SPECS: list[FieldSpec] = [
    FieldSpec("property_label", "text", "critical",
              "Wrong label means the years never reconcile into one property."),
    FieldSpec("date_placed_in_service", "date", "critical",
              "Sets the entire depreciation schedule and the mid-month convention."),
    FieldSpec("cost_or_basis", "money", "critical",
              "Drives adjusted basis, therefore gain, therefore exit tax."),
    FieldSpec("depreciation", "money", "critical",
              "Cross-checks basis and implies the land allocation."),
    FieldSpec("recovery_period_years", "number", "high",
              "27.5 vs 39 changes the annual deduction by a third."),
    FieldSpec("mortgage_interest", "money", "high",
              "Largest single expense; drives net rental income and passive losses."),
    FieldSpec("rents_received", "money", "high", "Top line of the property."),
    FieldSpec("total_expenses", "money", "medium", "Aggregate; individually checkable."),
    FieldSpec("property_taxes", "money", "medium", "Expense component."),
    FieldSpec("insurance", "money", "low",
              "Expense component; moves cash flow a little."),
]
