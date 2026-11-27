#!/usr/bin/env python3
"""Refresh the economic indicators in economy-data.js. Standard library only.

  python3 scripts/refresh_economy.py

Every indicator is rebuilt from its published series, then checked against the history already on
the page. An indicator is only updated when the rebuilt history matches what was there, so a change
in a source, a series code or a method cannot silently put a wrong number on the site. Anything that
fails the check keeps its last good reading and is listed in the output.

Sources
  FRED (Federal Reserve Bank of St. Louis), which republishes each agency's release unchanged
  US Treasury Fiscal Data: Debt to the Penny, Average Interest Rates
  University of Michigan Surveys of Consumers: index of consumer sentiment
  National Association of Realtors: Pending Home Sales Index, read from its latest news release;
  existing-home sales through FRED, which carries only NAR's latest 13 months
  Ramp AI Index: adoption through FRED; Ramp's latest summary table, weekly token volume and spend,
  and daily token prices from ramp.com/data/ai-index. Each part keeps its last good copy if it fails.
"""
import datetime as dt
import json
import os
import re
import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "economy-data.js")
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
VIA_FRED = "Read through FRED, the St. Louis Fed's copy of the official release"

# indicator id: (kind, FRED series). Kinds are explained in build().
FRED = {
    "gdp": ("level", "A191RL1Q225SBEA"), "productivity": ("level", "PRS85006092"),
    "ip_yoy": ("yoy", "INDPRO"), "unrate": ("level", "UNRATE"), "payrolls_chg": ("diff", "PAYEMS"),
    "epop_prime": ("level", "LNS12300060"), "ahe_yoy": ("yoy", "CES0500000003"), "openings_m": ("thousandth", "JTSJOL"),
    "quits": ("level", "JTSQUR"), "cpi_yoy": ("yoy", "CPIAUCNS"), "cpi_chg": ("mom", "CPIAUCSL"), "core_cpi_yoy": ("yoy", "CPILFENS"),
    "shelter_yoy": ("yoy", "CUUR0000SAH1"), "core_goods_yoy": ("yoy", "CUUR0000SACL1E"),
    "core_services_yoy": ("yoy", "CUUR0000SASLE"), "core_pce_yoy": ("yoy", "PCEPILFE"), "ppi_yoy": ("yoy", "PPIFID"),
    "real_pce_yoy": ("yoy", "PCEC96"), "retail_yoy": ("yoy", "RSAFS"), "saving": ("level", "PSAVERT"),
    "starts": ("level", "HOUST"), "permits": ("level", "PERMIT"), "info_emp_yoy": ("yoy", "USINFO"),
    "biz_apps": ("thousandth", "BABATOTALSAUS"), "ai_adoption": ("level", "RAMPAIALL"),
    "mortgage30": ("daily", "MORTGAGE30US"), "effr": ("daily", "EFFR"), "y2": ("daily", "DGS2"), "y10": ("daily", "DGS10"),
}
EXTRA = ["UNEMPLOY", "DGS3MO", "MTSDS133FMS", "EXHOSLUSM495S"]


def get(url, agent="python-requests/2.31", tries=4):
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": agent}), timeout=45) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def fred(code, least=20):
    out = {}
    for line in get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={code}").splitlines()[1:]:
        d, _, v = line.partition(",")
        try:
            out[d] = float(v)
        except ValueError:
            pass
    if len(out) < least:
        raise RuntimeError(f"FRED {code}: only {len(out)} observations")
    return out


def fiscal(path, field, extra=""):
    url = (f"https://api.fiscaldata.treasury.gov/services/api/fiscal_service/{path}?fields=record_date,{field}"
           f"&filter=record_date:gte:2016-12-01{extra}&sort=record_date&page%5Bsize%5D=10000")
    rows = json.loads(get(url, agent="Mozilla/5.0"))["data"]
    return {r["record_date"]: float(r[field]) for r in rows if r[field] not in ("", "null", None)}


def michigan():
    out = {}
    for line in get("http://www.sca.isr.umich.edu/files/tbmics.csv", agent="Mozilla/5.0").splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 3 and p[0] in LONG:
            try:
                out[f"{int(p[1])}-{LONG.index(p[0]) + 1:02d}-01"] = float(p[2])
            except ValueError:
                pass
    return out


NAR_PAGE = "https://www.nar.realtor/research-and-statistics/housing-statistics/pending-home-sales"


