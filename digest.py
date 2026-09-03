#!/usr/bin/env python3
"""
Reddit Community Digest - daily edition
=======================================

Collects the top posts of the day from a set of subreddits and posts a
digest to Slack. Everything brand-specific (subreddits, competitors,
keywords, labels) lives in config.json - nothing in this file needs editing.

Data path, tried in order until one works (Reddit blocks cloud IPs often):
  1. Reddit public JSON      reddit.com/r/SUB/top.json
  2. Reddit RSS/Atom feeds   reddit.com/r/SUB/top.rss
  3. old.reddit.com JSON     old.reddit.com/r/SUB/top.json

Note: the RSS path returns neither upvotes nor comment counts. On those
days the "Top N" list is Reddit's own top-of-day order, and the marketing
block relies purely on the relevance concepts from config.json.

Besides the Slack message, every run writes data/YYYY-MM-DD.json with ALL
fetched posts (not just the ones posted). That file is the input for
weekly_summary.py and for any analysis you want to run later.

Environment
-----------
  SLACK_WEBHOOK_URL   required (without it the digest prints to stdout)
  CONFIG_PATH         optional, default config.json
"""

import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

# -- configuration ------------------------------------------------------------

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")
with open(CONFIG_PATH, encoding="utf-8") as _f:
    CFG = json.load(_f)

BRAND   = CFG["brand"]
SRC     = CFG["sources"]
LANG    = CFG.get("language_block", {"enabled": False})
DIGEST  = CFG["digest"]

MAIN_SUBS      = SRC.get("subreddits", [])
SECONDARY_SUBS = SRC.get("secondary_subreddits", [])
SOURCES = ([(s, SRC.get("timeframe", "day")) for s in MAIN_SUBS]
           + [(s, SRC.get("secondary_timeframe", "week")) for s in SECONDARY_SUBS])

TOP_N            = DIGEST.get("top_n", 10)
MKT_TOP_N        = DIGEST.get("marketing_top_n", 5)
LANG_TOP_N       = DIGEST.get("language_top_n", 5)
MKT_MIN_SCORE    = DIGEST.get("marketing_min_score", 8)
MKT_MIN_CONCEPTS = DIGEST.get("marketing_min_concepts", 2)
REQUEST_PAUSE    = float(DIGEST.get("request_pause_seconds", 2.0))
RETRY_PAUSE      = 5.0

KEYWORDS = [k.lower() for k in CFG.get("keywords", [])]

# Relevance concepts: each concept counts at most once per post, regardless
# of how many of its spellings occur. Matched with word boundaries so that
# e.g. "ads" does not match "roads".
CONCEPTS = [
    (c["name"], c["weight"], re.compile(r"\b(?:" + "|".join(c["patterns"]) + r")\b", re.I))
    for c in CFG.get("relevance_concepts", [])
]

TOPIC_CATEGORIES = [(t["name"], t["fragments"]) for t in CFG.get("topic_categories", [])]
DEFAULT_TOPIC    = CFG.get("default_topic", "General")

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

LOG_DIR      = "data"
LOG_TEXT_MAX = 1500

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

# subreddit -> number of posts delivered, or the HTTP error code.
# Shown in the Slack footer so that a blocked source never disappears silently.
FETCH_LOG = {}


# -- helpers ------------------------------------------------------------------

