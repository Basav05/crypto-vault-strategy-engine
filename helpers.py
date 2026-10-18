from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from regimevault.config import Config
from regimevault.engine import Engine
from regimevault.market_data import SyntheticProvider
from regimevault.models import Instrument, Leg, Candidate, Quote, CALL, PUT
from regimevault.store import Store

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
EXPIRY = NOW + timedelta(days=10)
ZERO_COST = Config(fee_rate_underlying=D(0), fee_cap_option_pct=D(0), slippage_spread_fraction=D(0))


def expect_raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


def inst(otype, strike, mult="1", model="LINEAR_USD", expiry=EXPIRY, lot="0.1", active=True):
    return Instrument(f"X-{otype[0]}-{strike}", "BTC", otype, D(str(strike)), expiry,
                      None if mult is None else D(mult), D(lot), "USD", "USD", active, model)


def quote(bid, ask, ts=NOW, bsz=None, asz=None):
    return Quote(D(str(bid)), D(str(ask)), ts, None if bsz is None else D(str(bsz)), None if asz is None else D(str(asz)))


def leg(otype, strike, qty, bid, ask, **kw):
    q = quote(bid, ask, kw.pop("ts", NOW))
    return Leg(inst(otype, strike, **kw), D(str(qty)), q)


def bull_put(short_bid=2, short_ask=2, long_bid=1, long_ask=1, short_k=100, long_k=95, **kw):
    return Candidate("DEFINED_RISK_PUT_SPREAD", "BTC",
                     (leg(PUT, short_k, -1, short_bid, short_ask, **kw), leg(PUT, long_k, 1, long_bid, long_ask, **kw)))


def iron_condor():
    return Candidate("IRON_CONDOR", "BTC", (
        leg(PUT, 90, 1, 1, 1), leg(PUT, 95, -1, 2, 2), leg(CALL, 105, -1, 2, 2), leg(CALL, 110, 1, 1, 1)))


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def make_engine(scenario="range", cfg=None, db=":memory:", seed=7, provider=None, clock=None, **prov_kw):
    cfg = cfg or Config()
    clock = clock or Clock()
    provider = provider or SyntheticProvider(cfg, scenario, seed=seed, clock=clock, **prov_kw)
    return Engine(cfg, provider, Store(db), clock=clock), clock


def run_cycles(engine, clock, n):
    out = []
    for _ in range(n):
        out.append(engine.run_decision_cycle())
        clock.advance(minutes=1)
    return out
