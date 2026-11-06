# Agent Grid data pack

Rebuilt by the `market-data` GitHub Action. The `data` branch always holds a single commit with the latest pack.

Read it from an agent session with:

    git clone -q --depth 1 --branch data https://github.com/enelson43/Agent-grid-data /tmp/agd

Check `manifest.json` first: `generatedAt`, `lastBarDates`, `sectionsUpdatedAt` and `errors` tell you how fresh each part is.

| Path | Contents | Refreshed |
|---|---|---|
| `indicators/latest.json` | Per ticker: close, 20/50/200-day and 10/40-week averages (and whether rising), RSI(14), MACD, 14-day and weekly ATR, volume ratio, 1/3/6/12-month returns, relative strength vs SPY and its watchlist rank, 52-week and 20/50-day highs and lows, 20-day realized volatility, `trendScoreHint` (0-2) | Hourly in market hours |
| `prices/daily/T.csv` | 3 years of daily OHLCV | Hourly |
| `prices/weekly/T.csv` | Weekly bars (week ending Friday; the current week may be partial) | Hourly |
| `prices/hourly/T.csv` | 60 days of 1-hour bars | Hourly |
| `options/summary.json` | IV30, HV20, IV/HV, IV rank and percentile (once 20+ days of history exist), put/call volume and open interest, ATM IV and straddle by expiry, unusual volume (volume > 2x OI and 500+), earnings implied move | 4x per trading day + full run |
| `options/chains/T.json` | Selected expiries (nearest 2, ~30, ~90, ~150 and ~400 days), strikes within 25% of spot: bid, ask, last, volume, OI, IV, delta | Same |
| `options/iv_history.csv` | Daily IV30 per ticker, used for IV rank | Daily |
| `fundamentals/T.json` | Valuation and margin metrics, 8 quarters of revenue/EPS/FCF, balance sheet, earnings history and surprises, analyst targets and ratings, Yahoo insider transactions, top institutional holders | After the close |
| `calendar/earnings.json` | Next earnings date per company (an estimate unless the company confirmed it) | After the close |
| `sec/form4.json` | SEC Form 4 transactions parsed from EDGAR (currently unavailable: the SEC blocks GitHub-hosted runners; use `insiderTransactionsYahoo` in `fundamentals/T.json`) | After the close |

Prices come from Yahoo Finance via yfinance (Stooq as backup) and are delayed. Verify any price used for a trade decision.
