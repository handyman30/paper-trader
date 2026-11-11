"""streamlit run dashboard.py"""
import json
import sqlite3

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from trader.config import alpaca_keys, has_keys, load_config

# reference palette: series-1 blue, series-2 orange; target = muted ink, dashed; diverging blue<->red, gray mid
STRAT, BENCH, TARGET = "#2a78d6", "#eb6834", "#898781"
DIVERGING = [[0, "#c03535"], [0.5, "#f0efec"], [1, "#1c5cab"]]

cfg = load_config()
st.set_page_config(page_title="Paper Trader", layout="wide")
con = sqlite3.connect(cfg["db_path"])
try:
    runs = pd.read_sql("SELECT * FROM runs WHERE summary IS NOT NULL OR mode LIKE '%live' ORDER BY id DESC", con)
except Exception:
    runs = pd.DataFrame()


def label(r):
    s = json.loads(r.summary) if r.summary else {}
    name = s.get("label") or r.mode
    return f"#{r.id} · {name} · {r.started[:10]}"


def layout(fig, h=380, **kw):
    kw = {"hovermode": "x unified", "legend": dict(orientation="h", y=1.08, x=0), **kw}
    fig.update_layout(height=h, margin=dict(l=10, r=10, t=30, b=10), **kw)
    fig.update_xaxes(showgrid=False)
    return fig


st.sidebar.title("Paper Trader")
if runs.empty:
    runs = pd.DataFrame(columns=["id", "mode", "started", "source", "config", "summary"])
kinds = {"Swing backtests": runs[runs["mode"] == "swing"], "Live paper": runs[runs["mode"].str.endswith("live")],
         "Intraday backtests": runs[runs["mode"] == "backtest"]}
kind = st.sidebar.radio("View", ["Forward test (live)"] + [k for k, v in kinds.items() if len(v)])
if kind == "Forward test (live)":
    import forward_page
    forward_page.render()
    st.stop()
sel = kinds[kind]
rid = st.sidebar.selectbox("Run", sel.id, format_func=lambda i: label(sel[sel.id == i].iloc[0]))
run = sel[sel.id == rid].iloc[0]
s = json.loads(run.summary) if run.summary else {}
target_m = cfg["swing"]["monthly_target_pct"]
target_y = ((1 + target_m / 100) ** 12 - 1) * 100

eq = pd.read_sql("SELECT ts, equity, benchmark FROM equity WHERE run_id=? ORDER BY ts", con, params=(int(rid),))
eq["ts"] = pd.to_datetime(eq.ts, utc=True, format="mixed").dt.tz_convert("America/New_York").dt.tz_localize(None)
eq = eq.drop_duplicates("ts").set_index("ts")

st.title(s.get("label", run["mode"]))
st.caption(f"{s.get('period', '')} · run #{rid} · goal: {target_m:.0f}%/month ≈ {target_y:.1f}%/year")

# ---------------- KPI tiles ----------------
if run["mode"] == "swing" and s:
    c = st.columns(6)
    c[0].metric("CAGR", f"{s['cagr_pct']:.1f}%", f"{s['cagr_pct'] - target_y:+.1f} pts vs {target_y:.1f}% goal")
    c[1].metric("SPY CAGR", f"{s['benchmark_cagr_pct']:.1f}%",
                f"{s['cagr_pct'] - s['benchmark_cagr_pct']:+.1f} pts strategy edge", delta_color="off")
    c[2].metric("Avg month", f"{s['avg_month_pct']:.2f}%", f"{s['avg_month_pct'] - target_m:+.2f} vs 1% goal")
    c[3].metric("Months ≥ 1%", f"{s['months_hit_target_pct']:.0f}%",
                f"SPY {s['benchmark_months_hit_target_pct']:.0f}%", delta_color="off")
    c[4].metric("Max drawdown", f"{s['max_drawdown_pct']:.1f}%", f"SPY {s['benchmark_max_dd_pct']:.1f}%",
                delta_color="off")
    c[5].metric("Win rate", f"{s.get('win_rate_pct', 0):.0f}%",
                f"{s['trades']} trades · PF {s.get('profit_factor', 0):.2f}", delta_color="off")
