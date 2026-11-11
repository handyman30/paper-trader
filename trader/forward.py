"""Forward test: the strategy trading simulated money on REAL prices from a fixed start date.

No server and no API keys needed. Every `update` rebuilds the whole test from the start date with fresh
market data (deterministic), so it doesn't matter if your computer was off for a few days.
Universe and config are frozen at start, so editing config.yaml doesn't rewrite history;
use `forward start --restart` to begin a new test with new settings.
"""
import copy
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from .config import ROOT
from .fundamentals import select_quality
from .swing import load_daily, simulate

STATE = ROOT / "data" / "forward_state.json"
JOURNAL = ROOT / "data" / "forward_journal.json"
NOTES = ROOT / "data" / "forward_notes.json"
NY = ZoneInfo("America/New_York")
CHECKPOINTS = [1, 5, 10, 20, 60]


def local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.path.realpath("/etc/localtime").split("zoneinfo/")[1])
    except Exception:
        return ZoneInfo("UTC")


def _next_session(now_et: pd.Timestamp) -> pd.Timestamp:
    d = now_et.normalize().tz_localize(None)
    if now_et.hour * 60 + now_et.minute >= 9 * 60 + 30 or d.weekday() >= 5:  # today's open already passed
        d += pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d += pd.Timedelta(days=1)
    return d


def start(cfg: dict, restart=False) -> dict:
    if STATE.exists() and not restart:
        st = json.loads(STATE.read_text())
        print(f"Forward test already running since {st['started_local']}. Use --restart to start over.")
        return st
    s = cfg["swing"]
    now_local = datetime.now(local_tz())
    now_et = pd.Timestamp.now(tz=NY)
    universe = select_quality(s["universe"], s["top_n_quality"]) if s["fundamental_filter"] else list(s["universe"])
    st = dict(
        started_utc=now_local.astimezone(ZoneInfo("UTC")).isoformat(),
        started_local=now_local.strftime("%a %d %b %Y, %I:%M %p %Z"),
        started_et=now_et.strftime("%a %d %b %Y, %I:%M %p ET"),
        local_tz=str(local_tz()),
        first_session=str(_next_session(now_et).date()),
        universe=universe,
        config=copy.deepcopy(cfg),
    )
    st["config"]["swing"]["start"] = st["first_session"]
    st["config"]["db_path"] = os.path.basename(st["config"].get("db_path", "trader.db"))
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1, default=str))
    JOURNAL.unlink(missing_ok=True)
    print(f"Forward test started {st['started_local']} ({st['started_et']}).")
    print(f"First simulated trades at the US open on {st['first_session']}. Universe: {', '.join(universe)}")
    return st


def _complete_bars(px: dict) -> dict:
    """Drop today's bar if the US session hasn't closed yet (avoid using a partial day)."""
    now = pd.Timestamp.now(tz=NY)
    if now.hour * 60 + now.minute < 16 * 60 + 15:
        today = now.normalize().tz_localize(None)
        px = {k: v[v.index < today] for k, v in px.items()}
    return px