def nar_pending():
    """The latest national Pending Home Sales figures, as NAR states them in its news release.

    NAR publishes no machine-readable series. Its release states the month, the change from the month
    before and the change from a year earlier in plain text, and the infographic's description gives the
    index level. Anything that does not read cleanly raises, and the indicator keeps its last reading.
    """
    page = get(NAR_PAGE, agent="Mozilla/5.0")
    m = re.search(r'(?:https://(?:www\.)?nar\.realtor)?/newsroom/[a-z0-9-]*pending-home-sales[a-z0-9-]*', page)
    if not m:
        raise RuntimeError("NAR: no link to the latest pending home sales release")
    url = m.group(0) if m.group(0).startswith("http") else "https://www.nar.realtor" + m.group(0)
    html = get(url, agent="Mozilla/5.0")
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style).*?</\1>", " ", html))).replace("&nbsp;", " ")
    b = re.search(r"(" + "|".join(LONG) + r") (20\d\d) National Pending Home Sales(.{0,300}?)(?:" + "|".join(LONG) + r") 20\d\d Regional", text)
    if not b:
        raise RuntimeError(f"NAR: national figures not found in {url}")

    def pct(what):
        x = re.search(r"(no change|(\d{1,2}\.\d)% (increase|decrease)) " + what, b.group(3), re.I)
        if not x:
            raise RuntimeError(f"NAR: {what} change not found in {url}")
        return 0.0 if x.group(1).lower() == "no change" else float(x.group(2)) * (1 if x.group(3).lower() == "increase" else -1)
    mom, yoy = pct(r"month[- ]over[- ]month"), pct(r"year[- ]over[- ]year")
    if abs(mom) > 40 or abs(yoy) > 60:
        raise RuntimeError(f"NAR: implausible figures {mom}% and {yoy}%")
    # The index level is only in the infographic's description. A page can carry other months' infographics,
    # so only a description that names this month and year is used.
    when = f"{b.group(1)} {b.group(2)}"
    level = None
    for a in re.findall(r'alt="([^"]*)"', html):
        x = re.search(r"[Ii]ndex (?:was|of) (\d{2,3}\.\d)", a)
        if when in a and x:
            level = float(x.group(1))
            break
    rel = re.search(r"WASHINGTON \((\w+ \d{1,2}, 20\d\d)\)", text)
    return {"date": f"{b.group(2)}-{LONG.index(b.group(1)) + 1:02d}-01", "mom": mom, "yoy": yoy,
            "index": level, "released": rel.group(1) if rel else "", "url": url}


