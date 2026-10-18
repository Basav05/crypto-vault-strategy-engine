"""Engine configuration. Kept separate from logic; every value is a *software-test*
parameter, not a validated trading recommendation."""
from dataclasses import dataclass, asdict
from decimal import Decimal as D
import hashlib
import json

ENV_PAPER = "TESTNET_PAPER_ONLY"
RULE_VERSION = "rules-0.2"

ALL_STRATEGIES = (
    "DEFINED_RISK_PUT_SPREAD",     # bull put credit spread
    "DEFINED_RISK_CALL_SPREAD",    # bear call credit spread
    "BULL_CALL_DEBIT_SPREAD",
    "BEAR_PUT_DEBIT_SPREAD",
    "IRON_CONDOR",
)


@dataclass(frozen=True)
class Config:
    environment: str = ENV_PAPER
    allow_live_orders: bool = False
    underlyings: tuple = ("BTC", "ETH")
    initial_nav: D = D("100000")

    decision_interval_seconds: int = 60
    max_market_data_age_seconds: int = 10
    max_clock_skew_seconds: int = 5

    allowed_strategies: tuple = ALL_STRATEGIES
    # Deribit options are coin-settled; INVERSE_COIN_NORMALIZED means "premiums converted to USD
    # at the snapshot's underlying price" -- an APPROXIMATION (see IMPROVEMENTS.md).
    supported_payoff_models: tuple = ("LINEAR_USD", "INVERSE_COIN_NORMALIZED")

    # portfolio limits
    max_open_positions: int = 3
    max_positions_per_underlying: int = 2
    max_risk_per_trade_pct_nav: D = D("0.005")
    max_total_open_risk_pct_nav: D = D("0.02")
    max_daily_loss_pct_nav: D = D("0.02")
    max_drawdown_pct_nav: D = D("0.05")

    # instrument eligibility
    minimum_days_to_expiry: int = 3
    maximum_days_to_expiry: int = 30
    max_spread_pct: D = D("0.30")
    minimum_quote_quality: float = 0.80
    relevant_moneyness_band: float = 0.25

    # candidate generation / filters
    short_otm_min: float = 0.03
    short_otm_max: float = 0.12
    atm_band: float = 0.03
    max_width_steps: int = 2
    max_candidates_per_underlying: int = 400
    minimum_candidate_score: float = 0.70
    min_reward_to_risk: D = D("0.15")
    reward_risk_target: D = D("0.5")
    require_valid_payoff: bool = True
    require_collateral_check: bool = True

    # execution-cost model (assumptions; calibrate against real fills)
    slippage_spread_fraction: D = D("0.25")
    fee_rate_underlying: D = D("0.0003")
    fee_cap_option_pct: D = D("0.125")
    limit_slack_pct_of_max_loss: D = D("0.05")
    require_depth_info: bool = False

    # exits
    stop_loss_fraction_of_max_loss: D = D("0.75")
    profit_target_fraction: D = D("0.50")
    expiry_cutoff_hours: int = 12

    # features / regime (placeholders; calibrate with walk-forward tests)
    min_price_observations: int = 30
    fast_window: int = 6
    slow_window: int = 24
    short_vol_window: int = 24
    long_vol_window: int = 168
    positive_return_threshold: float = 0.008     # 4h return
    negative_return_threshold: float = -0.008
    range_return_24h_threshold: float = 0.02
    high_vol_threshold: float = 1.00             # annualised realised vol
    extreme_iv_threshold: float = 1.20
    ofi_bullish_threshold: float = 0.15
    ofi_bearish_threshold: float = -0.15
    min_trades_for_ofi: int = 20
    regime_confirmations: int = 3
    regime_min_hold_cycles: int = 3


def validate_config(cfg: Config) -> list:
    errors = []
    if cfg.environment != ENV_PAPER:
        errors.append("ENVIRONMENT_NOT_TESTNET_PAPER_ONLY")
    if cfg.allow_live_orders:
        errors.append("LIVE_ORDERS_MUST_BE_DISABLED")
    if not cfg.underlyings:
        errors.append("NO_UNDERLYINGS")
    if cfg.initial_nav <= 0:
        errors.append("INVALID_INITIAL_NAV")
    for name in ("max_risk_per_trade_pct_nav", "max_total_open_risk_pct_nav",
                 "max_daily_loss_pct_nav", "max_drawdown_pct_nav"):
        v = getattr(cfg, name)
        if not (D(0) < v < D(1)):
            errors.append(f"{name.upper()}_OUT_OF_RANGE")
    if cfg.max_total_open_risk_pct_nav < cfg.max_risk_per_trade_pct_nav:
        errors.append("TOTAL_RISK_LESS_THAN_PER_TRADE_RISK")
    if cfg.minimum_days_to_expiry >= cfg.maximum_days_to_expiry:
        errors.append("DTE_RANGE_INVALID")
    if cfg.max_market_data_age_seconds <= 0:
        errors.append("DATA_AGE_INVALID")
    if cfg.fast_window >= cfg.slow_window:
        errors.append("MA_WINDOWS_INVALID")
    if cfg.min_price_observations <= cfg.slow_window:
        errors.append("MIN_OBSERVATIONS_TOO_SMALL")
    bad = [s for s in cfg.allowed_strategies if s not in ALL_STRATEGIES]
    if bad:
        errors.append("UNKNOWN_STRATEGY:" + ",".join(bad))
    return errors


def config_hash(cfg: Config) -> str:
    blob = json.dumps(asdict(cfg), sort_keys=True, default=str)
    return hashlib.sha256((RULE_VERSION + blob).encode()).hexdigest()[:16]
