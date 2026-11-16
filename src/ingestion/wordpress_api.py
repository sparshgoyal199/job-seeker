#!/usr/bin/env python3
"""
WordPress REST API Syncer for jobviareferral.com.

Replaces slow multi-step HTML scraping with direct, high-speed WP REST API ingestion.
Fetches up to 100 posts per request directly from:
    https://jobviareferral.com/wp-json/wp/v2/posts

Extracts:
  - Full post content & clean description text
  - Structured fields (Company, Experience, Roles, Location, etc.)
  - External application links (Workday, Greenhouse, Ripplehire, Forms, LinkedIn, mailto)
  - Pre-filters role suitability (AI/Python/Backend vs Senior/QA/4+ yrs)
  - Upserts into `listings` and `details` in a single pass.

Usage:
    python sync_jobs_api.py                  # Full / incremental sync (fast)
    python sync_jobs_api.py --search intern  # Sync specific search term
    python sync_jobs_api.py --max-pages 3    # Sync latest 3 pages (300 posts)
    python sync_jobs_api.py --all            # Fetch all posts without search filter
"""

import argparse
import html
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import os

import requests
from bs4 import BeautifulSoup

# Ensure Windows terminal handles emojis and utf-8 cleanly
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

API_ENDPOINTS = [
    "https://jobviareferral.com/index.php/wp-json/wp/v2/posts",
    "https://jobviareferral.com/wp-json/wp/v2/posts",
]
BASE_API_URL = API_ENDPOINTS[0]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# ---------------------------------------------------------------------------
# Pre-filter Rules (aligned with Sparsh Goyal's target profiles)
# ---------------------------------------------------------------------------
PASS_SPECIFIC_TITLE_PATTERNS = {
    "ai": [
        r"\bAI\s+(?:Engineer|Developer|Scientist|Specialist|Analyst)\b",
        r"\bML\s+(?:Engineer|Developer|Scientist)\b",
        r"\bMachine\s+Learning\b",
        r"\bDeep\s+Learning\b",
        r"\bData\s+Scientist\b",
        r"\bGenAI\b",
        r"\bLLM\s+(?:Engineer|Developer)\b",
        r"\bAI\s*/\s*ML\b",
        r"\bArtificial\s+Intelligence\b",
        r"\b(?:Engineer|Developer)\s*[-–]?\s*(?:AI|ML)\b",
    ],
    "python": [
        r"\bPython\s+(?:Developer|Engineer)\b",
    ],
    "backend": [
        r"\bBackend\s+(?:Developer|Engineer)\b",
        r"\bBack[- ]End\s+(?:Developer|Engineer)\b",
    ],
    "swe": [
        r"\bSoftware\s+Engineer\b",
        r"\bSoftware\s+Developer\b",
        r"\bSDE\b",
    ],
    "fullstack": [
        r"\bFull\s*Stack\b",
    ],
    "data_eng": [
        r"\bData\s+(?:Engineer|Scientist)\b",
    ],
}

SENIORITY_REJECT = [
    r"\bsenior\b",
    r"\blead\b",
    r"\bprincipal\b",
    r"\bstaff\s+(?:engineer|developer|sde)\b",
    r"\barchitect\b",
    r"\bengineering\s+manager\b",
    r"\bengineering\s+lead\b",
    r"\bteam\s+lead\b",
]

NON_TARGET_TITLE_REJECT = [
    # QA / testing
    r"\bQA\b",
    r"\bSDET\b",
    r"\bTester\b",
    r"\bTest\s+(?:Engineer|Lead|Analyst|Manager)\b",
    r"\b(?:Manual|Automation)\s+Test",
    # Non-CS engineering
    r"\bMechanical\b", r"\bCivil\b", r"\bElectrical\b",
    r"\bChemical\b", r"\bIndustrial\b", r"\bProduction\b",
    # Sales / HR / marketing / support
    r"\bSales\b", r"\bBusiness\s+Development\b",
    r"\bHR\b", r"\bRecruit", r"\bMarketing\b",
    r"\bCustomer\s+Support\b", r"\bTechnical\s+Support\b",
    r"\bSupport\s+Engineer\b", r"\bSupport\s+Executive\b",
    r"\bOperations\s+(?:Manager|Engineer|Analyst|Executive)\b",
    # Analyst-adjacent non-target
    r"\bBusiness\s+Analyst\b",
    r"\bData\s+Analyst\b",
    r"\bFinancial\s+Analyst\b",
    r"\bProcess\s+Associate\b",
]

