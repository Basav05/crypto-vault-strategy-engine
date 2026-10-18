"""Position state machine, paper account, mark-to-market and exit rules."""
from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List
from .config import Config
from .execution import simulate_close_fill, FILLED
from .models import Candidate, Leg, leg_to_dict, leg_from_dict
from .payoff import payoff_at_expiry
from .regime import BULLISH, BEARISH, RANGE, HIGH_VOL

ZERO = Decimal(0)

PROPOSED, REJECTED, APPROVED, PAPER_PENDING, OPEN = "PROPOSED", "REJECTED", "APPROVED", "PAPER_PENDING", "OPEN"
EXIT_TRIGGERED, PAPER_CLOSING, CLOSED, EXPIRED, UNRESOLVED = "EXIT_TRIGGERED", "PAPER_CLOSING", "CLOSED", "EXPIRED", "UNRESOLVED"

TRANSITIONS = {
    PROPOSED: {REJECTED, APPROVED},
    APPROVED: {PAPER_PENDING},
    PAPER_PENDING: {OPEN, REJECTED, UNRESOLVED},
    OPEN: {EXIT_TRIGGERED, EXPIRED, UNRESOLVED},
    EXIT_TRIGGERED: {PAPER_CLOSING},
    PAPER_CLOSING: {CLOSED, UNRESOLVED},
    UNRESOLVED: {OPEN, CLOSED},      # only after reconciliation proves the real state
    REJECTED: set(), CLOSED: set(), EXPIRED: set(),
}
LIVE_STATES = {APPROVED, PAPER_PENDING, OPEN, EXIT_TRIGGERED, PAPER_CLOSING, UNRESOLVED}

THESIS_BREAKERS = {   # strategy -> regimes that invalidate the entry thesis
    "DEFINED_RISK_PUT_SPREAD": {BEARISH, HIGH_VOL}, "BULL_CALL_DEBIT_SPREAD": {BEARISH, HIGH_VOL},
    "DEFINED_RISK_CALL_SPREAD": {BULLISH, HIGH_VOL}, "BEAR_PUT_DEBIT_SPREAD": {BULLISH, HIGH_VOL},
    "IRON_CONDOR": {BULLISH, BEARISH, HIGH_VOL},
}


class IllegalTransition(Exception):
    pass


@dataclass
class Position:
    position_id: str
    underlying: str
    strategy_type: str
    legs: tuple                      # sized Leg tuple (no quotes)
    units: Decimal
    entry_prices: dict
    net_debit: Decimal               # total paid (+) / received (-) incl. entry fees
    entry_fees: Decimal
    max_profit: Decimal
    max_loss: Decimal
    collateral: Decimal
    expiry: datetime
    opened_at: datetime
    regime_at_entry: str
    exposure_key: str
    state: str = PROPOSED
    last_pnl: Decimal = ZERO
    realized_pnl: Optional[Decimal] = None
    needs_review: bool = False
    events: List[dict] = field(default_factory=list)
    persisted_events: int = 0

    def as_candidate(self) -> Candidate:
        return Candidate(self.strategy_type, self.underlying, self.legs)

    def transition(self, new: str, reason: str, now: datetime, snapshot: Optional[dict] = None):
        if new not in TRANSITIONS.get(self.state, set()):
            raise IllegalTransition(f"{self.state} -> {new}")
        self.events.append({"time": now.isoformat(), "from": self.state, "to": new,
                            "reason": reason, "snapshot": snapshot or {}})
        self.state = new

    # exit policy (created at entry from config)
    def stop_loss_limit(self, cfg: Config) -> Decimal:
        return self.max_loss * cfg.stop_loss_fraction_of_max_loss

    def profit_target(self, cfg: Config) -> Decimal:
        return self.max_profit * cfg.profit_target_fraction

    def to_dict(self) -> dict:
        return {"position_id": self.position_id, "underlying": self.underlying, "strategy_type": self.strategy_type,
                "legs": [leg_to_dict(l) for l in self.legs], "units": str(self.units),
                "entry_prices": {k: str(v) for k, v in self.entry_prices.items()},
                "net_debit": str(self.net_debit), "entry_fees": str(self.entry_fees),
                "max_profit": str(self.max_profit), "max_loss": str(self.max_loss),
                "collateral": str(self.collateral), "expiry": self.expiry.isoformat(),
                "opened_at": self.opened_at.isoformat(), "regime_at_entry": self.regime_at_entry,
                "exposure_key": self.exposure_key, "state": self.state, "last_pnl": str(self.last_pnl),
                "realized_pnl": None if self.realized_pnl is None else str(self.realized_pnl),
                "needs_review": self.needs_review, "events": self.events,
                "persisted_events": self.persisted_events}

    @staticmethod
    def from_dict(d: dict) -> "Position":
        return Position(
            d["position_id"], d["underlying"], d["strategy_type"], tuple(leg_from_dict(x) for x in d["legs"]),
            Decimal(d["units"]), {k: Decimal(v) for k, v in d["entry_prices"].items()}, Decimal(d["net_debit"]),
            Decimal(d["entry_fees"]), Decimal(d["max_profit"]), Decimal(d["max_loss"]), Decimal(d["collateral"]),
            datetime.fromisoformat(d["expiry"]), datetime.fromisoformat(d["opened_at"]), d["regime_at_entry"],
            d["exposure_key"], d["state"], Decimal(d["last_pnl"]),
            None if d["realized_pnl"] is None else Decimal(d["realized_pnl"]),
            d["needs_review"], list(d["events"]), d.get("persisted_events", 0))


