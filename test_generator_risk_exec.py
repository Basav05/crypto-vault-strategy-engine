from decimal import Decimal as D
from regimevault.config import Config
from regimevault.execution import simulate_open_fill, simulate_close_fill, FILLED, NOT_FILLED
from regimevault.generator import generate_candidates, validate_candidate_structure
from regimevault.market_data import SyntheticProvider
from regimevault.models import Candidate, Leg, CALL, PUT
from regimevault.payoff import price_candidate
from regimevault.positions import PaperAccount, Position
from regimevault.regime import BULLISH, BEARISH, RANGE, HIGH_VOL, UNCERTAIN
from regimevault.risk import calculate_position_size, portfolio_risk_gate, floor_to_lot
from tests.helpers import *

CFG = Config()


def test_structure_validation_catches_wrong_direction_and_strike_order():
    good = bull_put()
    assert validate_candidate_structure(good) == ""
    flipped = Candidate(good.strategy_type, "BTC", tuple(Leg(l.instrument, -l.quantity, l.quote) for l in good.legs))
    assert validate_candidate_structure(flipped) != ""
    assert validate_candidate_structure(Candidate("IRON_CONDOR", "BTC", iron_condor().legs[:3])) != ""
    one_missing_quote = Candidate(good.strategy_type, "BTC", (good.legs[0], Leg(good.legs[1].instrument, good.legs[1].quantity, None)))
    assert validate_candidate_structure(one_missing_quote) == "LEG_QUOTE_MISSING"


def test_generator_maps_regimes_to_strategies_and_skips_high_vol_uncertain():
    snap = SyntheticProvider(CFG, "range", clock=Clock()).get_snapshot()
    chain, spot = snap.chains["BTC"], snap.spot["BTC"].index_price
    kinds = lambda r: {c.strategy_type for c in generate_candidates(r, "BTC", spot, chain, snap.timestamp, CFG)[0]}
    assert kinds(BULLISH) == {"DEFINED_RISK_PUT_SPREAD", "BULL_CALL_DEBIT_SPREAD"}
    assert kinds(BEARISH) == {"DEFINED_RISK_CALL_SPREAD", "BEAR_PUT_DEBIT_SPREAD"}
    assert kinds(RANGE) == {"IRON_CONDOR"}
    assert kinds(HIGH_VOL) == set() and kinds(UNCERTAIN) == set()


def test_generated_candidates_use_real_instruments_same_expiry_valid_structure():
    snap = SyntheticProvider(CFG, "range", clock=Clock()).get_snapshot()
    listed = {o.instrument.instrument_id for o in snap.chains["BTC"]}
    cands, _ = generate_candidates(RANGE, "BTC", snap.spot["BTC"].index_price, snap.chains["BTC"], snap.timestamp, CFG)
    assert cands
    for c in cands:
        assert validate_candidate_structure(c) == ""
        assert all(l.instrument.instrument_id in listed for l in c.legs)


def test_generator_rejects_instead_of_crashing_when_a_leg_has_no_quote():
    snap = SyntheticProvider(CFG, "range", clock=Clock()).get_snapshot()
    chain = [o if o.instrument.option_type != PUT else type(o)(o.instrument, None, o.iv) for o in snap.chains["BTC"]]
    cands, rejected = generate_candidates(BULLISH, "BTC", snap.spot["BTC"].index_price, chain, snap.timestamp, CFG)
    assert all(c.strategy_type == "BULL_CALL_DEBIT_SPREAD" for c in cands)
    assert any(r["reason"] == "LEG_QUOTE_MISSING" for r in rejected)


def test_sizing_floors_to_lot_and_respects_budget():
    assert floor_to_lot(D("1.99"), D("0.1")) == D("1.9")
    units, why = calculate_position_size(D(100), D(500), D("0.1"), D(100000), D(100000), CFG)
    assert units == D("5.0") and why == "OK"                       # 0.5% of 100k = 500 risk / 100
    assert calculate_position_size(D(100000), D(500000), D("0.1"), D(100000), D(100000), CFG)[1] == "SIZE_BELOW_EXCHANGE_MINIMUM"
    assert calculate_position_size(D(0), D(1), D("0.1"), D(100000), D(100000), CFG)[1] == "INVALID_MAX_LOSS"
    assert calculate_position_size(D(100), D(500), D("0.1"), D(0), D(0), CFG)[1] == "INVALID_NAV"
    assert calculate_position_size(D(100), D(500), D("0.1"), D(100000), D(100), CFG)[0] == D("0.2")   # shrinks to fit collateral
    assert calculate_position_size(D(100), D(500), D("0.1"), D(100000), D(40), CFG)[1] == "INSUFFICIENT_COLLATERAL"


def _pos(max_loss=500, collateral=2000, underlying="BTC", key="k"):
    return Position("p", underlying, "IRON_CONDOR", (), D(1), {}, D(0), D(0), D(100), D(max_loss), D(collateral),
                    EXPIRY, NOW, "RANGE", key, state="OPEN")


