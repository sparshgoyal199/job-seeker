#!/usr/bin/env python3
"""
Multi-Portal Tech & AI Internship Ingestion Engine.

Scrapes live, high-intent Tech, Software, and AI roles from 100% FREE external sources:
  1. Internshala (Targeted streams: AI/ML, CS, Python/Django, Data Science, Web Dev)
  2. LinkedIn Jobs (Public Guest API - Rotated AI/Backend/Python queries & pagination offsets)
  3. Indeed India (Playwright Headless scraper with targeted queries)
  4. Remotive API (Free remote software & AI developer positions)

Features:
  - Smart In-Memory & DB Deduplication: Pre-checks against existing post_ids, URLs, and (company, title)
    in jobviareferral.db so it pages deeper to find genuinely NEW jobs rather than repeating page 1.
  - Strict Role Relevance Filter: Rejects non-tech internships (Sales, Marketing, HR, Content, Civil, etc.)
  - Synthetic, deterministic post_ids (200,000,000+ range) that never collide with WordPress IDs.
  - Zero Apify credits consumed (100% free HTTP, REST APIs & headless browser).
  - Pre-tagged verdict='yes' and structured_fields containing exact company names.

Usage:
  python -m src.ingestion.multi_portal              # Ingest from all 4 sources
  python -m src.ingestion.multi_portal --dry-run    # Preview discovered jobs without writing to DB
  python -m src.ingestion.multi_portal --limit 20   # Max 20 new jobs per source
"""

import argparse
import asyncio
import hashlib
import html
import json
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Strict Tech Role Relevance Filter
# ---------------------------------------------------------------------------
BAD_ROLE_KEYWORDS = {
    "sales", "business development", "bde", "telecaller", "tele-caller", "content writer",
    "content writing", "social media", "marketing", "digital marketing", "seo",
    "graphic designer", "ui/ux", "video editor", "hr intern", "human resources",
    "recruiter", "talent acquisition", "accountant", "accounting", "finance", "audit",
    "legal", "counselor", "operations intern", "event management", "teaching", "tutor",
    "medical", "nurse", "civil engineer", "mechanical engineer", "chemical engineer",
    "pharmacist", "doctor", "animator", "copywriter", "voice process", "bpo", "customer support"
}

GOOD_TECH_KEYWORDS = {
    "python", "ai", "artificial intelligence", "ml", "machine learning", "deep learning",
    "nlp", "llm", "generative ai", "genai", "rag", "langchain", "data science",
    "data engineer", "software", "sde", "developer", "backend", "full stack", "fullstack",
    "web developer", "react", "node", "django", "fastapi", "flask", "golang", "java",
    "c++", "cloud", "devops", "engineer", "intern", "coder", "programmer"
}


def is_valid_tech_internship(title: str) -> bool:
    """Returns True only if the role title is technical and relevant to Sparsh's stack."""
    if not title:
        return False
    t_clean = re.sub(r"[^a-zA-Z0-9\s]", " ", title).lower()
    words = set(t_clean.split())

    # Check for hard negative non-tech keywords
    for bad in BAD_ROLE_KEYWORDS:
        if bad in t_clean:
            return False

    # Check for positive software/AI engineering keywords
    for good in GOOD_TECH_KEYWORDS:
        if good in t_clean or good in words:
            return True

    return False


def make_synthetic_id(portal: str, company: str, title: str) -> int:
    """Generates a stable, collision-free integer ID in the 200,000,000 - 299,999,999 range."""
    raw = f"{portal.lower()}:{company.strip().lower()}:{title.strip().lower()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    int_val = int(digest[:8], 16)
    return 200_000_000 + (int_val % 99_999_999)


def normalize_url(url: str) -> str:
    """Strips query parameters and trailing slashes for clean URL matching."""
    if not url:
        return ""
    base = url.split("?")[0].split("#")[0].strip().lower().rstrip("/")
    return base


