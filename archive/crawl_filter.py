#!/usr/bin/env python3
"""
jobviareferral.com crawler + LENIENT keyword pre-filter.

Target role families (per user):
    AI Engineer / ML Engineer / Data Scientist
    Python Developer / Python Engineer
    Backend Developer / Backend Engineer
    Software Engineer / Developer / SDE (kept for LLM review)

Fresher-adjacent policy: up to ~3 years OK; reject 4+.

The filter is intentionally lenient — its job is only to throw out
obviously-wrong titles (QA, sales, senior, 4+ years, non-CS engineering)
and pass anything Python/AI/backend-adjacent to the LLM judge later.

    python crawl_filter.py                     # crawl + filter + report
    python crawl_filter.py --refilter          # re-apply filter, no re-crawl
    python crawl_filter.py --show-rejected 30  # audit recent rejections
    python crawl_filter.py --reset             # wipe DB, start over
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

# ---------------------------------------------------------------------------
# Site constants
# ---------------------------------------------------------------------------
BASE_URL      = "https://jobviareferral.com/"
SEARCH_TERM   = "intern"
PAGE_PARAM    = "query-19731658-page"
CARD_SELECTOR = "div.gb-loop-item-ad7fcdad"      # excludes sidebar cards

HEADERS = {
    "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.7"),
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "priority": "u=0, i",
    "sec-ch-ua": '"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/155.0.0.0 Safari/537.36"),
}
COOKIES = {"growme_version": '{"name":"f69b217","version":"f69b217"}'}

# ---------------------------------------------------------------------------
# Title-level pattern sets
# ---------------------------------------------------------------------------
# Checked FIRST — if any of these match, we treat the role as a target role
# regardless of other words in the title (fixes "Python Developer | QA team").
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

# Rejected unconditionally — seniority words in the title.
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

# Rejected only if PASS_SPECIFIC did NOT match first (see classifier order).
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

# Anything matched here is kept (role_class="ambiguous") and handed to LLM.
# Kept narrow-ish to reduce noise; covers Python / AI / dev-adjacent phrasing.
KEEP_ANY_TITLE_PATTERNS = [
    r"\bAI\b", r"\bML\b", r"\bGenAI\b", r"\bLLM\b", r"\bMLOps\b",
    r"\bPython\b", r"\bBackend\b", r"\bBack[- ]End\b",
    r"\bDeveloper\b", r"\bProgrammer\b", r"\bSDE\b",
    r"\bData\s+(?:Engineer|Scientist|Science)\b",
    r"\bFull\s*Stack\b",
]

# Experience reject — fresher-adjacent policy = up to 3 years OK.
# Rejects explicit 4+ years or ranges whose UPPER bound is >= 4.
REJECT_EXP_PATTERNS = [
    # bare "4 years", "5+ yrs", "8 years of experience"
    r"\b[4-9]\s*\+?\s*(?:years?|yrs?)\b",
    r"\b1\d\s*\+?\s*(?:years?|yrs?)\b",
    # ranges like "1-5", "2-6", "3 – 10", "0 to 8"
    r"\b\d+\s*(?:-|–|to)\s*(?:[4-9]|1\d)\s*(?:years?|yrs?)\b",
    # ranges like "4-10", "5 – 8" (upper already covered, this catches start>=4)
    r"\b[4-9]\s*(?:-|–|to)\s*\d+\s*(?:years?|yrs?)\b",
]

# ---------------------------------------------------------------------------
# HTML parsers
# ---------------------------------------------------------------------------
def parse_listing_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []
    for card in soup.select(CARD_SELECTOR):
        post_id = None
        for cls in card.get("class", []):
            m = re.fullmatch(r"post-(\d+)", cls)
            if m:
                post_id = int(m.group(1))
                break
        if post_id is None:
            continue
        cat_a     = card.select_one(".gb-text-afce0e33 a")
        title_a   = card.select_one("h2.gb-text-642bff27 a")
        excerpt_e = card.select_one("p.gb-text-b829d9fd")
        if not title_a or not title_a.get("href"):
            continue
        jobs.append({
            "post_id":      post_id,
            "title":        title_a.get_text(" ", strip=True),
            "url":          title_a.get("href"),
            "category":     cat_a.get_text(strip=True) if cat_a else None,
            "category_url": cat_a.get("href") if cat_a else None,
            "excerpt":      excerpt_e.get_text(" ", strip=True) if excerpt_e else "",
        })
    return jobs


def parse_last_page(html: str) -> int | None:
    soup = BeautifulSoup(html, "html.parser")
    nums = []
    for a in soup.select("a.page-numbers"):
        m = re.search(rf"{PAGE_PARAM}=(\d+)", a.get("href", ""))
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else None

# ---------------------------------------------------------------------------
# Classifiers
# ---------------------------------------------------------------------------
def classify_title(title: str) -> tuple[bool, str | None, str | None]:
    """
    Returns (is_role_match: bool, role_class: str | None, reject_class: str | None).

    Order:
      1. seniority reject     → always drop
      2. PASS_SPECIFIC        → keep, tag by role
      3. NON_TARGET reject    → drop
      4. KEEP_ANY             → keep, tag "ambiguous"
      5. no match             → drop, "no_role_match"
    """
    # 1) Seniority — hard drop
    for pat in SENIORITY_REJECT:
        if re.search(pat, title, re.I):
            return False, None, "seniority"

    # 2) PASS_SPECIFIC — hard keep
    for role, patterns in PASS_SPECIFIC_TITLE_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, title, re.I):
                return True, role, None

    # 3) NON_TARGET — hard drop
    for pat in NON_TARGET_TITLE_REJECT:
        if re.search(pat, title, re.I):
            return False, None, "excluded_title"

    # 4) KEEP_ANY — ambiguous but keep for LLM
    for pat in KEEP_ANY_TITLE_PATTERNS:
        if re.search(pat, title, re.I):
            return True, "ambiguous", None

    # 5) Nothing
    return False, None, "no_role_match"


def has_experience_reject(title: str, excerpt: str) -> tuple[bool, list[str]]:
    text = f"{title} {excerpt}"
    reasons = [p for p in REJECT_EXP_PATTERNS if re.search(p, text, re.I)]
    return bool(reasons), reasons

# ---------------------------------------------------------------------------
# DB layer
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
    role_class TEXT,         -- 'ai' | 'python' | 'backend' | 'swe' | 'fullstack' | 'data_eng' | 'ambiguous' | NULL
    reject_class TEXT,       -- 'seniority' | 'excluded_title' | 'experience_too_high' | 'no_role_match' | NULL
    is_match INTEGER NOT NULL DEFAULT 0,
    match_reasons TEXT,      -- JSON: which role matched
    reject_reasons TEXT      -- JSON: which patterns rejected / reason
);
CREATE INDEX IF NOT EXISTS idx_listings_match ON listings(is_match);
"""


