"""Forward-test page: head-to-head (your algo vs Claude's), then per-algo journal, checkpoints, positions."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from trader import forward

# reference palette slots 1-3 (validated all-pairs for 3 series) + muted dashed reference
COLORS = {"yours": "#2a78d6", "spy": "#eb6834", "claude": "#1baf7a"}
TARGET = "#898781"
ICON = {"pass": "✅", "fail": "⚠️", "pending": "⏳"}
BACKTEST_CAGR = {"yours": 17.6, "claude": 10.4}  # 2016-2026 backtest with costs, for reference only


@st.cache_data(ttl=900, show_spinner="Updating with the latest market data…")
def _update(_bucket, name):
    return forward.update(name, verbose=False)


@st.cache_data(ttl=300, show_spinner=False)
def _marks(_bucket, out_json):
    return forward.live_marks(json.loads(out_json))


def render():
    if not forward.exists("yours"):
        st.info("No forward test yet. Run `python main.py forward start`.")
        return
    now = pd.Timestamp.now(tz="UTC")
    names = [n for n in forward.TESTS if forward.exists(n)]
    outs = {n: _update(now.floor("15min").isoformat(), n) or forward.load_journal(n) for n in names}
    marks = {n: _marks(now.floor("5min").isoformat(), json.dumps(o, default=str)) for n, o in outs.items()}
    y = outs["yours"]
    tz = ZoneInfo(y["state"].get("local_tz", "UTC"))
    cap = y["start_cash"]

    st.title("Forward test: your algo vs Claude's algo")
    st.markdown(f"Simulated **${cap:,.0f}** each, real US prices, fees included (`{y['cost_preset']}`). "
                "Same stocks and same buy rules; **only the stop-loss and sell rules differ**.")
    upd = datetime.fromisoformat(y["updated_utc"]).astimezone(tz)
    c1, c2 = st.columns([4, 1])
    c1.caption(f"Data refreshed {upd:%a %d %b, %I:%M %p %Z}. Re-checks every 15 min while the page is open. "
               "Trades are decided on each US close and filled at the next open.")
    if c2.button("Refresh now"):
        _update.clear()
        _marks.clear()
        st.rerun()

    # ---------- scoreboard ----------
    st.subheader("Scoreboard")
    cols = st.columns(len(names))
    for col, n in zip(cols, names):
        o, m = outs[n], marks[n]
        j = o["journal"]
        last = j[-1] if j else None
        with col:
            st.markdown(f"**{forward.TESTS[n]}** · started {o['state']['started_local']}")
            if m:
                eq, sub = m["equity"], f"{m['today_pct']:+.2f}% today · live {m['as_of']}"
            elif last:
                eq, sub = last["equity"], f"{last['day_pct']:+.2f}% on {pd.Timestamp(last['date']):%a %d %b}"
            else:
                eq, sub = cap - o.get("fx_fee", 0), "waiting for first session"
            a, b, c = st.columns(3)
            a.metric("Equity", f"${eq:,.0f}", sub, delta_color="off")
            b.metric("Since start", f"{(eq / cap - 1) * 100:+.2f}%",
                     f"SPY {last['spy_cum_pct']:+.2f}%" if last else "SPY —", delta_color="off")
            passed = sum(r["status"] == "pass" for r in o["readiness"])
            c.metric("Checkpoints", f"{passed}/{len(o['readiness'])}",
                     f"{len(o['closed_trades'])} closed trades", delta_color="off")
    if not y["journal"]:
        first = pd.Timestamp(y["state"]["first_session"])
        close_local = pd.Timestamp.combine(first.date(), pd.Timestamp("16:00").time()).tz_localize(
            "America/New_York").tz_convert(tz)
        st.info(f"⏳ No trades yet. Both algos buy at the US open on **{first:%a %d %b}** and the first result "
                f"lands around **{close_local:%a %d %b, %I:%M %p %Z}**. For reference only, the 10-year backtest "
                f"made {BACKTEST_CAGR['yours']}%/yr for your algo vs {BACKTEST_CAGR['claude']}%/yr for Claude's. "
                "The forward test is the one that counts.")

    # ---------- head-to-head chart ----------
    if any(len(o["equity"]) > 1 for o in outs.values()):
        fig = go.Figure()
        e0 = pd.to_datetime([d for d, _ in y["equity"]])
        goal = cap * 1.01 ** ((e0 - e0[0]).days / 30.4375)
        fig.add_scatter(x=e0, y=goal, name="Goal 1%/mo", line=dict(color=TARGET, dash="dash", width=2),
                        hovertemplate="$%{y:,.0f}")
        fig.add_scatter(x=pd.to_datetime([d for d, _ in y["bench"]]), y=[v for _, v in y["bench"]],
                        name="SPY buy & hold", line=dict(color=COLORS["spy"], width=2), hovertemplate="$%{y:,.0f}")
        for n, o in outs.items():
            x = pd.to_datetime([d for d, _ in o["equity"]])
            fig.add_scatter(x=x, y=[v for _, v in o["equity"]], name=forward.TESTS[n],
                            line=dict(color=COLORS[n], width=2), mode="lines+markers" if len(x) < 40 else "lines",
                            marker=dict(size=8, line=dict(width=2, color="white")), hovertemplate="$%{y:,.0f}")
        fig.update_layout(height=380, margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified",
                          legend=dict(orientation="h", y=1.1, x=0), yaxis=dict(tickprefix="$", tickformat=",.0f"),
                          xaxis=dict(rangebreaks=[dict(bounds=["sat", "mon"])]))
        st.plotly_chart(fig, width="stretch")

    with st.expander("What's different between the two algos?"):
        st.markdown(
            "| | Your algo | Claude's algo |\n|---|---|---|\n"
            "| Short-term sell | **+10%** target | when the bounce is done (close above 5-day average) |\n"
            "| Short-term stop | **−5%** | 2.5 × ATR(14) below entry (scales with volatility) |\n"
            "| Short-term time limit | 40 days | 10 days |\n"
            "| Long-term stop | **15%** trailing from the high | 3 × ATR(22) trailing from the high |\n"
            "| Stocks, buy rules, sizing, fees | same | same |\n\n"
            "Claude's rules were fixed **before** testing. In the 10-year backtest they did worse "
            "(10.4% vs 17.6%/yr): selling bounces early cut winners short, and the ATR trailing stop got "
            "shaken out of trends. They're being forward-tested anyway, unchanged, as an honest head-to-head.")

    # ---------- per-algo detail ----------
    tabs = st.tabs([forward.TESTS[n] for n in names])
    for tab, n in zip(tabs, names):
        with tab:
            render_test(n, outs[n], marks[n], tz)


def render_test(name, out, marks, tz):
    stt, j, cap = out["state"], out["journal"], out["start_cash"]
    last = j[-1] if j else None
    st.markdown(f"**Started {stt['started_local']}** ({stt['started_et']}) · baseline = US close "
                f"{out['baseline_date']}")
    if last:
        c = st.columns(4)
        c[0].metric("vs 1%/month goal", f"{'+' if last['vs_goal'] >= 0 else '-'}${abs(last['vs_goal']):,.0f}",
                    "ahead" if last["vs_goal"] >= 0 else "behind",
                    delta_color="normal" if last["vs_goal"] >= 0 else "inverse")
        c[1].metric("Fees paid", f"${last['fees_cum']:,.2f}", f"incl. ${out.get('fx_fee', 0):,.0f} one-time FX",
                    delta_color="off")
        c[2].metric("Trading days", f"{len(j)}")
        c[3].metric("Worst drawdown", f"{min(x['drawdown_pct'] for x in j):.1f}%")
    if out["queued"]:
        st.info("**Orders queued for the next US open:** " + " · ".join(out["queued"]))

    st.markdown("##### Road to real money")
    cols = st.columns(len(out["readiness"]))
    for col, r in zip(cols, out["readiness"]):
        with col:
            st.markdown(f"**{ICON[r['status']]} Day {r['day']}**  \n{r['title']}")
            st.caption(r["question"] + (f" (judged {r['date']})" if r.get("date") else ""))
            for ch in r["checks"]:
                st.markdown(f"<small>{ICON[ch['status']]} {ch['label']}: {ch['detail']}</small>",
                            unsafe_allow_html=True)

    st.markdown("##### Daily journal")
    notes = forward.load_notes(name)
    st.markdown(f"**{stt['started_local']}: Started.** ${cap:,.0f} simulated, "
                f"{len(stt['universe'])} quality stocks: {', '.join(stt['universe'])}.")
    for x in reversed(j):
        icon = "🟢" if x["day_pct"] >= 0 else "🔴"
        with st.expander(f"{icon} {pd.Timestamp(x['date']):%a %d %b} · Day {x['day']} · "
                         f"{x['day_pct']:+.2f}% · total {x['cum_pct']:+.2f}%", expanded=x is j[-1]):
            st.write(x["note"])
            if x["date"] in notes:
                st.markdown(f"📝 *Your note:* {notes[x['date']]}")
            with st.form(f"note_{name}_{x['date']}", border=False):
                txt = st.text_input("Note", value=notes.get(x["date"], ""), label_visibility="collapsed",
                                    placeholder="Add your own note for this day")
                if st.form_submit_button("Save note"):
                    forward.save_note(x["date"], txt, name)
                    st.rerun()

    if out["open_positions"]:
        st.markdown("##### Open positions")
        rows = []
        for p in out["open_positions"]:
            px = (marks or {}).get("prices", {}).get(p["ticker"], p["price"])
            rows.append({"ticker": p["ticker"], "sleeve": p["sleeve"].replace("_", "-"), "qty": p["qty"],
                         "bought": p["entry_date"], "cost": p["entry"], "price": px,
                         "P&L $": p["qty"] * (px - p["entry"]), "P&L %": (px / p["entry"] - 1) * 100,
                         "sell below": p["stop"], "sell at (target)": p["target"]})
        st.dataframe(pd.DataFrame(rows).round(2), hide_index=True, width="stretch")
    if out["closed_trades"]:
        st.markdown("##### Closed trades")
        t = pd.DataFrame(out["closed_trades"])[["sleeve", "ticker", "entry_time", "exit_time", "qty", "entry_price",
                                                 "exit_price", "fees", "pnl", "ret_pct", "reason"]]
        st.dataframe(t.sort_values("exit_time", ascending=False).round(2), hide_index=True, width="stretch")
