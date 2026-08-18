#!/usr/bin/env python3
"""H1 crash-reversion refinement — TRAIN ONLY (test stays sealed until final pick)."""
import numpy as np
from bt import (ALTS, DATA, FEE, HS, N, TRAIN, TS, h1_crash, metrics, ret_k, roll_sum, simulate)

def h1_limit(drop, target, vol_mult=3.0, timeout=60):
    """Same signal, but entry via resting limit at the signal candle's close:
    filled only if next candle's low trades strictly below it (maker fee, no spread)."""
    trades = []
    sigs = h1_crash(drop, target, vol_mult, timeout)
    busy = -1
    for idx, s, tgt, to in sigs:
        if idx <= busy or idx + 1 >= N - to - 1:
            continue
        d = DATA[s]
        px = d["c"][idx]
        if d["l"][idx + 1] >= px:            # never traded through our resting bid
            continue
        entry = px * (1 + FEE)
        tp = px * (1 + tgt)
        end = idx + 1 + to
        win = d["h"][idx + 1:end]
        hit = np.argmax(win >= tp) if (win >= tp).any() else -1
        if hit >= 0:
            exit_px = tp * (1 - FEE)
            end = idx + 1 + hit
        else:
            exit_px = d["c"][end] * (1 - HS[s]) * (1 - FEE)
        trades.append((idx, end, exit_px / entry - 1))
        busy = end
    return trades

def mae(drop, target, timeout):
    """Max adverse excursion per trade: how far under water do winners go?"""
    out = []
    for idx, s, tgt, to in h1_crash(drop, target, 3.0, timeout):
        if idx + 1 >= N - to - 1 or not TRAIN[idx]:
            continue
        d = DATA[s]
        entry = d["o"][idx + 1]
        out.append(d["l"][idx + 1:idx + 1 + to].min() / entry - 1)
    return np.array(out)

print(f"{'variant':<34}{'n':>5}{'tot%':>8}{'avg%':>8}{'win%':>6}{'dd%':>7}")
for drop in (0.08, 0.10, 0.12):
    for tgt in (0.02, 0.03, 0.05):
        for to in (60, 240):
            m = metrics(simulate(h1_crash(drop, tgt, 3.0, to), None), TRAIN)
            print(f"taker d={drop} t={tgt} to={to}m       {m['n']:>5}{m['total']:>8.1f}{m['avg']:>8.3f}{m['win']:>6.0f}{m['dd']:>7.1f}")
print()
for drop in (0.05, 0.08, 0.10):
    for tgt in (0.02, 0.03):
        for to in (60, 240):
            m = metrics(h1_limit(drop, tgt, 3.0, to), TRAIN)
            print(f"LIMIT d={drop} t={tgt} to={to}m       {m['n']:>5}{m['total']:>8.1f}{m['avg']:>8.3f}{m['win']:>6.0f}{m['dd']:>7.1f}")

print("\nper-pair attribution, taker d=0.08 t=0.02 to=60m (TRAIN):")
sigs = h1_crash(0.08, 0.02, 3.0, 60)
for s in ALTS:
    tr = [t for t in simulate([g for g in sigs if g[1] == s], None) if TRAIN[t[0]]]
    if tr:
        r = np.array([t[2] for t in tr])
        print(f"  {s:<10} n={len(tr):<4} tot={(np.prod(1+r)-1)*100:>7.1f}%  avg={r.mean()*100:>6.2f}%")

a = mae(0.08, 0.02, 60)
print(f"\nMAE (drop=8% tgt=2% to=60m, train): median {np.median(a)*100:.1f}%  p10 {np.percentile(a,10)*100:.1f}%  worst {a.min()*100:.1f}%")
