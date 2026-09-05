#!/usr/bin/env python3
"""
Global Signal — daily tech newsletter generator.

What this does, every time it runs:
  1. Reads a list of public tech-news RSS feeds from around the world.
  2. Collects articles published since the last successful run (usually
     about 24 hours, since this runs daily).
  3. Sends each one to the Gemini API, which writes a short summary and
     tags which country the story is mainly about.
  4. Groups everything by country (whichever countries actually had
     news that day — nothing is hardcoded).
  5. Saves that day's data as JSON under data/<year>/<YYYY-MM-DD>.json.
     Every day gets its own permanent file — nothing is ever overwritten.
  6. Rebuilds the static site in docs/:
       - docs/index.html is a ROLLING digest of the last ROLLING_WINDOW_DAYS
         days, merged by country. A single quiet day never makes the
         homepage look empty — it always shows the recent past.
       - docs/days/<date>.html is that one day's dispatch on its own,
         permanently archived.
       - docs/archive.html lists every day the agent has ever checked.

If a run finds zero qualifying stories, it still records that the check
happened (status "no_new_news") so the site can show "checked, nothing
to report" instead of looking broken or abandoned — the rolling digest
on the homepage keeps showing recent coverage regardless.

Run modes:
  python generate_newsletter.py            normal run (needs GEMINI_API_KEY)
  python generate_newsletter.py --mock     no network/API calls; builds
                                            the site from fake sample data,
                                            useful to preview the design
                                            or test changes to templates.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

# ---------------------------------------------------------------------------
# Configuration — edit this list to add or remove news sources.
# Aim for a spread of countries/regions so the "top countries" section is
# actually global rather than US/UK-only. If a feed URL goes stale, the
# script logs a warning and skips it rather than failing the whole run.
# ---------------------------------------------------------------------------

FEEDS = [
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "Wired", "url": "https://www.wired.com/feed/rss"},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"name": "BBC Technology", "url": "http://feeds.bbci.co.uk/news/technology/rss.xml"},
    {"name": "The Register", "url": "https://www.theregister.com/headlines.atom"},
    {"name": "Rest of World", "url": "https://restofworld.org/feed/latest/"},
    {"name": "Tech in Asia", "url": "https://www.techinasia.com/feed"},
    {"name": "Inc42 (India)", "url": "https://inc42.com/feed/"},
    {"name": "Sifted (Europe)", "url": "https://sifted.eu/feed"},
    {"name": "TechCabal (Africa)", "url": "https://techcabal.com/feed/"},
    {"name": "e27 (Southeast Asia)", "url": "https://e27.co/feed/"},
    {"name": "Wamda (Middle East)", "url": "https://www.wamda.com/feed"},
    {"name": "Startup Daily (Australia)", "url": "https://www.startupdaily.net/feed/"},
    {"name": "BetaKit (Canada)", "url": "https://betakit.com/feed/"},
    {"name": "CNBC Technology", "url": "https://www.cnbc.com/id/19854910/device/rss/rss.html"},
]

MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"

MAX_CANDIDATES_PER_RUN = 60          # caps LLM calls so a run always fits the free tier
MAX_STORIES_PER_COUNTRY_DAY = 5      # cap per country on that day's own page
MAX_STORIES_PER_COUNTRY_WINDOW = 8   # cap per country on the rolling homepage digest
ROLLING_WINDOW_DAYS = 30             # homepage always shows at least this much history
FIRST_RUN_LOOKBACK_DAYS = 7          # if there's no prior state, only look back this far
FEED_TIMEOUT_SECONDS = 12
ARTICLE_FETCH_TIMEOUT_SECONDS = 8

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
TEMPLATES_DIR = ROOT / "templates"
STATE_FILE = DATA_DIR / "state.json"

HEADERS = {"User-Agent": "GlobalSignalBot/1.0 (+weekly tech news digest; contact: repo owner)"}


# ---------------------------------------------------------------------------
# Step 1: fetch candidate articles from RSS feeds
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"last_run_utc": None}


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_cutoff(state):
    if state.get("last_run_utc"):
        try:
            return datetime.fromisoformat(state["last_run_utc"])
        except ValueError:
            pass
    return datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)


def entry_published(entry):
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc)
    return None


def extract_image(entry):
    media = entry.get("media_content") or entry.get("media_thumbnail")
    if media and isinstance(media, list) and media[0].get("url"):
        return media[0]["url"]
    for link in entry.get("links", []):
        if str(link.get("type", "")).startswith("image"):
            return link.get("href")
    # Some feeds embed an <img> in the summary HTML.
    summary_html = entry.get("summary", "")
    if summary_html:
        soup = BeautifulSoup(summary_html, "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            return img["src"]
    return None


def fetch_og_image(url):
    """Best-effort fallback: grab the article page's og:image meta tag."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=ARTICLE_FETCH_TIMEOUT_SECONDS)
        soup = BeautifulSoup(resp.text, "html.parser")
        tag = soup.find("meta", property="og:image")
        if tag and tag.get("content"):
            return tag["content"]
    except requests.RequestException:
        pass
    return None