def get_known_db_entities(db_path: str = "jobviareferral.db") -> tuple[set[int], set[str], set[str]]:
    """Loads existing synthetic IDs, URLs, and (company - title) pairs from database."""
    known_ids: set[int] = set()
    known_urls: set[str] = set()
    known_pairs: set[str] = set()

    if not Path(db_path).exists():
        return known_ids, known_urls, known_pairs

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        for row in cur.execute("SELECT post_id, title, url FROM listings"):
            pid, title, url = row
            if pid:
                known_ids.add(int(pid))
            if url:
                known_urls.add(normalize_url(url))
            if title:
                known_pairs.add(title.strip().lower())

        for row in cur.execute("SELECT post_id, apply_url, title FROM details"):
            pid, aurl, title = row
            if pid:
                known_ids.add(int(pid))
            if aurl:
                known_urls.add(normalize_url(aurl))
            if title:
                known_pairs.add(title.strip().lower())
        conn.close()
    except Exception as e:
        print(f"   ⚠️ Could not load existing DB keys for deduplication: {e}")

    return known_ids, known_urls, known_pairs


# ---------------------------------------------------------------------------
# 1. Internshala Scraper (Targeted Tech Streams)
# ---------------------------------------------------------------------------
def scrape_internshala(max_jobs: int = 40, known_entities: tuple = None) -> list[dict]:
    """Scrapes active internships from Internshala targeted tech streams, skipping known jobs."""
    print(f"🌐 [Internshala] Fetching active tech internships across targeted streams (target: {max_jobs})...")
    jobs = []
    known_ids, known_urls, known_pairs = known_entities or (set(), set(), set())
    seen_in_run = set()

    streams = [
        "artificial-intelligence-ai-internship",
        "computer-science-internship",
        "python-django-internship",
        "data-science-internship",
        "machine-learning-internship",
        "web-development-internship",
    ]

    for stream in streams:
        if len(jobs) >= max_jobs:
            break
        page = 1
        consecutive_empty = 0

        while len(jobs) < max_jobs and page <= 5:
            url = f"https://internshala.com/internships/{stream}/page-{page}/"
            try:
                resp = requests.get(url, headers=HEADERS, timeout=12)
                if resp.status_code != 200:
                    break
                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.select(".individual_internship")
                if not cards:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        break
                    page += 1
                    continue

                for c in cards:
                    t = c.select_one(".job-internship-name")
                    comp = c.select_one(".company-name")
                    link = c.select_one(".job-title-href")
                    loc = c.select_one(".locations")

                    if not t or not comp:
                        continue

                    title_text = t.get_text(strip=True)
                    comp_text = comp.get_text(strip=True)
                    if len(comp_text) < 2 or not is_valid_tech_internship(title_text):
                        continue

                    job_url = "https://internshala.com" + link.get("href", "") if link else ""
                    norm_url = normalize_url(job_url)
                    pid = make_synthetic_id("Internshala", comp_text, title_text)
                    pair = f"{comp_text} - {title_text}".lower()

                    if pid in known_ids or norm_url in known_urls or pair in known_pairs:
                        continue
                    if pid in seen_in_run:
                        continue
                    seen_in_run.add(pid)

                    jobs.append({
                        "portal": "Internshala",
                        "title": title_text,
                        "company": comp_text,
                        "location": loc.get_text(strip=True) if loc else "India / Remote",
                        "url": job_url,
                    })

                    if len(jobs) >= max_jobs:
                        break

            except Exception as e:
                print(f"   ⚠️ Internshala {stream} page {page} scraping error: {e}")
                break

            page += 1
            time.sleep(0.3)

    print(f"   ✅ Internshala: Discovered {len(jobs)} brand-new active hiring companies.")
    return jobs


