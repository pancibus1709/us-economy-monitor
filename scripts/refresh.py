#!/usr/bin/env python3
"""Run the refreshes that are due, check each data file, and keep only files that pass. Standard library only.

  python3 scripts/refresh.py            the steps due at this hour (see SCHEDULE.md)
  python3 scripts/refresh.py --all      every step, whatever the hour
  python3 scripts/refresh.py news crypto economy markets     named steps only

Each refresh script writes its file whole or not at all. This runner then checks the file and, if a
check fails, puts back the committed copy with `git checkout`. It never edits the page or the scripts,
and it does not commit: the caller commits whatever changed.

Good files are kept even when another step fails. The exit code is 0 so the caller can still publish
them; a failed step is reported as an error line and as `failed=1` in $GITHUB_OUTPUT, which the
workflow turns into a failed run after publishing.
"""
import datetime as dt
import json
import os
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET = ZoneInfo("America/New_York")
FILES = {"news": ("news-data.js", "const NEWS = "), "crypto": ("crypto-data.js", "const CRYPTO = "),
         "economy": ("economy-data.js", "const DATA = "), "markets": ("markets-data.js", "const MARKETS = ")}
RETRY_AFTER = {"news": 0, "crypto": 60, "economy": 0, "markets": 120}  # seconds before one retry; 0 is no retry
TIMEOUT = 240  # seconds allowed for one attempt of one script

# Runs are scheduled at 47 minutes past the hour and can start late. A run belongs to the hour it was
# scheduled for, so one that starts up to 52 minutes late still does that hour's work.
ECONOMY_SLOTS = {8, 9, 10, 12, 16, 17}  # after the 8:30, 9:15, 10:00, noon, 2:00, 3:00 and 4:15 pm publications
SPREAD_SLOTS = {12, 13}                 # FRED posts the previous day's credit spreads around midday
CLOSE_SLOTS = {16, 17, 18, 19}          # after the 4 pm close: tried until that day's close is in
CONFIRM_SLOT = 18                       # one more pass, to pick up index levels corrected after the first one


class NothingNew(Exception):
    """The refreshed file says nothing the published one does not."""


def read(name, text=None):
    path, prefix = FILES[name]
    if text is None:
        with open(os.path.join(ROOT, path)) as f:
            text = f.read()
    if not text.startswith(prefix):
        raise ValueError(f"does not start with {prefix!r}")
    return json.loads(text[len(prefix):].rstrip().rstrip(";"))


def committed(name):
    out = subprocess.run(["git", "show", "HEAD:" + FILES[name][0]], cwd=ROOT, capture_output=True, text=True)
    try:
        return read(name, out.stdout) if out.returncode == 0 else None
    except ValueError:
        return None


def restore(name):
    subprocess.run(["git", "checkout", "--", FILES[name][0]], cwd=ROOT, check=False)


def prev_weekday(day):
    day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def last_session():
    """Date of the last finished S&P 500 session, from one small request. None if it cannot be read."""
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import refresh_markets
        return refresh_markets.yahoo("^GSPC", "5d")[-1][0].isoformat()
    except Exception as e:  # noqa: BLE001
        print(f"  could not read the last session date ({e!r})")
        return None


def due(now, probe=last_session):
    """The steps to run for the hour this run was scheduled in."""
    slot_time = now - dt.timedelta(minutes=40)
    slot, weekday, today = slot_time.hour, slot_time.weekday() < 5, slot_time.date().isoformat()
    steps = ["news", "crypto"]

    # Economy. The age is taken from the newest check: an indicator held at its last good reading keeps
    # an old time on purpose (the page flags it), and must not make every run a refresh.
    try:
        stamps = [r["retrieved"] for r in read("economy")["indicators"] if r.get("retrieved") and not r.get("static")]
        age = (now - dt.datetime.fromisoformat(max(stamps).replace("Z", "+00:00"))).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        age = 999
    scheduled = (weekday and slot in ECONOMY_SLOTS) or (not weekday and slot == 8)
    missed = weekday and (slot - 1) in ECONOMY_SLOTS and age > 1.5  # the hour before was skipped or failed
    if scheduled or missed or age > 26:
        steps.append("economy")

    # Markets. Driven by what is already published, so it runs until the day's data is in and then stops.
    try:
        mk = read("markets")
        close, spreads = mk["asof"], mk["spreads"][0]["asof"]
    except Exception:  # noqa: BLE001
        close = spreads = ""
    session_before = prev_weekday(slot_time.date()).isoformat()
    need = False
    if weekday and slot in SPREAD_SLOTS and spreads < session_before:
        need = True
    elif (weekday and slot in CLOSE_SLOTS) or (slot_time.weekday() == 5 and slot == 8):
        finished = probe()  # on a market holiday this equals the published close, and nothing is rebuilt
        if finished is None:
            need = close < (today if weekday else session_before)
        else:
            need = finished > close or (slot == CONFIRM_SLOT and finished == today)
    if need:
        steps.append("markets")
    return steps


