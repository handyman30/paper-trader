"""Daily-bar backtest: fundamentals-screened universe, long-term + short-term sleeves.

Fill model (conservative):
- Signals on day-t close -> orders fill at day t+1 open + slippage.
- Stop/target checked on daily high/low; if both are touched the same day, assume the stop hit first.
- A gap through a level fills at the open (worse for stops, better for targets).
- Each sleeve has its own cash, so allocations hold. A ticker can only be in one sleeve at a time.
"""
import hashlib
import json
import math
import pickle
from datetime import date

import numpy as np
import pandas as pd

from . import db
from .config import ROOT
from .fundamentals import select_quality

SLEEVES = ("long_term", "short_term")


# ---------------- data ----------------
def load_daily(tickers, start) -> dict[str, pd.DataFrame]:
    stamp = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d-%H")  # refreshes hourly (ET)
    key = hashlib.md5(f"{sorted(tickers)}{start}{stamp}".encode()).hexdigest()[:10]
    path = ROOT / "data" / f"daily_{key}.pkl"
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        return pickle.loads(path.read_bytes())
    import yfinance as yf
    raw = yf.download(list(tickers), start=start, auto_adjust=True, progress=False, threads=True)
    panel = {f: raw[f.capitalize()].copy() for f in ("open", "high", "low", "close")}
    path.write_bytes(pickle.dumps(panel))
    return panel


def rsi_panel(c: pd.DataFrame, n: int) -> pd.DataFrame:
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


# ---------------- stats ----------------
def swing_stats(equity: pd.Series, bench: pd.Series | None, trades: pd.DataFrame, target_m: float) -> dict:
    s = {}
    eq0, eq1 = equity.iloc[0], equity.iloc[-1]
    yrs = (equity.index[-1] - equity.index[0]).days / 365.25
    s["years"] = yrs
    s["total_return_pct"] = (eq1 / eq0 - 1) * 100
    s["cagr_pct"] = ((eq1 / eq0) ** (1 / yrs) - 1) * 100 if yrs > 0 else 0
    s["target_cagr_pct"] = ((1 + target_m / 100) ** 12 - 1) * 100
    dr = equity.pct_change().dropna()
    s["sharpe"] = float(dr.mean() / dr.std() * np.sqrt(252)) if dr.std() > 0 else 0
    s["max_drawdown_pct"] = float((equity / equity.cummax() - 1).min() * 100)

    def monthly(e):
        m = e.resample("ME").last()
        r = m.pct_change()
        r.iloc[0] = m.iloc[0] / e.iloc[0] - 1
        return r * 100

    mr = monthly(equity)
    s["avg_month_pct"] = float(mr.mean())
    s["median_month_pct"] = float(mr.median())
    s["months_hit_target_pct"] = float((mr >= target_m).mean() * 100)
    s["worst_month_pct"] = float(mr.min())
    s["best_month_pct"] = float(mr.max())
    yr = equity.resample("YE").last()
    yrr = yr.pct_change()
    yrr.iloc[0] = yr.iloc[0] / eq0 - 1
    s["yearly_pct"] = {str(k.year): round(v * 100, 2) for k, v in yrr.items()}
    s["monthly_pct"] = {f"{k:%Y-%m}": round(v, 2) for k, v in mr.items()}

    if bench is not None:
        s["benchmark_total_pct"] = (bench.iloc[-1] / bench.iloc[0] - 1) * 100
        s["benchmark_cagr_pct"] = ((bench.iloc[-1] / bench.iloc[0]) ** (1 / yrs) - 1) * 100
        s["benchmark_max_dd_pct"] = float((bench / bench.cummax() - 1).min() * 100)
        bm = monthly(bench)
        s["benchmark_months_hit_target_pct"] = float((bm >= target_m).mean() * 100)
        by = bench.resample("YE").last()
        byr = by.pct_change()
        byr.iloc[0] = by.iloc[0] / bench.iloc[0] - 1
        s["benchmark_yearly_pct"] = {str(k.year): round(v * 100, 2) for k, v in byr.items()}

    s["trades"] = len(trades)
    if len(trades):
        w, l = trades[trades.pnl > 0], trades[trades.pnl <= 0]
        s["win_rate_pct"] = len(w) / len(trades) * 100
        s["avg_win_pct"] = float(w.ret_pct.mean()) if len(w) else 0
        s["avg_loss_pct"] = float(l.ret_pct.mean()) if len(l) else 0
        s["profit_factor"] = float(w.pnl.sum() / -l.pnl.sum()) if l.pnl.sum() < 0 else float("inf")
        s["avg_hold_days"] = float(trades.hold_days.mean())
        s["exit_reasons"] = trades.reason.value_counts().to_dict()
        s["by_sleeve"] = {
            k: {"trades": int(len(g)), "pnl": round(float(g.pnl.sum()), 2),
                "win_rate_pct": round(float((g.pnl > 0).mean() * 100), 1),
                "avg_hold_days": round(float(g.hold_days.mean()), 1)}
            for k, g in trades.groupby("sleeve")}
    return s


