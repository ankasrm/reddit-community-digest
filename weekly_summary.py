#!/usr/bin/env python3
"""
Reddit Community Digest - weekly summary
========================================

Reads the daily logs (data/YYYY-MM-DD.json) of the past week, asks Claude
for a marketing-oriented summary and posts it to Slack:

  overview -> marketing takeaways -> competitor mentions
  -> content ideas for your website -> secondary-language posts

Principles
----------
* digest.py and the daily logs are never modified - one data source, a
  second lens.
* Claude only ever sees numbered posts and refers to them by number. URLs
  are resolved here from the logs, so a made-up link is impossible.
* Without ANTHROPIC_API_KEY (or on an API error) a rule-based fallback
  posts the top posts per relevance concept. The weekly post never fails
  silently.

Environment
-----------
  SLACK_WEBHOOK_URL        required (otherwise printed to stdout)
  ANTHROPIC_API_KEY        optional, enables the Claude summary
  ANTHROPIC_WORKSPACE_ID   optional, only for identity-linked API keys
  ANTHROPIC_MODEL          optional, overrides weekly.model from config.json
  CONFIG_PATH              optional, default config.json
"""

import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import requests

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")
with open(CONFIG_PATH, encoding="utf-8") as _f:
    CFG = json.load(_f)

BRAND  = CFG["brand"]
LANG   = CFG.get("language_block", {"enabled": False})
WEEKLY = CFG.get("weekly", {})

LOG_DIR     = "data"
ARCHIVE_DIR = "archive"
WEEK_DAYS   = int(WEEKLY.get("days", 7))

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
API_KEY       = os.environ.get("ANTHROPIC_API_KEY", "")
WORKSPACE_ID  = os.environ.get("ANTHROPIC_WORKSPACE_ID", "")
MODEL         = os.environ.get("ANTHROPIC_MODEL", WEEKLY.get("model", "claude-sonnet-4-5"))
API_URL       = "https://api.anthropic.com/v1/messages"

MAX_CANDIDATES        = int(WEEKLY.get("max_candidates", 70))
CAND_TEXT_MAX         = 700
MIN_RELEVANCE         = int(WEEKLY.get("min_relevance", 3))
MAX_ITEMS_PER_SECTION = int(WEEKLY.get("max_items_per_section", 5))
SLACK_TEXT_MAX        = 2900
OUTPUT_LANGUAGE       = WEEKLY.get("output_language", "English")


# --------------------------------------------------------------------------
# 1. Load the week's logs
# --------------------------------------------------------------------------

def load_week():
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=WEEK_DAYS - 1)
    files, days = [], []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "*.json"))):
        stamp = os.path.basename(path)[:-5]
        try:
            d = datetime.strptime(stamp, "%Y-%m-%d").date()
        except ValueError:
            continue
        if start <= d <= today:
            files.append(path)
            days.append(stamp)

    posts_by_url, fetch_issues = {}, Counter()
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            print("[WARN] {} unreadable: {}".format(path, e), flush=True)
            continue
        stamp = payload.get("date", os.path.basename(path)[:-5])
        for sub, info in (payload.get("fetch_log") or {}).items():
            if any(k in json.dumps(info).lower() for k in ("429", "403", "err", "blocked")):
                fetch_issues[sub] += 1
        for p in payload.get("posts", []):
            url = p.get("url")
            if not url:
                continue
            # The same post shows up on several days (secondary subreddits
            # use a weekly window). Keep one record with the highest numbers.
            rec = posts_by_url.get(url)
            if rec is None:
                rec = dict(p)
                rec["days_seen"] = [stamp]
                posts_by_url[url] = rec
            else:
                rec["days_seen"].append(stamp)
                rec["ups"] = max(rec.get("ups") or 0, p.get("ups") or 0)
                rec["num_comments"] = max(rec.get("num_comments") or 0, p.get("num_comments") or 0)
                for flag in ("in_digest", "in_language_block", "in_marketing_block"):
                    rec[flag] = rec.get(flag) or p.get(flag)
                if len(p.get("text") or "") > len(rec.get("text") or ""):
                    rec["text"] = p.get("text")

    posts = list(posts_by_url.values())
    print("[OK] {} daily logs ({} .. {}), {} unique posts".format(
        len(files), days[0] if days else "-", days[-1] if days else "-", len(posts)), flush=True)
    return posts, days, fetch_issues


