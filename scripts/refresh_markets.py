#!/usr/bin/env python3
"""Rebuild markets-data.js for the Markets tab. Standard library only.

  python3 scripts/refresh_markets.py

Sources
  Index levels and share prices   Yahoo Finance chart endpoint (daily closes of finished sessions only)
  Index membership and weights    Daily holdings of SPY and DIA (State Street) and QQQ (Invesco)
  GICS sector and sub-industry    Wikipedia's S&P 500 constituent table; GICS_EXTRA below for the rest
  VIX and MOVE                    Yahoo Finance chart endpoint (daily closes)
  Credit spreads                  ICE BofA option-adjusted spreads, via FRED (each rating keeps its daily history)
"""
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "markets-data.js")
SETTLE = 15 * 60  # seconds after the closing bell before a session counts as finished
EASTERN = ZoneInfo("America/New_York")

INDICES = [
    # id, Yahoo symbol, name, holdings key, label for the constituent set
    ("sp500", "^GSPC", "S&P 500", "spy", "S&P 500"),
    ("nasdaq", "^IXIC", "Nasdaq Composite", "qqq", "Nasdaq-100"),
    ("dow", "^DJI", "Dow Jones Industrial Average", "dia", "Dow 30"),
]
EXTRA_INDEX = [("ndx", "^NDX", "Nasdaq-100")]
VOLATILITY = [
    ("vix", "^VIX", "VIX", "Expected S&P 500 volatility over the next 30 days, from option prices"),
    ("move", "^MOVE", "MOVE Index", "Expected Treasury yield volatility over the next 30 days, from option prices"),
]

SECTORS = ["Information Technology", "Communication Services", "Consumer Discretionary", "Consumer Staples",
           "Health Care", "Financials", "Industrials", "Energy", "Materials", "Utilities", "Real Estate"]

# GICS sector and sub-industry for Nasdaq-100 members that are not in the S&P 500.
GICS_EXTRA = {
    "ALAB": {"sector": "Information Technology", "sub": "Semiconductors"},
    "ALNY": {"sector": "Health Care", "sub": "Biotechnology"},
    "ARM": {"sector": "Information Technology", "sub": "Semiconductors"},
    "ASML": {"sector": "Information Technology", "sub": "Semiconductor Materials & Equipment"},
    "CCEP": {"sector": "Consumer Staples", "sub": "Soft Drinks & Non-alcoholic Beverages"},
    "CRWV": {"sector": "Information Technology", "sub": "Internet Services & Infrastructure"},
    "FER": {"sector": "Industrials", "sub": "Construction & Engineering"},
    "MELI": {"sector": "Consumer Discretionary", "sub": "Broadline Retail"},
    "MSTR": {"sector": "Information Technology", "sub": "Application Software"},
    "NBIS": {"sector": "Information Technology", "sub": "Internet Services & Infrastructure"},
    "PDD": {"sector": "Consumer Discretionary", "sub": "Broadline Retail"},
    "RKLB": {"sector": "Industrials", "sub": "Aerospace & Defense"},
    "SHOP": {"sector": "Information Technology", "sub": "Internet Services & Infrastructure"},
    "SPCX": {"sector": "Industrials", "sub": "Aerospace & Defense"},
    "TRI": {"sector": "Industrials", "sub": "Research & Consulting Services"},
}

SPREADS = [
    ("hy", "BAMLH0A0HYM2", "High yield"),
    ("ig", "BAMLC0A0CM", "Investment grade"),
    ("aaa", "BAMLC0A1CAAA", "AAA"), ("aa", "BAMLC0A2CAA", "AA"), ("a", "BAMLC0A3CA", "A"), ("bbb", "BAMLC0A4CBBB", "BBB"),
    ("bb", "BAMLH0A1HYBB", "BB"), ("b", "BAMLH0A2HYB", "B"), ("ccc", "BAMLH0A3HYC", "CCC and lower"),
]