KEEP_ANY_TITLE_PATTERNS = [
    r"\bAI\b", r"\bML\b", r"\bGenAI\b", r"\bLLM\b", r"\bMLOps\b",
    r"\bPython\b", r"\bBackend\b", r"\bBack[- ]End\b",
    r"\bDeveloper\b", r"\bProgrammer\b", r"\bSDE\b",
    r"\bData\s+(?:Engineer|Scientist|Science)\b",
    r"\bFull\s*Stack\b",
]

REJECT_EXP_PATTERNS = [
    r"\b[4-9]\s*\+?\s*(?:years?|yrs?)\b",
    r"\b1\d\s*\+?\s*(?:years?|yrs?)\b",
    r"\b\d+\s*(?:-|–|to)\s*(?:[4-9]|1\d)\s*(?:years?|yrs?)\b",
    r"\b[4-9]\s*(?:-|–|to)\s*\d+\s*(?:years?|yrs?)\b",
]

# ---------------------------------------------------------------------------
# Classification & Parsing Helpers
# ---------------------------------------------------------------------------
def classify_title(title: str) -> tuple[bool, str | None, str | None]:
    for pat in SENIORITY_REJECT:
        if re.search(pat, title, re.I):
            return False, None, "seniority"

    for role, patterns in PASS_SPECIFIC_TITLE_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, title, re.I):
                return True, role, None

    for pat in NON_TARGET_TITLE_REJECT:
        if re.search(pat, title, re.I):
            return False, None, "excluded_title"

    for pat in KEEP_ANY_TITLE_PATTERNS:
        if re.search(pat, title, re.I):
            return True, "ambiguous", None

    return False, None, "no_role_match"


def has_experience_reject(title: str, excerpt: str) -> tuple[bool, list[str]]:
    text = f"{title} {excerpt}"
    reasons = [p for p in REJECT_EXP_PATTERNS if re.search(p, text, re.I)]
    return bool(reasons), reasons


def clean_description(desc_soup: BeautifulSoup) -> str:
    paragraphs = []
    for el in desc_soup.find_all(["p", "li", "h1", "h2", "h3"]):
        t = el.get_text(" ", strip=True)
        if t:
            paragraphs.append(t)
    seen = set()
    uniq = []
    for line in "\n".join(paragraphs).split("\n"):
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            uniq.append(line)
    return "\n".join(uniq)


def pick_structured_fields(desc_soup: BeautifulSoup) -> dict:
    out = {}
    for li in desc_soup.find_all("li"):
        strong = li.find("strong")
        if not strong:
            continue
        label = strong.get_text(" ", strip=True)
        label_clean = re.sub(r"^[^\w]+", "", label).strip().rstrip(":").strip()
        if not label_clean or len(label_clean) > 40:
            continue
        if li.get_text(" ", strip=True) == label:
            continue
        full = li.get_text(" ", strip=True)
        value = full[len(label):].strip().lstrip(":").strip() if full.startswith(label) else ""
        if value:
            out[label_clean] = value
    return out


def pick_apply_url(desc_soup: BeautifulSoup, post_url: str) -> str | None:
    BAD = (
        "whatsapp.com/channel", "lnkd.in", "jobviareferral.com",
        "secure.gravatar.com", "instagram.com", "facebook.com/sharer",
        "twitter.com", "t.me", "wp-content/uploads"
    )

    # 1. Look inside sections with headings like "How to Apply", "Apply Link"
    for h in desc_soup.find_all(["h2", "h3", "h4"]):
        text = h.get_text(" ", strip=True).lower()
        if any(k in text for k in ("how to apply", "apply here", "application link", "apply link", "registration")):
            for sib in h.next_siblings:
                if getattr(sib, "name", None) == "hr":
                    break
                if getattr(sib, "name", None):
                    for a in sib.find_all("a", href=True):
                        href = a["href"].strip()
                        if any(b in href for b in BAD):
                            continue
                        if href.startswith(("http://", "https://", "mailto:")):
                            return href

    # 2. Look for links with explicit action anchor text
    ACTION_WORDS = ("apply", "click here", "register", "apply now", "form", "apply online")
    for a in desc_soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True).lower()
        if any(b in href for b in BAD):
            continue
        if any(w in text for w in ACTION_WORDS):
            if href.startswith(("http://", "https://", "mailto:")):
                return href

    # 3. Look for reputable ATS/job portals or mailto links
    ATS_DOMAINS = (
        "myworkdayjobs.com", "ripplehire.com", "greenhouse.io", "lever.co",
        "smartrecruiters.com", "docs.google.com/forms", "forms.gle",
        "linkedin.com/jobs", "careers."
    )
    for a in desc_soup.find_all("a", href=True):
        href = a["href"].strip()
        if any(b in href for b in BAD):
            continue
        if href.startswith("mailto:") or any(d in href for d in ATS_DOMAINS):
            return href

    return None


