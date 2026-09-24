"""A synthetic corpus of tax documents, with ground truth for free.

Hand-labelling an eval set is slow, small, and quietly biased toward the cases
the labeller thought of. Generating the documents instead means the labels are
exact by construction, the set can be as large as you like, and - this is the
part that matters - **the mess can be deliberate**.

Real households do not arrive as clean data. The corpus therefore ships with,
on purpose:

- a **missing tax year** (2023), because clients lose returns;
- the same property **named three different ways** across years, because
  preparers are inconsistent;
- a **depreciation figure that disagrees** between 2022 and 2024, because the
  preparer changed the land allocation and nobody wrote that down;
- **OCR-style noise** on one document, because scanned returns are common;
- a property with **no Form 4562**, so basis has to come from somewhere else.

Every one of those is a case the pipeline has to survive, and none of them is
visible in an eval built from clean documents.

Generation is seeded and deterministic: the same corpus every run, so eval
numbers move only when the pipeline moves.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

__all__ = ["Document", "build_corpus", "GOLDEN"]


@dataclass(frozen=True)
class Document:
    """One document, as pages, plus the truth about what is in it."""

    id: str
    kind: str
    tax_year: int
    pages: tuple[str, ...]
    truth: tuple[dict[str, Any], ...]
    note: str = ""

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def char_count(self) -> int:
        return sum(len(p) for p in self.pages)


def _money(n: int | None) -> str:
    return f"{n:,}" if n is not None else ""


def _schedule_e_page(
    tax_year: int, taxpayer: str, ssn: str, rows: list[dict], page_no: int
) -> str:
    """Render a Schedule E page in the layout of the real form.

    Line numbers matter: they are the strongest signal the extractor has, and
    an eval built on prose rather than form layout would not test that.
    """
    a = rows[0] if len(rows) > 0 else None
    b = rows[1] if len(rows) > 1 else None
    c = rows[2] if len(rows) > 2 else None

    def cell(row, key):
        return _money(row.get(key)) if row else ""

    head = f"""SCHEDULE E                    Supplemental Income and Loss                OMB No. 1545-0074
(Form 1040)          (From rental real estate, royalties, partnerships,
Department of the             S corporations, estates, trusts, REMICs, etc.)          {tax_year}
Treasury                 Attach to Form 1040, 1040-SR, 1040-NR, or 1041.       Attachment
Internal Revenue Service      Go to www.irs.gov/ScheduleE for instructions.     Sequence No. 13

Name(s) shown on return                                        Your social security number
{taxpayer:<62}{ssn}

Part I   Income or Loss From Rental Real Estate and Royalties
         Note: If you are in the business of renting personal property, use Schedule C.

1a  Physical address of each property (street, city, state, ZIP code)
    A  {(a or {}).get('label', '')}
    B  {(b or {}).get('label', '')}
    C  {(c or {}).get('label', '')}

1b  Type of Property        2  For each rental real estate property listed above,   Fair Rental   Personal Use
    (from list below)          report the number of fair rental and personal            Days          Days
    A  {(a or {}).get('type_code', '')}                     use days.                       A   {(a or {}).get('fair_rental_days', '')}           0
    B  {(b or {}).get('type_code', '')}                                                     B   {(b or {}).get('fair_rental_days', '')}           0
    C  {(c or {}).get('type_code', '')}                                                     C   {(c or {}).get('fair_rental_days', '')}           0

Type of Property: 1 Single Family Residence  2 Multi-Family Residence  3 Vacation/Short-Term Rental
                  4 Commercial  5 Land  6 Royalties  7 Self-Rental  8 Other

Income:                                              Properties:      A              B              C
 3   Rents received  . . . . . . . . . . . . . . .  3    {cell(a,'rents'):>12}   {cell(b,'rents'):>12}   {cell(c,'rents'):>12}
 4   Royalties received  . . . . . . . . . . . . .  4               0              0              0

