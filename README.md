RegimeVault: Automatic Strategy Selection Engine — Full Pseudocode
The engine should behave like a disciplined trading system, not an AI that guesses which trade might make money. It should evaluate market conditions, generate candidate strategies, calculate their complete payoff and risk, reject anything unsafe, and only then permit a simulated testnet/paper trade.
For your MVP, I recommend a rules-based, explainable engine first. You can later compare it with machine-learning models using historical data. Rules are easier to test, debug, document for grants, and operate as a solo founder.
1. End-to-end decision architecture
1. Collect market data
BTC/ETH spot, option chains, bid/ask, IV, Greeks, expiries, order book

2. Validate data
Freshness, missing fields, crossed quotes, liquidity, timestamps

3. Calculate market features
Trend, realized volatility, implied volatility, skew, liquidity, order flow

4. Classify market regime
Bullish, bearish, range-bound, high-volatility, or uncertain

5. Generate strategy candidates
Covered calls, cash-secured puts, defined-risk spreads, or no trade

6. Price every candidate
Executable-side quotes, fees, slippage, payoff, Greeks, max loss

7. Risk engine — veto authority
Collateral, position sizing, portfolio exposure, drawdown, trade limits

8. Select, simulate, monitor
Choose the best eligible candidate, simulate fills, manage exits, log every decision


Non-negotiable rule: the strategy selector may recommend a trade, but the risk engine can veto it. If the data, payoff, collateral, or execution simulation cannot be verified, the result is NO_TRADE.
2. Define the decision contract
Every evaluation should produce a structured result, even when no trade is selected.
Decision:    timestamp    underlying                 # BTC or ETH    regime                     # BULLISH / BEARISH / RANGE / HIGH_VOL / UNCERTAIN    confidence                 # 0.0 to 1.0; not probability of profit    selected_strategy          # strategy ID or NONE    selected_legs              # option instruments and quantities    expected_net_credit_debit    max_profit    max_loss    collateral_required    estimated_fees    estimated_slippage    candidate_score    rejected_candidates        # candidate ID + reason    risk_checks    action                     # PAPER_OPEN / HOLD / NO_TRADE / PAPER_CLOSE    explanation                # human-readable reason



