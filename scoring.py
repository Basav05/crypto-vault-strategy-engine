"""Ranks candidates that already passed hard checks.  Scores never override a failed hard check."""
from decimal import Decimal
from .config import Config
from .generator import PUT_SPREAD, CALL_SPREAD, BULL_CALL, BEAR_PUT, CONDOR
from .regime import BULLISH, BEARISH, RANGE

WEIGHTS = {"regime_fit": 0.25, "reward_risk": 0.20, "liquidity": 0.15,
           "volatility_fit": 0.10, "trend_confirmation": 0.10, "execution_quality": 0.20}
FIT = {BULLISH: {PUT_SPREAD: 0.90, BULL_CALL: 0.75},
       BEARISH: {CALL_SPREAD: 0.90, BEAR_PUT: 0.75},
       RANGE: {CONDOR: 0.90}}
DIRECTION = {PUT_SPREAD: 1, BULL_CALL: 1, CALL_SPREAD: -1, BEAR_PUT: -1, CONDOR: 0}


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def score_candidate(c, regime, regime_conf, features, pricing, payoff, cfg: Config) -> dict:
    fv = features.values
    fit = FIT.get(regime, {}).get(c.strategy_type, 0.0) * (0.5 + 0.5 * regime_conf)

    rr = float(payoff.max_profit / payoff.max_loss)
    reward_risk = _clip(rr / float(cfg.reward_risk_target))

    spreads = [float(l.quote.spread_pct) for l in c.legs]
    liquidity = _clip(1 - (sum(spreads) / len(spreads)) / float(cfg.max_spread_pct))

    iv, rv = fv.get("atm_iv"), fv.get("realized_vol_short")
    if iv and rv:
        edge = _clip(0.5 + (iv / rv - 1.0) / 2.0)               # research heuristic, not a proven edge
        vol_fit = edge if pricing.net_debit < 0 else 1.0 - edge  # sellers like rich IV, buyers like cheap IV
    else:
        vol_fit = 0.5

    d = DIRECTION[c.strategy_type]
    r4 = fv.get("return_4h") or 0.0
    ofi = fv.get("ofi")
    if d == 0:
        trend = 1 - _clip(abs(r4) / cfg.positive_return_threshold)
    else:
        t = _clip(d * r4 / (2 * cfg.positive_return_threshold))
        o = 0.5 if ofi is None else _clip(0.5 + d * ofi / (2 * cfg.ofi_bullish_threshold) * 0.5)
        trend = 0.6 * t + 0.4 * o

    costs = float(pricing.entry_fees + pricing.est_exit_fees + pricing.slippage_cost)
    execq = _clip(1 - costs / float(payoff.max_profit)) if payoff.max_profit > 0 else 0.0

    parts = {"regime_fit": fit, "reward_risk": reward_risk, "liquidity": liquidity,
             "volatility_fit": vol_fit, "trend_confirmation": trend, "execution_quality": execq}
    total = sum(WEIGHTS[k] * v for k, v in parts.items())
    return {"total": round(total, 4), **{k: round(v, 4) for k, v in parts.items()}}
