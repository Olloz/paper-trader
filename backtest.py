"""Replay the last ~59 days of 15-minute prices through the four rule-based bots (Claude AI can't be
replayed). Same engine, fees and slippage as live. Writes backtest.json for the dashboard.
Run: python3 backtest.py   (takes a few minutes: ~600 downloads, then the replay)
"""
import bisect
import json
import os
import time
from datetime import datetime, timezone

import bot

DAYS = 59                 # Yahoo keeps 60 days of 15-minute candles
WINDOW = 260              # candles per market handed to the engine each tick (enough for hourly EMA50)
OUT = os.path.join(bot.HERE, "backtest.json")


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    end = int(time.time()) // bot.GRAN * bot.GRAN
    start = end - DAYS * 86400
    _, fxrows = bot.yahoo("GBPUSD=X", "60d")
    fx_t, fx_v = [r[0] for r in fxrows], [r[4] for r in fxrows]

    def fx_at(t):  # last known GBP/USD at time t (weekends carry Friday's rate)
        return fx_v[max(0, bisect.bisect_right(fx_t, t) - 1)]

    raw = {}
    for sym in bot.KIND:
        try:
            if bot.kind(sym) == "crypto":
                cs, t = {}, start
                while t < end:  # Coinbase returns at most 300 candles per request
                    for k in bot.fetch_crypto(sym, fx_at, iso(t), iso(min(t + 300 * bot.GRAN, end))):
                        cs[k["t"]] = k
                    t += 300 * bot.GRAN
                    time.sleep(0.12)
                raw[sym] = [cs[t] for t in sorted(cs)]
            else:
                raw[sym] = [k for k in bot.fetch_yahoo(sym, fx_at, "60d") if k["t"] >= start]
            print("%-9s %5d candles" % (sym, len(raw[sym])))
        except Exception as e:
            print("skip", sym, e)
    times = {s: [k["t"] for k in cs] for s, cs in raw.items()}

    rules = {n: c for n, c in bot.BOTS.items() if c["style"] != "ai"}
    t0 = start + 3 * 86400  # warm-up so indicators have history
    state = bot.new_state(t0)
    state["bots"] = {n: bot.new_bot() for n in rules}
    for now in range(t0, end + 1, bot.GRAN):
        data = {}
        for s, cs in raw.items():
            j = bisect.bisect_right(times[s], now - bot.GRAN)  # candles fully closed by `now`
            if j:
                data[s] = cs[max(0, j - WINDOW):j]
        bot.step(state, data, now, rules)
        if (now - t0) % (7 * 86400) == 0:
            print("day %d" % ((now - t0) // 86400), dict(zip(state["series"], state["history"][-1][1:])))

    hist = [r for r in state["history"] if (r[0] - t0) % bot.HOUR == 0] + state["history"][-1:]
    out = {"from": t0, "to": end, "days": (end - t0) // 86400, "start_cash": bot.START_CASH,
           "series": state["series"], "history": hist, "names": bot.NAMES,
           "bots": {n: {"trades": b["trades"], "fees": round(b["fees"], 2), "positions": b["positions"]}
                    for n, b in state["bots"].items()}}
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("final", dict(zip(state["series"], hist[-1][1:])))
    for n, b in state["bots"].items():
        print("%-11s %4d trades, fees £%.2f" % (n, len(b["trades"]), b["fees"]))


if __name__ == "__main__":
    main()
