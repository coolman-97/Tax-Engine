"""How a property leaves the portfolio: sale, exchange, installment, or death."""

from .death import step_up_at_death
from .exchange_1031 import ExchangeResult, ExchangeTerms, compute_exchange
from .sale import SaleResult, SaleTerms, compute_sale

__all__ = [
    "SaleTerms", "SaleResult", "compute_sale",
    "ExchangeTerms", "ExchangeResult", "compute_exchange",
    "step_up_at_death",
]