def fetch_article_excerpt(url, max_chars=1200):
    """Best-effort fallback text for feeds with a very short RSS summary."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=ARTICLE_FETCH_TIMEOUT_SECONDS)
        soup = BeautifulSoup(resp.text, "html.parser")
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = " ".join(paragraphs)
        return text[:max_chars]
    except requests.RequestException:
        return ""


def collect_candidates(cutoff, limit=MAX_CANDIDATES_PER_RUN):
    candidates = []
    seen_links = set()
    for feed in FEEDS:
        try:
            parsed = feedparser.parse(feed["url"], request_headers=HEADERS)
            if parsed.bozo and not parsed.entries:
                print(f"[warn] could not read feed '{feed['name']}': {parsed.bozo_exception}")
                continue
        except Exception as exc:  # noqa: BLE001 - a single bad feed must not kill the run
            print(f"[warn] error fetching feed '{feed['name']}': {exc}")
            continue

        for entry in parsed.entries:
            link = entry.get("link")
            if not link or link in seen_links:
                continue
            published = entry_published(entry)
            if published and published < cutoff:
                continue
            seen_links.add(link)
            candidates.append({
                "title": entry.get("title", "").strip(),
                "link": link,
                "source": feed["name"],
                "published": published.isoformat() if published else None,
                "raw_summary": BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True),
                "image_url": extract_image(entry),
            })

    # Most recent first, then cap so the LLM budget stays predictable.
    candidates.sort(key=lambda c: c["published"] or "", reverse=True)
    return candidates[:limit]


# ---------------------------------------------------------------------------
# Step 2: summarize + tag country with Gemini (free tier)
# ---------------------------------------------------------------------------

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_relevant": {"type": "BOOLEAN"},
        "country": {"type": "STRING"},
        "summary": {"type": "STRING"},
        "importance": {"type": "INTEGER"},
    },
    "required": ["is_relevant", "country", "summary", "importance"],
}

PROMPT_TEMPLATE = """You are tagging one tech-news article for a weekly global tech digest.

Headline: {title}
Source: {source}
Article text (may be partial): {text}

Decide:
- is_relevant: true only if this is genuinely about technology / IT / software / hardware / the tech industry
  (product launches, AI, chips, telecom, cybersecurity, tech policy, tech company business news, etc).
  False for unrelated news that merely mentions a tech company in passing.
- country: the single country this story is mainly ABOUT (where the news is happening / who it most affects),
  using a plain English country name (e.g. "United States", "South Korea", "United Arab Emirates").
  Use "Global" only if it genuinely has no single-country focus (e.g. an open-source project release, an
  international standards decision).
- summary: a short, factual paragraph a reader can act on. Write 2 sentences for a minor/incremental update,
  and up to 5-6 sentences if the development is genuinely major. Do not pad a small story with filler
  sentences just to reach a target length. Plain, direct language, no hype words.
- importance: your estimate of how significant this story is to a global tech audience, 1 (minor) to 10 (huge).

