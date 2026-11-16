#!/usr/bin/env python3
"""Rebuild crypto-data.js for the Crypto tab. Standard library only.

  python3 scripts/refresh_crypto.py

Sources
  Price, market value, volume, supply and period changes   CoinGecko public API
  Daily closing prices (UTC) for the charts                 Yahoo Finance chart endpoint

The page also asks CoinGecko for current prices when it is opened, so this file is the fallback
and the source of the price history.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "crypto-data.js")
DAILY_DAYS = 400  # daily closes for this many days, weekly closes before that

# The largest tokens by market value. Dollar-pegged stablecoins, exchange-issued tokens and
# tokenised claims on other assets are left out. CoinGecko id, ticker, name, Yahoo symbol.
COINS = [
    ("bitcoin", "BTC", "Bitcoin", "BTC-USD"),
    ("ethereum", "ETH", "Ethereum", "ETH-USD"),
    ("solana", "SOL", "Solana", "SOL-USD"),
    ("binancecoin", "BNB", "BNB", "BNB-USD"),
    ("ripple", "XRP", "XRP", "XRP-USD"),
    ("tron", "TRX", "TRON", "TRX-USD"),
    ("hyperliquid", "HYPE", "Hyperliquid", "HYPE32196-USD"),
    ("zcash", "ZEC", "Zcash", "ZEC-USD"),
    ("monero", "XMR", "Monero", "XMR-USD"),
    ("dogecoin", "DOGE", "Dogecoin", "DOGE-USD"),
    ("chainlink", "LINK", "Chainlink", "LINK-USD"),
    ("cardano", "ADA", "Cardano", "ADA-USD"),
]


def get(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def sig(v, n=5):
    """Round to n significant figures: prices run from cents to tens of thousands of dollars."""
    return None if v is None else float(f"{v:.{n}g}")


def yahoo(symbol):
    r = get(f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range=10y&interval=1d")["chart"]["result"][0]
    closes = r["indicators"]["quote"][0]["close"]
    pts = {}
    for t, c in zip(r["timestamp"], closes):
        if c is not None:
            pts[dt.datetime.fromtimestamp(t, dt.timezone.utc).date()] = float(c)
    meta = r["meta"]
    return pts, float(meta["regularMarketPrice"]), int(meta["regularMarketTime"])


def main():
    now = dt.datetime.now(dt.timezone.utc)
    today = now.date()

    def fetch(c):
        try:
            return c[0], yahoo(c[3])
        except Exception as e:  # noqa: BLE001
            return c[0], e
    with ThreadPoolExecutor(4) as ex:
        hist = dict(ex.map(fetch, COINS))
    bad = [k for k, v in hist.items() if isinstance(v, Exception)]
    if bad:
        sys.exit(f"Price history failed for {bad}: {hist[bad[0]]}. crypto-data.js left unchanged.")

    ids = ",".join(c[0] for c in COINS)
    try:
        mk = {m["id"]: m for m in get("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids=" + ids
                                       + "&price_change_percentage=24h,7d,30d,1y")}
        glob = get("https://api.coingecko.com/api/v3/global")["data"]
    except Exception as e:  # noqa: BLE001
        print("  CoinGecko failed, using Yahoo prices only:", e)
        mk, glob = {}, None

    coins = []
    for cid, sym, name, ysym in COINS:
        pts, live, live_t = hist[cid]
        # Finished days only: today's candle is still moving and is carried as the price instead.
        days = sorted(d for d in pts if d < today)
        last = days[-1]
        if (today - last).days > 3 or len(days) < 200:
            sys.exit(f"{name}: price history ends {last} with {len(days)} days. crypto-data.js left unchanged.")
        # Fill any missing day with the close before it, so the series has one value per calendar day.
        full, d, v = [], days[0], pts[days[0]]
        while d <= last:
            v = pts.get(d, v)
            full.append(v)
            d += dt.timedelta(days=1)
        daily = full[-DAILY_DAYS:]
        older = full[:-DAILY_DAYS]
        weekly = older[::-1][6::7][::-1]  # every seventh close, ending a week before the first daily close
        d0 = last - dt.timedelta(days=len(daily) - 1)
        m = mk.get(cid, {})
        price = m.get("current_price") or live
        back = lambda n: full[-n] if len(full) >= n else None
        pc = lambda base: None if not base else round((price / base - 1) * 100, 2)
        year = full[-365:] + [price]
        ytd_base = pts.get(dt.date(today.year - 1, 12, 31))
        coins.append({
            "id": cid, "sym": sym, "name": name, "p": sig(price, 6),
            "mc": m.get("market_cap"), "vol": m.get("total_volume"), "supply": m.get("circulating_supply"), "max": m.get("max_supply"),
            "ath": m.get("ath"), "ath_d": (m.get("ath_date") or "")[:10],
            # Rolling changes from CoinGecko; where it did not answer, measured against earlier daily closes.
            "h24": round(m["price_change_percentage_24h_in_currency"], 2) if m.get("price_change_percentage_24h_in_currency") is not None else pc(back(1)),
            "d7": round(m["price_change_percentage_7d_in_currency"], 2) if m.get("price_change_percentage_7d_in_currency") is not None else pc(back(7)),
            "d30": round(m["price_change_percentage_30d_in_currency"], 2) if m.get("price_change_percentage_30d_in_currency") is not None else pc(back(30)),
            "y1": round(m["price_change_percentage_1y_in_currency"], 2) if m.get("price_change_percentage_1y_in_currency") is not None else pc(back(365)),
            "ytd": pc(ytd_base), "ytd_base": sig(ytd_base, 6),
            "hi": sig(max(year), 6), "lo": sig(min(year), 6),
            "d0": d0.isoformat(), "d": [sig(x) for x in daily], "w": [sig(x) for x in weekly],
        })
    coins.sort(key=lambda c: -(c["mc"] or 0))
    if not mk:  # keep the saved order when market values are missing
        order = {c[0]: i for i, c in enumerate(COINS)}
        coins.sort(key=lambda c: order[c["id"]])

    data = {
        "built": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "live": bool(mk), "coins": coins,
        "global": None if not glob else {
            "mc": glob["total_market_cap"]["usd"], "vol": glob["total_volume"]["usd"],
            "chg": round(glob["market_cap_change_percentage_24h_usd"], 2),
            "btc": round(glob["market_cap_percentage"]["btc"], 1), "eth": round(glob["market_cap_percentage"]["eth"], 1),
        },
    }
    with open(OUT, "w") as f:
        f.write("const CRYPTO = " + json.dumps(data, separators=(",", ":")) + ";\n")
    print(f"wrote {OUT}: {len(coins)} tokens, Bitcoin ${coins[0]['p']:,.0f}, {os.path.getsize(OUT) // 1024} KB")


if __name__ == "__main__":
    main()
