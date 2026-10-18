"""Fail-closed data validation. Missing/stale/inconsistent input => no new trade."""
from datetime import datetime
from decimal import Decimal
from typing import Optional, Tuple
from .config import Config
from .models import OptionQuote, Quote, MarketSnapshot, CALL, PUT


def days_until(expiry: datetime, now: datetime) -> float:
    return (expiry - now).total_seconds() / 86400.0


def quote_problem(q: Optional[Quote], now: datetime, cfg: Config) -> Optional[str]:
    """Return a reason string if the quote is unusable, else None."""
    if q is None:
        return "QUOTE_MISSING"
    if q.bid is None or q.ask is None or q.timestamp is None:
        return "QUOTE_INCOMPLETE"
    if q.bid < 0 or q.ask < 0:
        return "NEGATIVE_OPTION_QUOTE"
    if q.ask <= 0:
        return "NO_ASK"
    if q.ask < q.bid:
        return "CROSSED_QUOTE"
    age = (now - q.timestamp).total_seconds()
    if age > cfg.max_market_data_age_seconds:
        return "STALE_QUOTE"
    if age < -cfg.max_clock_skew_seconds:
        return "QUOTE_FROM_FUTURE_CLOCK_SKEW"
    return None


def validate_spot(snapshot: MarketSnapshot, cfg: Config) -> list:
    errors = []
    if not snapshot.spot:
        return ["SPOT_DATA_MISSING"]
    for u in cfg.underlyings:
        s = snapshot.spot.get(u)
        if s is None:
            errors.append(f"{u}:SPOT_DATA_MISSING")
            continue
        if s.index_price is None or s.index_price <= 0:
            errors.append(f"{u}:INVALID_INDEX_PRICE")
        if s.timestamp is None:
            errors.append(f"{u}:MISSING_SPOT_TIMESTAMP")
            continue
        age = (snapshot.timestamp - s.timestamp).total_seconds()
        if age > cfg.max_market_data_age_seconds:
            errors.append(f"{u}:STALE_SPOT_DATA")
        if age < -cfg.max_clock_skew_seconds:
            errors.append(f"{u}:CLOCK_SKEW")
    return errors


def validate_structure(oq: OptionQuote) -> Optional[str]:
    i = oq.instrument
    if not i.instrument_id:
        return "MISSING_INSTRUMENT_ID"
    if i.expiry is None:
        return "MISSING_EXPIRY"
    if i.strike is None or i.strike <= 0:
        return "INVALID_STRIKE"
    if i.option_type not in (CALL, PUT):
        return "INVALID_OPTION_TYPE"
    return None


def option_is_eligible(oq: OptionQuote, now: datetime, cfg: Config) -> Tuple[bool, str]:
    bad = validate_structure(oq)
    if bad:
        return False, bad
    i = oq.instrument
    if not i.is_active:
        return False, "INSTRUMENT_INACTIVE"
    if i.multiplier is None or i.multiplier <= 0:
        return False, "INVALID_MULTIPLIER"
    if i.min_trade_amount is None or i.min_trade_amount <= 0:
        return False, "INVALID_MIN_TRADE_AMOUNT"
    dte = days_until(i.expiry, now)
    if not (cfg.minimum_days_to_expiry <= dte <= cfg.maximum_days_to_expiry):
        return False, "DTE_OUT_OF_RANGE"
    problem = quote_problem(oq.quote, now, cfg)
    if problem:
        return False, problem
    sp = oq.quote.spread_pct
    if sp is None or sp > cfg.max_spread_pct:
        return False, "SPREAD_TOO_WIDE"
    return True, "OK"


def relevant_chain(chain, spot: Decimal, now: datetime, cfg: Config):
    band = Decimal(str(cfg.relevant_moneyness_band))
    out = []
    for oq in chain:
        i = oq.instrument
        if i.expiry is None or i.strike is None or i.strike <= 0:
            out.append(oq)      # malformed records count against quality
            continue
        if not (cfg.minimum_days_to_expiry <= days_until(i.expiry, now) <= cfg.maximum_days_to_expiry):
            continue
        if abs(i.strike / spot - 1) <= band:
            out.append(oq)
    return out


def data_quality(chain, spot: Decimal, now: datetime, cfg: Config) -> float:
    rel = relevant_chain(chain, spot, now, cfg)
    if not rel:
        return 0.0
    ok = sum(1 for oq in rel if option_is_eligible(oq, now, cfg)[0])
    return ok / len(rel)
