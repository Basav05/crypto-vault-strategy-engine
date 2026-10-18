from dataclasses import replace
from decimal import Decimal as D
from regimevault.config import Config
from regimevault.features import FeatureResult
from regimevault.market_data import SyntheticProvider
from regimevault.models import OptionQuote, SpotData
from regimevault.regime import (classify_market_regime, RegimeTracker, Regime, BULLISH, BEARISH, RANGE, HIGH_VOL, UNCERTAIN)
from regimevault.validation import validate_spot, option_is_eligible, data_quality
from tests.helpers import *

CFG = Config()


def feats(**over):
    v = dict(return_4h=0.015, return_24h=0.05, trend_fast=101, trend_slow=100, realized_vol_short=0.3,
             atm_iv=0.5, ofi=0.3)
    v.update(over)
    return FeatureResult(True, "OK", v, 1.0)


def test_stale_spot_and_missing_spot_and_clock_skew_detected():
    snap = SyntheticProvider(CFG, "range", clock=Clock()).get_snapshot()
    assert validate_spot(snap, CFG) == []
    snap.spot["BTC"] = SpotData("BTC", D(60000), snap.timestamp - timedelta(seconds=120))
    assert "BTC:STALE_SPOT_DATA" in validate_spot(snap, CFG)
    snap.spot["BTC"] = SpotData("BTC", D(60000), snap.timestamp + timedelta(seconds=120))
    assert "BTC:CLOCK_SKEW" in validate_spot(snap, CFG)
    snap.spot["BTC"] = SpotData("BTC", D(0), snap.timestamp)
    assert "BTC:INVALID_INDEX_PRICE" in validate_spot(snap, CFG)
    del snap.spot["ETH"]
    assert "ETH:SPOT_DATA_MISSING" in validate_spot(snap, CFG)


def test_option_eligibility_rules():
    ok = OptionQuote(inst(PUT, 100), quote(2, 2.2))
    assert option_is_eligible(ok, NOW, CFG)[0]
    assert option_is_eligible(OptionQuote(inst(PUT, 100), quote(1, 3)), NOW, CFG) == (False, "SPREAD_TOO_WIDE")
    assert option_is_eligible(OptionQuote(inst(PUT, 100, active=False), quote(2, 2.2)), NOW, CFG)[1] == "INSTRUMENT_INACTIVE"
    assert option_is_eligible(OptionQuote(inst(PUT, 100, mult=None), quote(2, 2.2)), NOW, CFG)[1] == "INVALID_MULTIPLIER"
    assert option_is_eligible(OptionQuote(inst(PUT, 100, expiry=NOW + timedelta(days=1)), quote(2, 2.2)), NOW, CFG)[1] == "DTE_OUT_OF_RANGE"
    assert option_is_eligible(OptionQuote(inst(PUT, 100), None), NOW, CFG)[1] == "QUOTE_MISSING"
    assert option_is_eligible(OptionQuote(inst(PUT, 100), quote(2, 2.2, ts=NOW - timedelta(seconds=99))), NOW, CFG)[1] == "STALE_QUOTE"


def test_data_quality_zero_when_all_quotes_stale():
    snap = SyntheticProvider(CFG, "range", clock=Clock(), quote_age_seconds=60).get_snapshot()
    assert data_quality(snap.chains["BTC"], snap.spot["BTC"].index_price, snap.timestamp, CFG) == 0.0


def test_classifier_labels():
    assert classify_market_regime(feats(), CFG).label == BULLISH
    assert classify_market_regime(feats(return_4h=-0.015, trend_fast=99, ofi=-0.3), CFG).label == BEARISH
    assert classify_market_regime(feats(return_4h=0.001, return_24h=0.005, trend_fast=100, trend_slow=100.1, ofi=0), CFG).label == RANGE
    assert classify_market_regime(feats(realized_vol_short=1.5), CFG).label == HIGH_VOL
    assert classify_market_regime(feats(atm_iv=1.5), CFG).label == HIGH_VOL


def test_conflicting_or_missing_inputs_are_uncertain_with_zero_confidence():
    r = classify_market_regime(feats(ofi=-0.4), CFG)             # bullish trend, bearish flow
    assert r.label == UNCERTAIN and r.confidence == 0
    assert classify_market_regime(FeatureResult(False, "INSUFFICIENT_PRICE_HISTORY"), CFG).label == UNCERTAIN
    low_q = FeatureResult(True, "OK", feats().values, 0.5)
    assert classify_market_regime(low_q, CFG).label == UNCERTAIN


def test_hysteresis_requires_confirmations_and_high_vol_is_immediate():
    t = RegimeTracker(CFG)
    bull = Regime(BULLISH, .8)
    assert t.update(bull) == UNCERTAIN and t.update(bull) == UNCERTAIN
    assert t.update(bull) == BULLISH
    assert t.update(Regime(BEARISH, .8)) == BULLISH                 # single opposite reading ignored
    assert t.update(Regime(HIGH_VOL, .9)) == HIGH_VOL               # safety: immediate
    assert t.update(Regime(RANGE, .8)) == HIGH_VOL                  # leaving needs confirmation
    assert t.update(Regime(RANGE, .8)) == HIGH_VOL
    assert t.update(Regime(UNCERTAIN, 0)) == UNCERTAIN              # uncertain: immediate
