#!/usr/bin/env python3
"""Backtest 5 mechanism-based hypotheses on 1m Binance klines.

Honesty rules:
- signal on candle close, entry at NEXT candle open (no lookahead)
- taker fee 0.1%/side + per-pair half-spread on any market order
- target exits are resting limit sells: maker fee, no spread cost, fill only if high >= target
- one open position at a time, full equity per trade, compounding from $50
- tune on TRAIN (2024-01..2025-06), verdict on untouched TEST (2025-07..2026-07)
"""
import json, time
from pathlib import Path

import numpy as np

D = Path(__file__).with_name("data")
FEE = 0.001
# half-spread cost of a market order, measured from live books 2026-08-18 (scan run)
HS = {"BTCUSDT": 0.0001, "ETHUSDT": 0.0001, "SOLUSDT": 0.0002, "XRPUSDT": 0.0003,
      "DOGEUSDT": 0.0003, "LINKUSDT": 0.0004, "PEPEUSDT": 0.002, "ONEUSDT": 0.007,
      "RVNUSDT": 0.0018, "SNXUSDT": 0.0026}
SYMS = sorted(HS)
ALTS = [s for s in SYMS if s not in ("BTCUSDT", "ETHUSDT")]

print("loading...", flush=True)
DATA = {s: dict(np.load(D / f"{s}.npz")) for s in SYMS}
TS = DATA["BTCUSDT"]["ts"]
assert all(len(d["ts"]) == len(TS) and d["ts"][0] == TS[0] for d in DATA.values())
N = len(TS)
TRAIN = (TS >= time.mktime((2024, 1, 1, 0, 0, 0, 0, 0, 0))) & (TS < 1751328000)  # ..2025-07-01 UTC
TEST = TS >= 1751328000

def roll_sum(x, w):
    c = np.concatenate(([0.0], np.cumsum(x)))
    out = np.full_like(x, np.nan)
    out[w - 1:] = c[w:] - c[:-w]
    return out

def roll_max(x, w):
    import pandas as pd
    return pd.Series(x).rolling(w).max().to_numpy()

def ret_k(c, k):
    r = np.full_like(c, np.nan)
    r[k:] = c[k:] / c[:-k] - 1
    return r


def simulate(signals, exits):
    """signals: list of (idx, sym, target_pct or None, timeout_min) sorted by idx.
    Returns per-trade net returns + exit indices. One position at a time."""
    trades = []
    busy_until = -1
    for idx, sym, target, timeout in signals:
        if idx <= busy_until or idx + 1 >= N - timeout - 1:
            continue
        d = DATA[sym]
        hs = HS[sym]
        entry = d["o"][idx + 1] * (1 + hs) * (1 + FEE)
        exit_px = None
        end = idx + 1 + timeout
        if target is not None:
            tp = d["o"][idx + 1] * (1 + target)
            win = d["h"][idx + 1:end]
            hit = np.argmax(win >= tp) if (win >= tp).any() else -1
            if hit >= 0:
                exit_px = tp * (1 - FEE)          # resting limit sell, maker
                end = idx + 1 + hit
        if exit_px is None:
            exit_px = d["c"][end] * (1 - hs) * (1 - FEE)
        trades.append((idx, end, exit_px / entry - 1))
        busy_until = end
    return trades


def metrics(trades, mask):
    tr = [t for t in trades if mask[t[0]]]
    if not tr:
        return dict(n=0, total=0.0, avg=0.0, win=0.0, dd=0.0, perday=0.0)
    rets = np.array([t[2] for t in tr])
    eq = np.cumprod(1 + rets)
    peak = np.maximum.accumulate(eq)
    days = (TS[tr[-1][1]] - TS[tr[0][0]]) / 86400
    total = eq[-1] - 1
    return dict(n=len(tr), total=total * 100, avg=rets.mean() * 100,
                win=(rets > 0).mean() * 100, dd=((eq / peak) - 1).min() * 100,
                perday=((eq[-1]) ** (1 / max(days, 1)) - 1) * 100)


def fmt(name, params, m_tr, m_te):
    return (f"{name:<22}{params:<28}"
            f"{m_tr['n']:>6}{m_tr['total']:>9.1f}{m_tr['avg']:>8.3f}{m_tr['win']:>6.0f}{m_tr['perday']:>8.3f}"
            f" |{m_te['n']:>6}{m_te['total']:>9.1f}{m_te['avg']:>8.3f}{m_te['win']:>6.0f}{m_te['perday']:>8.3f}{m_te['dd']:>7.1f}")