# ---------------------------------------------------------------------------
# 2. LinkedIn Public Jobs Scraper (Rotated Queries & Offsets)
# ---------------------------------------------------------------------------
def scrape_linkedin_guest(max_jobs: int = 40, known_entities: tuple = None) -> list[dict]:
    """Scrapes public postings from LinkedIn Guest API across rotated queries & offsets."""
    print(f"🌐 [LinkedIn Jobs] Querying public postings across rotated search queries (target: {max_jobs})...")
    jobs = []
    known_ids, known_urls, known_pairs = known_entities or (set(), set(), set())
    seen_in_run = set()

    queries = [
        "Python Developer Intern",
        "AI Engineer Intern",
        "Machine Learning Intern",
        "Backend Developer Intern",
        "Software Engineer Intern",
        "Generative AI",
        "Data Science Intern",
    ]
    # Shuffle queries slightly to explore different sectors each run
    random.shuffle(queries)

    for q in queries:
        if len(jobs) >= max_jobs:
            break
        start = 0
        while len(jobs) < max_jobs and start <= 100:
            url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={q.replace(' ', '%20')}&location=India&start={start}"
            try:
                resp = requests.get(url, headers=HEADERS, timeout=12)
                if resp.status_code != 200:
                    break
                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.select("li")
                if not cards:
                    break

                for c in cards:
                    t = c.select_one(".base-search-card__title")
                    comp = c.select_one(".base-search-card__subtitle")
                    link = c.select_one("a.base-card__full-link")
                    loc = c.select_one(".job-search-card__location")

                    if not t or not comp:
                        continue

                    clean_title = t.get_text(strip=True)
                    clean_comp = comp.get_text(strip=True)
                    if len(clean_comp) < 2 or not is_valid_tech_internship(clean_title):
                        continue

                    clean_link = link.get("href", "").split("?")[0] if link else ""
                    norm_url = normalize_url(clean_link)
                    pid = make_synthetic_id("LinkedIn Jobs", clean_comp, clean_title)
                    pair = f"{clean_comp} - {clean_title}".lower()

                    if pid in known_ids or norm_url in known_urls or pair in known_pairs:
                        continue
                    if pid in seen_in_run:
                        continue
                    seen_in_run.add(pid)

                    jobs.append({
                        "portal": "LinkedIn Jobs",
                        "title": clean_title,
                        "company": clean_comp,
                        "location": loc.get_text(strip=True) if loc else "India",
                        "url": clean_link,
                    })

                    if len(jobs) >= max_jobs:
                        break

                start += 25
                time.sleep(0.3)
            except Exception as e:
                print(f"   ⚠️ LinkedIn Jobs scraping error at start={start}: {e}")
                break

    print(f"   ✅ LinkedIn Jobs: Discovered {len(jobs)} brand-new active hiring companies.")
    return jobs


# ---------------------------------------------------------------------------
# 3. Remotive Free Remote API Ingestion
# ---------------------------------------------------------------------------
def scrape_remotive_api(max_jobs: int = 40, known_entities: tuple = None) -> list[dict]:
    """Scrapes live remote software, AI, and developer postings from 100% free Remotive API."""
    print(f"🌐 [Remotive API] Fetching live remote developer & AI positions (target: {max_jobs})...")
    jobs = []
    known_ids, known_urls, known_pairs = known_entities or (set(), set(), set())
    seen_in_run = set()

    categories = ["software-dev", "data"]
    for cat in categories:
        if len(jobs) >= max_jobs:
            break
        url = f"https://remotive.com/api/remote-jobs?category={cat}&limit=50"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            if resp.status_code != 200:
                continue
            data = resp.json()
            raw_jobs = data.get("jobs", [])
            for rj in raw_jobs:
                title = rj.get("title", "").strip()
                company = rj.get("company_name", "").strip()
                apply_url = rj.get("url", "").strip()
                location = rj.get("candidate_required_location", "Remote")

                if not title or not company:
                    continue
                if not is_valid_tech_internship(title):
                    continue

                pid = make_synthetic_id("Remotive", company, title)
                clean_url = normalize_url(apply_url)
                pair = f"{company} - {title}".lower()

                if pid in known_ids or clean_url in known_urls or pair in known_pairs:
                    continue
                if pid in seen_in_run:
                    continue
                seen_in_run.add(pid)

                jobs.append({
                    "portal": "Remotive",
                    "title": title,
                    "company": company,
                    "location": f"Remote ({location})" if location else "Remote",
                    "url": apply_url,
                    "description": rj.get("description", "")
                })

                if len(jobs) >= max_jobs:
                    break
        except Exception as e:
            print(f"   ⚠️ Remotive API error for category {cat}: {e}")

    print(f"   ✅ Remotive: Discovered {len(jobs)} brand-new active hiring companies.")
    return jobs