def get(url, agent="Mozilla/5.0", tries=4, accept=None):
    last = None
    for i in range(tries):
        try:
            h = {"User-Agent": agent}
            if accept:
                h["Accept"] = accept
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=45) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def xlsx_rows(blob):
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z = zipfile.ZipFile(io.BytesIO(blob))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(ns + "si"):
            shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
    sheet = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]
    for row in ET.fromstring(z.read(sheet)).iter(ns + "row"):
        out = []
        for c in row.findall(ns + "c"):
            v = c.find(ns + "v")
            if c.get("t") == "s" and v is not None:
                out.append(shared[int(v.text)])
            elif c.get("t") == "inlineStr":
                out.append("".join(t.text or "" for t in c.iter(ns + "t")))
            else:
                out.append(v.text if v is not None else "")
        yield out


def ssga_holdings(ticker):
    url = f"https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{ticker}.xlsx"
    rows = list(xlsx_rows(get(url)))
    asof = next((r[1] for r in rows if r and r[0].startswith("Holdings")), "")
    start = next(i for i, r in enumerate(rows) if r[:2] == ["Name", "Ticker"])
    out = {}
    for r in rows[start + 1:]:
        if len(r) < 5 or not r[1] or r[1] == "-" or not re.fullmatch(r"[A-Z.]+", r[1]):
            continue
        out[r[1]] = {"name": r[0].title(), "w": float(r[4])}
    return out, asof.replace("As of ", "")


def invesco_holdings():
    url = ("https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/QQQ/holdings/fund"
           "?idType=ticker&interval=monthly&productType=ETF")
    d = json.loads(get(url, accept="application/json"))
    out = {}
    for h in d["holdings"]:
        t = (h.get("ticker") or "").strip()
        kind = h.get("securityTypeName") or ""
        if "Stock" not in kind and "Depository" not in kind:
            continue
        if not re.fullmatch(r"[A-Z.]+", t):
            continue
        out[t] = {"name": h["issuerName"], "w": float(h["percentageOfTotalNetAssets"])}
    return out, d.get("effectiveBusinessDate", "")


def wiki_gics():
    page = get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", agent="us-economy-monitor/1.0").decode()
    table = page[page.index('id="constituents"'):]
    table = table[:table.index("</table>")]
    out = {}
    text = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) >= 4:
            out[text(cells[0])] = {"name": text(cells[1]), "sector": text(cells[2]), "sub": text(cells[3])}
    return out


def yahoo(symbol, rng):
    s = urllib.request.quote(symbol.replace(".", "-"))
    d = json.loads(get(f"https://query2.finance.yahoo.com/v8/finance/chart/{s}?range={rng}&interval=1d"))
    r = d["chart"]["result"][0]
    closes = r["indicators"]["quote"][0]["close"]
    # While a session is still trading, Yahoo's last daily bar holds the price so far, not a close.
    # It is left out, so every figure the page calls a close comes from a finished session.
    reg = (r["meta"].get("currentTradingPeriod") or {}).get("regular") or {}
    start, end = reg.get("start"), reg.get("end")
    if not (start and end):  # no session times in the reply: assume today's regular session, 9:30 am to 4:00 pm Eastern
        day = dt.datetime.fromtimestamp(time.time(), EASTERN).replace(second=0, microsecond=0)
        start, end = day.replace(hour=9, minute=30).timestamp(), day.replace(hour=16, minute=0).timestamp()
    open_from = start if time.time() < end + SETTLE else None
    pts = []
    for t, c in zip(r["timestamp"], closes):
        if c is not None and not (open_from and t >= open_from):
            pts.append(((dt.datetime(1970, 1, 1) + dt.timedelta(seconds=t + r["meta"].get("gmtoffset", 0))).date(), float(c)))
    return pts


def on_or_before(pts, day):
    v = None
    for d, c in pts:
        if d > day:
            break
        v = c
    return v


def returns(pts, last_day):
    """Price returns, in percent, to the last close."""
    if not pts or pts[-1][0] != last_day:
        return None
    last = pts[-1][1]
    pct = lambda base: None if not base else round((last / base - 1) * 100, 2)
    back = lambda days: on_or_before(pts, last_day - dt.timedelta(days=days))
    year = [c for d, c in pts if d >= last_day - dt.timedelta(days=365)]
    return {
        "d1": pct(pts[-2][1]) if len(pts) > 1 else None,
        "w1": pct(pts[-6][1]) if len(pts) > 5 else None,
        "m1": pct(back(30)),
        "m3": pct(back(91)),
        "ytd": pct(on_or_before(pts, dt.date(last_day.year - 1, 12, 31))),
        "y1": pct(back(365)) if pts[0][0] <= last_day - dt.timedelta(days=360) else None,
        "hi": round(max(year), 2), "lo": round(min(year), 2),
    }


