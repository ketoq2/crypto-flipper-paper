#!/usr/bin/env python3
"""Crypto paper-trading brain — port of ~/osrs-fork/flipper/brain.py skeleton.

Modes:
  --test              offline self-checks
  --scan              rank Binance USDT pairs by net spread after fees (the "is there an edge" question)
  --paper SYMBOL      paper market-making loop on one symbol (simulated fills, SQLite ledger)
  --cash 50           paper bankroll in USDT

NO real orders. NO API keys. Public endpoints only.
Fills are queue-modeled: orders wait behind the resting size at their price (bookTicker
qty at placement); cancellations ahead are unobservable and assumed to never happen.
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

LOCAL_STATE = Path(__file__).with_name("state-local.json")   # NOT git-tracked (hosted owns state.json)
SCAN_EVERY_S = 300


def enrich(c, notional):
    """Add per-flip economics to a scan candidate: expected $ and rough fill estimate."""
    c["exp"] = notional * c["net_pct"] / 100
    # ponytail: GE-style rough estimate — our notional vs pair's per-minute volume, 4x competition factor
    c["fill_min"] = notional / max(c["vol24h"] / 1440, 1e-9) * 4
    c["why"] = (f"{c['net_pct']:.3f}% net after {ROUND_TRIP_FEE*100:.2f}% fees "
                f"({c['gross_pct']:.3f}% gross), ${c['vol24h']:,.0f}/24h vol, "
                f"~{c['fill_min']:.1f}m/leg est")
    return c


def choose_symbol(st, cands):
    """When idle, retarget to the best candidate (like GE brain picking its next item)."""
    if st["state"] == "idle" and cands and cands[0]["net_pct"] > 0 and cands[0]["symbol"] != st["symbol"]:
        st["symbol"], st["last_id"] = cands[0]["symbol"], None
        return f"picked {cands[0]['symbol']}: {cands[0]['why']}"
    return None


def fmt_t(t):
    return time.strftime("%m-%d %H:%M:%S", time.localtime(t))


def html_status(st, shared):
    now = time.time()
    days = max((now - st["start_t"]) / 86400, 1e-9)
    pct = (st["cash"] / st["start_cash"] - 1) * 100
    open_exp = ""
    if st["state"] != "idle":
        notional = st["qty"] * st["order_price"]
        cand = next((c for c in shared["cands"] if c["symbol"] == st["symbol"]), None)
        exp = f"{cand['exp']:+.4f}" if cand else "?"
        fill = f"~{cand['fill_min']:.1f}m" if cand else "?"
        open_exp = (f"<tr><td>{st['symbol']}</td><td>{'buy' if st['state']=='buying' else 'sell'}</td>"
                    f"<td>{st['order_price']:.6g}</td><td>{st['qty']:.4f}</td><td>${notional:.2f}</td>"
                    f"<td>{exp}</td><td>{(now-st['opened'])/60:.1f}m</td><td>{fill}</td></tr>")
    deployed = (st["qty"] * st["order_price"] if st["state"] == "buying"
                else st["buy_cost"] if st["state"] == "selling" else 0)
    cand = next((c for c in shared["cands"] if c["symbol"] == st["symbol"]), None)
    kpi = [("Bankroll", f"${st['cash']:.4f}"), ("Deployed", f"${deployed:.2f}"),
           ("Free cash", f"${st['cash'] - deployed:.2f}"),
           ("Realized P/L", f"${sum(f['pnl'] for f in st['flips']):+.4f}"),
           ("Open exp P/L", f"${cand['exp']:+.4f}" if cand and st["state"] != "idle" else "—"),
           ("Total", f"{pct:+.3f}%"), ("Per day", f"{pct/days:+.3f}%"),
           ("Flips done", str(len(st["flips"]))), ("State", st["state"]), ("Uptime", f"{days:.2f}d"),
           ("Scan", f"{len(shared['cands'])} pairs" if shared["cands"] else "warming up")]
    cards = "".join(f"<div class=c><div class=k>{k}</div><div class=v>{v}</div></div>" for k, v in kpi)
    nxt = "".join(f"<tr><td>{'★ ' if i == 0 else ''}{c['symbol']}{' (current)' if c['symbol'] == st['symbol'] else ''}</td>"
                  f"<td>{c['gross_pct']:.3f}</td><td>{c['net_pct']:.3f}</td><td>{c['vol24h']:,.0f}</td>"
                  f"<td>{c['exp']:+.4f}</td><td>{c['fill_min']:.1f}</td>"
                  f"<td class=l>{c['why']}</td></tr>" for i, c in enumerate(shared["cands"][:12]))
    flips = "".join(f"<tr><td>{fmt_t(f['t'])}</td><td>{f.get('sym', st['symbol'])}</td>"
                    f"<td>{f['qty']:.4f}</td><td>{f['buy']:.6g}</td><td>{f['sell']:.6g}</td>"
                    f"<td>{f['fees']:.4f}</td><td>{f['pnl']:+.4f}</td></tr>" for f in reversed(st["flips"][-30:]))
    decisions = "".join(f"<tr><td>{fmt_t(d['t'])}</td><td class=l>{d['line']}</td></tr>"
                        for d in reversed(st.get("log", [])[-40:]))
    return f"""<!doctype html><meta http-equiv=refresh content=8><title>Paper trader</title>
