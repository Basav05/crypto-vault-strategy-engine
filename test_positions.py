from decimal import Decimal as D
from regimevault.config import Config
from regimevault import positions as P
from regimevault.regime import BULLISH, BEARISH, HIGH_VOL, RANGE
from tests.helpers import *

CFG = Config()


def mk(state=P.OPEN, strategy="DEFINED_RISK_PUT_SPREAD"):
    c = bull_put(short_bid=1.9, short_ask=2.1, long_bid=0.9, long_ask=1.1).scaled(D(1))
    return P.Position("p1", "BTC", strategy, tuple(Leg_(l) for l in c.legs), D(1), {"X-P-100": D("1.9")},
                      D("-1"), D("0.1"), D(1), D(4), D(5), EXPIRY, NOW, "BULLISH", "key", state=state)


def Leg_(l):
    from regimevault.models import Leg
    return Leg(l.instrument, l.quantity, None)


def test_legal_path_and_audit_events():
    p = mk(P.PROPOSED)
    for s in (P.APPROVED, P.PAPER_PENDING, P.OPEN, P.EXIT_TRIGGERED, P.PAPER_CLOSING, P.CLOSED):
        p.transition(s, "test", NOW)
    assert [e["to"] for e in p.events] == ["APPROVED", "PAPER_PENDING", "OPEN", "EXIT_TRIGGERED", "PAPER_CLOSING", "CLOSED"]
    assert all({"time", "from", "to", "reason", "snapshot"} <= set(e) for e in p.events)


def test_illegal_transitions_raise():
    for start, bad in [(P.OPEN, P.CLOSED), (P.CLOSED, P.OPEN), (P.PROPOSED, P.OPEN), (P.EXPIRED, P.OPEN),
                       (P.PAPER_PENDING, P.CLOSED), (P.REJECTED, P.APPROVED)]:
        expect_raises(P.IllegalTransition, mk(start).transition, bad, "x", NOW)
    mk(P.UNRESOLVED).transition(P.OPEN, "reconciled", NOW)        # allowed only from UNRESOLVED
    mk(P.UNRESOLVED).transition(P.CLOSED, "reconciled", NOW)


def test_serialization_roundtrip():
    p = mk()
    p.transition(P.EXIT_TRIGGERED, "r", NOW)
    q = P.Position.from_dict(p.to_dict())
    assert (q.position_id, q.state, q.net_debit, q.max_loss, q.expiry, q.exposure_key) == \
           (p.position_id, p.state, p.net_debit, p.max_loss, p.expiry, p.exposure_key)
    assert [l.quantity for l in q.legs] == [l.quantity for l in p.legs] and q.events == p.events


def test_exit_priority_order():
    p = mk()
    far = NOW                                                    # 10 days to expiry
    f = lambda pnl, regime=RANGE, kill=False, cb=False, now=far: P.exit_reason(p, D(pnl), now, regime, kill, cb, CFG)
    assert f(0) is None
    assert f(-100, BEARISH, True, True) == "KILL_SWITCH"
    assert f(-100, BEARISH, False, True) == "PORTFOLIO_CIRCUIT_BREAKER"
    assert f(-3, BEARISH) == "MAX_LOSS_TRIGGER"                  # 75% of max loss 4 = 3
    assert f(-1, BEARISH) == "THESIS_INVALIDATION"
    assert f(-1, BULLISH, now=EXPIRY - timedelta(hours=1)) == "TIME_EXIT"
    assert f("0.6") == "PROFIT_TARGET"                           # 50% of max profit 1 = 0.5
    assert f(-1, HIGH_VOL) == "THESIS_INVALIDATION"


def test_mark_to_market_none_when_quotes_missing_and_value_when_present():
    p = mk()
    q = {"X-P-100": quote(1.9, 2.1), "X-P-95": quote(0.9, 1.1)}
    assert P.mark_to_market(p, q.get, D(100), NOW, CFG) is not None
    assert P.mark_to_market(p, {"X-P-100": q["X-P-100"]}.get, D(100), NOW, CFG) is None


def test_account_nav_drawdown_and_daily_loss():
    a = P.PaperAccount(D(100000))
    p = mk(); p.last_pnl = D(-500)
    nav = a.nav([p])
    assert nav == D(99500)
    a.roll(NOW, D(100000))
    assert a.daily_loss(nav) == D(500) and abs(a.drawdown(nav) - 0.005) < 1e-12
    a.realized_pnl = D(250)
    assert a.nav([]) == D(100250)
    assert P.PaperAccount.from_dict(a.to_dict()).realized_pnl == D(250)


def test_expiry_settlement_pnl_uses_payoff_net_debit_and_fees():
    p = mk()
    assert P.settlement_pnl(p, D(200)) == -p.net_debit - p.entry_fees     # all OTM: keep credit, pay fees
    assert P.settlement_pnl(p, D(50)) == D(-5) - p.net_debit - p.entry_fees
