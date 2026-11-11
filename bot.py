"""Paper day-traders: four strategies race, each with a simulated £1000, across crypto (Coinbase)
and US stocks (Yahoo) on 15-minute candles, against buy-and-hold BTC and SPY benchmarks.

All prices are converted to GBP at the live GBP/USD rate, so FX moves count, as for a UK trader.
One run = one tick: per bot, check stops/targets (trailing where enabled), strategy exits, new
entries; then record equity, send phone alerts (ntfy, if NTFY_TOPIC is set) and a daily summary.
Stdlib only. Run: python3 bot.py
"""
import json
import os
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

CRYPTO = ["BTC", "ETH", "SOL", "DOGE", "ADA",                       # majors
          "AVAX", "LINK", "DOT", "NEAR", "UNI", "INJ", "SUI", "RENDER", "FET", "ARB", "OP",
          "SHIB", "PEPE", "BONK", "WIF"]                              # smaller caps / memes
STOCKS = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "AMD",
          "PLTR", "COIN", "MSTR", "SPY", "QQQ"]
FEES = {"crypto": 0.004, "stock": 0.0015}  # per side: retail crypto taker fee; stock app FX fee
GRAN, HOUR = 900, 3600
START_CASH = 1000.0
RISK = 0.02           # risk 2% of equity per trade (distance to stop)
MAX_POS_FRAC = 0.25   # no position larger than 25% of equity
MAX_POSITIONS = 5
COOLDOWN = 4 * GRAN   # wait an hour before re-entering a market after exit
MIN_ORDER = 10.0
BENCH = ["BTC", "SPY"]

# style: "trend" follows momentum, "revert" buys oversold dips in an uptrend.
# atr: candle size the stop distance is measured on. filter: only trade with the BTC/SPY trend.
BOTS = {
    "Original":   dict(style="trend", atr=GRAN, stop=2.0, tp=3.0, trail=False, filter=False, shorts=False),
    "Trend+":     dict(style="trend", atr=HOUR, stop=2.0, tp=4.0, trail=True, filter=True, shorts=False),
    "Long/Short": dict(style="trend", atr=HOUR, stop=2.0, tp=4.0, trail=True, filter=True, shorts=True),
    "Dip buyer":  dict(style="revert", atr=HOUR, stop=1.5, tp=2.0, trail=False, filter=True, shorts=False),
}
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
LONDON = ZoneInfo("Europe/London")


# ---------- data ----------

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (paper-trader-sim)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_crypto(sym, fx):
    rows = sorted(get("https://api.exchange.coinbase.com/products/%s-USD/candles?granularity=%d" % (sym, GRAN)))
    # rows: [time, low, high, open, close, volume]
    return [dict(t=x[0], l=x[1] / fx, h=x[2] / fx, o=x[3] / fx, c=x[4] / fx) for x in rows]


def fetch_stock(sym, fx):
    # 1 month of 15m candles so hourly EMA50 (market filter) has enough history
    res = get("https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=15m&range=1mo" % sym)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    return [dict(t=t, o=o / fx, h=h / fx, l=l / fx, c=c / fx)
            for t, o, h, l, c in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"])
            if None not in (o, h, l, c)]  # Yahoo leaves gaps as nulls


def gbpusd():
    return get("https://query1.finance.yahoo.com/v8/finance/chart/GBPUSD=X?interval=15m&range=1d")[
        "chart"]["result"][0]["meta"]["regularMarketPrice"]


def kind(sym):
    return "stock" if sym in STOCKS else "crypto"


# ---------- indicators ----------

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


