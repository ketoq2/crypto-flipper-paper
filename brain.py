#!/usr/bin/env python3
"""Crypto paper-trading brain — port of ~/osrs-fork/flipper/brain.py skeleton.

Modes:
  --test              offline self-checks
  --scan              rank Binance USDT pairs by net spread after fees (the "is there an edge" question)
  --paper SYMBOL      paper market-making loop on one symbol (simulated fills, SQLite ledger)
  --cash 50           paper bankroll in USDT

NO real orders. NO API keys. Public endpoints only.
Paper fills are OPTIMISTIC (assume front of queue): real results will be worse.
"""
import argparse, http.server, json, ssl, sys, threading, time, urllib.request
from pathlib import Path

import certifi

# data-api.binance.vision = official public market-data mirror; api.binance.com 451s from US
# cloud IPs (GitHub Actions runners). Market data only — all we use.
BASES = ["https://data-api.binance.vision/api/v3", "https://api.binance.com/api/v3"]
API = BASES[-1]
FEE = 0.001          # 0.1% spot fee per side (default tier, no BNB discount)
ROUND_TRIP_FEE = 2 * FEE
MIN_QUOTE_VOL_24H = 200_000   # $/day; below this fills take too long to matter
DASH_PORT = 8438              # GE brain uses 8437
CTX = ssl.create_default_context(cafile=certifi.where())  # same Mac SSL fix as GE brain


def get(path, params=None):
    qs = "?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
    err = None
    for base in BASES:
        try:
            req = urllib.request.Request(base + "/" + path + qs,
                                         headers={"User-Agent": "paper-research/0.1"})
            with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            err = e
            if e.code not in (451, 403):
                raise
    raise err


# ---------- scan ----------

def bad_symbol(s):
    return (not s.endswith("USDT")) or any(t in s for t in ("UP", "DOWN", "BULL", "BEAR"))


def scan_candidates(book, day):
    """book: bookTicker rows, day: 24hr ticker rows -> ranked candidate list."""
    vol = {d["symbol"]: float(d["quoteVolume"]) for d in day}
    out = []
    for b in book:
        s = b["symbol"]
        if bad_symbol(s) or vol.get(s, 0) < MIN_QUOTE_VOL_24H:
            continue
        bid, ask = float(b["bidPrice"]), float(b["askPrice"])
        if bid <= 0 or ask <= bid:
            continue
        mid = (bid + ask) / 2
        gross = (ask - bid) / mid
        net = gross - ROUND_TRIP_FEE
        out.append({"symbol": s, "bid": bid, "ask": ask,
                    "gross_pct": gross * 100, "net_pct": net * 100,
                    "vol24h": vol[s]})
    out.sort(key=lambda c: c["net_pct"], reverse=True)
    return out


def cmd_scan():
    book = get("ticker/bookTicker")
    day = get("ticker/24hr")
    cands = scan_candidates(book, day)
    pos = [c for c in cands if c["net_pct"] > 0]
    print(f"{len(cands)} liquid USDT pairs (vol24h >= ${MIN_QUOTE_VOL_24H:,}); "
          f"{len(pos)} with spread > {ROUND_TRIP_FEE*100:.2f}% round-trip fees\n")
    print(f"{'symbol':<14}{'gross%':>8}{'net%':>8}{'vol24h $':>16}")
    for c in cands[:20]:
        print(f"{c['symbol']:<14}{c['gross_pct']:>8.3f}{c['net_pct']:>8.3f}{c['vol24h']:>16,.0f}")
    return cands


# ---------- paper loop ----------

def crossed(order_side, order_price, trades):
    """Optimistic fill: any trade at-or-through our price."""
    for t in trades:
        p = float(t["p"])
        if order_side == "buy" and p <= order_price:
            return True
        if order_side == "sell" and p >= order_price:
            return True
    return False


LOCAL_STATE = Path(__file__).with_name("state-local.json")   # NOT git-tracked (hosted owns state.json)


