"""Paper day-traders: five bots race, each with a simulated £1000, across crypto (Coinbase), US and UK
stocks, gold/silver/oil and forex (Yahoo) on 15-minute candles, against buy-and-hold BTC and SPY.

Four bots follow coded rules; "Claude AI" trades on orders Claude writes to claude_orders.json
(see CLAUDE_BOT.md). Every price is converted to GBP; fees and slippage are charged on every fill.
One run = one tick. Stdlib only. Run: python3 bot.py
"""
import json
import os
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

CRYPTO = ["BTC", "ETH", "SOL", "DOGE", "ADA",
          "AVAX", "LINK", "DOT", "NEAR", "UNI", "INJ", "SUI", "RENDER", "FET", "ARB", "OP",
          "SHIB", "PEPE", "BONK", "WIF"]
US = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "AMD", "PLTR", "COIN", "MSTR", "SPY", "QQQ"]
UK = ["BP.L", "LLOY.L", "RR.L", "HSBA.L", "AZN.L", "SHEL.L", "BARC.L", "ISF.L"]
COMMODITY = ["GC=F", "SI=F", "CL=F"]
FX = ["EURGBP=X", "GBP=X"]
NAMES = {"BP.L": "BP", "LLOY.L": "Lloyds", "RR.L": "Rolls-Royce", "HSBA.L": "HSBC", "AZN.L": "AstraZeneca",
         "SHEL.L": "Shell", "BARC.L": "Barclays", "ISF.L": "FTSE 100", "GC=F": "Gold", "SI=F": "Silver",
         "CL=F": "Oil", "EURGBP=X": "EUR/GBP", "GBP=X": "USD/GBP"}
KIND = {**{s: "crypto" for s in CRYPTO}, **{s: "stock" for s in US}, **{s: "uk" for s in UK},
        **{s: "commodity" for s in COMMODITY}, **{s: "fx" for s in FX}}
# per side. uk includes stamp duty averaged over buy+sell; fx/commodity are spread-style CFD costs
FEES = {"crypto": 0.004, "stock": 0.0015, "uk": 0.003, "commodity": 0.001, "fx": 0.0005}
SLIP = {"crypto": 0.0005, "stock": 0.0002, "uk": 0.0005, "commodity": 0.0003, "fx": 0.0001}
SMALL_CRYPTO_SLIP = 0.002  # thin order books on everything but the majors
MAJORS = {"BTC", "ETH", "SOL"}
REGIME_OF = {"crypto": "BTC", "stock": "SPY", "uk": "ISF.L"}  # others use their own hourly trend

GRAN, HOUR = 900, 3600
START_CASH = 1000.0
RISK = 0.02           # risk 2% of equity per trade (distance to stop)
MAX_POS_FRAC = 0.25   # no position larger than 25% of equity
MAX_POSITIONS = 5
COOLDOWN = 4 * GRAN
MIN_ORDER = 10.0
BENCH = ["BTC", "SPY"]
AI = "Claude AI"
AI_ORDER_MAX_AGE = 2 * HOUR

BOTS = {
    "Original":   dict(style="trend", atr=GRAN, stop=2.0, tp=3.0, trail=False, filter=False, shorts=False),
    "Trend+":     dict(style="trend", atr=HOUR, stop=2.0, tp=4.0, trail=True, filter=True, shorts=False),
    "Long/Short": dict(style="trend", atr=HOUR, stop=2.0, tp=4.0, trail=True, filter=True, shorts=True),
    "Dip buyer":  dict(style="revert", atr=HOUR, stop=1.5, tp=2.0, trail=False, filter=True, shorts=False),
    AI:           dict(style="ai"),
}
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
ORDERS = os.path.join(HERE, "claude_orders.json")
LONDON = ZoneInfo("Europe/London")


def kind(sym):
    return KIND.get(sym, "crypto")


def slip(sym):
    return SMALL_CRYPTO_SLIP if kind(sym) == "crypto" and sym not in MAJORS else SLIP[kind(sym)]


