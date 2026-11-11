import bot

G = bot.GRAN
BASE = dict(style="trend", atr=G, stop=2.0, tp=3.0, trail=False, filter=False, shorts=False)


def trend(n=80, start=100.0, step=0.3):
    # steady trend with small pullbacks so RSI stays in the 50-70 (or 30-50) band
    cs, p = [], start
    for i in range(n):
        o = p
        p = p + (step if i % 3 else -step * 1.2)
        cs.append(dict(t=i * G, o=o, c=p, h=max(o, p) + 0.2, l=min(o, p) - 0.2))
    return cs


def candle(t, o, c, h=None, l=None):
    return dict(t=t, o=o, c=c, h=max(o, c) if h is None else h, l=min(o, c) if l is None else l)


def run(cfg, cs, now):
    s = bot.new_state(0)
    s["bots"] = {}
    log, _ = bot.step(s, {"X": cs}, now, {"t": cfg})
    return s, s["bots"]["t"], log


# 1. long entry, then a crash through the stop: loss about 2% risk + fees, cash adds up
cs = trend(); now = cs[-1]["t"] + G
s, b, log = run(BASE, cs, now)
assert log and "BUY X" in log[0], log
pos = b["positions"]["X"]
assert pos["stop"] < pos["entry"] < pos["tp"]
assert abs(b["cash"] + pos["cost"] - 1000) < 1e-9
log, _ = bot.step(s, {"X": cs + [candle(now, pos["entry"], pos["stop"] - 5)]}, now + 60, {"t": BASE})
assert "stop loss" in log[0], log
t = b["trades"][0]
assert t["pnl"] < 0 and abs(t["pnl"]) < 1000 * bot.RISK + 10, t
assert not b["positions"] and abs(b["cash"] - (1000 + t["pnl"])) < 0.01
assert not bot.step(s, {"X": cs}, now + 120, {"t": BASE})[0]  # cooldown blocks re-entry

# 2. trailing stop: price runs up (closed candles raise the stop), then dips -> exits in profit
cfg = dict(BASE, trail=True, tp=50)
s, b, log = run(cfg, cs, now)
pos = b["positions"]["X"]; e = pos["entry"]
up = [candle(now + i * G, e + i * 0.5, e + (i + 1) * 0.5) for i in range(6)]
bot.step(s, {"X": cs + up}, now + 6 * G + 1, {"t": cfg})
assert pos["stop"] > pos["stop0"] and pos["stop"] > e, pos
dip = candle(now + 6 * G, e + 3, pos["stop"] - 1)
log, _ = bot.step(s, {"X": cs + up + [dip]}, now + 6 * G + 60, {"t": cfg})
assert "trailing stop" in log[0] and b["trades"][-1]["pnl"] > 0, (log, b["trades"])

# 3. short: downtrend opens a short, price falls to target -> profit
cfg = dict(BASE, shorts=True)
down = trend(step=-0.3, start=200)
s, b, log = run(cfg, down, now)
assert log and "SHORT X" in log[0], log
pos = b["positions"]["X"]
assert pos["tp"] < pos["entry"] < pos["stop"]
log, _ = bot.step(s, {"X": down + [candle(now, pos["entry"], pos["tp"] - 1)]}, now + 60, {"t": cfg})
assert "take profit" in log[0] and b["trades"][-1]["pnl"] > 0, (log, b["trades"])

# 4. market filter: with no regime data, filtered bots stay out
s, b, log = run(dict(BASE, filter=True), cs, now)
assert not log and not b["positions"]

# 5. daily summary fires once on a new London day
s = bot.new_state(0)
assert bot.daily_summary(s, {}, 1794355200) is None          # first day: just records the start
assert "Daily summary" in bot.daily_summary(s, {}, 1794355200 + 86400)
assert bot.daily_summary(s, {}, 1794355200 + 86400 + 60) is None
print("ok")