<style>body{{font-family:-apple-system,sans-serif;background:#14171c;color:#dde;margin:24px}}
.cards{{display:flex;gap:12px;flex-wrap:wrap}}.c{{background:#1e232b;border-radius:8px;padding:12px 18px}}
.k{{color:#8899aa;font-size:12px}}.v{{font-size:20px;font-weight:600}}
h3{{margin:22px 0 6px}}table{{border-collapse:collapse}}td,th{{padding:4px 12px;border-bottom:1px solid #2a303a;text-align:right;font-size:13px}}
td.l,th.l{{text-align:left}}.note{{color:#8899aa;margin-top:12px;font-size:13px}}</style>
<h2>Paper trader — local fast loop</h2>
<p class=note>Rules: pick the pair with the widest spread after fees (rescans every {SCAN_EVERY_S//60}m) ·
quote buy at best bid only when spread &gt; {ROUND_TRIP_FEE*100:.2f}% round-trip fees · on fill, sell at best ask,
chase down if undercut · requote if outbid · buy timeout {BUY_TIMEOUT_S//60}m · fee {FEE*100:.1f}%/side ·
bankroll fully deployed on one flip at a time.</p>
<div class=cards>{cards}</div>
<h3>Open flip</h3>
<table><tr><th class=l>pair</th><th>side</th><th>price</th><th>qty</th><th>notional</th><th>exp P/L $</th><th>elapsed</th><th>est fill</th></tr>
{open_exp or '<tr><td colspan=8>none — waiting for a spread that beats fees</td></tr>'}</table>
<h3>Next buys (what it will buy and why — ★ = next up)</h3>
<table><tr><th class=l>pair</th><th>gross %</th><th>net %</th><th>vol 24h $</th><th>exp $/flip</th><th>est m/leg</th><th class=l>why</th></tr>{nxt}</table>
<h3>Flips done</h3>
<table><tr><th>closed</th><th>pair</th><th>qty</th><th>buy</th><th>sell</th><th>fees $</th><th>pnl $</th></tr>{flips}</table>
<h3>Decisions</h3><table><tr><th>when</th><th class=l>decision</th></tr>{decisions}</table>
<p class=note>Fills are queue-modeled: orders wait behind the resting size at their price; cancels ahead assumed never (conservative). Icebergs/hidden size not visible.
Est fill = notional / per-minute volume × 4 (rough). Hosted 15-min twin (fixed ONEUSDT):
<a style="color:#7ab" href="https://github.com/ketoq2/crypto-flipper-paper/blob/master/STATUS.md">STATUS.md</a></p>"""


def cmd_paper(symbol, cash):
    if LOCAL_STATE.exists():
        st = json.loads(LOCAL_STATE.read_text())
        print(f"resumed {st['symbol']}: cash ${st['cash']:.4f}, {len(st['flips'])} flips, state {st['state']}")
        if st["state"] != "idle" and "queue" not in st:
            st["state"] = "idle"    # state written by pre-queue-model code; drop the open flip
    else:
        st = {"symbol": symbol, "cash": cash, "start_cash": cash, "start_t": time.time(),
              "state": "idle", "order_price": 0, "qty": 0, "buy_cost": 0, "opened": 0,
              "last_id": None, "flips": []}
    st.setdefault("log", [])
    shared = {"line": "starting", "cands": []}

    def note(line):
        print(f"[{time.strftime('%H:%M:%S')}] {line}")
        st["log"] = (st["log"] + [{"t": time.time(), "line": line}])[-60:]

    def scanner():
        while True:
            try:
                cands = scan_candidates(get("ticker/bookTicker"), get("ticker/24hr"))
                shared["cands"] = [enrich(c, st["cash"]) for c in cands[:15]]
            except Exception as e:
                print(f"[warn] scan: {e}")
            time.sleep(SCAN_EVERY_S)

    threading.Thread(target=scanner, daemon=True).start()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = html_status(st, shared).encode()
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
            picked = choose_symbol(st, shared["cands"])
            if picked:
                note(picked)
            book = get("ticker/bookTicker", {"symbol": st["symbol"]})
            trades, st["last_id"] = trades_since(st["symbol"], st["last_id"])
            line = step(st, float(book["bidPrice"]), float(book["askPrice"]),
                        float(book["bidQty"]), float(book["askQty"]), trades, time.time())
            if line != shared["line"]:
                note(line)
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


SELL_DUMP_S = 2 * 3600   # unsold after 2h -> dump remainder at the bid (cross the spread to exit)
EPS = 1e-9


def sim_fills(side, op, trades, queue, filled, target):
    """Queue-modeled fills: trades at our price burn the queue ahead first, only the
    overflow fills us; a trade through our price fills everything. Cancellations ahead
    are unobservable -> assumed none (conservative). Returns (queue, filled)."""
    for t in trades:
        if filled >= target:
            break
        p, q = float(t["p"]), float(t["q"])
        through = p < op * (1 - EPS) if side == "buy" else p > op * (1 + EPS)
        at_level = abs(p - op) <= op * EPS
        if through:
            return 0.0, target
        if at_level:
            take = min(q, queue)
            queue -= take
            filled = min(target, filled + (q - take))
    return queue, filled


def book_flip(st, now):
    fees = (st["buy_cost"] + st["proceeds"]) * FEE
    pnl = st["proceeds"] - st["buy_cost"] - fees
    st["cash"] += pnl
    st["flips"].append({"t": now, "sym": st["symbol"], "qty": st["qty"],
                        "buy": st["buy_cost"] / st["qty"], "sell": st["proceeds"] / st["qty"],
                        "fees": fees, "pnl": pnl})
    st["state"] = "idle"
    return pnl


def enter_sell(st, ask, ask_qty, now):
    st.update(state="selling", qty=st["filled"], buy_cost=st["filled"] * st["order_price"],
              order_price=ask, queue=ask_qty, filled=0.0, proceeds=0.0, sell_t=now)


def step(st, bid, ask, bid_qty, ask_qty, trades, now):
    """One decision tick on state dict st. Returns a one-line status string."""
    mid = (bid + ask) / 2
    if st["state"] == "idle":
        if (ask - bid) / mid > ROUND_TRIP_FEE:
            st.update(state="buying", order_price=bid, qty=st["cash"] / bid,
                      queue=bid_qty, filled=0.0, opened=now)
            return (f"BUY {st['qty']:.6f} @ {bid} (spread {(ask-bid)/mid*100:.3f}%, "
                    f"${bid_qty*bid:,.0f} queued ahead)")
        return f"idle, spread {(ask-bid)/mid*100:.3f}% < fees"

    if st["state"] == "buying":
        st["queue"], st["filled"] = sim_fills("buy", st["order_price"], trades,
                                              st["queue"], st["filled"], st["qty"])
        if st["filled"] >= st["qty"] * (1 - EPS):
            enter_sell(st, ask, ask_qty, now)
            return f"buy filled, SELL {st['qty']:.6f} @ {ask} (${ask_qty*ask:,.0f} queued ahead)"
        if bid > st["order_price"] or now - st["opened"] > BUY_TIMEOUT_S:
            if st["filled"] > 0:
                partial = st["filled"]
                enter_sell(st, ask, ask_qty, now)
                return f"buy outbid/stale at {partial/st['qty']*100:.0f}% filled, selling the partial"
            st["state"] = "idle"
            return "buy stale/outbid with no fill, requoting next step"
        return (f"buying @ {st['order_price']} ({st['filled']/st['qty']*100:.0f}% filled, "
                f"${st['queue']*st['order_price']:,.0f} still ahead)")

    if st["state"] == "selling":
        before = st["filled"]
        st["queue"], st["filled"] = sim_fills("sell", st["order_price"], trades,
                                              st["queue"], st["filled"], st["qty"])
        st["proceeds"] += (st["filled"] - before) * st["order_price"]
        if st["filled"] >= st["qty"] * (1 - EPS):
            pnl = book_flip(st, now)
            return f"SOLD, pnl ${pnl:+.4f}, cash ${st['cash']:.2f}"
        if now - st["sell_t"] > SELL_DUMP_S:
            st["proceeds"] += (st["qty"] - st["filled"]) * bid
            pnl = book_flip(st, now)
            return f"sell timed out, DUMPED remainder at bid, pnl ${pnl:+.4f}, cash ${st['cash']:.2f}"
        if ask < st["order_price"]:
            st.update(order_price=ask, queue=ask_qty)
            return f"undercut, sell repriced to {ask} (${ask_qty*ask:,.0f} queued ahead)"
        return (f"selling @ {st['order_price']} ({st['filled']/st['qty']*100:.0f}% filled, "
                f"${st['queue']*st['order_price']:,.0f} still ahead)")
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
        f"Fills are queue-modeled (wait behind resting size; cancels ahead assumed never).\n\n"
        f"| closed (UTC) | qty | buy | sell | pnl $ |\n|---|---|---|---|---|\n{rows}\n")


def cmd_step(symbol, cash):
    if STATE.exists():
        st = json.loads(STATE.read_text())
        if st["state"] != "idle" and "queue" not in st:
            st["state"] = "idle"    # state written by pre-queue-model code; drop the open flip
    else:
        st = {"symbol": symbol, "cash": cash, "start_cash": cash, "start_t": time.time(),
              "state": "idle", "order_price": 0, "qty": 0, "buy_cost": 0, "opened": 0,
              "last_id": None, "flips": []}
    book = get("ticker/bookTicker", {"symbol": st["symbol"]})
    trades, st["last_id"] = trades_since(st["symbol"], st["last_id"])
    line = step(st, float(book["bidPrice"]), float(book["askPrice"]),
                float(book["bidQty"]), float(book["askQty"]), trades, time.time())
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
    # queue model: trades at our price burn the queue first, overflow fills us, through-trade fills all
    q, f = sim_fills("buy", 100.0, [{"p": "100.0", "q": "4"}], 10.0, 0.0, 0.5)
    assert (q, f) == (6.0, 0.0), (q, f)                    # queue absorbs it, we get nothing
    q, f = sim_fills("buy", 100.0, [{"p": "100.0", "q": "6.2"}], 6.0, 0.0, 0.5)
    assert q == 0.0 and abs(f - 0.2) < 1e-12, (q, f)       # 0.2 overflow -> partial fill
    q, f = sim_fills("buy", 100.0, [{"p": "99.0", "q": "0.01"}], 999.0, 0.0, 0.5)
    assert (q, f) == (0.0, 0.5)                            # traded through -> full fill
    q, f = sim_fills("sell", 100.0, [{"p": "100.5", "q": "1"}], 0.0, 0.0, 0.5)
    assert f == 0.5                                        # sell side symmetric
    # full flip: idle -> buying (queue) -> selling (queue) -> booked with fees
    def fresh():
        return {"symbol": "AAAUSDT", "cash": 50.0, "start_cash": 50.0, "start_t": 0,
                "state": "idle", "order_price": 0, "qty": 0, "buy_cost": 0, "opened": 0,
                "last_id": 0, "flips": []}
    st = fresh()
    step(st, 100.0, 100.5, 10.0, 5.0, [], 0)
    assert st["state"] == "buying" and st["queue"] == 10.0
    step(st, 100.0, 100.5, 10.0, 5.0, [{"p": "100.0", "q": "20"}], 1)
    assert st["state"] == "selling" and st["queue"] == 5.0 and st["buy_cost"] == 50.0
    step(st, 100.0, 100.5, 10.0, 5.0, [{"p": "100.5", "q": "3"}], 2)
    assert st["state"] == "selling" and st["filled"] == 0.0  # still behind the ask queue
    step(st, 100.0, 100.5, 10.0, 5.0, [{"p": "100.5", "q": "2.5"}], 3)
    assert st["state"] == "idle" and len(st["flips"]) == 1 and st["cash"] > 50.0, st
    exp = 0.5 * 100.5 - 50.0 - (50.0 + 0.5 * 100.5) * FEE
    assert abs(st["flips"][0]["pnl"] - exp) < 1e-9
    # partial buy that goes stale sells what it got; zero-fill stale requotes
    st = fresh()
    step(st, 100.0, 100.5, 1.0, 5.0, [], 0)
    step(st, 100.0, 100.5, 1.0, 5.0, [{"p": "100.0", "q": "1.2"}], 1)      # 0.2 partial
    step(st, 100.0, 100.5, 1.0, 5.0, [], BUY_TIMEOUT_S + 2)
    assert st["state"] == "selling" and abs(st["qty"] - 0.2) < 1e-12, st
    # unsold past SELL_DUMP_S dumps at bid and books the flip
    step(st, 99.0, 100.5, 1.0, 5.0, [], BUY_TIMEOUT_S + 3 + SELL_DUMP_S)
    assert st["state"] == "idle" and len(st["flips"]) == 1 and st["flips"][0]["pnl"] < 0
    st = fresh()
    step(st, 100.0, 100.5, 1.0, 5.0, [], 0)
    step(st, 100.0, 100.5, 1.0, 5.0, [], BUY_TIMEOUT_S + 1)
    assert st["state"] == "idle"
    # symbol picking: idle retargets to best candidate, busy states never switch
    cands = [enrich({"symbol": "BBBUSDT", "net_pct": 0.5, "gross_pct": 0.7, "vol24h": 1e6}, 50)]
    assert choose_symbol(st, cands).startswith("picked BBBUSDT") and st["symbol"] == "BBBUSDT"
    st["state"] = "buying"
    assert choose_symbol(st, [dict(cands[0], symbol="CCCUSDT")]) is None
    assert cands[0]["exp"] == 50 * 0.5 / 100 and cands[0]["fill_min"] > 0
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
