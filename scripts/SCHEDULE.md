# Refresh schedule

The site is static. A scheduled Claude task ("US Economy Monitor") runs the scripts in this folder,
commits the data files and publishes them to `gh-pages`.

Schedule: every hour from 6:47 am to 10:47 pm Eastern (`CRON_TZ=America/New_York 47 6-22 * * *`).

| Data file | Script | When |
|---|---|---|
| `news-data.js` | `refresh_news.py` | every run |
| `crypto-data.js` | `refresh_crypto.py` | every run (the page also fetches live prices in the browser) |
| `economy-data.js` | `refresh_economy.py` | every run |
| `markets-data.js` | `refresh_markets.py` | weekdays, runs nearest 9:47 am, 5:47 pm and 7:47 pm Eastern |

## Instructions for the scheduled task

Scheduled refresh for the US Economy Monitor (prompt v3, 16 Nov 2026). The user asked for a daily market refresh and same-day headlines on Nov 14, 2026, asked the same day for the site to be published in full, and said on Nov 16, 2026 that it is critical every data set is as recent as possible. So this runs every hour from 6:47 am to 10:47 pm Eastern and also refreshes crypto and the economic indicators. Refreshed data goes to the live site. Work autonomously and keep the reply to one line.

In /home/user/us-economy-monitor (re-clone pancibus1709/us-economy-monitor if the checkout is gone), on branch claude/vigilant-cerf-fcjubl. First fetch origin gh-pages and merge it in (fast-forward or a normal merge). If the merge conflicts, abort it, change nothing and report.

Each step below applies only if its script and its data file both exist in the checkout after the merge. Skip a missing step silently. Every script writes its data file whole or not at all; "restore" means git checkout -- <file>.

A. Headlines, every run: python3 scripts/refresh_news.py (writes news-data.js). If it exits with an error, restore the file and carry on. Check the file starts with "const NEWS = ", parses as JSON and holds at least 8 items; otherwise restore it.

B. Crypto, every run: python3 scripts/refresh_crypto.py (writes crypto-data.js). On an error, wait one minute and retry once; if it fails again, restore and carry on. Check the file starts with "const CRYPTO = ", parses as JSON, has at least 10 coins, and every coin has a price p above 0 and at least 300 daily values in d; otherwise restore it.

C. Economic indicators, every run: python3 scripts/refresh_economy.py (writes economy-data.js). The script only updates an indicator whose rebuilt history matches what is published, and prints "updated:" and "held at last good reading:" lines. On an error, restore and carry on. Check the file starts with "const DATA = ", parses as JSON, has at least 35 indicators and at least 4,000 series rows; otherwise restore it. Never edit the script to make a held indicator pass; report held indicators by name in the reply.

D. Markets, only on weekdays, on the runs nearest 9:47 am (yesterday's credit spreads are published by then), 5:47 pm and 7:47 pm Eastern: python3 scripts/refresh_markets.py. On an error, retry once after two minutes; if it fails again, restore the file and report the error. If it reports tickers with no GICS classification, do not guess a sector; report them. Check markets-data.js starts with "const MARKETS = ", parses as JSON, and has at least 480 companies, three indices, nine spreads and, when a "credit" key is present, at least 700 dates in credit.dates with a first date no later than the committed copy's first date; otherwise restore it. Keep the new file only if its "asof" close date, any spreads[].asof or any vol[].asof differs from the committed copy; otherwise restore it.

E. Commit only news-data.js, crypto-data.js, economy-data.js and markets-data.js, whichever changed, as "Refresh data <date> <HH:MM ET>". Push to origin claude/vigilant-cerf-fcjubl, then publish with: git push origin HEAD:gh-pages. Never force-push. If the gh-pages push is rejected, fetch, merge and try once more; if it still fails, report it. Never edit index.html or the scripts.

Reply with one line: headline count, Bitcoin price, economic indicators updated and held (names), market close date if refreshed, and whether the live site was updated.
