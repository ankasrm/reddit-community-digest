# Reddit Community Digest

A zero-infrastructure Slack digest of what your community is talking about on Reddit — plus a weekly, Claude-written summary of what matters for marketing and website content.

Runs entirely on GitHub Actions. No server, no database, no Reddit API key.

## What you get

**Every weekday** — a Slack post with:

- **At a glance** — the day's posts grouped by topic (pricing, migration, performance, support, …)
- **Top 10 posts** from your subreddits
- **Relevant for marketing** — posts that mention your brand, a competitor, an intent to switch, pricing, etc.
- **Secondary-language block** (optional) — posts in a second language that would otherwise be drowned out by the big English subreddits

**Every Friday** — a Slack post with:

- an overview of the week
- marketing takeaways (pain points, arguments you can use)
- competitor mentions (praise, criticism, reasons people switch)
- concrete content ideas for your website (working title + format)

Every link in the weekly post is guaranteed to come from a real Reddit thread that was collected during the week: Claude only sees numbered posts and refers to them by number; the URLs are resolved from the logs.

All fetched posts are stored as `data/YYYY-MM-DD.json` in the repo, so you can run your own analysis later.

## Setup (5 steps)

1. **Use this repo** — click *Use this template* (or fork/clone it).
2. **Edit `config.json`** — at minimum: your brand name, the subreddits, and the `brand` pattern in `relevance_concepts`. See [Configuration](#configuration).
3. **Create a Slack incoming webhook** — Slack → Apps → *Incoming Webhooks* → pick a channel → copy the URL.
4. **Add secrets** in your repo under *Settings → Secrets and variables → Actions*:
   | Secret | Required | What |
   |---|---|---|
   | `SLACK_WEBHOOK_URL` | yes | the webhook from step 3 |
   | `ANTHROPIC_API_KEY` | for the weekly summary | key from [console.anthropic.com](https://console.anthropic.com). Without it, the weekly post falls back to a rule-based list. |
   | `ANTHROPIC_WORKSPACE_ID` | only for identity-linked keys | if the run logs `anthropic-workspace-id is required`, add your workspace ID here — or create a regular workspace key instead. |
5. **Test** — *Actions → Daily Reddit Digest → Run workflow*. The first post should land in Slack within a minute. Then run *Weekly Reddit Summary* once a few daily logs exist.

The schedules live in `.github/workflows/*.yml` (cron in UTC). Adjust to your timezone.

## Configuration

Everything brand-specific lives in `config.json`. The shipped example is set up for a fictional web-hosting company ("ExampleHost") — replace it with your own brand, subreddits and competitors.

| Key | What it does |
|---|---|
| `brand.name`, `brand.description`, `brand.audiences`, `brand.website` | Used in the weekly prompt so Claude writes from your perspective. |
| `sources.subreddits` | Main subreddits, fetched with `sources.timeframe` (usually `day`). |
| `sources.secondary_subreddits` | Small subreddits that don't have posts every day. Fetched with `secondary_timeframe` (usually `week`). Their posts always count as secondary-language. |
| `language_block` | Optional block for a second language. `markers` are word fragments; a post with `min_marker_hits` or more is treated as that language. Set `enabled: false` to drop the block. |
| `digest.*` | Top-N sizes, thresholds for the marketing block, pause between requests. |
| `keywords` | Multi-word phrases that boost a post in the Top-N ranking (each hit = +50 points). |
| `relevance_concepts` | The heart of the marketing block. Each concept has a `weight` and regex `patterns` (matched with word boundaries). A concept counts once per post. Keep a concept named `brand` — it is used for the "brand mentioned" counter. |
| `topic_categories` | Fragments for the "At a glance" grouping. First match wins, so put specific categories first. |
| `weekly.*` | Title, output language, Claude model, lookback days, candidate limits. |

A post enters the marketing block when its relevance score is at least `marketing_min_score` **or** it hits at least `marketing_min_concepts` different concepts. The defaults (8 / 2) mean: a single tool mention is not enough, but "competitor + switching" is.

## How it works

```
digest.py (weekdays)                    weekly_summary.py (Fridays)
  Reddit JSON → RSS → old.reddit          data/*.json of the last 7 days
  dedup, score, categorize                dedup across days, pick candidates
  Slack post                              Claude → JSON (numbers only)
  data/YYYY-MM-DD.json  ──────────────▶   resolve URLs, Slack post
                                          archive/YYYY-Www.json
```

Reddit blocks most cloud IPs for its JSON endpoints, so the daily digest falls back to RSS. RSS delivers no upvotes or comment counts — on those days the Top 10 is Reddit's own top-of-day order, and the marketing block relies purely on the relevance concepts. Blocked sources (HTTP 403/429) are listed in the Slack footer so they never disappear silently.

## Running locally

```bash
pip install -r requirements.txt
python digest.py            # prints to stdout when SLACK_WEBHOOK_URL is unset
python weekly_summary.py    # same; uses the rule-based fallback without ANTHROPIC_API_KEY
```

## License

MIT
