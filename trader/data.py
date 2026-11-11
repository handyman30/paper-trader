"""Historical bars: Alpaca (if keys in .env) else yfinance fallback."""
from datetime import datetime, timedelta, timezone

import pandas as pd

from .config import alpaca_keys, has_keys

NY = "America/New_York"


def _regular_hours(df: pd.DataFrame) -> pd.DataFrame:
    df = df.tz_convert(NY) if df.index.tz else df.tz_localize("UTC").tz_convert(NY)
    return df.between_time("09:30", "15:59")


def fetch_alpaca(tickers, days, tf_min, end=None) -> dict[str, pd.DataFrame]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed

    key, sec = alpaca_keys()
    client = StockHistoricalDataClient(key, sec)
    end = end or datetime.now(timezone.utc) - timedelta(minutes=16)  # free feed: 15-min delay on recent SIP
    req = StockBarsRequest(
        symbol_or_symbols=list(tickers),
        timeframe=TimeFrame(tf_min, TimeFrameUnit.Minute),
        start=end - timedelta(days=days),
        end=end,
        feed=DataFeed.IEX,
    )
    raw = client.get_stock_bars(req).df
    out = {}
    for t in tickers:
        if t in raw.index.get_level_values(0):
            d = raw.xs(t, level=0)[["open", "high", "low", "close", "volume"]]
            out[t] = _regular_hours(d)
    return out


def fetch_yf(tickers, days, tf_min) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    interval = f"{tf_min}m"
    cap = 7 if tf_min == 1 else 59
    days = min(days, cap)
    raw = yf.download(list(tickers), period=f"{days}d", interval=interval,
                      group_by="ticker", auto_adjust=False, progress=False, threads=True)
    out = {}
    for t in tickers:
        try:
            d = raw[t].rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
        except KeyError:
            continue
        if len(d):
            out[t] = _regular_hours(d)
    return out


def fetch_bars(tickers, days, tf_min) -> tuple[dict[str, pd.DataFrame], str]:
    if has_keys():
        return fetch_alpaca(tickers, days, tf_min), "alpaca-iex"
    return fetch_yf(tickers, days, tf_min), "yfinance"
