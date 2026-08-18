#!/usr/bin/env python3
"""Parse monthly kline zips -> one .npz per symbol (sorted, deduped 1m grid)."""
import csv, glob, io, zipfile
from pathlib import Path

import numpy as np

D = Path(__file__).with_name("data")
SYMS = sorted({p.name.split("-")[0] for p in D.glob("*.zip")})

for sym in SYMS:
    out = D / f"{sym}.npz"
    if out.exists():
        continue
    rows = []
    for z in sorted(D.glob(f"{sym}-1m-*.zip")):
        with zipfile.ZipFile(z) as zf:
            with zf.open(zf.namelist()[0]) as f:
                for r in csv.reader(io.TextIOWrapper(f)):
                    if r[0].isdigit() or r[0].replace(".", "").isdigit():
                        # open_time, open, high, low, close, volume, close_time, quote_vol, trades, taker_buy_base, ...
                        rows.append((float(r[0]), float(r[1]), float(r[2]), float(r[3]),
                                     float(r[4]), float(r[5]), float(r[7]), float(r[8]), float(r[9])))
    a = np.array(rows)
    a = a[np.argsort(a[:, 0])]
    ts = a[:, 0]
    ts = np.where(ts > 1e14, ts / 1000, ts)  # some archives use microseconds
    np.savez_compressed(out, ts=ts / 1000, o=a[:, 1], h=a[:, 2], l=a[:, 3], c=a[:, 4],
                        v=a[:, 5], qv=a[:, 6], n=a[:, 7], tb=a[:, 8])
    print(sym, len(a), "rows")
