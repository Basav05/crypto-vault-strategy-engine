import os, tempfile
from dataclasses import replace
from decimal import Decimal as D
from regimevault.config import Config
from regimevault.engine import Engine, EngineDisabled
from regimevault.market_data import SyntheticProvider
from regimevault import positions as P
from regimevault.store import Store
from tests.helpers import *


def opened(decisions):
    return [d for batch in decisions for d in batch if d.action == "PAPER_OPEN"]


def test_warmup_then_entries_and_everything_is_logged():
    e, clk = make_engine("range")
    out = run_cycles(e, clk, 4)
    assert all(d.action == "NO_TRADE" for d in out[0] + out[1])           # hysteresis warm-up
    assert opened(out)
    n = lambda t: e.store.db.execute(f"select count(*) from {t}").fetchone()[0]
    assert n("decisions") == 8 and n("regime_decisions") == 8 and n("risk_checks") > 0
    assert n("paper_orders") >= len(opened(out)) and n("position_events") >= 3 * len(opened(out))
    assert n("configuration_versions") == 1
    d = opened(out)[0]
    assert d.selected_legs and D(d.max_loss) > 0 and D(d.collateral_required) > 0 and d.risk_checks


def test_stale_market_data_means_no_trade():
    class Stale:
        def __init__(self, inner): self.inner = inner
        def get_snapshot(self):
            s = self.inner.get_snapshot()
            for u in s.spot:
                s.spot[u] = type(s.spot[u])(u, s.spot[u].index_price, s.timestamp - timedelta(seconds=300))
            return s
    clk = Clock(); cfg = Config()
    e, _ = make_engine(provider=Stale(SyntheticProvider(cfg, "range", clock=clk)), clock=clk)
    out = run_cycles(e, clk, 5)
    assert all(d.action == "NO_TRADE" and d.reason_code == "DATA_VALIDATION_FAILED" for b in out for d in b)


def test_stale_option_quotes_means_no_trade():
    e, clk = make_engine("range", quote_age_seconds=120)
    assert not opened(run_cycles(e, clk, 6))


def test_data_outage_is_reported_not_hidden():
    class Down:
        def get_snapshot(self): raise ConnectionError("down")
    e, clk = make_engine(provider=Down())
    out = run_cycles(e, clk, 2)
    assert all(d.reason_code == "DATA_OUTAGE" for b in out for d in b)
    assert e.store.rows("engine_events")[0]["event_type"] == "DATA_OUTAGE"


def test_unsafe_configuration_refuses_to_start():
    for bad in (Config(allow_live_orders=True), Config(environment="PRODUCTION"),
                Config(max_risk_per_trade_pct_nav=D("1.5")), Config(allowed_strategies=("NAKED_CALL",))):
        expect_raises(EngineDisabled, Engine, bad, None, Store(), Clock())


def test_high_vol_never_trades():
    e, clk = make_engine("highvol")
    out = run_cycles(e, clk, 6)
    assert not opened(out) and all(d.regime == "HIGH_VOL" for b in out for d in b)


def test_all_candidates_rejected_gives_no_trade_and_records_why():
    e, clk = make_engine("range", cfg=Config(minimum_candidate_score=0.999))
    out = run_cycles(e, clk, 5)
    assert not opened(out)
    last = out[-1][0]
    assert last.reason_code == "ALL_CANDIDATES_REJECTED" and last.rejected_candidates
    assert all("SCORE_BELOW_MINIMUM" in r["reason"] or r["reason"] for r in last.rejected_candidates)


def test_risk_budget_too_small_for_exchange_minimum_means_no_trade():
    e, clk = make_engine("range", cfg=Config(max_risk_per_trade_pct_nav=D("0.00001")))
    assert not opened(run_cycles(e, clk, 5))


def test_portfolio_limits_hold_over_many_cycles_and_scenarios():
    for sc in ("bull", "bear", "range"):
        e, clk = make_engine(sc)
        run_cycles(e, clk, 15)
        live = e.open_positions()
        assert len(live) <= e.cfg.max_open_positions
        assert sum(p.max_loss for p in live) <= D("100000") * e.cfg.max_total_open_risk_pct_nav
        for u in e.cfg.underlyings:
            assert sum(1 for p in live if p.underlying == u) <= e.cfg.max_positions_per_underlying
        assert len({p.exposure_key for p in live}) == len(live)
        assert all(p.max_loss <= D("100000") * e.cfg.max_risk_per_trade_pct_nav for p in live)


