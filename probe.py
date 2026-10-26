"""
probe.py - pull BetMGM NBA player props into a CSV. Built to run 1-2 times a day.

Setup:
    pip install playwright
    (needs Google Chrome installed)

Run:
    python probe.py             pull props (Chrome window stays off-screen)
    python probe.py --show      show the Chrome window (for debugging)
    python probe.py --headless  no window at all (BetMGM may block this)

Files (all created next to this script, so it works from any folder):
    props_betmgm.csv          latest lines (replaced on each good run)
    props_betmgm_history.csv  every run appended, for tracking line moves
    probe.log                 run log
    debug/                    screenshot saved when the page fails to load
    chrome_profile/           browser cookies. NEVER share this folder.

Exit code: 0 = ok, 1 = failed, 2 = blocked by the site.

Note: scraping may break BetMGM's terms. Keep it to 1-2 runs a day.
"""
import argparse
import csv
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

HERE = Path(__file__).resolve().parent
LATEST = HERE / "props_betmgm.csv"
HISTORY = HERE / "props_betmgm_history.csv"
LOG_FILE = HERE / "probe.log"
DEBUG_DIR = HERE / "debug"
PROFILE_DIR = HERE / "chrome_profile"
LOCK_FILE = HERE / ".probe.lock"

PAGE_URL = "https://nj.betmgm.com/en/sports/basketball-7/betting/usa-9/nba-6004"
NBA_COMPETITION_ID = 6004

# BetMGM stat code -> market name
MARKETS = {
    "Point": "points",
    "Rebound": "rebounds",
    "Assist": "assists",
    "ThreePointer": "threes",
    "Block": "blocks",
    "Steal": "steals",
    "CombinedPointsRebounds": "pts+reb",
    "CombinedPointsAssists": "pts+ast",
    "CombinedReboundsAssists": "reb+ast",
    "CombinedPointsReboundsAssists": "pts+reb+ast",
}

FIELDS = ["book", "fixture_id", "game", "stage", "start_utc", "player",
          "player_norm", "team_abbr", "market", "line", "over_price",
          "under_price", "pulled_utc"]

LIST_QS = {
    "fixtureTypes": "Standard", "state": "Latest", "offerMapping": "Filtered",
    "offerCategories": "Gridable", "fixtureCategories": "Gridable,NonGridable,Other",
    "sportIds": "7", "isPriceBoost": "false", "statisticsModes": "None",
    "take": "50", "sortBy": "Tags",
}
VIEW_QS = {
    "offerMapping": "All", "scoreboardMode": "Full", "state": "Latest",
    "includePrecreatedBetBuilder": "true", "supportVirtual": "true",
    "isBettingInsightsEnabled": "true", "useRegionalisedConfiguration": "true",
    "includeRelatedFixtures": "false", "statisticsModes": "All",
    "firstMarketGroupOnly": "false",
}

# Runs inside the page, so requests look like the site's own.
FETCH_JS = ("async u => { const r = await fetch(u, {credentials: 'include'});"
            " return {s: r.status, t: await r.text()}; }")

log = logging.getLogger("probe")


class FetchError(Exception):
    pass


class Blocked(FetchError):
    """403/429 or a block page. Do not retry."""


# ---------- names ----------

TEAM_TAG = re.compile(r"\s*\(([A-Za-z]{2,4})\)\s*$")   # "Sion James (CHA)"
EXTRA_LETTERS = str.maketrans({"đ": "d", "ł": "l", "ø": "o", "ß": "ss", "æ": "ae"})


def split_name(raw):
    """'Sion James (CHA)' -> ('Sion James', 'CHA')"""
    m = TEAM_TAG.search(raw)
    return TEAM_TAG.sub("", raw).strip(), (m.group(1).upper() if m else "")


def norm_name(name):
    """Lowercase, no accents/punctuation/Jr, so names match across books."""
    n = unicodedata.normalize("NFKD", name.lower()).translate(EXTRA_LETTERS)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"[.'\u2019`]", "", n)        # P.J. -> pj, D'Angelo -> dangelo
    n = re.sub(r"[^a-z]+", " ", n)           # hyphens etc. -> space
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


# ---------- parsing ----------

