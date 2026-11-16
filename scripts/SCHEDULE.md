# Refresh schedule

The site is static. A GitHub Actions job (`.github/workflows/refresh.yml`) runs `scripts/refresh.py`
at 47 minutes past each hour from 6:47 am to 10:47 pm Eastern. That script decides what is due, runs
the refresh scripts, checks each data file and keeps only files that pass. The job then commits the
files that changed to `gh-pages`, which is the live site. No AI model is involved, and a full refresh
of all four files takes under a minute.

All times are Eastern.

| Data file | Script | When | Why then |
|---|---|---|---|
| `news-data.js` | `refresh_news.py` | every run, every day | headlines change all day |
| `crypto-data.js` | `refresh_crypto.py` | every run, every day | crypto trades around the clock; the page also fetches live prices in the browser |
| `economy-data.js` | `refresh_economy.py` | weekdays at 8:47, 9:47 and 10:47 am, 12:47, 4:47 and 5:47 pm; weekends at 8:47 am | agencies publish at fixed times (below), so a check in between cannot find anything new |
| `markets-data.js` | `refresh_markets.py` | weekdays from 12:47 pm until the previous day's credit spreads are in, and from 4:47 pm until that day's close is in | spreads reach FRED around midday; prices are final after the 4 pm close |

## Why these hours for the economic data

| Check | What can be new by then |
|---|---|
| 8:47 am | The 8:30 am releases: jobs report, CPI, PPI, GDP, personal income and spending (PCE), retail sales, housing starts and permits, productivity |
| 9:47 am | Federal funds rate (New York Fed, about 9:00 am, for the day before) and industrial production (9:15 am). Also a second look at the 8:30 releases in case FRED was slow |
| 10:47 am | The 10:00 am releases: job openings and quits (JOLTS), consumer sentiment, business applications |
| 12:47 pm | Mortgage rates (Freddie Mac, Thursdays at noon) |
| 4:47 pm | Monthly Treasury Statement (2:00 pm on its day), Debt to the Penny (about 3:00 pm), Treasury yields (Federal Reserve H.15, 4:15 pm, for the day before) |
| 5:47 pm | A second look at the afternoon publications |
| Weekends, 8:47 am | Nothing is scheduled on weekends. One check a day picks up late postings and keeps the header's "checked" time current |

FRED usually has an agency's figures within minutes of the release. Checking every hour instead would
make 17 checks a day where 6 can find something. If the economic data has not been checked for more
than 26 hours for any reason, the next run checks it whatever the hour.

## Safeguards

- A run that starts up to 52 minutes late still does the work of the hour it was scheduled for.
- Markets: a session that is still trading is never published as a close. `refresh_markets.py` drops
  Yahoo's in-progress daily bar until 15 minutes after the closing bell.
- Economy: an indicator is updated only when its rebuilt history matches what is already published.
  Anything else is held at its last good reading and named in the run summary (search the log for `HELD`).
- Every file is checked before it is kept (`check()` in `refresh.py`): headlines 8 or more; 10 or more
  tokens, each with a price and 300 days of history; 35 or more indicators and 4,000 or more series rows;
  480 or more companies, three indices, nine spreads, 700 or more days of credit history that starts no
  later than the published copy. A file that fails is put back from the last commit.
- The job never edits `index.html` or the scripts and never force-pushes.

## Running it by hand

On GitHub: Actions, "Refresh data", "Run workflow". Leave the box empty for the steps due now, or type
`--all`, or name steps (`economy markets`).

On a computer: `python3 scripts/refresh.py --all`, then commit the data files that changed and push to `gh-pages`.

## Things to know

- GitHub can start scheduled jobs late when it is busy, and occasionally skips one. The next hour's run
  picks up the work.
- GitHub switches scheduled jobs off in a public repository after 60 days without activity. This job
  commits several times a day, so that only applies if every refresh fails for two months. The page
  header flags any data set that is overdue.
- The Ramp AI Index is still read by hand.
- This job replaced a scheduled Claude task ("US Economy Monitor: news and market refresh") on 16 Nov 2026.
  Do not run both: they would publish over each other.
