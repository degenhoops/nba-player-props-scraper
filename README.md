# nba-player-props-scraper

Python scraper that pulls NBA player prop lines from a sportsbook (BetMGM) into CSV files. For personal and educational use.

## What it does

- Finds today's open NBA games and pulls every two-sided (over/under) player prop
- Saves the latest lines to a CSV, and appends every run to a history CSV so you can track line moves
- Normalizes player names (accents, `Jr.`, punctuation) so they can be matched across books
- Goes slowly on purpose, with random delays between requests

**Markets covered:** points, rebounds, assists, threes, blocks, steals, pts+reb, pts+ast, reb+ast, pts+reb+ast

## Requirements

- Python 3.8+
- Google Chrome installed
- [Playwright](https://playwright.dev/python/)

```bash
pip install playwright
```

## Usage

```bash
python probe.py             # pull props (Chrome window stays off-screen)
python probe.py --show      # show the Chrome window (for debugging)
python probe.py --headless  # no window at all (may get blocked)
python probe.py --limit 10  # only pull the first 10 games
```

It's built to run **1-2 times a day**. Running it more often is a good way to get blocked.

## Output

All files are created next to the script:

| File | What it is |
|---|---|
| `props_betmgm.csv` | Latest lines, replaced on each good run |
| `props_betmgm_history.csv` | Every run appended, for tracking line moves |
| `probe.log` | Run log |
| `debug/` | Screenshot saved when the page fails to load |
| `chrome_profile/` | Browser cookies. **Never share or commit this folder.** |

**CSV columns:** `book`, `fixture_id`, `game`, `stage`, `start_utc`, `player`, `player_norm`, `team_abbr`, `market`, `line`, `over_price`, `under_price`, `pulled_utc`

Prices are American odds.

**Exit codes:** `0` = ok, `1` = failed, `2` = blocked by the site.

## Scheduling

Use cron (Linux/macOS) or Task Scheduler (Windows) to run it once or twice a day. A lock file prevents overlapping runs.

## Troubleshooting

- **"Could not read the site key":** run with `--show` and check `debug/load_failed.png` to see what the page displayed.
- **Blocked (exit code 2):** wait a few hours, then try again with `--show`.
- **CSV is open in Excel:** the script saves to a timestamped file instead of failing.

## Disclaimer

This project is not affiliated with or endorsed by BetMGM or any sportsbook. Scraping may violate a site's terms of service. You are responsible for how you use this code. Nothing here is betting or financial advice.

## License

[MIT](LICENSE)
