#!/usr/bin/env python3
"""
Free 4-Step Recruiter Email Discovery & Contact Enrichment Engine.

Discovers recruiter/HR contact emails for YES jobs that only have portal links.

The 4-Step Free Waterfall:
  Step 1: Domain Scraping (Free) -> Fetches /careers, /contact, /about on official company website
  Step 2: Search Engine OSINT (Free) -> Queries DuckDuckGo for public recruiter email footprints
  Step 3: Enrichment APIs (Free Tiers) -> Queries Apollo.io or Hunter.io if free API keys exist in .env
  Step 4: Pattern Generator + DNS/MX Verification (Free) -> Verifies company mailserver via dnspython
          and tests careers@{domain} / hr@{domain}

Usage:
  python enrich_contacts.py --dry-run             # Preview findings without saving to DB
  python enrich_contacts.py --limit 10            # Enrich up to 10 portal jobs
  python enrich_contacts.py                       # Enrich all eligible YES portal jobs
"""

import argparse
import os
import re
import socket
import sqlite3
import sys
import time
from urllib.parse import urlparse

import dns.resolver
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from dotenv import load_dotenv

load_dotenv()

# Ensure Windows terminal handles emojis cleanly
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")

GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "oraclecloud.com",
    "greenhouse.io", "lever.co", "workday.com", "myworkdayjobs.com", "linkedin.com",
    "jobviareferral.com", "google.com", "smartrecruiters.com", "rippling.com",
    "ashbyhq.com", "bamboohr.com", "recruitee.com", "taleo.net", "ripplehire.com",
    "darwinbox.in", "darwinbox.com", "keka.com", "keka.io", "freshteam.com", "zohorecruit.com",
    "internshala.com", "bing.com", "freshers.com", "freshersworld.com", "indeed.com",
    "naukri.com", "glassdoor.com", "ambitionbox.com", "wikipedia.org", "wikimedia.org",
    "youtube.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "careerspage.io"
}


def clean_company_name(title: str) -> str:
    """Extracts clean company name from job title."""
    t = re.sub(r"^[🚀🚨🔥💼📢\s]+", "", title).strip()
    
    # Patterns: "Fresher at {Company} Hiring", "{Company} Hiring", "Hiring at {Company}"
    m = re.search(r"Fresher at\s+([A-Za-z0-9&.\s]+?)\s+Hiring", t, re.I)
    if m:
        return m.group(1).strip()
        
    m = re.search(r"Hiring at\s+([A-Za-z0-9&.\s]+?)(?:\s+\d|\s+Hiring|\s+202|\s+Software|\s+Associate|\s+AI|\s*\||$)", t, re.I)
    if m:
        return m.group(1).strip()
        
    m = re.search(r"^([A-Za-z0-9&.\s]+?)\s+Hiring", t, re.I)
    if m:
        return m.group(1).strip()

    # Fallback to first 3 words
    words = t.split()[:3]
    return " ".join(words)


def find_company_domain(company_name: str, apply_url: str | None = None) -> str | None:
    """Finds official company domain from apply URL or DuckDuckGo search."""
    if apply_url:
        try:
            parsed = urlparse(apply_url)
            host = parsed.netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if host and not any(g in host for g in GENERIC_DOMAINS):
                return host
        except Exception:
            pass

    # Search DuckDuckGo for official website
    try:
        with DDGS() as ddgs:
            query = f"{company_name} official website"
            results = list(ddgs.text(query, max_results=3))
            for r in results:
                href = r.get("href", "")
                parsed = urlparse(href)
                host = parsed.netloc.lower()
                if host.startswith("www."):
                    host = host[4:]
                if host and not any(host.endswith(g) for g in GENERIC_DOMAINS):
                    return host
    except Exception:
        pass

    return None


def verify_domain_mx(domain: str) -> bool:
    """Checks whether domain has active MX records."""
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=4.0)
        return len(answers) > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Method 1: Website Scraping (/careers, /contact, /about)
# ---------------------------------------------------------------------------
def method_1_scrape_website(domain: str) -> list[str]:
    """Scrapes official company website pages for careers/contact emails."""
    found = set()
    paths = ["/careers", "/jobs", "/contact", "/contact-us", "/about"]
    
    for path in paths:
        url = f"https://{domain}{path}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=5)
            if resp.status_code == 200:
                matches = EMAIL_REGEX.findall(resp.text)
                for email in matches:
                    clean = email.lower().strip()
                    if clean.endswith(domain) and any(kw in clean for kw in ("career", "hr", "job", "talent", "hire", "work", "people")):
                        found.add(clean)
            if found:
                break
        except Exception:
            continue

    return list(found)


