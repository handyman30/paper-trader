"""Bar-replay backtest using the same strategy code as live.

Fill model (deliberately conservative):
- Signal on bar close -> enter at NEXT bar open + slippage.
- TP/SL checked on each later bar's high/low; if both touched in one bar, assume stop hit first.
- Gap through a level fills at the bar open (worse than the level).
- Flatten N minutes before close at bar close - slippage.
"""
import json

import pandas as pd

from . import db
from .data import fetch_bars
from .report import compute_stats, format_stats
from .strategy import add_indicators, entry_signal, in_entry_window


def run_backtest(cfg: dict, verbose=True) -> dict:
    tickers = cfg["tickers"]
    bench = cfg.get("benchmark", "SPY")
    bars, source = fetch_bars(tickers + [bench], cfg["backtest_days"], cfg["timeframe_min"])
    bench_df = bars.pop(bench, None)
    if not bars:
        raise SystemExit("No data returned. Check network / tickers.")

    prepared = {}
    for t, d in bars.items():
        d = add_indicators(d, cfg)
        d["signal"] = entry_signal(d, cfg)
        prepared[t] = d

    timeline = sorted(set().union(*[d.index for d in prepared.values()]))
    slip = cfg["slippage_bps"] / 1e4
    comm = cfg["commission_per_share"]
    tp, sl = cfg["take_profit_pct"] / 100, cfg["stop_loss_pct"] / 100
    flat_min = 16 * 60 - cfg["flatten_min_before_close"]

    cash = float(cfg["starting_cash"])
    positions = {}       # ticker -> dict(qty, entry, tp, sl, time)
    pending = {}         # ticker -> signal time (enter next bar open)
    trades, equity_pts = [], []
    last_px = {}
    day, day_start_eq, halted, entered_today = None, cash, False, set()

    def close_pos(t, px, ts, reason):
        nonlocal cash
        p = positions.pop(t)
        cash += p["qty"] * px - p["qty"] * comm
        pnl = p["qty"] * (px - p["entry"]) - 2 * p["qty"] * comm
        trades.append(dict(ticker=t, entry_time=p["time"], exit_time=ts, qty=p["qty"],
                           entry_price=p["entry"], exit_price=px, pnl=pnl,
                           ret_pct=(px / p["entry"] - 1) * 100, reason=reason))

    for ts in timeline:
        if ts.date() != day:
            day = ts.date()
            day_start_eq = cash + sum(p["qty"] * last_px.get(t, p["entry"]) for t, p in positions.items())
            halted, entered_today = False, set()
            pending.clear()
        mins = ts.hour * 60 + ts.minute

        # 1) fills of pending entries at this bar's open
        for t in list(pending):
            d = prepared[t]
            if ts not in d.index:
                continue
            pending.pop(t)
            if halted or t in positions or len(positions) >= cfg["max_open_positions"]:
                continue
            o = d.at[ts, "open"] * (1 + slip)
            eq = cash + sum(p["qty"] * last_px.get(x, p["entry"]) for x, p in positions.items())
            qty = int(eq * cfg["position_size_pct"] / 100 // o)
            if qty <= 0 or qty * o > cash:
                continue
            cash -= qty * o + qty * comm
            positions[t] = dict(qty=qty, entry=o, tp=o * (1 + tp), sl=o * (1 - sl), time=ts)
            entered_today.add(t)

        # 2) exits on this bar
        for t in list(positions):
            d = prepared[t]
            if ts not in d.index:
                continue
            p, row = positions[t], d.loc[ts]
            if row["low"] <= p["sl"]:
                close_pos(t, min(row["open"], p["sl"]) * (1 - slip), ts, "stop")
            elif row["high"] >= p["tp"]:
                close_pos(t, max(row["open"], p["tp"]), ts, "target")   # limit fill, no slippage
            elif cfg["flatten_at_close"] and mins >= flat_min:
                close_pos(t, row["close"] * (1 - slip), ts, "eod")

        for t, d in prepared.items():
            if ts in d.index:
                last_px[t] = d.at[ts, "close"]
        eq = cash + sum(p["qty"] * last_px[t] for t, p in positions.items())

        # 3) daily loss limit
        if not halted and eq <= day_start_eq * (1 - cfg["max_daily_loss_pct"] / 100):
            halted = True
            for t in list(positions):
                close_pos(t, last_px[t] * (1 - slip), ts, "daily_loss_limit")
            eq = cash

        # 4) new signals (fill next bar)
        if not halted and in_entry_window(ts, cfg):
            for t, d in prepared.items():
                if ts in d.index and d.at[ts, "signal"] and t not in positions and t not in pending:
                    if cfg["one_entry_per_ticker_per_day"] and t in entered_today:
                        continue
                    if len(positions) + len(pending) < cfg["max_open_positions"]:
                        pending[t] = ts

        equity_pts.append((ts, eq))

    for t in list(positions):  # close anything left
        close_pos(t, last_px[t], timeline[-1], "end_of_test")

    equity = pd.Series(dict(equity_pts)).sort_index()
    tdf = pd.DataFrame(trades)
    bser = None
    if bench_df is not None and len(bench_df):
        b = bench_df["close"].reindex(equity.index, method="ffill").bfill()
        bser = b / b.iloc[0] * cfg["starting_cash"]
    stats = compute_stats(tdf if len(tdf) else pd.DataFrame(columns=["pnl", "ret_pct", "reason"]), equity, bser)
    stats["source"] = source
    stats["period"] = f"{equity.index[0]:%Y-%m-%d} -> {equity.index[-1]:%Y-%m-%d}"

    con = db.connect(cfg["db_path"])
    run_id = db.new_run(con, "backtest", source, cfg)
    if len(tdf):
        out = tdf.copy()
        out["entry_time"] = out.entry_time.astype(str)
        out["exit_time"] = out.exit_time.astype(str)
        out.insert(0, "run_id", run_id)
        out.to_sql("trades", con, if_exists="append", index=False)
    eqdf = pd.DataFrame({"run_id": run_id, "ts": equity.index.astype(str), "equity": equity.values,
                         "benchmark": bser.values if bser is not None else None})
    eqdf.to_sql("equity", con, if_exists="append", index=False)
    con.execute("UPDATE runs SET summary=? WHERE id=?", (json.dumps(stats, default=str), run_id))
    con.commit()

    if verbose:
        print(f"\nBacktest run #{run_id} | data: {source} | {stats['period']} | "
              f"rule: {cfg['entry_rule']} | {cfg['timeframe_min']}m bars | {len(prepared)} tickers")
        print(format_stats(stats, cfg.get("benchmark", "SPY")))
    stats["run_id"] = run_id
    return stats