def clean_text(raw):
    """RSS delivers the post body as an HTML fragment incl. a 'submitted by'
    footer. Turn it into plain text."""
    if not raw:
        return ""
    t = raw
    if "<!-- SC_OFF -->" in t and "<!-- SC_ON -->" in t:
        t = t.split("<!-- SC_OFF -->", 1)[1].split("<!-- SC_ON -->", 1)[0]
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
    t = re.sub(r"(?i)</(p|div|li|br|h[1-6])\s*>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"(?is)\bsubmitted by\b.*$", "", t)
    t = re.sub(r"[ \t ]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def keyword_score(text):
    t = text.lower()
    return sum(1 for kw in KEYWORDS if kw in t)


def relevance(post):
    """(score, [concept names]) - how close a post is to what the brand sells.
    Does NOT influence score_post(); it only feeds the marketing block and
    the daily log."""
    t = "{} {}".format(post.get("title", ""), post.get("text", ""))
    score, names = 0, []
    for name, weight, rx in CONCEPTS:
        if rx.search(t):
            score += weight
            names.append(name)
    return score, names


def is_secondary_language(post):
    """True if the post is from a secondary-language subreddit or contains
    enough of the configured language markers."""
    if not LANG.get("enabled"):
        return False
    if post.get("sub") in SECONDARY_SUBS:
        return True
    text = " {} {} ".format(post.get("title", ""), post.get("text", "")).lower()
    hits = sum(1 for m in LANG.get("markers", []) if m in text)
    return hits >= LANG.get("min_marker_hits", 3)


def score_post(p):
    ks = keyword_score(p.get("title", "") + " " + p.get("text", ""))
    return p.get("ups", 0) * 1.0 + p.get("comments", 0) * 2.0 + ks * 50.0


def dedup(posts):
    seen, out = set(), []
    for p in posts:
        key = p["title"].lower()[:80]
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def categorize_post(post):
    text = (post.get("title", "") + " " + post.get("text", "")).lower()
    for category, fragments in TOPIC_CATEGORIES:
        if any(frag in text for frag in fragments):
            return category
    return DEFAULT_TOPIC


def build_at_a_glance(posts):
    cats = {}
    for p in posts:
        cats.setdefault(categorize_post(p), []).append(p)
    lines = []
    for cat, cat_posts in sorted(cats.items(), key=lambda kv: len(kv[1]), reverse=True):
        best = max(cat_posts, key=score_post)
        title = best["title"] if len(best["title"]) <= 72 else best["title"][:69] + "..."
        lines.append(":small_blue_diamond: *{}* ({} post{})  --  \"{}\"".format(
            cat, len(cat_posts), "s" if len(cat_posts) != 1 else "", title))
    return "\n".join(lines)


# -- fetching -----------------------------------------------------------------

def _normalise_json(sub, d):
    return {
        "sub": sub, "id": d.get("id", ""), "title": d.get("title", ""),
        "url": "https://www.reddit.com" + d.get("permalink", ""),
        "ups": d.get("ups", 0), "comments": d.get("num_comments", 0),
        "text": d.get("selftext", ""), "author": d.get("author", ""),
        "created": d.get("created_utc", 0),
    }


def fetch_json(base="https://www.reddit.com"):
    posts, blocked = [], 0
    for sub, tf in SOURCES:
        url = "{}/r/{}/top.json?t={}&limit=100".format(base, sub, tf)
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            print("  [JSON] r/{} (t={}) -> HTTP {}".format(sub, tf, r.status_code), flush=True)
            if r.status_code in (403, 429):
                blocked += 1
                FETCH_LOG[sub] = str(r.status_code)
                time.sleep(RETRY_PAUSE)
                continue
            if r.status_code != 200:
                FETCH_LOG[sub] = str(r.status_code)
                continue
            found = 0
            for item in r.json().get("data", {}).get("children", []):
                posts.append(_normalise_json(sub, item["data"]))
                found += 1
            FETCH_LOG[sub] = str(found)
            time.sleep(REQUEST_PAUSE)
        except Exception as e:
            print("  [JSON] r/{} error: {}".format(sub, e), flush=True)
            FETCH_LOG[sub] = "err"
    if blocked == len(SOURCES):
        print("[WARN] Reddit JSON: every subreddit returned 403/429", flush=True)
        return None
    return posts


def fetch_rss():
    posts, blocked = [], 0
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    hdrs = dict(HEADERS, Accept="application/rss+xml, text/xml, */*")
    for sub, tf in SOURCES:
        url = "https://www.reddit.com/r/{}/top.rss?t={}&limit=100".format(sub, tf)
        try:
            r = requests.get(url, headers=hdrs, timeout=20)
            print("  [RSS] r/{} (t={}) -> HTTP {}".format(sub, tf, r.status_code), flush=True)
            if r.status_code in (403, 429):
                blocked += 1
                FETCH_LOG[sub] = str(r.status_code)
                time.sleep(RETRY_PAUSE)
                continue
            if r.status_code != 200:
                FETCH_LOG[sub] = str(r.status_code)
                continue
            found = 0
            for entry in ET.fromstring(r.text).findall("atom:entry", ns):
                title_el, link_el, content_el = (entry.find("atom:" + k, ns) for k in ("title", "link", "content"))
                posts.append({
                    "sub": sub,
                    "title": title_el.text if title_el is not None else "",
                    "url": link_el.attrib.get("href", "") if link_el is not None else "",
                    "ups": 0, "comments": 0,
                    "text": clean_text(content_el.text if content_el is not None else "")[:LOG_TEXT_MAX],
                })
                found += 1
            FETCH_LOG[sub] = str(found)
            time.sleep(REQUEST_PAUSE)
        except Exception as e:
            print("  [RSS] r/{} error: {}".format(sub, e), flush=True)
            FETCH_LOG[sub] = "err"
    if blocked == len(SOURCES):
        print("[WARN] Reddit RSS: every subreddit returned 403/429", flush=True)
        return None
    return posts


# -- daily log ----------------------------------------------------------------

def write_log(all_posts, top, lang_top, mkt_top, method, ranking):
    """Write data/YYYY-MM-DD.json with ALL fetched posts."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(LOG_DIR, "{}.json".format(stamp))
        in_top, in_lang, in_mkt = ({p["url"] for p in lst} for lst in (top, lang_top, mkt_top))
        records = []
        for p in all_posts:
            rel = relevance(p)
            records.append({
                "sub": p.get("sub", ""), "title": p.get("title", ""), "url": p.get("url", ""),
                "ups": p.get("ups", 0), "num_comments": p.get("comments", 0),
                "score": round(score_post(p), 1),
                "relevance": rel[0], "relevance_concepts": rel[1],
                "topic": categorize_post(p),
                "secondary_language": is_secondary_language(p),
                "in_digest": p.get("url") in in_top,
                "in_language_block": p.get("url") in in_lang,
                "in_marketing_block": p.get("url") in in_mkt,
                "text": (p.get("text") or "")[:LOG_TEXT_MAX],
            })
        payload = {
            "date": stamp,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "method": method, "ranking": ranking,
            "sources": [{"sub": s, "timeframe": t} for s, t in SOURCES],
            "fetch_log": dict(FETCH_LOG),
            "post_count": len(records),
            "digest_count": len(top),
            "secondary_language_count": sum(1 for r in records if r["secondary_language"]),
            "posts": records,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print("[OK] Daily log written: {} ({} posts)".format(path, len(records)), flush=True)
        return path
    except Exception as e:
        print("[WARN] Could not write daily log: {}".format(e), flush=True)
        return None


# -- Slack --------------------------------------------------------------------

def slack_error(msg):
    text = ":warning: *{} failed*\n{}".format(DIGEST.get("title", "Reddit Digest"), msg)
    if not SLACK_WEBHOOK:
        print("[ERROR] {}".format(msg), flush=True)
        return
    requests.post(SLACK_WEBHOOK, json={"text": text, "blocks": [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}}]}, timeout=15)


def _post_block(i, p, extra=""):
    metrics = ""
    if p.get("ups") or p.get("comments"):
        metrics = "  {:,} up  {:,} comments".format(p["ups"], p["comments"])
    return {"type": "section", "text": {"type": "mrkdwn", "text":
        "*{}. <{}|{}>*\n`r/{}`{}{}".format(i, p["url"], p["title"], p["sub"], metrics, extra)}}


def send_to_slack(top, lang_top, mkt_top, method, log_path):
    if not SLACK_WEBHOOK:
        print("[WARN] SLACK_WEBHOOK_URL not set - printing to stdout", flush=True)
        for i, p in enumerate(top, 1):
            print("{}. [{}up {}c] {} -> {}".format(i, p["ups"], p["comments"], p["title"], p["url"]))
        print("-- marketing --")
        for i, p in enumerate(mkt_top, 1):
            print("{}. [{} pts] r/{} {} -> {}".format(i, relevance(p)[0], p["sub"], p["title"], p["url"]))
        print("-- {} --".format(LANG.get("label", "secondary language")))
        for i, p in enumerate(lang_top, 1):
            print("{}. r/{} {} -> {}".format(i, p["sub"], p["title"], p["url"]))
        print("-- sources: {}".format(", ".join("r/{}={}".format(k, v) for k, v in FETCH_LOG.items())))
        return

    today = datetime.now(timezone.utc).strftime("%A, %B %-d %Y")
    method_label = {"json": "Reddit JSON API", "rss": "Reddit RSS feed",
                    "old_json": "old.reddit.com JSON"}.get(method, method)
    title = DIGEST.get("title", "Reddit Digest")

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "emoji": True,
                                    "text": ":microphone: {} - {}".format(title, today)}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "Top {} posts from {}  |  via {}".format(
            len(top), ", ".join("r/" + s for s, _ in SOURCES), method_label)}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": "*:mag: At a glance -- what's being discussed today:*\n{}".format(build_at_a_glance(top))}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Top {} posts:*".format(len(top))}},
    ]
    for i, p in enumerate(top, 1):
        blocks.append(_post_block(i, p))

    # Marketing block: posts close to what the brand sells. Deliberately does
    # NOT exclude posts already in the Top N - the point is to flag them.
    if mkt_top:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "*:dart: Relevant for marketing ({}):*".format(len(mkt_top))}})
        for i, p in enumerate(mkt_top, 1):
            blocks.append(_post_block(i, p, "  _{}_".format(" · ".join(relevance(p)[1][:4]))))

    # Secondary-language block: own quota so small communities are not
    # drowned out by the big English subreddits. Excludes the Top N.
    if LANG.get("enabled"):
        blocks.append({"type": "divider"})
        label, emoji = LANG.get("label", "Secondary language"), LANG.get("emoji", ":globe_with_meridians:")
        if lang_top:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": "*{} {} ({} posts):*".format(emoji, label, len(lang_top))}})
            for i, p in enumerate(lang_top, 1):
                blocks.append(_post_block(i, p))
        else:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": "*{} {}:*\n_No {} posts found today._".format(emoji, label, label)}})

    sources_txt = "  |  ".join("r/{}: {}".format(s, FETCH_LOG.get(s, "-")) for s, _ in SOURCES)
    if log_path:
        sources_txt += "  |  log: {}".format(log_path)
    blocks += [
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "Posts per source -- {}".format(sources_txt)}]},
    ]

    resp = requests.post(SLACK_WEBHOOK, json={"blocks": blocks}, timeout=15)
    print("[Slack] status={} body={}".format(resp.status_code, resp.text[:80]), flush=True)
    if not (resp.status_code == 200 and resp.text.strip() == "ok"):
        print("[ERROR] Slack returned: {}".format(resp.text), flush=True)
        sys.exit(1)
    print("[OK] Sent {} posts to Slack (source: {}).".format(len(top), method), flush=True)


# -- main ---------------------------------------------------------------------

def main():
    if not SLACK_WEBHOOK:
        print("[WARN] SLACK_WEBHOOK_URL is not set.", flush=True)
    if not SOURCES:
        print("[FAIL] No subreddits configured in {}".format(CONFIG_PATH), flush=True)
        sys.exit(1)

    posts, method = None, None
    for label, fn, m in (("Reddit JSON", lambda: fetch_json("https://www.reddit.com"), "json"),
                         ("Reddit RSS", fetch_rss, "rss"),
                         ("old.reddit.com JSON", lambda: fetch_json("https://old.reddit.com"), "old_json")):
        if posts is None:
            print("[INFO] Trying {} ...".format(label), flush=True)
            posts = fn()
            if posts is not None:
                method = m
                print("[INFO] {} returned {} raw posts".format(label, len(posts)), flush=True)

    if posts is None:
        msg = ("All Reddit endpoints (JSON, RSS, old.reddit.com) answered 403/429. "
               "Reddit is blocking the IP this job runs from. Re-run later or from "
               "a different runner; the official API requires Reddit's approval.")
        print("[FAIL] {}".format(msg), flush=True)
        slack_error(msg)
        sys.exit(1)

    posts = dedup(posts)
    # Stable sort: without upvotes/comments (RSS) every post scores 0 and
    # Reddit's own top-of-day order is kept; keyword hits still float up.
    posts.sort(key=score_post, reverse=True)
    has_metrics = any(p.get("ups") or p.get("comments") for p in posts)
    ranking = "score" if has_metrics else "feed-order (no metrics)"

    top = posts[:TOP_N]
    already = {p["url"] for p in top}
    lang_top = [p for p in posts if is_secondary_language(p) and p["url"] not in already][:LANG_TOP_N]

    def marketing_worthy(p):
        pts, names = relevance(p)
        return pts >= MKT_MIN_SCORE or len(names) >= MKT_MIN_CONCEPTS

    mkt_top = [p for p in sorted(posts, key=lambda p: relevance(p)[0], reverse=True)
               if marketing_worthy(p)][:MKT_TOP_N]
    print("[INFO] {} posts total, {} in marketing block, {} in language block".format(
        len(posts), len(mkt_top), len(lang_top)), flush=True)

    if not top and not lang_top:
        msg = "No posts found today across all configured subreddits."
        print("[WARN] {}".format(msg), flush=True)
        slack_error(msg)
        sys.exit(0)

    log_path = write_log(posts, top, lang_top, mkt_top, method, ranking)
    send_to_slack(top, lang_top, mkt_top, method, log_path)


if __name__ == "__main__":
    main()