HEADER = (f"{'hypothesis':<22}{'params':<28}"
          f"{'n':>6}{'tot%':>9}{'avg%':>8}{'win%':>6}{'%/day':>8} |{'n':>6}{'tot%':>9}{'avg%':>8}{'win%':>6}{'%/day':>8}{'dd%':>7}")


# ---- signal generators (each returns sorted signal list) ----

def h1_crash(drop, target, vol_mult=3.0, timeout=60):
    sigs = []
    for s in ALTS:
        d = DATA[s]
        r15 = ret_k(d["c"], 15)
        v15 = roll_sum(d["qv"], 15)
        vbase = roll_sum(d["qv"], 1440) / 96          # avg 15m volume over prior day
        idxs = np.where((r15 < -drop) & (v15 > vol_mult * vbase))[0]
        sigs += [(int(i), s, target, timeout) for i in idxs]
    return sorted(sigs)


def h2_burst(mult, minret, timeout):
    sigs = []
    for s in ALTS:
        d = DATA[s]
        r5 = ret_k(d["c"], 5)
        v5 = roll_sum(d["qv"], 5)
        vbase = roll_sum(d["qv"], 1440) / 288
        idxs = np.where((r5 > minret) & (v5 > mult * vbase))[0]
        sigs += [(int(i), s, None, timeout) for i in idxs]
    return sorted(sigs)


def h3_lag(lead_ret, timeout, follow_syms=("ONEUSDT", "RVNUSDT", "SNXUSDT")):
    btc = ret_k(DATA["BTCUSDT"]["c"], 5)
    eth = ret_k(DATA["ETHUSDT"]["c"], 5)
    lead = np.where((btc > lead_ret) | (eth > lead_ret))[0]
    sigs = []
    fr = {s: ret_k(DATA[s]["c"], 5) for s in follow_syms}
    for i in lead:
        lagger = min(follow_syms, key=lambda s: fr[s][i] if not np.isnan(fr[s][i]) else 9)
        if fr[lagger][i] < lead_ret / 2:              # hasn't moved yet
            sigs.append((int(i), lagger, None, timeout))
    return sorted(sigs)


def h4_hour(hours, sym="BTCUSDT"):
    hod = ((TS // 3600) % 24).astype(int)
    starts = np.where(np.isin(hod, hours) & (TS % 3600 < 60))[0]
    return sorted((int(i), sym, None, 60) for i in starts)


def h4_scan_hours():
    """Train-only: per-hour mean 1h forward return (gross), to pick candidate hours."""
    out = {}
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        c = DATA[s]["c"]
        r60 = np.full(N, np.nan)
        r60[:-60] = c[60:] / c[:-60] - 1
        hod = ((TS // 3600) % 24).astype(int)
        at_hour = TS % 3600 < 60
        out[s] = {h: float(np.nanmean(r60[TRAIN & at_hour & (hod == h)]) * 100) for h in range(24)}
    return out


def h5_breakout(margin, timeout):
    sigs = []
    for s in SYMS:
        d = DATA[s]
        hi24 = roll_max(d["h"], 1440)
        prev = np.roll(hi24, 1)
        idxs = np.where(d["c"] > prev * (1 + margin))[0]
        # only the first breakout candle (close crossed above), not every candle above
        cross = idxs[np.where(np.diff(idxs, prepend=-10) > 5)[0]]
        sigs += [(int(i), s, None, timeout) for i in cross]
    return sorted(sigs)


if __name__ == "__main__":
    print(HEADER)
    grids = []
    for drop in (0.03, 0.05, 0.08):
        for target in (0.01, 0.02):
            grids.append((f"H1 crash-revert", f"drop={drop} tgt={target}", h1_crash(drop, target)))
    for mult in (5, 10, 20):
        for timeout in (15, 60):
            grids.append((f"H2 vol-burst", f"mult={mult} hold={timeout}m", h2_burst(mult, 0.01, timeout)))
    for lead in (0.005, 0.01, 0.02):
        for timeout in (15, 60):
            grids.append((f"H3 btc-lag", f"lead={lead} hold={timeout}m", h3_lag(lead, timeout)))
    for margin in (0.0, 0.002):
        for timeout in (30, 120):
            grids.append((f"H5 24h-breakout", f"m={margin} hold={timeout}m", h5_breakout(margin, timeout)))
    for name, params, sigs in grids:
        tr = simulate(sigs, None)
        print(fmt(name, params, metrics(tr, TRAIN), metrics(tr, TEST)), flush=True)
    print("\nH4 hour-of-day mean forward 1h return %, TRAIN only (pick hours, then test):")
    print(json.dumps(h4_scan_hours(), indent=0))