def parse_fixture(fx, pulled):
    """One game from fixture-view -> list of two-sided player prop rows."""
    players = {}
    for p in fx.get("participants", []):
        if p.get("properties", {}).get("type") == "Player" and "id" in p:
            players[str(p["id"])] = p.get("name", {}).get("value", "")

    rows, seen = [], set()
    for om in fx.get("optionMarkets", []):
        if om.get("status") != "Visible":
            continue
        P = {x["key"]: x["value"] for x in om.get("parameters", [])}
        market = MARKETS.get(P.get("Happening"))
        if (not market or P.get("MarketType") != "Over/Under"
                or P.get("Period") != "FullTime"
                or "FixtureParticipant" not in P or "DecimalValue" not in P):
            continue
        raw = players.get(P["FixtureParticipant"])
        if not raw:
            continue

        over = under = None
        for o in om.get("options", []):
            if o.get("status") != "Visible":
                continue
            price = o.get("price", {}).get("americanOdds")
            label = o.get("name", {}).get("value", "").lower()
            if label.startswith("over"):
                over = price
            elif label.startswith("under"):
                under = price
        if over is None and under is None:   # "to score 10+" ladders only have Yes
            continue

        name, team = split_name(raw)
        line = float(P["DecimalValue"])
        key = (norm_name(name), market, line)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "book": "betmgm", "fixture_id": fx["id"], "game": fx["name"]["value"],
            "stage": fx.get("stage", ""), "start_utc": fx.get("startDate", ""),
            "player": name, "player_norm": key[0], "team_abbr": team,
            "market": market, "line": line, "over_price": over,
            "under_price": under, "pulled_utc": pulled,
        })
    return rows


# ---------- browser ----------

class Browser:
    """Real Chrome. Calls the site's API from inside the page."""

    def __init__(self, mode):
        from playwright.sync_api import sync_playwright
        args = ["--disable-blink-features=AutomationControlled"]
        if mode == "headless":
            args += ["--headless=new", "--window-size=1280,800"]
        elif mode == "offscreen":
            args += ["--window-position=-32000,-32000", "--window-size=1280,800"]
        self.accessid = None        # site key, copied from the page's own requests
        self.subdivision = None     # state value the site sends, copied the same way
        self.origin = None
        self._pw = sync_playwright().start()
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR), channel="chrome",
                headless=False, viewport=None, args=args)
            self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
            self.page.on("request", self._sniff)
            self._load()
        except Exception:
            self.close()
            raise

    def _sniff(self, req):
        if self.accessid or "/cds-api/" not in req.url:
            return
        q = dict(parse_qsl(urlsplit(req.url).query))
        if q.get("x-bwin-accessid"):
            self.accessid = q["x-bwin-accessid"]
            self.subdivision = q.get("subdivision")

    def _load(self):
        self.page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
        for _ in range(45):                       # wait up to ~45s for the first API call
            if self.accessid:
                break
            self.page.wait_for_timeout(1000)
        if not self.accessid:
            self._snapshot()
            raise FetchError("Could not read the site key. See debug/ for what the page showed.")
        self.origin = self.page.evaluate("location.origin")

    def _snapshot(self):
        """Save what the page looked like, to see why it failed."""
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            self.page.screenshot(path=str(DEBUG_DIR / "load_failed.png"))
            log.error("Page title: %r  URL: %s", self.page.title(), self.page.url)
        except Exception:
            pass

    def base(self):
        d = {"x-bwin-accessid": self.accessid, "lang": "en-us",
             "country": "US", "userCountry": "US"}
        if self.subdivision:
            d["subdivision"] = self.subdivision
        return d

    def get_json(self, path, params):
        url = "%s%s?%s" % (self.origin, path, urlencode(params))
        last = "request failed"
        for _ in range(2):                        # one retry for flaky requests
            try:
                res = self.page.evaluate(FETCH_JS, url)
            except Exception as e:
                last = str(e).splitlines()[0]
                self.page.wait_for_timeout(2000)
                continue
            if res["s"] in (401, 403, 429):
                raise Blocked("HTTP %d (blocked or rate limited)" % res["s"])
            if res["s"] != 200:
                last = "HTTP %d" % res["s"]
                self.page.wait_for_timeout(2000)
                continue
            try:
                return json.loads(res["t"])
            except ValueError:
                raise Blocked("Got a web page instead of data (likely a block screen)")
        raise FetchError(last)

    def close(self):
        try:
            self.ctx.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass


# ---------- scraping ----------