def weekly(pts, since):
    """Last close of each ISO week, from `since`."""
    out = {}
    for d, c in pts:
        if d >= since:
            out[d.isocalendar()[:2]] = (d, c)
    return [v for _, v in sorted(out.items())]


def fred(series):
    raw = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}", agent="python-requests/2.31").decode()
    out = []
    for line in raw.splitlines()[1:]:
        d, _, v = line.partition(",")
        try:
            out.append((d, round(float(v) * 100)))  # percent to basis points
        except ValueError:
            pass
    return out


def saved_credit():
    """Spread history already in markets-data.js, as {series id: {date: basis points}}."""
    if not os.path.exists(OUT):
        return {}
    try:
        with open(OUT) as f:
            old = json.loads(f.read().split("=", 1)[1].rstrip().rstrip(";"))
    except (OSError, ValueError, IndexError) as e:
        # Stopping here protects the saved days that FRED no longer serves.
        sys.exit(f"markets-data.js exists but cannot be read ({e}); left unchanged so saved credit history is not lost.")
    out = {}
    c = old.get("credit")
    if c:
        for i, vals in c["v"].items():
            out[i] = {d: v for d, v in zip(c["dates"], vals) if v is not None}
    else:  # earlier layout: history sat on the high yield and investment grade rows only
        for sp in old.get("spreads", []):
            if sp.get("hist"):
                out[sp["id"]] = {d: v for d, v in sp["hist"]}
    return out