def pending(r, p, now):
    """Update the pending home sales indicator from NAR's release. Returns (result, message)."""
    if not p:
        return "held", "Pending home sales (NAR's release could not be read)"
    if p["date"] < r["asof"]:
        return "held", f"Pending home sales (source is behind the page: {p['date']} against {r['asof']})"
    if p["date"] > month(now.strftime("%Y-%m-%d")):
        return "held", f"Pending home sales (release dated in the future: {p['date']})"
    hist = {h[0]: h for h in r.get("history", [])}  # [month, change from a year earlier, from the month before, index]
    hist[p["date"]] = [p["date"], p["yoy"], p["mom"], p["index"]]
    d = p["date"]
    pk = shift(d, -1) if shift(d, -1) in hist else max((k for k in hist if k < d), default=None)
    ya = hist.get(shift(d, -12))
    rd = lambda x: None if x is None else round(x + 0.0, 1)
    fresh = {"period": label(d, "M"), "asof": d, "latest": rd(p["yoy"]),
             "prior": rd(hist[pk][1]) if pk else None, "prior_label": f"vs {label(pk, 'M')}" if pk else "",
             "year_ago": rd(ya[1]) if ya else None}
    fresh["change"] = rd(fresh["latest"] - fresh["prior"]) if pk else None
    fresh["change_1y"] = rd(fresh["latest"] - fresh["year_ago"]) if ya else None
    prev = LONG[int(shift(d, -1)[5:7]) - 1]
    move = "unchanged from" if p["mom"] == 0 else f"{'up' if p['mom'] > 0 else 'down'} {abs(p['mom']):.1f}% from"
    via = f"Read from NAR's news release of {p['released']}" if p["released"] else "Read from NAR's latest news release"
    if p["index"] is not None:
        via += f". The index was {p['index']:.1f} (2001 = 100), {move} {prev}"
    else:
        via += f". Contract signings were {move} {prev}"
    msg = f"{r['name']}: {r['latest']} ({r['period']}) -> {fresh['latest']} ({fresh['period']})"
    changed = any(r.get(k) != fresh[k] for k in ("asof", "latest", "prior", "year_ago"))
    r.update(fresh)
    r["history"] = [hist[k] for k in sorted(hist)]
    r["via"], r["release_url"] = via, p["url"]
    r["retrieved"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ("updated" if changed else "same"), msg


def existing(r, src, now):
    """Update existing-home sales from FRED, which carries only NAR's latest 13 months.

    Each run's 13 months overwrite the same months already kept (so revisions come through), and
    months that have dropped out of FRED's window are kept, so the history grows. A rebuilt month
    that differs from the kept one by more than 0.15 million is refused. Returns (result, message).
    """
    if not src or len(src) < 12:
        return "held", f"Existing-home sales (FRED returned {len(src or {})} months)"
    hist = {h[0]: h for h in r.get("history", [])}
    new = {d: round(v / 1e6, 2) for d, v in src.items()}
    off = [d for d in new if d in hist and d < max(new) and abs(hist[d][1] - new[d]) > 0.15]
    if off:
        return "held", f"Existing-home sales (rebuilt months differ from those kept: {off[:3]})"
    d = max(new)
    if d < r["asof"]:
        return "held", f"Existing-home sales (source is behind the page: {d} against {r['asof']})"
    for k, v in new.items():
        hist[k] = [k, v]
    pk = shift(d, -1) if shift(d, -1) in hist else max((k for k in hist if k < d), default=None)
    ya = hist.get(shift(d, -12))
    rd = lambda x: None if x is None else round(x + 0.0, 2)
    fresh = {"period": label(d, "M"), "asof": d, "latest": rd(hist[d][1]),
             "prior": rd(hist[pk][1]) if pk else None, "prior_label": f"vs {label(pk, 'M')}" if pk else "",
             "year_ago": rd(ya[1]) if ya else None}
    fresh["change"] = rd(fresh["latest"] - fresh["prior"]) if pk else None
    fresh["change_1y"] = rd(fresh["latest"] - fresh["year_ago"]) if ya else None
    msg = f"{r['name']}: {r['latest']} ({r['period']}) -> {fresh['latest']} ({fresh['period']})"
    changed = any(r.get(k) != fresh[k] for k in ("asof", "latest", "prior", "year_ago"))
    r.update(fresh)
    r["history"] = [hist[k] for k in sorted(hist)]
    r["via"] = VIA_FRED
    r["retrieved"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ("updated" if changed else "same"), msg


RAMP = "https://ramp.com/data/ai-index"
RAMP_FRED = {"all": "RAMPAIALL", "anthropic": "RAMPAIANTHROPIC", "openai": "RAMPAIOPENAI", "xai": "RAMPAIXAI"}
MAKERS = ["anthropic", "openai", "google", "xai"]  # charted on their own; every other maker is summed as "other"


def flat(html):
    t = re.sub(r"(?s)<(script|style).*?</\1>", " ", html)
    t = re.sub(r"\|+", "|", re.sub(r"<[^>]+>", "|", t))
    return re.sub(r"\s+", " ", t).replace("&amp;", "&")


def ramp_table(html):
    """Ramp's summary table: latest and prior month for each adoption segment, spend per employee and AI's share of spend."""
    t = flat(html)
    m = re.search(r"Metric ?\| ?(\w{3} \d{4}) ?\| ?(\w{3} \d{4}) ?\| ?Change ?\|(.*?)\| ?Tokenomics", t)
    if not m:
        raise RuntimeError("Ramp: summary table not found")
    cells = [c.strip() for c in m.group(3).split("|") if c.strip()]
    val = lambda c: float(c.replace("$", "").replace(",", "").replace("%", ""))
    rows = []
    for i in range(0, len(cells) - 3, 4):
        name, a, b, ch = cells[i], cells[i + 1], cells[i + 2], cells[i + 3]
        try:
            a, b = val(a), val(b)
            ch = float(re.sub(r"[^0-9.+-]", "", ch.replace("−", "-")))  # Ramp's own change, worked from unrounded figures
        except ValueError:
            raise RuntimeError(f"Ramp: unreadable row {cells[i:i + 4]}")
        if name.startswith("AI adoption: "):
            seg = name[len("AI adoption: "):]
            if seg == "All businesses":
                group = "All"
            elif seg.endswith(" sector"):
                group, seg = "Industry", seg[:-len(" sector")]
            elif re.match(r"\d\. ", seg):
                group, seg = "Company size", re.sub(r"^\d\. | \(\w+\)$", "", seg)
            else:
                group = "Provider"
        elif name.startswith("AI spend per employee per month: "):
            group, seg = "Spend per employee", name.split(": ", 1)[1]
        elif name.startswith("AI share of business spend"):
            group, seg = "Share of spend", "AI share of business spend, excluding payroll"
        else:
            continue
        rows.append({"group": group, "segment": seg, "latest": a, "prior": b, "change": ch})
    groups = {r["group"] for r in rows}
    if not {"All", "Provider", "Industry", "Company size", "Spend per employee"} <= groups or len(rows) < 20:
        raise RuntimeError(f"Ramp: table incomplete ({len(rows)} rows, groups {sorted(groups)})")
    if any(not (0 <= r["latest"] <= 100) for r in rows if r["group"] not in ("Spend per employee",)):
        raise RuntimeError("Ramp: a share outside 0 to 100")
    mon = lambda x: f"{x[4:]}-{MON.index(x[:3]) + 1:02d}-01"
    return {"month": mon(m.group(1)), "prior_month": mon(m.group(2)), "rows": rows}


def ramp_tokens(raw):
    """Weekly token volume and spend: total and each model maker's share."""
    rows = json.loads(raw)
    acc = {}
    for r in rows:
        if r.get("period_grain") != "weekly" or r.get("metric_key") not in ("token_volume", "token_spend"):
            continue
        k = acc.setdefault((r["period_start"], r["period_end"]), {"token_volume": {}, "token_spend": {}})[r["metric_key"]]
        mk = r["model_maker"] if r["model_maker"] in MAKERS else "other"
        k[mk] = k.get(mk, 0) + float(r["metric_value"])
    weeks = sorted(acc)
    if len(weeks) < 20:
        raise RuntimeError(f"Ramp tokens: only {len(weeks)} weeks")
    out = {"weeks": [w[0] for w in weeks], "week_end": weeks[-1][1], "makers": MAKERS + ["other"]}
    for metric, key in (("token_volume", "volume"), ("token_spend", "spend")):
        tot = [sum(acc[w][metric].values()) for w in weeks]
        if not all(t > 0 for t in tot):
            raise RuntimeError(f"Ramp tokens: a week with no {key}")
        out[key] = {"total": [round(t, 3) for t in tot],
                    "share": {m: [round(100 * acc[w][metric].get(m, 0) / t, 4) for w, t in zip(weeks, tot)] for m in out["makers"]}}
    return out


def ramp_prices(raw):
    """Daily price per million tokens (Ramp's figures are already seven-day averages), by token type."""
    rows = json.loads(raw)
    types = {"all_tokens": "all", "uncached_input": "input", "output": "output"}
    by = {}
    for r in rows:
        if r.get("token_type") in types and r.get("series_key") in ("blend", "anthropic", "openai"):
            by.setdefault(r["usage_date"], {})[(types[r["token_type"]], r["series_key"])] = float(r["price_per_million"])
    dates = sorted(d for d in by if len(by[d]) == 9)
    if len(dates) < 60:
        raise RuntimeError(f"Ramp prices: only {len(dates)} complete days")
    if not all(v > 0 for d in dates for v in by[d].values()):
        raise RuntimeError("Ramp prices: a price at or below zero")
    return {"dates": dates, **{t: {k: [round(by[d][(t, k)], 4) for d in dates] for k in ("blend", "anthropic", "openai")} for t in types.values()}}


def ramp_ai(prev, now):
    """The Ramp AI Index section. Returns (data, list of parts held at their last good copy)."""
    out, held = dict(prev or {}), []
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        ser = {k: {d: v for d, v in fred(c).items() if d >= "2023-07-01"} for k, c in RAMP_FRED.items()}
        dates = sorted(ser["all"])
        if len(dates) < 30 or not all(0 <= v <= 100 for s in ser.values() for v in s.values()):
            raise RuntimeError(f"{len(dates)} months or a share outside 0 to 100")
        out["adoption"] = {"dates": dates, **{k: [None if d not in s else round(s[d], 2) for d in dates] for k, s in ser.items()}, "retrieved": stamp}
    except Exception as e:  # noqa: BLE001
        held.append(f"Ramp adoption history ({e})")
    try:
        out["table"] = {**ramp_table(get(RAMP, agent="Mozilla/5.0")), "retrieved": stamp}
    except Exception as e:  # noqa: BLE001
        held.append(f"Ramp summary table ({e})")
    try:
        out["tokens"] = {**ramp_tokens(get(RAMP + "/chart-data/tokenVolumes", agent="Mozilla/5.0")), "retrieved": stamp}
    except Exception as e:  # noqa: BLE001
        held.append(f"Ramp token volume and spend ({e})")
    try:
        out["prices"] = {**ramp_prices(get(RAMP + "/chart-data/tokenPrices", agent="Mozilla/5.0")), "retrieved": stamp}
    except Exception as e:  # noqa: BLE001
        held.append(f"Ramp token prices ({e})")
    # The adoption chart and the table must agree on the month they share.
    a, tb = out.get("adoption"), out.get("table")
    if a and tb and tb["month"] in a["dates"]:
        overall = next(r["latest"] for r in tb["rows"] if r["group"] == "All")
        if abs(a["all"][a["dates"].index(tb["month"])] - overall) > 0.15:
            held.append(f"Ramp check: FRED and Ramp disagree on {tb['month']} ({a['all'][a['dates'].index(tb['month'])]} against {overall})")
    return out, held


def month(d):
    return d[:7] + "-01"


def shift(d, months):
    y, m = int(d[:4]), int(d[5:7]) + months
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y}-{m:02d}-01"


def yoy(s):
    return {d: (v / s[shift(d, -12)] - 1) * 100 for d, v in s.items() if shift(d, -12) in s and s[shift(d, -12)]}


def monthly_avg(s):
    acc = {}
    for d, v in s.items():
        acc.setdefault(month(d), []).append(v)
    return {m: sum(v) / len(v) for m, v in acc.items()}


def minus(a, b):
    return {d: a[d] - b[d] for d in a if d in b}


def label(d, freq):
    y, m, day = int(d[:4]), int(d[5:7]), int(d[8:10])
    if freq == "Q":
        return f"Q{(m - 1) // 3 + 1} {y}"
    if freq in ("D", "W"):
        return f"{MON[m - 1]} {day}, {y}"
    return f"{MON[m - 1]} {y}"


def build(raw):
    """Return {indicator id: (monthly or quarterly series, daily series or None)}."""
    out = {}
    for i, (kind, code) in FRED.items():
        s = raw[code]
        if kind == "yoy":
            out[i] = (yoy(s), None)
        elif kind == "mom":
            # Rounded to one decimal here, as BLS states it, so the chart and the headline show the same figure.
            out[i] = ({d: round((v / s[shift(d, -1)] - 1) * 100, 1) for d, v in s.items() if shift(d, -1) in s and s[shift(d, -1)]}, None)
        elif kind == "diff":
            out[i] = ({d: s[d] - s[shift(d, -1)] for d in s if shift(d, -1) in s}, None)
        elif kind == "thousandth":
            out[i] = ({d: v / 1000 for d, v in s.items()}, None)
        elif kind == "daily":  # charted as monthly averages, headline is the latest day or week
            out[i] = (monthly_avg(s), s)
        else:
            out[i] = (s, None)
    unrate = raw["UNRATE"]
    out["openings_per_unemployed"] = ({d: raw["JTSJOL"][d] / raw["UNEMPLOY"][d] for d in raw["JTSJOL"] if d in raw["UNEMPLOY"]}, None)
    # Sahm rule: three-month average unemployment rate less the lowest such average of the previous 12 months.
    avg3 = {d: (unrate[d] + unrate[shift(d, -1)] + unrate[shift(d, -2)]) / 3 for d in unrate if shift(d, -1) in unrate and shift(d, -2) in unrate}
    sahm = {}
    for d in avg3:
        prev = [avg3[shift(d, -k)] for k in range(1, 13) if shift(d, -k) in avg3]
        if len(prev) >= 9:
            sahm[d] = avg3[d] - min(prev)
    out["sahm"] = (sahm, None)
    out["real_ahe_yoy"] = (minus(out["ahe_yoy"][0], out["cpi_yoy"][0]), None)
    for i, short in (("curve_10y2y", "DGS2"), ("curve_10y3m", "DGS3MO")):
        daily = minus(raw["DGS10"], raw[short])
        out[i] = (monthly_avg(daily), daily)
    out["mortgage_spread"] = (minus(out["mortgage30"][0], out["y10"][0]), None)
    out["real_ffr"] = (minus(out["effr"][0], out["core_pce_yoy"][0]), None)
    out["sentiment"] = (raw["michigan"], None)
    debt = {d: v / 1e12 for d, v in raw["debt"].items()}
    ends = {}
    for d in sorted(debt):
        ends[month(d)] = debt[d]  # last reading of each month
    out["debt"] = (ends, debt)
    mts = raw["MTSDS133FMS"]  # monthly surplus, millions of dollars; a deficit is negative
    out["deficit_12m"] = ({d: -sum(mts[shift(d, -k)] for k in range(12)) / 1e6 for d in mts if all(shift(d, -k) in mts for k in range(12))}, None)
    out["avg_rate"] = ({month(d): v for d, v in raw["avg_rate"].items()}, None)
    return out


# Benchmark revisions confirmed by hand against the agency's own notice. For 14 days after the date the
# history check is waived for that series, so the revised history replaces the published one. Once it
# has, the next run's check passes on its own, and after the window the entry does nothing.
REVISIONS = {
    "ip_yoy": ("2026-11-24", "Federal Reserve annual revision to industrial production, base year 2022"),
}


def matches(old, new, dec):
    """Does the rebuilt history agree with the history already published?

    Agencies revise past months, so small differences are allowed. A wrong series, a shifted month
    or a changed method produces large ones and is refused.
    """
    common = sorted(d for d in old if d in new)[-40:-3]  # the newest readings may have been revised
    if len(common) < 12:
        return False, f"only {len(common)} months overlap"
    recent = [old[d] for d in common]
    tol = max(1.5 * 10 ** -dec, 0.05 * (max(recent) - min(recent)))
    diffs = [abs(old[d] - new[d]) for d in common]
    off = sum(x > tol for x in diffs)
    ok = off <= 0.15 * len(common) and max(diffs) <= 10 * tol
    return ok, f"{len(common) - off} of {len(common)} months agree, largest difference {max(diffs):.3g}"


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def states(ind, ser):
    """The one-line reading shown on each connection, rewritten from the refreshed numbers."""
    v = lambda i: ind[i]["latest"]
    out = {}
    ms = sorted(ser["mortgage_spread"].values())
    med = statistics.median(ms)
    out["policy_housing"] = (f"The mortgage spread over the 10-year Treasury is {v('mortgage_spread'):.2f} points, "
                             f"{'wider' if v('mortgage_spread') > med else 'narrower'} than its median since 2017 of {med:.2f}.")
    o = v("openings_per_unemployed")
    out["slack_wages"] = f"There are {o:.2f} openings per unemployed worker. " + ("Openings outnumber unemployed workers." if o > 1 else "Unemployed workers outnumber openings.")
    w, cs = v("ahe_yoy"), v("core_services_yoy")
    out["wages_services"] = f"Wage growth and core services inflation are both {w:.1f}%." if w == cs else f"Wage growth is {w:.1f}% and core services inflation is {cs:.1f}%."
    rw = [x for _, x in sorted(ser["real_ahe_yoy"].items())]
    n = 0
    while n < len(rw) and (rw[-1 - n] < 0) == (rw[-1] < 0):
        n += 1
    out["real_wage_spending"] = f"Real wage growth has been {'negative' if rw[-1] < 0 else 'positive'} for {n} consecutive month{'s' if n != 1 else ''} of available data."
    c, s = v("curve_10y3m"), v("sahm")
    out["curve_cycle"] = (f"The 10-year minus 3-month curve is {'positive' if c > 0 else 'inverted' if c < 0 else 'flat'} at {c:+.2f} points. "
                          f"The Sahm indicator is {s:.2f}, {'below' if s < 0.5 else 'at or above'} its 0.50 threshold.")
    p = v("core_pce_yoy")
    out["policy_stance"] = f"The real policy rate is {v('real_ffr'):+.2f} points. Core PCE inflation is {abs(p - 2):.1f} points {'above' if p >= 2 else 'below'} the 2% target."
    g = v("y10") - v("avg_rate")
    out["debt_rates"] = (f"The 10-year yield is {abs(g):.2f} points {'above' if g >= 0 else 'below'} the average rate on existing debt, "
                         f"so new long-term borrowing costs {'more' if g >= 0 else 'less'} than the average on existing debt.")
    cg = v("core_goods_yoy")
    out["goods_services"] = f"Core goods prices are {'rising' if cg >= 0 else 'falling'} at {abs(cg):.1f}% a year. Core services prices are rising at {cs:.1f}% a year."
    sv = list(ser["sentiment"].values())
    out["sentiment_spending"] = f"Sentiment is at the {ordinal(max(1, round(100 * sum(x <= v('sentiment') for x in sv) / len(sv))))} percentile of its readings since Jan 2017."
    return out


def main():
    with open(OUT) as f:
        data = json.loads(f.read().split("=", 1)[1].rstrip().rstrip(";"))
    now = dt.datetime.now(dt.timezone.utc)
    for s in data.get("summary", []):
        s.pop("text", None)  # written once by hand and never refreshed; the page builds these cards from the indicators

    codes = sorted({c for _, c in FRED.values()} | set(EXTRA))
    jobs = {c: (lambda c=c: fred(c, 12 if c == "EXHOSLUSM495S" else 20)) for c in codes}
    jobs["debt"] = lambda: fiscal("v2/accounting/od/debt_to_penny", "tot_pub_debt_out_amt")
    jobs["avg_rate"] = lambda: fiscal("v2/accounting/od/avg_interest_rates", "avg_interest_rate_amt", ",security_desc:eq:Total%20Marketable")
    jobs["michigan"] = michigan
    jobs["nar"] = nar_pending

    def run(k):
        try:
            return k, jobs[k]()
        except Exception as e:  # noqa: BLE001
            return k, e
    with ThreadPoolExecutor(6) as ex:
        raw = dict(ex.map(run, jobs))
    failed = {k: v for k, v in raw.items() if isinstance(v, Exception)}
    for k, e in failed.items():
        print(f"  source failed: {k}: {e}")
        raw[k] = {}
    if len(failed) > len(jobs) // 3:
        sys.exit(f"{len(failed)} of {len(jobs)} sources failed; economy-data.js left unchanged.")
    try:
        built = build(raw)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Could not rebuild the indicators ({e!r}); economy-data.js left unchanged.")

    old = {}
    for r in data["series"]:
        old.setdefault(r["id"], {})[r["date"]] = float(r["value"])
    updated, same, held = [], [], []
    series = {}
    for r in data["indicators"]:
        i = r["id"]
        series[i] = old.get(i, {})
        if r.get("static") or i not in built:
            continue
        new, daily = built[i]
        first = min(old[i]) if i in old else "2017-01-01"
        new = {d: v for d, v in new.items() if d >= first}
        ok, why = matches(old.get(i, {}), new, int(r["dec"]))
        if not ok and i in REVISIONS:
            day, what = REVISIONS[i]
            age = (now.date() - dt.date.fromisoformat(day)).days
            if 0 <= age <= 14:  # the first run after the revision takes it; after that the normal check agrees
                print(f"  accepting {r['name']}: {what} ({why})")
                ok = True
        # A month already published stays, even where a gap in the source stops it being rebuilt.
        new = {**{d: v for d, v in old.get(i, {}).items() if d not in new and new and d < max(new)}, **new}
        if not ok:
            held.append(f"{r['name']} ({why})")
            continue
        freq, dec = r["freq"], int(r["dec"])
        dates = sorted(new)
        if daily:
            day = max(daily)
            latest, asof = daily[day], day
            pm = shift(month(day), -1)
            if pm not in new:  # no reading last month: use the latest month before this one
                pm = max((d for d in new if d < month(day)), default=None)
            if pm is None:
                held.append(f"{r['name']} (no earlier month to compare with)")
                continue
            prior = new[pm]
            if i == "debt":
                prior_label = f"vs end of {label(pm, 'M')}"
            else:
                prior_label = f"vs {label(pm, 'M')} average"
            ya_key = shift(month(day), -12)
        else:
            asof = dates[-1]
            latest = new[asof]
            step = 3 if freq == "Q" else 1
            pk = shift(asof, -step)
            prior = new.get(pk)
            if prior is None:  # a month the agency did not collect: fall back to the reading before it
                pk = dates[-2]
                prior = new[pk]
            prior_label = f"vs {label(pk, freq)}"
            ya_key = shift(asof, -12)
        if asof < r["asof"]:
            held.append(f"{r['name']} (source is behind the page: {asof} against {r['asof']})")
            continue
        ya = new.get(ya_key)
        rd = lambda x: None if x is None else round(x + 0.0, dec)
        fresh = {
            "period": label(asof, freq), "asof": asof, "latest": rd(latest), "prior": rd(prior), "prior_label": prior_label,
            # Changes are taken between the rounded figures, so they agree with the numbers shown beside them.
            "change": rd(rd(latest) - rd(prior)), "year_ago": rd(ya), "change_1y": None if ya is None else rd(rd(latest) - rd(ya)),
        }
        vals = list(new.values()) + ([latest] if daily else [])
        fresh["range_low"], fresh["range_high"] = rd(min(vals)), rd(max(vals))
        fresh["percentile"] = round(100 * sum(x <= latest for x in vals) / len(vals))
        changed = any(r.get(k) != fresh[k] for k in ("asof", "latest", "prior", "year_ago"))
        (updated if changed else same).append(f"{r['name']}: {r['latest']} ({r['period']}) -> {fresh['latest']} ({fresh['period']})")
        r.update(fresh)
        r["retrieved"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")  # only indicators that passed the check
        if i not in ("debt", "avg_rate", "sentiment"):
            r["via"] = VIA_FRED
        series[i] = new
    for r in data["indicators"]:
        if r["id"] in ("pending_sales", "existing_sales"):
            kind, msg = pending(r, raw.get("nar"), now) if r["id"] == "pending_sales" else existing(r, raw.get("EXHOSLUSM495S"), now)
            {"updated": updated, "same": same, "held": held}[kind].append(msg)
    ai, ai_held = ramp_ai(data.get("ai"), now)
    if ai:
        data["ai"] = ai
    held.extend(ai_held)
    data.pop("ramp", None)  # the hand-entered Ramp table, replaced by data["ai"]["table"]
    data["series"] = [{"id": r["id"], "date": d, "value": str(round(v, 2))} for r in data["indicators"] for d, v in sorted(series[r["id"]].items())]

    ind = {r["id"]: r for r in data["indicators"]}
    fmt = lambda r, x, signed=False: (f"{x:+,.{int(r['dec'])}f}" if signed else f"{x:,.{int(r['dec'])}f}") + {"%": "%", "pts": " pts", "K": "K", "M": "M"}.get(r["unit"], "")
    chg = lambda r, x: f"{abs(x):,.{int(r['dec'])}f}" + {"%": " pts", "pts": " pts", "K": "K", "M": "M"}.get(r["unit"], "")
    def reading(r):
        t = f"{r['name']}: {fmt(r, r['latest'])} ({r['period']})"
        if r.get("change_1y") is not None:
            t += f", {'up' if r['change_1y'] >= 0 else 'down'} {chg(r, r['change_1y'])} from a year earlier"
        return t + "."
    st = states(ind, series)
    for l in data["links"]:
        a, b = ind.get(l["a"]), ind.get(l["b"])
        if a and b:
            l["reading"] = reading(a) + " " + reading(b)
        if l["id"] in st:
            l["state"] = st[l["id"]]
        # Correlation on monthly data since 2018, the second series read `lead` months later.
        sa, sb, lead = series.get(l["a"], {}), series.get(l["b"], {}), int(l.get("lead") or 0)
        pairs = [(sa[d], sb[shift(d, lead)]) for d in sorted(sa) if d >= "2018-01-01" and shift(d, lead) in sb]
        if len(pairs) >= 24:
            c = statistics.correlation([p[0] for p in pairs], [p[1] for p in pairs])
            if abs(c - float(l["corr"])) <= 0.08 and abs(len(pairs) - int(l["corr_n"])) <= 6:  # same method as before
                l["corr"], l["corr_n"] = round(c, 2), len(pairs)

    text = "const DATA = " + json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/") + ";\n"
    with open(OUT + ".tmp", "w") as f:  # written beside the live file, then swapped in whole
        f.write(text)
    os.replace(OUT + ".tmp", OUT)
    print(f"wrote {OUT}: {len(updated)} updated, {len(same)} unchanged, {len(held)} held")
    for u in updated:
        print("  updated:", u)
    for h in held:
        print("  held at last good reading:", h)


if __name__ == "__main__":
    main()
