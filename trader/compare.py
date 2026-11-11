"""Run several swing variants side by side so you can see what each piece contributes."""
import copy

from .swing import run_swing


def variant(cfg, **changes):
    c = copy.deepcopy(cfg)
    for path, val in changes.items():
        node = c["swing"]
        *parents, leaf = path.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = val
    return c


def compare(cfg):
    runs = {
        "Your config (60% long / 40% short)": cfg,
        "Short-term only (+10%/-5%)": variant(cfg, long_term__allocation_pct=0, short_term__allocation_pct=100,
                                              short_term__max_positions=8),
        "Long-term only": variant(cfg, long_term__allocation_pct=100, short_term__allocation_pct=0,
                                  long_term__max_positions=10),
        "Both sleeves +10%/-5%": variant(cfg, long_term__take_profit_pct=10, long_term__stop_loss_pct=5,
                                         long_term__trailing_stop_pct=None),
        "No fundamentals filter": variant(cfg, fundamental_filter=False),
    }
    res = {k: run_swing(c, label=k, verbose=False) for k, c in runs.items()}
    any_s = next(iter(res.values()))
    hdr = f"{'Variant':38} {'CAGR':>6} {'Total':>7} {'AvgMo':>6} {'Mo>=1%':>6} {'MaxDD':>6} {'Sharpe':>6} {'Trades':>6}"
    print(f"\nPeriod {any_s['period']}  | target 1%/month = {any_s['target_cagr_pct']:.1f}%/yr\n")
    print(hdr)
    print("-" * len(hdr))
    for k, s in res.items():
        print(f"{k:38} {s['cagr_pct']:6.1f} {s['total_return_pct']:7.0f} {s['avg_month_pct']:6.2f} "
              f"{s['months_hit_target_pct']:6.0f} {s['max_drawdown_pct']:6.1f} {s['sharpe']:6.2f} {s['trades']:6d}")
    print(f"{'SPY buy & hold':38} {any_s['benchmark_cagr_pct']:6.1f} {any_s['benchmark_total_pct']:7.0f} "
          f"{'':6} {any_s['benchmark_months_hit_target_pct']:6.0f} {any_s['benchmark_max_dd_pct']:6.1f}")
    print(f"{'Target (1%/month)':38} {any_s['target_cagr_pct']:6.1f}")
    print("\nQuality universe used:", ", ".join(any_s["universe"]))
    return res
