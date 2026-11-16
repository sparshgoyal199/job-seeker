#!/usr/bin/env python3
"""
Fetch the detail page for every is_match=1 listing, parse it, store in `details`.

    python fetch_details.py             # fetch only what's missing (resumable)
    python fetch_details.py --limit 50  # test on 50 first
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.7"),
    "accept-language": "en-US,en;q=0.9",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/155.0.0.0 Safari/537.36"),
}
COOKIES = {"growme_version": '{"name":"f69b217","version":"f69b217"}'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS details (
    post_id             INTEGER PRIMARY KEY,
    url                 TEXT NOT NULL,
    title               TEXT,
    category            TEXT,
    published_on        TEXT,
    description_text    TEXT,
    description_html    TEXT,
    apply_url           TEXT,
    structured_fields   TEXT,   -- JSON: {"Company": "...", "Experience Required": "..."}
    fetched_at          TEXT,
    fetch_status        TEXT,   -- 'ok' | 'http_404' | 'error'
    -- filled in by judge_llm.py:
    verdict             TEXT,   -- 'yes' | 'maybe' | 'no' | NULL
    verdict_reason      TEXT,
    matched_skills      TEXT,   -- JSON array
    concerns            TEXT,   -- JSON array
    experience_required TEXT,   -- as stated on page
    location            TEXT,
    judged_at           TEXT,
    judge_model         TEXT
);
CREATE INDEX IF NOT EXISTS idx_details_verdict ON details(verdict);
"""


def connect(path: Path):
    c = sqlite3.connect(str(path))
    c.executescript(SCHEMA)
    c.commit()
    return c


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pick_apply_url(desc_soup: BeautifulSoup, post_url: str) -> str | None:
    """Best-effort: extract the external 'apply' link from the description."""
    # 1) look inside the "How to Apply" section
    for h in desc_soup.find_all(["h2", "h3"]):
        text = h.get_text(" ", strip=True).lower()
        if any(k in text for k in ("how to apply", "apply here", "application link", "apply link")):
            for sib in h.next_siblings:
                if getattr(sib, "name", None) == "hr":
                    break
                if getattr(sib, "name", None):
                    for a in sib.find_all("a", href=True):
                        href = a["href"].strip()
                        if href.startswith(("http://", "https://", "mailto:")):
                            return href

    # 2) fallback: LinkedIn job links or mailto anywhere in description
    BAD = ("whatsapp.com/channel", "lnkd.in", "jobviareferral.com",
           "secure.gravatar.com", "instagram.com", "facebook.com/sharer")
    for a in desc_soup.find_all("a", href=True):
        href = a["href"].strip()
        if any(b in href for b in BAD):
            continue
        if "linkedin.com/jobs" in href or href.startswith("mailto:"):
            return href
    return None


def pick_structured_fields(desc_soup: BeautifulSoup) -> dict:
    """Extract <li><strong>🏢 Company:</strong> Concentrix</li>-style pairs."""
    out = {}
    for li in desc_soup.find_all("li"):
        strong = li.find("strong")
        if not strong:
            continue
        label = strong.get_text(" ", strip=True)
        # strip leading emoji
        label_clean = re.sub(r"^[^\w]+", "", label).strip().rstrip(":").strip()
        if not label_clean or len(label_clean) > 40:
            continue
        # skip if the strong contains the whole <li>
        if li.get_text(" ", strip=True) == label:
            continue
        full = li.get_text(" ", strip=True)
        value = full[len(label):].strip().lstrip(":").strip() if full.startswith(label) else ""
        if value:
            out[label_clean] = value
    return out


