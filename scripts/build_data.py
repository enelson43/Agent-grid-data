"""Build the Agent Grid data pack.

Usage: python scripts/build_data.py OUT_DIR MODE
MODE: light   - daily, weekly and hourly prices plus indicators
      options - light + option chains and volatility summary
      full    - options + S&P 500 screener, fundamentals, earnings calendar and SEC Form 4
Any run also refreshes a section that has gone stale (a missed scheduled run), whatever its mode.

Coverage: the core watchlist (watchlist.txt), the sector ETFs (sector_etfs.csv) and the screener
shortlist get full data. The rest of the S&P 500 is only screened.

OUT_DIR may already hold the previous pack; files not rebuilt in this mode are kept.
"""
import datetime as dt
import json
import math
import os
import sys
import time
import traceback
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd
import requests
import yfinance as yf

OUT = sys.argv[1] if len(sys.argv) > 1 else "out"
MODE = sys.argv[2] if len(sys.argv) > 2 else "full"
DO_OPTIONS = MODE in ("options", "full")
DO_FULL = MODE == "full"
NOW = dt.datetime.now(dt.timezone.utc)
SEC_UA = "AgentGridData research script"
RISK_FREE = 0.045  # flat rate used only for option deltas

errors = {}


def err(section, ticker, e):
    errors.setdefault(section, {})[ticker] = str(e)[:300]


def path(*parts):
    p = os.path.join(OUT, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def clean(o):
    """Make pandas/numpy objects JSON-safe."""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (pd.Timestamp, dt.datetime, dt.date)):
        return o.isoformat()[:10] if isinstance(o, dt.date) and not isinstance(o, dt.datetime) else pd.Timestamp(o).isoformat()
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if (o is None or math.isnan(o) or math.isinf(o)) else round(float(o), 6)
    if isinstance(o, np.bool_):
        return bool(o)
    if o is pd.NaT:
        return None
    return o


def write_json(obj, *parts):
    with open(path(*parts), "w") as f:
        json.dump(clean(obj), f, separators=(",", ":"))


def read_tickers():
    with open("watchlist.txt") as f:
        return [l.strip().upper() for l in f if l.strip() and not l.startswith("#")]


CORE = read_tickers()
if "SPY" not in CORE:
    CORE.insert(0, "SPY")
SECTORS = pd.read_csv("sector_etfs.csv").set_index("ticker")["name"].to_dict()
TICKERS = list(CORE)  # replaced in main() by the full coverage list

SHORTLIST_SIZE = 35      # top-ranked names
SHORTLIST_MAX = 40       # plus up to 5 breakout / 40-week reclaim alerts
MIN_PRICE = 10
MIN_DOLLAR_VOL = 25e6    # 20-day average


def load_prev_manifest():
    p = os.path.join(OUT, "manifest.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}


PREV = load_prev_manifest()


def age_hours(section):
    ts = PREV.get("sectionsUpdatedAt", {}).get(section)
    if not ts:
        return 1e9
    return (NOW - dt.datetime.fromisoformat(ts)).total_seconds() / 3600


# ---------------------------------------------------------------- prices
def split_download(df, tickers):
    out = {}
    if df is None or df.empty:
        return out
    if isinstance(df.columns, pd.MultiIndex):
        lvl0 = set(df.columns.get_level_values(0))
        for t in tickers:
            if t in lvl0:
                sub = df[t].dropna(how="all")
                if not sub.empty:
                    out[t] = sub
    elif len(tickers) == 1:
        out[tickers[0]] = df.dropna(how="all")
    return out


def stooq_daily(t):
    url = f"https://stooq.com/q/d/l/?s={t.lower()}.us&i=d"
    df = pd.read_csv(url)
    if df.empty or "Close" not in df.columns:
        raise ValueError("stooq returned no data")
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()
    df = df[df.index >= df.index.max() - pd.Timedelta(days=3 * 366)]
    return df


def naive(df):
    if getattr(df.index, "tz", None) is not None:
        df = df.copy()
        df.index = df.index.tz_localize(None)
    return df


def download_daily(tickers, period, section, chunk=100, retry_limit=40):
    """Batch download in chunks, then retry missing tickers one at a time."""
    out = {}
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        try:
            out.update(split_download(
                yf.download(part, period=period, interval="1d", group_by="ticker", auto_adjust=False,
                            threads=True, progress=False), part))
        except Exception as e:
            err(section, f"chunk {i // chunk + 1}", e)
        if len(tickers) > chunk:
            time.sleep(2)
    missing = [t for t in tickers if t not in out]
    for t in missing[:retry_limit]:
        try:
            h = yf.Ticker(t).history(period=period, interval="1d", auto_adjust=False)
            if h is not None and not h.empty:
                out[t] = naive(h)
        except Exception:
            pass
        time.sleep(0.5)
    return {t: naive(d) for t, d in out.items()}


def get_prices():
    daily = download_daily(TICKERS, "3y", "daily")
    for t in TICKERS:
        if t not in daily:
            try:
                daily[t] = stooq_daily(t)
            except Exception as e:
                err("daily", t, f"yfinance (batch and single) and stooq failed: {e}")
    hourly = {}
    try:
        hourly = split_download(
            yf.download(TICKERS, period="60d", interval="60m", group_by="ticker", auto_adjust=False,
                        threads=True, progress=False), TICKERS)
    except Exception as e:
        err("hourly", "*", e)
    for t in TICKERS:
        if t not in hourly:
            err("hourly", t, "no hourly data")
    return daily, hourly


def to_weekly(d):
    w = pd.DataFrame({
        "Open": d["Open"].resample("W-FRI").first(),
        "High": d["High"].resample("W-FRI").max(),
        "Low": d["Low"].resample("W-FRI").min(),
        "Close": d["Close"].resample("W-FRI").last(),
        "Volume": d["Volume"].resample("W-FRI").sum(),
    }).dropna(subset=["Close"])
    return w


def save_bars(df, kind, t, ndigits=4):
    cols = [c for c in ["Open", "High", "Low", "Close", "Adj Close", "Volume"] if c in df.columns]
    out = df[cols].copy()
    out.index.name = "Datetime" if kind == "hourly" else "Date"
    out = out.round(ndigits)
    out.to_csv(path("prices", kind, f"{t}.csv"))


# ------------------------------------------------------------ indicators
def wilder(series, n):
    return series.ewm(alpha=1 / n, adjust=False).mean()


def rsi(close, n=14):
    delta = close.diff()
    up = wilder(delta.clip(lower=0), n)
    down = wilder(-delta.clip(upper=0), n)
    rs = up / down.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df, n=14):
    prev = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - prev).abs(), (df["Low"] - prev).abs()], axis=1).max(axis=1)
    return wilder(tr, n)


