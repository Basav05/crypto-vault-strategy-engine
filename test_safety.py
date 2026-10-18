import pathlib
from decimal import Decimal as D
from datetime import datetime, timezone
from regimevault.config import Config, validate_config, config_hash
from regimevault.market_data import (DeribitTestnetProvider, ProductionDenied, parse_option_chain, parse_chart_data,
                                     parse_trades)
from tests.helpers import *

PKG = pathlib.Path(__file__).resolve().parent.parent / "regimevault"


def test_no_private_or_order_endpoints_or_credentials_anywhere_in_package():
    forbidden = ["private/", "client_secret", "client_id", "access_token", "api_key", "apikey", "requests.post",
                 "ws/api", "websocket", "public/auth", "/buy", "/sell"]
    for f in PKG.glob("*.py"):
        text = f.read_text().lower()
        for bad in forbidden:
            assert bad not in text, f"{f.name} contains {bad!r}"


def test_execution_and_engine_have_no_network_imports():
    for name in ("execution.py", "engine.py", "risk.py", "payoff.py", "positions.py"):
        text = (PKG / name).read_text()
        assert "import requests" not in text and "urllib" not in text and "socket" not in text, name


def test_production_url_is_denied():
    cfg = Config()
    for url in ("https://www.deribit.com/api/v2", "https://test.deribit.com.evil.example/api/v2",
                "http://localhost:8000/api/v2"):
        expect_raises(ProductionDenied, DeribitTestnetProvider, cfg, url)
    DeribitTestnetProvider(cfg, "https://test.deribit.com/api/v2")


def test_only_public_methods_are_callable():
    p = DeribitTestnetProvider(Config(), http_get=lambda url, params: {"result": {}})
    expect_raises(ProductionDenied, p._get, "private/buy", {})
    assert p._get("public/get_index_price", {}) == {}


def test_live_flag_and_environment_validation():
    assert "LIVE_ORDERS_MUST_BE_DISABLED" in validate_config(Config(allow_live_orders=True))
    assert "ENVIRONMENT_NOT_TESTNET_PAPER_ONLY" in validate_config(Config(environment="PROD"))
    assert validate_config(Config()) == []
    assert config_hash(Config()) != config_hash(Config(max_open_positions=2))


def _ms(dt): return int(dt.timestamp() * 1000)


def test_deribit_parsers_with_canned_payloads_and_usd_normalisation():
    now = NOW
    ins = [{"instrument_name": "BTC-1-60000-P", "strike": 60000, "option_type": "put",
            "expiration_timestamp": _ms(EXPIRY), "contract_size": 1, "min_trade_amount": 0.1,
            "is_active": True, "base_currency": "BTC", "settlement_currency": "BTC"},
           {"instrument_name": "BAD", "strike": "x"}]
    summ = [{"instrument_name": "BTC-1-60000-P", "bid_price": 0.01, "ask_price": 0.012, "underlying_price": 60000,
             "mark_iv": 55.0, "creation_timestamp": _ms(now)}]
    chain = parse_option_chain(ins, summ, D(60000), now)
    assert len(chain) == 1                                       # malformed record skipped, not crashed
    o = chain[0]
    assert o.quote.bid == D(600) and o.quote.ask == D(720) and abs(o.iv - 0.55) < 1e-9
    assert o.instrument.payoff_model == "INVERSE_COIN_NORMALIZED"
    ins[0]["contract_size"] = None
    assert parse_option_chain(ins[:1], summ, D(60000), now)[0].instrument.multiplier is None
    assert parse_chart_data({"ticks": [_ms(now), _ms(now) + 3600000], "close": [100.0, None]}) == [(now, 100.0)]
    assert parse_trades({"trades": [{"direction": "buy", "amount": 3}, {"direction": "sell", "amount": 1}]}) == (3.0, 1.0, 2)


def test_deribit_provider_end_to_end_with_fake_http():
    def fake(url, params):
        if "get_index_price" in url: return {"result": {"index_price": 60000}}
        if "get_instruments" in url: return {"result": []}
        if "get_book_summary" in url: return {"result": []}
        if "get_tradingview" in url: return {"result": {"ticks": [], "close": []}}
        if "get_last_trades" in url: return {"result": {"trades": []}}
        raise AssertionError(url)
    snap = DeribitTestnetProvider(Config(), http_get=fake, clock=Clock()).get_snapshot()
    assert snap.spot["BTC"].index_price == D(60000) and snap.chains["ETH"] == []
