"""Forward-test page: started-on note, daily journal, readiness checkpoints, live marks."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from trader import forward

STRAT, BENCH, TARGET = "#2a78d6", "#eb6834", "#898781"
ICON = {"pass": "✅", "fail": "⚠️", "pending": "⏳"}


@st.cache_data(ttl=900, show_spinner="Updating with the latest market data…")
def _update(_bucket):
    return forward.update(verbose=False)


@st.cache_data(ttl=300, show_spinner=False)
def _marks(_bucket, out_json):
    import json
    return forward.live_marks(json.loads(out_json))


def render():
    if not forward.STATE.exists():
        st.info("No forward test yet. Run `python main.py forward start`.")
        return
    now = pd.Timestamp.now(tz="UTC")
    out = _update(now.floor("15min").isoformat()) or forward.load_journal()
    stt = out["state"]
    tz = ZoneInfo(stt.get("local_tz", "UTC"))
    j = out["journal"]
    cap = out["start_cash"]

    st.title("Forward test: simulated money, real prices")
    st.markdown(f"**Started {stt['started_local']}** ({stt['started_et']}) with **${cap:,.0f}**. "
                f"Fees model: `{out['cost_preset']}`. Baseline = US close on {out['baseline_date']}.")
    upd = datetime.fromisoformat(out["updated_utc"]).astimezone(tz)
    st.caption(f"Data refreshed {upd:%a %d %b, %I:%M %p %Z}. The page re-checks every 15 min while open. "
               "Trades are decided once a day on the US close and filled at the next open.")
    if st.button("Refresh now"):
        _update.clear()
        _marks.clear()
        st.rerun()

    import json
    marks = _marks(now.floor("5min").isoformat(), json.dumps(out, default=str))
    last = j[-1] if j else None

    # ---------- headline ----------
    c = st.columns(5)
    if marks:
        c[0].metric(f"Equity now ({marks['as_of']})", f"${marks['equity']:,.0f}",
                    f"{marks['today_pct']:+.2f}% today (live, ~15 min delay)")
    else:
        eq_now = last["equity"] if last else out["equity"][-1][1]
        c[0].metric("Equity (last close)", f"${eq_now:,.0f}",
                    f"{last['day_pct']:+.2f}% on {last['date']}" if last else "no sessions yet")
    if last:
        c[1].metric("Since start (after fees)", f"{last['cum_pct']:+.2f}%", f"SPY {last['spy_cum_pct']:+.2f}%",
                    delta_color="off")
        c[2].metric("vs 1%/month goal", f"{'+' if last['vs_goal'] >= 0 else '-'}${abs(last['vs_goal']):,.0f}",
                    "ahead" if last["vs_goal"] >= 0 else "behind", delta_color="normal" if last["vs_goal"] >= 0 else "inverse")
        c[3].metric("Fees paid", f"${last['fees_cum']:,.2f}",
                    f"incl. ${out.get('fx_fee', 0):,.0f} one-time FX", delta_color="off")
        c[4].metric("Trading days", f"{len(j)}", f"worst drawdown {min(x['drawdown_pct'] for x in j):.1f}%",
                    delta_color="off")
    else:
        first = pd.Timestamp(stt["first_session"])
        close_local = pd.Timestamp.combine(first.date(), pd.Timestamp("16:00").time()).tz_localize(
            "America/New_York").tz_convert(tz)
        c[1].metric("First session", f"{first:%a %d %b} (US)", f"first result ≈ {close_local:%a %d %b %I:%M %p %Z}",
                    delta_color="off")
        c[2].metric("Cash", f"${sum(out['cash'].values()):,.0f}")

    if out["queued"]:
        st.info("**Orders queued for the next US open:** " + " · ".join(out["queued"]))

    # ---------- readiness ----------
    st.subheader("Road to real money")
    cols = st.columns(len(out["readiness"]))
    for col, r in zip(cols, out["readiness"]):
        with col:
            st.markdown(f"**{ICON[r['status']]} Day {r['day']}**  \n{r['title']}")
            st.caption(r["question"] + (f" (judged {r['date']})" if r.get("date") else ""))
            for ch in r["checks"]:
                st.markdown(f"<small>{ICON[ch['status']]} {ch['label']}: {ch['detail']}</small>",
                            unsafe_allow_html=True)
    st.caption("A ⚠️ isn't a disaster, but it is a reason to wait before using real money. Even at day 60, "
               "start real money small; the paper results don't include emotions, outages or worse real-world fills.")

    # ---------- chart ----------
    if len(out["equity"]) > 1:
        e = pd.DataFrame(out["equity"], columns=["d", "v"]).set_index("d")["v"]
        b = pd.DataFrame(out["bench"], columns=["d", "v"]).set_index("d")["v"]
        idx = pd.to_datetime(e.index)
        goal = cap * (1 + 0.01) ** ((idx - idx[0]).days / 30.4375)
        fig = go.Figure()
        fig.add_scatter(x=idx, y=goal, name="Goal 1%/mo", line=dict(color=TARGET, dash="dash", width=2),
                        hovertemplate="$%{y:,.0f}")
        fig.add_scatter(x=idx, y=b.values, name="SPY buy & hold", line=dict(color=BENCH, width=2),
                        hovertemplate="$%{y:,.0f}")
        fig.add_scatter(x=idx, y=e.values, name="Strategy (after fees)", line=dict(color=STRAT, width=2),
                        mode="lines+markers" if len(e) < 40 else "lines", marker=dict(size=8),
                        hovertemplate="$%{y:,.0f}")
        fig.update_layout(height=360, margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified",
                          legend=dict(orientation="h", y=1.1, x=0), yaxis=dict(tickprefix="$", tickformat=",.0f"),
                          xaxis=dict(rangebreaks=[dict(bounds=["sat", "mon"])]))
        st.plotly_chart(fig, width="stretch")

    # ---------- journal ----------
    st.subheader("Daily journal")
    notes = forward.load_notes()
    st.markdown(f"**{stt['started_local']}: Started.** ${cap:,.0f} simulated, "
                f"{len(stt['universe'])} quality stocks: {', '.join(stt['universe'])}.")
    for x in reversed(j):
        icon = "🟢" if x["day_pct"] >= 0 else "🔴"
        with st.expander(f"{icon} {pd.Timestamp(x['date']):%a %d %b} · Day {x['day']} · "
                         f"{x['day_pct']:+.2f}% · total {x['cum_pct']:+.2f}%", expanded=x is j[-1]):
            st.write(x["note"])
            if x["date"] in notes:
                st.markdown(f"📝 *Your note:* {notes[x['date']]}")
            with st.form(f"note_{x['date']}", clear_on_submit=False, border=False):
                txt = st.text_input("Add a note for this day", value=notes.get(x["date"], ""),
                                    label_visibility="collapsed", placeholder="Add your own note for this day")
                if st.form_submit_button("Save note"):
                    forward.save_note(x["date"], txt)
                    st.rerun()

    # ---------- positions & trades ----------
    if out["open_positions"]:
        st.subheader("Open positions")
        rows = []
        for p in out["open_positions"]:
            px = (marks or {}).get("prices", {}).get(p["ticker"], p["price"])
            rows.append({"ticker": p["ticker"], "sleeve": p["sleeve"].replace("_", "-"), "qty": p["qty"],
                         "bought": p["entry_date"], "cost": p["entry"], "price": px,
                         "P&L $": p["qty"] * (px - p["entry"]), "P&L %": (px / p["entry"] - 1) * 100,
                         "sell below": p["stop"], "sell at (target)": p["target"]})
        st.dataframe(pd.DataFrame(rows).round(2), hide_index=True, width="stretch")
    if out["closed_trades"]:
        st.subheader("Closed trades")
        t = pd.DataFrame(out["closed_trades"])[["sleeve", "ticker", "entry_time", "exit_time", "qty", "entry_price",
                                                 "exit_price", "fees", "pnl", "ret_pct", "reason"]]
        st.dataframe(t.sort_values("exit_time", ascending=False).round(2), hide_index=True, width="stretch")