def format_swing(s: dict) -> str:
    g = lambda k, f="{:.2f}": "n/a" if s.get(k) is None else f.format(s[k])
    out = [
        f"Period               {s.get('period')}  ({s['years']:.1f} yrs)",
        f"Universe             {s.get('universe_size')} stocks after fundamentals screen",
        f"Total return         {g('total_return_pct', '{:.1f}')}%   (SPY {g('benchmark_total_pct', '{:.1f}')}%)",
        f"CAGR                 {g('cagr_pct')}%   (SPY {g('benchmark_cagr_pct')}%, target {g('target_cagr_pct')}%)",
        f"Avg / median month   {g('avg_month_pct')}% / {g('median_month_pct')}%   (target 1.00%)",
        f"Months >= target     {g('months_hit_target_pct', '{:.0f}')}%   (SPY {g('benchmark_months_hit_target_pct', '{:.0f}')}%)",
        f"Max drawdown         {g('max_drawdown_pct', '{:.1f}')}%   (SPY {g('benchmark_max_dd_pct', '{:.1f}')}%)",
        f"Sharpe               {g('sharpe')}",
        f"Trades               {s['trades']}  win {g('win_rate_pct', '{:.0f}')}%  "
        f"avg win {g('avg_win_pct', '{:.1f}')}% / loss {g('avg_loss_pct', '{:.1f}')}%  PF {g('profit_factor')}",
        f"Exit reasons         {s.get('exit_reasons')}",
    ]
    for k, v in (s.get("by_sleeve") or {}).items():
        out.append(f"  {k:11}        {v['trades']} trades, P&L ${v['pnl']:,.0f}, win {v['win_rate_pct']}%, "
                   f"hold {v['avg_hold_days']}d")
    return "\n".join(out)



# ---------------- costs ----------------
def cost_model(s: dict) -> dict:
    c = s.get("costs") or {}
    return dict(c.get("presets", {}).get(c.get("preset"), {}))


def order_cost(cm: dict, side: str, qty: float, price: float) -> float:
    """Dollar cost of one order (commission + regulatory + per-trade FX). Slippage is separate."""
    if not cm or qty <= 0:
        return 0.0
    v = qty * price
    comm = cm.get("commission_per_order", 0) + cm.get("commission_per_share", 0) * qty + v * cm.get("commission_pct", 0) / 100
    if comm > 0 or cm.get("min_commission", 0) > 0:
        comm = max(comm, cm.get("min_commission", 0))
    if cm.get("max_commission_pct"):
        comm = min(comm, v * cm["max_commission_pct"] / 100)
    fee = comm
    if side == "sell":
        fee += v * cm.get("sec_fee_per_million", 0) / 1e6
        fee += min(qty * cm.get("finra_taf_per_share", 0), cm.get("finra_taf_max", float("inf")))
    if cm.get("fx_mode") == "per_trade":
        fee += v * cm.get("fx_fee_pct", 0) / 100
    return fee


