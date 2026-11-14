#!/usr/bin/env python3
"""Rebuild news-data.js for the headline ticker. Standard library only.

  python3 scripts/refresh_news.py

Headlines come from the public RSS feeds of Bloomberg, The Wall Street Journal and the Financial Times.
Only the headline, link, outlet and publication time are kept.
"""
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import urllib.request
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "news-data.js")
PER_CATEGORY = 6
MAX_AGE_HOURS = 30

# category, outlet, feed
FEEDS = [
    ("Markets", "Bloomberg", "https://feeds.bloomberg.com/markets/news.rss"),
    ("Markets", "WSJ", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
    ("Markets", "Financial Times", "https://www.ft.com/markets?format=rss"),
    ("Economy", "Bloomberg", "https://feeds.bloomberg.com/economics/news.rss"),
    ("Economy", "Financial Times", "https://www.ft.com/global-economy?format=rss"),
    ("Politics", "Bloomberg", "https://feeds.bloomberg.com/politics/news.rss"),
    ("Politics", "Financial Times", "https://www.ft.com/us-politics-policy?format=rss"),
    ("Technology", "Bloomberg", "https://feeds.bloomberg.com/technology/news.rss"),
    ("Technology", "WSJ", "https://feeds.content.dowjones.io/public/rss/RSSWSJD"),
    ("Technology", "Financial Times", "https://www.ft.com/technology?format=rss"),
    ("AI", "Financial Times", "https://www.ft.com/artificial-intelligence?format=rss"),
]
ALLOWED_HOSTS = ("bloomberg.com", "wsj.com", "ft.com")
AI_WORDS = re.compile(r"\b(A\.?I\.?|artificial intelligence|OpenAI|Anthropic|ChatGPT|Claude|Gemini|Copilot|chatbots?|"
                      r"Nvidia|data cent(?:er|re)s?|large language models?|LLMs?|xAI|DeepSeek|Mistral)\b")
# Sponsored guides, newsletters, transcripts and lifestyle columns are not news.
SKIP = re.compile(r"(^\d+ of the Best|Best .* Companies|You May Have Missed|^Transcript|Our Reporter|^Chart of the Week|"
                  r"^Europe Express|^FirstFT|^By The Way|side hustle|^How I |^My |^Opinion|Newsletter|Podcast|Quiz|Crossword)", re.I)
SKIP_PATHS = ("/buyside/", "/lifestyle/", "/style/", "/arts-culture/", "/personal-finance/", "/video/")
ORDER = ["Markets", "Economy", "Politics", "Technology", "AI"]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def field(item, tag):
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item, re.S)
    if not m:
        return ""
    text = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", m.group(1).strip(), flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def main():
    now = dt.datetime.now(dt.timezone.utc)
    seen, pool, failed = set(), {c: [] for c in ORDER}, []
    for cat, outlet, url in FEEDS:
        try:
            xml = fetch(url)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{outlet} {cat}: {e}")
            continue
        for item in re.findall(r"<item[ >].*?</item>", xml, re.S):
            title, link, pub = field(item, "title"), field(item, "link"), field(item, "pubDate")
            host = (urlparse(link).hostname or "").lower()
            if not title or len(title) > 130 or len(title) < 25 or urlparse(link).scheme != "https":
                continue
            if SKIP.search(title) or any(x in link for x in SKIP_PATHS):
                continue
            if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
                continue
            try:
                when = email.utils.parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            age = (now - when).total_seconds() / 3600
            key = re.sub(r"\W+", "", title.lower())
            if age < -1 or age > MAX_AGE_HOURS or key in seen:
                continue
            seen.add(key)
            c = "AI" if cat == "Technology" and AI_WORDS.search(title) else cat
            pool[c].append({"c": c, "t": title, "u": link.split("?")[0], "s": outlet, "d": when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})

    items = []
    for c in ORDER:
        # Newest first, then take turns between outlets so one feed cannot fill a category.
        by_outlet = {}
        for it in sorted(pool[c], key=lambda x: x["d"], reverse=True):
            by_outlet.setdefault(it["s"], []).append(it)
        picked = []
        while len(picked) < PER_CATEGORY and any(by_outlet.values()):
            for o in sorted(by_outlet, key=lambda o: by_outlet[o][0]["d"] if by_outlet[o] else "", reverse=True):
                if by_outlet[o] and len(picked) < PER_CATEGORY:
                    picked.append(by_outlet[o].pop(0))
        items += sorted(picked, key=lambda x: x["d"], reverse=True)

    for f in failed:
        print("  feed failed:", f)
    if len(items) < 8:
        sys.exit(f"Only {len(items)} headlines found; news-data.js left unchanged.")
    data = {"built": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "items": items}
    with open(OUT, "w") as f:
        f.write("const NEWS = " + json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/") + ";\n")
    counts = {c: sum(1 for i in items if i["c"] == c) for c in ORDER}
    print(f"wrote {OUT}: {len(items)} headlines {counts}")


if __name__ == "__main__":
    main()