def connect_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_config(conn, key, value):
    conn.execute("INSERT INTO config(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))
    conn.commit()


def get_config(conn, key):
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def page_crawled(conn, page: int) -> bool:
    return conn.execute("SELECT 1 FROM crawled_pages WHERE page_number=?",
                        (page,)).fetchone() is not None


def mark_page_crawled(conn, page: int, count: int):
    conn.execute(
        "INSERT INTO crawled_pages(page_number, crawled_at, job_count) "
        "VALUES(?,?,?) ON CONFLICT(page_number) DO UPDATE SET "
        "crawled_at=excluded.crawled_at, job_count=excluded.job_count",
        (page, _now(), count))
    conn.commit()


# ---------------------------------------------------------------------------
# Filtering pipeline
# ---------------------------------------------------------------------------
def evaluate_job(job: dict) -> dict:
    is_role, role_class, reject_class = classify_title(job["title"])

    if not is_role:
        return {
            "role_class":    None,
            "reject_class":  reject_class,
            "is_match":      False,
            "match_reasons": json.dumps({"reject": reject_class}),
            "reject_reasons": json.dumps({
                "reject_class": reject_class,
                "title":        job["title"],
            }),
        }

    exp_reject, exp_reasons = has_experience_reject(
        job["title"], job["excerpt"])

    if exp_reject:
        return {
            "role_class":    role_class,
            "reject_class":  "experience_too_high",
            "is_match":      False,
            "match_reasons": json.dumps({
                "role_class": role_class,
                "category":   job["category"],
            }),
            "reject_reasons": json.dumps({
                "reject_class":         "experience_too_high",
                "experience_patterns":  exp_reasons,
            }),
        }

    return {
        "role_class":    role_class,
        "reject_class":  None,
        "is_match":      True,
        "match_reasons": json.dumps({
            "role_class": role_class,
            "category":   job["category"],
        }),
        "reject_reasons": json.dumps({}),
    }


def upsert_listing(conn, job: dict, page: int, now: str):
    ev = evaluate_job(job)
    conn.execute(
        """INSERT INTO listings (
               post_id, title, url, category, category_url, excerpt,
               source_page, first_seen, last_seen,
               role_class, reject_class, is_match, match_reasons, reject_reasons)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(post_id) DO UPDATE SET
               title=excluded.title,
               url=excluded.url,
               category=excluded.category,
               category_url=excluded.category_url,
               excerpt=excluded.excerpt,
               last_seen=excluded.last_seen,
               role_class=excluded.role_class,
               reject_class=excluded.reject_class,
               is_match=excluded.is_match,
               match_reasons=excluded.match_reasons,
               reject_reasons=excluded.reject_reasons""",
        (job["post_id"], job["title"], job["url"], job["category"],
         job["category_url"], job["excerpt"], page, now, now,
         ev["role_class"], ev["reject_class"], 1 if ev["is_match"] else 0,
         ev["match_reasons"], ev["reject_reasons"]))
    conn.commit()


def refilter_all(conn) -> int:
    rows = conn.execute(
        "SELECT post_id,title,url,category,category_url,excerpt,source_page "
        "FROM listings").fetchall()
    now = _now()
    for r in rows:
        job = {"post_id": r[0], "title": r[1], "url": r[2],
               "category": r[3], "category_url": r[4], "excerpt": r[5]}
        upsert_listing(conn, job, r[6], now)
    return len(rows)


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.update(COOKIES)
    return s


def build_url(page: int) -> str:
    if page == 1:
        return f"{BASE_URL}?s={SEARCH_TERM}"
    return f"{BASE_URL}?{PAGE_PARAM}={page}&s={SEARCH_TERM}"


def fetch_page(session: requests.Session, page: int, retries: int = 4) -> str:
    url = build_url(page)
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=30, allow_redirects=True)
            if r.status_code == 200:
                return r.text
            print(f"  page {page}: HTTP {r.status_code}, retry {attempt}/{retries}")
        except requests.RequestException as e:
            last_exc = e
            print(f"  page {page}: {e}, retry {attempt}/{retries}")
        time.sleep(2 * attempt)
    raise RuntimeError(f"failed to fetch page {page}: {last_exc}")


