"""Build the Agent Grid data pack.

Usage: python scripts/build_data.py OUT_DIR MODE
MODE: light   - daily, weekly and hourly prices plus indicators
      options - light + option chains and volatility summary
      full    - options + fundamentals, earnings calendar and SEC Form 4

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
SEC_UA = "AgentGridData enelson43@users.noreply.github.com"
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


TICKERS = read_tickers()
if "SPY" not in TICKERS:
    TICKERS.insert(0, "SPY")


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


def get_prices():
    daily = split_download(
        yf.download(TICKERS, period="3y", interval="1d", group_by="ticker", auto_adjust=False,
                    threads=True, progress=False), TICKERS)
    for t in TICKERS:
        if t not in daily:
            try:
                daily[t] = stooq_daily(t)
            except Exception as e:
                err("daily", t, f"yfinance and stooq failed: {e}")
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
            err("sec", t, e)
    write_json({"asOf": NOW.isoformat(), "windowDays": days,
                "codes": {"P": "open-market purchase", "S": "open-market sale", "A": "grant/award", "M": "option exercise",
                          "F": "tax withholding", "G": "gift"}, "tickers": out}, "sec", "form4.json")


# ------------------------------------------------------------------- main
def main():
    started = time.time()
    sections = {}
    daily, hourly = get_prices()
    for t, d in daily.items():
        save_bars(d, "daily", t)
        save_bars(to_weekly(d), "weekly", t)
    for t, h in hourly.items():
        save_bars(h, "hourly", t)
    ind = indicators(daily)
    write_json({"asOf": NOW.isoformat(), "spyClose": ind.get("SPY", {}).get("close"), "tickers": ind}, "indicators", "latest.json")
    sections["prices"] = NOW.isoformat()
    earnings = load_earnings()
    if DO_FULL:
        try:
            earnings = fundamentals()
            sections["fundamentals"] = NOW.isoformat()
        except Exception:
            err("fundamentals", "*", traceback.format_exc())
        try:
            sec_form4()
            sections["sec"] = NOW.isoformat()
        except Exception:
            err("sec", "*", traceback.format_exc())
    if DO_OPTIONS:
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
    write_json({
        "generatedAt": NOW.isoformat(), "mode": MODE, "runSeconds": round(time.time() - started),
        "tickers": TICKERS,
        "tickersWithDaily": sorted(daily), "tickersMissingDaily": sorted(set(TICKERS) - set(daily)),
        "lastBarDates": {t: r["lastBarDate"] for t, r in ind.items()},
        "sectionsUpdatedAt": updated,
        "errors": errors,
    }, "manifest.json")
    print(json.dumps({"mode": MODE, "daily": len(daily), "hourly": len(hourly), "errors": {k: len(v) for k, v in errors.items()}}))


if __name__ == "__main__":
    main()
