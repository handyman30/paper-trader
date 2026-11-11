"""Swing strategy on Alpaca PAPER. Runs once per trading day ~5 min after the open, using
yesterday's daily close for signals (same as the backtest: signal at close, fill next open).

- Short-term buys: market bracket order, GTC, take-profit +X% / stop -Y% held server-side.
- Long-term buys: market order, then a server-side GTC trailing stop (or bracket if TP/SL set).
- Month start: long-term trend exits + fill empty slots.
- Daily: short-term time stops, reconcile positions closed by the server, log equity.
"""
import math
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from . import db
from .config import alpaca_keys, assert_paper_only, has_keys
from .fundamentals import select_quality
from .swing import load_daily, rsi_panel

NY = "America/New_York"


def run_swing_live(cfg: dict):
    assert_paper_only()
    if not has_keys():
        raise SystemExit("Add ALPACA_API_KEY / ALPACA_SECRET_KEY (paper) to .env first. See .env.example.")
    from alpaca.trading.client import TradingClient

    k, s = alpaca_keys()
    tc = TradingClient(k, s, paper=True)
    con = db.connect(cfg["db_path"])
    run_id = db.new_run(con, "swing-live", "alpaca-paper", cfg)
    print(f"Swing paper run #{run_id} | equity ${float(tc.get_account().equity):,.0f} | Ctrl+C to stop")
    last_done = None
    while True:
        try:
            clock = tc.get_clock()
            today = clock.timestamp.astimezone(timezone.utc).date()
            if clock.is_open and last_done != today:
                from alpaca.trading.requests import GetCalendarRequest
                now_ny = pd.Timestamp(clock.timestamp).tz_convert(NY)
                cal = tc.get_calendar(GetCalendarRequest(start=now_ny.date(), end=now_ny.date()))
                if cal:
                    o = pd.Timestamp(cal[0].open)
                    open_ny = o.tz_localize(NY) if o.tzinfo is None else o.tz_convert(NY)
                    if now_ny >= open_ny + pd.Timedelta(minutes=5):
                        daily_routine(cfg, tc, con, run_id)
                        last_done = today
            elif not clock.is_open:
                print(f"Market closed. Next open {clock.next_open:%Y-%m-%d %H:%M %Z}.", end="\r")
        except KeyboardInterrupt:
            print("Stopped.")
            return
        except Exception as e:
            db.log_event(con, run_id, "error", detail=str(e))
            print(f"Error (will retry): {e}")
        time.sleep(300)


def _open_orders(tc, sym):
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    return tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym], nested=True))


def _exit(tc, con, run_id, sym, reason):
    for o in _open_orders(tc, sym):
        tc.cancel_order_by_id(o.id)
    time.sleep(1)
    tc.close_position(sym)
    db.log_event(con, run_id, "exit", sym, {"reason": reason})
    print(f"  SELL {sym} ({reason})")


def _record_closed(tc, con, run_id, row):
    """Position vanished (server-side TP/SL/trailing or our own exit). Log the trade from the fill."""
    from alpaca.trading.enums import OrderSide, QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    sym, sleeve, entry, entry_date, qty = row
    fills = [o for o in tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[sym], limit=20))
             if o.side == OrderSide.SELL and o.filled_avg_price]
    if fills and entry:
        o = max(fills, key=lambda x: x.filled_at)
        px = float(o.filled_avg_price)
        reason = {"limit": "target", "stop": "stop", "trailing_stop": "trailing_stop"}.get(
            str(o.order_type.value) if o.order_type else "", "market_exit")
        con.execute("INSERT INTO trades (run_id, ticker, entry_time, exit_time, qty, entry_price, exit_price, pnl, "
                    "ret_pct, reason, sleeve) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, sym, entry_date, str(o.filled_at.date()), qty, entry, px, qty * (px - entry),
                     (px / entry - 1) * 100, reason, sleeve))
    con.execute("DELETE FROM swing_positions WHERE symbol=?", (sym,))
    con.commit()