def crawl(conn, session, delay: float, max_pages: int):
    if not page_crawled(conn, 1):
        print("[page 1] fetching ...")
        html = fetch_page(session, 1)
        detected = parse_last_page(html)
        if detected:
            set_config(conn, "last_page", detected)
            print(f"[page 1] detected last page = {detected}")
        jobs = parse_listing_page(html)
        now = _now()
        for j in jobs:
            upsert_listing(conn, j, 1, now)
        mark_page_crawled(conn, 1, len(jobs))
        print(f"[page 1] {len(jobs)} listings stored")
        time.sleep(delay)
    else:
        print("[page 1] already crawled, skipping")

    last_page = int(get_config(conn, "last_page") or 1)
    end = min(last_page, max_pages)

    for page in range(2, end + 1):
        if page_crawled(conn, page):
            continue
        print(f"[page {page}/{end}] fetching ...")
        try:
            html = fetch_page(session, page)
        except RuntimeError as e:
            print(f"  ERROR: {e} — stopping")
            break
        jobs = parse_listing_page(html)
        if not jobs:
            print("  empty page, stopping")
            break
        now = _now()
        for j in jobs:
            upsert_listing(conn, j, page, now)
        mark_page_crawled(conn, page, len(jobs))
        print(f"  {len(jobs)} listings stored")
        time.sleep(args_delay_holder.get("delay", 1.5))


# small helper so crawl() can see the CLI delay without threading it through
args_delay_holder = {"delay": 1.5}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def report(conn, show_rejected: int = 0):
    total_listings = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    total_pages    = conn.execute("SELECT COUNT(*) FROM crawled_pages").fetchone()[0]
    matched        = conn.execute("SELECT COUNT(*) FROM listings WHERE is_match=1").fetchone()[0]

    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"Pages crawled:   {total_pages}")
    print(f"Listings stored: {total_listings}")
    print(f"Matched jobs:    {matched}")
    print()

    if matched:
        print("=" * 72)
        print("MATCHED JOBS (pre-filter survivors — will be judged by LLM next)")
        print("=" * 72)
        rows = conn.execute("""
            SELECT role_class, post_id, title, category, url
            FROM listings WHERE is_match=1
            ORDER BY role_class, post_id
        """).fetchall()
        current_role = None
        for role, post_id, title, category, url in rows:
            if role != current_role:
                print(f"\n--- role: {role} ---")
                current_role = role
            print(f"[{post_id}] {title}")
            print(f"        category: {category}")
            print(f"        url: {url}")
            print()

    if show_rejected:
        print("=" * 72)
        print(f"REJECTED  (showing {show_rejected} most recent)")
        print("=" * 72)
        rows = conn.execute("""
            SELECT role_class, reject_class, post_id, title, excerpt
            FROM listings WHERE is_match=0
            ORDER BY post_id DESC LIMIT ?
        """, (show_rejected,)).fetchall()
        for role, reject, post_id, title, excerpt in rows:
            reason = reject or "unknown"
            print(f"[{post_id}] ({reason}) {title}")
            print(f"        excerpt: {(excerpt or '')[:130]}...")
            print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="jobviareferral.db")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--max-pages", type=int, default=500)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--refilter", action="store_true",
                    help="Re-apply filter to existing DB without crawling")
    ap.add_argument("--show-rejected", type=int, default=0,
                    help="Also print N recent rejected jobs (for tuning)")
    args = ap.parse_args()

    args_delay_holder["delay"] = args.delay

    db_path = Path(args.db)
    if args.reset and db_path.exists():
        print(f"removing {db_path}")
        db_path.unlink()

    conn = connect_db(db_path)

    if args.refilter:
        print("Re-filtering existing DB ...")
        n = refilter_all(conn)
        print(f"Re-filtered {n} listings")
    else:
        session = make_session()
        try:
            crawl(conn, session, args.delay, args.max_pages)
        except KeyboardInterrupt:
            print("\nInterrupted. Progress is saved; re-run to resume.")

    report(conn, show_rejected=args.show_rejected)
    conn.close()


if __name__ == "__main__":
    main()