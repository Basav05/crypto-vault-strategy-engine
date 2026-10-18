"""Position sizing, portfolio veto and the final pre-trade gate.  The risk engine has veto authority."""
from decimal import Decimal, ROUND_FLOOR
from typing import Tuple, Optional
from .config import Config, ENV_PAPER

ZERO = Decimal(0)


def floor_to_lot(x: Decimal, lot: Decimal) -> Decimal:
    if lot <= 0:
        return ZERO
    return (x / lot).to_integral_value(rounding=ROUND_FLOOR) * lot


def calculate_position_size(max_loss_per_unit: Decimal, collateral_per_unit: Decimal, lot: Decimal,
                            nav: Decimal, available_collateral: Decimal, cfg: Config) -> Tuple[Decimal, str]:
    """Returns (units, reason).  units == 0 means rejected.  'unit' = 1 contract per leg."""
    if nav <= 0:
        return ZERO, "INVALID_NAV"
    if max_loss_per_unit <= 0:
        return ZERO, "INVALID_MAX_LOSS"
    if lot <= 0:
        return ZERO, "INVALID_LOT_SIZE"
    budget = nav * cfg.max_risk_per_trade_pct_nav
    units = floor_to_lot(budget / max_loss_per_unit, lot)
    if units < lot:
        return ZERO, "SIZE_BELOW_EXCHANGE_MINIMUM"
    if cfg.require_collateral_check:
        if collateral_per_unit <= 0:
            return ZERO, "COLLATERAL_UNKNOWN"
        affordable = floor_to_lot(available_collateral / collateral_per_unit, lot)
        units = min(units, affordable)
        if units < lot:
            return ZERO, "INSUFFICIENT_COLLATERAL"
    return units, "OK"


def _chk(name, passed, detail):
    return {"check": name, "passed": bool(passed), "detail": detail}


def portfolio_risk_gate(candidate_id: str, underlying: str, max_loss_total: Optional[Decimal],
                        collateral_total: Optional[Decimal], account, open_positions, nav: Decimal,
                        cfg: Config, exposure_key: str) -> list:
    """Evaluates *all* checks (for a complete audit trail).  Caller rejects if any failed."""
    open_ = [p for p in open_positions]
    total_open_loss = sum((p.max_loss for p in open_), ZERO)
    used_collateral = sum((p.collateral for p in open_), ZERO)
    available = nav - used_collateral
    per_u = sum(1 for p in open_ if p.underlying == underlying)
    checks = [
        _chk("POSITION_COUNT_LIMIT", len(open_) < cfg.max_open_positions, f"{len(open_)}/{cfg.max_open_positions}"),
        _chk("UNDERLYING_POSITION_LIMIT", per_u < cfg.max_positions_per_underlying,
             f"{per_u}/{cfg.max_positions_per_underlying} {underlying}"),
        _chk("VALID_MAX_LOSS", max_loss_total is not None and max_loss_total > 0, f"max_loss={max_loss_total}"),
        _chk("COLLATERAL_KNOWN", collateral_total is not None and collateral_total > 0, f"collateral={collateral_total}"),
    ]
    if max_loss_total is not None and max_loss_total > 0:
        checks.append(_chk("PER_TRADE_RISK_LIMIT", max_loss_total <= nav * cfg.max_risk_per_trade_pct_nav,
                           f"{max_loss_total} <= {nav * cfg.max_risk_per_trade_pct_nav}"))
        checks.append(_chk("TOTAL_RISK_LIMIT", total_open_loss + max_loss_total <= nav * cfg.max_total_open_risk_pct_nav,
                           f"{total_open_loss + max_loss_total} <= {nav * cfg.max_total_open_risk_pct_nav}"))
    if collateral_total is not None:
        checks.append(_chk("SUFFICIENT_COLLATERAL", collateral_total <= available, f"{collateral_total} <= {available}"))
    checks.append(_chk("DAILY_LOSS_LIMIT", account.daily_loss(nav) < account.start_of_day_nav * cfg.max_daily_loss_pct_nav,
                       f"daily_loss={account.daily_loss(nav)}"))
    checks.append(_chk("DRAWDOWN_LIMIT", account.drawdown(nav) < cfg.max_drawdown_pct_nav,
                       f"drawdown={account.drawdown(nav):.4f}"))
    checks.append(_chk("NO_DUPLICATE_EXPOSURE", all(p.exposure_key != exposure_key for p in open_), exposure_key))
    return checks


def final_pre_trade_gate(cfg: Config, kill_switch: bool, quotes_fresh: bool, payoff_valid: bool,
                         fees_available: bool, size_valid: bool, portfolio_ok: bool,
                         unresolved_exists: bool, entries_paused: bool) -> list:
    return [
        _chk("ENVIRONMENT_PAPER_ONLY", cfg.environment == ENV_PAPER, cfg.environment),
        _chk("LIVE_ORDERS_DISABLED", cfg.allow_live_orders is False, str(cfg.allow_live_orders)),
        _chk("KILL_SWITCH_OFF", not kill_switch, str(kill_switch)),
        _chk("QUOTES_FRESH", quotes_fresh, ""),
        _chk("PAYOFF_BOUNDED", payoff_valid, ""),
        _chk("FEES_AVAILABLE", fees_available, ""),
        _chk("SIZE_VALID", size_valid, ""),
        _chk("PORTFOLIO_LIMITS", portfolio_ok, ""),
        _chk("NO_UNRESOLVED_POSITIONS", not unresolved_exists, ""),
        _chk("ENTRIES_NOT_PAUSED", not entries_paused, ""),
    ]
