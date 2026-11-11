"""Paper day-trader: simulated £1000 across crypto (Coinbase) and US stocks (Yahoo), 15-minute candles.

All prices are converted to GBP at the live GBP/USD rate, so FX moves count, as for a UK trader.
One run = one tick: check stops/targets, exit broken trends, open new trades, save state.json.
Stdlib only. Run: python3 bot.py
"""
import json
import os
import time
import urllib.request

CRYPTO = ["BTC", "ETH", "SOL", "DOGE", "ADA",                       # majors
          "AVAX", "LINK", "DOT", "NEAR", "UNI", "INJ", "SUI", "RENDER", "FET", "ARB", "OP",
          "SHIB", "PEPE", "BONK", "WIF"]                              # smaller caps / memes
STOCKS = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "AMD",
          "PLTR", "COIN", "MSTR", "SPY", "QQQ"]
FEES = {"crypto": 0.004, "stock": 0.0015}  # per side: retail crypto taker fee; stock app FX fee
GRAN = 900            # 15-minute candles
START_CASH = 1000.0
RISK = 0.02           # risk 2% of equity per trade (distance to stop)
MAX_POS_FRAC = 0.25   # no position larger than 25% of equity
MAX_POSITIONS = 5
STOP_ATR, TP_ATR = 2.0, 3.0
COOLDOWN = 4 * GRAN   # wait an hour before re-entering a pair after exit
MIN_ORDER = 10.0
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (paper-trader-sim)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_crypto(sym, fx):
    rows = sorted(get("https://api.exchange.coinbase.com/products/%s-USD/candles?granularity=%d" % (sym, GRAN)))
    # rows: [time, low, high, open, close, volume]
    return [dict(t=x[0], l=x[1] / fx, h=x[2] / fx, o=x[3] / fx, c=x[4] / fx) for x in rows]


def fetch_stock(sym, fx):
    res = get("https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=15m&range=5d" % sym)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    return [dict(t=t, o=o / fx, h=h / fx, l=l / fx, c=c / fx)
            for t, o, h, l, c in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"])
            if None not in (o, h, l, c)]  # Yahoo leaves gaps as nulls


def gbpusd():
    return get("https://query1.finance.yahoo.com/v8/finance/chart/GBPUSD=X?interval=15m&range=1d")[
        "chart"]["result"][0]["meta"]["regularMarketPrice"]


def kind(sym):
    return "stock" if sym in STOCKS else "crypto"


def ema(vals, n):
    k, out = 2 / (n + 1), [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, n=14):
    gains = losses = 0.0
    for a, b in zip(closes[:n], closes[1:n + 1]):
        gains += max(b - a, 0); losses += max(a - b, 0)
    ag, al = gains / n, losses / n
    for a, b in zip(closes[n:], closes[n + 1:]):
        ag = (ag * (n - 1) + max(b - a, 0)) / n
        al = (al * (n - 1) + max(a - b, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(cs, n=14):
    trs = [max(k["h"] - k["l"], abs(k["h"] - p["c"]), abs(k["l"] - p["c"])) for p, k in zip(cs, cs[1:])]
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def new_state():
    return {"start_cash": START_CASH, "cash": START_CASH, "positions": {}, "trades": [],
            "history": [], "last_exit": {}, "log": [], "started": int(time.time())}


def close_pos(state, pair, px, why, now):
    pos = state["positions"].pop(pair)
    proceeds = pos["qty"] * px * (1 - FEES[kind(pair)])
    state["cash"] += proceeds
    pnl = proceeds - pos["cost"]
    state["trades"].append({"pair": pair, "entry": pos["entry"], "exit": px, "qty": pos["qty"],
                            "opened": pos["opened"], "closed": now, "pnl": round(pnl, 2),
                            "pct": round(100 * pnl / pos["cost"], 2), "why": why})
    state["last_exit"][pair] = now
    return "SELL %s @ %.6g (%s) pnl £%.2f" % (pair, px, why, pnl)


def step(state, data, now):
    """Advance one tick. data = {pair: candles oldest-first}, last candle may still be forming."""
    log = []
    last = {p: cs[-1]["c"] for p, cs in data.items() if cs}

    # 1. stops / targets on every candle since entry (stop first if both hit: conservative)
    for pair, pos in list(state["positions"].items()):
        for k in data.get(pair, []):
            if k["t"] < pos["since"]:
                continue
            if k["l"] <= pos["stop"]:
                log.append(close_pos(state, pair, min(pos["stop"], k["o"]), "stop loss", now)); break
            if k["h"] >= pos["tp"]:
                log.append(close_pos(state, pair, max(pos["tp"], k["o"]), "take profit", now)); break

    equity = state["cash"] + sum(p["qty"] * last.get(s, p["entry"]) for s, p in state["positions"].items())

    for pair, cs in data.items():
        closed = [k for k in cs if k["t"] + GRAN <= now]
        if len(closed) < 60 or now - closed[-1]["t"] > 4 * GRAN:  # too little or stale data
            continue
        closes = [k["c"] for k in closed]
        e20, e50 = ema(closes, 20)[-1], ema(closes, 50)[-1]
        c = closes[-1]

        # 2. trend exit
        if pair in state["positions"]:
            if e20 < e50:
                log.append(close_pos(state, pair, c, "trend reversed", now))
            continue

        # 3. entry: uptrend, price above fast EMA, momentum positive but not overbought
        if len(state["positions"]) >= MAX_POSITIONS or now - state["last_exit"].get(pair, 0) < COOLDOWN:
            continue
        r, a = rsi(closes), atr(closed)
        if not (e20 > e50 and c > e20 and 50 < r < 70 and a > 0):
            continue
        stop, tp = c - STOP_ATR * a, c + TP_ATR * a
        fee = FEES[kind(pair)]
        cost = min(equity * RISK / (c - stop) * c, equity * MAX_POS_FRAC, state["cash"] / (1 + fee))
        if cost < MIN_ORDER:
            continue
        state["cash"] -= cost * (1 + fee)
        state["positions"][pair] = {"qty": cost / c, "entry": c, "stop": stop, "tp": tp, "kind": kind(pair),
                                    "cost": cost * (1 + fee), "opened": now, "since": cs[-1]["t"]}
        log.append("BUY %s £%.2f @ %.6g stop %.6g tp %.6g (rsi %.0f)" % (pair, cost, c, stop, tp, r))

    equity = state["cash"] + sum(p["qty"] * last.get(s, p["entry"]) for s, p in state["positions"].items())
    state["history"] = (state["history"] + [[now, round(equity, 2)]])[-10000:]
    state["log"] = (state["log"] + [[now, m] for m in log])[-500:]
    state["updated"] = now
    state["last_prices"] = last
    return log


def main():
    state = json.load(open(STATE)) if os.path.exists(STATE) else new_state()
    fx = gbpusd()  # no FX rate -> fail the run rather than mis-price everything
    data = {}
    for sym in CRYPTO + STOCKS:
        try:
            data[sym] = (fetch_stock if sym in STOCKS else fetch_crypto)(sym, fx)
        except Exception as e:  # one bad feed shouldn't stop the tick
            print("fetch failed", sym, e)
    state["fx_gbpusd"] = fx
    for m in step(state, data, int(time.time())):
        print(m)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, STATE)
    print("equity £%.2f cash £%.2f open %s" % (state["history"][-1][1], state["cash"], list(state["positions"])))


if __name__ == "__main__":
    main()
