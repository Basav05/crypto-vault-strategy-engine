"""Market-data providers.  PUBLIC endpoints only.  There is no order, auth or private-API code here:
the production-deny guard refuses any base URL other than Deribit's testnet host."""
import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional
from urllib.parse import urlparse
from .config import Config
from .models import (Instrument, OptionQuote, Quote, SpotData, MarketSnapshot, CALL, PUT, utcnow)

DERIBIT_TESTNET_BASE = "https://test.deribit.com/api/v2"
ALLOWED_HOST = "test.deribit.com"
REQUEST_TIMEOUT_SECONDS = 8


class ProductionDenied(Exception):
    pass


def _dec(x) -> Optional[Decimal]:
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except Exception:
        return None


# ---- pure parsers (unit-testable without network) --------------------------------------
def parse_option_chain(instruments: list, summaries: list, spot: Decimal, now: datetime) -> list:
    """Join instrument metadata with book summaries.  Deribit quotes options in the underlying coin;
    prices are converted to USD at the summary's underlying price (APPROXIMATION, see payoff_model)."""
    by_name = {s.get("instrument_name"): s for s in summaries if isinstance(s, dict)}
    chain = []
    for ins in instruments:
        name = ins.get("instrument_name")
        s = by_name.get(name)
        try:
            expiry = datetime.fromtimestamp(int(ins["expiration_timestamp"]) / 1000, tz=timezone.utc)
            inst = Instrument(
                instrument_id=name, underlying=str(ins.get("base_currency", "")).upper(),
                option_type=CALL if str(ins.get("option_type")).lower() == "call" else PUT,
                strike=Decimal(str(ins["strike"])), expiry=expiry, multiplier=_dec(ins.get("contract_size")),
                min_trade_amount=_dec(ins.get("min_trade_amount")) or Decimal(0),
                quote_currency="USD(normalized)", settlement_currency=str(ins.get("settlement_currency", "")).upper(),
                is_active=bool(ins.get("is_active", False)), payoff_model="INVERSE_COIN_NORMALIZED")
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
        quote = None
        iv = None
        if s:
            und = _dec(s.get("underlying_price")) or spot
            bid, ask = _dec(s.get("bid_price")), _dec(s.get("ask_price"))
            ts_ms = s.get("creation_timestamp")
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else None
            if ask is not None and ts is not None:
                quote = Quote((bid or Decimal(0)) * und, ask * und, ts)
            mark_iv = _dec(s.get("mark_iv"))
            iv = float(mark_iv) / 100.0 if mark_iv is not None else None
        chain.append(OptionQuote(inst, quote, iv))
    return chain


def parse_chart_data(result: dict) -> list:
    ticks, closes = result.get("ticks") or [], result.get("close") or []
    out = []
    for t, c in zip(ticks, closes):
        if c is not None and c > 0:
            out.append((datetime.fromtimestamp(t / 1000, tz=timezone.utc), float(c)))
    return sorted(out)


def parse_trades(result) -> tuple:
    trades = result.get("trades", []) if isinstance(result, dict) else []
    buy = sum(float(t.get("amount", 0)) for t in trades if t.get("direction") == "buy")
    sell = sum(float(t.get("amount", 0)) for t in trades if t.get("direction") == "sell")
    return buy, sell, len(trades)


class DeribitTestnetProvider:
    """Public REST market data from Deribit TESTNET.  Testnet liquidity is artificial."""

    def __init__(self, cfg: Config, base_url: str = DERIBIT_TESTNET_BASE,
                 http_get: Optional[Callable] = None, clock: Callable = utcnow):
        if urlparse(base_url).hostname != ALLOWED_HOST:
            raise ProductionDenied(f"Only {ALLOWED_HOST} is allowed; got {base_url!r}")
        self.cfg, self.base, self.clock = cfg, base_url, clock
        self._http_get = http_get or self._default_get

    def _default_get(self, url, params):
        import requests
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS,
                         headers={"User-Agent": "RegimeVault-research/0.2"})
        r.raise_for_status()
        return r.json()

    def _get(self, method: str, params: dict):
        if not method.startswith("public/"):
            raise ProductionDenied(f"Only public methods are allowed: {method}")
        payload = self._http_get(f"{self.base}/{method}", params)
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return payload.get("result", {})

    def get_snapshot(self) -> MarketSnapshot:
        now = self.clock()
        spot, chains, prices, trades = {}, {}, {}, {}
        for u in self.cfg.underlyings:
            idx = self._get("public/get_index_price", {"index_name": f"{u.lower()}_usd"})
            price = Decimal(str(idx.get("index_price", 0)))
            spot[u] = SpotData(u, price, self.clock())
            instruments = self._get("public/get_instruments", {"currency": u, "kind": "option", "expired": "false"})
            summaries = self._get("public/get_book_summary_by_currency", {"currency": u, "kind": "option"})
            chains[u] = parse_option_chain(instruments or [], summaries if isinstance(summaries, list) else [], price, now)
            end = int(now.timestamp() * 1000)
            chart = self._get("public/get_tradingview_chart_data", {
                "instrument_name": f"{u}-PERPETUAL", "start_timestamp": end - 7 * 24 * 3600 * 1000,
                "end_timestamp": end, "resolution": "60"})
            prices[u] = parse_chart_data(chart)
            try:
                trades[u] = parse_trades(self._get("public/get_last_trades_by_instrument", {
                    "instrument_name": f"{u}-PERPETUAL", "count": 100, "sorting": "desc"}))
            except Exception:
                trades[u] = (0.0, 0.0, 0)
        return MarketSnapshot(now, spot, chains, prices, trades, "Deribit testnet public REST")