# ---------- data ----------

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (paper-trader-sim)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_crypto(sym, fx_at, start=None, end=None):
    url = "https://api.exchange.coinbase.com/products/%s-USD/candles?granularity=%d" % (sym, GRAN)
    if start:
        url += "&start=%s&end=%s" % (start, end)
    rows = sorted(get(url))  # [time, low, high, open, close, volume], newest first
    return [dict(t=x[0], l=x[1] / fx_at(x[0]), h=x[2] / fx_at(x[0]), o=x[3] / fx_at(x[0]), c=x[4] / fx_at(x[0]))
            for x in rows]


def yahoo(sym, rng):
    res = get("https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=15m&range=%s" % (sym, rng))["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = [(t, o, h, l, c) for t, o, h, l, c in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"])
            if None not in (o, h, l, c)]  # Yahoo leaves gaps as nulls
    return res["meta"]["currency"], rows


def fetch_yahoo(sym, fx_at, rng="1mo"):
    cur, rows = yahoo(sym, rng)
    div = {"USD": None, "GBp": 100.0, "GBP": 1.0}[cur]  # unknown currency -> KeyError, market skipped
    return [dict(t=t, o=o / (div or fx_at(t)), h=h / (div or fx_at(t)), l=l / (div or fx_at(t)), c=c / (div or fx_at(t)))
            for t, o, h, l, c in rows]


def gbpusd():
    return get("https://query1.finance.yahoo.com/v8/finance/chart/GBPUSD=X?interval=15m&range=1d")[
        "chart"]["result"][0]["meta"]["regularMarketPrice"]


def fetch_all(fx_at, log=print):
    data = {}
    for sym in KIND:
        try:
            data[sym] = fetch_crypto(sym, fx_at) if kind(sym) == "crypto" else fetch_yahoo(sym, fx_at)
        except Exception as e:  # one bad feed shouldn't stop the tick
            log("fetch failed %s %s" % (sym, e))
    return data


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


def features(cs, now):
    """Indicators for one market, computed once per tick and shared by all bots. None if stale/short."""
    closed = [k for k in cs if k["t"] + GRAN <= now]
    if len(closed) < 60 or now - closed[-1]["t"] > 4 * GRAN:  # too little data, or market shut
        return None
    closes = [k["c"] for k in closed]
    hs = hourly(closed)
    hc = [h["c"] for h in hs]
    f = dict(c=closes[-1], r=rsi(closes), e20=ema(closes, 20)[-1], e50=ema(closes, 50)[-1], atr15=atr(closed),
             atr1h=atr(hs) if len(hs) >= 16 else None, he50=None, trend=None, since=cs[-1]["t"],
             chg1h=closes[-1] / closes[-5] - 1, chg24h=closes[-1] / closes[max(-97, -len(closes))] - 1)
    if len(hs) >= 50:
        f["he50"] = ema(hc, 50)[-1]
        f["trend"] = "up" if ema(hc, 20)[-1] > f["he50"] else "down"
    return f


def regime(sym, feats):
    f = feats.get(REGIME_OF.get(kind(sym), sym))
    return f and f["trend"]


# ---------- portfolio ----------

def new_bot():
    return {"cash": START_CASH, "positions": {}, "trades": [], "last_exit": {}, "fees": 0.0}


def new_state(now):
    return {"start_cash": START_CASH, "started": now, "bots": {n: new_bot() for n in BOTS},
            "series": list(BOTS) + ["BTC hold", "SPY hold"], "history": [], "bench": {}, "names": NAMES,
            "log": [], "daily": [], "day_start": {}, "last_day": None, "ai": {}}


def value(pos, px):
    # long: qty*px; short: collateral plus (entry - px) per unit
    return pos["qty"] * (pos["entry"] + pos["side"] * (px - pos["entry"]))


def equity(bot, last):
    return bot["cash"] + sum(value(p, last.get(s, p["entry"])) for s, p in bot["positions"].items())