def test_kill_switch_blocks_entries_and_closes_open_positions():
    e, clk = make_engine("range")
    run_cycles(e, clk, 4)
    assert e.open_positions()
    e.store.set_kill_switch(True)
    out = run_cycles(e, clk, 2)
    assert all(d.reason_code == "KILL_SWITCH_ACTIVE" for b in out for d in b)
    assert not e.open_positions()
    closed = [p for p in e.positions if p.state == P.CLOSED]
    assert closed and all(p.events[-1]["reason"] == "KILL_SWITCH" for p in closed)
    assert all(p.realized_pnl is not None for p in closed)
    assert e.account.realized_pnl == sum(p.realized_pnl for p in closed)


def test_vanishing_quotes_mark_position_for_review_and_block_new_entries():
    class Vanish:
        def __init__(self, inner): self.inner, self.on = inner, False
        def get_snapshot(self):
            s = self.inner.get_snapshot()
            if self.on:
                for u in s.chains:
                    s.chains[u] = [type(o)(o.instrument, None, o.iv) for o in s.chains[u]]
            return s
    clk = Clock(); prov = Vanish(SyntheticProvider(Config(), "range", clock=clk))
    e, _ = make_engine(provider=prov, clock=clk)
    run_cycles(e, clk, 4)
    n_open = len(e.open_positions())
    assert n_open
    prov.on = True
    out = run_cycles(e, clk, 1)[0]
    assert all(d.action == "NO_TRADE" and d.reason_code == "UNRESOLVED_POSITION" for d in out)
    assert all(p.needs_review for p in e.open_positions()) and len(e.open_positions()) == n_open


def test_circuit_breaker_blocks_entries():
    e, clk = make_engine("range")
    e.account.peak_nav = D("200000")
    out = run_cycles(e, clk, 5)
    assert not opened(out) and out[-1][0].reason_code == "DRAWDOWN_OR_DAILY_LOSS_LIMIT"


def test_restart_recovery_restores_positions_account_and_blocks_duplicates():
    path = os.path.join(tempfile.mkdtemp(), "rv.db")
    e1, clk = make_engine("range", db=path)
    run_cycles(e1, clk, 4)
    ids = sorted(p.position_id for p in e1.open_positions())
    assert ids
    realized = e1.account.realized_pnl
    e1.store.close()
    e2, _ = make_engine("range", db=path, clock=clk)
    assert sorted(p.position_id for p in e2.open_positions()) == ids
    assert e2.account.realized_pnl == realized
    run_cycles(e2, clk, 4)                     # regime tracker restarts in UNCERTAIN -> safe warm-up
    keys = [p.exposure_key for p in e2.open_positions()]
    assert len(keys) == len(set(keys))


def test_expiry_is_settled_explicitly():
    e, clk = make_engine("range")
    run_cycles(e, clk, 4)
    before = [p for p in e.open_positions()]
    assert before
    clk.advance(days=40)
    e.run_decision_cycle()
    for p in before:
        assert p.state in (P.EXPIRED, P.CLOSED)
        assert p.realized_pnl is not None
    expired = [p for p in before if p.state == P.EXPIRED]
    assert all("SETTLEMENT" in p.events[-1]["reason"] for p in expired)


def test_deterministic_given_same_inputs():
    seq = []
    for _ in range(2):
        e, clk = make_engine("bull", seed=11)
        seq.append([(d.underlying, d.action, d.selected_strategy, d.units, d.candidate_score)
                    for b in run_cycles(e, clk, 5) for d in b])
    assert seq[0] == seq[1]


def test_kill_switch_state_survives_restart():
    path = os.path.join(tempfile.mkdtemp(), "rv.db")
    e1, clk = make_engine("range", db=path)
    e1.store.set_kill_switch(True)
    e1.store.close()
    e2, _ = make_engine("range", db=path, clock=clk)
    assert e2.store.kill_switch_on()
    assert not opened(run_cycles(e2, clk, 5))