def make_exposure_key(c: Candidate) -> str:
    legs = sorted(f"{l.instrument.option_type}{l.instrument.strike}:{'L' if l.quantity > 0 else 'S'}" for l in c.legs)
    return f"{c.underlying}|{c.strategy_type}|{c.expiry.isoformat()}|" + ",".join(legs)


class PaperAccount:
    """Simulated NAV.  NAV = initial + realized P&L + unrealised (executable-side MTM) of live positions."""

    def __init__(self, initial_nav: Decimal):
        self.initial_nav = initial_nav
        self.realized_pnl = ZERO
        self.peak_nav = initial_nav
        self.start_of_day_nav = initial_nav
        self.day: Optional[str] = None

    def nav(self, positions) -> Decimal:
        unreal = sum((p.last_pnl for p in positions if p.state in LIVE_STATES and p.state != APPROVED), ZERO)
        return self.initial_nav + self.realized_pnl + unreal

    def roll(self, now: datetime, nav: Decimal):
        today = now.date().isoformat()
        if self.day != today:
            self.day, self.start_of_day_nav = today, nav
        if nav > self.peak_nav:
            self.peak_nav = nav

    def drawdown(self, nav: Decimal) -> float:
        return float(max(ZERO, (self.peak_nav - nav) / self.peak_nav)) if self.peak_nav > 0 else 0.0

    def daily_loss(self, nav: Decimal) -> Decimal:
        return max(ZERO, self.start_of_day_nav - nav)

    def to_dict(self):
        return {"initial_nav": str(self.initial_nav), "realized_pnl": str(self.realized_pnl),
                "peak_nav": str(self.peak_nav), "start_of_day_nav": str(self.start_of_day_nav), "day": self.day}

    @staticmethod
    def from_dict(d) -> "PaperAccount":
        a = PaperAccount(Decimal(d["initial_nav"]))
        a.realized_pnl, a.peak_nav = Decimal(d["realized_pnl"]), Decimal(d["peak_nav"])
        a.start_of_day_nav, a.day = Decimal(d["start_of_day_nav"]), d["day"]
        return a


def mark_to_market(p: Position, quote_for, spot: Decimal, now: datetime, cfg: Config) -> Optional[Decimal]:
    """P&L if closed now at executable-side quotes (incl. slippage + fees).  None if any quote is unusable."""
    fill = simulate_close_fill(p.legs, quote_for, spot, now, cfg)
    if fill.status != FILLED:
        return None
    return fill.cashflow - fill.fees - p.net_debit


def settlement_pnl(p: Position, settlement_price: Decimal) -> Decimal:
    """Expiry P&L using an *approximated* settlement price (index at monitoring time)."""
    return payoff_at_expiry(p.as_candidate(), settlement_price) - p.net_debit - p.entry_fees


def exit_reason(p: Position, pnl: Decimal, now: datetime, regime_now: str, kill_switch: bool,
                circuit_breaker: bool, cfg: Config) -> Optional[str]:
    """Priority: KILL_SWITCH > PORTFOLIO_CIRCUIT_BREAKER > MAX_LOSS_TRIGGER > THESIS_INVALIDATION
    > TIME_EXIT > PROFIT_TARGET.  (Data/position-integrity failures are handled before this call.)"""
    if kill_switch:
        return "KILL_SWITCH"
    if circuit_breaker:
        return "PORTFOLIO_CIRCUIT_BREAKER"
    if pnl <= -p.stop_loss_limit(cfg):
        return "MAX_LOSS_TRIGGER"
    if regime_now in THESIS_BREAKERS.get(p.strategy_type, set()):
        return "THESIS_INVALIDATION"
    hours_left = (p.expiry - now).total_seconds() / 3600.0
    if hours_left <= cfg.expiry_cutoff_hours:
        return "TIME_EXIT"
    if pnl >= p.profit_target(cfg):
        return "PROFIT_TARGET"
    return None
