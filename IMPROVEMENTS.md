# RegimeVault v0.2 — where to improve

Everything below comes from building and probing this version. "Measured" means I ran it; "untested" means I could not.
Environment limits: the sandbox had no internet, so **nothing was run against the real Deribit testnet**, and
**Streamlit/pytest were not installed** (tests ran via `run_tests.py`; the UI was only smoke-run against a stub).

## What exists now (maps to your spec)
| Spec section | Status |
|---|---|
| 2 Decision contract | Done (`Decision`) |
| 3 Config + safety rules | Done; `validate_config` refuses live orders / non-paper environment |
| 4 Decision cycle | Done (`engine.py`) |
| 5 Validation | Done (spot, clock skew, quotes, eligibility, data quality) |
| 6 Features | Partial: trend, realised vol, ATM IV, put/call skew, OFI, spread. **Missing: term structure, order-book imbalance, open interest/volume** |
| 7 Regime + hysteresis | Done |
| 8–9 Generation / construction | Done for all 5 structures, from real listed strikes |
| 10 Pricing + payoff | Done (Decimal, analytic bounds, grid cross-check) |
| 11 Scoring | Done, weights are arbitrary |
| 12 Sizing + portfolio veto | Done |
| 13 Paper execution | Done (atomic conservative fills) |
| 14–15 Exits + state machine | Done |
| 16 Background loop | **Not done** — cycles are triggered by CLI/button, no scheduler |
| 17 Database | Done (SQLite, all 9 tables) |
| 18 Tests | 56 tests, all passing (run with `python run_tests.py` or `python -m pytest`) |
| 20 Definition of done | Met in code **except** "fetches real testnet data" (untested) and the background loop |

## Priority 1 — correctness issues you must resolve before trusting any output
1. **Deribit options are coin-settled; the engine approximates them in USD.** The adapter converts BTC-quoted premiums to USD
   at the snapshot price and tags them `INVERSE_COIN_NORMALIZED`. Real P&L, collateral and margin are in BTC/ETH and move with the
   underlying. Build a real inverse-payoff and margin model. Until then every dollar figure is an approximation.
2. **Real-API field handling is unverified.** Parsers are tested only against payloads I wrote from my understanding of the API.
   Run `python -m regimevault.cli --source deribit --cycles 1` on your machine and check the first output. Testnet book summaries
   have no depth, so the depth check is skipped by default (`require_depth_info=False`).
3. **The stop-loss is mis-calibrated against round-trip costs (measured).** MTM uses executable-side quotes, so a position starts
   already "down". At entry, iron condors had used **56–67% of their stop distance** (42–50% of max loss) before the market moved;
   debit spreads 38–48%; credit spreads 29%. Stops will fire on spread noise. Fix: measure the stop from the entry MTM baseline, or
   trigger on mid marks and charge exit costs at close.
4. **Expiry settlement uses the index price at monitoring time**, not Deribit's delivery price, and only if a cycle runs near expiry.
5. **A `PAPER_PENDING`/`UNRESOLVED` position has no reconciliation procedure** — the state transitions exist, but nothing proves
   whether a position is really open or closed, so these states block trading until a human intervenes.

## Priority 2 — strategy quality (nothing here is evidence of an edge)
6. **Regime classifier misses clear trends (measured).** Over 60 seeds x 2 assets of *my own synthetic scenarios*: bull labelled
   BULLISH 84%, bear 88%, range 100%, high-vol 100%; the misses are `UNCERTAIN`, caused by the single 4-hour return gate (e.g. BTC
   −6.7% over 24h, fast MA below slow MA, bearish flow — but 4h return −0.48% < threshold). These numbers are circular (the
   scenarios were built to match the rules); real data will be worse. Use multi-horizon agreement, then calibrate with walk-forward tests.
7. **Positions stack on consecutive cycles (measured).** Two ETH bear put spreads and two BTC iron condors were opened one minute apart,
   same thesis, different strikes. Add a re-entry cooldown and a per-thesis exposure cap.
8. **BTC and ETH are treated as independent.** Three bullish positions across both assets can be open at once. Add a correlation /
   same-direction cap, as your spec's `concentration_limit` intends (only a position-count limit exists now).
9. **Scoring weights, slippage (25% of spread) and fee model are assumptions.** Calibrate against recorded testnet quotes/fills.
   IV-vs-realised-vol is a heuristic in the score, not a validated signal.
10. **Strikes are chosen by % out-of-the-money, not delta.** Add Greeks (delta/theta/vega) for strike selection and portfolio exposure.
11. **Thresholds are placeholders** (`config.py`): return, vol, OFI, score (0.70), reward/risk (0.15). None are calibrated.

## Priority 3 — testing and engineering
12. **The synthetic provider is static** — the same price path every call, so positions barely evolve and P&L dynamics (stops, targets,
    drawdown) are exercised only in unit tests. Build a replay provider that plays recorded snapshots, and a Monte Carlo path generator.
13. **No historical backtest or walk-forward harness** — required before claiming anything about performance.
14. **Regime tracker state is not persisted** — after a restart it re-enters warm-up (safe, but means ~3 cycles of no entries).
15. **Add the background loop** (spec §16): scheduler, interval, graceful shutdown, reconcile-on-start. Keep the dashboard read-only
    apart from the kill switch.
16. **Mixed float/Decimal**: money is Decimal; features are float (fine) but IV/skew inputs are not unit-tested against known values.
17. **Untested here:** Windows run, Python 3.10 (written to be compatible, not verified), real Streamlit rendering, `pytest` collection.
18. Add CI, structured logging, config-file loading, and DB migrations.

## Deviations from your spec (deliberate — change if you disagree)
- `allowed_strategies` includes the two debit spreads (your §3 config lists three; §8–9 describe five).
- Sizing uses the closed-form lot floor instead of the `WHILE` loop (equivalent result; tested).
- Risk gate evaluates *all* checks instead of stopping at the first failure, so the audit trail is complete.
- Position sizing, fills and NAV are USD-normalised (see item 1).

## Safety properties verified by tests
No `private/` endpoints, credentials, websockets or order methods anywhere in the package (static scan); production hosts are
refused (`ProductionDenied`); only `public/` methods callable; `allow_live_orders=True` or a non-paper environment prevents startup;
kill switch persists across restarts and closes paper positions; vanished quotes mark positions for review and block entries.