elif s:
    c = st.columns(5)
    c[0].metric("Return", f"{s['total_return_pct']:.2f}%")
    c[1].metric("SPY", f"{s.get('benchmark_return_pct', float('nan')):.2f}%")
    c[2].metric("Win rate", f"{s.get('win_rate_pct', 0):.1f}%")
    c[3].metric("Max DD", f"{s['max_drawdown_pct']:.2f}%")
    c[4].metric("Trades", f"{s['trades']}")

# ---------------- equity vs SPY vs goal ----------------
if len(eq):
    base = eq.equity.iloc[0]
    b = eq.benchmark.ffill()
    bench_norm = b / b.dropna().iloc[0] * base if b.notna().any() else None
    days = (eq.index - eq.index[0]).days / 30.4375
    goal = base * (1 + target_m / 100) ** days
    log = st.toggle("Log scale", value=run["mode"] == "swing")
    fig = go.Figure()
    fig.add_scatter(x=eq.index, y=goal, name=f"Goal {target_m:.0f}%/mo", line=dict(color=TARGET, width=2, dash="dash"),
                    hovertemplate="$%{y:,.0f}")
    if bench_norm is not None:
        fig.add_scatter(x=eq.index, y=bench_norm, name="SPY buy & hold", line=dict(color=BENCH, width=2),
                        hovertemplate="$%{y:,.0f}")
    fig.add_scatter(x=eq.index, y=eq.equity, name="Strategy", line=dict(color=STRAT, width=2),
                    hovertemplate="$%{y:,.0f}")
    st.subheader("Growth of $" + f"{base:,.0f}")
    st.plotly_chart(layout(fig, 420, yaxis=dict(type="log" if log else "linear", tickprefix="$", tickformat=",.0f")),
                    width="stretch")

    dd = go.Figure()
    if bench_norm is not None:
        dd.add_scatter(x=eq.index, y=(bench_norm / bench_norm.cummax() - 1) * 100, name="SPY",
                       line=dict(color=BENCH, width=2), hovertemplate="%{y:.1f}%")
    dd.add_scatter(x=eq.index, y=(eq.equity / eq.equity.cummax() - 1) * 100, name="Strategy",
                   line=dict(color=STRAT, width=2), hovertemplate="%{y:.1f}%")
    st.subheader("Drawdown from peak")
    st.plotly_chart(layout(dd, 240, yaxis=dict(ticksuffix="%")), width="stretch")

# ---------------- monthly & yearly ----------------
if run["mode"] == "swing" and s.get("monthly_pct"):
    left, right = st.columns([3, 2])
    m = pd.Series(s["monthly_pct"])
    grid = pd.DataFrame({"y": m.index.str[:4], "m": m.index.str[5:].astype(int), "v": m.values}) \
        .pivot(index="y", columns="m", values="v").reindex(columns=range(1, 13))
    lim = float(np.nanpercentile(np.abs(grid.values), 95)) or 1
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    text = grid.map(lambda v: "" if pd.isna(v) else f"{v:+.1f}{'✓' if v >= target_m else ''}")
    hm = go.Figure(go.Heatmap(z=grid.values, x=months, y=grid.index, colorscale=DIVERGING, zmid=0, zmin=-lim,
                              zmax=lim, text=text.values, texttemplate="%{text}", xgap=2, ygap=2,
                              hovertemplate="%{x} %{y}: %{z:.2f}%<extra></extra>",
                              colorbar=dict(ticksuffix="%", thickness=10)))
    hm.update_yaxes(autorange="reversed", type="category")
    with left:
        st.subheader("Monthly returns")
        st.caption(f"✓ = month hit the {target_m:.0f}% goal")
        st.plotly_chart(layout(hm, 34 * len(grid) + 80, hovermode="closest"), width="stretch")

    yrs = list(s["yearly_pct"])
    yb = go.Figure()
    yb.add_bar(x=yrs, y=[s["yearly_pct"][y] for y in yrs], name="Strategy", marker_color=STRAT,
               hovertemplate="%{y:.1f}%")
    yb.add_bar(x=yrs, y=[s["benchmark_yearly_pct"].get(y) for y in yrs], name="SPY", marker_color=BENCH,
               hovertemplate="%{y:.1f}%")
    yb.add_hline(y=target_y, line=dict(color=TARGET, dash="dash", width=2),
                 annotation_text=f"goal {target_y:.1f}%", annotation_position="top right",
                 annotation_font_color=TARGET)
    yb.update_traces(marker_line_width=0)
    with right:
        st.subheader("Yearly returns")
        st.caption("A partial first or last year shows only the months tested.")
        st.plotly_chart(layout(yb, 34 * len(grid) + 80, barmode="group", bargap=0.25, bargroupgap=0.08,
                               yaxis=dict(ticksuffix="%"), xaxis=dict(type="category")), width="stretch")

