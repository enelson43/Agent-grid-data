# agent-grid-data

Free market-data pipeline for the Agent Grid. A scheduled GitHub Action (`.github/workflows/market-data.yml`) runs `scripts/build_data.py` for the tickers in `watchlist.txt` and publishes the results to the `data` branch. See `DATA_README.md` for the file layout.

To change the tickers, edit `watchlist.txt`. To refresh now, run the workflow from the Actions tab with mode `full`.
