"""Decision-cycle orchestration.  Order: safety -> monitor existing risk -> data -> validate ->
features -> regime -> candidates -> price/payoff -> hard filters -> score -> size -> risk veto ->
final gate -> conservative paper fill -> persist.  Any failure upstream => NO_TRADE."""
import uuid
from decimal import Decimal
from typing import Callable, Dict, List, Optional
from . import positions as P
from .config import Config, validate_config, config_hash, ENV_PAPER
from .execution import simulate_open_fill, simulate_close_fill, FILLED
from .features import calculate_features
from .generator import generate_candidates
from .models import Decision, MarketSnapshot, utcnow, leg_to_dict
from .payoff import price_candidate, analyze_payoff, payoff_at_expiry
from .regime import classify_market_regime, RegimeTracker, UNCERTAIN
from .risk import calculate_position_size, portfolio_risk_gate, final_pre_trade_gate
from .scoring import score_candidate
from .store import Store
from .validation import validate_spot, data_quality

ZERO = Decimal(0)


class EngineDisabled(Exception):
    pass


class Engine:
    def __init__(self, cfg: Config, provider, store: Store, clock: Callable = utcnow):
        errs = validate_config(cfg)
        if errs:
            raise EngineDisabled("Invalid configuration: " + ", ".join(errs))
        self.cfg, self.provider, self.store, self.clock = cfg, provider, store, clock
        self.cfg_hash = config_hash(cfg)
        self.trackers: Dict[str, RegimeTracker] = {u: RegimeTracker(cfg) for u in cfg.underlyings}
        self.entries_paused = False
        self.enabled = True
        failures = self.run_startup_self_tests()
        if failures:
            self.enabled = False
            store.log_event("STARTUP_FAILED", "CRITICAL", failures, clock())
            raise EngineDisabled("Startup self-tests failed: " + ", ".join(failures))
        self.account = store.load_account() or P.PaperAccount(cfg.initial_nav)
        self.positions: List[P.Position] = store.load_live_positions()      # restart recovery
        store.save_config_version(self.cfg_hash, {"rule_version": "rules", "cfg": str(cfg)}, clock())
        if any(p.state == P.UNRESOLVED or p.state in (P.PAPER_PENDING, P.PAPER_CLOSING, P.EXIT_TRIGGERED)
               for p in self.positions):
            store.log_event("RESTART_FOUND_UNRESOLVED", "WARNING", [p.position_id for p in self.positions], clock())

    # ---- self tests ---------------------------------------------------------------
    def run_startup_self_tests(self) -> list:
        from datetime import timedelta
        from .models import Instrument, Leg, Candidate, Quote, CALL, PUT
        failures = []
        now = self.clock()
        exp = now + timedelta(days=10)
        def inst(t, k): return Instrument(f"T-{t}-{k}", "TST", t, Decimal(k), exp, Decimal(1), Decimal("0.1"))
        q = Quote(Decimal(1), Decimal(1), now)
        c = Candidate("DEFINED_RISK_PUT_SPREAD", "TST",
                      (Leg(inst(PUT, 90), Decimal(1), q), Leg(inst(PUT, 100), Decimal(-1), q)))
        zero = Config(fee_rate_underlying=Decimal(0), fee_cap_option_pct=Decimal(0), slippage_spread_fraction=Decimal(0))
        pr = price_candidate(c, Decimal(100), now, zero)
        po = analyze_payoff(c, pr, zero)
        if not (po.is_valid and po.max_loss == Decimal(10) and pr.net_debit == Decimal(0)):
            failures.append("PAYOFF_SELF_TEST")
        if payoff_at_expiry(c, Decimal(50)) != Decimal(-10) or payoff_at_expiry(c, Decimal(200)) != Decimal(0):
            failures.append("EXPIRY_PAYOFF_SELF_TEST")
        return failures

    # ---- helpers ------------------------------------------------------------------
    def _no_trade(self, now, cycle_id, u, regime, conf, code, why, **kw) -> Decision:
        d = Decision(now, u, regime, conf, "NO_TRADE", code, why, cycle_id, self.cfg_hash, **kw)
        self.store.save_decision(d)
        return d

    def live_positions(self):
        return [p for p in self.positions if p.state in P.LIVE_STATES]

    def open_positions(self):
        return [p for p in self.positions if p.state == P.OPEN]

    def unresolved_exists(self):
        return any(p.state in (P.UNRESOLVED, P.PAPER_PENDING, P.PAPER_CLOSING, P.EXIT_TRIGGERED) or p.needs_review
                   for p in self.positions if p.state in P.LIVE_STATES)

    def current_nav(self) -> Decimal:
        return self.account.nav(self.live_positions())

    def circuit_breaker(self) -> bool:
        nav = self.current_nav()
        return (self.account.drawdown(nav) >= float(self.cfg.max_drawdown_pct_nav) or
                self.account.daily_loss(nav) >= self.account.start_of_day_nav * self.cfg.max_daily_loss_pct_nav)

    def _persist(self, now):
        for p in self.positions:
            self.store.save_position(p, now)
        self.store.save_account(self.account)

    # ---- position monitoring ------------------------------------------------------
    def monitor_positions(self, snap: Optional[MarketSnapshot], regimes: Dict[str, str]):
        now = self.clock()
        kill = self.store.kill_switch_on()
        for p in list(self.open_positions()):
            spot = snap.spot.get(p.underlying) if snap else None
            if snap is None or spot is None or spot.index_price <= 0:
                p.needs_review = True
                self.entries_paused = True
                self.store.log_event("POSITION_DATA_UNAVAILABLE", "ALERT", p.position_id, now)
                continue
            quote_for = snap.quote_lookup()
            if now >= p.expiry:                                   # explicit expiry handling
                pnl = P.settlement_pnl(p, spot.index_price)
                p.transition(P.EXPIRED, "EXPIRY_SETTLEMENT_APPROXIMATED_BY_INDEX",
                             now, {"settlement_price": str(spot.index_price)})
                p.realized_pnl, p.last_pnl = pnl, ZERO
                self.account.realized_pnl += pnl
                continue
            pnl = P.mark_to_market(p, quote_for, spot.index_price, now, self.cfg)
            if pnl is None:                                       # quotes vanished -> unresolved, block entries
                p.needs_review = True
                self.entries_paused = True
                if kill:
                    p.transition(P.UNRESOLVED, "KILL_SWITCH_BUT_NO_QUOTES", now)
                self.store.log_event("POSITION_MTM_UNAVAILABLE", "ALERT", p.position_id, now)
                continue
            p.needs_review = False
            p.last_pnl = pnl
            nav = self.current_nav()
            self.account.roll(now, nav)
            reason = P.exit_reason(p, pnl, now, regimes.get(p.underlying, UNCERTAIN), kill,
                                   self.circuit_breaker(), self.cfg)
            if reason:
                self._close(p, reason, quote_for, spot.index_price, now)
        self.account.roll(now, self.current_nav())

    def _close(self, p: P.Position, reason: str, quote_for, spot: Decimal, now):
        p.transition(P.EXIT_TRIGGERED, reason, now, {"pnl": str(p.last_pnl)})
        p.transition(P.PAPER_CLOSING, "CLOSE_REQUESTED", now)
        fill = simulate_close_fill(p.legs, quote_for, spot, now, self.cfg)
        if fill.status != FILLED:
            p.transition(P.UNRESOLVED, fill.reason, now)
            self.entries_paused = True
            self.store.log_event("CLOSE_UNRESOLVED", "ALERT", {"position": p.position_id, "why": fill.reason}, now)
            return
        pnl = fill.cashflow - fill.fees - p.net_debit
        p.transition(P.CLOSED, reason, now, {"realized_pnl": str(pnl), "fill_prices": {k: str(v) for k, v in fill.prices.items()}})
        p.realized_pnl, p.last_pnl = pnl, ZERO
        self.account.realized_pnl += pnl

    # ---- main cycle ---------------------------------------------------------------
    def run_decision_cycle(self) -> List[Decision]:
        if not self.enabled:
            raise EngineDisabled("Engine disabled")
        cycle_id = uuid.uuid4().hex[:12]
        now = self.clock()
        decisions: List[Decision] = []
        self.entries_paused = False

        def all_no_trade(code, why):
            for u in self.cfg.underlyings:
                decisions.append(self._no_trade(now, cycle_id, u, UNCERTAIN, 0.0, code, why))
            self._persist(now)
            return decisions

        # A. safety / environment
        if self.cfg.environment != ENV_PAPER or self.cfg.allow_live_orders:
            return all_no_trade("UNSAFE_ENVIRONMENT", "Environment is not TESTNET_PAPER_ONLY.")

        # B. data
        try:
            snap = self.provider.get_snapshot()
        except Exception as exc:                                  # data outage is reported, never hidden
            self.store.log_event("DATA_OUTAGE", "ALERT", f"{type(exc).__name__}: {exc}", now)
            self.monitor_positions(None, {})
            return all_no_trade("DATA_OUTAGE", f"Market data unavailable ({type(exc).__name__}).")
        now = self.clock()

        # monitoring first: manage existing risk before any new entry
        current_regimes = {u: t.current for u, t in self.trackers.items()}
        spot_errs = validate_spot(snap, self.cfg)
        self.monitor_positions(None if spot_errs else snap, current_regimes)

        if self.store.kill_switch_on():
            self._persist(now)
            return all_no_trade("KILL_SWITCH_ACTIVE", "Kill switch is on.")
        if spot_errs:
            self.store.log_event("DATA_VALIDATION_FAILED", "WARNING", spot_errs, now)
            return all_no_trade("DATA_VALIDATION_FAILED", "; ".join(spot_errs))

        nav = self.current_nav()
        self.account.roll(now, nav)
        if self.circuit_breaker():
            self.entries_paused = True
            self.store.log_event("CIRCUIT_BREAKER", "ALERT", {"nav": str(nav)}, now)
            return all_no_trade("DRAWDOWN_OR_DAILY_LOSS_LIMIT", "Portfolio circuit breaker active.")
        if self.unresolved_exists():
            return all_no_trade("UNRESOLVED_POSITION", "Unresolved/needs-review position blocks new entries.")

        for u in self.cfg.underlyings:
            decisions.append(self._cycle_for(u, snap, now, cycle_id))
        self.store.commit()
        self._persist(now)
        return decisions

    def _cycle_for(self, u: str, snap: MarketSnapshot, now, cycle_id: str) -> Decision:
        cfg = self.cfg
        spot = snap.spot[u].index_price
        chain = snap.chains.get(u, [])
        feats = calculate_features(u, spot, snap.prices.get(u, []), chain, snap.trades.get(u), now, cfg)
        raw = classify_market_regime(feats, cfg)
        label = self.trackers[u].update(raw)
        confidence = raw.confidence if label == raw.label else 0.0
        self.store.save_regime(now, u, label, raw.label, raw.confidence,
                               {"features": feats.values, "quality": feats.data_quality, "reasons": raw.reasons})
        self.store.save_snapshot_summary(now, u, snap.source, {"spot": str(spot), "chain_size": len(chain),
                                                                "data_quality": feats.data_quality})
        base = dict(regime=label, confidence=confidence)
        if label not in ("BULLISH", "BEARISH", "RANGE"):
            return self._no_trade(now, cycle_id, u, label, confidence, f"REGIME_{label}",
                                  f"Regime {label} (raw {raw.label}): " + "; ".join(raw.reasons))

        cands, rejected = generate_candidates(label, u, spot, chain, now, cfg)
        survivors = []
        for c in cands:
            cid = c.candidate_id
            pr = price_candidate(c, spot, now, cfg)
            po = analyze_payoff(c, pr, cfg)
            if not po.is_valid:
                rejected.append({"candidate_id": cid, "reason": po.reason}); continue
            if po.max_profit / po.max_loss < cfg.min_reward_to_risk:
                rejected.append({"candidate_id": cid, "reason": "REWARD_RISK_BELOW_MINIMUM"}); continue
            sc = score_candidate(c, label, confidence, feats, pr, po, cfg)
            if sc["total"] < cfg.minimum_candidate_score:
                rejected.append({"candidate_id": cid, "reason": f"SCORE_BELOW_MINIMUM:{sc['total']}"}); continue
            survivors.append((sc["total"], c, pr, po, sc))
        survivors.sort(key=lambda t: t[0], reverse=True)

        nav = self.current_nav()
        live = self.live_positions()
        collateral_used = sum((p.collateral for p in live), ZERO)
        chosen = None
        for total, c, pr, po, sc in survivors:
            cid = c.candidate_id
            lot = min(l.instrument.min_trade_amount for l in c.legs)
            units, why = calculate_position_size(po.max_loss, po.collateral_required, lot, nav,
                                                 nav - collateral_used, cfg)
            if units <= 0:
                rejected.append({"candidate_id": cid, "reason": why}); continue
            max_loss_t, collat_t = po.max_loss * units, po.collateral_required * units
            xkey = P.make_exposure_key(c)
            checks = portfolio_risk_gate(cid, u, max_loss_t, collat_t, self.account, live, nav, cfg, xkey)
            port_ok = all(ch["passed"] for ch in checks)
            final = final_pre_trade_gate(cfg, self.store.kill_switch_on(), True, po.is_valid, True, True,
                                         port_ok, self.unresolved_exists(), self.entries_paused)
            self.store.save_risk_checks(cycle_id, u, cid, checks + final)
            failed = [ch for ch in checks + final if not ch["passed"]]
            if failed:
                rejected.append({"candidate_id": cid, "reason": "RISK_VETO:" + failed[0]["check"]}); continue
            chosen = (total, c, pr, po, sc, units, max_loss_t, collat_t, checks + final, xkey)
            break

        for r in rejected[:300]:
            self.store.save_candidate(cycle_id, u, r["candidate_id"], "REJECTED", r["reason"])
        if not chosen:
            return self._no_trade(now, cycle_id, u, label, confidence, "ALL_CANDIDATES_REJECTED",
                                  f"{len(cands)} candidates generated; none passed hard checks, scoring and risk veto.",
                                  n_candidates=len(cands), rejected_candidates=rejected[:50])

        total, c, pr, po, sc, units, max_loss_t, collat_t, checks, xkey = chosen
        sized = c.scaled(units)
        expected_total = pr.net_debit * units
        slack = max_loss_t * cfg.limit_slack_pct_of_max_loss
        quote_for = snap.quote_lookup()
        pos = P.Position(uuid.uuid4().hex[:12], u, c.strategy_type, tuple(l.__class__(l.instrument, l.quantity, None) for l in sized.legs),
                         units, {}, expected_total, pr.entry_fees * units, po.max_profit * units, max_loss_t,
                         collat_t, c.expiry, now, label, xkey)
        pos.transition(P.APPROVED, "RISK_GATE_PASSED", now, {"score": sc})
        pos.transition(P.PAPER_PENDING, "ORDER_SIMULATION_STARTED", now)
        fill = simulate_open_fill(sized.legs, quote_for, spot, now, cfg, expected_total + slack)
        self.store.save_order(now, cycle_id, c.candidate_id, fill.status, fill.reason,
                              {"prices": {k: str(v) for k, v in fill.prices.items()}, "fees": str(fill.fees)})
        if fill.status != FILLED:
            pos.transition(P.REJECTED if fill.status == "NOT_FILLED" else P.UNRESOLVED, fill.reason, now)
            self.positions.append(pos)
            if pos.state == P.UNRESOLVED:
                self.entries_paused = True
            return self._no_trade(now, cycle_id, u, label, confidence, f"PAPER_{fill.status}",
                                  f"Best candidate not filled: {fill.reason}", n_candidates=len(cands),
                                  rejected_candidates=rejected[:50], selected_strategy=c.strategy_type,
                                  candidate_score=total)
        pos.entry_prices = dict(fill.prices)
        pos.net_debit = fill.cashflow + fill.fees
        pos.entry_fees = fill.fees
        pos.transition(P.OPEN, "PAPER_FILL_COMPLETE", now, {"fill_prices": {k: str(v) for k, v in fill.prices.items()}})
        self.positions.append(pos)
        pos.last_pnl = P.mark_to_market(pos, quote_for, spot, now, cfg) or ZERO
        d = Decision(now, u, label, confidence, "PAPER_OPEN", "ALL_CHECKS_PASSED",
                     f"{c.strategy_type} selected (score {total}); " + "; ".join(raw.reasons),
                     cycle_id, self.cfg_hash, c.strategy_type, [leg_to_dict(l) for l in sized.legs], str(units),
                     str(pos.net_debit), str(pos.max_profit), str(pos.max_loss), str(pos.collateral),
                     str(pos.entry_fees), str(fill.slippage), total, len(cands), rejected[:50], checks)
        self.store.save_decision(d)
        return d