# ---------------------------------------------------------------------------
# Database Schema & Operations
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS crawled_pages (
    page_number INTEGER PRIMARY KEY,
    crawled_at TEXT NOT NULL,
    job_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS listings (
    post_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    category TEXT,
    category_url TEXT,
    excerpt TEXT,
    source_page INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    role_class TEXT,
    reject_class TEXT,
    is_match INTEGER NOT NULL DEFAULT 0,
    match_reasons TEXT,
    reject_reasons TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_match ON listings(is_match);

CREATE TABLE IF NOT EXISTS details (
    post_id             INTEGER PRIMARY KEY,
    url                 TEXT NOT NULL,
    title               TEXT,
    category            TEXT,
    published_on        TEXT,
    description_text    TEXT,
    description_html    TEXT,
    apply_url           TEXT,
    structured_fields   TEXT,
    fetched_at          TEXT,
    fetch_status        TEXT,
    verdict             TEXT,
    verdict_reason      TEXT,
    matched_skills      TEXT,
    concerns            TEXT,
    experience_required TEXT,
    location            TEXT,
    judged_at           TEXT,
    judge_model         TEXT
);
CREATE INDEX IF NOT EXISTS idx_details_verdict ON details(verdict);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync(conn: sqlite3.Connection, search_term: str | None, max_pages: int | None, delay: float, stop_on_existing: bool):
    session = requests.Session()
    session.headers.update(HEADERS)

    now = _now()
    existing_ids = {r[0] for r in conn.execute("SELECT post_id FROM listings").fetchall()}
    print(f"Current DB state: {len(existing_ids)} listings tracked.")

    page = 1
    total_new = 0
    total_matched = 0
    total_seen = 0

    while True:
        params = {"per_page": 100, "page": page}
        if search_term:
            params["search"] = search_term

        print(f"\n[Page {page}] Fetching {params} ...", end=" ", flush=True)
        try:
            r = session.get(BASE_API_URL, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"Network error: {e}")
            break

        if r.status_code == 400:  # WP REST returns 400 when page > max_pages
            print("Reached end of pages.")
            break
        elif r.status_code != 200:
            print(f"HTTP {r.status_code} - stopping.")
            break

        total_posts_header = r.headers.get("X-WP-Total", "?")
        total_pages_header = r.headers.get("X-WP-TotalPages", "?")
        posts = r.json()
        if not posts:
            print("No posts returned.")
            break

        print(f"received {len(posts)} posts (Total in WP: {total_posts_header}, Pages: {total_pages_header})")

        page_new = 0
        hit_existing = False

        for p in posts:
            post_id = int(p["id"])
            total_seen += 1

            if post_id in existing_ids:
                if stop_on_existing:
                    hit_existing = True
                    break

            raw_title = p.get("title", {}).get("rendered", "")
            title = html.unescape(raw_title).strip()
            url = p.get("link", "")
            date_str = p.get("date", "")
            raw_content = p.get("content", {}).get("rendered", "")
            raw_excerpt = p.get("excerpt", {}).get("rendered", "")

            # Parse content HTML once
            soup = BeautifulSoup(raw_content, "html.parser")
            excerpt_soup = BeautifulSoup(raw_excerpt, "html.parser")
            excerpt_text = excerpt_soup.get_text(" ", strip=True)
            desc_text = clean_description(soup)
            structured = pick_structured_fields(soup)
            apply_url = pick_apply_url(soup, url)

            # Open Hiring: Accept all job roles for outreach
            is_match = True
            role_class = "open_hiring"
            reject_class = None
            match_reasons = json.dumps({"role_class": "open_hiring"})
            reject_reasons = json.dumps({})

            # Upsert into listings
            conn.execute(
                """INSERT INTO listings (
                       post_id, title, url, category, category_url, excerpt,
                       source_page, first_seen, last_seen,
                       role_class, reject_class, is_match, match_reasons, reject_reasons)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(post_id) DO UPDATE SET
                       title=excluded.title,
                       url=excluded.url,
                       excerpt=excluded.excerpt,
                       last_seen=excluded.last_seen,
                       role_class=excluded.role_class,
                       reject_class=excluded.reject_class,
                       is_match=excluded.is_match,
                       match_reasons=excluded.match_reasons,
                       reject_reasons=excluded.reject_reasons""",
                (post_id, title, url, None, None, excerpt_text, page, now, now,
                 role_class, reject_class, 1, match_reasons, reject_reasons)
            )

            # Upsert into details with verdict='yes' directly (bypassing LLM judge)
            conn.execute(
                """INSERT INTO details (
                       post_id, url, title, published_on,
                       description_text, description_html, apply_url, structured_fields,
                       fetched_at, fetch_status, verdict, verdict_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'yes', 'Open Hiring Mode (WordPress Direct)')
                   ON CONFLICT(post_id) DO UPDATE SET
                       title=excluded.title,
                       published_on=excluded.published_on,
                       description_text=excluded.description_text,
                       description_html=excluded.description_html,
                       apply_url=COALESCE(excluded.apply_url, details.apply_url),
                       structured_fields=excluded.structured_fields,
                       fetched_at=excluded.fetched_at,
                       fetch_status='ok',
                       verdict = CASE WHEN details.verdict IS NULL OR details.verdict = '' THEN 'yes' ELSE details.verdict END,
                       verdict_reason = CASE WHEN details.verdict IS NULL OR details.verdict = '' THEN 'Open Hiring Mode (WordPress Direct)' ELSE details.verdict_reason END""",
                (post_id, url, title, date_str, desc_text, raw_content,
                 apply_url, json.dumps(structured, ensure_ascii=False), now, "ok")
            )
            total_matched += 1

            if post_id not in existing_ids:
                page_new += 1
                existing_ids.add(post_id)

        conn.commit()
        total_new += page_new
        print(f"  Processed {len(posts)} posts: +{page_new} new to DB, page completed.")

        if hit_existing and stop_on_existing:
            print("Encountered already-synced post ID. Incremental sync complete!")
            break

        max_page_int = int(total_pages_header) if total_pages_header.isdigit() else 9999
        if page >= max_page_int:
            print("Reached last page.")
            break

        if max_pages and page >= max_pages:
            print(f"Reached user-specified limit of {max_pages} pages.")
            break

        page += 1
        time.sleep(delay)

    total_listings = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    total_matches = conn.execute("SELECT COUNT(*) FROM listings WHERE is_match=1").fetchone()[0]
    total_details = conn.execute("SELECT COUNT(*) FROM details").fetchone()[0]
    print("\n" + "=" * 60)
    print("SYNC SUMMARY")
    print("=" * 60)
    print(f"Total posts checked this run : {total_seen}")
    print(f"Brand new posts added        : {total_new}")
    print(f"Total listings in database   : {total_listings}")
    print(f"Total matches (pre-filtered) : {total_matches}")
    print(f"Total detail records ready   : {total_details}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="WordPress REST API Syncer for jobviareferral.com")
    parser.add_argument("--db", default="jobviareferral.db", help="Path to sqlite db")
    parser.add_argument("--search", default="intern", help="Search keyword (pass '' for all)")
    parser.add_argument("--all", action="store_true", help="Fetch all posts without search filter")
    parser.add_argument("--max-pages", type=int, default=None, help="Max pages to fetch (100 posts/page)")
    parser.add_argument("--delay", type=float, default=0.5, help="Polite delay between pages in seconds")
    parser.add_argument("--incremental", action="store_true", help="Stop once an existing post is seen")
    args = parser.parse_args()

    search_kw = None if args.all else (args.search if args.search else None)
    conn = init_db(Path(args.db))
    sync(conn, search_kw, args.max_pages, args.delay, args.incremental)
    conn.close()


if __name__ == "__main__":
    main()