Expenses:
 5   Advertising . . . . . . . . . . . . . . . . .  5    {cell(a,'advertising'):>12}   {cell(b,'advertising'):>12}   {cell(c,'advertising'):>12}
 6   Auto and travel . . . . . . . . . . . . . . .  6    {cell(a,'auto'):>12}   {cell(b,'auto'):>12}   {cell(c,'auto'):>12}
 7   Cleaning and maintenance  . . . . . . . . . .  7    {cell(a,'cleaning'):>12}   {cell(b,'cleaning'):>12}   {cell(c,'cleaning'):>12}
 8   Commissions . . . . . . . . . . . . . . . . .  8    {cell(a,'commissions'):>12}   {cell(b,'commissions'):>12}   {cell(c,'commissions'):>12}
 9   Insurance . . . . . . . . . . . . . . . . . .  9    {cell(a,'insurance'):>12}   {cell(b,'insurance'):>12}   {cell(c,'insurance'):>12}
10   Legal and other professional fees . . . . . . 10    {cell(a,'legal'):>12}   {cell(b,'legal'):>12}   {cell(c,'legal'):>12}
11   Management fees . . . . . . . . . . . . . . . 11    {cell(a,'management'):>12}   {cell(b,'management'):>12}   {cell(c,'management'):>12}
12   Mortgage interest paid to banks, etc.  . . .  12    {cell(a,'mortgage_interest'):>12}   {cell(b,'mortgage_interest'):>12}   {cell(c,'mortgage_interest'):>12}
13   Other interest  . . . . . . . . . . . . . . . 13    {cell(a,'other_interest'):>12}   {cell(b,'other_interest'):>12}   {cell(c,'other_interest'):>12}
14   Repairs . . . . . . . . . . . . . . . . . . . 14    {cell(a,'repairs'):>12}   {cell(b,'repairs'):>12}   {cell(c,'repairs'):>12}
15   Supplies  . . . . . . . . . . . . . . . . . . 15    {cell(a,'supplies'):>12}   {cell(b,'supplies'):>12}   {cell(c,'supplies'):>12}
16   Taxes . . . . . . . . . . . . . . . . . . . . 16    {cell(a,'taxes'):>12}   {cell(b,'taxes'):>12}   {cell(c,'taxes'):>12}
17   Utilities . . . . . . . . . . . . . . . . . . 17    {cell(a,'utilities'):>12}   {cell(b,'utilities'):>12}   {cell(c,'utilities'):>12}
18   Depreciation expense or depletion . . . . . . 18    {cell(a,'depreciation'):>12}   {cell(b,'depreciation'):>12}   {cell(c,'depreciation'):>12}
19   Other (list)  . . . . . . . . . . . . . . . . 19               0              0              0
20   Total expenses. Add lines 5 through 19  . . . 20    {cell(a,'total_expenses'):>12}   {cell(b,'total_expenses'):>12}   {cell(c,'total_expenses'):>12}
21   Subtract line 20 from line 3. If result is a
     (loss), see instructions to find out if you
     must file Form 6198  . . . . . . . . . . . . 21    {cell(a,'net'):>12}   {cell(b,'net'):>12}   {cell(c,'net'):>12}
22   Deductible rental real estate loss after
     limitation, if any, on Form 8582  . . . . . . 22   ({cell(a,'disallowed'):>11})  ({cell(b,'disallowed'):>11})  ({cell(c,'disallowed'):>11})

For Paperwork Reduction Act Notice, see the separate instructions.    Schedule E (Form 1040) {tax_year}
                                                                                    Page {page_no}"""
    return head


def _form_4562_page(tax_year: int, taxpayer: str, rows: list[dict], page_no: int) -> str:
    lines = []
    for r in rows:
        lines.append(
            f"{'  19h' if r.get('recovery') == 27.5 else '  19i'}  Residential rental"
            f"{'':<8}{r.get('placed_in_service', ''):<12}"
            f"{_money(r.get('cost')):>14}{'':<6}{r.get('recovery', ''):>5} yrs"
            f"{'':<4}MM{'':<6}S/L{'':<6}{_money(r.get('deduction')):>12}")
        lines.append(f"       {r.get('label', '')}")
    body = "\n".join(lines)
    return f"""Form 4562                  Depreciation and Amortization              OMB No. 1545-0172
