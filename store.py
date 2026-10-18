"""SQLite persistence: every decision, rejection, risk check, order, position and event is stored.
Never store API credentials here (this MVP uses public endpoints only)."""
import json
import sqlite3
from datetime import datetime
from typing import List, Optional
from .positions import Position, PaperAccount, TRANSITIONS, LIVE_STATES

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots(id INTEGER PRIMARY KEY, ts TEXT, underlying TEXT, source TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS regime_decisions(id INTEGER PRIMARY KEY, ts TEXT, underlying TEXT, label TEXT, raw_label TEXT, confidence REAL, payload TEXT);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, ts TEXT, cycle_id TEXT, underlying TEXT, action TEXT, reason_code TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS candidates(id INTEGER PRIMARY KEY, cycle_id TEXT, underlying TEXT, candidate_id TEXT, status TEXT, reason TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS risk_checks(id INTEGER PRIMARY KEY, cycle_id TEXT, underlying TEXT, candidate_id TEXT, check_name TEXT, passed INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS paper_orders(id INTEGER PRIMARY KEY, ts TEXT, cycle_id TEXT, candidate_id TEXT, status TEXT, reason TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS positions(position_id TEXT PRIMARY KEY, state TEXT, payload TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS position_events(id INTEGER PRIMARY KEY, position_id TEXT, ts TEXT, old_state TEXT, new_state TEXT, reason TEXT, snapshot TEXT);
CREATE TABLE IF NOT EXISTS engine_events(id INTEGER PRIMARY KEY, ts TEXT, event_type TEXT, severity TEXT, details TEXT);
CREATE TABLE IF NOT EXISTS configuration_versions(config_hash TEXT PRIMARY KEY, ts TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS engine_state(key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    # ---- engine state / kill switch -----------------------------------------------
    def set_state(self, key: str, value: str):
        self.db.execute("INSERT INTO engine_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.db.commit()

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.db.execute("SELECT value FROM engine_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kill_switch_on(self) -> bool:
        return self.get_state("kill_switch", "0") == "1"

    def set_kill_switch(self, on: bool):
        self.set_state("kill_switch", "1" if on else "0")

    def save_account(self, acct: PaperAccount):
        self.set_state("account", json.dumps(acct.to_dict()))

    def load_account(self) -> Optional[PaperAccount]:
        raw = self.get_state("account")
        return PaperAccount.from_dict(json.loads(raw)) if raw else None

    # ---- logging ------------------------------------------------------------------
    def log_event(self, event_type: str, severity: str, details, now: datetime):
        self.db.execute("INSERT INTO engine_events(ts,event_type,severity,details) VALUES(?,?,?,?)",
                        (now.isoformat(), event_type, severity, json.dumps(details, default=str)))
        self.db.commit()

    def save_config_version(self, cfg_hash: str, payload: dict, now: datetime):
        self.db.execute("INSERT OR IGNORE INTO configuration_versions VALUES(?,?,?)",
                        (cfg_hash, now.isoformat(), json.dumps(payload, default=str)))
        self.db.commit()

    def save_snapshot_summary(self, now, underlying, source, summary: dict):
        self.db.execute("INSERT INTO market_snapshots(ts,underlying,source,payload) VALUES(?,?,?,?)",
                        (now.isoformat(), underlying, source, json.dumps(summary, default=str)))
        self.db.commit()

    def save_regime(self, now, underlying, label, raw_label, confidence, payload):
        self.db.execute("INSERT INTO regime_decisions(ts,underlying,label,raw_label,confidence,payload) VALUES(?,?,?,?,?,?)",
                        (now.isoformat(), underlying, label, raw_label, confidence, json.dumps(payload, default=str)))
        self.db.commit()

    def save_decision(self, d):
        from dataclasses import asdict
        payload = asdict(d)
        self.db.execute("INSERT INTO decisions(ts,cycle_id,underlying,action,reason_code,payload) VALUES(?,?,?,?,?,?)",
                        (d.timestamp.isoformat(), d.cycle_id, d.underlying, d.action, d.reason_code,
                         json.dumps(payload, default=str)))
        self.db.commit()

    def save_candidate(self, cycle_id, underlying, candidate_id, status, reason, payload=None):
        self.db.execute("INSERT INTO candidates(cycle_id,underlying,candidate_id,status,reason,payload) VALUES(?,?,?,?,?,?)",
                        (cycle_id, underlying, candidate_id, status, reason, json.dumps(payload or {}, default=str)))

    def save_risk_checks(self, cycle_id, underlying, candidate_id, checks: list):
        self.db.executemany("INSERT INTO risk_checks(cycle_id,underlying,candidate_id,check_name,passed,detail) VALUES(?,?,?,?,?,?)",
                            [(cycle_id, underlying, candidate_id, c["check"], int(c["passed"]), str(c["detail"])) for c in checks])

    def save_order(self, now, cycle_id, candidate_id, status, reason, payload):
        self.db.execute("INSERT INTO paper_orders(ts,cycle_id,candidate_id,status,reason,payload) VALUES(?,?,?,?,?,?)",
                        (now.isoformat(), cycle_id, candidate_id, status, reason, json.dumps(payload, default=str)))
        self.db.commit()

    def commit(self):
        self.db.commit()

    # ---- positions ----------------------------------------------------------------
    def save_position(self, p: Position, now: datetime):
        for ev in p.events[p.persisted_events:]:
            self.db.execute("INSERT INTO position_events(position_id,ts,old_state,new_state,reason,snapshot) VALUES(?,?,?,?,?,?)",
                            (p.position_id, ev["time"], ev["from"], ev["to"], ev["reason"], json.dumps(ev["snapshot"], default=str)))
        p.persisted_events = len(p.events)
        self.db.execute("INSERT INTO positions(position_id,state,payload,updated_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(position_id) DO UPDATE SET state=excluded.state,payload=excluded.payload,updated_at=excluded.updated_at",
                        (p.position_id, p.state, json.dumps(p.to_dict()), now.isoformat()))
        self.db.commit()

    def load_live_positions(self) -> List[Position]:
        marks = ",".join("?" * len(LIVE_STATES))
        rows = self.db.execute(f"SELECT payload FROM positions WHERE state IN ({marks})", tuple(LIVE_STATES)).fetchall()
        return [Position.from_dict(json.loads(r[0])) for r in rows]

    def all_positions(self) -> List[Position]:
        return [Position.from_dict(json.loads(r[0])) for r in self.db.execute("SELECT payload FROM positions ORDER BY rowid")]

    def rows(self, table: str, limit: int = 200):
        cur = self.db.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