def parse_detail_page(html: str, page_url: str):
    soup = BeautifulSoup(html, "html.parser")
    article = soup.find("article")
    if not article:
        return None

    title_el = article.select_one("h1")
    title = title_el.get_text(" ", strip=True) if title_el else None

    cat_el = article.select_one(".gb-text-5d34f45a a")
    category = cat_el.get_text(strip=True) if cat_el else None

    date_el = article.select_one("p.gb-text-14127097")
    published_on = date_el.get_text(strip=True).replace("On:", "").strip() if date_el else None

    desc_el = article.select_one(".dynamic-entry-content")
    if not desc_el:
        return None

    description_html = str(desc_el)
    # text with paragraph breaks preserved
    paragraphs = []
    for el in desc_el.find_all(["p", "li", "h1", "h2", "h3"]):
        t = el.get_text(" ", strip=True)
        if t:
            paragraphs.append(t)
    description_text = "\n".join(paragraphs)
    # de-duplicate (bs4 gives us both <li> and its parents sometimes)
    seen = set()
    uniq = []
    for line in description_text.split("\n"):
        if line and line not in seen:
            seen.add(line); uniq.append(line)
    description_text = "\n".join(uniq)

    desc_soup = BeautifulSoup(description_html, "html.parser")
    apply_url = pick_apply_url(desc_soup, page_url)
    structured = pick_structured_fields(desc_soup)

    return {
        "title": title,
        "category": category,
        "published_on": published_on,
        "description_text": description_text,
        "description_html": description_html,
        "apply_url": apply_url,
        "structured_fields": json.dumps(structured, ensure_ascii=False),
    }


def fetch_one(session, url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=30, allow_redirects=True)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
            print(f"  HTTP {r.status_code}, retry {attempt}/{retries}")
        except requests.RequestException as e:
            print(f"  {e}, retry {attempt}/{retries}")
        time.sleep(2 * attempt)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="jobviareferral.db")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = connect(Path(args.db))
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.update(COOKIES)

    already = {r[0] for r in conn.execute("SELECT post_id FROM details WHERE fetch_status='ok'")}
    rows = conn.execute(
        "SELECT post_id, url, title FROM listings WHERE is_match=1 "
        "ORDER BY post_id"
    ).fetchall()

    todo = [r for r in rows if r[0] not in already]
    if args.limit:
        todo = todo[: args.limit]
    print(f"Total is_match=1: {len(rows)}   already fetched: {len(already)}   todo: {len(todo)}")

    for i, (post_id, url, title) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {post_id}  {title[:90]}")
        html = fetch_one(session, url)
        if html is None:
            conn.execute(
                "INSERT INTO details (post_id,url,title,fetched_at,fetch_status) "
                "VALUES (?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET "
                "fetch_status=excluded.fetch_status, fetched_at=excluded.fetched_at",
                (post_id, url, title, now_iso(), "http_404"),
            )
            conn.commit()
            time.sleep(args.delay)
            continue

        try:
            parsed = parse_detail_page(html, url)
        except Exception as e:
            print(f"  parse error: {e}")
            parsed = None
        if not parsed:
            conn.execute(
                "INSERT INTO details (post_id,url,title,fetched_at,fetch_status) "
                "VALUES (?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET "
                "fetch_status=excluded.fetch_status, fetched_at=excluded.fetched_at",
                (post_id, url, title, now_iso(), "error"),
            )
            conn.commit()
            time.sleep(args.delay)
            continue

        conn.execute(
            """INSERT INTO details (
                    post_id, url, title, category, published_on,
                    description_text, description_html, apply_url, structured_fields,
                    fetched_at, fetch_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(post_id) DO UPDATE SET
                    title=excluded.title,
                    category=excluded.category,
                    published_on=excluded.published_on,
                    description_text=excluded.description_text,
                    description_html=excluded.description_html,
                    apply_url=excluded.apply_url,
                    structured_fields=excluded.structured_fields,
                    fetched_at=excluded.fetched_at,
                    fetch_status=excluded.fetch_status""",
            (post_id, url, parsed["title"], parsed["category"], parsed["published_on"],
             parsed["description_text"], parsed["description_html"],
             parsed["apply_url"], parsed["structured_fields"],
             now_iso(), "ok"),
        )
        conn.commit()
        apply_flag = " (apply link ✓)" if parsed["apply_url"] else ""
        print(f"  ok{apply_flag}  {len(parsed['description_text'])} chars")
        time.sleep(args.delay)

    total_ok = conn.execute("SELECT COUNT(*) FROM details WHERE fetch_status='ok'").fetchone()[0]
    with_apply = conn.execute("SELECT COUNT(*) FROM details WHERE apply_url IS NOT NULL").fetchone()[0]
    print(f"\nDone. details_ok={total_ok}  with_apply_url={with_apply}")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())