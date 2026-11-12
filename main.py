"""CLI: python main.py backtest | live | sweep | report | swing | swing-compare | swing-live"""
import argparse
import itertools
import json

from trader import db
from trader.config import load_config
from trader.report import format_stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["backtest", "live", "sweep", "report", "swing", "swing-compare", "swing-live", "forward"])
    ap.add_argument("action", nargs="?", default="update", help="forward: start | update")
    ap.add_argument("--name", default="yours", choices=["yours", "claude"], help="which forward test")
    ap.add_argument("--restart", action="store_true", help="forward start: discard the current forward test")
    ap.add_argument("--set", nargs="*", default=[], help="override config, e.g. --set entry_rule=rsi dip_pct=1.5")
    args = ap.parse_args()

    cfg = load_config()
    for kv in args.set:
        k, v = kv.split("=", 1)
        cfg[k] = json.loads(v) if v[:1] in "0123456789-[{tf" and v not in ("dip", "rsi") else v

    if args.mode == "backtest":
        from trader.backtest import run_backtest
        run_backtest(cfg)
    elif args.mode == "live":
        from trader.live import run_live
        run_live(cfg)
    elif args.mode == "sweep":
        from trader.backtest import run_backtest
        rows = []
        grid = {"entry_rule": ["dip", "rsi"], "take_profit_pct": [1.0, 1.5, 2.5], "stop_loss_pct": [0.75, 1.0, 1.5]}
        for combo in itertools.product(*grid.values()):
            c = {**cfg, **dict(zip(grid, combo))}
            s = run_backtest(c, verbose=False)
            rows.append((combo, s))
        rows.sort(key=lambda r: r[1]["total_return_pct"], reverse=True)
        print(f"{'rule':5} {'TP':>5} {'SL':>5} {'ret%':>7} {'win%':>6} {'PF':>5} {'DD%':>6} {'trades':>6}")
        for (rule, tp, sl), s in rows:
            print(f"{rule:5} {tp:5.2f} {sl:5.2f} {s['total_return_pct']:7.2f} {s.get('win_rate_pct', 0):6.1f} "
                  f"{s.get('profit_factor', 0):5.2f} {s['max_drawdown_pct']:6.2f} {s['trades']:6d}")
        print(f"SPY buy & hold over same period: {rows[0][1].get('benchmark_return_pct', float('nan')):.2f}%")
        print("Caution: picking the best row of a sweep overfits. Re-test it on a different period.")
    elif args.mode == "swing":
        from trader.swing import run_swing
        run_swing(cfg)
    elif args.mode == "swing-compare":
        from trader.compare import compare
        compare(cfg)
    elif args.mode == "swing-live":
        from trader.swing_live import run_swing_live
        run_swing_live(cfg)
    elif args.mode == "forward":
        from trader import forward
        if args.action == "start":
            forward.start(cfg, restart=args.restart, name=args.name)
            forward.update(args.name)
        else:
            for n in forward.TESTS:
                forward.update(n)
    elif args.mode == "report":
        con = db.connect(cfg["db_path"])
        for rid, mode, started, source, summary in con.execute(
                "SELECT id, mode, started, source, summary FROM runs ORDER BY id DESC LIMIT 5"):
            print(f"\n#{rid} {mode} {started[:16]} ({source})")
            if summary:
                print(format_stats(json.loads(summary)))


if __name__ == "__main__":
    main()
