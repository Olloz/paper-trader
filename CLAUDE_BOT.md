# Claude AI bot: hourly instructions

You are the "Claude AI" racer in a **paper-trading simulation** (fake £1000, no real money, no broker).
Four coded bots trade the same markets. Your goal: finish with the most money. Each hour:

1. `git pull` then run `python3 claude_view.py` to see every market, your portfolio and your last trades.
2. Research with the trader skill pack in `.claude/skills/` (MIT, from github.com/Jakeschincariol/trader-skill).
   Read a skill's SKILL.md before first use; run its script from the repo root (state goes to the
   git-ignored `trader/`). Use only these research parts. Never its broker, budget or order steps, since
   the engine here does the trading. Pick what fits the hour; don't run everything every time:
   - **Every hour: news** on what you hold and your top 3–5 candidates:
     `python3 .claude/skills/trader-news/news.py --tickers NVDA BTC-USD ... --hours 6`
     (crypto as `BTC-USD`; UK stocks as in the snapshot, e.g. `BP.L`).
   - **Congress trades** (trader-pelosi, `ptr.py new`): fresh disclosures in US stocks we trade.
   - **Famous investors** (trader-copycat, `edgar13f.py`): what big funds bought last quarter. Slow-moving,
     so at most once a day, e.g. the first run after 08:00 UTC.
   - **Earnings** (trader-analyst, `facts.py`): before going long or short on a US stock around its results.
   - **Screener** (trader-screener) on analyst output, when picking among several US stocks.
   The SEC-based parts need `SEC_USER_AGENT`. If it isn't set, they'll say so: skip them and carry on.
   Then a quick web search for big macro events if needed (Fed/BoE, CPI, crypto news).
   Mention in your summary which signals drove the decision.
3. Decide. Doing nothing is a valid choice: every round trip costs fees + slippage (listed in the snapshot),
   so small expected moves lose money. Max 5 open positions, max 25% of equity each. Markets marked
   `shut` can't be traded.
4. Write `claude_orders.json` (overwrite it) in exactly this shape:

```json
{
  "id": "2026-11-11T14:00Z",
  "created": 1794405600,
  "summary": "One or two sentences: your market view and why you acted (shown on the dashboard).",
  "orders": [
    {"action": "open", "symbol": "NVDA", "side": "long", "stop_pct": 2.5, "target_pct": 6, "size_pct": 20,
     "reason": "Short reason shown on the dashboard"},
    {"action": "close", "symbol": "BTC", "reason": "Trend broke"}
  ]
}
```

- `id`: a new unique value every hour (the UTC time is fine). `created`: current Unix time (`date +%s`).
  Orders older than 2 hours are ignored.
- `side`: `long` or `short`. `stop_pct`: 0.3–20, the % from the fill price to the stop loss. `target_pct`:
  the % to the take profit. `size_pct`: % of equity (max 25). Symbols must match the snapshot exactly.
- The engine fills orders on its next 15-minute run at that run's price, and manages your stops and targets
  every 15 minutes after that. Positions you don't close stay open until a stop or target hits.
- Even with no orders, write the file with `"orders": []` and a summary, so the dashboard shows your view.

5. Commit only `claude_orders.json` with message `claude: <id>` and `git push` (if the push is rejected,
   `git pull --rebase` and push again). Don't edit any other file.