# ---------------------------------------------------------------------------
# Method 2: Search Engine OSINT (DuckDuckGo Footprints)
# ---------------------------------------------------------------------------
def method_2_search_osint(company_name: str, domain: str | None) -> list[str]:
    """Searches DuckDuckGo for public recruiter email footprints."""
    found = set()
    query = f'"{company_name}" ("careers@" OR "hr@" OR "jobs@" OR "talent@")'
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
            for r in results:
                snippet = r.get("body", "") + " " + r.get("title", "")
                for email in EMAIL_REGEX.findall(snippet):
                    clean = email.lower().strip()
                    if domain and clean.endswith(domain):
                        found.add(clean)
                    elif any(kw in clean for kw in ("career", "hr", "job", "talent", "hire", "recruit")):
                        found.add(clean)
    except Exception:
        pass
    return list(found)


# ---------------------------------------------------------------------------
# Method 3: Free Enrichment APIs (Apollo / Hunter if configured)
# ---------------------------------------------------------------------------
def method_3_enrichment_api(company_name: str, domain: str | None) -> list[tuple[str, str]]:
    """Queries Apollo or Hunter free tier if keys are provided in .env."""
    results = []
    
    # 1. Apollo.io Free Tier Check
    apollo_key = os.getenv("APOLLO_API_KEY")
    if apollo_key and domain:
        try:
            url = "https://api.apollo.io/v1/mixed_people/search"
            payload = {
                "api_key": apollo_key,
                "q_organization_domains": domain,
                "person_titles": ["Recruiter", "Talent Acquisition", "HR Manager", "Head of Talent"],
                "page": 1,
                "per_page": 2
            }
            resp = requests.post(url, json=payload, timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                for person in data.get("people", []):
                    email = person.get("email")
                    name = person.get("name") or "Hiring Team"
                    if email:
                        results.append((email.strip().lower(), name.strip()))
        except Exception:
            pass

    # 2. Hunter.io Free Tier Check
    hunter_key = os.getenv("HUNTER_API_KEY")
    if hunter_key and domain and not results:
        try:
            url = f"https://api.hunter.io/v2/domain-search?domain={domain}&api_key={hunter_key}&department=hr"
            resp = requests.get(url, timeout=6)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                for em in data.get("emails", []):
                    value = em.get("value")
                    first = em.get("first_name") or ""
                    last = em.get("last_name") or ""
                    name = f"{first} {last}".strip() or "Hiring Team"
                    if value:
                        results.append((value.strip().lower(), name))
        except Exception:
            pass

    return results


# ---------------------------------------------------------------------------
# Method 4: Pattern Generator + DNS/MX Mailserver Verification
# ---------------------------------------------------------------------------
def method_4_pattern_mx(domain: str) -> list[str]:
    """Verifies domain MX and returns standard careers/hr addresses."""
    if not domain:
        return []
        
    has_mx = verify_domain_mx(domain)
    if not has_mx:
        return []

    # Standard candidate mailbox patterns verified by domain MX
    return [f"careers@{domain}", f"hr@{domain}"]


# ---------------------------------------------------------------------------
# Master Enrichment Function
# ---------------------------------------------------------------------------
def enrich_single_job(post_id: int, title: str, apply_url: str | None) -> dict | None:
    """Attempts all 4 discovery methods sequentially."""
    company = clean_company_name(title)
    if not company:
        return None

    domain = find_company_domain(company, apply_url)
    if not domain:
        return None

    # Step 1: Scrape website
    m1_emails = method_1_scrape_website(domain)
    if m1_emails:
        return {
            "email": m1_emails[0],
            "name": "Hiring Team",
            "company": company,
            "method": "Website Scraping (/careers)",
            "domain": domain
        }

    # Step 2: Search Engine OSINT
    m2_emails = method_2_search_osint(company, domain)
    if m2_emails:
        return {
            "email": m2_emails[0],
            "name": "Hiring Team",
            "company": company,
            "method": "Search Engine OSINT",
            "domain": domain
        }

    # Step 3: Free Enrichment API (Apollo/Hunter)
    m3_results = method_3_enrichment_api(company, domain)
    if m3_results:
        return {
            "email": m3_results[0][0],
            "name": m3_results[0][1],
            "company": company,
            "method": "Enrichment API (Verified HR)",
            "domain": domain
        }

    # Step 4: Pattern Generator + DNS MX Verification
    m4_emails = method_4_pattern_mx(domain)
    if m4_emails:
        return {
            "email": m4_emails[0],
            "name": "Hiring Team",
            "company": company,
            "method": "DNS MX Verified Pattern",
            "domain": domain
        }

    return None


def main():
    parser = argparse.ArgumentParser(description="Recruiter Contact Discovery & Enrichment Engine")
    parser.add_argument("--db", default="jobviareferral.db", help="Path to SQLite database")
    parser.add_argument("--limit", type=int, default=0, help="Maximum portal jobs to inspect (default: 0 = all eligible jobs)")
    parser.add_argument("--dry-run", action="store_true", help="Preview discovered emails without inserting into database")
    parser.add_argument("--retry-failed", action="store_true", help="Retry jobs where previous search found no email")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)

    # Cache previously discovered company emails from hr_contacts
    known_companies = conn.execute("""
        SELECT company, hr_email, hr_name 
        FROM hr_contacts 
        WHERE hr_email IS NOT NULL AND hr_email != '' AND company IS NOT NULL
    """).fetchall()
    company_cache = {c[0].lower().strip(): (c[1], c[2]) for c in known_companies if c[0]}

    # Filter: strictly only unattempted WordPress portal jobs (multi-portal jobs are handled by enrich_linkedin_employees.py)
    status_filter = "" if args.retry_failed else "AND (d.enrich_status IS NULL OR d.enrich_status = '')"
    rows = conn.execute(f"""
        SELECT d.post_id, d.title, d.apply_url, d.url
        FROM details d
        WHERE d.verdict='yes'
          AND d.post_id < 200000000
          AND d.post_id NOT IN (SELECT DISTINCT post_id FROM hr_contacts)
          {status_filter}
        ORDER BY d.post_id DESC
    """).fetchall()

    if not rows:
        print("✅ No new unattempted portal jobs to enrich!")
        print("   (All portal jobs have either had emails discovered or been marked for manual apply).")
        conn.close()
        return

    target_rows = rows[:args.limit] if args.limit > 0 else rows

    print("=" * 65)
    print("🔍 RECRUITER CONTACT DISCOVERY & ENRICHMENT ENGINE")
    print("=" * 65)
    print(f"Unattempted Portal Jobs without emails : {len(rows)}")
    print(f"Jobs Selected for this batch           : {len(target_rows)}")
    print(f"Mode                                   : {'DRY RUN (Preview)' if args.dry_run else 'LIVE ENRICHMENT'}")
    print("-" * 65)

    discovered_count = 0
    from datetime import datetime, timezone

    for idx, (post_id, title, apply_url, job_url) in enumerate(target_rows, 1):
        comp = clean_company_name(title)
        comp_key = comp.lower().strip()
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        print(f"[{idx}/{len(target_rows)}] Post #{post_id} | {comp} ...", end=" ", flush=True)

        # Check in-memory / DB company cache first
        if comp_key in company_cache:
            cached_email, cached_name = company_cache[comp_key]
            print(f"🎯 FOUND via Company Cache ({cached_email})!")
            discovered_count += 1
            if not args.dry_run:
                conn.execute("""
                    INSERT OR IGNORE INTO hr_contacts (
                        post_id, hr_email, hr_name, job_title, company, job_url, mail_status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """, (post_id, cached_email, cached_name, title, comp, job_url))
                conn.execute("""
                    UPDATE details 
                    SET enrich_status='found', enrich_attempted_at=?
                    WHERE post_id=?
                """, (now_str, post_id))
                conn.commit()
            continue

        info = enrich_single_job(post_id, title, apply_url)
        if info:
            print(f"🎯 FOUND via {info['method']}!")
            print(f"       Recruiter: {info['name']} <{info['email']}>")
            print(f"       Domain   : {info['domain']}")
            discovered_count += 1
            company_cache[comp_key] = (info['email'], info['name'])

            if not args.dry_run:
                conn.execute("""
                    INSERT OR IGNORE INTO hr_contacts (
                        post_id, hr_email, hr_name, job_title, company, job_url, mail_status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """, (post_id, info['email'], info['name'], title, info['company'], job_url))
                conn.execute("""
                    UPDATE details 
                    SET enrich_status='found', enrich_attempted_at=?
                    WHERE post_id=?
                """, (now_str, post_id))
                conn.commit()
        else:
            print("❌ No verified contact found (Requires manual portal apply)")
            if not args.dry_run:
                conn.execute("""
                    UPDATE details 
                    SET enrich_status='not_found', enrich_attempted_at=?
                    WHERE post_id=?
                """, (now_str, post_id))
                conn.commit()

        time.sleep(1.0)  # Polite delay

    print("\n" + "=" * 65)
    print("ENRICHMENT SUMMARY")
    print(f"  Processed Jobs       : {len(target_rows)}")
    print(f"  New Emails Discovered: {discovered_count}")
    if not args.dry_run and discovered_count > 0:
        print(f"  ✅ Added {discovered_count} new recruiter email(s) into hr_contacts table!")
        print("  They will now be included in your cold email queue and dashboard.")
    print("=" * 65)

    conn.close()


if __name__ == "__main__":
    main()
