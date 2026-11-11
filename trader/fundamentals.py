"""Fundamental quality screen using Yahoo's current snapshot (cached for a day)."""
import json
import time
from datetime import date
from pathlib import Path

import pandas as pd

from .config import ROOT

CACHE = ROOT / "data" / "fundamentals.json"
FIELDS = ["returnOnEquity", "profitMargins", "revenueGrowth", "debtToEquity", "forwardPE", "freeCashflow"]


def fetch_fundamentals(tickers) -> pd.DataFrame:
    CACHE.parent.mkdir(exist_ok=True)
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    today = date.today().isoformat()
    import yfinance as yf
    for t in tickers:
        if cache.get(t, {}).get("_date") == today:
            continue
        try:
            info = yf.Ticker(t).info
            cache[t] = {k: info.get(k) for k in FIELDS} | {"_date": today}
        except Exception as e:
            print(f"  fundamentals failed for {t}: {e}")
        time.sleep(0.2)
    CACHE.write_text(json.dumps(cache, indent=1))
    return pd.DataFrame({t: cache[t] for t in tickers if t in cache}).T.drop(columns="_date")


def quality_scores(tickers) -> pd.DataFrame:
    f = fetch_fundamentals(tickers).apply(pd.to_numeric, errors="coerce")
    ok = (f.profitMargins > 0) & (f.freeCashflow > 0)
    pe = f.forwardPE.where(f.forwardPE > 0)
    ranks = pd.DataFrame({
        "roe": f.returnOnEquity.rank(pct=True),
        "margin": f.profitMargins.rank(pct=True),
        "growth": f.revenueGrowth.rank(pct=True),
        "low_debt": (-f.debtToEquity).rank(pct=True),
        "value": (-pe).rank(pct=True),
    }).fillna(0.5)
    f["score"] = ranks.mean(axis=1).where(ok, 0)
    f["passes"] = ok
    return f.sort_values("score", ascending=False)


def select_quality(tickers, n) -> list[str]:
    q = quality_scores(tickers)
    return list(q[q.passes].head(n).index)