def ret(close, days):
    if len(close) <= days:
        return None
    return float(close.iloc[-1] / close.iloc[-1 - days] - 1) * 100


def indicators(daily):
    res = {}
    spy = daily.get("SPY")
    for t, d in daily.items():
        try:
            d = d.dropna(subset=["Close"])
            c = d["Close"]
            w = to_weekly(d)
            wc = w["Close"]
            macd_line = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
            macd_sig = macd_line.ewm(span=9, adjust=False).mean()
            sma = lambda s, n: float(s.rolling(n).mean().iloc[-1]) if len(s) >= n else None
            sma40w_series = wc.rolling(40).mean()
            sma50_series = c.rolling(50).mean()
            r = {
                "lastBarDate": d.index[-1].strftime("%Y-%m-%d"),
                "bars": len(d),
                "close": float(c.iloc[-1]),
                "change1dPct": ret(c, 1),
                "open": float(d["Open"].iloc[-1]), "high": float(d["High"].iloc[-1]), "low": float(d["Low"].iloc[-1]),
                "sma20": sma(c, 20), "sma50": sma(c, 50), "sma200": sma(c, 200),
                "sma50Rising": bool(sma50_series.iloc[-1] > sma50_series.iloc[-11]) if len(c) >= 61 else None,
                "weeklyClose": float(wc.iloc[-1]), "weeklyBarDate": w.index[-1].strftime("%Y-%m-%d"),
                "weekComplete": bool(d.index[-1].weekday() == 4),
                "sma10w": sma(wc, 10), "sma40w": sma(wc, 40),
                "sma40wRising": bool(sma40w_series.iloc[-1] > sma40w_series.iloc[-5]) if len(wc) >= 45 else None,
                "rsi14": float(rsi(c).iloc[-1]),
                "macd": float(macd_line.iloc[-1]), "macdSignal": float(macd_sig.iloc[-1]),
                "macdHist": float(macd_line.iloc[-1] - macd_sig.iloc[-1]),
                "macdCrossUp": bool(macd_line.iloc[-1] > macd_sig.iloc[-1] and macd_line.iloc[-2] <= macd_sig.iloc[-2]),
                "macdCrossDown": bool(macd_line.iloc[-1] < macd_sig.iloc[-1] and macd_line.iloc[-2] >= macd_sig.iloc[-2]),
                "atr14": float(atr(d).iloc[-1]),
                "weeklyAtr14": float(atr(w).iloc[-1]) if len(w) > 15 else None,
                "avgVol20": float(d["Volume"].iloc[-21:-1].mean()) if "Volume" in d else None,
                "volRatio": float(d["Volume"].iloc[-1] / d["Volume"].iloc[-21:-1].mean()) if d["Volume"].iloc[-21:-1].mean() else None,
                "ret1m": ret(c, 21), "ret3m": ret(c, 63), "ret6m": ret(c, 126), "ret12m": ret(c, 252),
                "high52w": float(d["High"].iloc[-252:].max()), "low52w": float(d["Low"].iloc[-252:].min()),
                "high20d": float(d["High"].iloc[-21:-1].max()), "low20d": float(d["Low"].iloc[-21:-1].min()),
                "high50d": float(d["High"].iloc[-51:-1].max()), "low50d": float(d["Low"].iloc[-51:-1].min()),
                "hv20": float(np.log(c).diff().iloc[-20:].std() * math.sqrt(252) * 100),
            }
            r["pctFrom52wHigh"] = (r["close"] / r["high52w"] - 1) * 100
            r["pctFromSma40w"] = (r["weeklyClose"] / r["sma40w"] - 1) * 100 if r["sma40w"] else None
            if spy is not None and t != "SPY":
                sc = spy["Close"].reindex(c.index).ffill()
                for k, n in (("rs3m", 63), ("rs6m", 126), ("rs12m", 252)):
                    a, b = ret(c, n), ret(sc, n)
                    r[k] = None if a is None or b is None else a - b
            res[t] = r
        except Exception as e:
            err("indicators", t, f"{e}")
    # rank 6-month relative strength across the watchlist (100 = strongest)
    vals = {t: r.get("ret6m") for t, r in res.items() if r.get("ret6m") is not None}
    ordered = sorted(vals, key=vals.get)
    for i, t in enumerate(ordered):
        res[t]["rs6mRankPct"] = round(100 * (i + 1) / len(ordered), 1)
    for t, r in res.items():
        above = r.get("sma40w") and r["weeklyClose"] > r["sma40w"] and r.get("sma40wRising")
        top = (r.get("rs6mRankPct") or 0) >= 66.7
        r["trendScoreHint"] = int(bool(above)) + int(bool(top))
    return res


