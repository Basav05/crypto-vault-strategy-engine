"""Feature engine: measured values + data-quality flags. No trade recommendations here."""
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional
from .config import Config
from .models import CALL, PUT
from .validation import option_is_eligible, data_quality, days_until

HOURS_PER_YEAR = 24 * 365


@dataclass
class FeatureResult:
    are_complete: bool
    reason: str = "OK"
    values: dict = field(default_factory=dict)
    data_quality: float = 0.0


def _ret(prices, hours):
    if len(prices) <= hours or prices[-1 - hours] <= 0:
        return None
    return prices[-1] / prices[-1 - hours] - 1.0


def _sma(prices, n):
    return sum(prices[-n:]) / n if len(prices) >= n else None


def annualized_vol(prices, window):
    seg = prices[-(window + 1):]
    if len(seg) < 3:
        return None
    rets = [math.log(b / a) for a, b in zip(seg, seg[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    return statistics.stdev(rets) * math.sqrt(HOURS_PER_YEAR)


def _nearest_iv(opts, target_strike, option_type=None, tol=0.05, spot=None):
    best, best_d = None, None
    for oq in opts:
        if oq.iv is None or (option_type and oq.instrument.option_type != option_type):
            continue
        d = abs(float(oq.instrument.strike) / spot - target_strike)
        if d <= tol and (best_d is None or d < best_d):
            best, best_d = oq.iv, d
    return best


def atm_iv_and_skew(chain, spot: Decimal, now: datetime, cfg: Config):
    spot_f = float(spot)
    eligible = [oq for oq in chain if option_is_eligible(oq, now, cfg)[0] and oq.iv is not None]
    if not eligible:
        return None, None, None
    expiry = min({oq.instrument.expiry for oq in eligible})
    near = [oq for oq in eligible if oq.instrument.expiry == expiry]
    atm = [oq for oq in near if abs(float(oq.instrument.strike) / spot_f - 1) <= cfg.atm_band]
    if not atm:
        return None, None, None
    w = [1.0 / (0.005 + abs(float(o.instrument.strike) / spot_f - 1)) for o in atm]
    iv = sum(wi * o.iv for wi, o in zip(w, atm)) / sum(w)
    put90 = _nearest_iv(near, 0.90, PUT, spot=spot_f)
    call110 = _nearest_iv(near, 1.10, CALL, spot=spot_f)
    return iv, (None if put90 is None else put90 - iv), (None if call110 is None else call110 - iv)


def calculate_features(underlying, spot: Decimal, prices, chain, trades, now, cfg: Config) -> FeatureResult:
    closes = [p for _, p in prices]
    if len(closes) < cfg.min_price_observations:
        return FeatureResult(False, "INSUFFICIENT_PRICE_HISTORY")
    if any(p <= 0 for p in closes):
        return FeatureResult(False, "INVALID_PRICE_HISTORY")
    quality = data_quality(chain, spot, now, cfg)
    v = {
        "return_1h": _ret(closes, 1), "return_4h": _ret(closes, 4), "return_24h": _ret(closes, 24),
        "trend_fast": _sma(closes, cfg.fast_window), "trend_slow": _sma(closes, cfg.slow_window),
        "realized_vol_short": annualized_vol(closes, cfg.short_vol_window),
        "realized_vol_long": annualized_vol(closes, min(cfg.long_vol_window, len(closes) - 1)),
    }
    iv, put_skew, call_skew = atm_iv_and_skew(chain, spot, now, cfg)
    v.update(atm_iv=iv, put_skew=put_skew, call_skew=call_skew)
    buy, sell, count = trades if trades else (0.0, 0.0, 0)
    v["ofi"] = (buy - sell) / (buy + sell) if (buy + sell) > 0 and count >= cfg.min_trades_for_ofi else None
    spreads = [float(oq.quote.spread_pct) for oq in chain
               if option_is_eligible(oq, now, cfg)[0]]
    v["median_spread_pct"] = statistics.median(spreads) if spreads else None
    required = ("return_4h", "return_24h", "trend_fast", "trend_slow", "realized_vol_short", "atm_iv")
    missing = [k for k in required if v[k] is None]
    if missing:
        return FeatureResult(False, "MISSING_FEATURES:" + ",".join(missing), v, quality)
    return FeatureResult(True, "OK", v, quality)