def html_status(st, line):
    now = time.time()
    days = max((now - st["start_t"]) / 86400, 1e-9)
    pct = (st["cash"] / st["start_cash"] - 1) * 100
    kpi = [("Cash", f"${st['cash']:.4f}"), ("Total", f"{pct:+.3f}%"),
           ("Per day", f"{pct/days:+.3f}%"), ("Flips", str(len(st["flips"]))),
           ("State", st["state"]), ("Uptime", f"{days:.2f}d")]
    cards = "".join(f"<div class=c><div class=k>{k}</div><div class=v>{v}</div></div>" for k, v in kpi)
    order = (f"{st['state']} {st['qty']:.4f} @ {st['order_price']:.6g}"
             if st["state"] != "idle" else "no open order")
    rows = "".join(f"<tr><td>{time.strftime('%m-%d %H:%M', time.localtime(f['t']))}</td>"
                   f"<td>{f['qty']:.4f}</td><td>{f['buy']:.6g}</td><td>{f['sell']:.6g}</td>"
                   f"<td>{f['pnl']:+.4f}</td></tr>" for f in reversed(st["flips"][-30:]))
    return f"""<!doctype html><meta http-equiv=refresh content=8><title>Paper — {st['symbol']}</title>
<style>body{{font-family:-apple-system,sans-serif;background:#14171c;color:#dde;margin:24px}}
.cards{{display:flex;gap:12px;flex-wrap:wrap}}.c{{background:#1e232b;border-radius:8px;padding:12px 18px}}
.k{{color:#8899aa;font-size:12px}}.v{{font-size:20px;font-weight:600}}
table{{border-collapse:collapse;margin-top:16px}}td,th{{padding:4px 12px;border-bottom:1px solid #2a303a;text-align:right}}
.note{{color:#8899aa;margin-top:12px;font-size:13px}}</style>
<h2>Paper trader — {st['symbol']} (local fast loop)</h2><div class=cards>{cards}</div>
<p>Open order: {order}<br>Last: {line}</p>
<table><tr><th>closed</th><th>qty</th><th>buy</th><th>sell</th><th>pnl $</th></tr>{rows}</table>
<p class=note>Fills are OPTIMISTIC (front-of-queue assumed) — all P&L is a ceiling. Hosted 15-min twin:
<a style="color:#7ab" href="https://github.com/ketoq2/crypto-flipper-paper/blob/master/STATUS.md">STATUS.md</a></p>"""


