import bot

G = bot.GRAN


def trend(n=80, start=100.0, step=0.3):
    # steady uptrend with small pullbacks so RSI stays below 70
    cs, p = [], start
    for i in range(n):
        o = p
        p = p + (step if i % 3 else -step * 1.2)
        cs.append(dict(t=i * G, o=o, c=p, h=max(o, p) + 0.2, l=min(o, p) - 0.2))
    return cs


cs = trend()
now = cs[-1]["t"] + G  # last candle just closed
s = bot.new_state()
log = bot.step(s, {"BTC-GBP": cs}, now)
assert log and log[0].startswith("BUY"), log
pos = s["positions"]["BTC-GBP"]
assert pos["stop"] < pos["entry"] < pos["tp"]
assert abs(s["cash"] + pos["cost"] - 1000) < 1e-9

# next candle crashes through the stop -> exit at stop, a loss near 2% risk + fees
crash = dict(t=now, o=pos["entry"], c=pos["stop"] - 5, h=pos["entry"], l=pos["stop"] - 5)
log = bot.step(s, {"BTC-GBP": cs + [crash]}, now + 60)
assert "stop loss" in log[0], log
t = s["trades"][0]
assert t["pnl"] < 0 and abs(t["pnl"]) < 1000 * bot.RISK + 10, t
assert not s["positions"] and abs(s["cash"] - (1000 + t["pnl"])) < 0.01

# cooldown blocks immediate re-entry
assert not bot.step(s, {"BTC-GBP": cs}, now + 120)
print("ok", s["trades"][0])
