"""How a dollar of gain is characterised, which decides what it costs.

A sale does not produce "a gain". It produces up to five different kinds of
gain, each taxed under a different rule, and the split is the whole game:

- **Unrecaptured section 1250 gain** - the part attributable to straight-line
  depreciation on real property. Taxed at up to 25%. This is the slice clients
  never see coming, because they think of depreciation as a deduction they
  took, not a tax they deferred.
- **Section 1245 recapture** - depreciation on the 5- and 7-year personal
  property a cost segregation study carved out. Taxed as **ordinary income**,
  not at 25%. A cost seg study that saved tax at 37% gives some of it back at
  37%, and an engine that lumps all recapture together at 25% understates the
  exit cost of every cost-segregated property it touches.
- **Section 1250(a) ordinary recapture** - the *additional* depreciation on
  section 1250 property: the excess of what was actually deducted over what
  straight line would have given. Zero for a building, because a building is
  already on straight line. Large for 15-year land improvements, which run on
  150% declining balance and are bonus-eligible. Sell a cost-segregated
  property a few years after claiming 100% bonus and most of the land
  improvement basis comes back as ordinary income, not as 25% gain.
- **Adjusted net capital gain** - the rest, taxed at 0/15/20%.
- **Section 1231 ordinary** - gain recharacterised as ordinary by the five-year
  nonrecaptured-loss lookback in 1231(c).
- **Section 121 excluded** - gain that simply is not taxed.

Keeping these as separate fields rather than one number is what lets the exit
tax waterfall in the UI show a client *why* the bill is what it is.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .money import Money

__all__ = ["GainCharacter"]


@dataclass(frozen=True)
class GainCharacter:
    """A realised gain, split by how it will be taxed."""

    unrecaptured_1250: Money = Money(0)
    section_1245_ordinary: Money = Money(0)
    section_1250_ordinary: Money = Money(0)
    section_1231_ordinary: Money = Money(0)
    adjusted_net_capital_gain: Money = Money(0)
    section_121_excluded: Money = Money(0)
    deferred_1031: Money = Money(0)
    capital_loss: Money = Money(0)

    @property
    def total_realized(self) -> Money:
        return (
            self.unrecaptured_1250
            + self.section_1245_ordinary
            + self.section_1250_ordinary
            + self.section_1231_ordinary
            + self.adjusted_net_capital_gain
            + self.section_121_excluded
            + self.deferred_1031
            - self.capital_loss
        )

    @property
    def recognized(self) -> Money:
        """Gain that shows up on this year's return."""
        return (
            self.unrecaptured_1250
            + self.section_1245_ordinary
            + self.section_1250_ordinary
            + self.section_1231_ordinary
            + self.adjusted_net_capital_gain
            - self.capital_loss
        )

    @property
    def ordinary_component(self) -> Money:
        return (
            self.section_1245_ordinary
            + self.section_1250_ordinary
            + self.section_1231_ordinary
        )

    @property
    def net_investment_income(self) -> Money:
        """Gain from a passive rental is net investment income for IRC 1411.

        Gain excluded under 121 is not; gain deferred under 1031 is not
        recognised, so it is not either.
        """
        return (
            self.unrecaptured_1250
            + self.section_1245_ordinary
            + self.section_1250_ordinary
            + self.section_1231_ordinary
            + self.adjusted_net_capital_gain
            - self.capital_loss
        ).clamp_at_zero()

    def __add__(self, other: GainCharacter) -> GainCharacter:
        return GainCharacter(
            self.unrecaptured_1250 + other.unrecaptured_1250,
            self.section_1245_ordinary + other.section_1245_ordinary,
            self.section_1250_ordinary + other.section_1250_ordinary,
            self.section_1231_ordinary + other.section_1231_ordinary,
            self.adjusted_net_capital_gain + other.adjusted_net_capital_gain,
            self.section_121_excluded + other.section_121_excluded,
            self.deferred_1031 + other.deferred_1031,
            self.capital_loss + other.capital_loss,
        )

    @classmethod
    def sum(cls, items) -> GainCharacter:
        total = cls()
        for item in items:
            total = total + item
        return total

    def with_1231_recharacterized(self, amount: Money) -> GainCharacter:
        """Move ``amount`` of capital gain to ordinary under 1231(c) lookback."""
        moved = min(amount, self.adjusted_net_capital_gain)
        return replace(
            self,
            adjusted_net_capital_gain=self.adjusted_net_capital_gain - moved,
            section_1231_ordinary=self.section_1231_ordinary + moved,
        )