# ---------------- engine ----------------
def simulate(cfg: dict, universe: list[str], px: dict, start: str, close_at_end=True) -> dict:
    """Core day loop. The trading day before `start` is day 0: no fills, but signals are taken at its
    close so the first fills land on `start`'s open (and the long-term sleeve fills immediately)."""
    s = cfg["swing"]
    bench = cfg.get("benchmark", "SPY")
    O, H, L, C = (px[f][universe] for f in ("open", "high", "low", "close"))
    dates = px["close"][bench].dropna().index
    O, H, L, C = (x.reindex(dates) for x in (O, H, L, C))
    Cf = C.ffill()
    sma200, sma50 = C.rolling(200).mean(), C.rolling(50).mean()
    rsi = rsi_panel(C, s["short_term"]["rsi_period"])
    mom = C.shift(21) / C.shift(252) - 1
    first = int(np.searchsorted(dates, pd.Timestamp(start)))
    dates = dates[max(first - 1, 0):]
    O, H, L, C, Cf, sma200, sma50, rsi, mom = (x.loc[dates] for x in (O, H, L, C, Cf, sma200, sma50, rsi, mom))

    slip = s["slippage_bps"] / 1e4
    cm = cost_model(s)
    start_cash = float(s["starting_cash"])
    fx_once = start_cash * cm.get("fx_fee_pct", 0) / 100 if cm.get("fx_mode") == "once" else 0.0
    sl = {k: dict(cfg=s[k], cash=(start_cash - fx_once) * s[k]["allocation_pct"] / 100, pos={})
          for k in SLEEVES if s[k]["allocation_pct"] > 0}
    buys, sells, trades, eq, fills = [], [], [], [], []
    fees_by_day = {dates[0]: fx_once}

    def held(t):
        return any(t in v["pos"] for v in sl.values())

    def close(name, t, price, d, reason):
        v = sl[name]
        p = v["pos"].pop(t)
        fee = order_cost(cm, "sell", p["qty"], price)
        v["cash"] += p["qty"] * price - fee
        fees_by_day[d] = fees_by_day.get(d, 0) + fee
        fees = p["entry_fee"] + fee
        gross = p["qty"] * (price - p["entry"])
        trades.append(dict(sleeve=name, ticker=t, entry_time=str(p["date"].date()), exit_time=str(d.date()),
                           qty=p["qty"], entry_price=p["entry"], exit_price=price, fees=fees,
                           pnl=gross - fees, ret_pct=((gross - fees) / (p["qty"] * p["entry"])) * 100,
                           reason=reason, hold_days=p["days"]))
        fills.append(dict(date=d, side="SELL", ticker=t, sleeve=name, qty=p["qty"], price=price, fee=fee,
                          reason=reason, pnl=gross - fees))

    def sleeve_eq(v, i):
        return v["cash"] + sum(p["qty"] * Cf.iat[i, Cf.columns.get_loc(t)] for t, p in v["pos"].items())

    for i, d in enumerate(dates):
        if i > 0:
            # 1) queued sells, then buys, at the open
            for name, t, reason in sells:
                if t in sl[name]["pos"] and not math.isnan(O.at[d, t]):
                    close(name, t, O.at[d, t] * (1 - slip), d, reason)
            sells = []
            for name, t in buys:
                o = O.at[d, t]
                v, c = sl[name], sl[name]["cfg"]
                if math.isnan(o) or held(t) or len(v["pos"]) >= c["max_positions"]:
                    continue
                price = o * (1 + slip)
                budget = min(sleeve_eq(v, i - 1) / c["max_positions"], v["cash"])
                qty = math.floor(budget / price)
                while qty > 0 and qty * price + order_cost(cm, "buy", qty, price) > v["cash"]:
                    qty -= 1
                if qty <= 0:
                    continue
                fee = order_cost(cm, "buy", qty, price)
                v["cash"] -= qty * price + fee
                fees_by_day[d] = fees_by_day.get(d, 0) + fee
                v["pos"][t] = dict(qty=qty, entry=price, date=d, hw=price, days=0, entry_fee=fee,
                                   tp=price * (1 + c["take_profit_pct"] / 100) if c.get("take_profit_pct") else None,
                                   sl=price * (1 - c["stop_loss_pct"] / 100) if c.get("stop_loss_pct") else None)
                fills.append(dict(date=d, side="BUY", ticker=t, sleeve=name, qty=qty, price=price, fee=fee,
                                  reason="entry", pnl=None))
            buys = []

            # 2) intraday exits
            for name, v in sl.items():
                c = v["cfg"]
                for t in list(v["pos"]):
                    o, h, l, cl = O.at[d, t], H.at[d, t], L.at[d, t], C.at[d, t]
                    if math.isnan(o):
                        continue
                    p = v["pos"][t]
                    p["days"] += 1
                    stop, why = p["sl"], "stop"
                    if c.get("trailing_stop_pct"):
                        ts = p["hw"] * (1 - c["trailing_stop_pct"] / 100)
                        if stop is None or ts > stop:
                            stop, why = ts, "trailing_stop"
                    if stop and o <= stop:
                        close(name, t, o * (1 - slip), d, why)
                    elif stop and l <= stop:
                        close(name, t, stop * (1 - slip), d, why)
                    elif p["tp"] and o >= p["tp"]:
                        close(name, t, o, d, "target")
                    elif p["tp"] and h >= p["tp"]:
                        close(name, t, p["tp"], d, "target")
                    elif c.get("max_hold_days") and p["days"] >= c["max_hold_days"]:
                        close(name, t, cl * (1 - slip), d, "time_stop")
                    else:
                        p["hw"] = max(p["hw"], h)

        eq.append((d, sum(sleeve_eq(v, i) for v in sl.values())))

        # 3) signals at today's close -> next open (skipped on the last available day only in backtests;
        #    in forward mode we keep them as "orders queued for tomorrow")
        uptrend = (C.loc[d] > sma200.loc[d])
        month_end = i + 1 < len(dates) and dates[i + 1].month != d.month
        if "long_term" in sl and (month_end or i == 0):
            v, c = sl["long_term"], sl["long_term"]["cfg"]
            for t in v["pos"]:
                if not uptrend.get(t, False):
                    sells.append(("long_term", t, "trend_exit"))
            free = c["max_positions"] - len(v["pos"]) + sum(1 for x in sells if x[0] == "long_term")
            cand = mom.loc[d][uptrend & (sma50.loc[d] > sma200.loc[d])].dropna().sort_values(ascending=False)
            for t in cand.index:
                if free <= 0:
                    break
                if not held(t):
                    buys.append(("long_term", t))
                    free -= 1
        if "short_term" in sl:
            v, c = sl["short_term"], sl["short_term"]["cfg"]
            free = c["max_positions"] - len(v["pos"])
            cand = rsi.loc[d][uptrend & (rsi.loc[d] < c["rsi_threshold"])].dropna().sort_values()
            queued = {t for _, t in buys}
            for t in cand.index:
                if free <= 0:
                    break
                if not held(t) and t not in queued:
                    buys.append(("short_term", t))
                    free -= 1

    last = dates[-1]
    open_pos = [dict(sleeve=name, ticker=t, qty=p["qty"], entry_date=str(p["date"].date()), entry=p["entry"],
                     price=float(Cf.at[last, t]), unrealized=p["qty"] * (float(Cf.at[last, t]) - p["entry"]),
                     unrealized_pct=(float(Cf.at[last, t]) / p["entry"] - 1) * 100,
                     stop=p["sl"] if not sl[name]["cfg"].get("trailing_stop_pct") else
                     p["hw"] * (1 - sl[name]["cfg"]["trailing_stop_pct"] / 100), target=p["tp"])
                for name, v in sl.items() for t, p in v["pos"].items()]
    if close_at_end:
        for name, v in sl.items():
            for t in list(v["pos"]):
                close(name, t, Cf.at[last, t], last, "open_at_end")

    equity = pd.Series(dict(eq))
    b = px["close"][bench].reindex(equity.index).ffill()
    return dict(equity=equity, bench=b / b.iloc[0] * start_cash, trades=pd.DataFrame(trades),
                fills=pd.DataFrame(fills), fees_by_day=pd.Series(fees_by_day).reindex(equity.index, fill_value=0),
                open_positions=open_pos, queued_buys=buys, queued_sells=sells,
                cash={k: v["cash"] for k, v in sl.items()})