# --------------------------------------------------------------------------
# 2. Stats + candidates
# --------------------------------------------------------------------------

def stats(posts):
    by_group = Counter()
    for p in posts:
        for g in p.get("relevance_concepts", []):
            by_group[g] += 1
    return {
        "post_count": len(posts),
        "by_sub": dict(Counter(p.get("sub", "?") for p in posts)),
        "by_group": dict(by_group),
        "language_count": sum(1 for p in posts if p.get("secondary_language")),
        "brand_mentions": sum(1 for p in posts if "brand" in p.get("relevance_concepts", [])),
    }


def pick_candidates(posts):
    cands = [p for p in posts if p.get("relevance", 0) >= MIN_RELEVANCE
             or p.get("in_marketing_block") or p.get("secondary_language")]
    cands.sort(key=lambda p: (p.get("relevance", 0), p.get("num_comments") or 0, p.get("ups") or 0), reverse=True)
    lang = [p for p in cands if p.get("secondary_language")][:10]
    rest = [p for p in cands if not p.get("secondary_language")]
    out = rest[:MAX_CANDIDATES - len(lang)] + lang
    for i, p in enumerate(out, 1):
        p["_id"] = i
    return out


# --------------------------------------------------------------------------
# 3. Claude
# --------------------------------------------------------------------------

def system_prompt():
    lang_line = ""
    if LANG.get("enabled"):
        lang_line = ',\n "language_block": [\n   {"title": "...", "why": "1 sentence", "posts": [numbers]}\n ]'
    return """You are a marketing analyst at {name}, {description}. Target audiences: {audiences}.

You receive numbered Reddit posts from one week. Your job: extract what is useful for {name}'s marketing and for content on {website}.

Rules:
- Refer ONLY to posts from the list, by their number. Never invent anything.
- Write in {language}. Short and concrete. No filler, no introduction.
- "why" means: what {name} can do with it (messaging, argument, page, article), not a retelling.
- Prefer 3 strong points over 5 weak ones. Empty lists are allowed.
- Content ideas must be concrete: working title + format (blog post / FAQ entry / comparison page / feature page / help article).

Answer with JSON only, exactly in this shape:
{{
 "overview": "2-3 sentences: what kept the community busy this week?",
 "marketing": [
   {{"title": "short headline", "why": "1-2 sentences", "posts": [numbers]}}
 ],
 "competitors": [
   {{"vendor": "name", "observation": "1 sentence: praise / criticism / reason for switching", "posts": [numbers]}}
 ],
 "content_ideas": [
   {{"working_title": "...", "format": "...", "why": "1 sentence referencing the demand", "posts": [numbers]}}
 ]{lang}
}}
At most {n} entries per list.""".format(
        name=BRAND.get("name", "the company"), description=BRAND.get("description", ""),
        audiences=BRAND.get("audiences", ""), website=BRAND.get("website", "the website"),
        language=OUTPUT_LANGUAGE, lang=lang_line, n=MAX_ITEMS_PER_SECTION)


def user_prompt(cands, st, days):
    lines = ["Period: {} to {} ({} posts in total, {} relevant enough for this list, {} in {}).".format(
        days[0], days[-1], st["post_count"], len(cands), st["language_count"],
        LANG.get("label", "secondary language")), ""]
    for p in cands:
        text = re.sub(r"\s+", " ", (p.get("text") or "")).strip()[:CAND_TEXT_MAX]
        lines.append("[{}] r/{} | {}{}".format(p["_id"], p.get("sub", "?"), p.get("title", "").strip(),
                                              " | " + LANG.get("label", "secondary") if p.get("secondary_language") else ""))
        lines.append("    concepts: {} | comments: {} | upvotes: {}".format(
            ", ".join(p.get("relevance_concepts", [])) or "-", p.get("num_comments", 0), p.get("ups", 0)))
        if text:
            lines.append("    " + text)
        lines.append("")
    return "\n".join(lines)