def find_nba_games(b):
    games, seen, skip = [], set(), 0
    for _ in range(8):                            # the list is paged, 50 at a time
        data = b.get_json("/cds-api/bettingoffer/fixtures", {**b.base(), **LIST_QS, "skip": skip})
        batch = data.get("fixtures", [])
        skip += len(batch)
        found = 0
        for g in batch:
            comp = g.get("competition", {}).get("id")
            if comp == NBA_COMPETITION_ID and g.get("isOpenForBetting") and g["id"] not in seen:
                seen.add(g["id"])
                games.append(g)
                found += 1
        # stop at the end of the list, or once NBA games have run out
        if not batch or skip >= data.get("totalCount", 0) or (games and not found):
            break
        time.sleep(random.uniform(1, 2))
    games.sort(key=lambda g: g.get("startDate", ""))
    return games


def scrape(b, limit):
    pulled = datetime.now(timezone.utc).isoformat(timespec="seconds")
    games = find_nba_games(b)
    log.info("Found %d open NBA games", len(games))
    rows, skipped = [], 0
    for g in games[:limit]:
        time.sleep(random.uniform(2, 4))          # go slow on purpose
        name = g.get("name", {}).get("value", str(g["id"]))
        try:
            q = {**b.base(), **VIEW_QS, "fixtureIds": g["id"]}
            fx = b.get_json("/cds-api/bettingoffer/fixture-view", q)["fixture"]
            got = parse_fixture(fx, pulled)
        except Blocked:
            raise
        except (FetchError, KeyError) as e:
            skipped += 1
            log.warning("Skipped %s: %s", name, e)
            continue
        log.info("  %-45s %4d props", name[:45], len(got))
        rows.extend(got)
    rows.sort(key=lambda r: (r["start_utc"], r["game"], r["player_norm"], r["market"], r["line"]))
    return rows, skipped


# ---------- output ----------

def write_latest(rows, now):
    """Write to a temp file, then swap it in, so a crash never leaves a half file."""
    tmp = LATEST.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig: Excel shows accents
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    try:
        os.replace(tmp, LATEST)
        return LATEST
    except PermissionError:                       # file is open in Excel
        alt = HERE / ("props_betmgm_%s.csv" % now.strftime("%Y%m%d_%H%M"))
        os.replace(tmp, alt)
        log.warning("%s is open in another program. Saved to %s instead.", LATEST.name, alt.name)
        return alt


def append_history(rows):
    new_file = not HISTORY.exists()
    try:
        with open(HISTORY, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            w.writerows(rows)
    except PermissionError:
        log.warning("%s is open in another program. Skipped history this run.", HISTORY.name)


# ---------- run ----------

def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in (RotatingFileHandler(LOG_FILE, maxBytes=1000000, backupCount=3, encoding="utf-8"),
              logging.StreamHandler()):
        h.setFormatter(fmt)
        log.addHandler(h)


def take_lock():
    """Stop two runs from fighting over the same Chrome profile."""
    if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime < 1800:
        return False
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def open_browser(mode):
    for attempt in (1, 2):                        # one retry if the page doesn't load
        try:
            return Browser(mode)
        except FetchError as e:
            if attempt == 2:
                raise
            log.warning("%s Retrying in 10s...", e)
            time.sleep(10)


def run(args):
    mode = "show" if args.show else "headless" if args.headless else "offscreen"
    b = open_browser(mode)
    try:
        rows, skipped = scrape(b, args.limit)
    finally:
        b.close()
    if not rows:
        log.error("No props found. Keeping the previous file untouched.")
        return 1
    now = datetime.now()
    saved = write_latest(rows, now)
    append_history(rows)
    games = len({r["fixture_id"] for r in rows})
    log.info("OK: %d props from %d games -> %s%s", len(rows), games, saved.name,
             " (%d games skipped)" % skipped if skipped else "")
    return 0


def main():
    ap = argparse.ArgumentParser(description="BetMGM NBA player props -> CSV")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--show", action="store_true", help="show the Chrome window")
    g.add_argument("--headless", action="store_true", help="no window (may be blocked)")
    ap.add_argument("--limit", type=int, default=30, help="max games per run")
    args = ap.parse_args()

    setup_logging()
    if not take_lock():
        log.error("Another run is in progress (or crashed less than 30 min ago). Exiting.")
        return 1
    try:
        return run(args)
    except Blocked as e:
        log.error("Blocked: %s. Wait a few hours, then try again with --show.", e)
        return 2
    except KeyboardInterrupt:
        log.info("Stopped.")
        return 130
    except Exception:
        log.exception("Run failed")
        return 1
    finally:
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