def run_swing(cfg: dict, label="Your config (60% long / 40% short)", verbose=True, save=True) -> dict:
    s = cfg["swing"]
    bench = cfg.get("benchmark", "SPY")
    universe = select_quality(s["universe"], s["top_n_quality"]) if s["fundamental_filter"] else list(s["universe"])
    warmup = (pd.Timestamp(s["start"]) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")  # indicators need ~1yr
    px = load_daily(sorted(set(s["universe"]) | {bench}), warmup)
    r = simulate(cfg, universe, px, s["start"])
    equity, bser, tdf = r["equity"], r["bench"], r["trades"]
    stats = swing_stats(equity, bser, tdf, s["monthly_target_pct"])
    stats.update(label=label, period=f"{equity.index[0]:%Y-%m-%d} -> {equity.index[-1]:%Y-%m-%d}",
                 universe=universe, universe_size=len(universe), cost_preset=(s.get("costs") or {}).get("preset"),
                 total_fees=float(r["fees_by_day"].sum()))

    if save:
        con = db.connect(cfg["db_path"])
        run_id = db.new_run(con, "swing", "yfinance-daily", cfg)
        if len(tdf):
            out = tdf.drop(columns=["hold_days", "fees"]).copy()
            out.insert(0, "run_id", run_id)
            out.to_sql("trades", con, if_exists="append", index=False)
        pd.DataFrame({"run_id": run_id, "ts": equity.index.astype(str), "equity": equity.values,
                      "benchmark": bser.values}).to_sql("equity", con, if_exists="append", index=False)
        con.execute("UPDATE runs SET summary=? WHERE id=?", (json.dumps(stats, default=str), run_id))
        con.commit()
        stats["run_id"] = run_id
    if verbose:
        print(f"\n[{label}]" + (f" run #{stats.get('run_id')}" if save else ""))
        print(format_swing(stats))
        print(f"Costs ({stats['cost_preset']}): ${stats['total_fees']:,.0f} total fees")
    return stats