def ask_claude(cands, st, days):
    if not API_KEY:
        print("[WARN] ANTHROPIC_API_KEY not set - using rule-based summary.", flush=True)
        return None
    headers = {"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    if WORKSPACE_ID:
        headers["anthropic-workspace-id"] = WORKSPACE_ID
    body = {"model": MODEL, "max_tokens": 3000, "system": system_prompt(),
            "messages": [{"role": "user", "content": user_prompt(cands, st, days)}]}
    try:
        r = requests.post(API_URL, json=body, headers=headers, timeout=120)
        if r.status_code != 200:
            print("[WARN] Claude API {}: {}".format(r.status_code, r.text[:300]), flush=True)
            return None
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0) if m else text)
    except Exception as e:
        print("[WARN] Claude response unusable: {}".format(e), flush=True)
        return None

    by_id = {p["_id"]: p for p in cands}
    for section in ("marketing", "competitors", "content_ideas", "language_block"):
        cleaned = []
        for it in (data.get(section) or [])[:MAX_ITEMS_PER_SECTION]:
            it["refs"] = [by_id[n] for n in (it.get("posts") or []) if isinstance(n, int) and n in by_id][:3]
            cleaned.append(it)
        data[section] = cleaned
    data["source"] = "claude:{}".format(MODEL)
    return data


# --------------------------------------------------------------------------
# 4. Rule-based fallback
# --------------------------------------------------------------------------

def rule_based(cands, st):
    by_group = defaultdict(list)
    for p in cands:
        for g in p.get("relevance_concepts", []):
            by_group[g].append(p)
    marketing = [{"title": "{} ({} posts)".format(g, len(ps)), "why": "Most discussed posts in this concept.",
                  "refs": ps[:3]} for g, ps in sorted(by_group.items(), key=lambda kv: -len(kv[1]))]
    lang = [p for p in cands if p.get("secondary_language")][:MAX_ITEMS_PER_SECTION]
    return {
        "overview": "Rule-based summary (no API key or API error): posts per relevance concept, no interpretation.",
        "marketing": marketing[:MAX_ITEMS_PER_SECTION],
        "competitors": [], "content_ideas": [],
        "language_block": [{"title": p.get("title", ""), "why": "", "refs": [p]} for p in lang],
        "source": "rules",
    }


# --------------------------------------------------------------------------
# 5. Slack
# --------------------------------------------------------------------------

def _clip(s, n=SLACK_TEXT_MAX):
    return s if len(s) <= n else s[:n - 1] + "…"


def _links(refs):
    return " ".join("<{}|[{}]>".format(p["url"], i + 1) for i, p in enumerate(refs))


def _section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": _clip(text)}}


