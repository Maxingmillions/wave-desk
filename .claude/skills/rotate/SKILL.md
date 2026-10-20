---
name: rotate
description: Run the rotation terminal against the live IBKR account - gather the day's news and prices, decide next-session buys/sells, and (on request) turn the plan into IBKR review-and-approve order instructions. Use when the user says /rotate, "what should I rotate into tomorrow", "run the terminal", or asks for tomorrow's buys.
---

# /rotate - news-driven next-day rotation plan

You are the decision-maker. `plan.py` gathers the dossier (news, prices,
technicals, screener picks); you read it and write the plan. Do not place
orders. The IBKR connector in this environment is review-and-approve: the most
you can do is create *instructions* the user submits themselves.

## Steps

1. **Live account.** Call the IBKR tools `get_account_positions`,
   `get_account_summary` and `get_account_orders` (show_completed=true). Build
   the `--positions TICKER:QTY:AVG,...` and `--cash <available_funds>` arguments
   from them. Working orders count against cash; say so if any exist.
2. **Gather.** Run, from the repo root (use `./.venv/bin/python` if a venv exists):
   ```
   python plan.py --no-llm --positions ... --cash ... [--watch X,Y]
   ```
   `--no-llm` is deliberate: you are the planner here, not the API. It writes
   `plans/<session>.dossier.json`, `plans/<session>.json` (rule-based draft)
   and `.md`. Takes 1-3 minutes.
3. **Read, then decide.** Read the dossier: `market_headlines`, every
   candidate's `headlines` and `features`, `account`, `constraints`. For the
   held names, read all of their headlines, not just the top ones. Then write
   your own plan in the same shape as `rotation.PLAN_SCHEMA` and save it over
   `plans/<session>.json` (keep `_engine: "claude-session"`). Rules that bind
   you the same way they bind the API planner:
   - The owner prefers buys. SELL/TRIM only with a concrete reason (stop
     breached, thesis broke, earnings inside the buffer, or to fund one
     rotation when cash is short - and say that it is optional).
   - Whole shares, no margin, cost <= cash + proposed sale proceeds, at most
     `max_buys` names, each <= `max_weight_per_name` of equity, no buy inside
     `earnings_buffer_days` of earnings, nothing with `avg_dollar_vol_m` < 5.
   - A catalyst in the headlines beats a chart. A secondary offering,
     going-concern note, SEC action or guidance cut disqualifies a buy.
   - "Nothing worth buying" is a valid plan; say why.
4. **Report.** Show the plan as a short table: holdings with action + reason,
   buys with qty / limit / stop / catalyst / risk, cash after plan, the top
   risks, and the 3-5 headlines that drove it. Re-render with
   `python plan.py --show plans/<session>.json` if a terminal view helps.
5. **Only if the user says to stage the orders:** for each BUY/SELL/TRIM call
   `search_contracts` to get the contract id, then `create_order_instruction`
   (side, quantity, LIMIT + limit_price or MARKET, time_in_force DAY unless
   told otherwise). Surface every returned deep-link URL - the user must open
   it to submit. Never create an instruction the plan did not list.

## Notes

- `universe.txt` is the always-scan list; Yahoo screeners (day gainers, most
  actives, growth tech, undervalued growth, small-cap gainers) and tickers
  found in headlines are added automatically.
- Yahoo's RSS rate-limits cloud IPs; Google News, Finviz and WSJ still cover
  the pool. Say so if `n_headlines` in the dossier looks thin (< 300).
- The account is small. Spread matters more than commission; prefer liquid
  names and limit orders.