# ---------------------------------------------------------------- options
def bs_delta(S, K, T, iv, call):
    if not iv or iv <= 0 or T <= 0 or not S or not K:
        return None
    d1 = (math.log(S / K) + (RISK_FREE + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    n = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    return n if call else n - 1


def pick_expiries(exps):
    today = NOW.date()
    dte = [(e, (dt.date.fromisoformat(e) - today).days) for e in exps]
    dte = [x for x in dte if x[1] >= 2]
    if not dte:
        return []
    chosen = [x[0] for x in dte[:2]]
    for target in (30, 90, 150, 400):
        e = min(dte, key=lambda x: abs(x[1] - target))[0]
        if e not in chosen:
            chosen.append(e)
    return sorted(chosen)


def options(ind, earnings):
    iv_hist_path = path("options", "iv_history.csv")
    hist = pd.read_csv(iv_hist_path) if os.path.exists(iv_hist_path) else pd.DataFrame(columns=["date", "ticker", "iv30"])
    today = NOW.strftime("%Y-%m-%d")
    hist = hist[hist["date"] != today]
    summary, new_rows = {}, []
    for t in TICKERS:
        try:
            tk = yf.Ticker(t)
            exps = list(tk.options or [])
            if not exps:
                err("options", t, "no listed options")
                continue
            spot = ind.get(t, {}).get("close")
            chains, atm = {}, []
            all_calls_v = all_puts_v = all_calls_oi = all_puts_oi = 0
            unusual = []
            for e in pick_expiries(exps):
                ch = tk.option_chain(e)
                T = max((dt.date.fromisoformat(e) - NOW.date()).days, 1) / 365
                rows = []
                for side, df in (("C", ch.calls), ("P", ch.puts)):
                    df = df.copy()
                    all_v = df["volume"].fillna(0).sum()
                    all_oi = df["openInterest"].fillna(0).sum()
                    if side == "C":
                        all_calls_v += all_v; all_calls_oi += all_oi
                    else:
                        all_puts_v += all_v; all_puts_oi += all_oi
                    if spot:
                        df = df[(df["strike"] >= spot * 0.75) & (df["strike"] <= spot * 1.25)]
                    for _, o in df.iterrows():
                        iv = o.get("impliedVolatility")
                        vol, oi = o.get("volume"), o.get("openInterest")
                        row = {"side": side, "strike": o["strike"], "bid": o.get("bid"), "ask": o.get("ask"),
                               "last": o.get("lastPrice"), "volume": vol, "openInterest": oi, "iv": iv,
                               "delta": bs_delta(spot, o["strike"], T, iv, side == "C") if spot else None}
                        rows.append(row)
                        if vol and vol >= 500 and (oi or 0) * 2 < vol:
                            unusual.append({"expiry": e, **row})
                chains[e] = rows
                if spot:
                    near = [r for r in rows if r["iv"] and r["iv"] > 0.01]
                    if near:
                        k = min(near, key=lambda r: abs(r["strike"] - spot))["strike"]
                        ivs = [r["iv"] for r in near if r["strike"] == k]
                        straddle = [((r["bid"] or 0) + (r["ask"] or 0)) / 2 for r in rows if r["strike"] == k]
                        atm.append({"expiry": e, "dte": round(T * 365), "strike": k, "atmIv": float(np.mean(ivs)),
                                    "straddleMid": float(sum(straddle)) if len(straddle) == 2 else None})
            write_json({"ticker": t, "spot": spot, "asOf": NOW.isoformat(), "chains": chains}, "options", "chains", f"{t}.json")
            iv30 = None
            if atm:
                iv30 = min(atm, key=lambda a: abs(a["dte"] - 30))["atmIv"]
            th = hist[hist["ticker"] == t]["iv30"].dropna().tolist() + ([iv30] if iv30 else [])
            ivr = ivp = None
            if iv30 and len(th) >= 20:
                lo, hi = min(th), max(th)
                ivr = (iv30 - lo) / (hi - lo) * 100 if hi > lo else None
                ivp = sum(1 for x in th if x <= iv30) / len(th) * 100
            hv = ind.get(t, {}).get("hv20")
            s = {"spot": spot, "iv30": iv30, "hv20": hv, "ivToHv": (iv30 * 100 / hv) if iv30 and hv else None,
                 "ivRank": ivr, "ivPercentile": ivp, "ivHistoryDays": len(th),
                 "putCallVolume": (all_puts_v / all_calls_v) if all_calls_v else None,
                 "putCallOI": (all_puts_oi / all_calls_oi) if all_calls_oi else None,
                 "atmByExpiry": atm,
                 "unusual": sorted(unusual, key=lambda u: -(u["volume"] or 0))[:10]}
            ed = earnings.get(t, {}).get("next")
            if ed and spot:
                after = [a for a in atm if a["expiry"] >= ed and a["straddleMid"]]
                if after:
                    a = after[0]
                    s["earningsImpliedMovePct"] = a["straddleMid"] / spot * 100
                    s["earningsImpliedMoveExpiry"] = a["expiry"]
                    s["nextEarnings"] = ed
            summary[t] = s
            if iv30:
                new_rows.append({"date": today, "ticker": t, "iv30": round(iv30, 5)})
            time.sleep(0.3)
        except Exception as e:
            err("options", t, e)
    hist = pd.concat([hist, pd.DataFrame(new_rows)], ignore_index=True)
    hist.to_csv(iv_hist_path, index=False)
    write_json({"asOf": NOW.isoformat(), "riskFreeForDelta": RISK_FREE,
                "note": "ivRank/ivPercentile need 20+ days of history; history builds one row per ticker per day.",
                "tickers": summary}, "options", "summary.json")


# ------------------------------------------------------------ fundamentals
def frame_rows(df, wanted, n=8):
    if df is None or df.empty:
        return {}
    out = {}
    for name in wanted:
        if name in df.index:
            s = df.loc[name].dropna().iloc[:n]
            out[name] = {pd.Timestamp(k).strftime("%Y-%m-%d"): float(v) for k, v in s.items()}
    return out


INFO_KEYS = ["quoteType", "longName", "sector", "industry", "marketCap", "enterpriseValue", "trailingPE", "forwardPE",
             "pegRatio", "priceToSalesTrailing12Months", "priceToBook", "enterpriseToEbitda", "enterpriseToRevenue",
             "profitMargins", "grossMargins", "operatingMargins", "returnOnEquity", "revenueGrowth", "earningsGrowth",
             "earningsQuarterlyGrowth", "totalCash", "totalDebt", "freeCashflow", "operatingCashflow", "debtToEquity",
             "dividendYield", "beta", "sharesOutstanding", "heldPercentInsiders", "heldPercentInstitutions",
             "shortPercentOfFloat", "shortRatio", "targetMeanPrice", "targetHighPrice", "targetLowPrice",
             "recommendationMean", "numberOfAnalystOpinions", "trailingEps", "forwardEps", "category", "totalAssets",
             "fundFamily", "yield", "netExpenseRatio"]


def fundamentals():
    earnings = {}
    for t in TICKERS:
        try:
            tk = yf.Ticker(t)
            info = tk.info or {}
            f = {"ticker": t, "asOf": NOW.isoformat(), "info": {k: info.get(k) for k in INFO_KEYS if k in info}}
            is_fund = info.get("quoteType") in ("ETF", "MUTUALFUND")
            if not is_fund:
                f["quarterlyIncome"] = frame_rows(tk.quarterly_income_stmt, ["Total Revenue", "Gross Profit", "Operating Income", "Net Income", "Diluted EPS"])
                f["quarterlyCashflow"] = frame_rows(tk.quarterly_cashflow, ["Operating Cash Flow", "Capital Expenditure", "Free Cash Flow"])
                f["quarterlyBalance"] = frame_rows(tk.quarterly_balance_sheet, ["Total Debt", "Cash And Cash Equivalents", "Stockholders Equity"], n=4)
                try:
                    ed = tk.get_earnings_dates(limit=12)
                    if ed is not None and not ed.empty:
                        rows = []
                        for k, v in ed.iterrows():
                            rows.append({"date": pd.Timestamp(k).strftime("%Y-%m-%d"), "epsEstimate": v.get("EPS Estimate"),
                                         "epsReported": v.get("Reported EPS"), "surprisePct": v.get("Surprise(%)")})
                        f["earningsDates"] = rows
                        future = sorted(r["date"] for r in rows if r["date"] >= NOW.strftime("%Y-%m-%d"))
                        earnings[t] = {"next": future[0] if future else None, "source": "yfinance earnings dates (estimate unless the company confirmed)"}
                except Exception as e:
                    err("earningsDates", t, e)
                if t not in earnings or not earnings[t].get("next"):
                    try:
                        cal = tk.calendar or {}
                        d = cal.get("Earnings Date")
                        if d:
                            earnings[t] = {"next": pd.Timestamp(d[0]).strftime("%Y-%m-%d"), "source": "yfinance calendar"}
                    except Exception as e:
                        err("calendar", t, e)
                try:
                    tg = tk.analyst_price_targets
                    f["priceTargets"] = dict(tg) if tg else None
                except Exception:
                    pass
                try:
                    rs = tk.recommendations_summary
                    if rs is not None and not rs.empty:
                        f["recommendations"] = rs.head(4).to_dict(orient="records")
                except Exception:
                    pass
                try:
                    it = tk.insider_transactions
                    if it is not None and not it.empty:
                        f["insiderTransactionsYahoo"] = it.head(25).to_dict(orient="records")
                except Exception:
                    pass
                try:
                    ih = tk.institutional_holders
                    if ih is not None and not ih.empty:
                        f["institutionalHolders"] = ih.head(10).to_dict(orient="records")
                except Exception:
                    pass
            write_json(f, "fundamentals", f"{t}.json")
            time.sleep(0.4)
        except Exception as e:
            err("fundamentals", t, e)
    write_json({"asOf": NOW.isoformat(), "earnings": earnings}, "calendar", "earnings.json")
    return earnings


def load_earnings():
    p = path("calendar", "earnings.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f).get("earnings", {})
    return {}


# --------------------------------------------------------------- SEC Form 4
FALLBACK_CIK = {"AAPL": 320193, "MSFT": 789019, "NVDA": 1045810, "AMZN": 1018724, "GOOGL": 1652044, "META": 1326801,
                "AVGO": 1730168, "AMD": 2488, "JPM": 19617, "LLY": 59478, "UNH": 731766, "XOM": 34088, "COST": 909832,
                "CAT": 18230, "PLTR": 1321655, "GNRC": 1474735, "MU": 723125, "MRVL": 1835632, "ETN": 1551182,
                "DE": 315189, "WFC": 72971, "JNJ": 200406, "VRT": 1674101, "NFLX": 1065280, "TSLA": 1318605}
def sec_form4(days=45):
    s = requests.Session()
    s.headers.update({"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"})
    try:
        ct = s.get("https://www.sec.gov/files/company_tickers.json", timeout=30)
        ct.raise_for_status()
        cik = {v["ticker"].upper(): int(v["cik_str"]) for v in ct.json().values()}
    except Exception as e:
        err("sec", "company_tickers", f"{e}; using built-in CIK list (verified against each filer's tickers)")
        cik = dict(FALLBACK_CIK)
    cutoff = (NOW - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    out = {}
    for t in TICKERS:
        c = cik.get(t) or cik.get(t.replace(".", "-"))
        if not c:
            continue  # ETFs and foreign filers without Form 4
        try:
            sub = s.get(f"https://data.sec.gov/submissions/CIK{c:010d}.json", timeout=30)
            sub.raise_for_status()
            js = sub.json()
            if t not in [x.upper() for x in js.get("tickers", [])]:
                err("sec", t, f"CIK {c} belongs to {js.get('tickers')}, skipped")
                continue
            rec = js["filings"]["recent"]
            txs = []
            n = 0
            for form, acc, fdate, doc in zip(rec["form"], rec["accessionNumber"], rec["filingDate"], rec["primaryDocument"]):
                if fdate < cutoff:
                    break
                if form != "4" or n >= 20:
                    continue
                n += 1
                raw = doc.split("/")[-1]
                url = f"https://www.sec.gov/Archives/edgar/data/{c}/{acc.replace('-', '')}/{raw}"
                time.sleep(0.12)
                x = s.get(url, timeout=30)
                if x.status_code != 200:
                    continue
                root = ET.fromstring(x.content)
                owner = root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or ""
                rel = root.find(".//reportingOwner/reportingOwnerRelationship")
                title = []
                if rel is not None:
                    if (rel.findtext("isDirector") or "").strip() in ("1", "true"):
                        title.append("Director")
                    if (rel.findtext("isOfficer") or "").strip() in ("1", "true"):
                        title.append(rel.findtext("officerTitle") or "Officer")
                    if (rel.findtext("isTenPercentOwner") or "").strip() in ("1", "true"):
                        title.append("10% owner")
                plan = (root.findtext(".//aff10b5One") or "").strip() in ("1", "true")
                for tx in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
                    g = lambda p: (tx.findtext(p) or "").strip()
                    try:
                        sh = float(g("transactionAmounts/transactionShares/value") or 0)
                        px = float(g("transactionAmounts/transactionPricePerShare/value") or 0)
                    except ValueError:
                        sh, px = 0.0, 0.0
                    txs.append({"filed": fdate, "owner": owner, "title": ", ".join(title),
                                "date": g("transactionDate/value"), "code": g("transactionCoding/transactionCode"),
                                "acqDisp": g("transactionAmounts/transactionAcquiredDisposedCode/value"),
                                "shares": sh, "price": px, "value": round(sh * px, 2),
                                "ownedAfter": g("postTransactionAmounts/sharesOwnedFollowingTransaction/value"),
                                "rule10b5_1": plan, "url": url})
            buys = [x for x in txs if x["code"] == "P"]
            out[t] = {"cik": c, "transactions": txs,
                      "openMarketBuys": len(buys), "openMarketBuyValue": round(sum(x["value"] for x in buys), 2),
                      "distinctBuyers": len({x["owner"] for x in buys}),
                      "sales": len([x for x in txs if x["code"] == "S"])}
            time.sleep(0.12)
        except Exception as e:
            if "403" in str(e):
                errors.setdefault("sec", {}).clear()
                err("sec", "*", "SEC blocks requests from GitHub's servers; use insiderTransactionsYahoo in fundamentals/T.json")
                return
            err("sec", t, e)
    write_json({"asOf": NOW.isoformat(), "windowDays": days,
                "codes": {"P": "open-market purchase", "S": "open-market sale", "A": "grant/award", "M": "option exercise",
                          "F": "tax withholding", "G": "gift"}, "tickers": out}, "sec", "form4.json")


# --------------------------------------------------------------- screener
UNIVERSE_URLS = [
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
]
WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def load_universe():
    cache = path("screener", "sp500_constituents.csv")
    df = None
    for u in UNIVERSE_URLS:
        try:
            df = pd.read_csv(u)
            if len(df) < 450:
                raise ValueError(f"only {len(df)} rows")
            break
        except Exception as e:
            err("universe", u, e)
            df = None
    if df is None:
        try:
            html = requests.get(WIKI_URL, headers={"User-Agent": SEC_UA}, timeout=30).text
            df = pd.read_html(html)[0]
        except Exception as e:
            err("universe", "wikipedia", e)
    if df is None and os.path.exists(cache):
        df = pd.read_csv(cache)
        err("universe", "cache", "using the cached constituent list")
    if df is None:
        return pd.DataFrame(columns=["Symbol", "Security", "GICS Sector", "GICS Sub-Industry", "yf"])
    for c in ("Security", "GICS Sector", "GICS Sub-Industry"):
        if c not in df.columns:
            df[c] = None
    df["yf"] = df["Symbol"].astype(str).str.strip().str.replace(".", "-", regex=False)
    df = df[["Symbol", "Security", "GICS Sector", "GICS Sub-Industry", "yf"]].drop_duplicates("yf")
    df.to_csv(cache, index=False)
    return df


def accumulation_ratio(d, n=50):
    x = d.iloc[-n:]
    chg = x["Close"].diff()
    up = x["Volume"][chg > 0].sum()
    down = x["Volume"][chg < 0].sum()
    return float(up / down) if down else None


def run_screener():
    uni = load_universe()
    names = uni.set_index("yf").to_dict(orient="index")
    tickers = list(dict.fromkeys(["SPY"] + list(uni["yf"]) + list(SECTORS)))
    daily = download_daily(tickers, "2y", "screenDaily", chunk=100, retry_limit=40)
    ind = indicators(daily)  # ranks relative strength across the whole universe
    rows = []
    for t, r in ind.items():
        d = daily[t]
        try:
            dollar_vol = float((d["Close"] * d["Volume"]).iloc[-21:-1].mean())
        except Exception:
            dollar_vol = None
        wc = to_weekly(d)["Close"]
        sma40 = wc.rolling(40).mean()
        reclaim40w = bool(len(wc) > 41 and wc.iloc[-1] > sma40.iloc[-1] and wc.iloc[-2] <= sma40.iloc[-2])
        acc = accumulation_ratio(d) if "Volume" in d else None
        rows.append({"ticker": t, **{k: r.get(k) for k in (
            "lastBarDate", "close", "sma50", "sma200", "sma10w", "sma40w", "sma40wRising", "sma50Rising", "rsi14",
            "macdHist", "atr14", "volRatio", "ret1m", "ret3m", "ret6m", "ret12m", "rs3m", "rs6m", "rs6mRankPct",
            "high50d", "pctFrom52wHigh", "hv20")},
            "avgDollarVol20": dollar_vol, "accumulation50d": acc, "reclaim40w": reclaim40w})
    df = pd.DataFrame(rows).set_index("ticker")
    df["rs3mRankPct"] = df["ret3m"].rank(pct=True) * 100

    def score(r):
        trend = int(bool(r.sma40w and r.close > r.sma40w and r.sma40wRising)) + int((r.rs6mRankPct or 0) >= 66.7)
        breakout = bool(r.high50d and r.close > r.high50d and (r.volRatio or 0) >= 1.5)
        pullback = bool(r.sma50Rising and r.atr14 and r.sma50 and 0 <= (r.close - r.sma50) <= 1.5 * r.atr14) or \
            bool(r.sma10w and r.atr14 and 0 <= (r.close - r.sma10w) <= 1.0 * r.atr14 and r.close > r.sma40w)
        setup = int(breakout or pullback)
        momentum = int(50 <= (r.rsi14 or 0) <= 70 and (r.macdHist or 0) > 0)
        accum = int((r.accumulation50d or 0) >= 1.2)
        rs3 = int((r.rs3mRankPct or 0) >= 80)
        flags = [f for f, on in (("breakout", breakout), ("pullback", pullback), ("reclaim40w", r.reclaim40w)) if on]
        return pd.Series({"screenScore": trend + setup + momentum + accum + rs3, "trend": trend, "setup": setup,
                          "momentum": momentum, "accumulation": accum, "rs3mTop": rs3, "flags": ",".join(flags)})

    df = df.join(df.apply(score, axis=1))
    df["liquid"] = (df["close"] >= MIN_PRICE) & (df["avgDollarVol20"].fillna(0) >= MIN_DOLLAR_VOL)
    for col, key in (("name", "Security"), ("sector", "GICS Sector"), ("subIndustry", "GICS Sub-Industry")):
        df[col] = [names.get(t, {}).get(key) if t in names else (SECTORS.get(t) if col == "name" else None) for t in df.index]
    df.round(4).to_csv(path("screener", "universe_scores.csv"))

    # shortlist with continuity: members stay while they still score 3+
    prev_path = path("screener", "shortlist.json")
    prev = {}
    if os.path.exists(prev_path):
        with open(prev_path) as f:
            prev = {x["ticker"]: x for x in json.load(f).get("shortlist", [])}
    excluded = set(CORE) | set(SECTORS)
    pool = df[df["liquid"] & ~df.index.isin(excluded) & df.index.isin(list(uni["yf"]))]
    pool = pool.sort_values(["screenScore", "rs6mRankPct"], ascending=False)
    keep = [t for t in prev if t in pool.index and pool.loc[t, "screenScore"] >= 3]
    new = [t for t in pool[pool["screenScore"] >= 4].index if t not in keep]
    chosen = (keep + new)[:SHORTLIST_SIZE] if len(keep) < SHORTLIST_SIZE else \
        sorted(keep, key=lambda t: (-pool.loc[t, "screenScore"], -(pool.loc[t, "rs6mRankPct"] or 0)))[:SHORTLIST_SIZE]
    alerts = [t for t in pool.index if t not in chosen and pool.loc[t, "screenScore"] >= 3
              and any(f in (pool.loc[t, "flags"] or "") for f in ("breakout", "reclaim40w"))]
    chosen += alerts[:SHORTLIST_MAX - len(chosen)]
    today = NOW.strftime("%Y-%m-%d")
    items = []
    for t in chosen:
        r = pool.loc[t]
        reasons = []
        if r.trend >= 1:
            reasons.append(f"weekly close {r.close:.2f} vs 40-week avg {r.sma40w:.2f}" + (" (rising)" if r.sma40wRising else ""))
        reasons.append(f"6-month strength rank {r.rs6mRankPct:.0f}/100 in the S&P 500")
        if "breakout" in (r["flags"] or ""):
            reasons.append(f"closed above its 50-day high {r.high50d:.2f} on {r.volRatio:.1f}x volume")
        if "pullback" in (r["flags"] or ""):
            reasons.append(f"pulled back near support (50-day avg {r.sma50:.2f}, ATR {r.atr14:.2f})")
        if "reclaim40w" in (r["flags"] or ""):
            reasons.append("reclaimed its 40-week average this week")
        if r.accumulation:
            reasons.append(f"up-day volume {r.accumulation50d:.2f}x down-day volume over 50 days")
        items.append({
            "ticker": t, "name": r["name"], "sector": r["sector"], "subIndustry": r["subIndustry"],
            "screenScore": int(r.screenScore), "breakdown": {k: int(r[k]) for k in ("trend", "setup", "momentum", "accumulation", "rs3mTop")},
            "flags": [f for f in (r["flags"] or "").split(",") if f], "reasons": reasons,
            "close": r.close, "sma50": r.sma50, "sma40w": r.sma40w, "atr14": r.atr14, "rsi14": r.rsi14,
            "rs6mRankPct": r.rs6mRankPct, "ret3m": r.ret3m, "ret6m": r.ret6m, "pctFrom52wHigh": r.pctFrom52wHigh,
            "avgDollarVol20": r.avgDollarVol20, "lastBarDate": r.lastBarDate,
            "addedOn": prev.get(t, {}).get("addedOn", today), "alert": t in alerts,
        })
    dropped = [t for t in prev if t not in chosen]
    write_json({"asOf": NOW.isoformat(), "universe": "S&P 500", "universeSize": int(len(uni)),
                "screened": int(len(df)), "liquidPassed": int(len(pool)),
                "rules": {"minPrice": MIN_PRICE, "minAvgDollarVolume20d": MIN_DOLLAR_VOL,
                          "score": "0-6: trend 0-2 (weekly close above a rising 40-week avg; 6-month strength top third), setup 0-1 (50-day-high breakout on 1.5x volume, or pullback within 1.5 ATR of a rising 50-day avg or 1 ATR of the 10-week avg), momentum 0-1 (RSI 50-70 and MACD histogram > 0), accumulation 0-1 (50-day up/down volume >= 1.2), rs3mTop 0-1 (3-month return in the top 20%)",
                          "membership": f"top {SHORTLIST_SIZE} new names score 4+; current members stay while they score 3+; up to {SHORTLIST_MAX - SHORTLIST_SIZE} breakout or 40-week reclaim alerts at 3+; core watchlist and sector ETFs excluded (always covered)"},
                "shortlist": items, "droppedToday": dropped}, "screener", "shortlist.json")

    # sector view: breadth by GICS sector and sector ETF leadership
    breadth = {}
    members = df[df.index.isin(list(uni["yf"]))]
    for sec, g in members.groupby("sector"):
        if not sec:
            continue
        breadth[sec] = {"count": int(len(g)),
                        "pctAbove50d": float((g["close"] > g["sma50"]).mean() * 100),
                        "pctAbove200d": float((g["close"] > g["sma200"]).mean() * 100),
                        "pctAbove40w": float((g["close"] > g["sma40w"]).mean() * 100),
                        "medianRet3m": float(g["ret3m"].median()), "medianRs6mRank": float(g["rs6mRankPct"].median()),
                        "avgScreenScore": float(g["screenScore"].mean())}
    etfs = []
    for t, nm in SECTORS.items():
        if t in df.index:
            r = df.loc[t]
            etfs.append({"ticker": t, "name": nm, "close": r.close, "ret1m": r.ret1m, "ret3m": r.ret3m, "ret6m": r.ret6m,
                         "rs3m": r.rs3m, "rs6m": r.rs6m, "above40w": bool(r.sma40w and r.close > r.sma40w),
                         "sma40wRising": r.sma40wRising, "screenScore": int(r.screenScore)})
        else:
            err("sectors", t, "no data")
    etfs.sort(key=lambda x: -(x["rs3m"] if x["rs3m"] is not None else -999))
    for i, e in enumerate(etfs):
        e["rs3mRank"] = i + 1
    all_m = members
    write_json({"asOf": NOW.isoformat(),
                "market": {"pctAbove50d": float((all_m["close"] > all_m["sma50"]).mean() * 100),
                           "pctAbove200d": float((all_m["close"] > all_m["sma200"]).mean() * 100),
                           "pctAbove40w": float((all_m["close"] > all_m["sma40w"]).mean() * 100),
                           "newHighs": int((all_m["pctFrom52wHigh"] >= -1).sum())},
                "gicsBreadth": breadth, "sectorEtfs": etfs}, "screener", "sectors.json")
    ranks = df["rs6mRankPct"].to_dict()
    return [x["ticker"] for x in items], ranks, len(uni), len([t for t in uni["yf"] if t in daily])


def load_shortlist():
    p = path("screener", "shortlist.json")
    if os.path.exists(p):
        with open(p) as f:
            return [x["ticker"] for x in json.load(f).get("shortlist", [])]
    return []


def add_earnings_to_shortlist(earnings):
    p = path("screener", "shortlist.json")
    if not os.path.exists(p):
        return
    with open(p) as f:
        sl = json.load(f)
    for x in sl.get("shortlist", []):
        x["nextEarnings"] = (earnings.get(x["ticker"]) or {}).get("next")
    write_json(sl, "screener", "shortlist.json")


# ------------------------------------------------------------------- main
def main():
    global TICKERS
    started = time.time()
    sections = {}
    do_screen = DO_FULL or age_hours("screener") > 20 or not os.path.exists(path("screener", "shortlist.json"))
    do_full = DO_FULL or age_hours("fundamentals") > 26
    do_options = DO_OPTIONS or do_full or age_hours("options") > 6
    universe_ranks, uni_size, uni_with_data = {}, None, None
    if do_screen:
        try:
            shortlist, universe_ranks, uni_size, uni_with_data = run_screener()
            sections["screener"] = NOW.isoformat()
        except Exception:
            err("screener", "*", traceback.format_exc()[-1200:])
            shortlist = load_shortlist()
    else:
        shortlist = load_shortlist()
    TICKERS = list(dict.fromkeys(CORE + list(SECTORS) + shortlist))
    groups = {t: ("core" if t in CORE else "sector" if t in SECTORS else "shortlist") for t in TICKERS}
    daily, hourly = get_prices()
    for t, d in daily.items():
        save_bars(d, "daily", t)
        save_bars(to_weekly(d), "weekly", t)
    for t, h in hourly.items():
        save_bars(h, "hourly", t)
    ind = indicators(daily)
    if not universe_ranks:
        try:
            sc = pd.read_csv(path("screener", "universe_scores.csv"), index_col=0)
            universe_ranks = sc["rs6mRankPct"].to_dict()
        except Exception:
            universe_ranks = {}
    for t, r in ind.items():
        r["group"] = groups.get(t)
        if SECTORS.get(t):
            r["sectorName"] = SECTORS[t]
        u = universe_ranks.get(t)
        if u is not None and not (isinstance(u, float) and math.isnan(u)):
            r["rs6mRankWatchlist"] = r.get("rs6mRankPct")
            r["rs6mRankPct"] = float(u)  # rank within the S&P 500 (100 = strongest)
            above = r.get("sma40w") and r["weeklyClose"] > r["sma40w"] and r.get("sma40wRising")
            r["trendScoreHint"] = int(bool(above)) + int(r["rs6mRankPct"] >= 66.7)
    write_json({"asOf": NOW.isoformat(), "spyClose": ind.get("SPY", {}).get("close"),
                "rs6mRankBasis": "S&P 500 universe where screened, otherwise this file's tickers",
                "tickers": ind}, "indicators", "latest.json")
    sections["prices"] = NOW.isoformat()
    earnings = load_earnings()
    if do_full:
        try:
            earnings = fundamentals()
            add_earnings_to_shortlist(earnings)
            sections["fundamentals"] = NOW.isoformat()
        except Exception:
            err("fundamentals", "*", traceback.format_exc())
        try:
            sec_form4()
            sections["sec"] = NOW.isoformat()
        except Exception:
            err("sec", "*", traceback.format_exc())
    if do_options:
        try:
            options(ind, earnings)
            sections["options"] = NOW.isoformat()
        except Exception:
            err("options", "*", traceback.format_exc())
    mp = path("manifest.json")
    prev = {}
    if os.path.exists(mp):
        with open(mp) as f:
            prev = json.load(f)
    updated = {**prev.get("sectionsUpdatedAt", {}), **sections}
    # data health: flags the kind of throttling or outage that would call for a paid feed
    issues = []
    n_cov = len(TICKERS)
    miss_cov = sorted(set(TICKERS) - set(daily))
    if len(miss_cov) > max(2, 0.03 * n_cov):
        issues.append(f"{len(miss_cov)} of {n_cov} covered tickers have no daily prices")
    if uni_size:
        miss_pct = 100 * (1 - (uni_with_data or 0) / uni_size)
        if miss_pct > 5:
            issues.append(f"{miss_pct:.0f}% of the S&P 500 had no data in the screen")
    if do_screen and "screener" not in sections:
        issues.append("the S&P 500 screen failed; agents are using the previous shortlist")
    if len(set(TICKERS) - set(hourly)) > 0.1 * n_cov:
        issues.append(f"hourly bars missing for {len(set(TICKERS) - set(hourly))} of {n_cov} tickers")
    if do_options and len(errors.get("options", {})) > 0.1 * n_cov + 2:
        issues.append(f"options failed for {len(errors['options'])} of {n_cov} tickers")
    if do_full and len(errors.get("fundamentals", {})) > 0.1 * n_cov:
        issues.append(f"fundamentals failed for {len(errors['fundamentals'])} of {n_cov} tickers")
    for sec_name, hrs in (("options", 30), ("fundamentals", 54), ("screener", 54)):
        ts = updated.get(sec_name)
        if ts and (NOW - dt.datetime.fromisoformat(ts)).total_seconds() / 3600 > hrs:
            issues.append(f"{sec_name} data is more than {hrs} hours old")
    write_json({
        "generatedAt": NOW.isoformat(), "mode": MODE, "runSeconds": round(time.time() - started),
        "ranSections": sorted(sections),
        "dataHealth": {"status": "degraded" if issues else "ok", "issues": issues},
        "coverage": {"core": len(CORE), "sectorEtfs": len(SECTORS), "shortlist": len(shortlist), "total": n_cov},
        "groups": groups,
        "tickers": TICKERS,
        "tickersWithDaily": sorted(daily), "tickersMissingDaily": sorted(set(TICKERS) - set(daily)),
        "lastBarDates": {t: r["lastBarDate"] for t, r in ind.items()},
        "sectionsUpdatedAt": updated,
        "errors": errors,
    }, "manifest.json")
    print(json.dumps({"mode": MODE, "sections": sorted(sections), "covered": n_cov, "daily": len(daily),
                      "hourly": len(hourly), "shortlist": len(shortlist), "health": issues,
                      "errors": {k: len(v) for k, v in errors.items()}}))


if __name__ == "__main__":
    main()