def hourly(closed):
    """Group closed 15m candles into hourly ones; the last (possibly partial) hour is dropped."""
    hs = {}
    for k in closed:
        h = hs.setdefault(k["t"] // HOUR, dict(t=k["t"] // HOUR * HOUR, o=k["o"], h=k["h"], l=k["l"], c=k["c"]))
        h["h"], h["l"], h["c"] = max(h["h"], k["h"]), min(h["l"], k["l"]), k["c"]
    return [hs[t] for t in sorted(hs)][:-1]


def regime(cs, now):
    """'up' / 'down' from hourly EMA20 vs EMA50, or None without enough data."""
    hs = hourly([k for k in cs if k["t"] + GRAN <= now])
    if len(hs) < 50:
        return None
    closes = [h["c"] for h in hs]
    return "up" if ema(closes, 20)[-1] > ema(closes, 50)[-1] else "down"


# ---------- portfolio ----------

def new_bot():
    return {"cash": START_CASH, "positions": {}, "trades": [], "last_exit": {}, "fees": 0.0}


def new_state(now):
    return {"start_cash": START_CASH, "started": now, "bots": {n: new_bot() for n in BOTS},
            "series": list(BOTS) + ["BTC hold", "SPY hold"], "history": [], "bench": {},
            "log": [], "daily": [], "day_start": {}, "last_day": None}


def value(pos, px):
    # long: qty*px; short: collateral plus (entry - px) per unit
    return pos["qty"] * (pos["entry"] + pos["side"] * (px - pos["entry"]))


def equity(bot, last):
    return bot["cash"] + sum(value(p, last.get(s, p["entry"])) for s, p in bot["positions"].items())


def open_pos(bot, sym, side, c, stop, tp, trail, now, since):
    eq = equity(bot, {})  # entry prices: only used for sizing
    fee = FEES[kind(sym)]
    cost = min(eq * RISK / abs(c - stop) * c, eq * MAX_POS_FRAC, bot["cash"] / (1 + fee))
    if cost < MIN_ORDER:
        return None
    bot["cash"] -= cost * (1 + fee)
    bot["fees"] += cost * fee
    bot["positions"][sym] = {"side": side, "qty": cost / c, "entry": c, "stop": stop, "stop0": stop, "tp": tp,
                             "trail": trail, "cost": cost * (1 + fee), "opened": now, "since": since, "seen": -1}
    return "%s %s £%.2f @ %.6g stop %.6g target %.6g" % ("BUY" if side > 0 else "SHORT", sym, cost, c, stop, tp)


def close_pos(bot, sym, px, why, now):
    pos = bot["positions"].pop(sym)
    fee = pos["qty"] * px * FEES[kind(sym)]
    proceeds = value(pos, px) - fee
    bot["cash"] += proceeds
    bot["fees"] += fee
    pnl = proceeds - pos["cost"]
    bot["trades"].append({"pair": sym, "side": pos["side"], "entry": pos["entry"], "exit": px, "qty": pos["qty"],
                          "opened": pos["opened"], "closed": now, "pnl": round(pnl, 2),
                          "pct": round(100 * pnl / pos["cost"], 2), "why": why})
    bot["last_exit"][sym] = now
    return "%s %s @ %.6g (%s) P/L £%.2f" % ("SELL" if pos["side"] > 0 else "COVER", sym, px, why, pnl)


def check_stops(bot, sym, cs, now):
    """Walk candles since entry: stop first (conservative), then target, then trail on closed candles."""
    pos = bot["positions"][sym]
    for k in cs:
        if k["t"] < pos["since"] or k["t"] <= pos["seen"]:
            continue
        why = "trailing stop" if pos["stop"] != pos["stop0"] else "stop loss"
        if pos["side"] > 0:
            if k["l"] <= pos["stop"]:
                return close_pos(bot, sym, min(pos["stop"], k["o"]), why, now)
            if k["h"] >= pos["tp"]:
                return close_pos(bot, sym, max(pos["tp"], k["o"]), "take profit", now)
        else:
            if k["h"] >= pos["stop"]:
                return close_pos(bot, sym, max(pos["stop"], k["o"]), why, now)
            if k["l"] <= pos["tp"]:
                return close_pos(bot, sym, min(pos["tp"], k["o"]), "take profit", now)
        if k["t"] + GRAN <= now:  # only closed candles move the trail, so no candle is judged twice
            if pos["trail"]:
                if pos["side"] > 0:
                    pos["stop"] = max(pos["stop"], k["h"] - pos["trail"])
                else:
                    pos["stop"] = min(pos["stop"], k["l"] + pos["trail"])
            pos["seen"] = k["t"]
    return None


def step_bot(name, cfg, bot, data, regimes, now):
    log = []
    for sym in list(bot["positions"]):
        if sym in data:
            m = check_stops(bot, sym, data[sym], now)
            if m:
                log.append(m)

    candidates = []
    for sym, cs in data.items():
        closed = [k for k in cs if k["t"] + GRAN <= now]
        if len(closed) < 60 or now - closed[-1]["t"] > 4 * GRAN:  # too little or stale (market shut)
            continue
        closes = [k["c"] for k in closed]
        c, r = closes[-1], rsi(closes)
        e20, e50 = ema(closes, 20)[-1], ema(closes, 50)[-1]

        pos = bot["positions"].get(sym)
        if pos:  # strategy exits
            if cfg["style"] == "trend" and (e20 - e50) * pos["side"] < 0:
                log.append(close_pos(bot, sym, c, "trend reversed", now))
            elif cfg["style"] == "revert" and r > 55:
                log.append(close_pos(bot, sym, c, "bounced back", now))
            continue
        if now - bot["last_exit"].get(sym, 0) < COOLDOWN:
            continue

        reg = regimes.get(kind(sym))
        ok_long = not cfg["filter"] or reg == "up"
        ok_short = cfg["shorts"] and (not cfg["filter"] or reg == "down")
        side = 0
        if cfg["style"] == "trend":
            if e20 > e50 and c > e20 and 50 < r < 70 and ok_long:
                side = 1
            elif e20 < e50 and c < e20 and 30 < r < 50 and ok_short:
                side = -1
            strength = abs(e20 / e50 - 1)
        else:
            hs = hourly(closed)
            if len(hs) >= 50 and r < 30 and c > ema([h["c"] for h in hs], 50)[-1] and ok_long:
                side = 1
            strength = 30 - r
        if not side:
            continue
        bars = closed if cfg["atr"] == GRAN else hourly(closed)
        if len(bars) < 16:
            continue
        a = atr(bars)
        if a <= 0:
            continue
        candidates.append((strength, sym, side, c, a, cs[-1]["t"]))

    for _, sym, side, c, a, since in sorted(candidates, reverse=True):  # strongest signals first
        if len(bot["positions"]) >= MAX_POSITIONS:
            break
        stop, tp = c - side * cfg["stop"] * a, c + side * cfg["tp"] * a
        m = open_pos(bot, sym, side, c, stop, tp, cfg["stop"] * a if cfg["trail"] else 0, now, since)
        if m:
            log.append(m)
    return log


def daily_summary(state, last, now):
    """On the first tick of a new London day, summarise the previous day. Returns text or None."""
    today = datetime.fromtimestamp(now, LONDON).date().isoformat()
    if state["last_day"] == today:
        return None
    eqs = {n: equity(b, last) for n, b in state["bots"].items()}
    text = None
    if state["last_day"]:
        day = state["last_day"]
        rows = {}
        for n, b in state["bots"].items():
            done = [t for t in b["trades"] if datetime.fromtimestamp(t["closed"], LONDON).date().isoformat() == day]
            start = state["day_start"].get(n, START_CASH)
            rows[n] = {"equity": round(eqs[n], 2), "change": round(eqs[n] - start, 2), "trades": len(done),
                       "wins": sum(t["pnl"] > 0 for t in done)}
        state["daily"] = (state["daily"] + [{"day": day, "bots": rows}])[-90:]
        text = "Daily summary %s\n" % day + "\n".join(
            "%s: £%.2f (%+.2f today), %d trades, %d wins" % (n, r["equity"], r["change"], r["trades"], r["wins"])
            for n, r in rows.items())
    state["day_start"], state["last_day"] = eqs, today
    return text


def step(state, data, now, bots=BOTS):
    regimes = {"crypto": regime(data.get("BTC", []), now), "stock": regime(data.get("SPY", []), now)}
    last = {s: cs[-1]["c"] for s, cs in data.items() if cs}
    log = []
    for name, cfg in bots.items():
        bot = state["bots"].setdefault(name, new_bot())
        log += ["[%s] %s" % (name, m) for m in step_bot(name, cfg, bot, data, regimes, now)]

    for b in BENCH:
        if b in last:
            state["bench"].setdefault(b, last[b])
    row = [now] + [round(equity(state["bots"][n], last), 2) for n in bots]
    row += [round(START_CASH * last[b] / state["bench"][b], 2) if b in last and b in state["bench"] else None
            for b in BENCH]
    state["history"] = (state["history"] + [row])[-10000:]
    state["log"] = (state["log"] + [[now, m] for m in log])[-500:]
    state.update(updated=now, last_prices=last, regimes=regimes)
    summary = daily_summary(state, last, now)
    return log, summary


def notify(text):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic or not text:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://ntfy.sh/" + topic, data=text.encode(), headers={"Title": "Paper Trader"}), timeout=10)
    except Exception as e:  # alerts are nice-to-have; never fail the tick over them
        print("notify failed", e)


def main():
    now = int(time.time())
    state = json.load(open(STATE)) if os.path.exists(STATE) else new_state(now)
    fx = gbpusd()  # no FX rate -> fail the run rather than mis-price everything
    data = {}
    for sym in CRYPTO + STOCKS:
        try:
            data[sym] = (fetch_stock if sym in STOCKS else fetch_crypto)(sym, fx)
        except Exception as e:  # one bad feed shouldn't stop the tick
            print("fetch failed", sym, e)
    state["fx_gbpusd"] = fx
    log, summary = step(state, data, now)
    for m in log:
        print(m)
    notify("\n".join(log))
    notify(summary)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, STATE)
    print("equity", dict(zip(state["series"], state["history"][-1][1:])), "regimes", state["regimes"])


if __name__ == "__main__":
    main()
