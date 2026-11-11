# Paper Trader

US stock day-trading bot that only uses fake money: it backtests on historical bars and runs live on Alpaca's paper account. The backtest and live modes call the same strategy code.

## Setup
    python3 -m venv .venv && .venv/bin/pip install alpaca-py pandas numpy pyyaml python-dotenv yfinance streamlit plotly
    cp .env.example .env   # add your Alpaca PAPER keys (only needed for live; backtest falls back to yfinance)

Edit `config.yaml` to change tickers, the rule (`dip` / `rsi`), thresholds, TP/SL, sizing and risk limits.

## Run
    .venv/bin/python main.py backtest                               # single backtest using config.yaml
    .venv/bin/python main.py backtest --set entry_rule=rsi rsi_threshold=20
    .venv/bin/python main.py sweep                                  # grid over rule x TP x SL
    .venv/bin/python main.py live                                   # Alpaca paper; loops every minute, sleeps when market is closed
    .venv/bin/python main.py report                                 # stats for the last 5 runs
    .venv/bin/streamlit run dashboard.py                            # equity vs SPY, trades, positions

## Notes
- Without keys, backtests use yfinance (5m bars, about 60 days max). With Alpaca keys you get IEX minute bars going back 6-12 months.
- The live bot refuses to start unless ALPACA_BASE_URL is the paper endpoint.
- The backtest fill model is conservative: it enters at the next bar's open plus slippage, assumes the stop fills first when a bar touches both stop and target, and fills gaps at the open.

## Swing mode (daily bars: fundamentals, long-term + short-term)
    .venv/bin/python main.py swing           # backtest config.yaml -> swing section (2016 to today)
    .venv/bin/python main.py swing-compare   # your config vs short-only, long-only, all +10/-5, no fundamentals filter
    .venv/bin/python main.py swing-live      # Alpaca paper; acts once per trading day, 5 min after the open

- Fundamentals screen: profitable, positive free cash flow, ranked on ROE, margin, revenue growth, debt and forward P/E (Yahoo snapshot, cached daily).
- Short-term sleeve: buys a quality stock above its 200-day average when its 2-day RSI is below 10. Exits at +10% / −5% via server-side bracket orders, or after a 40-day time stop.
- Long-term sleeve: each month, holds the strongest quality stocks by 12-month momentum that are in an uptrend. Exits on a 15% trailing stop (server-side) or a month-end close below the 200-day average.
- Dashboard: growth vs SPY vs the 1%/month goal, drawdown, a monthly heatmap with ✓ for months that hit the goal, yearly bars, sleeve P&L, all runs compared, and the trade log.

## Forward test (simulated money, real prices, from a fixed start date)
    .venv/bin/python main.py forward start            # freezes the universe and config and records the start time
    .venv/bin/python main.py forward                  # update now (the dashboard also does this on every visit)
    .venv/bin/python main.py forward start --restart  # throw away the current test and start a new one

- Every update replays the test from the start date on fresh market data, so no server or daily run is needed.
- Costs come from `swing.costs` in config.yaml (flat fee per order, FX, SEC/FINRA fees) plus slippage.
- The checkpoints on days 1, 5, 10, 20 and 60 are each judged on the data as of that day.

## Hosting (Streamlit Community Cloud, free)
Push this folder to a private GitHub repo, then go to share.streamlit.io → Create app → pick the repo → main file `dashboard.py`.
Notes you add on the hosted app are lost when the app restarts; the trading results are not, because they rebuild from the start date.
