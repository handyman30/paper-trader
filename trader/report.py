import numpy as np
import pandas as pd


def compute_stats(trades: pd.DataFrame, equity: pd.Series, bench: pd.Series | None) -> dict:
    s = {}
    eq0, eq1 = equity.iloc[0], equity.iloc[-1]
    s["total_return_pct"] = (eq1 / eq0 - 1) * 100
    daily = equity.groupby(equity.index.date).last()
    dr = daily.pct_change().dropna()
    s["sharpe"] = float(dr.mean() / dr.std() * np.sqrt(252)) if len(dr) > 1 and dr.std() > 0 else 0.0
    s["max_drawdown_pct"] = float(((equity / equity.cummax()) - 1).min() * 100)
    n_days = max(len(daily), 1)
    s["days"] = n_days
    s["trades"] = len(trades)
    s["trades_per_day"] = len(trades) / n_days
    if len(trades):
        w, l = trades[trades.pnl > 0], trades[trades.pnl <= 0]
        s["win_rate_pct"] = len(w) / len(trades) * 100
        s["avg_win_pct"] = w.ret_pct.mean() if len(w) else 0.0
        s["avg_loss_pct"] = l.ret_pct.mean() if len(l) else 0.0
        s["profit_factor"] = float(w.pnl.sum() / -l.pnl.sum()) if l.pnl.sum() < 0 else float("inf")
        s["exit_reasons"] = trades.reason.value_counts().to_dict()
    if bench is not None and len(bench):
        s["benchmark_return_pct"] = (bench.iloc[-1] / bench.iloc[0] - 1) * 100
        s["excess_vs_benchmark_pct"] = s["total_return_pct"] - s["benchmark_return_pct"]
    return s


def format_stats(s: dict, bench_name="SPY") -> str:
    def f(k, fmt="{:.2f}"):
        v = s.get(k)
        return "n/a" if v is None else fmt.format(v)

    lines = [
        f"Days tested          {s['days']}",
        f"Total return         {f('total_return_pct')}%",
        f"{bench_name} buy & hold      {f('benchmark_return_pct')}%",
        f"Excess vs {bench_name}       {f('excess_vs_benchmark_pct')}%",
        f"Trades               {s['trades']}  ({f('trades_per_day')}/day)",
        f"Win rate             {f('win_rate_pct', '{:.1f}')}%",
        f"Avg win / avg loss   {f('avg_win_pct')}% / {f('avg_loss_pct')}%",
        f"Profit factor        {f('profit_factor')}",
        f"Max drawdown         {f('max_drawdown_pct')}%",
        f"Sharpe (daily, ann.) {f('sharpe')}",
        f"Exit reasons         {s.get('exit_reasons', {})}",
    ]
    return "\n".join(lines)
