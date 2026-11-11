"""Live paper loop against Alpaca's paper endpoint. Bracket orders = server-side TP/SL."""
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import db
from .config import alpaca_keys, assert_paper_only, has_keys
from .data import fetch_alpaca
from .strategy import add_indicators, entry_signal, in_entry_window

NY = "America/New_York"


def run_live(cfg: dict):
    assert_paper_only()
    if not has_keys():
        raise SystemExit("Add ALPACA_API_KEY / ALPACA_SECRET_KEY (paper) to .env first. See .env.example.")

    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
    from alpaca.trading.requests import (GetOrdersRequest, LimitOrderRequest, StopLossRequest,
                                         TakeProfitRequest)

    key, sec = alpaca_keys()
    tc = TradingClient(key, sec, paper=True)
    con = db.connect(cfg["db_path"])
    run_id = db.new_run(con, "live", "alpaca-paper", cfg)
    acct = tc.get_account()
    print(f"Live paper run #{run_id} | equity ${float(acct.equity):,.0f} | Ctrl+C to stop")

    entered_today, halted, cur_day, day_start_eq = set(), False, None, float(acct.equity)
    bench_start = None

    while True:
        try:
            clock = tc.get_clock()
            if not clock.is_open:
                wait = (clock.next_open - clock.timestamp).total_seconds()
                print(f"Market closed. Next open {clock.next_open:%Y-%m-%d %H:%M %Z}; sleeping.")
                time.sleep(min(max(wait, 30), 1800))
                continue

            now = pd.Timestamp.now(tz=NY)
            if now.date() != cur_day:
                cur_day, entered_today, halted = now.date(), set(), False
                day_start_eq = float(tc.get_account().last_equity)

            acct = tc.get_account()
            equity = float(acct.equity)
            positions = {p.symbol: p for p in tc.get_all_positions()}
            open_orders = tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))

            # cancel stale unfilled entry orders
            for o in open_orders:
                if o.side == OrderSide.BUY and o.filled_at is None and \
                        datetime.now(timezone.utc) - o.submitted_at > timedelta(minutes=cfg["stale_order_min"]):
                    tc.cancel_order_by_id(o.id)
                    db.log_event(con, run_id, "cancel_stale", o.symbol)

            mins_to_close = (clock.next_close - clock.timestamp).total_seconds() / 60
            # daily loss limit / flatten at close
            if not halted and equity <= day_start_eq * (1 - cfg["max_daily_loss_pct"] / 100):
                halted = True
                tc.close_all_positions(cancel_orders=True)
                db.log_event(con, run_id, "daily_loss_halt", detail={"equity": equity})
                print("Daily loss limit hit: flattened, no more entries today.")
            if cfg["flatten_at_close"] and mins_to_close <= cfg["flatten_min_before_close"] and positions:
                tc.close_all_positions(cancel_orders=True)
                db.log_event(con, run_id, "flatten_eod")

            # signals
            pending_syms = {o.symbol for o in open_orders if o.side == OrderSide.BUY}
            bars = fetch_alpaca(cfg["tickers"] + [cfg["benchmark"]], 3, cfg["timeframe_min"],
                                end=datetime.now(timezone.utc))
            b = bars.get(cfg["benchmark"])
            if b is not None and len(b):
                bench_start = bench_start or float(b["close"].iloc[-1])
                bench_val = float(b["close"].iloc[-1]) / bench_start
            else:
                bench_val = None
            con.execute("INSERT INTO equity VALUES (?,?,?,?)",
                        (run_id, now.isoformat(), equity, bench_val))
            con.commit()

            if not halted and in_entry_window(now, cfg):
                slots = cfg["max_open_positions"] - len(positions) - len(pending_syms)
                for t in cfg["tickers"]:
                    if slots <= 0:
                        break
                    if t in positions or t in pending_syms:
                        continue
                    if cfg["one_entry_per_ticker_per_day"] and t in entered_today:
                        continue
                    d = bars.get(t)
                    if d is None or len(d) < 20:
                        continue
                    d = add_indicators(d, cfg)
                    if not bool(entry_signal(d, cfg).iloc[-1]):
                        continue
                    px = float(d["close"].iloc[-1])
                    qty = int(equity * cfg["position_size_pct"] / 100 // px)
                    if qty <= 0:
                        continue
                    req = LimitOrderRequest(
                        symbol=t, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                        limit_price=round(px, 2), order_class=OrderClass.BRACKET,
                        take_profit=TakeProfitRequest(limit_price=round(px * (1 + cfg["take_profit_pct"] / 100), 2)),
                        stop_loss=StopLossRequest(stop_price=round(px * (1 - cfg["stop_loss_pct"] / 100), 2)),
                    )
                    order = tc.submit_order(req)
                    entered_today.add(t)
                    slots -= 1
                    db.log_event(con, run_id, "entry_order", t,
                                 {"qty": qty, "limit": px, "rule": cfg["entry_rule"], "order_id": str(order.id)})
                    print(f"{now:%H:%M} BUY {qty} {t} @ {px:.2f} (bracket)")

            print(f"{now:%H:%M} equity ${equity:,.2f} | positions {len(positions)} | "
                  f"{'HALTED' if halted else 'active'}")
        except KeyboardInterrupt:
            print("Stopped.")
            return
        except Exception as e:  # network blips, API hiccups: log and keep going
            db.log_event(con, run_id, "error", detail=str(e))
            print(f"Error (will retry): {e}")
        time.sleep(60)