def check(name):
    """Raise if the refreshed file should not be published. Return a short description if it should."""
    d = read(name)
    today = dt.datetime.now(ET).date().isoformat()
    if name == "news":
        items = d.get("items") or []
        if len(items) < 8:
            raise ValueError(f"only {len(items)} headlines")
        return f"{len(items)} headlines"
    if name == "crypto":
        coins = d.get("coins") or []
        if len(coins) < 10:
            raise ValueError(f"only {len(coins)} tokens")
        bad = [c.get("sym") for c in coins if not (c.get("p") or 0) > 0 or len(c.get("d") or []) < 300]
        if bad:
            raise ValueError(f"no price or short history for {bad}")
        note = f"Bitcoin ${next(c['p'] for c in coins if c['id'] == 'bitcoin'):,.0f}"
        return note if d.get("live") else note + ", market values missing (CoinGecko did not answer)"
    if name == "economy":
        ind, series = d.get("indicators") or [], d.get("series") or []
        if len(ind) < 35 or len(series) < 4000:
            raise ValueError(f"{len(ind)} indicators and {len(series)} series rows")
        blank = [r.get("id") for r in ind if r.get("latest") is None]
        if len(blank) > 2:
            raise ValueError(f"{len(blank)} indicators have no latest reading: {blank[:6]}")
        return f"{len(ind)} indicators"
    # markets
    old = committed("markets")
    stocks, indices, spreads, vol = d.get("stocks") or {}, d.get("indices") or [], d.get("spreads") or [], d.get("vol") or []
    if len(stocks) < 480 or len(indices) != 3 or len(spreads) != 9 or len(vol) != 2:
        raise ValueError(f"{len(stocks)} companies, {len(indices)} indices, {len(spreads)} spreads, {len(vol)} volatility series")
    if not d.get("asof") or d["asof"] > today:
        raise ValueError(f"last close is dated {d.get('asof')}, after today")
    if any(not (ix.get("level") or 0) > 0 for ix in indices):
        raise ValueError("an index has no level")
    unpriced = [k for k, v in stocks.items() if not (v.get("p") or 0) > 0]
    if len(unpriced) > 5:
        raise ValueError(f"{len(unpriced)} companies have no price")
    dates = (d.get("credit") or {}).get("dates") or []
    if len(dates) < 700:
        raise ValueError(f"credit history has only {len(dates)} days")
    if old:
        if old.get("credit") and dates[0] > old["credit"]["dates"][0]:
            raise ValueError("credit history starts later than the published copy, so saved days would be lost")
        if d["asof"] < old.get("asof", ""):
            raise ValueError(f"last close {d['asof']} is older than the published {old['asof']}")
        dated = lambda m: [s.get("asof", "") for s in m.get("spreads") or []] + [v.get("asof", "") for v in m.get("vol") or []]
        if any(a < b for a, b in zip(dated(d), dated(old))):
            raise ValueError("a spread or volatility date is older than the published copy")
        # Same dates and same index levels as the published copy: nothing to publish.
        stamp = lambda m: (m.get("asof"), dated(m), [ix.get("level") for ix in m.get("indices") or []], [v.get("latest") for v in m.get("vol") or []])
        if stamp(d) == stamp(old):
            raise NothingNew("nothing new")
    return f"close of {d['asof']}, spreads to {spreads[0]['asof']}"


def run(name):
    cmd = [sys.executable, os.path.join(ROOT, "scripts", f"refresh_{name}.py")]
    for attempt in (1, 2):
        try:
            p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT)
            out, code = p.stdout + p.stderr, p.returncode
        except subprocess.TimeoutExpired:
            out, code = f"gave up after {TIMEOUT} seconds", 1
        for line in out.strip().splitlines():
            print(f"    {line}")
        if code == 0:
            break
        if attempt == 2 or not RETRY_AFTER[name]:
            restore(name)
            return False, "script failed, published copy kept"
        time.sleep(RETRY_AFTER[name])
    held = [l.split(":", 1)[1].strip() for l in out.splitlines() if l.strip().startswith("held at last good reading:")]
    try:
        note = check(name)
    except NothingNew as e:
        restore(name)
        return True, str(e)
    except Exception as e:  # noqa: BLE001
        restore(name)
        return False, f"check failed ({e!r}), published copy kept"
    if held:
        note += "; HELD: " + "; ".join(held)
    return True, note


def main():
    args = sys.argv[1:]
    unknown = [a for a in args if a != "--all" and a not in FILES]
    if unknown:
        sys.exit(f"Unknown step {unknown}. Use --all or any of: {' '.join(FILES)}")
    now = dt.datetime.now(ET)
    steps = list(FILES) if "--all" in args else args or due(now)
    print(f"{now:%a %b %-d, %-I:%M %p} Eastern. Steps: {', '.join(steps)}")
    results = {}
    for name in steps:
        print(f"  {name}")
        try:
            results[name] = run(name)
        except Exception as e:  # noqa: BLE001
            restore(name)
            results[name] = (False, f"{e!r}, published copy kept")
    lines = [f"{k}: {'ok' if ok else 'FAILED'}, {note}" for k, (ok, note) in results.items()]
    print("Summary: " + " | ".join(lines))
    failed = {k: note for k, (ok, note) in results.items() if not ok}
    for k, note in failed.items():
        print(f"::error title=Refresh failed: {k}::{note}")
    report = {"GITHUB_STEP_SUMMARY": f"**{now:%a %b %-d, %-I:%M %p} Eastern**\n\n" + "".join(f"- {l}\n" for l in lines),
              "GITHUB_OUTPUT": f"failed={'1' if failed else '0'}\n"}
    for var, text in report.items():
        if os.environ.get(var):
            with open(os.environ[var], "a") as f:
                f.write(text)


if __name__ == "__main__":
    main()