# ---------------------------------------------------------------------------
# 4. Arbeitnow Free Tech & Remote API Ingestion
# ---------------------------------------------------------------------------
def scrape_arbeitnow_api(max_jobs: int = 40, known_entities: tuple = None) -> list[dict]:
    """Scrapes live tech & developer roles from Arbeitnow free public API."""
    print(f"🌐 [Arbeitnow API] Fetching live tech & developer positions (target: {max_jobs})...")
    jobs = []
    known_ids, known_urls, known_pairs = known_entities or (set(), set(), set())
    seen_in_run = set()

    try:
        resp = requests.get("https://www.arbeitnow.com/api/job-board-api", headers=HEADERS, timeout=12)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            for item in data:
                title = item.get("title", "").strip()
                company = item.get("company_name", "").strip()
                apply_url = item.get("url", "").strip()
                loc = item.get("location", "Remote")
                is_remote = item.get("remote", False)

                if not title or not company or len(company) < 2:
                    continue
                if not is_valid_tech_internship(title):
                    continue

                pid = make_synthetic_id("Arbeitnow", company, title)
                clean_url = normalize_url(apply_url)
                pair = f"{company} - {title}".lower()

                if pid in known_ids or clean_url in known_urls or pair in known_pairs:
                    continue
                if pid in seen_in_run:
                    continue
                seen_in_run.add(pid)

                location_str = "Remote" if is_remote else (loc or "Remote")
                jobs.append({
                    "portal": "Arbeitnow",
                    "title": title,
                    "company": company,
                    "location": location_str,
                    "url": apply_url,
                    "description": item.get("description", ""),
                })

                if len(jobs) >= max_jobs:
                    break
    except Exception as e:
        print(f"   ⚠️ Arbeitnow API error: {e}")

    print(f"   ✅ Arbeitnow: Discovered {len(jobs)} brand-new active hiring companies.")
    return jobs


# ---------------------------------------------------------------------------
# 5. Indeed India Scraper via Playwright
# ---------------------------------------------------------------------------
async def scrape_indeed_playwright(max_jobs: int = 40, known_entities: tuple = None) -> list[dict]:
    """Scrapes vacancies from Indeed India via Playwright, deduplicating against database."""
    print(f"🌐 [Indeed India] Launching browser to scan active postings (target: {max_jobs})...")
    jobs = []
    known_ids, known_urls, known_pairs = known_entities or (set(), set(), set())
    seen_in_run = set()

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            queries = ["python intern", "ai intern", "software developer intern"]
            for q in queries:
                if len(jobs) >= max_jobs:
                    break
                start = 0
                while len(jobs) < max_jobs and start <= 40:
                    url = f"https://in.indeed.com/jobs?q={q.replace(' ', '+')}&l=India&start={start}"
                    try:
                        resp = await page.goto(url, timeout=12000)
                        if resp and resp.status == 403:
                            print("   ⚠️ Indeed India returned 403 (Cloudflare block) — skipping Indeed.")
                            await browser.close()
                            return jobs
                        title_text = await page.title()
                        if "blocked" in title_text.lower():
                            print("   ⚠️ Indeed India bot detection triggered — skipping Indeed.")
                            await browser.close()
                            return jobs

                        cards = await page.locator("div.cardOutline, div.job_seen_beacon").all()
                        if not cards:
                            break
                        for c in cards:
                            title, company, loc = "", "", "India"
                            title_elem = c.locator("h2.jobTitle, a.jcs-JobTitle, [data-testid='jobTitle']").first
                            if await title_elem.count() > 0:
                                title = (await title_elem.inner_text()).strip()

                            comp_elem = c.locator("[data-testid='company-name'], span.companyName, [data-testid='company_name']").first
                            if await comp_elem.count() > 0:
                                company = (await comp_elem.inner_text()).strip()

                            loc_elem = c.locator("[data-testid='text-location'], div.companyLocation").first
                            if await loc_elem.count() > 0:
                                loc = (await loc_elem.inner_text()).strip()

                            if not title or not company:
                                text = await c.inner_text()
                                lines = [l.strip() for l in text.split("\n") if l.strip()]
                                bad_prefixes = ("opportunity:", "urgently", "easily apply", "new", "responsive", "employer", "hiring", "from ₹", "from $")
                                clean_lines = [l for l in lines if not any(l.lower().startswith(bp) for bp in bad_prefixes) and "performance" not in l.lower()]
                                if len(clean_lines) >= 2:
                                    if not title:
                                        title = clean_lines[0].replace("Internship:", "").replace("Full-time:", "").strip()
                                    if not company:
                                        company = clean_lines[1]
                                    if len(clean_lines) >= 3 and loc == "India":
                                        loc = clean_lines[2]

                            bad_company = any(b in company.lower() for b in ["opportunity", "performance", "easily apply", "urgently hiring", "responsive employer"])
                            if not title or not company or bad_company or len(company) < 2 or len(company) > 60:
                                continue

                            title = title.replace("Internship:", "").replace("Full-time:", "").strip()
                            if not is_valid_tech_internship(title):
                                continue

                            # Extract direct job URL
                            job_url = url
                            jk = await c.get_attribute("data-jk")
                            if not jk and await title_elem.count() > 0:
                                jk = await title_elem.get_attribute("data-jk")
                            if not jk and await title_elem.count() > 0:
                                href = await title_elem.get_attribute("href") or ""
                                if "jk=" in href:
                                    m = re.search(r"jk=([a-zA-Z0-9]+)", href)
                                    if m:
                                        jk = m.group(1)
                                elif href.startswith("/rc/clk") or href.startswith("/viewjob"):
                                    job_url = f"https://in.indeed.com{href}"
                            if jk:
                                job_url = f"https://in.indeed.com/viewjob?jk={jk}"

                            pid = make_synthetic_id("Indeed", company, title)
                            norm_url = normalize_url(job_url)
                            pair = f"{company} - {title}".lower()

                            if pid in known_ids or norm_url in known_urls or pair in known_pairs:
                                continue
                            if pid in seen_in_run:
                                continue
                            seen_in_run.add(pid)

                            jobs.append({
                                "portal": "Indeed",
                                "title": title,
                                "company": company,
                                "location": loc or "India",
                                "url": job_url,
                            })

                            if len(jobs) >= max_jobs:
                                break
                    except Exception:
                        break
                    start += 10
            await browser.close()
    except Exception as e:
        print(f"   ⚠️ Indeed India scraping error: {e}")

    print(f"   ✅ Indeed India: Discovered {len(jobs)} brand-new active hiring companies.")
    return jobs