def _failed(checks):
    return {c["check"] for c in checks if not c["passed"]}


def test_risk_gate_vetoes():
    acct, nav = PaperAccount(D(100000)), D(100000)
    ok = portfolio_risk_gate("c", "BTC", D(400), D(1000), acct, [], nav, CFG, "new")
    assert _failed(ok) == set()
    assert "PER_TRADE_RISK_LIMIT" in _failed(portfolio_risk_gate("c", "BTC", D(900), D(1000), acct, [], nav, CFG, "new"))
    three = [_pos(key=str(i), underlying="ETH") for i in range(3)]
    assert "POSITION_COUNT_LIMIT" in _failed(portfolio_risk_gate("c", "BTC", D(400), D(1000), acct, three, nav, CFG, "new"))
    heavy = [_pos(max_loss=800, key="a"), _pos(max_loss=800, key="b")]
    assert "TOTAL_RISK_LIMIT" in _failed(portfolio_risk_gate("c", "ETH", D(500), D(1000), acct, heavy, nav, CFG, "new"))
    assert "TOTAL_RISK_LIMIT" not in _failed(portfolio_risk_gate("c", "ETH", D(400), D(1000), acct, heavy, nav, CFG, "new"))   # exactly at limit is allowed
    assert "UNDERLYING_POSITION_LIMIT" in _failed(portfolio_risk_gate("c", "BTC", D(100), D(100), acct, heavy, nav, CFG, "new"))
    assert "NO_DUPLICATE_EXPOSURE" in _failed(portfolio_risk_gate("c", "ETH", D(100), D(100), acct, [_pos(key="same")], nav, CFG, "same"))
    assert "VALID_MAX_LOSS" in _failed(portfolio_risk_gate("c", "BTC", None, D(10), acct, [], nav, CFG, "new"))
    assert "COLLATERAL_KNOWN" in _failed(portfolio_risk_gate("c", "BTC", D(10), None, acct, [], nav, CFG, "new"))
    assert "SUFFICIENT_COLLATERAL" in _failed(portfolio_risk_gate("c", "BTC", D(10), D(10 ** 6), acct, [], nav, CFG, "new"))
    assert "DAILY_LOSS_LIMIT" in _failed(portfolio_risk_gate("c", "BTC", D(10), D(10), acct, [], D(97000), CFG, "new"))
    acct.peak_nav = D(110000)
    assert "DRAWDOWN_LIMIT" in _failed(portfolio_risk_gate("c", "BTC", D(10), D(10), acct, [], D(100000), CFG, "new"))


def test_conservative_fills_buy_at_or_above_ask_sell_at_or_below_bid():
    c = bull_put(short_bid=1.9, short_ask=2.1, long_bid=0.9, long_ask=1.1)
    sized = c.scaled(D(2))
    q = {l.instrument.instrument_id: l.quote for l in c.legs}
    fill = simulate_open_fill(sized.legs, q.get, D(100), NOW, CFG, D(10))
    assert fill.status == FILLED and fill.simulated
    assert fill.prices["X-P-95"] >= q["X-P-95"].ask          # buy
    assert fill.prices["X-P-100"] <= q["X-P-100"].bid        # sell
    close = simulate_close_fill(sized.legs, q.get, D(100), NOW, CFG)
    assert close.prices["X-P-95"] <= q["X-P-95"].bid and close.prices["X-P-100"] >= q["X-P-100"].ask


def test_fill_refused_on_stale_missing_adverse_or_thin_quotes():
    c = bull_put(short_bid=1.9, short_ask=2.1, long_bid=0.9, long_ask=1.1)
    legs = c.scaled(D(1)).legs
    exp = price_candidate(c, D(100), NOW, CFG).net_debit
    q = {l.instrument.instrument_id: l.quote for l in c.legs}
    assert simulate_open_fill(legs, q.get, D(100), NOW + timedelta(seconds=60), CFG, exp).reason.startswith("STALE_OR_INVALID_QUOTE")
    assert simulate_open_fill(legs, {}.get, D(100), NOW, CFG, exp).reason.startswith("STALE_OR_INVALID_QUOTE")
    adverse = dict(q)
    adverse["X-P-100"] = quote(1.0, 1.2)                      # short leg's bid collapses
    r = simulate_open_fill(legs, adverse.get, D(100), NOW, CFG, exp)
    assert r.status == NOT_FILLED and r.reason == "LIMIT_PRICE_NOT_REACHED"
    thin = dict(q)
    thin["X-P-95"] = quote(0.9, 1.1, asz=0.5)
    assert simulate_open_fill(c.scaled(D(2)).legs, thin.get, D(100), NOW, CFG, D(99)).reason == "INSUFFICIENT_SIMULATED_DEPTH"
    assert simulate_open_fill(legs, q.get, D(100), NOW, Config(require_depth_info=True), exp).reason == "DEPTH_UNKNOWN"
