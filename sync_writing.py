#!/usr/bin/env python3
"""
sync_writing.py

Fetches Omar's Substack RSS feed directly and writes a static writing.json
next to this script. writing.html reads that same-origin file instead of
calling a third-party RSS-to-JSON bridge (rss2json.com) at page-load time.

Why: rss2json's free tier caches feed responses server-side for some
undocumented period (observed: over an hour), so brand-new posts didn't
show up on the site until that cache cleared on its own. Fetching the feed
ourselves on a schedule (via the paired GitHub Action, every 30 min) and
committing the parsed result as a static file removes that dependency
entirely — the page always reads whatever we last fetched, and "how fresh"
is now something we control (the Action's schedule), not a third party.

No credentials needed: the Substack RSS feed is public.

Run: python3 sync_writing.py
"""

import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET

FEED_URL = "https://omarkutbi.substack.com/feed"
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "writing.json")

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

# RFC 2822 pubDate, e.g. "Fri, 10 Jul 2026 08:00:44 GMT"
PUBDATE_RE = re.compile(
    r"^\w+,\s+(\d{1,2})\s+(\w{3})\s+(\d{4})\s+(\d{2}):(\d{2}):(\d{2})"
)
MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def fetch_feed(url):
    req = urllib.request.Request(url, headers={"User-Agent": "omarkutbi.com writing sync"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def parse_pubdate(raw):
    m = PUBDATE_RE.match(raw or "")
    if not m:
        return None, "date unknown"
    day, mon_abbr, year, hh, mm, ss = m.groups()
    month = MONTH_ABBR.get(mon_abbr)
    if not month:
        return None, "date unknown"
    sort_key = f"{year}-{month:02d}-{int(day):02d}T{hh}:{mm}:{ss}"
    label = f"{MONTHS[month - 1]} {year}"
    return sort_key, label


def parse_items(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pubdate_raw = item.findtext("pubDate") or ""
        sort_key, label = parse_pubdate(pubdate_raw)
        if not title or not link:
            continue
        items.append({
            "title": title,
            "link": link,
            "date_label": label,
            "date_sort": sort_key or "0000-00-00T00:00:00",
        })
    items.sort(key=lambda i: i["date_sort"], reverse=True)
    return items


def main():
    print(f"Fetching {FEED_URL} ...")
    xml_bytes = fetch_feed(FEED_URL)
    items = parse_items(xml_bytes)
    print(f"  parsed {len(items)} post(s)")
    for i in items:
        print(f"    {i['date_label']:>12}  {i['title']}")

    new_content = json.dumps({"posts": items}, indent=2) + "\n"

    old_content = None
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH) as f:
            old_content = f.read()

    changed = new_content != old_content
    if changed:
        with open(OUTPUT_PATH, "w") as f:
            f.write(new_content)
        print("writing.json updated.")
    else:
        print("writing.json unchanged.")

    return changed


if __name__ == "__main__":
    main()
