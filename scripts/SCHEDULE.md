# Refresh schedule

The site is static. A GitHub Actions job (`.github/workflows/refresh.yml`) starts `scripts/refresh.py`
twice an hour, at 17 and 47 minutes past, from about 6:45 am to 10:45 pm Eastern. That script works out
which data files are behind, refreshes only those, checks each one and keeps only files that pass. The
job then commits the files that changed to `gh-pages`, which is the live site. No AI model is involved,
and a full refresh of all four files takes about a minute.

## What is due is decided from what is published

GitHub starts scheduled jobs late and sometimes not at all. On 16 Nov 2026, the first day, one run in
nine hours started. So the script never asks "what hour is it". It asks "what is behind", and any run
that does start catches up everything that was missed.

All times are Eastern.

| Data file | Script | Refreshed when | Why |
|---|---|---|---|
| `news-data.js` | `refresh_news.py` | the published copy is more than 50 minutes old | headlines change all day |
| `crypto-data.js` | `refresh_crypto.py` | the published copy is more than 50 minutes old | crypto trades around the clock; the page also fetches live prices in the browser |
| `economy-data.js` | `refresh_economy.py` | it has not been checked since the latest checkpoint (below) | agencies publish at fixed times, so a check in between cannot find anything new |
| `markets-data.js` | `refresh_markets.py` | a finished session is newer than the published close (after 4:31 pm on the day), or FRED has a newer credit spread (from 12:10 pm) | prices are final after the 4 pm close; spreads reach FRED around midday, one business day behind |

Before any market rebuild the script makes one small request to see whether there is anything new: the
date of the last finished S&P 500 session, or the date of the newest spread on FRED. On a market holiday
both stay where they were and nothing is rebuilt. One extra pass runs between 6:15 and 7:15 pm on a
trading day, because index levels can be corrected for an hour or so after the close.

## Checkpoints for the economic data

| After | What can be new by then |
|---|---|
| 8:40 am | The 8:30 am releases: jobs report, CPI, PPI, GDP, personal income and spending (PCE), retail sales, housing starts and permits, productivity |
| 9:25 am | Federal funds rate (New York Fed, about 9:00 am, for the day before) and industrial production (9:15 am) |
| 10:10 am | The 10:00 am releases: job openings and quits (JOLTS), consumer sentiment, business applications |
| 12:10 pm | Mortgage rates (Freddie Mac, Thursdays at noon) |
| 4:25 pm | Monthly Treasury Statement (2:00 pm on its day), Debt to the Penny (about 3:00 pm), Treasury yields (Federal Reserve H.15, 4:15 pm, for the day before) |
| 5:25 pm | A second look at the afternoon publications |
| Weekends, 8:40 am | Nothing is scheduled on weekends. One check a day picks up late postings and keeps the header's "checked" time current |

FRED usually has an agency's figures within minutes of the release. With every run on time this is six
checks each weekday. If runs are skipped, the next run that starts does the check.

## Safeguards

- Markets: a session that is still trading is never published as a close. `refresh_markets.py` drops
  Yahoo's in-progress daily bar until 15 minutes after the closing bell. Tested live on 16 Nov 2026 at
  3:49 pm: Yahoo showed the day's price so far, and the job kept the previous close.
- Economy: an indicator is updated only when its rebuilt history matches what is already published.
  Anything else is held at its last good reading and named in the run summary (search the log for `HELD`).
- Every file is checked before it is kept (`check()` in `refresh.py`): headlines 8 or more; 10 or more
  tokens, each with a price and 300 days of history; 35 or more indicators and 4,000 or more series rows;
  480 or more companies with prices, three indices with levels, nine spreads, two volatility series, no
  date later than today or earlier than the published copy, and 700 or more days of credit history that
  starts no later than the published copy. A file that fails is put back from the last commit.
- A failed step does not stop the good files from being published, and it turns the run red on GitHub.
- The job never edits `index.html` or the scripts and never force-pushes.

## Running it by hand

On GitHub: Actions, "Refresh data", "Run workflow". Leave the box empty to refresh whatever is behind, or
type `--all`, or name steps (`economy markets`). It always refreshes the live branch.

On a computer: `python3 scripts/refresh.py --all`, then commit the data files that changed and push to `gh-pages`.

## Things to know

- If GitHub's scheduler keeps skipping runs, the job needs a second way to be started. The page header
  flags any data set that is overdue.
- GitHub switches scheduled jobs off in a public repository after 60 days without activity. This job
  commits several times a day, which should count as activity, but GitHub does not define the term. If
  the job ever stops: Actions, "Refresh data", "Enable workflow" turns it back on.
- The Ramp AI Index is still read by hand.
- This job replaced a scheduled Claude task ("US Economy Monitor: news and market refresh") on 16 Nov 2026.
  That task is paused. Do not run both: they would publish over each other.
