#!/usr/bin/env python3
"""
Global Signal — weekly tech newsletter generator.

What this does, every time it runs:
  1. Reads a list of public tech-news RSS feeds from around the world.
  2. Collects articles published since the last successful run.
  3. Sends each one to the Gemini API, which writes a short summary and
     tags which country the story is mainly about.
  4. Groups everything by country (whichever countries actually had
     news that week — nothing is hardcoded).
  5. Saves the week's data as JSON under data/<year>/<week_id>.json.
  6. Rebuilds the static site in docs/ so GitHub Pages can serve it.

If a run finds zero qualifying stories, it still records that the check
happened (status "no_new_news") so the site can show "checked, nothing
to report" instead of looking broken or abandoned.

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
    {"name": "Engadget", "url": "https://www.engadget.com/rss.xml"},
    {"name": "BBC Technology", "url": "http://feeds.bbci.co.uk/news/technology/rss.xml"},
    {"name": "The Register", "url": "https://www.theregister.com/headlines.atom"},
    {"name": "Rest of World", "url": "https://restofworld.org/feed/latest/"},
    {"name": "Tech in Asia", "url": "https://www.techinasia.com/feed"},
    {"name": "Inc42 (India)", "url": "https://inc42.com/feed/"},
    {"name": "Sifted (Europe)", "url": "https://sifted.eu/feed"},
    {"name": "CNBC Technology", "url": "https://www.cnbc.com/id/19854910/device/rss/rss.html"},
]

MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"

MAX_CANDIDATES_PER_RUN = 60      # caps LLM calls so a run always fits the free tier
MAX_STORIES_PER_COUNTRY = 5
FIRST_RUN_LOOKBACK_DAYS = 7      # if there's no prior state, only look back this far
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


def collect_candidates(cutoff):
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
    return candidates[:MAX_CANDIDATES_PER_RUN]


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
# Step 3: assemble the week's data file
# ---------------------------------------------------------------------------

def iso_week_id(dt):
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def build_week_data(stories, run_dt):
    by_country = {}
    for s in stories:
        by_country.setdefault(s["country"], []).append(s)
    for country, items in by_country.items():
        items.sort(key=lambda s: s["importance"], reverse=True)
        by_country[country] = items[:MAX_STORIES_PER_COUNTRY]

    countries = [
        {"name": name, "stories": items}
        for name, items in sorted(by_country.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]

    return {
        "week_id": iso_week_id(run_dt),
        "run_timestamp": run_dt.isoformat(),
        "status": "updated" if stories else "no_new_news",
        "message": None if stories else "Checked all sources this week — nothing that cleared the relevance/importance bar.",
        "total_stories": len(stories),
        "countries": countries,
    }


def save_week_data(week_data, run_dt):
    year_dir = DATA_DIR / str(run_dt.year)
    year_dir.mkdir(parents=True, exist_ok=True)
    path = year_dir / f"{week_data['week_id']}.json"
    path.write_text(json.dumps(week_data, indent=2))
    return path


# ---------------------------------------------------------------------------
# Step 4: render the static site
# ---------------------------------------------------------------------------

def load_all_weeks():
    weeks = []
    if not DATA_DIR.exists():
        return weeks
    for year_dir in sorted(DATA_DIR.glob("*")):
        if not year_dir.is_dir():
            continue
        for path in sorted(year_dir.glob("*.json")):
            try:
                weeks.append(json.loads(path.read_text()))
            except json.JSONDecodeError:
                continue
    weeks.sort(key=lambda w: w["run_timestamp"], reverse=True)
    return weeks


def format_date_label(iso_ts):
    dt = datetime.fromisoformat(iso_ts)
    return dt.strftime("%B %-d, %Y") if os.name != "nt" else dt.strftime("%B %d, %Y")


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def to_dispatches(week_data):
    dispatches = []
    for c in week_data["countries"]:
        dispatches.append({
            "anchor": slugify(c["name"]),
            "place": c["name"],
            "date_label": format_date_label(week_data["run_timestamp"]),
            "lit": True,
            "stories": c["stories"],
            "empty_message": "",
        })
    if not dispatches:
        dispatches.append({
            "anchor": "status",
            "place": "Status",
            "date_label": format_date_label(week_data["run_timestamp"]),
            "lit": False,
            "stories": [],
            "empty_message": week_data.get("message") or "No notable tech developments found this week.",
        })
    return dispatches


def run_note_text(week_data):
    label = format_date_label(week_data["run_timestamp"])
    if week_data["status"] == "updated":
        n = week_data["total_stories"]
        n_countries = len(week_data["countries"])
        return f"<strong>Checked {label}</strong> — {n} stor{'y' if n == 1 else 'ies'} across {n_countries} countr{'y' if n_countries == 1 else 'ies'}."
    return f"<strong>Checked {label}</strong> — the agent ran on schedule; nothing cleared the bar for inclusion."


def ticker_text_for(week_data):
    label = format_date_label(week_data["run_timestamp"])
    if week_data["status"] == "updated":
        return f"LAST CHECK: {label} · {week_data['total_stories']} STORIES · {len(week_data['countries'])} COUNTRIES · NEXT CHECK: MONDAY"
    return f"LAST CHECK: {label} · NO NOTABLE DEVELOPMENTS · NEXT CHECK: MONDAY"


def render_site(all_weeks):
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=False)
    week_tpl = env.get_template("week.html")
    archive_tpl = env.get_template("archive.html")

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "weeks").mkdir(parents=True, exist_ok=True)

    if all_weeks:
        latest = all_weeks[0]
        countries_nav = [{"name": c["name"], "count": len(c["stories"]), "anchor": slugify(c["name"])} for c in latest["countries"]]

        index_html = week_tpl.render(
            page_title="Latest",
            asset_prefix="",
            ticker_text=ticker_text_for(latest),
            countries=countries_nav,
            run_note=run_note_text(latest),
            dispatches=to_dispatches(latest),
        )
        (DOCS_DIR / "index.html").write_text(index_html)

        for week_data in all_weeks:
            countries_nav_w = [{"name": c["name"], "count": len(c["stories"]), "anchor": slugify(c["name"])} for c in week_data["countries"]]
            page_html = week_tpl.render(
                page_title=week_data["week_id"],
                asset_prefix="../",
                ticker_text=ticker_text_for(week_data),
                countries=countries_nav_w,
                run_note=run_note_text(week_data),
                dispatches=to_dispatches(week_data),
            )
            (DOCS_DIR / "weeks" / f"{week_data['week_id']}.html").write_text(page_html)

    archive_rows = [
        {
            "week_id": w["week_id"],
            "date_label": format_date_label(w["run_timestamp"]),
            "status_label": f"{w['total_stories']} stories" if w["status"] == "updated" else "checked, nothing to report",
        }
        for w in all_weeks
    ]
    archive_html = archive_tpl.render(
        page_title="Archive",
        asset_prefix="",
        ticker_text=ticker_text_for(all_weeks[0]) if all_weeks else "NO RUNS RECORDED YET",
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
        state = load_state()
        cutoff = get_cutoff(state)
        print(f"[info] looking for articles published after {cutoff.isoformat()}")
        candidates = collect_candidates(cutoff)
        print(f"[info] {len(candidates)} candidate articles found across {len(FEEDS)} feeds")
        stories = summarize_candidates(candidates, api_key)
        print(f"[info] {len(stories)} stories judged relevant after tagging")
        save_state({"last_run_utc": run_dt.isoformat()})

    week_data = build_week_data(stories, run_dt)
    path = save_week_data(week_data, run_dt)
    print(f"[info] wrote {path}")

    all_weeks = load_all_weeks()
    render_site(all_weeks)
    print(f"[info] site rebuilt in {DOCS_DIR}")


if __name__ == "__main__":
    main()
