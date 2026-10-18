"""Plain data models. Money is Decimal; statistics (returns, vol) are float."""
from __future__ import annotations
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Tuple, List, Dict

CALL, PUT = "CALL", "PUT"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal
    timestamp: datetime
    bid_size: Optional[Decimal] = None
    ask_size: Optional[Decimal] = None

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> Optional[Decimal]:
        m = self.mid
        return (self.ask - self.bid) / m if m > 0 else None


@dataclass(frozen=True)
class Instrument:
    instrument_id: str
    underlying: str
    option_type: str                  # CALL / PUT
    strike: Decimal
    expiry: datetime
    multiplier: Optional[Decimal]     # contract size in underlying units
    min_trade_amount: Decimal
    quote_currency: str = "USD"
    settlement_currency: str = "USD"
    is_active: bool = True
    payoff_model: str = "LINEAR_USD"


@dataclass(frozen=True)
class OptionQuote:
    instrument: Instrument
    quote: Optional[Quote]
    iv: Optional[float] = None        # decimal, e.g. 0.55 == 55%


@dataclass(frozen=True)
class Leg:
    instrument: Instrument
    quantity: Decimal                 # +long / -short, in contracts
    quote: Optional[Quote] = None     # quote the leg was priced from

    def scaled(self, units: Decimal) -> "Leg":
        return replace(self, quantity=self.quantity * units)


@dataclass(frozen=True)
class Candidate:
    strategy_type: str
    underlying: str
    legs: Tuple[Leg, ...]

    @property
    def expiry(self) -> datetime:
        return self.legs[0].instrument.expiry

    @property
    def candidate_id(self) -> str:
        parts = sorted(f"{l.instrument.instrument_id}x{l.quantity}" for l in self.legs)
        return f"{self.strategy_type}|{self.underlying}|" + "|".join(parts)

    def scaled(self, units: Decimal) -> "Candidate":
        return replace(self, legs=tuple(l.scaled(units) for l in self.legs))


@dataclass(frozen=True)
class SpotData:
    underlying: str
    index_price: Decimal
    timestamp: Optional[datetime]


@dataclass
class MarketSnapshot:
    timestamp: datetime
    spot: Dict[str, SpotData]
    chains: Dict[str, List[OptionQuote]]
    prices: Dict[str, List[Tuple[datetime, float]]]      # hourly closes, ascending
    trades: Dict[str, Tuple[float, float, int]]           # (buy_vol, sell_vol, count)
    source: str = "unknown"

    def quote_lookup(self):
        table = {}
        for chain in self.chains.values():
            for oq in chain:
                table[oq.instrument.instrument_id] = oq.quote
        return table.get


@dataclass
class Decision:
    timestamp: datetime
    underlying: str
    regime: str
    confidence: float
    action: str                       # PAPER_OPEN / NO_TRADE
    reason_code: str
    explanation: str
    cycle_id: str = ""
    config_hash: str = ""
    selected_strategy: str = "NONE"
    selected_legs: list = field(default_factory=list)
    units: str = "0"
    expected_net_debit: Optional[str] = None     # +cost / -credit, total incl. fees
    max_profit: Optional[str] = None
    max_loss: Optional[str] = None
    collateral_required: Optional[str] = None
    estimated_fees: Optional[str] = None
    estimated_slippage: Optional[str] = None
    candidate_score: Optional[float] = None
    n_candidates: int = 0
    rejected_candidates: list = field(default_factory=list)
    risk_checks: list = field(default_factory=list)


# ---- (de)serialisation helpers -------------------------------------------------------
def _dt(s):
    return datetime.fromisoformat(s) if s else None


def instrument_to_dict(i: Instrument) -> dict:
    return {"instrument_id": i.instrument_id, "underlying": i.underlying, "option_type": i.option_type,
            "strike": str(i.strike), "expiry": i.expiry.isoformat(),
            "multiplier": None if i.multiplier is None else str(i.multiplier),
            "min_trade_amount": str(i.min_trade_amount), "quote_currency": i.quote_currency,
            "settlement_currency": i.settlement_currency, "is_active": i.is_active,
            "payoff_model": i.payoff_model}


def instrument_from_dict(d: dict) -> Instrument:
    return Instrument(d["instrument_id"], d["underlying"], d["option_type"], Decimal(d["strike"]),
                      _dt(d["expiry"]), None if d["multiplier"] is None else Decimal(d["multiplier"]),
                      Decimal(d["min_trade_amount"]), d["quote_currency"], d["settlement_currency"],
                      d["is_active"], d["payoff_model"])


def leg_to_dict(l: Leg) -> dict:
    return {"instrument": instrument_to_dict(l.instrument), "quantity": str(l.quantity)}


def leg_from_dict(d: dict) -> Leg:
    return Leg(instrument_from_dict(d["instrument"]), Decimal(d["quantity"]), None)