def open_pos(bot, sym, side, c, stop, tp, trail, now, since, size=MAX_POS_FRAC, note=None):
    fill = c * (1 + side * slip(sym))  # you pay up when buying, get less when shorting
    if side * (fill - stop) <= 0 or side * (tp - fill) <= 0:
        return None
    eq = equity(bot, {})  # positions at entry prices: only used for sizing
    fee = FEES[kind(sym)]
    cost = min(eq * RISK / abs(fill - stop) * fill, eq * min(size, MAX_POS_FRAC), bot["cash"] / (1 + fee))
    if cost < MIN_ORDER:
        return None
    bot["cash"] -= cost * (1 + fee)
    bot["fees"] += cost * fee
    bot["positions"][sym] = {"side": side, "qty": cost / fill, "entry": fill, "stop": stop, "stop0": stop, "tp": tp,
                             "trail": trail, "cost": cost * (1 + fee), "opened": now, "since": since, "seen": -1,
                             "note": note}
    return "%s %s £%.2f @ %.6g stop %.6g target %.6g" % ("BUY" if side > 0 else "SHORT", sym, cost, fill, stop, tp)


def close_pos(bot, sym, px, why, now):
    pos = bot["positions"].pop(sym)
    fill = px * (1 - pos["side"] * slip(sym))
    fee = pos["qty"] * fill * FEES[kind(sym)]
    proceeds = value(pos, fill) - fee
    bot["cash"] += proceeds
    bot["fees"] += fee
    pnl = proceeds - pos["cost"]
    bot["trades"].append({"pair": sym, "side": pos["side"], "entry": pos["entry"], "exit": fill, "qty": pos["qty"],
                          "opened": pos["opened"], "closed": now, "pnl": round(pnl, 2),
                          "pct": round(100 * pnl / pos["cost"], 2), "why": why, "note": pos.get("note")})
    bot["last_exit"][sym] = now
    return "%s %s @ %.6g (%s) P/L £%.2f" % ("SELL" if pos["side"] > 0 else "COVER", sym, fill, why, pnl)


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


def step_rules(cfg, bot, feats, now):
    log, candidates = [], []
    for sym, f in feats.items():
        c, r = f["c"], f["r"]
        pos = bot["positions"].get(sym)
        if pos:  # strategy exits
            if cfg["style"] == "trend" and (f["e20"] - f["e50"]) * pos["side"] < 0:
                log.append(close_pos(bot, sym, c, "trend reversed", now))
            elif cfg["style"] == "revert" and r > 55:
                log.append(close_pos(bot, sym, c, "bounced back", now))
            continue
        if now - bot["last_exit"].get(sym, 0) < COOLDOWN:
            continue
        reg = regime(sym, feats)
        ok_long = not cfg["filter"] or reg == "up"
        ok_short = cfg["shorts"] and (not cfg["filter"] or reg == "down")
        side = 0
        if cfg["style"] == "trend":
            if f["e20"] > f["e50"] and c > f["e20"] and 50 < r < 70 and ok_long:
                side = 1
            elif f["e20"] < f["e50"] and c < f["e20"] and 30 < r < 50 and ok_short:
                side = -1
            strength = abs(f["e20"] / f["e50"] - 1)
        else:
            if f["he50"] and r < 30 and c > f["he50"] and ok_long:
                side = 1
            strength = 30 - r
        a = f["atr15"] if cfg["atr"] == GRAN else f["atr1h"]
        if side and a:
            candidates.append((strength, sym, side, c, a, f["since"]))
    for _, sym, side, c, a, since in sorted(candidates, reverse=True):  # strongest signals first
        if len(bot["positions"]) >= MAX_POSITIONS:
            break
        m = open_pos(bot, sym, side, c, c - side * cfg["stop"] * a, c + side * cfg["tp"] * a,
                     cfg["stop"] * a if cfg["trail"] else 0, now, since)
        if m:
            log.append(m)
    return log