Department of the Treasury          (Including Information on Listed Property)        {tax_year}
Internal Revenue Service      Attach to your tax return.                       Attachment
                        Go to www.irs.gov/Form4562 for instructions.            Sequence No. 179

Name(s) shown on return                                   Business or activity to which this form relates
{taxpayer:<58}Rental Real Estate

Part I   Election To Expense Certain Property Under Section 179
 1  Maximum amount . . . . . . . . . . . . . . . . . . . . . . . . . . . .  1          0

Part II  Special Depreciation Allowance and Other Depreciation
14  Special depreciation allowance for qualified property placed in
    service during the tax year . . . . . . . . . . . . . . . . . . . . .  14          0
17  MACRS deductions for assets placed in service in tax years
    beginning before {tax_year} . . . . . . . . . . . . . . . . . . . . . . .  17

Part III Section B - Assets Placed in Service During {tax_year} Tax Year Using the
         General Depreciation System

     (a) Classification   (b) Month and    (c) Basis for      (d) Recovery  (e) Con-  (f) Me-  (g) Depreciation
         of property          year placed      depreciation       period       vention   thod        deduction
                              in service
{body}

For Paperwork Reduction Act Notice, see separate instructions.          Form 4562 ({tax_year})
                                                                                    Page {page_no}"""


def _ocr_noise(
    text: str, rng: random.Random, rate: float = 0.03, corrupt_digits: bool = True
) -> str:
    """Plausible scanner confusions, not random bytes.

    Real OCR fails in specific ways - O/0, l/1, S/5, B/8, rn/m - and an
    extractor that survives random corruption may still fall over on the
    confusions that actually occur.

    **Digits are corrupted too, and the ground truth stays the true value.**
    That is deliberate and it is the most informative case in the corpus: the
    figure is genuinely unreadable, so there is no reading that scores as
    correct. What the eval is really measuring on this document is whether the
    model *knows* it cannot read it.

    Three possible behaviours, in descending order of what we want:

    1. omit the field entirely - a human reviews it, costing minutes;
    2. report it with low confidence - the auto-accept threshold catches it;
    3. report a corrupted digit confidently - it flows into a recommendation an
       advisor defends with their license.

    Only the third is a failure, and an eval built on clean documents cannot
    tell the three apart at all.
    """
    swaps = {"O": "0", "l": "1", "I": "1", "S": "5", "B": "8", "G": "6", "Z": "2"}
    digit_swaps = {"0": "O", "1": "l", "5": "S", "8": "B", "6": "G", "3": "8", "7": "1"}
    out = []
    for ch in text:
        roll = rng.random()
        if ch in swaps and roll < rate:
            out.append(swaps[ch])
        elif corrupt_digits and ch in digit_swaps and roll < rate * 0.8:
            out.append(digit_swaps[ch])
        else:
            out.append(ch)
    return "".join(out)


# --------------------------------------------------------------------------
# The household. These figures agree with packages/engine fixtures, so the
# pipeline and the engine tell the same story.
# --------------------------------------------------------------------------
TAXPAYER = "Adaeze N. & Chidi O. Okafor"
SSN = "***-**-4417"

# The same property written three different ways across years. Reconciliation
# has to notice these are one property; averaging them would be worse than
# failing loudly.
LONG_BEACH_ALIASES = {
    2021: "1247 Ocean Blvd, Long Beach, CA 90802",
    2022: "1247 Ocean Boulevard, Long Beach CA 90802",
    2024: "1247 Ocean Blv., Long Beach, California 90802",
}


def _long_beach(year: int, depreciation: int) -> dict:
    """The disputed property. Depreciation moves between 2022 and 2024 because
    the preparer changed the land allocation - and said so nowhere."""
    rents = {2021: 114_000, 2022: 118_300, 2024: 126_400}[year]
    interest = {2021: 9_100, 2022: 9_640, 2024: 12_870}[year]
    taxes = {2021: 13_900, 2022: 14_300, 2024: 15_180}[year]
    insurance = {2021: 11_200, 2022: 12_900, 2024: 16_400}[year]
    mgmt = round(rents * 0.08)
    repairs = {2021: 7_400, 2022: 5_200, 2024: 9_800}[year]
    total = interest + taxes + insurance + mgmt + repairs + depreciation + 2_400
    return {
        "label": LONG_BEACH_ALIASES[year], "type_code": 2, "fair_rental_days": 365,
        "rents": rents, "mortgage_interest": interest, "taxes": taxes,
        "insurance": insurance, "management": mgmt, "repairs": repairs,
        "cleaning": 2_400, "depreciation": depreciation,
        "advertising": 0, "auto": 0, "commissions": 0, "legal": 0,
        "other_interest": 0, "supplies": 0, "utilities": 0,
        "total_expenses": total, "net": rents - total,
        "disallowed": max(0, total - rents),
        "_truth": {
            "property_label": LONG_BEACH_ALIASES[year], "tax_year": year,
            "rents_received": rents, "total_expenses": total,
            "mortgage_interest": interest, "depreciation": depreciation,
            "insurance": insurance, "property_taxes": taxes,
        },
    }


def _pasadena(year: int) -> dict:
    rents = {2021: 79_200, 2022: 82_100, 2024: 86_400}[year]
    interest = {2021: 18_615, 2022: 18_615, 2024: 18_615}[year]
    depreciation = 16_938
    taxes = {2021: 7_900, 2022: 8_150, 2024: 8_640}[year]
    insurance = {2021: 4_900, 2022: 5_700, 2024: 7_400}[year]
    mgmt = round(rents * 0.07)
    repairs = {2021: 3_100, 2022: 11_400, 2024: 4_600}[year]
    total = interest + taxes + insurance + mgmt + repairs + depreciation + 1_900
    return {
        "label": "1842 Casitas Ave, Pasadena, CA 91103", "type_code": 2,
        "fair_rental_days": 365, "rents": rents, "mortgage_interest": interest,
        "taxes": taxes, "insurance": insurance, "management": mgmt,
        "repairs": repairs, "cleaning": 1_900, "depreciation": depreciation,
        "advertising": 0, "auto": 0, "commissions": 0, "legal": 0,
        "other_interest": 0, "supplies": 0, "utilities": 0,
        "total_expenses": total, "net": rents - total,
        "disallowed": max(0, total - rents),
        "_truth": {
            "property_label": "1842 Casitas Ave, Pasadena, CA 91103",
            "tax_year": year, "rents_received": rents, "total_expenses": total,
            "mortgage_interest": interest, "depreciation": depreciation,
            "insurance": insurance, "property_taxes": taxes,
        },
    }


def _austin(year: int) -> dict:
    rents = {2021: 30_600, 2022: 39_800, 2024: 43_200}[year]
    interest = {2021: 11_180, 2022: 14_890, 2024: 21_640}[year]
    depreciation = {2021: 135_734, 2022: 24_119, 2024: 16_402}[year]
    taxes = {2021: 9_700, 2022: 11_200, 2024: 12_900}[year]
    insurance = {2021: 2_600, 2022: 3_400, 2024: 4_900}[year]
    mgmt = round(rents * 0.08)
    repairs = {2021: 2_200, 2022: 1_800, 2024: 6_300}[year]
    total = interest + taxes + insurance + mgmt + repairs + depreciation + 1_100
    return {
        "label": "4412 Ramsey Ave, Austin, TX 78756", "type_code": 1,
        "fair_rental_days": {2021: 276, 2022: 365, 2024: 365}[year],
        "rents": rents, "mortgage_interest": interest, "taxes": taxes,
        "insurance": insurance, "management": mgmt, "repairs": repairs,
        "cleaning": 1_100, "depreciation": depreciation,
        "advertising": 0, "auto": 0, "commissions": 0, "legal": 0,
        "other_interest": 0, "supplies": 0, "utilities": 0,
        "total_expenses": total, "net": rents - total,
        "disallowed": max(0, total - rents),
        "_truth": {
            "property_label": "4412 Ramsey Ave, Austin, TX 78756",
            "tax_year": year, "rents_received": rents, "total_expenses": total,
            "mortgage_interest": interest, "depreciation": depreciation,
            "insurance": insurance, "property_taxes": taxes,
        },
    }


def build_corpus(seed: int = 20260923) -> list[Document]:
    """The corpus. Deterministic: the same documents every run."""
    rng = random.Random(seed)
    docs: list[Document] = []

    # --- Schedule E, three years. 2023 is deliberately absent. ---------------
    # 2022 implies a 30% land allocation; 2024 implies 20%. Both were filed.
    for year, lb_depreciation in ((2021, 28_509), (2022, 28_509), (2024, 32_582)):
        rows = [_pasadena(year), _austin(year), _long_beach(year, lb_depreciation)]
        page = _schedule_e_page(year, TAXPAYER, SSN, rows, 1)
        if year == 2021:
            # This year's return only exists as a scan, and the scan is bad.
            page = _ocr_noise(page, rng)
        docs.append(Document(
            id=f"{year}-form-1040-schedule-e",
            kind="schedule_e", tax_year=year, pages=(page,),
            truth=tuple(r["_truth"] for r in rows),
            note=("degraded scan: character and digit level OCR errors"
                  if year == 2021 else ""),
        ))

    # --- Form 4562, only for the years a new asset was placed in service -----
    docs.append(Document(
        id="2021-form-4562", kind="form_4562", tax_year=2021,
        pages=(_form_4562_page(2021, TAXPAYER, [{
            "label": "4412 Ramsey Ave, Austin, TX 78756",
            "placed_in_service": "04/2021", "cost": 489_900,
            "recovery": 27.5, "deduction": 9_734,
        }], 1),),
        truth=({
            "property_label": "4412 Ramsey Ave, Austin, TX 78756",
            "tax_year": 2021, "date_placed_in_service": "2021-04-01",
            "cost_or_basis": 489_900, "recovery_period_years": 27.5,
            # The form prints a deduction column. Leaving it out of the truth
            # made a correct extraction score as "spurious" - the eval was
            # wrong before the pipeline was.
            "depreciation": 9_734,
        },),
    ))
    docs.append(Document(
        id="2018-form-4562", kind="form_4562", tax_year=2018,
        pages=(_form_4562_page(2018, TAXPAYER, [{
            "label": "1247 Ocean Blvd, Long Beach, CA 90802",
            "placed_in_service": "03/2018", "cost": 784_000,
            "recovery": 27.5, "deduction": 23_758,
        }], 1),),
        truth=({
            "property_label": "1247 Ocean Blvd, Long Beach, CA 90802",
            "tax_year": 2018, "date_placed_in_service": "2018-03-01",
            "cost_or_basis": 784_000, "recovery_period_years": 27.5,
            "depreciation": 23_758,
        },),
        note="the only document stating Long Beach's depreciable basis directly",
    ))
    return docs


#: Flattened ground truth: (document id, property label) -> field -> value.
def GOLDEN(seed: int = 20260923) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for doc in build_corpus(seed):
        for truth in doc.truth:
            out[(doc.id, truth["property_label"])] = dict(truth)
    return out