def daily_routine(cfg, tc, con, run_id):
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import (MarketOrderRequest, StopLossRequest, TakeProfitRequest,
                                         TrailingStopOrderRequest)
    s = cfg["swing"]
    bench = cfg.get("benchmark", "SPY")
    now = pd.Timestamp.now(tz=NY)
    print(f"\n{now:%Y-%m-%d %H:%M} daily routine")

    # 1) reconcile
    positions = {p.symbol: p for p in tc.get_all_positions()}
    rows = con.execute("SELECT symbol, sleeve, entry_price, entry_date, qty FROM swing_positions").fetchall()
    for row in rows:
        if row[0] not in positions:
            _record_closed(tc, con, run_id, row)
        else:  # refresh with actual fill price/qty
            p = positions[row[0]]
            con.execute("UPDATE swing_positions SET entry_price=?, qty=? WHERE symbol=?",
                        (float(p.avg_entry_price), float(p.qty), row[0]))
    con.commit()
    mine = {r[0]: r[1] for r in con.execute("SELECT symbol, sleeve FROM swing_positions")}

    # 2) data (completed daily bars only)
    start = (date.today() - timedelta(days=500)).isoformat()
    px = load_daily(sorted(set(s["universe"]) | {bench}), start)
    C = px["close"]
    C = C[C.index.date < now.date()]
    universe = select_quality(s["universe"], s["top_n_quality"]) if s["fundamental_filter"] else list(s["universe"])
    Cu = C[universe]
    last = Cu.iloc[-1]
    sma200, sma50 = Cu.rolling(200).mean().iloc[-1], Cu.rolling(50).mean().iloc[-1]
    uptrend = last > sma200
    rsi = rsi_panel(Cu, s["short_term"]["rsi_period"]).iloc[-1]
    mom = (Cu.shift(21) / Cu.shift(252) - 1).iloc[-1]
    equity = float(tc.get_account().equity)
    month_start = C.index[-1].month != now.month

    # 3) exits we manage ourselves
    trading_days = list(C.index.date)
    for sym, sleeve in list(mine.items()):
        if sym not in positions:
            continue
        if sleeve == "short_term" and s["short_term"].get("max_hold_days"):
            ed = con.execute("SELECT entry_date FROM swing_positions WHERE symbol=?", (sym,)).fetchone()[0]
            held = sum(1 for d in trading_days if d >= date.fromisoformat(ed)) + 1
            if held >= s["short_term"]["max_hold_days"]:
                _exit(tc, con, run_id, sym, "time_stop")
        if sleeve == "long_term" and month_start and not bool(uptrend.get(sym, True)):
            _exit(tc, con, run_id, sym, "trend_exit")

    # 4) entries
    def buy(sym, sleeve):
        c = s[sleeve]
        ref = float(last[sym])
        qty = math.floor(equity * c["allocation_pct"] / 100 / c["max_positions"] / ref)
        if qty <= 0:
            return
        if c.get("take_profit_pct") or c.get("stop_loss_pct"):
            req = MarketOrderRequest(
                symbol=sym, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=round(ref * (1 + (c.get("take_profit_pct") or 1000) / 100), 2)),
                stop_loss=StopLossRequest(stop_price=round(ref * (1 - (c.get("stop_loss_pct") or 99) / 100), 2)))
        else:
            req = MarketOrderRequest(symbol=sym, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        tc.submit_order(req)
        con.execute("INSERT OR REPLACE INTO swing_positions VALUES (?,?,?,?,?)",
                    (sym, sleeve, ref, now.date().isoformat(), qty))
        con.commit()
        mine[sym] = sleeve
        db.log_event(con, run_id, "entry", sym, {"sleeve": sleeve, "qty": qty, "ref": ref})
        print(f"  BUY {qty} {sym} [{sleeve}] ref {ref:.2f}")

    count = lambda sl: sum(1 for v in mine.values() if v == sl)
    if s["long_term"]["allocation_pct"] > 0 and (month_start or count("long_term") == 0):
        cand = mom[uptrend & (sma50 > sma200)].dropna().sort_values(ascending=False)
        for sym in cand.index:
            if count("long_term") >= s["long_term"]["max_positions"]:
                break
            if sym not in mine and sym not in positions:
                buy(sym, "long_term")
    if s["short_term"]["allocation_pct"] > 0:
        st = s["short_term"]
        cand = rsi[uptrend & (rsi < st["rsi_threshold"])].dropna().sort_values()
        for sym in cand.index:
            if count("short_term") >= st["max_positions"]:
                break
            if sym not in mine and sym not in positions:
                buy(sym, "short_term")

    # 5) attach trailing stops to long-term holdings that have no protective order
    trail = s["long_term"].get("trailing_stop_pct")
    if trail:
        time.sleep(20)
        for p in tc.get_all_positions():
            if mine.get(p.symbol) == "long_term" and not _open_orders(tc, p.symbol):
                tc.submit_order(TrailingStopOrderRequest(symbol=p.symbol, qty=float(p.qty), side=OrderSide.SELL,
                                                         time_in_force=TimeInForce.GTC, trail_percent=trail))
                print(f"  trailing stop {trail}% on {p.symbol}")

    # 6) equity snapshot
    spy = float(C[bench].iloc[-1])
    con.execute("INSERT INTO equity VALUES (?,?,?,?)", (run_id, now.isoformat(), float(tc.get_account().equity), spy))
    con.commit()
    print(f"  equity ${float(tc.get_account().equity):,.2f} | long {count('long_term')} | short {count('short_term')}")