def step_ai(state, bot, feats, orders, now):
    """Apply Claude's latest order file once. Stops/targets are percentages from the fill price."""
    if not orders or orders.get("id") == state["ai"].get("applied"):
        return []
    state["ai"] = {"applied": orders.get("id"), "summary": orders.get("summary", ""), "at": now}
    if now - orders.get("created", 0) > AI_ORDER_MAX_AGE:
        return ["ignored stale orders %s" % orders.get("id")]
    log = []
    for o in orders.get("orders", [])[:10]:
        sym, f, why = o.get("symbol"), feats.get(o.get("symbol")), str(o.get("reason", ""))[:200]
        if o.get("action") == "close":
            if sym in bot["positions"] and f:
                log.append(close_pos(bot, sym, f["c"], "Claude: " + why, now))
            continue
        try:
            side = {"long": 1, "short": -1}[o["side"]]
            sp, tp, size = float(o["stop_pct"]) / 100, float(o["target_pct"]) / 100, float(o.get("size_pct", 10)) / 100
        except (KeyError, ValueError, TypeError):
            log.append("rejected malformed order %s" % json.dumps(o)[:120]); continue
        if not f or sym in bot["positions"] or len(bot["positions"]) >= MAX_POSITIONS:
            log.append("rejected %s: market shut/unknown, already held, or 5 positions open" % sym); continue
        if not (0.003 <= sp <= 0.2 and 0 < tp <= 1 and 0 < size):
            log.append("rejected %s: stop must be 0.3-20%%, target 0-100%%" % sym); continue
        fill = f["c"] * (1 + side * slip(sym))
        m = open_pos(bot, sym, side, f["c"], fill * (1 - side * sp), fill * (1 + side * tp), 0, now, f["since"],
                     size=size, note=why)
        log.append(m or "rejected %s: order too small" % sym)
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


def step(state, data, now, bots=BOTS, orders=None):
    feats = {s: f for s, f in ((s, features(cs, now)) for s, cs in data.items()) if f}
    last = {s: cs[-1]["c"] for s, cs in data.items() if cs}
    log = []
    for name, cfg in bots.items():
        bot = state["bots"].setdefault(name, new_bot())
        msgs = [m for s in list(bot["positions"]) if s in data for m in [check_stops(bot, s, data[s], now)] if m]
        msgs += step_ai(state, bot, feats, orders, now) if cfg["style"] == "ai" else step_rules(cfg, bot, feats, now)
        log += ["[%s] %s" % (name, m) for m in msgs]

    for b in BENCH:
        if b in last:
            state["bench"].setdefault(b, last[b])
    row = [now] + [round(equity(state["bots"][n], last), 2) for n in bots]
    row += [round(START_CASH * last[b] / state["bench"][b], 2) if b in last and b in state["bench"] else None
            for b in BENCH]
    state["series"] = list(bots) + ["BTC hold", "SPY hold"]
    state["history"] = (state["history"] + [row])[-10000:]
    state["log"] = (state["log"] + [[now, m] for m in log])[-500:]
    state.update(updated=now, last_prices=last, regimes={k: regime(s, feats) for k, s in REGIME_OF.items()})
    return log, daily_summary(state, last, now)


def notify(text):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic or not text:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            "https://ntfy.sh/" + topic, data=text.encode(), headers={"Title": "Paper Trader"}), timeout=10)
    except Exception as e:  # alerts are nice-to-have; never fail the tick over them
        print("notify failed", e)


def load_state(now):
    if not os.path.exists(STATE):
        return new_state(now)
    state = json.load(open(STATE))
    for n in BOTS:  # a bot added later joins with a fresh £1000; old history rows lack it (None)
        if n not in state["bots"]:
            state["bots"][n] = new_bot()
            i = 1 + list(state["bots"]).index(n)
            state["history"] = [r[:i] + [None] + r[i:] for r in state["history"]]
    state.setdefault("ai", {}); state["names"] = NAMES
    return state


def main():
    now = int(time.time())
    state = load_state(now)
    fx = gbpusd()  # no FX rate -> fail the run rather than mis-price everything
    data = fetch_all(lambda t: fx)
    state["fx_gbpusd"] = fx
    orders = json.load(open(ORDERS)) if os.path.exists(ORDERS) else None
    log, summary = step(state, data, now, orders=orders)
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
