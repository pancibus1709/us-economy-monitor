#!/usr/bin/env python3
"""Run the refreshes that are due, check each data file, and keep only files that pass. Standard library only.

  python3 scripts/refresh.py            the steps due at this hour (see SCHEDULE.md)
  python3 scripts/refresh.py --all      every step, whatever the hour
  python3 scripts/refresh.py news crypto economy markets     named steps only

Each refresh script writes its file whole or not at all. This runner then checks the file and, if a
check fails, puts back the committed copy with `git checkout`. It never edits the page or the scripts,
and it does not commit: the caller commits whatever changed.

Exit code 0 unless every step that ran failed.
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

# Runs are scheduled at 47 minutes past the hour and can start late. A run belongs to the hour it was
# scheduled for, so one that starts up to 52 minutes late still does that hour's work.
ECONOMY_SLOTS = {8, 9, 10, 12, 16, 17}  # after the 8:30, 9:15, 10:00, noon, 2:00, 3:00 and 4:15 pm publications
SPREAD_SLOTS = {12, 13}                 # FRED posts the previous day's credit spreads around midday
CLOSE_SLOTS = {16, 17, 18, 19}          # after the 4 pm close, until that day's close is in


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
    return read(name, out.stdout) if out.returncode == 0 else None


def restore(name):
    subprocess.run(["git", "checkout", "--", FILES[name][0]], cwd=ROOT, check=False)


def prev_weekday(day):
    day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def due(now):
    """The steps to run for the hour this run was scheduled in."""
    slot_time = now - dt.timedelta(minutes=40)
    slot, weekday, today = slot_time.hour, slot_time.weekday() < 5, slot_time.date()
    steps = ["news", "crypto"]
    try:
        econ = read("economy")
        checked = min(r["retrieved"] for r in econ["indicators"] if r.get("retrieved") and not r.get("static"))
        age = (now - dt.datetime.fromisoformat(checked.replace("Z", "+00:00"))).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        age = 999
    if (weekday and slot in ECONOMY_SLOTS) or (not weekday and slot == 8) or age > 26:
        steps.append("economy")
    try:
        mk = read("markets")
        close, spreads = mk["asof"], mk["spreads"][0]["asof"]
    except Exception:  # noqa: BLE001
        close = spreads = ""
    last_session = prev_weekday(today).isoformat()
    if weekday and ((slot in SPREAD_SLOTS and spreads < last_session) or (slot in CLOSE_SLOTS and close < today.isoformat())):
        steps.append("markets")
    if slot_time.weekday() == 5 and slot == 8 and close < last_session:
        steps.append("markets")  # Saturday catch-up if Friday's close never arrived
    return steps


def check(name):
    """Raise if the refreshed file should not be published. Return a short description if it should."""
    d = read(name)
    if name == "news":
        if len(d["items"]) < 8:
            raise ValueError(f"only {len(d['items'])} headlines")
        return f"{len(d['items'])} headlines"
    if name == "crypto":
        coins = d["coins"]
        if len(coins) < 10:
            raise ValueError(f"only {len(coins)} tokens")
        bad = [c["sym"] for c in coins if not (c.get("p") or 0) > 0 or len(c.get("d") or []) < 300]
        if bad:
            raise ValueError(f"no price or short history for {bad}")
        return f"Bitcoin ${next(c['p'] for c in coins if c['id'] == 'bitcoin'):,.0f}"
    if name == "economy":
        if len(d["indicators"]) < 35 or len(d["series"]) < 4000:
            raise ValueError(f"{len(d['indicators'])} indicators and {len(d['series'])} series rows")
        return f"{len(d['indicators'])} indicators"
    old = committed("markets")
    if len(d["stocks"]) < 480 or len(d["indices"]) != 3 or len(d["spreads"]) != 9:
        raise ValueError(f"{len(d['stocks'])} companies, {len(d['indices'])} indices, {len(d['spreads'])} spreads")
    dates = d["credit"]["dates"]
    if len(dates) < 700:
        raise ValueError(f"credit history has only {len(dates)} days")
    if old:
        if old.get("credit") and dates[0] > old["credit"]["dates"][0]:
            raise ValueError("credit history starts later than the published copy, so saved days would be lost")
        if d["asof"] < old["asof"]:
            raise ValueError(f"last close {d['asof']} is older than the published {old['asof']}")
        stamp = lambda m: (m["asof"], [s["asof"] for s in m["spreads"]], [v["asof"] for v in m["vol"]])
        if stamp(d) == stamp(old):
            raise LookupError("nothing new")  # same close, same spreads: keep the published copy
    return f"close of {d['asof']}, spreads to {d['spreads'][0]['asof']}"


def run(name):
    cmd = [sys.executable, os.path.join(ROOT, "scripts", f"refresh_{name}.py")]
    for attempt in (1, 2):
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600)
        for line in (p.stdout + p.stderr).strip().splitlines():
            print(f"    {line}")
        if p.returncode == 0:
            break
        if attempt == 2 or not RETRY_AFTER[name]:
            restore(name)
            return False, "script failed, published copy kept"
        time.sleep(RETRY_AFTER[name])
    held = [l.split(":", 1)[1].strip() for l in p.stdout.splitlines() if l.strip().startswith("held at last good reading:")]
    try:
        note = check(name)
    except LookupError as e:
        restore(name)
        return True, str(e)
    except Exception as e:  # noqa: BLE001
        restore(name)
        return False, f"check failed ({e}), published copy kept"
    if held:
        note += "; HELD: " + "; ".join(held)
    return True, note


def main():
    args = [a for a in sys.argv[1:]]
    now = dt.datetime.now(ET)
    steps = list(FILES) if "--all" in args else [a for a in args if a in FILES] or due(now)
    print(f"{now:%a %b %-d, %-I:%M %p} Eastern. Steps: {', '.join(steps)}")
    results = {}
    for name in steps:
        print(f"  {name}")
        try:
            results[name] = run(name)
        except Exception as e:  # noqa: BLE001
            restore(name)
            results[name] = (False, f"{e!r}, published copy kept")
    print("Summary: " + " | ".join(f"{k}: {'ok' if ok else 'FAILED'}, {note}" for k, (ok, note) in results.items()))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"**{now:%a %b %-d, %-I:%M %p} Eastern**\n\n" + "".join(f"- {k}: {'ok' if ok else 'FAILED'}, {note}\n" for k, (ok, note) in results.items()))
    if results and not any(ok for ok, _ in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
