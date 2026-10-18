"""RegimeVault dashboard (Streamlit).  Displays and triggers the engine; it is NOT where strategy logic lives.
Paper / testnet only.  No orders are ever sent to an exchange."""
import json
import pandas as pd
import streamlit as st

from regimevault.config import Config, config_hash
from regimevault.engine import Engine, EngineDisabled
from regimevault.market_data import SyntheticProvider, DeribitTestnetProvider
from regimevault.store import Store
from regimevault import positions as P

DB_PATH = "regimevault.db"
st.set_page_config(page_title="RegimeVault", page_icon="📊", layout="wide")
st.title("RegimeVault")
st.caption("BTC / ETH options strategy engine · rules-based · PAPER TRADING ONLY · no exchange orders are ever sent")

cfg = Config()


def make_provider(source, scenario):
    if source == "Synthetic (offline demo)":
        return SyntheticProvider(cfg, scenario)
    return DeribitTestnetProvider(cfg)


with st.sidebar:
    st.header("Controls")
    source = st.radio("Market data source", ["Synthetic (offline demo)", "Deribit testnet (public data)"])
    scenario = st.selectbox("Synthetic scenario", ["range", "bull", "bear", "highvol"])
    st.caption("Synthetic data is generated, not market data. Testnet liquidity is artificial; neither "
               "says anything about live-market performance.")

if "engine" not in st.session_state:
    try:
        st.session_state.engine = Engine(cfg, make_provider(source, scenario), Store(DB_PATH))
    except EngineDisabled as exc:
        st.error(f"Engine refused to start: {exc}")
        st.stop()
engine: Engine = st.session_state.engine
try:
    engine.provider = make_provider(source, scenario)
except Exception as exc:
    st.error(f"Could not create data provider: {exc}")
    st.stop()

with st.sidebar:
    kill = st.toggle("🛑 Kill switch (block entries, close paper positions)", value=engine.store.kill_switch_on())
    if kill != engine.store.kill_switch_on():
        engine.store.set_kill_switch(kill)
    st.caption(f"Config hash `{config_hash(cfg)}` · env `{cfg.environment}` · live orders: {cfg.allow_live_orders}")

if st.button("Run decision cycle", type="primary"):
    with st.spinner("Running cycle..."):
        st.session_state.last = engine.run_decision_cycle()
    st.caption("The regime needs several consecutive confirmations (hysteresis); run a few cycles before expecting entries.")

nav = engine.current_nav()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Paper NAV (USD)", f"{nav:,.2f}")
c2.metric("Drawdown", f"{engine.account.drawdown(nav):.2%}")
c3.metric("Open positions", len(engine.open_positions()))
c4.metric("Realized P&L", f"{engine.account.realized_pnl:,.2f}")

for d in st.session_state.get("last", []):
    with st.expander(f"{d.underlying} · {d.regime} (confidence {d.confidence:.0%}) → {d.action}: {d.reason_code}", expanded=True):
        st.write(d.explanation)
        if d.action == "PAPER_OPEN":
            st.write(f"**{d.selected_strategy}** × {d.units} · score {d.candidate_score} · "
                     f"max loss {float(d.max_loss):,.2f} · max profit {float(d.max_profit):,.2f} · "
                     f"collateral {float(d.collateral_required):,.2f}")
            st.dataframe(pd.DataFrame([{"instrument": l["instrument"]["instrument_id"], "qty": l["quantity"]}
                                       for l in d.selected_legs]), hide_index=True)
        if d.rejected_candidates:
            st.caption(f"{d.n_candidates} candidates generated; sample of rejections:")
            st.dataframe(pd.DataFrame(d.rejected_candidates).head(15), hide_index=True)

st.subheader("Positions")
rows = [{"id": p.position_id, "asset": p.underlying, "strategy": p.strategy_type, "state": p.state,
         "units": str(p.units), "max_loss": float(p.max_loss), "mtm_pnl": float(p.last_pnl),
         "realized": None if p.realized_pnl is None else float(p.realized_pnl), "review": p.needs_review,
         "expiry": p.expiry.date().isoformat()} for p in engine.positions]
st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True) if rows else st.info("No positions yet.")

tab1, tab2 = st.tabs(["Decision log", "Engine events"])
with tab1:
    st.dataframe(pd.DataFrame(engine.store.rows("decisions", 100)).drop(columns=["payload"], errors="ignore"),
                 use_container_width=True, hide_index=True)
with tab2:
    st.dataframe(pd.DataFrame(engine.store.rows("engine_events", 100)), use_container_width=True, hide_index=True)

st.caption("Educational research prototype. Not investment advice. A paper or testnet result does not indicate live-market performance.")