Respond with JSON only, matching the schema."""


def call_gemini(api_key, title, source, text, retries=3):
    prompt = PROMPT_TEMPLATE.format(title=title, source=source, text=text[:2000] or "(no extra text available)")
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "temperature": 0.3,
        },
    }
    for attempt in range(retries):
        try:
            resp = requests.post(
                GEMINI_URL,
                params={"key": api_key},
                json=body,
                timeout=30,
            )
            if resp.status_code == 429:
                wait = 2 ** attempt * 5
                print(f"[warn] rate limited by Gemini, waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            payload = resp.json()
            text_out = payload["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text_out)
        except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError) as exc:
            print(f"[warn] Gemini call failed (attempt {attempt + 1}/{retries}): {exc}")
            time.sleep(2)
    return None


def summarize_candidates(candidates, api_key):
    results = []
    for i, c in enumerate(candidates):
        text = c["raw_summary"]
        if len(text) < 200:
            text += " " + fetch_article_excerpt(c["link"])
        tagged = call_gemini(api_key, c["title"], c["source"], text)
        if not tagged or not tagged.get("is_relevant"):
            continue
        image_url = c["image_url"] or fetch_og_image(c["link"])
        results.append({
            "headline": c["title"],
            "summary": tagged["summary"],
            "country": tagged.get("country") or "Global",
            "importance": int(tagged.get("importance", 5)),
            "source": c["source"],
            "source_url": c["link"],
            "image_url": image_url,
            "published": c["published"],
        })
        # A small, polite pause keeps us comfortably inside free-tier RPM limits.
        time.sleep(1.5)
    return results


# ---------------------------------------------------------------------------
# Mock mode — lets you preview/test the site without a network call or API key
# ---------------------------------------------------------------------------

def mock_stories():
    return [
        {"headline": "Startup unveils new on-device AI chip for phones", "summary": "A Bay Area startup announced a low-power chip designed to run large-language-model inference directly on smartphones, cutting cloud costs for manufacturers. Two mid-tier phone makers said they plan to evaluate it for 2027 models.", "country": "United States", "importance": 6, "source": "TechCrunch", "source_url": "https://example.com/a", "image_url": None, "published": None},
        {"headline": "Seoul expands subsidy for chip equipment makers", "summary": "South Korea's government widened a tax credit for domestic semiconductor equipment suppliers, aiming to reduce reliance on imported lithography tools.", "country": "South Korea", "importance": 5, "source": "Tech in Asia", "source_url": "https://example.com/b", "image_url": None, "published": None},
        {"headline": "UK regulator opens probe into cloud pricing practices", "summary": "Britain's competition authority opened a formal review of bundled discount practices among the country's largest cloud providers, following complaints from smaller hosting firms.", "country": "United Kingdom", "importance": 7, "source": "BBC Technology", "source_url": "https://example.com/c", "image_url": None, "published": None},
        {"headline": "Open-source database project reaches 2.0 release", "summary": "A widely used open-source database project shipped its 2.0 release with a rewritten storage engine, claiming significant gains on write-heavy workloads.", "country": "Global", "importance": 4, "source": "Ars Technica", "source_url": "https://example.com/d", "image_url": None, "published": None},
    ]


# ---------------------------------------------------------------------------
# Step 3: assemble that day's data file
# ---------------------------------------------------------------------------

def day_id(dt):
    return dt.strftime("%Y-%m-%d")


def build_day_data(stories, run_dt):
    by_country = {}
    for s in stories:
        by_country.setdefault(s["country"], []).append(s)
    for country, items in by_country.items():
        items.sort(key=lambda s: s["importance"], reverse=True)
        by_country[country] = items[:MAX_STORIES_PER_COUNTRY_DAY]

    countries = [
        {"name": name, "stories": items}
        for name, items in sorted(by_country.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]

    return {
        "date": day_id(run_dt),
        "run_timestamp": run_dt.isoformat(),
        "status": "updated" if stories else "no_new_news",
        "message": None if stories else "Checked all sources today — nothing that cleared the relevance/importance bar.",
        "total_stories": len(stories),
        "countries": countries,
    }


def save_day_data(day_data, run_dt):
    year_dir = DATA_DIR / str(run_dt.year)
    year_dir.mkdir(parents=True, exist_ok=True)
    path = year_dir / f"{day_data['date']}.json"
    path.write_text(json.dumps(day_data, indent=2))
    return path


# ---------------------------------------------------------------------------
# Step 4: render the static site
# ---------------------------------------------------------------------------

def load_all_days():
    """Loads every day's JSON file ever saved. Tolerates the old
    week-based file format from before daily runs started, so nothing
    from before this change is lost."""
    days = []
    if not DATA_DIR.exists():
        return days
    for year_dir in sorted(DATA_DIR.glob("*")):
        if not year_dir.is_dir():
            continue
        for path in sorted(year_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            if "date" not in data and "run_timestamp" in data:
                data["date"] = data["run_timestamp"][:10]  # legacy week-based file
            days.append(data)
    days.sort(key=lambda d: d["run_timestamp"], reverse=True)
    return days


def format_date_label(iso_ts):
    dt = datetime.fromisoformat(iso_ts)
    return dt.strftime("%B %-d, %Y") if os.name != "nt" else dt.strftime("%B %d, %Y")


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def to_dispatches(day_data):
    """Builds the section list for a single day's own archive page."""
    dispatches = []
    for c in day_data["countries"]:
        stories = []
        for s in c["stories"]:
            story = dict(s)
            story["date_label"] = format_date_label(story.get("published") or day_data["run_timestamp"])
            stories.append(story)
        dispatches.append({
            "anchor": slugify(c["name"]),
            "place": c["name"],
            "date_label": format_date_label(day_data["run_timestamp"]),
            "lit": True,
            "stories": stories,
            "empty_message": "",
        })
    if not dispatches:
        dispatches.append({
            "anchor": "status",
            "place": "Status",
            "date_label": format_date_label(day_data["run_timestamp"]),
            "lit": False,
            "stories": [],
            "empty_message": day_data.get("message") or "No notable tech developments found today.",
        })
    return dispatches