def update(verbose=True) -> dict | None:
    if not STATE.exists():
        return None
    st = json.loads(STATE.read_text())
    cfg = st["config"]
    s = cfg["swing"]
    bench = cfg.get("benchmark", "SPY")
    warmup = (pd.Timestamp(st["first_session"]) - pd.Timedelta(days=420)).strftime("%Y-%m-%d")
    px = _complete_bars(load_daily(sorted(set(st["universe"]) | {bench}), warmup))
    r = simulate(cfg, st["universe"], px, st["first_session"], close_at_end=False)
    eq, b, fees = r["equity"], r["bench"], r["fees_by_day"]
    start_cash = float(s["starting_cash"])
    target_m = s["monthly_target_pct"]
    fills = r["fills"]

    days = []
    for n, d in enumerate(eq.index):
        if n == 0:
            continue  # day 0 = baseline (close before the first session)
        f = fills[fills.date == d] if len(fills) else fills
        months = (d - eq.index[0]).days / 30.4375
        goal = start_cash * (1 + target_m / 100) ** months
        e, prev = float(eq.iloc[n]), float(eq.iloc[n - 1])
        days.append(dict(
            date=str(d.date()), day=n, equity=e, day_pnl=e - prev, day_pct=(e / prev - 1) * 100,
            cum_pct=(e / start_cash - 1) * 100,
            spy_day_pct=(b.iloc[n] / b.iloc[n - 1] - 1) * 100, spy_cum_pct=(b.iloc[n] / start_cash - 1) * 100,
            goal_equity=goal, vs_goal=e - goal, fees=float(fees.iloc[n]), fees_cum=float(fees.iloc[: n + 1].sum()),
            drawdown_pct=(e / float(eq.iloc[: n + 1].max()) - 1) * 100,
            buys=[f"{x.ticker} x{int(x.qty)} @ ${x.price:,.2f} ({x.sleeve.replace('_', '-')})"
                  for x in f.itertuples() if x.side == "BUY"],
            sells=[f"{x.ticker} @ ${x.price:,.2f} {x.reason.replace('_', ' ')} ({'+' if x.pnl >= 0 else '-'}${abs(x.pnl):,.0f})"
                   for x in f.itertuples() if x.side == "SELL"],
        ))
    for j in days:
        j["note"] = _auto_note(j, start_cash)

    out = dict(state={k: v for k, v in st.items() if k != "config"}, start_cash=start_cash, journal=days,
               baseline_date=str(eq.index[0].date()),
               open_positions=r["open_positions"], cash=r["cash"],
               queued=[f"BUY {t} ({sl.replace('_', '-')})" for sl, t in r["queued_buys"]] +
                      [f"SELL {t} ({why.replace('_', ' ')})" for _, t, why in r["queued_sells"]],
               closed_trades=r["trades"].to_dict("records") if len(r["trades"]) else [],
               equity=[(str(k.date()), float(v)) for k, v in eq.items()],
               bench=[(str(k.date()), float(v)) for k, v in b.items()],
               bench_close=float(px["close"][bench].dropna().iloc[-1]),
               fx_fee=float(fees.iloc[0]), cost_preset=s["costs"]["preset"], updated_utc=datetime.now(ZoneInfo("UTC")).isoformat())
    out["readiness"] = readiness(out, cfg)
    JOURNAL.write_text(json.dumps(out, indent=1, default=str))
    if verbose:
        print_summary(out)
    return out


def _auto_note(j, start_cash) -> str:
    d = pd.Timestamp(j["date"])
    head = (f"{d:%a %d %b} · Day {j['day']}: ${j['equity']:,.0f} ({j['day_pct']:+.2f}% today, "
            f"{j['cum_pct']:+.2f}% since start, after fees). SPY {j['spy_day_pct']:+.2f}% today, "
            f"{j['spy_cum_pct']:+.2f}% since start.")
    acts = []
    if j["buys"]:
        acts.append("Bought " + "; ".join(j["buys"]))
    if j["sells"]:
        acts.append("Sold " + "; ".join(j["sells"]))
    if not acts:
        acts.append("No trades, holding.")
    pace = "ahead of" if j["vs_goal"] >= 0 else "behind"
    tail = f"{pace} the 1%/month pace by ${abs(j['vs_goal']):,.0f}. Fees today ${j['fees']:,.2f} (total ${j['fees_cum']:,.2f})."
    return f"{head} {'. '.join(acts)}. {tail[0].upper() + tail[1:]}".replace("..", ".")


def _metrics(out: dict, jj: list) -> dict:
    last = jj[-1]
    trades = [t for t in out["closed_trades"] if t["exit_time"] <= last["date"]]
    return dict(
        maxdd=min(x["drawdown_pct"] for x in jj), cum=last["cum_pct"], spy=last["spy_cum_pct"],
        goal=(last["goal_equity"] / out["start_cash"] - 1) * 100, trades=len(trades),
        win=(sum(t["pnl"] > 0 for t in trades) / len(trades) * 100) if trades else None,
        trade_fees=last["fees_cum"] - out.get("fx_fee", 0), date=last["date"])


