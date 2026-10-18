"""Conservative pricing and analytical payoff/risk for multi-leg option candidates.

Conventions: quantity > 0 long, < 0 short.  net_debit > 0 means the position costs money to open;
< 0 means a net credit.  Slippage is already inside execution prices (it is reported separately but
never added a second time)."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict
from .config import Config
from .models import Candidate, CALL, PUT
from .validation import quote_problem

ZERO = Decimal(0)


def estimate_slippage(q, cfg: Config) -> Decimal:
    return (q.ask - q.bid) * cfg.slippage_spread_fraction


def estimate_fee(exec_price: Decimal, spot: Decimal, amount: Decimal, cfg: Config) -> Decimal:
    """Fee per leg = min(rate * underlying value, cap * option value).  Assumption, not exchange truth."""
    return min(cfg.fee_rate_underlying * spot * amount, cfg.fee_cap_option_pct * exec_price * amount)


@dataclass
class Pricing:
    is_valid: bool
    reason: str = "OK"
    net_debit: Decimal = ZERO            # incl. entry fees; +cost / -credit
    entry_fees: Decimal = ZERO
    est_exit_fees: Decimal = ZERO
    slippage_cost: Decimal = ZERO
    exec_prices: Dict[str, Decimal] = field(default_factory=dict)


def price_candidate(c: Candidate, spot: Decimal, now: datetime, cfg: Config) -> Pricing:
    cash = fees = slip_total = ZERO
    prices = {}
    for leg in c.legs:
        i = leg.instrument
        if i.multiplier is None or i.multiplier <= 0:
            return Pricing(False, "INVALID_MULTIPLIER")
        problem = quote_problem(leg.quote, now, cfg)
        if problem:
            return Pricing(False, f"QUOTE_INVALID:{problem}")
        q = leg.quote
        slip = estimate_slippage(q, cfg)
        px = q.ask + slip if leg.quantity > 0 else q.bid - slip
        if px < 0:
            return Pricing(False, "INVALID_EXECUTION_PRICE")
        amount = abs(leg.quantity) * i.multiplier
        cash += leg.quantity * px * i.multiplier
        fees += estimate_fee(px, spot, amount, cfg)
        slip_total += slip * amount
        prices[i.instrument_id] = px
    # exit-fee estimate: assume symmetric to entry (conservative for expiry settlement)
    return Pricing(True, "OK", cash + fees, fees, fees, slip_total, prices)


def intrinsic(option_type: str, strike: Decimal, spot: Decimal) -> Decimal:
    if option_type == CALL:
        return max(spot - strike, ZERO)
    return max(strike - spot, ZERO)


def payoff_at_expiry(c: Candidate, expiry_spot: Decimal) -> Decimal:
    total = ZERO
    for leg in c.legs:
        total += leg.quantity * leg.instrument.multiplier * intrinsic(
            leg.instrument.option_type, leg.instrument.strike, expiry_spot)
    return total


def pnl_at_expiry(c: Candidate, p: Pricing, expiry_spot: Decimal) -> Decimal:
    return payoff_at_expiry(c, expiry_spot) - p.net_debit - p.est_exit_fees


@dataclass
class PayoffResult:
    is_valid: bool
    reason: str = "OK"
    max_profit: Optional[Decimal] = None
    max_loss: Optional[Decimal] = None          # positive number
    collateral_required: Optional[Decimal] = None
    breakpoints: tuple = ()


def analyze_payoff(c: Candidate, p: Pricing, cfg: Config) -> PayoffResult:
    """Analytical bounds.  The expiry payoff of vanilla options is piecewise linear with kinks at the
    strikes, so extrema occur at S=0 or at a strike; a finite grid is only used as a cross-check."""
    if not p.is_valid:
        return PayoffResult(False, f"PRICING_INVALID:{p.reason}")
    if not c.legs:
        return PayoffResult(False, "NO_LEGS")
    for leg in c.legs:
        i = leg.instrument
        if i.multiplier is None or i.multiplier <= 0:
            return PayoffResult(False, "INVALID_MULTIPLIER")
        if i.payoff_model not in cfg.supported_payoff_models:
            return PayoffResult(False, "PAYOFF_MODEL_NOT_SUPPORTED")
        if i.option_type not in (CALL, PUT):
            return PayoffResult(False, "INVALID_OPTION_TYPE")
    if len({l.instrument.expiry for l in c.legs}) != 1:
        return PayoffResult(False, "EXPIRY_MISMATCH")
    call_slope = sum((l.quantity * l.instrument.multiplier for l in c.legs
                      if l.instrument.option_type == CALL), ZERO)
    if call_slope < 0:
        return PayoffResult(False, "UNBOUNDED_UPSIDE_LOSS")
    if call_slope > 0:
        return PayoffResult(False, "UNBOUNDED_UPSIDE_PROFIT_UNSUPPORTED")
    bps = sorted({ZERO} | {l.instrument.strike for l in c.legs})
    pnls = [pnl_at_expiry(c, p, s) for s in bps]
    max_profit, worst = max(pnls), min(pnls)
    max_loss = -worst
    # grid cross-check: midpoints and far tails must never beat the analytical extrema
    grid = [(bps[k] + bps[k + 1]) / 2 for k in range(len(bps) - 1)] + [bps[-1] * 3]
    gp = [pnl_at_expiry(c, p, s) for s in grid]
    if gp and (min(gp) < worst or max(gp) > max_profit):
        return PayoffResult(False, "PAYOFF_GRID_DISAGREES_WITH_ANALYTIC_BOUNDS")
    if max_loss <= 0:
        return PayoffResult(False, "NON_POSITIVE_MAX_LOSS_SUSPECT_QUOTES")
    collateral = max_loss + max(-p.net_debit, ZERO)      # gross of any credit received
    return PayoffResult(True, "OK", max_profit, max_loss, collateral, tuple(bps))
