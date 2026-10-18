# RegimeVault — BTC/ETH options strategy research engine (v0.2)

Rules-based, explainable **paper-trading** engine built from the "Automatic Strategy Selection Engine" spec:
data validation → features → regime (with hysteresis) → real-leg candidates → conservative pricing and analytic payoff →
scoring → sizing → risk veto → simulated fill → position state machine → SQLite audit trail.

**It never sends exchange orders, uses credentials, or moves funds. A paper/testnet result is not evidence of live performance.**
See `IMPROVEMENTS.md` for known gaps (read priority 1 first).

## Run (Windows PowerShell)
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest              # or: python run_tests.py   (no pytest needed)
python -m regimevault.cli --scenario range --cycles 5      # offline demo, synthetic data
streamlit run app.py                                       # dashboard
python -m regimevault.cli --source deribit --cycles 1      # public testnet data (needs internet)
```
macOS/Linux: same, with `python3 -m venv .venv && source .venv/bin/activate`.

The regime needs 3 consecutive confirmations (hysteresis), so the first two cycles never trade — by design.

## Layout
`config` (all thresholds) · `models` · `validation` · `features` · `regime` · `generator` · `payoff` · `scoring` · `risk` ·
`execution` (paper fills only) · `positions` (state machine, exits) · `store` (SQLite) · `engine` (cycle) · `market_data`
(public Deribit testnet + synthetic) · `cli` · `app.py` (dashboard) · `tests/`.
