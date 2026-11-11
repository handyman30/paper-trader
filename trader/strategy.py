"""Entry rules. The same functions drive backtest and live, so results are comparable."""
import numpy as np
import pandas as pd


def add_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """df: OHLCV bars indexed by tz-aware NY timestamps, single ticker."""
    df = df.copy()
    day = df.index.date
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = (tp * df["volume"]).groupby(day).cumsum()
    vol = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    df["vwap"] = (pv / vol).fillna(df["close"])

    n = cfg.get("rsi_period", 14)
    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = (100 - 100 / (1 + rs)).fillna(50)
    return df


def entry_signal(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Boolean series: True where the rule fires on that bar's close."""
    rule = cfg["entry_rule"]
    if rule == "dip":
        return df["close"] <= df["vwap"] * (1 - cfg["dip_pct"] / 100)
    if rule == "rsi":
        return df["rsi"] < cfg["rsi_threshold"]
    raise ValueError(f"Unknown entry_rule '{rule}' (use 'dip' or 'rsi')")


def in_entry_window(ts: pd.Timestamp, cfg: dict) -> bool:
    mins = ts.hour * 60 + ts.minute
    open_m, close_m = 9 * 60 + 30, 16 * 60
    return open_m + cfg["no_entry_first_min"] <= mins < close_m - cfg["no_entry_last_min"]
