"""Paper execution only.  There is deliberately NO exchange order path in this package."""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Dict
from .config import Config
from .payoff import estimate_slippage, estimate_fee
from .validation import quote_problem

ZERO = Decimal(0)
FILLED, NOT_FILLED, UNRESOLVED = "FILLED", "NOT_FILLED", "UNRESOLVED"


@dataclass
class FillResult:
    status: str
    reason: str = "OK"
    prices: Dict[str, Decimal] = field(default_factory=dict)
    fees: Decimal = ZERO
    slippage: Decimal = ZERO
    cashflow: Decimal = ZERO       # signed: sum(qty*price*mult). +cost on open, +proceeds on close
    simulated: bool = True


def _conservative_price(qty: Decimal, q, cfg: Config) -> Decimal:
    """Buy at ask+slip; sell at bid-slip (floored at 0 by caller where appropriate)."""
    slip = estimate_slippage(q, cfg)
    return q.ask + slip if qty > 0 else q.bid - slip


def simulate_open_fill(legs, quote_for: Callable, spot: Decimal, now, cfg: Config,
                       limit_net_debit: Decimal) -> FillResult:
    """Atomic multi-leg fill at conservative prices on *refreshed* quotes."""
    prices, cash, fees, slip_total = {}, ZERO, ZERO, ZERO
    for leg in legs:
        i = leg.instrument
        q = quote_for(i.instrument_id)
        problem = quote_problem(q, now, cfg)
        if problem:
            return FillResult(NOT_FILLED, f"STALE_OR_INVALID_QUOTE:{problem}")
        px = _conservative_price(leg.quantity, q, cfg)
        if px < 0:
            return FillResult(NOT_FILLED, "INVALID_EXECUTION_PRICE")
        size = q.ask_size if leg.quantity > 0 else q.bid_size
        if size is None:
            if cfg.require_depth_info:
                return FillResult(NOT_FILLED, "DEPTH_UNKNOWN")
        elif size < abs(leg.quantity):
            return FillResult(NOT_FILLED, "INSUFFICIENT_SIMULATED_DEPTH")
        amount = abs(leg.quantity) * i.multiplier
        prices[i.instrument_id] = px
        cash += leg.quantity * px * i.multiplier
        fees += estimate_fee(px, spot, amount, cfg)
        slip_total += estimate_slippage(q, cfg) * amount
    if len(prices) != len(legs):
        return FillResult(UNRESOLVED, "PARTIAL_OR_UNBALANCED_FILL")
    if cash + fees > limit_net_debit:
        return FillResult(NOT_FILLED, "LIMIT_PRICE_NOT_REACHED", prices, fees, slip_total, cash)
    return FillResult(FILLED, "OK", prices, fees, slip_total, cash)


def simulate_close_fill(legs, quote_for: Callable, spot: Decimal, now, cfg: Config) -> FillResult:
    """Closing sells longs at bid-slip (floored at 0) and buys shorts at ask+slip."""
    prices, cash, fees, slip_total = {}, ZERO, ZERO, ZERO
    for leg in legs:
        i = leg.instrument
        q = quote_for(i.instrument_id)
        problem = quote_problem(q, now, cfg)
        if problem:
            return FillResult(UNRESOLVED, f"CLOSE_QUOTE_UNAVAILABLE:{problem}")
        slip = estimate_slippage(q, cfg)
        px = max(q.bid - slip, ZERO) if leg.quantity > 0 else q.ask + slip
        amount = abs(leg.quantity) * i.multiplier
        prices[i.instrument_id] = px
        cash += leg.quantity * px * i.multiplier          # longs: +proceeds, shorts: -cost
        fees += estimate_fee(px, spot, amount, cfg)
        slip_total += slip * amount
    return FillResult(FILLED, "OK", prices, fees, slip_total, cash)