def main():
    print("holdings", flush=True)
    spy, spy_asof = ssga_holdings("spy")
    dia, _ = ssga_holdings("dia")
    qqq, _ = invesco_holdings()
    held = {"spy": spy, "qqq": qqq, "dia": dia}
    gics = wiki_gics()
    for k, v in GICS_EXTRA.items():
        gics.setdefault(k, v)
    symbols = sorted(set(spy) | set(qqq) | set(dia))
    missing = [s for s in symbols if s not in gics]
    if missing:
        sys.exit("No GICS classification for: " + ", ".join(missing) + ". Add them to GICS_EXTRA.")
    bad = sorted({gics[s]["sector"] for s in symbols} - set(SECTORS))
    if bad:
        sys.exit("Unknown sector names: " + ", ".join(bad))

    print("indices", flush=True)
    idx_pts = {i: yahoo(sym, "10y") for i, sym, *_ in INDICES + EXTRA_INDEX}
    last_day = idx_pts["sp500"][-1][0]
    year_ago = last_day - dt.timedelta(days=372)

    print(f"prices for {len(symbols)} companies", flush=True)
    def fetch(sym):
        try:
            return sym, yahoo(sym, "2y")
        except Exception as e:  # noqa: BLE001
            return sym, e
    with ThreadPoolExecutor(6) as ex:
        px = dict(ex.map(fetch, symbols))
    failed = [s for s, v in px.items() if isinstance(v, Exception)]
    if len(failed) > 5:
        sys.exit(f"Price history failed for {len(failed)} companies: {failed[:10]}")

    stocks = {}
    for s in symbols:
        pts = px[s]
        r = None if isinstance(pts, Exception) else returns(pts, last_day)
        if r is None:
            print("  skipped (no price for the last session):", s)
            continue
        g = gics[s]
        stocks[s] = {
            "n": g.get("name") or (spy.get(s) or qqq.get(s) or dia.get(s))["name"],
            "s": SECTORS.index(g["sector"]), "sub": g["sub"], "p": round(pts[-1][1], 2), **r,
            "w": {k: round(held[k][s]["w"], 3) for k in held if s in held[k]},
            "h": [round(c, 2) for _, c in weekly(pts, year_ago)],
        }

    horizons = ["d1", "w1", "m1", "m3", "ytd", "y1"]
    indices = []
    for i, sym, name, key, set_name in INDICES:
        pts = idx_pts[i]
        members = [s for s in held[key] if s in stocks]
        sectors = []
        for k, sec in enumerate(SECTORS):
            ms = [s for s in members if stocks[s]["s"] == k]
            if not ms:
                continue
            row = {"s": k, "n": len(ms), "w": round(sum(stocks[m]["w"][key] for m in ms), 2)}
            for h in horizons:
                # Weight each company by its share of the index at the start of the period.
                num = den = tot = 0.0
                for m in members:
                    r = stocks[m][h]
                    if r is None:
                        continue
                    w0 = stocks[m]["w"][key] / (1 + r / 100)
                    tot += w0
                    if stocks[m]["s"] == k:
                        num += w0 * r
                        den += w0
                row[h] = round(num / den, 2) if den else None
                row[h + "_c"] = round(num / tot, 2) if tot else None  # points contributed to the index
            sectors.append(row)
        recent = [(d, c) for d, c in pts if d >= year_ago]
        older = weekly([(d, c) for d, c in pts if d < year_ago], dt.date(1900, 1, 1))
        indices.append({
            "id": i, "name": name, "symbol": sym, "set": set_name, "key": key, "members": len(members),
            "level": round(pts[-1][1], 2), **returns(pts, last_day), "sectors": sectors,
            "hist": [[d.isoformat(), round(c, 2)] for d, c in older + recent],
        })
    ndx = idx_pts["ndx"]
    extra = {"ndx": {"name": "Nasdaq-100", "level": round(ndx[-1][1], 2), **returns(ndx, last_day)}}

    print("volatility", flush=True)
    vol = []
    for i, sym, name, what in VOLATILITY:
        pts = yahoo(sym, "10y")
        day, last = pts[-1]
        ago = lambda days: on_or_before(pts, day - dt.timedelta(days=days))
        vol.append({
            "id": i, "name": name, "symbol": sym, "what": what, "asof": day.isoformat(), "latest": round(last, 2),
            "d1": round(last - pts[-2][1], 2), "m1": round(last - ago(30), 2), "y1": round(last - ago(365), 2),
            "hist": [[d.isoformat(), round(c, 2)] for d, c in pts],  # daily, so spikes survive
        })

    print("credit spreads", flush=True)
    # FRED serves a rolling three-year window for these series. Days already saved are kept,
    # so the history on the page grows past three years as the window moves on.
    saved = saved_credit()
    by_day = {}
    for i, code, name in SPREADS:
        merged = dict(saved.get(i, {}))
        merged.update(fred(code))
        if len(merged) < 200:
            sys.exit(f"Credit spread history for {name} ({code}) has only {len(merged)} days.")
        for d, v in merged.items():
            by_day.setdefault(d, {})[i] = v
    days = sorted(by_day)
    spreads = []
    for i, code, name in SPREADS:
        pts = [(d, by_day[d][i]) for d in days if i in by_day[d]]
        vals = [v for _, v in pts]
        last_d = dt.date.fromisoformat(pts[-1][0])
        prev = lambda n: next((v for d, v in reversed(pts) if dt.date.fromisoformat(d) <= last_d - dt.timedelta(days=n)), None)
        chg = lambda base: None if base is None else vals[-1] - base
        spreads.append({
            "id": i, "name": name, "code": code, "asof": pts[-1][0], "latest": vals[-1],
            "d1": vals[-1] - vals[-2], "m1": chg(prev(30)), "y1": chg(prev(365)),
            "lo": min(vals), "hi": max(vals), "med": sorted(vals)[len(vals) // 2],
            "pct": round(100 * sum(v <= vals[-1] for v in vals) / len(vals)), "from": pts[0][0],
        })
    credit = {"dates": days, "v": {i: [by_day[d].get(i) for d in days] for i, _, _ in SPREADS}}

    data = {
        "asof": last_day.isoformat(), "holdings_asof": spy_asof,
        "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sectors": SECTORS, "indices": indices, "extra": extra, "stocks": stocks, "vol": vol, "spreads": spreads, "credit": credit,
    }
    text = "const MARKETS = " + json.dumps(data, separators=(",", ":")) + ";\n"
    with open(OUT + ".tmp", "w") as f:  # written beside the live file, then swapped in whole
        f.write(text)
    os.replace(OUT + ".tmp", OUT)
    print(f"wrote {OUT}: {len(stocks)} companies, last close {last_day}, {os.path.getsize(OUT) // 1024} KB")


if __name__ == "__main__":
    main()