# ---------------------------------------------------------------------------
# Database Ingestion & Upsert
# ---------------------------------------------------------------------------
def ingest_jobs_to_db(jobs: list[dict], db_path: str = "jobviareferral.db", dry_run: bool = False) -> tuple[int, int]:
    """Upserts multi-portal jobs into listings and details tables with verdict='yes' pre-approved."""
    if not jobs:
        return 0, 0

    if dry_run:
        print("\n🔍 DRY-RUN MODE: Preview of jobs to be ingested:")
        for i, j in enumerate(jobs, 1):
            pid = make_synthetic_id(j["portal"], j["company"], j["title"])
            print(f"   [{i:2d}] ID: {pid} | [{j['portal']:12}] {j['company']:25} | {j['title'][:35]} ({j['location']})")
        return len(jobs), 0

    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).isoformat()
    today_str = datetime.now().strftime("%Y-%m-%d")

    new_count = 0
    updated_count = 0

    for j in jobs:
        pid = make_synthetic_id(j["portal"], j["company"], j["title"])
        portal = j["portal"]
        company = j["company"]
        title = j["title"]
        loc = j.get("location", "India / Remote")
        url = j.get("url", "")
        raw_desc = j.get("description", "")
        desc_text = raw_desc or f"Role: {title}\nCompany: {company}\nLocation: {loc}\nPortal Source: {portal}\nJob URL: {url}"

        row = conn.execute("SELECT post_id FROM listings WHERE post_id=?", (pid,)).fetchone()
        is_new = row is None

        structured_fields = json.dumps({
            "Company": company,
            "Portal": portal,
            "Location": loc,
            "Role": title,
            "Source": "multi_portal_scraper"
        }, ensure_ascii=False)

        # 1. Upsert into listings
        conn.execute("""
            INSERT INTO listings (
                post_id, title, url, category, excerpt,
                source_page, first_seen, last_seen,
                role_class, reject_class, is_match, match_reasons, reject_reasons
            ) VALUES (?, ?, ?, 'Internship', ?, ?, ?, ?, 'open_hiring', NULL, 1, ?, '{}')
            ON CONFLICT(post_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                url = excluded.url
        """, (
            pid,
            f"{company} - {title}",
            url,
            f"{title} at {company} ({loc})",
            portal,
            now,
            now,
            json.dumps({"portal": portal, "company": company, "role_class": "open_hiring"}),
        ))

        # 2. Upsert into details (verdict='yes' pre-approved directly, bypassing LLM judge)
        conn.execute("""
            INSERT INTO details (
                post_id, url, title, category, published_on,
                description_text, description_html, apply_url, structured_fields,
                fetched_at, fetch_status, verdict, verdict_reason, location
            ) VALUES (?, ?, ?, 'Internship', ?, ?, ?, ?, ?, ?, 'ok', 'yes', 'Open Hiring Mode (Direct Ingestion)', ?)
            ON CONFLICT(post_id) DO UPDATE SET
                apply_url = COALESCE(excluded.apply_url, details.apply_url),
                structured_fields = excluded.structured_fields,
                fetched_at = excluded.fetched_at,
                verdict = CASE WHEN details.verdict IS NULL OR details.verdict = '' THEN 'yes' ELSE details.verdict END,
                verdict_reason = CASE WHEN details.verdict IS NULL OR details.verdict = '' THEN 'Open Hiring Mode (Direct Ingestion)' ELSE details.verdict_reason END
        """, (
            pid,
            url,
            f"{company} - {title}",
            today_str,
            desc_text,
            f"<p>{desc_text}</p>",
            url,
            structured_fields,
            now,
            loc,
        ))

        if is_new:
            new_count += 1
        else:
            updated_count += 1

    conn.commit()
    conn.close()

    return new_count, updated_count