Use decimal arithmetic for money and contract-specific payoff calculations. Never assume all option contracts have the same multiplier, settlement currency, or collateral requirements; load and validate the exchange's instrument metadata.
3. Core configuration and safety rules
Keep configuration separate from code. This lets you tune the test engine without silently changing its logic.
CONFIG = {    "environment": "TESTNET_PAPER_ONLY",    "underlyings": ["BTC", "ETH"],    # First MVP: one decision cycle at a time.    "decision_interval_seconds": 60,    "max_market_data_age_seconds": 10,    "max_clock_skew_seconds": 5,    "allowed_strategies": [        "DEFINED_RISK_PUT_SPREAD",        "DEFINED_RISK_CALL_SPREAD",        "IRON_CONDOR"    ],    # Add these only after their asset/collateral accounting is implemented.    "later_strategies": [        "COVERED_CALL",        "CASH_SECURED_PUT"    ],    "max_open_positions": 3,    "max_risk_per_trade_pct_nav": 0.005,    "max_total_open_risk_pct_nav": 0.02,    "max_daily_loss_pct_nav": 0.02,    "max_drawdown_pct_nav": 0.05,    "minimum_days_to_expiry": 3,    "maximum_days_to_expiry": 30,    "minimum_quote_quality": 0.80,    "minimum_candidate_score": 0.70,    "require_valid_payoff": True,    "require_collateral_check": True,    "require_execution_cost_estimate": True,    "allow_live_orders": False



These values are initial software-test parameters, not validated trading recommendations. For example, 0.005 means a simulated trade cannot risk more than 0.5% of simulated portfolio net asset value. Do not infer that these settings are appropriate for real capital.
The MVP should start with defined-risk spreads and an iron condor. Covered calls and cash-secured puts need correct asset inventory, assignment, and collateral handling before they can be considered eligible.
Deribit maintains separate test and production environments, with independent accounts and API keys. Its testnet also has artificial liquidity and volume, so testnet fills are not evidence that a strategy will work in live markets. 

Deribit Support
+1



4. Main engine: complete decision-cycle pseudocode
This is the orchestration logic. Each function is expanded in the sections below.
FUNCTION run_decision_cycle():    cycle_id = create_unique_id()    cycle_start_time = utc_now()    log_event("CYCLE_STARTED", cycle_id)    # A. Safety and system checks    IF system_kill_switch_is_on():        RETURN decision("NO_TRADE", "KILL_SWITCH_ACTIVE")    IF environment_is_not_testnet_paper():        RETURN decision("NO_TRADE", "UNSAFE_ENVIRONMENT")    IF daily_loss_limit_exceeded():        pause_new_entries()        RETURN decision("NO_TRADE", "DAILY_LOSS_LIMIT")    IF drawdown_limit_exceeded():        pause_new_entries()        close_or_flag_positions_using_paper_rules()        RETURN decision("NO_TRADE", "DRAWDOWN_LIMIT")    # B. Collect data    instruments = fetch_supported_option_instruments(["BTC", "ETH"])    spot_data = fetch_spot_and_index_data(["BTC", "ETH"])    option_quotes = fetch_option_quotes(instruments)    order_books = fetch_required_order_books(instruments)    recent_trades = fetch_recent_trades(["BTC", "ETH"])    # C. Validate data    validation = validate_all_data(        spot_data,        option_quotes,        order_books,        recent_trades



One key implementation detail: in a real exchange adapter, instrument discovery and quotes must be fetched from documented API methods, with the instrument metadata including the contract multiplier and minimum trade amount. Never hard-code those values. 

Deribit Support
+1



5. Market data validation engine
A sophisticated strategy cannot compensate for incorrect inputs. This layer should fail closed: missing, stale, or inconsistent information means no new trade.
FUNCTION validate_all_data(spot, options, order_books, trades):    errors = []    IF spot is missing:        errors.append("SPOT_DATA_MISSING")    FOR each underlying IN ["BTC", "ETH"]:        s = spot[underlying]        IF s.index_price <= 0:            errors.append("INVALID_INDEX_PRICE")        IF s.timestamp is missing:            errors.append("MISSING_SPOT_TIMESTAMP")        IF utc_now() - s.timestamp > MAX_DATA_AGE:            errors.append("STALE_SPOT_DATA")    FOR each option IN options:        IF option.instrument_id is missing:            errors.append("MISSING_INSTRUMENT_ID")        IF option.expiry is missing:            errors.append("MISSING_EXPIRY")        IF option.strike <= 0:            errors.append("INVALID_STRIKE")        IF option.type NOT IN ["CALL", "PUT"]:            errors.append("INVALID_OPTION_TYPE")        IF option.bid < 0 OR option.ask < 0:            errors.append("NEGATIVE_OPTION_QUOTE")



Do not reject the entire system just because an optional metric is missing. Instead, mark that metric unavailable and disable strategies that require it. For example, a spread can be rejected if one leg has no valid quote, while a missing order-flow feature could disable only the strategies whose rules depend on order flow.
Instrument eligibility
FUNCTION option_is_eligible(option, now):    dte = days_until(option.expiry, now)    RETURN (        option.is_active        AND option.bid >= 0        AND option.ask > 0        AND option.ask >= option.bid        AND option.contract_multiplier > 0        AND option.min_trade_amount > 0        AND MIN_DTE <= dte <= MAX_DTE        AND option.quote_age <= MAX_DATA_AGE        AND option.bid_ask_spread_pct <= MAX_SPREAD_PCT        AND option.minimum_liquidity_checks_pass    )



Thresholds for bid–ask spread and liquidity should be calibrated against observed testnet quotes and documented in the audit log. Testnet liquidity can be unrealistic, so these checks validate software behavior rather than live-market quality. 

Deribit Support
+1



6. Feature engine: convert raw data into useful signals
The feature engine should return measured values and data-quality flags—not a strategy recommendation.
FUNCTION calculate_market_features(spot, options, books, trades):    features = {}    FOR each underlying IN ["BTC", "ETH"]:        prices = load_valid_historical_prices(underlying)        IF count(prices) < MIN_REQUIRED_OBSERVATIONS:            RETURN FeatureResult(                are_complete=False,                reason="INSUFFICIENT_PRICE_HISTORY"            )        # Trend        features[underlying].return_1h = calculate_return(prices, "1h")        features[underlying].return_4h = calculate_return(prices, "4h")        features[underlying].return_24h = calculate_return(prices, "24h")        features[underlying].trend_fast = moving_average(prices, FAST_WINDOW)        features[underlying].trend_slow = moving_average(prices, SLOW_WINDOW)        # Realized volatility        features[underlying].realized_vol_short = annualized_realized_vol(            prices, SHORT_WINDOW        )        features[underlying].realized_vol_long = annualized_realized_vol(            prices, LONG_WINDOW        )        # Option market        chain = select_valid_option_chain(options, underlying)        features[underlying].atm_iv = calculate_weighted_atm_iv(chain)        features[underlying].put_skew = calculate_put_skew(chain)        features[underlying].call_skew = calculate_call_skew(chain)



How each factor affects the decision
Factor	What it tells the engine	How it influences selection
Short/medium-term trend	Direction and strength of price movement	Helps distinguish bullish, bearish, and neutral setups
Realized volatility	How much the underlying has actually moved	High volatility can reduce strategy eligibility or require smaller risk
Implied volatility (IV)	Volatility priced into options	Helps compare premiums and estimate option sensitivity
IV versus realized volatility	Difference between option-implied and historical movement	A research signal, not proof that options are overpriced
Put/call skew	Relative pricing of downside versus upside options	Helps compare candidate legs
Term structure	IV differences across expiries	Helps avoid expiries with unusual or unstable pricing
Order-flow imbalance (OFI)	Directional imbalance in recent trades	A secondary confirmation signal
Order-book imbalance (OBI)	Difference between visible bid and ask depth	A secondary liquidity/pressure signal; can be spoofed or unstable
Spread and depth	Approximate execution quality	Hard eligibility filter
Open interest and volume	Activity and position concentration clues	Optional context, never a standalone trade signal
Do not treat IV minus realized volatility as a guaranteed premium-selling edge. Realized volatility is backward-looking, while implied volatility reflects market pricing of future uncertainty and risk premia. Your MVP should measure the relationship and test it historically rather than assume a profitable result.
7. Regime classifier: determine what kind of market exists
Use transparent rules with an UNCERTAIN outcome. The thresholds below are placeholders for software development; calibrate them using historical data and walk-forward testing before making any claims about performance.
FUNCTION classify_market_regime(f):    IF f.data_quality < MIN_DATA_QUALITY:        RETURN Regime("UNCERTAIN", confidence=0)    IF required_features_missing(f):        RETURN Regime("UNCERTAIN", confidence=0)    trend_up = (        f.return_4h > POSITIVE_RETURN_THRESHOLD        AND f.trend_fast > f.trend_slow    )    trend_down = (        f.return_4h < NEGATIVE_RETURN_THRESHOLD        AND f.trend_fast < f.trend_slow    )    vol_high = (        f.realized_vol_short > HIGH_VOL_THRESHOLD        OR f.atm_iv > EXTREME_IV_THRESHOLD    )    vol_low = (        f.realized_vol_short < LOW_VOL_THRESHOLD        AND f.atm_iv < LOW_IV_THRESHOLD    )    flow_bullish = f.ofi > OFI_BULLISH_THRESHOLD    flow_bearish = f.ofi < OFI_BEARISH_THRESHOLD    # Extreme volatility is handled first.    IF vol_high:        RETURN Regime(            label="HIGH_VOL",            confidence=calculate_rule_confidence(f, "HIGH_VOL")



The confidence score should represent confidence in the rule-based label, not the probability of a profitable trade.
Prevent the regime from changing on every candle
FUNCTION apply_regime_hysteresis(new_regime):    append_to_regime_history(new_regime)    IF new_regime.label == "HIGH_VOL":        # Safety transition may happen immediately.        current_regime = "HIGH_VOL"        return current_regime    IF count_consecutive_same_label(new_regime.label) < REQUIRED_CONFIRMATIONS:        RETURN current_regime_or_uncertain()    IF regime_changed_too_recently():        RETURN current_regime    current_regime = new_regime.label    record_regime_transition(current_regime)    RETURN current_regime



A sudden change to high volatility, invalid market data, or a circuit-breaker event should be handled immediately. Normal trend transitions can require several observations to avoid excessive switching.
8. Strategy candidate generator
The generator should propose possible strategies but never authorize them. Each candidate must contain exact option instruments, legs, quantities, expiry, and the strategy's intended payoff.
For the first version, prioritize vertical spreads and iron condors. Add covered calls and cash-secured puts only after you have implemented their collateral, inventory, assignment, and settlement accounting.
FUNCTION generate_candidates(regime, features, quotes, instruments):    candidates = []    FOR each underlying IN ["BTC", "ETH"]:        chain = eligible_options_for(underlying)        IF regime.label == "BULLISH":            candidates += generate_bull_put_spreads(chain, features)            candidates += generate_bull_call_spreads(chain, features)        ELSE IF regime.label == "BEARISH":            candidates += generate_bear_call_spreads(chain, features)            candidates += generate_bear_put_spreads(chain, features)        ELSE IF regime.label == "RANGE":            candidates += generate_iron_condors(chain, features)        ELSE IF regime.label == "HIGH_VOL":            # Default behavior: conservative, not automatically short volatility.            candidates += generate_defined_risk_candidates_only_if_approved(                chain, features            )        ELSE:            CONTINUE    FOR candidate IN candidates:        candidate.valid = (            all_legs_have_valid_quotes(candidate)            AND all_legs_share_valid_expiry(candidate)            AND leg_directions_are_correct(candidate)            AND strikes_are_ordered_correctly(candidate)            AND quantities_match(candidate)



Strategy-by-regime map
Regime	Candidate strategy	Why it might be considered	Why the engine must reject it
Bullish	Bull put credit spread	A defined-risk bullish/neutral thesis	Poor credit, weak trend, excessive downside risk
Bullish	Bull call debit spread	Defined-risk bullish exposure	Excessive debit, weak reward relative to risk
Bearish	Bear call credit spread	A defined-risk bearish/neutral thesis	Poor credit, upward momentum, excessive risk
Bearish	Bear put debit spread	Defined-risk bearish exposure	Excessive debit, poor payoff, weak confirmation
Range-bound	Iron condor	Price may remain between short strikes	Breakout risk, poor liquidity, insufficient credit
High volatility	No trade by default	Market conditions may be unstable	Unbounded uncertainty, excessive spreads, gap risk
Uncertain	No trade	Signals conflict or are insufficient	No defensible strategy selection
These are candidate mappings, not guaranteed profitable strategies. Standard option strategy descriptions and payoff concepts are available from the Options Industry Council. 

Options Education
+1



9. Construct each strategy precisely
Every option leg needs a sign convention. In this pseudocode, positive quantity means long and negative quantity means short.
OptionLeg:    instrument_id    option_type           # CALL or PUT    strike    expiry    quantity              # positive = long; negative = short    contract_multiplier    quote_currency    settlement_currency



A. Bull put credit spread
FUNCTION build_bull_put_spread(chain, short_strike, width):    short_put = find_put(chain, strike=short_strike)    long_put  = find_put(chain, strike=short_strike - width)    IF either_leg_missing:        REJECT("MISSING_LEG")    IF short_put.expiry != long_put.expiry:        REJECT("EXPIRY_MISMATCH")    candidate.legs = [        leg(short_put, quantity=-1),        leg(long_put, quantity=+1)    ]    candidate.strategy_type = "DEFINED_RISK_PUT_SPREAD"    RETURN candidate



B. Bear call credit spread
FUNCTION build_bear_call_spread(chain, short_strike, width):    short_call = find_call(chain, strike=short_strike)    long_call  = find_call(chain, strike=short_strike + width)    IF either_leg_missing:        REJECT("MISSING_LEG")    IF short_call.expiry != long_call.expiry:        REJECT("EXPIRY_MISMATCH")    candidate.legs = [        leg(short_call, quantity=-1),        leg(long_call, quantity=+1)    ]    candidate.strategy_type = "DEFINED_RISK_CALL_SPREAD"    RETURN candidate



C. Bull call debit spread
FUNCTION build_bull_call_spread(chain, lower_strike, width):    long_call  = find_call(chain, strike=lower_strike)    short_call = find_call(chain, strike=lower_strike + width)    candidate.legs = [        leg(long_call, quantity=+1),        leg(short_call, quantity=-1)    ]    candidate.strategy_type = "BULL_CALL_DEBIT_SPREAD"    RETURN candidate



D. Bear put debit spread
FUNCTION build_bear_put_spread(chain, upper_strike, width):    long_put  = find_put(chain, strike=upper_strike)    short_put = find_put(chain, strike=upper_strike - width)    candidate.legs = [        leg(long_put, quantity=+1),        leg(short_put, quantity=-1)    ]    candidate.strategy_type = "BEAR_PUT_DEBIT_SPREAD"    RETURN candidate



E. Iron condor
FUNCTION build_iron_condor(chain, put_short, put_width,                            call_short, call_width):    short_put = find_put(chain, put_short)    long_put  = find_put(chain, put_short - put_width)    short_call = find_call(chain, call_short)    long_call  = find_call(chain, call_short + call_width)    candidate.legs = [        leg(long_put, quantity=+1),        leg(short_put, quantity=-1),        leg(short_call, quantity=-1),        leg(long_call, quantity=+1)    ]    candidate.strategy_type = "IRON_CONDOR"    IF strikes_overlap_in_invalid_way(candidate):        REJECT("INVALID_CONDOR_STRIKES")    RETURN candidate



Do not assume a particular strike is available just because it fits the desired distance from spot. The generator must search actual instruments, match expiry, validate quotes, and reject incomplete combinations.
10. Price candidates and calculate the full payoff
This is one of the most important components. A candidate must never be selected based only on its displayed premium.
For an initial simplified model, assume a linear option payoff expressed in the same currency, with a multiplier of one. The production calculation must use the exchange's actual contract specification, quote currency, settlement currency, and multiplier.
Conservative entry-price estimation
For a long leg, assume buying at the ask plus a slippage allowance. For a short leg, assume selling at the bid minus a slippage allowance.
FUNCTION estimate_executable_pricing(candidate):    total_entry_cashflow = 0    total_fees = 0    total_slippage = 0    FOR leg IN candidate.legs:        quote = get_fresh_quote(leg.instrument_id)        IF quote is invalid:            RETURN INVALID("QUOTE_INVALID")        IF leg.quantity > 0:            execution_price = quote.ask + estimated_slippage(leg, "BUY")        ELSE:            execution_price = quote.bid - estimated_slippage(leg, "SELL")        IF execution_price < 0:            RETURN INVALID("INVALID_EXECUTION_PRICE")        leg_cashflow = (            -1            * leg.quantity            * execution_price            * leg.contract_multiplier        )        total_entry_cashflow += leg_cashflow        total_fees += estimate_fee(leg, execution_price)        total_slippage += estimate_slippage_cost(leg)    net_initial_debit = (        total_entry_cashflow        + total_fees



Here, positive net_initial_debit means the position costs money to open; negative means a net credit is received. For short-option strategies, credits are not profit until the position is closed or settled and all costs are accounted for.
The slippage estimator must not count the same cost twice. If you adjust execution prices for slippage, retain that adjustment separately for reporting, and use a consistent fee and P&L convention.
Expiration payoff
For a vanilla option, the simplified payoff per underlying unit at expiry is:
FUNCTION option_intrinsic_value(option_type, strike, expiry_spot):    IF option_type == "CALL":        RETURN max(expiry_spot - strike, 0)    IF option_type == "PUT":        RETURN max(strike - expiry_spot, 0)



The full position payoff is:
FUNCTION payoff_at_expiry(candidate, expiry_spot):    total_payoff = 0    FOR leg IN candidate.legs:        intrinsic = option_intrinsic_value(            leg.option_type,            leg.strike,            expiry_spot        )        total_payoff += (            leg.quantity            * leg.contract_multiplier            * intrinsic        )    RETURN total_payoff



This simplified formula is only valid after normalizing the instrument's contract and settlement conventions. Inverse or coin-settled options may require a different payoff and conversion model.
Maximum profit and loss
FUNCTION calculate_full_payoff(candidate):    prices = generate_expiry_price_grid(        current_spot=candidate.spot,        strikes=candidate.all_strikes,        include_extreme_downside=True,        include_extreme_upside=True    )    pnl_values = []    FOR price IN prices:        expiry_value = payoff_at_expiry(candidate, price)        pnl = (            expiry_value            - candidate.initial_debit            - candidate.entry_fees            - candidate.estimated_exit_fees        )        pnl_values.append(pnl)    IF payoff_model_is_not_valid_for_instrument(candidate):        RETURN INVALID("PAYOFF_MODEL_NOT_SUPPORTED")    IF grid_does_not_capture_all_payoff_regions(candidate, prices):        RETURN INVALID("PAYOFF_GRID_INCOMPLETE")    max_profit = maximum(pnl_values)    worst_grid_loss = abs(minimum(pnl_values))    # A finite grid is not sufficient to prove a loss bound.    # Use a verified analytical payoff bound for the strategy.    analytical_bound = calculate_analytical_payoff_bounds(candidate)



For a simple vertical credit spread, in consistent per-unit terms:
\[
\text{Maximum profit}=\text{net credit}
\]

\[
\text{Maximum loss}=\text{spread width}-\text{net credit}
\]

Those formulas need adjustment for fees, contract multipliers, settlement conventions, and any extra execution costs. The engine should reject any spread whose computed net credit, width, or loss bound is invalid.
11. Strategy scoring engine
Scoring ranks eligible candidates. It must never override a failed hard risk check.
FUNCTION score_candidate(candidate, features, pricing, payoff, greeks, risk):    # Normalize every component to a 0.0–1.0 scale.    regime_fit = score_regime_fit(candidate, features.regime)    reward_risk = score_reward_relative_to_max_loss(        payoff.max_profit,        payoff.max_loss    )    liquidity = score_liquidity(        candidate.legs,        features.median_spread,        features.depth_score    )    volatility_fit = score_volatility_fit(        candidate,        features.atm_iv,        features.realized_vol_short,        features.iv_term_structure    )    trend_confirmation = score_trend_confirmation(        candidate,        features.trend,        features.ofi,        features.obi    )    execution_quality = score_execution_quality(        pricing.estimated_slippage,        pricing.estimated_fees,        candidate.legs    )



How the engine compares two candidates
Suppose the engine generates three hypothetical spreads:
Candidate	Regime fit	Liquidity	Reward/risk	Decision
A	0.85	0.90	0.70	Eligible if all hard checks pass
B	0.95	0.35	0.90	Reject if liquidity fails the minimum
C	0.65	0.90	0.80	May be eligible but rank lower
These numbers are illustrative, not market data. Candidate B cannot win merely because its other scores are high. If all candidates fail a hard constraint, the engine selects NO_TRADE.
12. Position sizing and portfolio risk gate
Use a simulated NAV and keep risk denominated in a consistent accounting currency.
FUNCTION calculate_position_size(candidate, portfolio):    nav = portfolio.net_asset_value    IF nav <= 0:        RETURN REJECT("INVALID_NAV")    risk_budget = nav * CONFIG.max_risk_per_trade_pct_nav    risk_per_unit = candidate.payoff.max_loss    IF risk_per_unit <= 0:        RETURN REJECT("INVALID_MAX_LOSS")    affordable_units = floor_to_valid_lot(        risk_budget / risk_per_unit,        candidate.minimum_trade_amount    )    IF affordable_units < candidate.minimum_trade_amount:        RETURN REJECT("SIZE_BELOW_EXCHANGE_MINIMUM")    WHILE affordable_units > 0:        sized_candidate = scale_candidate(candidate, affordable_units)        IF collateral_model_passes(sized_candidate):            IF all_leg_quantities_are_valid(sized_candidate):                RETURN ACCEPT(sized_candidate)        affordable_units = reduce_to_next_valid_lot(affordable_units)    RETURN REJECT("NO_VALID_POSITION_SIZE")



The floor_to_valid_lot function must respect the exchange's actual minimum trade amount and supported quantity increments. Do not assume that contract size and minimum order amount mean the same thing; Deribit documents them separately. 

Deribit Support
+1



Portfolio veto
FUNCTION portfolio_risk_gate(candidate, risk):    portfolio = load_current_portfolio()    IF portfolio.open_position_count >= CONFIG.max_open_positions:        RETURN REJECT("POSITION_COUNT_LIMIT")    IF risk.max_loss is missing OR risk.max_loss <= 0:        RETURN REJECT("INVALID_RISK")    IF risk.collateral_required is missing:        RETURN REJECT("COLLATERAL_UNKNOWN")    IF risk.collateral_required > portfolio.available_collateral:        RETURN REJECT("INSUFFICIENT_COLLATERAL")    proposed_total_risk = (        portfolio.total_open_max_loss        + risk.max_loss    )    IF proposed_total_risk >       portfolio.nav * CONFIG.max_total_open_risk_pct_nav:        RETURN REJECT("TOTAL_RISK_LIMIT")    IF portfolio.daily_realized_and_unrealized_loss >=       portfolio.start_of_day_nav * CONFIG.max_daily_loss_pct_nav:        RETURN REJECT("DAILY_LOSS_LIMIT")    IF portfolio.current_drawdown >= CONFIG.max_drawdown_pct_nav:        RETURN REJECT("DRAWDOWN_LIMIT")    IF correlation_or_concentration_limit_breached(candidate, portfolio):        RETURN REJECT("CONCENTRATION_LIMIT")    RETURN ACCEPT()



For the MVP, treat a risk limit as a hard boundary, not a target to use up. A position should not be opened merely because some risk budget remains.
13. Entry approval and paper order execution
The MVP's first version should use its own simulated order executor. You can separately verify that your data adapter connects to Deribit testnet, but do not give the strategy engine a path to production order-placement methods.
FUNCTION final_pre_trade_gate(candidate):    REQUIRE system_environment == "TESTNET_PAPER_ONLY"    REQUIRE CONFIG.allow_live_orders == False    REQUIRE all_quotes_fresh(candidate)    REQUIRE all_legs_valid(candidate)    REQUIRE payoff_is_analytically_bounded(candidate)    REQUIRE max_loss_is_finite(candidate)    REQUIRE collateral_is_verified(candidate)    REQUIRE estimated_fees_are_available(candidate)    REQUIRE position_size_is_valid(candidate)    REQUIRE portfolio_limits_pass(candidate)    REQUIRE no_duplicate_exposure(candidate)    REQUIRE no_kill_switch()    REQUIRE no_daily_loss_breach()    REQUIRE no_drawdown_breach()    RETURN all_requirements_passed



Conservative simulated fills
FUNCTION simulate_conservative_fill(paper_order):    FOR leg IN paper_order.legs:        quote = refresh_quote(leg.instrument_id)        IF quote is stale OR invalid:            RETURN NOT_FILLED("STALE_OR_INVALID_QUOTE")        IF leg.side == "BUY":            estimated_fill = quote.ask + slippage_buffer(leg)        ELSE:            estimated_fill = quote.bid - slippage_buffer(leg)        IF estimated_fill violates the simulated limit price:            RETURN NOT_FILLED("LIMIT_PRICE_NOT_REACHED")        IF available_depth_is_insufficient(leg):            RETURN NOT_FILLED("INSUFFICIENT_SIMULATED_DEPTH")    IF multi_leg_execution_model_is_not_supported(paper_order):        RETURN NOT_FILLED("MULTI_LEG_MODEL_UNSUPPORTED")    fill = calculate_all_leg_fills(paper_order)    IF any_leg_filled_inconsistently(fill):        RETURN UNRESOLVED("PARTIAL_OR_UNBALANCED_FILL")    RETURN FILLED(        fill_prices=fill.prices,        fees=calculate_fees(fill),        timestamp=utc_now(),        simulated=True    )



Important: a paper fill is not proof of a real executable fill. For the MVP, the simplest safe model is to simulate the entire spread atomically at conservative prices. If you later model individual leg fills, you must also model partial fills, temporary unhedged exposure, cancellation, and recovery.
Do not silently treat a partially filled spread as a complete position.
14. Exit engine: decide when to close a simulated position
The entry engine and exit engine should be separate modules. A position's exit rules must remain active even when the market regime changes or the strategy selector is disabled.
Create an exit policy at entry
FUNCTION create_exit_policy(position, entry_features):    policy = ExitPolicy()    policy.max_loss_limit = position.approved_loss_limit    policy.daily_loss_limit = CONFIG.max_daily_loss_pct_nav    policy.expiry_cutoff = calculate_expiry_cutoff(position.expiry)    policy.thesis_invalidation = define_thesis_invalidation(        position.strategy_type,        entry_features    )    policy.profit_target = OPTIONAL_CONFIGURED_PROFIT_TARGET    policy.time_exit = OPTIONAL_CONFIGURED_TIME_EXIT    policy.close_reason_priority = [        "KILL_SWITCH",        "DATA_OR_POSITION_INTEGRITY_FAILURE",        "PORTFOLIO_CIRCUIT_BREAKER",        "MAX_LOSS_TRIGGER",        "THESIS_INVALIDATION",        "TIME_EXIT",        "PROFIT_TARGET"    ]    RETURN policy



These are software control rules. They are not universally optimal trading exits. The profit target, time exit, and thesis invalidation thresholds must be tested for each strategy and expiry.
Monitor an open position
FUNCTION monitor_open_position(position):    IF position.status != "OPEN":        RETURN NO_ACTION    data = fetch_position_market_data(position)    IF data is invalid:        mark_position_for_manual_review(position)        block_new_entries()        RETURN ALERT("POSITION_DATA_UNAVAILABLE")    current_pnl = mark_to_market_using_executable_quotes(position, data)    update_position_metrics(position, current_pnl)    IF system_kill_switch_is_on():        RETURN request_paper_close(position, "KILL_SWITCH")    IF position_integrity_check_failed(position):        RETURN request_paper_close_or_flag_unresolved(            position, "POSITION_INTEGRITY_FAILURE"        )    IF portfolio_circuit_breaker_triggered():        block_new_entries()        RETURN request_paper_close_or_flag_unresolved(            position, "PORTFOLIO_CIRCUIT_BREAKER"        )    IF current_pnl <= -position.exit_policy.max_loss_limit:        RETURN request_paper_close_or_flag_unresolved(            position, "MAX_LOSS_TRIGGER"        )    IF thesis_is_invalidated(position, data):



Why a stop-loss is not a guaranteed maximum loss
A stop trigger merely tells the engine to attempt a simulated close. Real options can have wide spreads, limited liquidity, price gaps, and settlement complications. Even defined-risk positions may lose their entire defined maximum loss, including costs; short options without a properly constructed hedge may have substantially greater or unbounded risk.
For the testnet MVP:
- Mark positions using executable-side quotes rather than relying solely on mark prices.
- Record the time a stop triggers and the simulated price at which it closes.
- Include fees and slippage in realized P&L.
- If quotes disappear, mark the position as unresolved and stop opening new positions.
- Keep a manual kill switch available in the dashboard.
15. Position lifecycle and state machine
Each position must have an explicit state. Do not manage positions by merely adding and removing rows from a dashboard.
ENUM PositionState:    PROPOSED    REJECTED    APPROVED    PAPER_PENDING    OPEN    EXIT_TRIGGERED    PAPER_CLOSING    CLOSED    EXPIRED    UNRESOLVED



Allowed transitions:
PROPOSED -> REJECTEDPROPOSED -> APPROVEDAPPROVED -> PAPER_PENDINGPAPER_PENDING -> OPENPAPER_PENDING -> REJECTEDPAPER_PENDING -> UNRESOLVEDOPEN -> EXIT_TRIGGEREDOPEN -> EXPIREDOPEN -> UNRESOLVEDEXIT_TRIGGERED -> PAPER_CLOSINGPAPER_CLOSING -> CLOSEDPAPER_CLOSING -> UNRESOLVEDUNRESOLVED -> OPEN       # Only after reconciliation proves it is still open.UNRESOLVED -> CLOSED     # Only after reconciliation proves it is closed.



Every transition should record its timestamp, reason, input snapshot, and previous state. Expiration and settlement must be handled explicitly; do not simply assume that every option expires worthless or that every paper position closes at its last displayed price.
16. Main background loop
The dashboard should display the engine's results, not be responsible for running the entire strategy lifecycle itself.
FUNCTION start_regimevault_engine():    initialize_database()    load_configuration()    validate_configuration()    run_startup_self_tests()    IF self_tests_failed:        disable_engine()        log_event("STARTUP_FAILED")        EXIT    restore_positions_from_database()    reconcile_paper_position_states()    WHILE engine_is_enabled():        TRY:            # Manage existing risk first.            FOR position IN get_open_positions():                monitor_open_position(position)            # No new entries if an unresolved incident remains.            IF unresolved_position_exists():                block_new_entries()                sleep(CONFIG.decision_interval_seconds)                CONTINUE            IF new_entries_are_paused():                sleep(CONFIG.decision_interval_seconds)                CONTINUE            decision = run_decision_cycle()            save_decision(decision)            update_dashboard(decision)



For the initial version, run a single process with a database such as SQLite. Avoid adding a message queue, distributed workers, or complex cloud infrastructure until the engine is working and its behavior is reproducible.
17. Database design
At minimum, persist the following records.
Table	Purpose	Essential fields
market_snapshots	Preserve inputs used by the engine	timestamp, underlying, prices, IV, spreads, quality flags
regime_decisions	Explain each regime classification	label, confidence, features, rule version
candidates	Track generated and rejected strategies	strategy type, legs, score, rejection reason
risk_checks	Explain every risk gate	check name, input values, pass/fail, reason
paper_orders	Track simulated execution	candidate ID, order state, limit, simulated fills
positions	Track open and closed paper positions	legs, quantity, entry cost, max loss, state
position_events	Maintain a position audit trail	old state, new state, timestamp, reason
engine_events	Record faults and safety events	event type, severity, details
configuration_versions	Reproduce old decisions	config hash, rule version, timestamp
Never store private API credentials in market snapshots, application logs, or the browser. Public market-data access should be separate from any private testnet account integration.
18. Required tests before testnet launch
Write tests for the risk logic before implementing automatic candidate selection.
TEST "stale market data":    input = snapshot_with_old_timestamp()    result = run_decision_cycle(input)    ASSERT result.action == "NO_TRADE"TEST "missing option leg":    candidate = spread_with_missing_long_leg()    result = calculate_full_payoff(candidate)    ASSERT result.is_valid == FalseTEST "invalid payoff":    candidate = spread_with_missing_multiplier()    result = calculate_full_payoff(candidate)    ASSERT result.is_valid == FalseTEST "risk budget exceeded":    candidate = valid_spread_with_excessive_max_loss()    result = portfolio_risk_gate(candidate)    ASSERT result.allowed == FalseTEST "all candidates rejected":    candidates = [candidate_that_fails_risk]    result = select_candidate(candidates)    ASSERT result.action == "NO_TRADE"TEST "conservative buy fill":    quote = bid_ask_quote()    fill = simulate_buy_fill(quote)    ASSERT fill.price >= quote.askTEST "conservative sell fill":    quote = bid_ask_quote()    fill = simulate_sell_fill(quote)    ASSERT fill.price <= quote.bidTEST "kill switch":



Also test the payoff at prices below every strike, between each strike, and above every strike. Include fees, contract multipliers, settlement currency conversion, expiry, and adverse quote changes. For multi-leg strategies, verify the payoff at all breakpoints analytically rather than relying on a coarse price grid.
19. Build order for a beginner
Use this order rather than trying to implement everything at once.
1. Make the engine deterministic. Create data models for quotes, options, strategy legs, candidates, decisions, and positions.
2. Build and test payoff calculations. Support one defined-risk vertical spread with correct units, fees, and contract metadata.
3. Connect public testnet market data. Retrieve instruments, ticker/book data, and options data using documented API methods. Validate freshness and missing fields. 
   
   Cryptocurrency Derivatives Trading API
   +1
4. Implement one strategy end to end. Generate a bull put spread, price both legs, compute maximum loss, run the portfolio gate, and simulate the entry.
5. Add position monitoring. Implement mark-to-market, exit triggers, unresolved states, and restart recovery.
6. Add the regime classifier. Introduce bullish, bearish, range-bound, high-volatility, and uncertain labels.
7. Add more candidate strategies. Add the bear call spread, debit spreads, and iron condor only after the first strategy passes tests.
8. Add scoring and persistence. Rank candidates after hard filters and store every decision and rejection reason.
9. Run a paper-trading evaluation. Test repeated decisions, conservative fills, adverse scenarios, drawdown controls, and reproducibility.
Testnet connectivity
Use Deribit's official documentation as the API source of truth:
- Deribit API documentation
- Deribit testnet
- Deribit testnet guidance
The documented testnet HTTP API base is https://test.deribit.com/api/v2; the WebSocket endpoint is wss://test.deribit.com/ws/api/v2. Testnet and production are separate environments. 

Cryptocurrency Derivatives Trading API
+1



For the first milestone, use public data methods and simulated fills. Do not add authenticated order placement yet. Later, if you want to test exchange order workflows, use a separate testnet-only adapter with credentials stored outside source code and a hard-coded production-deny safeguard. Testnet liquidity and synthetic funds do not establish that the engine is ready for live trading. 

Deribit Support
+1



20. Definition of done
The RegimeVault MVP is ready for a testnet demonstration when all these are true:
- [ ] It fetches and validates BTC/ETH market data, or clearly reports a data outage.
- [ ] It identifies regimes with documented rules and confidence.
- [ ] It generates complete candidate legs from actual available instruments.
- [ ] It calculates payoff and risk using the correct contract specifications.
- [ ] It rejects candidates that fail liquidity, collateral, position-size, or portfolio checks.
- [ ] It never opens a position if all candidates fail.
- [ ] It simulates conservative fills and handles partial/unresolved states safely.
- [ ] It monitors exits, persists positions, and recovers after restart.
- [ ] It logs every decision, including why candidates were rejected.
- [ ] Its automated tests pass, including adverse and failure scenarios.
- [ ] The production order endpoint is unreachable from the MVP execution path.
This produces a demonstrable engineering product for early grant applications: a transparent, risk-gated options strategy research engine. It does not prove profitability, and a successful testnet run should not be presented as evidence of live-market returns.
