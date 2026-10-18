"""Command-line runner:  python -m regimevault.cli --source synthetic --scenario bull --cycles 5"""
import argparse
from datetime import timedelta
from .config import Config
from .engine import Engine
from .market_data import SyntheticProvider, DeribitTestnetProvider
from .models import utcnow
from .store import Store


def main(argv=None):
    ap = argparse.ArgumentParser(description="RegimeVault paper-trading engine (testnet/paper only)")
    ap.add_argument("--source", choices=["synthetic", "deribit"], default="synthetic")
    ap.add_argument("--scenario", default="bull", choices=["bull", "bear", "range", "highvol"])
    ap.add_argument("--cycles", type=int, default=5)
    ap.add_argument("--db", default=":memory:")
    a = ap.parse_args(argv)

    cfg = Config()
    store = Store(a.db)
    # simulated clock advances 1 minute per cycle so regime hysteresis can be demonstrated offline
    t = {"now": utcnow()}
    clock = (lambda: t["now"]) if a.source == "synthetic" else utcnow
    provider = SyntheticProvider(cfg, a.scenario, clock=clock) if a.source == "synthetic" else DeribitTestnetProvider(cfg)
    engine = Engine(cfg, provider, store, clock=clock)
    for n in range(a.cycles):
        for d in engine.run_decision_cycle():
            extra = f" {d.selected_strategy} x{d.units} score={d.candidate_score}" if d.action == "PAPER_OPEN" else ""
            print(f"[cycle {n+1}] {d.underlying:3} {d.regime:9} conf={d.confidence:.2f} {d.action:10} {d.reason_code}{extra}")
        if a.source == "synthetic":
            t["now"] += timedelta(minutes=1)
    print("\nOpen positions:")
    for p in engine.open_positions():
        print(f"  {p.underlying} {p.strategy_type} units={p.units} max_loss={p.max_loss:.2f} pnl={p.last_pnl:.2f}")
    print(f"NAV: {engine.current_nav():.2f}")


if __name__ == "__main__":
    main()