# ---- deterministic synthetic provider (offline demo / tests) ---------------------------
SCENARIOS = {   # drift per hour, noise per hour, ATM IV, (buy, sell) volume
    "bull": (0.0030, 0.0020, 0.55, (140.0, 100.0)),
    "bear": (-0.0030, 0.0020, 0.60, (100.0, 140.0)),
    "range": (0.0, 0.0012, 0.50, (100.0, 100.0)),
    "highvol": (0.0, 0.0200, 1.40, (100.0, 100.0)),
}
BASE_SPOT = {"BTC": 60000.0, "ETH": 3000.0}


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(opt_type, s, k, t, vol):
    if t <= 0 or vol <= 0:
        return max(s - k, 0.0) if opt_type == CALL else max(k - s, 0.0)
    d1 = (math.log(s / k) + 0.5 * vol * vol * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    call = s * _norm_cdf(d1) - k * _norm_cdf(d2)
    return call if opt_type == CALL else call - s + k


class SyntheticProvider:
    def __init__(self, cfg: Config, scenario: str = "range", seed: int = 7, clock: Callable = utcnow,
                 half_spread: float = 0.04, quote_age_seconds: float = 0.0, n_hours: int = 96):
        if scenario not in SCENARIOS:
            raise ValueError(f"scenario must be one of {sorted(SCENARIOS)}")
        self.cfg, self.scenario, self.seed, self.clock = cfg, scenario, seed, clock
        self.half_spread, self.quote_age, self.n_hours = half_spread, quote_age_seconds, n_hours

    def _history(self, u, now):
        drift, noise, _, _ = SCENARIOS[self.scenario]
        rng = random.Random(self.seed + sum(map(ord, u)))
        p, path = 1.0, []
        for _ in range(self.n_hours):
            p *= math.exp(drift + rng.gauss(0, noise))
            path.append(p)
        scale = BASE_SPOT[u] / path[-1]
        start = now - timedelta(hours=self.n_hours - 1)
        return [(start + timedelta(hours=k), v * scale) for k, v in enumerate(path)]

    def get_snapshot(self) -> MarketSnapshot:
        now = self.clock()
        _, _, atm_iv, (buy, sell) = SCENARIOS[self.scenario]
        spot, chains, prices, trades = {}, {}, {}, {}
        qts = now - timedelta(seconds=self.quote_age)
        for u in self.cfg.underlyings:
            hist = self._history(u, now)
            s = hist[-1][1]
            prices[u], trades[u] = hist, (buy, sell, 100)
            spot[u] = SpotData(u, Decimal(str(round(s, 2))), now)
            unit = 500.0 if s > 10000 else 25.0
            step = max(unit, round(s * 0.025 / unit) * unit)
            center = round(s / step) * step
            strikes = [center + step * k for k in range(-12, 13)]
            chain = []
            for days in (7, 14, 28):
                expiry = (now + timedelta(days=days)).replace(hour=8, minute=0, second=0, microsecond=0)
                t = (expiry - now).total_seconds() / (365 * 86400)
                for k in strikes:
                    m = math.log(k / s)
                    vol = atm_iv * (1 + 2.0 * m * m - 0.3 * m)
                    for ot in (CALL, PUT):
                        fair = max(bs_price(ot, s, k, t, vol), 0.01)
                        bid, ask = fair * (1 - self.half_spread), fair * (1 + self.half_spread)
                        inst = Instrument(f"{u}-{expiry:%Y%m%d}-{int(k)}-{ot[0]}", u, ot, Decimal(str(k)), expiry,
                                          Decimal("1"), Decimal("0.1"), "USD", "USD", True, "LINEAR_USD")
                        chain.append(OptionQuote(inst, Quote(Decimal(str(round(bid, 2))), Decimal(str(round(ask, 2))), qts), vol))
            chains[u] = chain
        return MarketSnapshot(now, spot, chains, prices, trades, f"SYNTHETIC:{self.scenario} (not market data)")
