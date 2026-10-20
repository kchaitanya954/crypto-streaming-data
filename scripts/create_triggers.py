"""
Create the evidence-backed trigger set in the bot's database (run on the server).

  1. BTC trend · daily       spot      LIVE   — the only config that was positive in both
                                                 2020–22 and 2023–26 backtests (see README)
  2. ETH trend · daily       spot      PAPER  — strong 2020–22, weak since 2023: earn it first
  3. BTC ICT/SMT · NY open   futures   PAPER  — backtest negative; shorts off; data only

No live futures trigger is created: no futures configuration tested here (indicator
1h/4h/1d, ICT/SMT in nine variants, trend at 1x) had a positive out-of-sample record.

Dry run by default — prints the plan and touches nothing:
    python -m scripts.create_triggers --user kchaitanya954
Create them (the running bot picks them up within 30 s):
    python -m scripts.create_triggers --user kchaitanya954 --apply
Also pause existing indicator (MACD) triggers, which lost money in every backtest:
    python -m scripts.create_triggers --user kchaitanya954 --apply --pause-indicator
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

from database import queries  # noqa: E402
from database.db import init_db  # noqa: E402


def plan(btc: float, eth: float, fut: float) -> list[dict]:
    return [
        dict(name="BTC trend · daily (spot)", symbol="BTCUSDT", interval="1d", market_type="spot",
             leverage=1, strategy="trend_donchian", strategy_mode="live", allow_shorts=False,
             trade_amount_usdt=btc),
        dict(name="ETH trend · daily (spot, paper)", symbol="ETHUSDT", interval="1d", market_type="spot",
             leverage=1, strategy="trend_donchian", strategy_mode="paper", allow_shorts=False,
             trade_amount_usdt=eth),
        dict(name="BTC ICT/SMT · NY open (paper)", symbol="BTCUSDT", interval="5m", market_type="futures",
             leverage=5, strategy="ict_smt", strategy_mode="paper", allow_shorts=False,
             trade_amount_usdt=fut),
    ]


async def main(a) -> None:
    load_dotenv()
    db = await init_db(a.db or os.getenv("DB_PATH", "data/trading.db"))
    try:
        users = await queries.get_all_users(db)
        user = next((u for u in users if u["username"] == a.user), None) if a.user else \
            (users[0] if len(users) == 1 else None)
        if not user:
            sys.exit(f"User not found. Known users: {[u['username'] for u in users]} — pass --user")
        existing = await queries.get_triggers(db, active_only=False, user_id=user["id"])
        print(f"User {user['username']} (id {user['id']}) — {len(existing)} existing trigger(s)\n")

        for t in plan(a.btc_budget, a.eth_budget, a.futures_budget):
            dup = next((e for e in existing if e.get("strategy") == t["strategy"]
                        and e["symbol"] == t["symbol"] and e.get("market_type") == t["market_type"]), None)
            tag = "exists (#%d) — skipped" % dup["id"] if dup else ("CREATE" if a.apply else "would create")
            print(f"  [{tag}] {t['name']}: {t['symbol']} {t['market_type']} {t['strategy']} "
                  f"{t['strategy_mode'].upper()} budget ${t['trade_amount_usdt']:.2f}")
            if a.apply and not dup:
                tid = await queries.create_trigger(
                    db, t["symbol"], t["interval"], "MEDIUM", name=t["name"],
                    trade_amount_usdt=t["trade_amount_usdt"], user_id=user["id"],
                    market_type=t["market_type"], leverage=t["leverage"], strategy=t["strategy"],
                    strategy_mode=t["strategy_mode"], allow_shorts=t["allow_shorts"])
                print(f"           → trigger #{tid}")

        indicator = [e for e in existing if (e.get("strategy") or "macd") == "macd" and e["active"]]
        if indicator:
            print(f"\n  Active indicator (MACD) triggers: "
                  + ", ".join(f"#{e['id']} {e['symbol']} {e['interval']} {e.get('market_type')}" for e in indicator))
            if a.pause_indicator and a.apply:
                for e in indicator:
                    await queries.update_trigger(db, e["id"], active=False)
                print("  → paused")
            else:
                print("  (use --apply --pause-indicator to pause them — they lost money in every backtest)")

        print("\nBefore the BTC trend trigger can buy: hold at least the budget in SPOT USDT "
              "(you hold INR — convert on CoinDCX). It buys only on a fresh breakout, one third per system,"
              " checked at each daily close (05:30 IST).")
        if not a.apply:
            print("\nDry run — nothing changed. Re-run with --apply.")
    finally:
        await db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user")
    ap.add_argument("--db")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--pause-indicator", action="store_true")
    ap.add_argument("--btc-budget", type=float, default=45.0, help="USDT for the live BTC trend trigger")
    ap.add_argument("--eth-budget", type=float, default=30.0, help="paper budget (no real money)")
    ap.add_argument("--futures-budget", type=float, default=17.0, help="paper budget (no real money)")
    asyncio.run(main(ap.parse_args()))