def readiness(out: dict, cfg: dict) -> list[dict]:
    """Checkpoints on the way to real money, each judged on the data AS OF that trading day."""
    j = out["journal"]
    cap = out["start_cash"]

    def c(label, ok, detail):
        return dict(label=label, status="pass" if ok else "fail", detail=detail)

    plan = {
        1: ("System check", "Did it trade on real prices and charge fees?", lambda m: [
            c("Simulated orders filled at real opening prices", True, f"session {m['date']} processed"),
            c("Fees charged on every order", m["trade_fees"] > 0 or m["trades"] == 0,
              f"${m['trade_fees']:,.2f} trading fees + ${out.get('fx_fee', 0):,.0f} one-time FX"),
        ]),
        5: ("Behaviour check", "Following the rules without blowing up?", lambda m: [
            c("Drawdown under 8%", m["maxdd"] > -8, f"worst {m['maxdd']:.1f}%"),
            c("Trading fees under 0.5% of capital", m["trade_fees"] < cap * 0.005, f"${m['trade_fees']:,.0f}"),
        ]),
        10: ("Early performance", "Not falling badly behind the market?", lambda m: [
            c("Within 3 pts of SPY", m["cum"] >= m["spy"] - 3, f"{m['cum']:+.2f}% vs SPY {m['spy']:+.2f}%"),
            c("Drawdown under 10%", m["maxdd"] > -10, f"worst {m['maxdd']:.1f}%"),
        ]),
        20: ("First month", "On pace for 1%/month, or ahead of SPY?", lambda m: [
            c("At goal pace OR ahead of SPY", m["cum"] >= m["goal"] or m["cum"] >= m["spy"],
              f"{m['cum']:+.2f}% vs goal {m['goal']:+.2f}% / SPY {m['spy']:+.2f}%"),
            c("Drawdown under 12%", m["maxdd"] > -12, f"worst {m['maxdd']:.1f}%"),
            c("At least 5 closed trades", m["trades"] >= 5, f"{m['trades']} closed"),
        ]),
        60: ("Real-money gate", "About 3 months in: enough evidence to try a SMALL real amount?", lambda m: [
            c("At goal pace OR ahead of SPY", m["cum"] >= m["goal"] or m["cum"] >= m["spy"],
              f"{m['cum']:+.2f}% vs goal {m['goal']:+.2f}% / SPY {m['spy']:+.2f}%"),
            c("Drawdown no worse than 15%", m["maxdd"] > -15, f"worst {m['maxdd']:.1f}%"),
            c("At least 20 closed trades", m["trades"] >= 20, f"{m['trades']} closed"),
            c("Win rate within 10 pts of backtest (46%)", m["win"] is not None and abs(m["win"] - 46) <= 10,
              f"{m['win']:.0f}%" if m["win"] is not None else "no closed trades yet"),
        ]),
    }
    res = []
    for day in CHECKPOINTS:
        title, q, fn = plan[day]
        if len(j) >= day:
            checks = fn(_metrics(out, j[:day]))
            status = "pass" if all(x["status"] == "pass" for x in checks) else "fail"
            when = j[day - 1]["date"]
        else:
            checks, status, when = [], "pending", None
        res.append(dict(day=day, title=title, question=q, status=status, checks=checks, date=when))
    return res


def print_summary(out):
    st = out["state"]
    print(f"\nForward test · started {st['started_local']} ({st['started_et']})")
    print(f"Baseline: {out['baseline_date']} close, ${out['start_cash']:,.0f} · costs: {out['cost_preset']}")
    if not out["journal"]:
        print(f"Waiting for the first US session ({st['first_session']}) to close.")
    for j in out["journal"]:
        print(" ", j["note"])
    if out["queued"]:
        print("Orders queued for next open:", ", ".join(out["queued"]))
    for r in out["readiness"]:
        mark = {"pass": "PASS", "fail": "FAIL", "pending": "...."}[r["status"]]
        print(f"  [{mark}] Day {r['day']:>2} {r['title']}")


def live_marks(out: dict) -> dict | None:
    """Latest (~15 min delayed) prices for open positions + SPY during the US session."""
    now = pd.Timestamp.now(tz=NY)
    mins = now.hour * 60 + now.minute
    if now.weekday() >= 5 or not (9 * 60 + 30 <= mins <= 16 * 60 + 15) or not out["open_positions"]:
        return None
    import yfinance as yf
    tick = sorted({p["ticker"] for p in out["open_positions"]} | {"SPY"})
    try:
        raw = yf.download(tick, period="1d", interval="5m", progress=False, auto_adjust=True)["Close"]
    except Exception:
        return None
    if raw.empty:
        return None
    last = raw.ffill().iloc[-1]
    ts = raw.index[-1].tz_convert(NY)
    pos_val = sum(p["qty"] * float(last.get(p["ticker"], p["price"])) for p in out["open_positions"])
    equity = sum(out["cash"].values()) + pos_val
    prev_close = out["equity"][-1][1]
    return dict(as_of=ts.strftime("%I:%M %p ET"), equity=equity, today_pct=(equity / prev_close - 1) * 100,
                prices={k: float(v) for k, v in last.items()},
                spy_today_pct=(float(last["SPY"]) / out["bench_close"] - 1) * 100 if out.get("bench_close") else None)


def load_journal() -> dict | None:
    return json.loads(JOURNAL.read_text()) if JOURNAL.exists() else None


def load_notes() -> dict:
    return json.loads(NOTES.read_text()) if NOTES.exists() else {}


def save_note(date: str, text: str):
    notes = load_notes()
    if text.strip():
        notes[date] = text.strip()
    else:
        notes.pop(date, None)
    NOTES.parent.mkdir(exist_ok=True)
    NOTES.write_text(json.dumps(notes, indent=1))
