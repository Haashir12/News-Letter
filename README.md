# Global Signal — a self-updating weekly tech newsletter

A static website that automatically republishes itself every Monday with
the week's most notable tech news, grouped by whichever countries actually
had notable developments that week. An AI model (Google Gemini, free tier)
writes the short summaries and decides which country each story belongs to.
Everything — hosting, automation, and the AI calls — runs at **$0/month**.

**How it works, in one sentence:** a script reads a list of tech-news RSS
feeds, asks Gemini to summarize and tag each new article, saves the result
as a JSON file, and rebuilds the plain HTML site — and a GitHub Action runs
that script automatically every week.

```
RSS feeds  →  Gemini API (summarize + tag country)  →  data/2026/2026-W36.json  →  site in docs/
   (free)         (free tier)                              (versioned history)      (GitHub Pages, free)
```

If a week produces zero qualifying stories, the site still records that the
check happened — you'll see "Checked [date] — nothing cleared the bar" with
a dimmed marker instead of a story, so it's obvious the agent ran rather
than silently failing.

---

## What you need before you start

- A free [GitHub](https://github.com) account.
- A free [Google AI Studio](https://aistudio.google.com/apikey) API key (no
  credit card required for the free tier, as of writing).
- About 20 minutes for the one-time setup below.

---

## Step 1 — Create the repository

1. On GitHub, click **New repository**.
2. Make it **public**. (Public repos get unlimited free GitHub Actions
   minutes; private repos are capped at 2,000 free minutes/month, which is
   still far more than this needs — but public is the simplest zero-cost
   choice, and GitHub Pages on a free account requires it anyway.)
3. Name it whatever you like, e.g. `global-signal`.
4. Upload every file from this project into the repo, keeping the folder
   structure exactly as-is (`.github/workflows/...`, `scripts/...`,
   `templates/...`, `data/...`, `docs/...`). The easiest way if you're not
   using `git` on the command line: use GitHub's "Add file → Upload files"
   in the web UI and drag the whole folder in, or install
   [GitHub Desktop](https://desktop.github.com/) and push the folder.

## Step 2 — Get a free Gemini API key

1. Go to <https://aistudio.google.com/apikey>.
2. Sign in and click **Create API key**.
3. Copy the key somewhere safe — you'll paste it once in the next step.

The free tier gives enough daily requests to comfortably cover a weekly
batch of articles (the script caps itself at 60 articles per run either
way, so you'll never come close to the limit).

## Step 3 — Add the key as a repo secret

1. In your repo, go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret**.
3. Name: `GEMINI_API_KEY`. Value: paste the key from Step 2.
4. Save.

This keeps the key out of your code — the workflow reads it securely at
run time.

## Step 4 — Let the workflow commit its own updates

1. Go to **Settings → Actions → General**.
2. Scroll to **Workflow permissions**.
3. Select **Read and write permissions**.
4. Save.

Without this, the weekly job can generate the update but won't be allowed
to save it back to the repo.

## Step 5 — Turn on GitHub Pages

1. Go to **Settings → Pages**.
2. Under **Build and deployment → Source**, choose **Deploy from a branch**.
3. Branch: `main`, folder: `/docs`. Save.
4. GitHub will give you a URL like `https://yourusername.github.io/global-signal/`.
   It'll 404 until Step 6 generates the first page — that's expected.

## Step 6 — Run it once, manually

1. Go to the **Actions** tab → **Weekly Tech Newsletter Update** → **Run workflow**.
2. Wait 1-3 minutes for it to finish (it's fetching feeds and calling the
   AI for each article, so it isn't instant).
3. Visit your Pages URL from Step 5 — you should see this week's dispatch.

From here on, it runs by itself every Monday at 06:00 UTC. You can also
trigger it manually any time from the Actions tab.

---

## Customizing it

- **Add or remove news sources:** edit the `FEEDS` list near the top of
  `scripts/generate_newsletter.py`. Each entry just needs a name and an
  RSS URL. If a feed URL ever goes stale, the script logs a warning and
  skips it instead of failing the whole run — check the Actions log if a
  source seems to have stopped appearing.
- **Change how many stories per country:** `MAX_STORIES_PER_COUNTRY`.
- **Change the schedule:** edit the `cron` line in
  `.github/workflows/weekly-update.yml`. ([crontab.guru](https://crontab.guru)
  is useful for writing these.)
- **Preview design changes without using the AI or the internet:** run
  `python scripts/generate_newsletter.py --mock` locally — it builds the
  site from a few fake sample stories so you can check `templates/` and
  `docs/assets/style.css` changes instantly.
- **Look and feel:** all styling is in `docs/assets/style.css`; page
  structure is in `templates/`.

## Cost and limits — what "free" actually means here

| Piece | Cost | Notes |
|---|---|---|
| GitHub Pages hosting | $0 | Free for public repos, no bandwidth billing at this scale |
| GitHub Actions (the weekly run) | $0 | Unlimited minutes on public repos; this job takes a few minutes/week |
| RSS feeds | $0 | Public feeds from each outlet |
| Gemini API (summarizing) | $0 | Google's free tier, no card required, as of writing rate-limited to roughly 10-15 requests/minute and a few hundred to ~1,000 requests/day depending on model — comfortably above a weekly batch of ~60 articles |

Two honest caveats worth knowing:
- Google's free tier lets them use free-tier inputs/outputs to improve
  their models. The input here is just public news headlines, so the
  privacy exposure is low, but it's worth knowing.
- Free-tier terms and limits do change over time (they were tightened
  once already, in December 2025) — if the script starts failing with
  rate-limit errors, check the current limits on the Google AI Studio
  pricing page before assuming something's broken.

## Troubleshooting

- **Action failed:** open the Actions tab, click the failed run, and read
  the log — it prints a `[warn]` or `[error]` line explaining what
  happened (a dead feed, a rate limit, a missing secret).
- **Site looks empty after Pages is enabled:** you haven't run the
  workflow yet — do Step 6.
- **A country you expected doesn't show up:** the AI decides country
  relevance per-article from the text; if no collected article was mainly
  about that country that week, it won't appear. That's the "dynamic"
  behavior working as intended, not a bug.
- **Want to reprocess older news for testing:** delete `data/state.json`
  before a manual run — the script will then look back
  `FIRST_RUN_LOOKBACK_DAYS` (7, by default) instead of from the last run.
