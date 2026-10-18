import random
from decimal import Decimal as D
from regimevault.models import CALL, PUT, Candidate
from regimevault.payoff import price_candidate, analyze_payoff, payoff_at_expiry, pnl_at_expiry
from regimevault.config import Config
from tests.helpers import *


def test_bull_put_credit_spread_known_numbers():
    c = bull_put()                                  # short 100P @2, long 95P @1, width 5
    p = price_candidate(c, D(100), NOW, ZERO_COST)
    assert p.is_valid and p.net_debit == D(-1)      # credit of 1
    r = analyze_payoff(c, p, ZERO_COST)
    assert r.is_valid and r.max_profit == D(1) and r.max_loss == D(4)
    assert r.collateral_required == D(5)            # width, gross of credit


def test_payoff_below_between_above_all_strikes():
    c = bull_put()
    p = price_candidate(c, D(100), NOW, ZERO_COST)
    assert pnl_at_expiry(c, p, D(50)) == D(-4)      # below every strike
    assert pnl_at_expiry(c, p, D(97)) == D(-2)      # between strikes
    assert pnl_at_expiry(c, p, D(150)) == D(1)      # above every strike


def test_iron_condor_bounds():
    c = iron_condor()
    p = price_candidate(c, D(100), NOW, ZERO_COST)
    r = analyze_payoff(c, p, ZERO_COST)
    assert p.net_debit == D(-2)                      # +2+2-1-1 credit
    assert r.max_profit == D(2) and r.max_loss == D(3)


def test_fees_and_slippage_make_pricing_more_conservative():
    c = bull_put(short_bid=1.9, short_ask=2.1, long_bid=0.9, long_ask=1.1)
    cheap = price_candidate(c, D(100), NOW, ZERO_COST)
    real = price_candidate(c, D(100), NOW, Config())
    assert real.net_debit > cheap.net_debit and real.entry_fees > 0 and real.slippage_cost > 0
    assert real.exec_prices["X-P-100"] <= D("1.9") and real.exec_prices["X-P-95"] >= D("1.1")


def test_contract_multiplier_scales_everything():
    p1 = price_candidate(bull_put(), D(100), NOW, ZERO_COST)
    p10 = price_candidate(bull_put(mult="10"), D(100), NOW, ZERO_COST)
    r10 = analyze_payoff(bull_put(mult="10"), p10, ZERO_COST)
    assert p10.net_debit == p1.net_debit * 10 and r10.max_loss == D(40)


def test_missing_multiplier_is_invalid():
    c = bull_put(mult=None)
    p = price_candidate(c, D(100), NOW, ZERO_COST)
    assert not p.is_valid and not analyze_payoff(c, p, ZERO_COST).is_valid


def test_unsupported_payoff_model_rejected():
    c = bull_put(model="INVERSE_COIN_RAW")
    p = price_candidate(c, D(100), NOW, ZERO_COST)
    assert analyze_payoff(c, p, ZERO_COST).reason == "PAYOFF_MODEL_NOT_SUPPORTED"


def test_naked_short_call_has_unbounded_loss_and_is_rejected():
    c = Candidate("X", "BTC", (leg(CALL, 100, -1, 2, 2),))
    p = price_candidate(c, D(100), NOW, ZERO_COST)
    assert analyze_payoff(c, p, ZERO_COST).reason == "UNBOUNDED_UPSIDE_LOSS"


def test_missing_leg_quote_stale_and_crossed_quotes_invalid():
    c = bull_put()
    missing = Candidate(c.strategy_type, "BTC", (c.legs[0], Leg(c.legs[1].instrument, c.legs[1].quantity, None)))
    assert not price_candidate(missing, D(100), NOW, ZERO_COST).is_valid
    stale = bull_put(ts=NOW - timedelta(seconds=60))
    assert "STALE" in price_candidate(stale, D(100), NOW, ZERO_COST).reason
    crossed = bull_put(short_bid=3, short_ask=2)
    assert "CROSSED" in price_candidate(crossed, D(100), NOW, ZERO_COST).reason


def test_short_leg_with_tiny_bid_and_slippage_gives_invalid_price():
    c = bull_put(short_bid=0, short_ask=1)
    assert price_candidate(c, D(100), NOW, Config()).reason == "INVALID_EXECUTION_PRICE"


def test_expiry_mismatch_invalid():
    a = leg(PUT, 100, -1, 2, 2)
    b = leg(PUT, 95, 1, 1, 1, expiry=EXPIRY + timedelta(days=7))
    c = Candidate("DEFINED_RISK_PUT_SPREAD", "BTC", (a, b))
    p = price_candidate(c, D(100), NOW, ZERO_COST)
    assert analyze_payoff(c, p, ZERO_COST).reason == "EXPIRY_MISMATCH"


def test_analytic_bounds_match_brute_force_grid_on_random_condors_and_spreads():
    rng = random.Random(1)
    for _ in range(200):
        ks = sorted(rng.sample(range(50, 150), 4))
        px = lambda: round(rng.uniform(0.5, 6), 2)
        kind = rng.choice(["condor", "putspread", "callspread"])
        if kind == "condor":
            legs = (leg(PUT, ks[0], 1, px(), px() + .5), leg(PUT, ks[1], -1, px(), px() + .5),
                    leg(CALL, ks[2], -1, px(), px() + .5), leg(CALL, ks[3], 1, px(), px() + .5))
        elif kind == "putspread":
            legs = (leg(PUT, ks[0], 1, px(), px() + .5), leg(PUT, ks[1], -1, px(), px() + .5))
        else:
            legs = (leg(CALL, ks[0], -1, px(), px() + .5), leg(CALL, ks[1], 1, px(), px() + .5))
        c = Candidate("T", "BTC", legs)
        p = price_candidate(c, D(100), NOW, Config())
        if not p.is_valid:
            continue
        r = analyze_payoff(c, p, Config())
        grid = [pnl_at_expiry(c, p, D(s) / 4) for s in range(0, 4 * 400)]
        if r.is_valid:
            assert -r.max_loss == min(grid) and r.max_profit == max(grid)
        else:
            assert r.reason in ("NON_POSITIVE_MAX_LOSS_SUSPECT_QUOTES",), r.reason
            assert min(grid) >= 0