# ---------------------------------------------------------------------------
# Pipeline Runner
# ---------------------------------------------------------------------------
async def run_pipeline(limit_per_portal: int = 40, dry_run: bool = False, db_path: str = "jobviareferral.db"):
    print("=" * 65)
    print("🚀 MULTI-PORTAL OPEN HIRING INGESTION ENGINE")
    print("Portals: LinkedIn Jobs (Public Guest), Internshala, Remotive API, Arbeitnow API, Indeed India")
    print("Cost   : $0.00 (Zero Apify credits consumed)")
    print(f"Target : {limit_per_portal} fresh jobs per portal (Total: ~{limit_per_portal * 5} jobs)")
    print("=" * 65)

    known_entities = get_known_db_entities(db_path)
    print(f"📊 Pre-filtering against {len(known_entities[0])} known DB entries to avoid duplicate scraping.\n")

    all_jobs = []

    # 1. Internshala
    int_jobs = scrape_internshala(max_jobs=limit_per_portal, known_entities=known_entities)
    all_jobs.extend(int_jobs)

    # 2. LinkedIn Public Jobs
    li_jobs = scrape_linkedin_guest(max_jobs=limit_per_portal, known_entities=known_entities)
    all_jobs.extend(li_jobs)

    # 3. Remotive API
    rem_jobs = scrape_remotive_api(max_jobs=limit_per_portal, known_entities=known_entities)
    all_jobs.extend(rem_jobs)

    # 4. Arbeitnow Free Tech & Remote API
    arb_jobs = scrape_arbeitnow_api(max_jobs=limit_per_portal, known_entities=known_entities)
    all_jobs.extend(arb_jobs)

    # 5. Indeed India via Playwright (with fast bot detection bypass)
    ind_jobs = await scrape_indeed_playwright(max_jobs=limit_per_portal, known_entities=known_entities)
    all_jobs.extend(ind_jobs)

    print("\n" + "=" * 65)
    print(f"SUMMARY: Gathered {len(all_jobs)} brand-new candidate postings across portals.")
    print("=" * 65)

    new_count, updated_count = ingest_jobs_to_db(all_jobs, db_path=db_path, dry_run=dry_run)

    if not dry_run:
        print(f"🎉 Database Ingestion Complete:")
        print(f"   ➕ Brand New Jobs Added: {new_count}")
        print(f"   🔄 Existing Jobs Refreshed: {updated_count}")
        print(f"   🎯 Total Ready for Decision-Maker Enrichment: {new_count + updated_count}")


def main():
    parser = argparse.ArgumentParser(description="Multi-Portal Open Hiring Ingestion Engine")
    parser.add_argument("--limit", type=int, default=40, help="Max fresh jobs to scrape per portal (default: 40)")
    parser.add_argument("--dry-run", action="store_true", help="Preview mode without writing to database")
    parser.add_argument("--db", default="jobviareferral.db", help="Path to SQLite database")
    args = parser.parse_args()

    asyncio.run(run_pipeline(limit_per_portal=args.limit, dry_run=args.dry_run, db_path=args.db))


if __name__ == "__main__":
    main()