# ---------------- sleeves ----------------
tr = pd.read_sql("SELECT * FROM trades WHERE run_id=? ORDER BY exit_time DESC", con, params=(int(rid),))
if len(tr) and tr.sleeve.notna().any():
    st.subheader("Long-term vs short-term")
    g = tr.groupby("sleeve").agg(trades=("pnl", "size"), pnl=("pnl", "sum"),
                                 win_rate=("pnl", lambda x: (x > 0).mean() * 100),
                                 avg_ret=("ret_pct", "mean"), best=("ret_pct", "max"), worst=("ret_pct", "min"))
    cols = st.columns(len(g))
    for col, (name, r) in zip(cols, g.iterrows()):
        col.metric(name.replace("_", " ").title(), f"${r.pnl:,.0f}",
                   f"{int(r.trades)} trades · {r.win_rate:.0f}% win · avg {r.avg_ret:+.1f}%", delta_color="off")
    reasons = tr.groupby(["sleeve", "reason"]).size().unstack(fill_value=0)
    st.caption("How trades ended")
    st.dataframe(reasons, width="stretch")

# ---------------- variant comparison ----------------
if run["mode"] == "swing":
    rows = []
    for r in kinds["Swing backtests"].itertuples():
        x = json.loads(r.summary)
        rows.append({"run": r.id, "variant": x.get("label"), "CAGR %": x["cagr_pct"], "avg month %": x["avg_month_pct"],
                     "months ≥1% (%)": x["months_hit_target_pct"], "max DD %": x["max_drawdown_pct"],
                     "Sharpe": x["sharpe"], "trades": x["trades"], "SPY CAGR %": x["benchmark_cagr_pct"]})
    if len(rows) > 1:
        st.subheader("All swing runs")
        st.dataframe(pd.DataFrame(rows).round(2), hide_index=True, width="stretch")

# ---------------- live positions ----------------
if run["mode"].endswith("live") and has_keys():
    from alpaca.trading.client import TradingClient
    k, sec = alpaca_keys()
    pos = TradingClient(k, sec, paper=True).get_all_positions()
    sleeves = dict(con.execute("SELECT symbol, sleeve FROM swing_positions").fetchall())
    st.subheader("Open positions")
    st.dataframe(pd.DataFrame([{"symbol": p.symbol, "sleeve": sleeves.get(p.symbol, ""), "qty": float(p.qty),
                                "avg cost": float(p.avg_entry_price), "price": float(p.current_price),
                                "P&L $": float(p.unrealized_pl), "P&L %": float(p.unrealized_plpc) * 100}
                               for p in pos]).round(2), hide_index=True, width="stretch")

# ---------------- trades ----------------
if len(tr):
    st.subheader("Trade log")
    opts = ["all"] + sorted(tr.sleeve.dropna().unique().tolist())
    pick = st.radio("Sleeve", opts, horizontal=True) if len(opts) > 1 else "all"
    view = tr if pick == "all" else tr[tr.sleeve == pick]
    st.dataframe(view.drop(columns=["run_id"]).round(2), hide_index=True, width="stretch")

if run["mode"] == "swing":
    with st.expander("Caveats on these numbers"):
        st.markdown(
            "- **Look-ahead bias:** the fundamentals screen uses *today's* numbers to pick stocks for a backtest "
            "going back to 2016, and the universe is today's large caps. Compare against the "
            "**No fundamentals filter** run for a fairer read.\n"
            "- **Taxes are not modelled.** Short-term gains are taxed with no CGT discount.\n"
            "- Fills assume next-day open plus slippage. Real fills, especially on gaps, can be worse.")