def cmd_paper(symbol, cash):
    if LOCAL_STATE.exists():
        st = json.loads(LOCAL_STATE.read_text())
        print(f"resumed {st['symbol']}: cash ${st['cash']:.4f}, {len(st['flips'])} flips, state {st['state']}")
    else:
        st = {"symbol": symbol, "cash": cash, "start_cash": cash, "start_t": time.time(),
              "state": "idle", "order_price": 0, "qty": 0, "buy_cost": 0, "opened": 0,
              "last_id": None, "flips": []}
    shared = {"line": "starting"}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = html_status(st, shared["line"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a): pass

    threading.Thread(target=http.server.ThreadingHTTPServer(("127.0.0.1", DASH_PORT), H).serve_forever,
                     daemon=True).start()
    print(f"paper-trading {st['symbol']}, fee {FEE*100:.1f}%/side, dashboard http://127.0.0.1:{DASH_PORT}")
    while True:
        try:
            book = get("ticker/bookTicker", {"symbol": st["symbol"]})
            trades, st["last_id"] = trades_since(st["symbol"], st["last_id"])
            line = step(st, float(book["bidPrice"]), float(book["askPrice"]), trades, time.time())
            if line != shared["line"]:
                print(f"[{time.strftime('%H:%M:%S')}] {line}")
            shared["line"] = line
            LOCAL_STATE.write_text(json.dumps(st))
            time.sleep(3)
        except KeyboardInterrupt:
            print(f"\nstopped. cash ${st['cash']:.2f}, state saved to {LOCAL_STATE}")
            return
        except Exception as e:
            print(f"[warn] {e}; retrying")
            time.sleep(10)


# ---------- step mode (stateless, for scheduled hosting e.g. GitHub Actions) ----------

STATE = Path(__file__).with_name("state.json")
BUY_TIMEOUT_S = 45 * 60          # cadence is ~15 min, so timeouts are in steps not seconds


def trades_since(symbol, last_id):
    """All aggTrades after last_id (paginated). None last_id -> just establish position."""
    if last_id is None:
        t = get("aggTrades", {"symbol": symbol, "limit": 1000})
        return [], (t[-1]["a"] if t else None)
    out, frm = [], last_id + 1
    for _ in range(5):               # cap: 5000 trades/step is plenty for these pairs
        t = get("aggTrades", {"symbol": symbol, "fromId": frm, "limit": 1000})
        if not t:
            break
        out += t
        frm = t[-1]["a"] + 1
        if len(t) < 1000:
            break
    return out, (out[-1]["a"] if out else last_id)


def step(st, bid, ask, trades, now):
    """One decision tick on state dict st. Returns a one-line status string."""
    mid = (bid + ask) / 2
    if st["state"] == "idle":
        if (ask - bid) / mid > ROUND_TRIP_FEE:
            st.update(state="buying", order_price=bid, qty=st["cash"] / bid, opened=now)
            return f"BUY {st['qty']:.6f} @ {bid} (spread {(ask-bid)/mid*100:.3f}%)"
        return f"idle, spread {(ask-bid)/mid*100:.3f}% < fees"
    if st["state"] == "buying":
        if crossed("buy", st["order_price"], trades):
            st.update(state="selling", buy_cost=st["qty"] * st["order_price"], order_price=ask)
            return f"buy filled, SELL @ {ask}"
        if bid > st["order_price"] or now - st["opened"] > BUY_TIMEOUT_S:
            st["state"] = "idle"
            return "buy stale/outbid, requoting next step"
        return f"buying @ {st['order_price']} (bid {bid})"
    if st["state"] == "selling":
        if crossed("sell", st["order_price"], trades):
            proceeds = st["qty"] * st["order_price"]
            fees = (st["buy_cost"] + proceeds) * FEE
            pnl = proceeds - st["buy_cost"] - fees
            st["cash"] += pnl
            st["flips"].append({"t": now, "qty": st["qty"], "buy": st["buy_cost"] / st["qty"],
                                "sell": st["order_price"], "fees": fees, "pnl": pnl})
            st["state"] = "idle"
            return f"SOLD, pnl ${pnl:+.4f}, cash ${st['cash']:.2f}"
        if ask < st["order_price"]:
            st["order_price"] = ask
            return f"undercut, sell repriced to {ask}"
        return f"selling @ {st['order_price']} (ask {ask})"
    return "?"


def write_status(st, line):
    days = max((time.time() - st["start_t"]) / 86400, 1e-9)
    pct = (st["cash"] / st["start_cash"] - 1) * 100
    rows = "\n".join(f"| {time.strftime('%m-%d %H:%M', time.gmtime(f['t']))} | {f['qty']:.4f} "
                     f"| {f['buy']:.6g} | {f['sell']:.6g} | {f['pnl']:+.4f} |"
                     for f in st["flips"][-15:])
    Path(__file__).with_name("STATUS.md").write_text(
        f"# Paper trader — {st['symbol']}\n\n"
        f"**Cash: ${st['cash']:.4f}** ({pct:+.3f}% total, {pct/days:+.3f}%/day over {days:.1f}d) — "
        f"{len(st['flips'])} flips done, state: {st['state']}\n\n"
        f"Last step (UTC {time.strftime('%F %T', time.gmtime())}): {line}\n\n"
        f"Fills are OPTIMISTIC (front-of-queue assumed): treat all P&L as a ceiling.\n\n"
        f"| closed (UTC) | qty | buy | sell | pnl $ |\n|---|---|---|---|---|\n{rows}\n")


def cmd_step(symbol, cash):
    if STATE.exists():
        st = json.loads(STATE.read_text())
    else:
        st = {"symbol": symbol, "cash": cash, "start_cash": cash, "start_t": time.time(),
              "state": "idle", "order_price": 0, "qty": 0, "buy_cost": 0, "opened": 0,
              "last_id": None, "flips": []}
    book = get("ticker/bookTicker", {"symbol": st["symbol"]})
    bid, ask = float(book["bidPrice"]), float(book["askPrice"])
    trades, st["last_id"] = trades_since(st["symbol"], st["last_id"])
    line = step(st, bid, ask, trades, time.time())
    STATE.write_text(json.dumps(st))
    write_status(st, line)
    print(f"{st['symbol']} {line} | cash ${st['cash']:.4f} | {len(trades)} trades replayed")


# ---------- self-check ----------

def cmd_test():
    book = [
        {"symbol": "AAAUSDT", "bidPrice": "100", "askPrice": "100.5"},   # 0.5% gross -> positive net
        {"symbol": "BBBUSDT", "bidPrice": "100", "askPrice": "100.05"},  # 0.05% gross -> negative net
        {"symbol": "CCCUSDT", "bidPrice": "100", "askPrice": "101"},     # illiquid, filtered
        {"symbol": "DDDUPUSDT", "bidPrice": "1", "askPrice": "2"},       # leveraged token, filtered
        {"symbol": "EEEBTC", "bidPrice": "1", "askPrice": "2"},          # not USDT, filtered
    ]
    day = [{"symbol": s, "quoteVolume": v} for s, v in
           [("AAAUSDT", 1e6), ("BBBUSDT", 1e6), ("CCCUSDT", 1000), ("DDDUPUSDT", 1e6), ("EEEBTC", 1e6)]]
    c = scan_candidates(book, day)
    assert [x["symbol"] for x in c] == ["AAAUSDT", "BBBUSDT"], c
    assert c[0]["net_pct"] > 0 > c[1]["net_pct"]
    assert crossed("buy", 100, [{"p": "99.9"}]) and not crossed("buy", 100, [{"p": "100.1"}])
    assert crossed("sell", 100, [{"p": "100.1"}]) and not crossed("sell", 100, [{"p": "99.9"}])
    # a full flip at AAAUSDT prices must net gross-spread minus both fees
    buy, sell, q = 100.0, 100.5, 0.5
    pnl = q * sell - q * buy - (q * buy + q * sell) * FEE
    assert 0 < pnl < q * (sell - buy), pnl
    # step-mode state machine: idle->buying->selling->idle with pnl booked
    st = {"symbol": "AAAUSDT", "cash": 50.0, "start_cash": 50.0, "start_t": 0,
          "state": "idle", "order_price": 0, "qty": 0, "buy_cost": 0, "opened": 0,
          "last_id": 0, "flips": []}
    step(st, 100.0, 100.5, [], 0);            assert st["state"] == "buying"
    step(st, 100.0, 100.5, [{"p": "99.9"}], 1); assert st["state"] == "selling"
    step(st, 100.0, 100.5, [{"p": "100.6"}], 2)
    assert st["state"] == "idle" and len(st["flips"]) == 1 and st["cash"] > 50.0, st
    # stale buy times out back to idle
    step(st, 100.0, 100.5, [], 3);            assert st["state"] == "buying"
    step(st, 100.0, 100.5, [], 3 + BUY_TIMEOUT_S + 1); assert st["state"] == "idle"
    print("self-checks OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--paper", metavar="SYMBOL")
    ap.add_argument("--step", metavar="SYMBOL", help="one hosted iteration (state.json), then exit")
    ap.add_argument("--cash", type=float, default=50.0)
    a = ap.parse_args()
    if a.test:
        cmd_test()
    elif a.scan:
        cmd_scan()
    elif a.step:
        cmd_step(a.step.upper(), a.cash)
    elif a.paper:
        cmd_paper(a.paper.upper(), a.cash)
    else:
        ap.print_help()
