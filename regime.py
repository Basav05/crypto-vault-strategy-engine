"""Transparent rule-based regime classifier + hysteresis.  Confidence = confidence in the *label*,
NOT the probability that a trade is profitable."""
from dataclasses import dataclass, field
from typing import List
from .config import Config

BULLISH, BEARISH, RANGE, HIGH_VOL, UNCERTAIN = "BULLISH", "BEARISH", "RANGE", "HIGH_VOL", "UNCERTAIN"
SAFETY_LABELS = (HIGH_VOL, UNCERTAIN)


@dataclass
class Regime:
    label: str
    confidence: float
    reasons: List[str] = field(default_factory=list)


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def classify_market_regime(f, cfg: Config) -> Regime:
    if not f.are_complete:
        return Regime(UNCERTAIN, 0.0, [f"Features incomplete: {f.reason}"])
    if f.data_quality < cfg.minimum_quote_quality:
        return Regime(UNCERTAIN, 0.0, [f"Data quality {f.data_quality:.2f} < {cfg.minimum_quote_quality:.2f}"])
    v = f.values
    r4, r24 = v["return_4h"], v["return_24h"]
    ofi = v.get("ofi")
    rv, iv = v["realized_vol_short"], v["atm_iv"]

    vol_ratio = max(rv / cfg.high_vol_threshold, iv / cfg.extreme_iv_threshold)
    if vol_ratio > 1.0:
        return Regime(HIGH_VOL, 0.5 + 0.5 * _clip(vol_ratio - 1.0),
                      [f"realized vol {rv:.0%} / ATM IV {iv:.0%} above high-vol limits"])

    trend_up = r4 > cfg.positive_return_threshold and v["trend_fast"] > v["trend_slow"]
    trend_down = r4 < cfg.negative_return_threshold and v["trend_fast"] < v["trend_slow"]
    flow_bull = ofi is not None and ofi > cfg.ofi_bullish_threshold
    flow_bear = ofi is not None and ofi < cfg.ofi_bearish_threshold

    if trend_up and not flow_bear:
        conf = 0.5 + 0.3 * _clip(r4 / (2 * cfg.positive_return_threshold)) + (0.2 if flow_bull else 0.0)
        return Regime(BULLISH, _clip(conf), [f"4h return {r4:+.2%}, fast MA > slow MA"] +
                      (["order flow confirms"] if flow_bull else []))
    if trend_down and not flow_bull:
        conf = 0.5 + 0.3 * _clip(r4 / (2 * cfg.negative_return_threshold)) + (0.2 if flow_bear else 0.0)
        return Regime(BEARISH, _clip(conf), [f"4h return {r4:+.2%}, fast MA < slow MA"] +
                      (["order flow confirms"] if flow_bear else []))
    if (not trend_up and not trend_down and abs(r24) < cfg.range_return_24h_threshold
            and abs(r4) < abs(cfg.positive_return_threshold)):
        conf = 0.5 + 0.5 * (1 - abs(r24) / cfg.range_return_24h_threshold)
        return Regime(RANGE, _clip(conf), [f"24h return {r24:+.2%} inside range band, no trend"])
    return Regime(UNCERTAIN, 0.0, ["Signals conflict or are insufficient"])


class RegimeTracker:
    """Hysteresis: safety transitions (HIGH_VOL / UNCERTAIN) apply immediately; others need N
    consecutive confirmations and a minimum hold time.  Leaving HIGH_VOL also needs confirmation."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.current = UNCERTAIN
        self.history: List[str] = []
        self.cycles_since_change = 10 ** 6

    def update(self, regime: Regime) -> str:
        self.history.append(regime.label)
        self.cycles_since_change += 1
        if regime.label in SAFETY_LABELS:
            if regime.label != self.current:
                self.current, self.cycles_since_change = regime.label, 0
            return self.current
        if regime.label == self.current:
            return self.current
        n = self.cfg.regime_confirmations
        recent = self.history[-n:]
        if len(recent) < n or any(l != regime.label for l in recent):
            return self.current
        if self.cycles_since_change < self.cfg.regime_min_hold_cycles:
            return self.current
        self.current, self.cycles_since_change = regime.label, 0
        return self.current