def build_blocks(summary, st, days, fetch_issues):
    week = datetime.now(timezone.utc).strftime("week %V")
    blocks = [{"type": "header", "text": {"type": "plain_text",
               "text": "{} ({})".format(WEEKLY.get("title", "Reddit Weekly Summary"), week)[:150]}}]

    subs = ", ".join("r/{} {}".format(s, n) for s, n in sorted(st["by_sub"].items(), key=lambda x: -x[1]))
    groups = ", ".join("{} {}".format(g, n) for g, n in sorted(st["by_group"].items(), key=lambda x: -x[1])[:6])
    glance = ["*{} - {}* | {} posts | brand mentioned: {}".format(days[0], days[-1], st["post_count"], st["brand_mentions"])]
    if LANG.get("enabled"):
        glance[0] += " | {}: {}".format(LANG.get("label", "secondary language"), st["language_count"])
    glance.append("Sources: " + subs)
    if groups:
        glance.append("Concepts: " + groups)
    if fetch_issues:
        glance.append(":warning: Blocked: " + ", ".join("r/{} on {} day(s)".format(s, n) for s, n in fetch_issues.most_common()))
    blocks.append(_section("\n".join(glance)))
    if summary.get("overview"):
        blocks.append(_section(summary["overview"]))

    def add_list(title, items, fmt):
        if not items:
            return
        blocks.append({"type": "divider"})
        blocks.append(_section("\n".join([title] + [fmt(it) for it in items])))

    add_list(":dart: *Marketing takeaways*", summary.get("marketing"),
             lambda it: "• *{}* - {} {}".format(it.get("title", ""), it.get("why", ""), _links(it.get("refs", []))))
    add_list(":crossed_swords: *Competitors*", summary.get("competitors"),
             lambda it: "• *{}*: {} {}".format(it.get("vendor", ""), it.get("observation", ""), _links(it.get("refs", []))))
    add_list(":memo: *Content ideas*", summary.get("content_ideas"),
             lambda it: "• *{}* _({})_ - {} {}".format(it.get("working_title", ""), it.get("format", ""), it.get("why", ""), _links(it.get("refs", []))))
    if LANG.get("enabled"):
        add_list("{} *{}*".format(LANG.get("emoji", ":globe_with_meridians:"), LANG.get("label", "Secondary language")),
                 summary.get("language_block"),
                 lambda it: "• *{}* {} {}".format(it.get("title", ""), ("- " + it["why"]) if it.get("why") else "", _links(it.get("refs", []))))

    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
        "Source: daily logs in data/ | summary: {} | depth: titles + post bodies, no comments".format(summary.get("source", "?"))}]})
    return blocks[:50]


def send(blocks):
    if not SLACK_WEBHOOK:
        print("[WARN] SLACK_WEBHOOK_URL not set - printing to stdout", flush=True)
        print(json.dumps({"blocks": blocks}, ensure_ascii=False, indent=1))
        return
    r = requests.post(SLACK_WEBHOOK, json={"blocks": blocks}, timeout=15)
    print("[{}] Slack {}: {}".format("OK" if r.status_code == 200 else "ERR", r.status_code, r.text[:200]), flush=True)
    if r.status_code != 200:
        sys.exit(1)


# --------------------------------------------------------------------------
# 6. Archive
# --------------------------------------------------------------------------

def write_archive(summary, st, days, cands):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%G-W%V")
    path = os.path.join(ARCHIVE_DIR, "{}.json".format(stamp))

    def slim(it):
        out = {k: v for k, v in it.items() if k not in ("refs", "posts")}
        out["posts"] = [{"title": p.get("title"), "url": p.get("url"), "sub": p.get("sub")} for p in it.get("refs", [])]
        return out

    payload = {"week": stamp, "generated_utc": datetime.now(timezone.utc).isoformat(), "days": days,
               "stats": st, "source": summary.get("source"), "overview": summary.get("overview")}
    for section in ("marketing", "competitors", "content_ideas", "language_block"):
        payload[section] = [slim(i) for i in summary.get(section, [])]
    payload["candidates"] = [{"id": p["_id"], "title": p.get("title"), "url": p.get("url"), "sub": p.get("sub"),
                              "relevance": p.get("relevance"), "concepts": p.get("relevance_concepts")} for p in cands]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("[OK] Archive written: {}".format(path), flush=True)


def main():
    posts, days, fetch_issues = load_week()
    if not posts:
        print("[WARN] No daily logs in the period - nothing to do.", flush=True)
        return
    st = stats(posts)
    cands = pick_candidates(posts)
    print("[OK] {} candidate posts for the summary".format(len(cands)), flush=True)
    summary = ask_claude(cands, st, days) or rule_based(cands, st)
    send(build_blocks(summary, st, days, fetch_issues))
    write_archive(summary, st, days, cands)


if __name__ == "__main__":
    main()
