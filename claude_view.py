"""Market snapshot for the Claude AI bot: every market's indicators plus its own portfolio.
Run: python3 claude_view.py
"""
import json
import os
import time

import bot


def main():
    now = int(time.time())
    fx = bot.gbpusd()
    data = bot.fetch_all(lambda t: fx, log=lambda m: None)
    feats = {s: bot.features(cs, now) for s, cs in data.items()}
    state = bot.load_state(now)
    me = state["bots"][bot.AI]
    last = {s: cs[-1]["c"] for s, cs in data.items() if cs}

    print("Time (UTC): %s   GBP/USD %.4f" % (time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)), fx))
    print("\nMARKETS (prices in GBP; 'shut' = market closed, cannot trade now)")
    print("%-9s %-12s %-9s %12s %7s %7s %5s %6s %6s %6s" %
          ("symbol", "name", "kind", "price", "1h%", "24h%", "rsi", "15m", "1h", "atr1h%"))
    for s, f in feats.items():
        name = bot.NAMES.get(s, s)
        if not f:
            print("%-9s %-12s %-9s shut" % (s, name, bot.kind(s))); continue
        print("%-9s %-12s %-9s %12.6g %+7.2f %+7.2f %5.0f %6s %6s %6s" % (
            s, name, bot.kind(s), f["c"], 100 * f["chg1h"], 100 * f["chg24h"], f["r"],
            "up" if f["e20"] > f["e50"] else "down", f["trend"] or "?",
            "%.2f" % (100 * f["atr1h"] / f["c"]) if f["atr1h"] else "?"))

    print("\nCOSTS per side: fees %s; slippage %s (small cryptos %.1f%%)" % (
        {k: "%.2f%%" % (100 * v) for k, v in bot.FEES.items()},
        {k: "%.2f%%" % (100 * v) for k, v in bot.SLIP.items()}, 100 * bot.SMALL_CRYPTO_SLIP))
    print("\nYOUR PORTFOLIO: equity £%.2f, cash £%.2f (started £%.0f)" % (
        bot.equity(me, last), me["cash"], bot.START_CASH))
    for s, p in me["positions"].items():
        px = last.get(s, p["entry"])
        print("  %s %-9s entry %.6g now %.6g stop %.6g target %.6g  P/L £%.2f  (%s)" % (
            "LONG " if p["side"] > 0 else "SHORT", s, p["entry"], px, p["stop"], p["tp"],
            bot.value(p, px) - p["cost"], p.get("note") or ""))
    print("\nYOUR LAST 10 TRADES")
    for t in me["trades"][-10:]:
        print("  %s %-9s P/L £%+.2f  %s" % ("LONG " if t["side"] > 0 else "SHORT", t["pair"], t["pnl"], t["why"]))
    print("\nOTHER BOTS:", {n: round(bot.equity(b, last), 2) for n, b in state["bots"].items() if n != bot.AI})
    if os.path.exists(bot.ORDERS):
        print("\nYOUR PREVIOUS ORDER FILE:", json.load(open(bot.ORDERS)).get("summary", ""))


if __name__ == "__main__":
    main()
