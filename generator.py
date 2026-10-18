"""Candidate generator: proposes exact-leg candidates from *actual listed instruments*; never authorises."""
from collections import defaultdict
from decimal import Decimal
from typing import List, Tuple
from .config import Config
from .models import Candidate, Leg, OptionQuote, CALL, PUT
from .validation import option_is_eligible, days_until
from .regime import BULLISH, BEARISH, RANGE

PUT_SPREAD, CALL_SPREAD = "DEFINED_RISK_PUT_SPREAD", "DEFINED_RISK_CALL_SPREAD"
BULL_CALL, BEAR_PUT, CONDOR = "BULL_CALL_DEBIT_SPREAD", "BEAR_PUT_DEBIT_SPREAD", "IRON_CONDOR"


def validate_candidate_structure(c: Candidate) -> str:
    """Return '' if structurally valid, else a reason (spec section 8 checks)."""
    if not c.legs:
        return "NO_LEGS"
    if len({l.instrument.expiry for l in c.legs}) != 1:
        return "EXPIRY_MISMATCH"
    if any(l.quote is None for l in c.legs):
        return "LEG_QUOTE_MISSING"
    if any(l.quantity == 0 for l in c.legs):
        return "ZERO_QUANTITY_LEG"
    if len({l.instrument.instrument_id for l in c.legs}) != len(c.legs):
        return "DUPLICATE_LEG"
    puts = sorted((l for l in c.legs if l.instrument.option_type == PUT), key=lambda l: l.instrument.strike)
    calls = sorted((l for l in c.legs if l.instrument.option_type == CALL), key=lambda l: l.instrument.strike)
    t = c.strategy_type

    def spread_ok(pair, lower_qty, upper_qty):
        return (len(pair) == 2 and pair[0].quantity == lower_qty and pair[1].quantity == upper_qty
                and pair[0].instrument.strike < pair[1].instrument.strike)

    if t == PUT_SPREAD:      # long lower, short upper
        ok = not calls and spread_ok(puts, 1, -1)
    elif t == BEAR_PUT:      # short lower, long upper
        ok = not calls and spread_ok(puts, -1, 1)
    elif t == CALL_SPREAD:   # short lower, long upper
        ok = not puts and spread_ok(calls, -1, 1)
    elif t == BULL_CALL:     # long lower, short upper
        ok = not puts and spread_ok(calls, 1, -1)
    elif t == CONDOR:
        ok = (len(puts) == 2 and len(calls) == 2 and puts[0].quantity == 1 and puts[1].quantity == -1
              and calls[0].quantity == -1 and calls[1].quantity == 1
              and puts[1].instrument.strike < calls[0].instrument.strike)
        if len(puts) == 2 and len(calls) == 2 and puts[1].instrument.strike >= calls[0].instrument.strike:
            return "INVALID_CONDOR_STRIKES"
    else:
        return "UNKNOWN_STRATEGY_TYPE"
    return "" if ok else "LEG_DIRECTION_OR_STRIKE_ORDER_INVALID"


def _leg(oq: OptionQuote, qty: int) -> Leg:
    return Leg(oq.instrument, Decimal(qty), oq.quote)


def _band(strikes_lo, strikes_hi, spot):
    return Decimal(str(strikes_lo)) * spot, Decimal(str(strikes_hi)) * spot


def generate_candidates(regime: str, underlying: str, spot: Decimal, chain: List[OptionQuote],
                        now, cfg: Config) -> Tuple[List[Candidate], list]:
    """Returns (valid_candidates, rejected[{candidate_id, reason}])."""
    if regime not in (BULLISH, BEARISH, RANGE):
        return [], []           # HIGH_VOL / UNCERTAIN: no candidates by default
    by_expiry = defaultdict(list)
    for oq in chain:
        i = oq.instrument
        if i.expiry is not None and cfg.minimum_days_to_expiry <= days_until(i.expiry, now) <= cfg.maximum_days_to_expiry:
            by_expiry[i.expiry].append(oq)

    out, rejected, seen = [], [], set()

    def add(stype, specs):
        c = Candidate(stype, underlying, tuple(_leg(oq, q) for oq, q in specs))
        cid = c.candidate_id
        if cid in seen:
            return
        seen.add(cid)
        if stype not in cfg.allowed_strategies:
            rejected.append({"candidate_id": cid, "reason": "STRATEGY_NOT_ALLOWED"})
            return
        bad = validate_candidate_structure(c) if all(q.quote for q, _ in specs) else "LEG_QUOTE_MISSING"
        if bad:
            rejected.append({"candidate_id": cid, "reason": bad})
            return
        for oq, _ in specs:
            ok, why = option_is_eligible(oq, now, cfg)
            if not ok:
                rejected.append({"candidate_id": cid, "reason": f"LEG_INELIGIBLE:{oq.instrument.instrument_id}:{why}"})
                return
        if len(out) >= cfg.max_candidates_per_underlying:
            rejected.append({"candidate_id": cid, "reason": "CANDIDATE_CAP_REACHED"})
            return
        out.append(c)
        return c

    for expiry in sorted(by_expiry):
        opts = by_expiry[expiry]
        puts = sorted((o for o in opts if o.instrument.option_type == PUT), key=lambda o: o.instrument.strike)
        calls = sorted((o for o in opts if o.instrument.option_type == CALL), key=lambda o: o.instrument.strike)
        otm_lo, otm_hi = cfg.short_otm_min, cfg.short_otm_max
        put_lo, put_hi = spot * Decimal(str(1 - otm_hi)), spot * Decimal(str(1 - otm_lo))
        call_lo, call_hi = spot * Decimal(str(1 + otm_lo)), spot * Decimal(str(1 + otm_hi))
        atm_lo, atm_hi = spot * Decimal(str(1 - cfg.atm_band)), spot * Decimal(str(1 + cfg.atm_band))
        steps = range(1, cfg.max_width_steps + 1)

        put_pairs = [(puts[i], puts[i - j], j) for i in range(len(puts)) if put_lo <= puts[i].instrument.strike <= put_hi
                     for j in steps if i - j >= 0]                   # (short, long, steps)
        call_pairs = [(calls[i], calls[i + j], j) for i in range(len(calls)) if call_lo <= calls[i].instrument.strike <= call_hi
                      for j in steps if i + j < len(calls)]

        if regime == BULLISH:
            for s, l, _ in put_pairs:
                add(PUT_SPREAD, [(s, -1), (l, 1)])
            for i in range(len(calls)):
                if atm_lo <= calls[i].instrument.strike <= atm_hi:
                    for j in steps:
                        if i + j < len(calls):
                            add(BULL_CALL, [(calls[i], 1), (calls[i + j], -1)])
        elif regime == BEARISH:
            for s, l, _ in call_pairs:
                add(CALL_SPREAD, [(s, -1), (l, 1)])
            for i in range(len(puts)):
                if atm_lo <= puts[i].instrument.strike <= atm_hi:
                    for j in steps:
                        if i - j >= 0:
                            add(BEAR_PUT, [(puts[i], 1), (puts[i - j], -1)])
        elif regime == RANGE:
            for ps, pl, pj in put_pairs:
                for cs, cl, cj in call_pairs:
                    if pj == cj:
                        add(CONDOR, [(pl, 1), (ps, -1), (cs, -1), (cl, 1)])
    return out, rejected