def aggregate_recent(all_days, window_days=ROLLING_WINDOW_DAYS):
    """Merges every story within the window into one country-grouped view,
    so the homepage always has substance even when today was quiet. Ages
    stories out by their own real publish date, not by which day's batch
    they were fetched in — this matters for backfilled data, where many
    stories with different real dates can land in a single day's file."""
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=window_days)
    days_in_window = set()

    by_country = {}
    for d in all_days:
        for c in d["countries"]:
            for s in c["stories"]:
                pub_raw = s.get("published") or d["run_timestamp"]
                try:
                    pub_dt = datetime.fromisoformat(pub_raw)
                except ValueError:
                    continue
                if pub_dt < cutoff_dt:
                    continue
                days_in_window.add(d["date"])
                story = dict(s)
                story["date_label"] = format_date_label(pub_raw)
                by_country.setdefault(c["name"], []).append(story)

    for name, items in by_country.items():
        # Most important first; recency breaks ties.
        items.sort(key=lambda s: (s["importance"], s.get("published") or ""), reverse=True)
        by_country[name] = items[:MAX_STORIES_PER_COUNTRY_WINDOW]

    countries = [
        {"name": name, "stories": items}
        for name, items in sorted(by_country.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        if items
    ]

    return {
        "countries": countries,
        "total_stories": sum(len(c["stories"]) for c in countries),
        "window_days": window_days,
        "days_included": len(days_in_window),
    }


def to_digest_dispatches(aggregate):
    dispatches = []
    for c in aggregate["countries"]:
        dispatches.append({
            "anchor": slugify(c["name"]),
            "place": c["name"],
            "date_label": f"last {aggregate['window_days']} days",
            "lit": True,
            "stories": c["stories"],
            "empty_message": "",
        })
    if not dispatches:
        dispatches.append({
            "anchor": "status",
            "place": "Status",
            "date_label": f"last {aggregate['window_days']} days",
            "lit": False,
            "stories": [],
            "empty_message": "No stories have cleared the relevance bar yet in this window. Check back after the next run.",
        })
    return dispatches


def run_note_for_day(day_data):
    label = format_date_label(day_data["run_timestamp"])
    if day_data["status"] == "updated":
        n = day_data["total_stories"]
        n_countries = len(day_data["countries"])
        return f"<strong>Checked {label}</strong> — {n} stor{'y' if n == 1 else 'ies'} across {n_countries} countr{'y' if n_countries == 1 else 'ies'}."
    return f"<strong>Checked {label}</strong> — the agent ran on schedule; nothing cleared the bar for inclusion."


def run_note_for_digest(latest_day, aggregate):
    today_note = run_note_for_day(latest_day)
    if aggregate["total_stories"]:
        window_note = f" Showing {aggregate['total_stories']} stories from the last {aggregate['window_days']} days across {len(aggregate['countries'])} countries."
    else:
        window_note = f" No stories found in the last {aggregate['window_days']} days yet — check back after the next run."
    return today_note + window_note


def ticker_text_for_day(day_data):
    label = format_date_label(day_data["run_timestamp"])
    if day_data["status"] == "updated":
        return f"LAST CHECK: {label} · {day_data['total_stories']} STORIES · {len(day_data['countries'])} COUNTRIES · NEXT CHECK: TOMORROW"
    return f"LAST CHECK: {label} · NO NOTABLE DEVELOPMENTS · NEXT CHECK: TOMORROW"


def render_site(all_days):
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=False)
    day_tpl = env.get_template("week.html")       # single-day dispatch page
    digest_tpl = env.get_template("digest.html")  # rolling homepage
    archive_tpl = env.get_template("archive.html")

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "days").mkdir(parents=True, exist_ok=True)

    if all_days:
        latest = all_days[0]
        aggregate = aggregate_recent(all_days)
        countries_nav = [{"name": c["name"], "count": len(c["stories"]), "anchor": slugify(c["name"])} for c in aggregate["countries"]]

        index_html = digest_tpl.render(
            page_title="Latest",
            asset_prefix="",
            ticker_text=ticker_text_for_day(latest),
            countries=countries_nav,
            run_note=run_note_for_digest(latest, aggregate),
            dispatches=to_digest_dispatches(aggregate),
        )
        (DOCS_DIR / "index.html").write_text(index_html)

        for day_data in all_days:
            countries_nav_d = [{"name": c["name"], "count": len(c["stories"]), "anchor": slugify(c["name"])} for c in day_data["countries"]]
            page_html = day_tpl.render(
                page_title=day_data["date"],
                asset_prefix="../",
                ticker_text=ticker_text_for_day(day_data),
                countries=countries_nav_d,
                run_note=run_note_for_day(day_data),
                dispatches=to_dispatches(day_data),
            )
            (DOCS_DIR / "days" / f"{day_data['date']}.html").write_text(page_html)

    archive_rows = [
        {
            "week_id": d["date"],
            "date_label": format_date_label(d["run_timestamp"]),
            "status_label": f"{d['total_stories']} stories" if d["status"] == "updated" else "checked, nothing to report",
        }
        for d in all_days
    ]
    archive_html = archive_tpl.render(
        page_title="Archive",
        asset_prefix="",
        ticker_text=ticker_text_for_day(all_days[0]) if all_days else "NO RUNS RECORDED YET",
        countries=[],
        weeks=archive_rows,
    )
    (DOCS_DIR / "archive.html").write_text(archive_html)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="Build the site from fake sample data; no network or API key needed.")
    parser.add_argument("--backfill-days", type=int, default=None, help="One-time wider lookback (e.g. 21) to pull in whatever older items the RSS feeds still carry. Use this once to seed real history; daily runs after that go back to normal incremental fetching.")
    args = parser.parse_args()

    run_dt = datetime.now(timezone.utc)

    if args.mock:
        print("[mock] using built-in sample stories, skipping fetch + Gemini calls")
        stories = mock_stories()
    else:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("[error] GEMINI_API_KEY environment variable is not set.")
            sys.exit(1)
        if args.backfill_days:
            cutoff = run_dt - timedelta(days=args.backfill_days)
            limit = max(MAX_CANDIDATES_PER_RUN, 200)
            print(f"[info] BACKFILL MODE: looking back {args.backfill_days} days, up to {limit} articles")
        else:
            state = load_state()
            cutoff = get_cutoff(state)
            limit = MAX_CANDIDATES_PER_RUN
            print(f"[info] looking for articles published after {cutoff.isoformat()}")
        candidates = collect_candidates(cutoff, limit=limit)
        print(f"[info] {len(candidates)} candidate articles found across {len(FEEDS)} feeds")
        stories = summarize_candidates(candidates, api_key)
        print(f"[info] {len(stories)} stories judged relevant after tagging")
        save_state({"last_run_utc": run_dt.isoformat()})

    day_data = build_day_data(stories, run_dt)
    path = save_day_data(day_data, run_dt)
    print(f"[info] wrote {path}")

    all_days = load_all_days()
    render_site(all_days)
    print(f"[info] site rebuilt in {DOCS_DIR}")


if __name__ == "__main__":
    main()
