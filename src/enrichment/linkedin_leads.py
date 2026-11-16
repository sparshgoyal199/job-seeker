#!/usr/bin/env python3
"""
LinkedIn Decision-Maker & Tech Leader Discovery Engine.

Features:
  1. Identifies companies offering jobs matching candidate skills (Python, AI/ML, Full Stack, SDE).
  2. Resolves company official LinkedIn page (https://www.linkedin.com/company/...).
  3. Discovers HRs, Recruiters, Founders, CTOs, and Engineering Leads using:
     - Apify People Scraper (memo23 with 6-token automatic pool rotation)
     - Browser-based LinkedIn search via logged-in Chrome profile (Zero Apify credits)
  4. Deduplicates profiles and classifies them into:
     - HR & Talent Acquisition (Recruiters, HR Managers, Campus Leads)
     - Tech Leadership & Engineering (CTOs, Founders, Engineering Managers, Tech Leads)
  5. Generates high-converting, personalized 300-char LinkedIn connection request notes.
  6. Stores in SQLite (linkedin_leads table) and exports to CSV + interactive HTML dashboard.

Usage:
  uv run python -m src.enrichment.linkedin_leads --limit 5
  uv run python -m src.enrichment.linkedin_leads --company "Datafoundry"
  uv run python -m src.enrichment.linkedin_leads --post-id 296373135
  uv run python -m src.enrichment.linkedin_leads --method browser   # Zero Apify credits
  uv run python -m src.enrichment.linkedin_leads --method apify     # Pure Apify
  uv run python -m src.enrichment.linkedin_leads --export           # Open HTML dashboard
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import requests
from dotenv import dotenv_values, load_dotenv

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(PROJECT_DIR / ".env", override=True)

CHROME_PROFILE_DIR = PROJECT_DIR / "chrome_profile"
DB_PATH = PROJECT_DIR / "jobviareferral.db"
CSV_EXPORT_PATH = PROJECT_DIR / "matched_linkedin_leads.csv"
HTML_REPORT_PATH = PROJECT_DIR / "matched_linkedin_leads.html"

GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "oraclecloud.com",
    "greenhouse.io", "lever.co", "workday.com", "myworkdayjobs.com", "linkedin.com",
    "jobviareferral.com", "google.com", "smartrecruiters.com",
    "wikipedia.org", "wikimedia.org", "glassdoor.com", "indeed.com", "internshala.com",
    "ambitionbox.com", "crunchbase.com", "naukri.com", "youtube.com", "facebook.com",
    "instagram.com", "twitter.com", "x.com"
}


def get_apify_tokens() -> list[str]:
    """Retrieves all configured Apify tokens from .env."""
    env = dotenv_values(str(PROJECT_DIR / ".env"))
    raw = env.get("APIFY_API_TOKEN", "") or os.getenv("APIFY_API_TOKEN", "")
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    for i in range(1, 10):
        val = env.get(f"APIFY_API_TOKEN_{i}", "") or os.getenv(f"APIFY_API_TOKEN_{i}", "")
        if val and val.strip() not in tokens:
            tokens.append(val.strip())
    return tokens


def get_primary_mx(domain: str) -> str | None:
    """Returns primary MX host for domain if valid."""
    if not domain:
        return None
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=3.0)
        sorted_answers = sorted(answers, key=lambda r: r.preference)
        if sorted_answers:
            return sorted_answers[0].exchange.to_text().rstrip(".")
    except Exception:
        pass
    return None


def find_company_domain(company_name: str, apply_url: str = "") -> str | None:
    """Finds official company domain from apply_url or search."""
    if apply_url:
        try:
            parsed = urlparse(apply_url)
            host = parsed.netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if host and not any(host.endswith(g) for g in GENERIC_DOMAINS):
                if get_primary_mx(host):
                    return host
        except Exception:
            pass

    clean_comp = re.sub(r"\b(Inc|LLC|Ltd|Pvt|Technologies|Labs|Solutions|Corporation|Corp)\b", "", company_name, flags=re.I).strip()
    clean_comp = re.sub(r"[^a-zA-Z0-9\s]", "", clean_comp).strip()

    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            res = list(ddgs.text(f"{clean_comp} official website", max_results=4))
            for r in res:
                host = urlparse(r.get("href", "")).netloc.lower()
                if host.startswith("www."):
                    host = host[4:]
                if host and not any(host.endswith(g) for g in GENERIC_DOMAINS):
                    if get_primary_mx(host):
                        return host
    except Exception:
        pass

    slug = re.sub(r"[^a-zA-Z0-9]", "", clean_comp.lower())
    if slug and len(slug) >= 3:
        for tld in [".com", ".ai", ".in", ".io", ".co"]:
            cand = f"{slug}{tld}"
            if get_primary_mx(cand):
                return cand

    return None


def predict_corporate_email(full_name: str, domain: str) -> str | None:
    """Predicts standard corporate email address (first.last@domain or first@domain)."""
    parts = [p.lower() for p in re.findall(r"[a-zA-Z]+", full_name or "")]
    if not parts or not domain:
        return None
    first = parts[0]
    last = parts[-1] if len(parts) > 1 else ""
    if len(first) < 2:
        return None
    if last and len(last) >= 2:
        return f"{first}.{last}@{domain}"
    return f"{first}@{domain}"


def init_leads_table(conn: sqlite3.Connection):
    """Initializes the linkedin_leads table in SQLite."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS linkedin_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER,
            company_name TEXT,
            job_title TEXT,
            person_name TEXT,
            headline TEXT,
            linkedin_url TEXT UNIQUE,
            category TEXT,
            connection_note TEXT,
            source TEXT,
            discovered_at TEXT,
            email TEXT,
            domain TEXT
        )
    """)
    cols = [col[1] for col in conn.execute("PRAGMA table_info(linkedin_leads)").fetchall()]
    if "email" not in cols:
        conn.execute("ALTER TABLE linkedin_leads ADD COLUMN email TEXT")
    if "domain" not in cols:
        conn.execute("ALTER TABLE linkedin_leads ADD COLUMN domain TEXT")
    conn.commit()


# ---------------------------------------------------------------------------
# Decision Maker Categorization & Relevance Scoring
# ---------------------------------------------------------------------------
def categorize_role(headline: str) -> tuple[str, int]:
    """
    Categorizes role into HR/Talent or Tech Leader, and returns (category, score).
    """
    h_low = (headline or "").lower()

    # HR & Talent Acquisition
    hr_keywords = [
        "recruiter", "talent acquisition", "technical recruiter", "campus recruiter",
        "human resources", "hr manager", "hr lead", "people partner", "head of talent",
        "talent lead", "talent specialist", "talent scout"
    ]
    if any(k in h_low for k in hr_keywords):
        return "HR & Talent Acquisition", 95

    # Tech Leadership
    tech_keywords = [
        "cto", "chief technology officer", "founder", "co-founder", "head of ai",
        "head of engineering", "vp of engineering", "vp engineering",
        "engineering manager", "technical lead", "tech lead", "lead engineer",
        "director of engineering", "principal engineer", "staff engineer"
    ]
    if any(k in h_low for k in tech_keywords):
        return "Tech Leadership & Engineering", 90

    # General Engineering / Developers
    if any(k in h_low for k in ["software engineer", "developer", "machine learning", "data scientist"]):
        return "Engineering & Developers", 60

    return "Other Team Member", 30


def generate_connection_note(person_name: str, headline: str, company: str, job_title: str) -> str:
    """
    Generates a personalized, professional connection request note under 300 characters.
    """
    first_name = (person_name.split()[0] if person_name else "there").capitalize()
    cat, _ = categorize_role(headline)

    if cat == "HR & Talent Acquisition":
        note = (
            f"Hi {first_name}, I saw {company} is hiring for {job_title or 'tech roles'}. "
            f"As a 2026 CS student experienced with Python, RAG & AI agents, "
            f"I’d love to connect and share my portfolio for upcoming internship opportunities!"
        )
    else:
        note = (
            f"Hi {first_name}, I'm a 2026 CS student building AI/RAG systems in Python "
            f"(KhojAI, DhanTrackAI). I noticed {company}'s exciting engineering work and "
            f"would love to connect and follow your updates!"
        )

    # Ensure strictly <= 300 characters (LinkedIn limit)
    if len(note) > 300:
        note = note[:297] + "..."
    return note


# ---------------------------------------------------------------------------
# Company LinkedIn Discovery
# ---------------------------------------------------------------------------
def find_company_linkedin(company_name: str, apply_url: str = "") -> str:
    """Resolves company's official LinkedIn page (https://www.linkedin.com/company/...)."""
    # 1. Check if apply_url already contains linkedin.com/company
    if apply_url and "linkedin.com/company/" in apply_url.lower():
        m = re.search(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([a-zA-Z0-9_\-]+)", apply_url, re.I)
        if m:
            return f"https://www.linkedin.com/company/{m.group(1).lower()}"

    # 2. Try DuckDuckGo / Bing Search
    clean_comp = re.sub(r"\b(Inc|LLC|Ltd|Pvt|Technologies|Labs|Solutions|Corporation|Corp)\b", "", company_name, flags=re.I).strip()
    if not clean_comp:
        clean_comp = company_name

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            res = list(ddgs.text(f'"{clean_comp}" site:linkedin.com/company', max_results=3))
            for r in res:
                m = re.search(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([a-zA-Z0-9_\-]+)", r.get("href", ""), re.I)
                if m:
                    return f"https://www.linkedin.com/company/{m.group(1).lower()}"
    except Exception:
        pass

    # Fallback default slug
    slug = re.sub(r"[^a-zA-Z0-9\-]", "", clean_comp.lower().replace(" ", "-"))
    return f"https://www.linkedin.com/company/{slug}"


# ---------------------------------------------------------------------------
# Discovery Method 1: Apify memo23 LinkedIn People Scraper
# ---------------------------------------------------------------------------
def fetch_decision_makers_apify(company_linkedin_url: str, max_items: int = 10) -> list[dict]:
    """
    Calls Apify actor memo23~linkedin-company-people-scraper with automatic token pool rotation.
    """
    tokens = get_apify_tokens()
    if not tokens:
        print("   ⚠️ No Apify tokens configured in .env.")
        return []

    payload = {
        "companies": [company_linkedin_url],
        "maxItems": max_items,
        "targetTitles": [
            "HR", "Recruiter", "Talent Acquisition", "Technical Recruiter", "Campus Recruiter",
            "Head of Talent", "People Partner", "CTO", "Founder", "Co-Founder",
            "Head of AI", "Head of Engineering", "Engineering Manager", "Technical Lead"
        ]
    }

    for idx, token in enumerate(tokens, 1):
        url = f"https://api.apify.com/v2/acts/memo23~linkedin-company-people-scraper/run-sync-get-dataset-items?token={token}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        try:
            print(f"   [Apify Token #{idx}/{len(tokens)}] Querying memo23 on {company_linkedin_url}...")
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
            if resp.status_code in (200, 201):
                raw = resp.json()
                cleaned = []
                for it in raw:
                    full = (it.get("fullName") or it.get("name") or "").strip()
                    first = (it.get("firstName") or "").strip()
                    last = (it.get("lastName") or "").strip()
                    if not full and first:
                        full = f"{first} {last}".strip()

                    hl = (it.get("jobTitle") or it.get("headline") or it.get("title") or "").strip()
                    url_p = it.get("profileUrl") or it.get("linkedinUrl") or ""

                    if full and url_p and "linkedin.com/in/" in url_p:
                        cleaned.append({
                            "name": full,
                            "headline": hl,
                            "linkedin_url": url_p.split("?")[0],
                            "source": f"apify_token_{idx}"
                        })

                if cleaned:
                    print(f"   ✅ Apify delivered {len(cleaned)} decision-makers!")
                    return cleaned
            elif resp.status_code in (401, 403, 429):
                print(f"   ⚠️ Apify Token #{idx} rate limit/quota error ({resp.status_code}). Trying next token...")
                continue
            else:
                print(f"   ⚠️ Apify Token #{idx} notice ({resp.status_code})")
        except Exception as e:
            print(f"   ⚠️ Apify Token #{idx} error: {e}")
            continue

    return []


# ---------------------------------------------------------------------------
# Discovery Method 2: Real Browser LinkedIn Search (Playwright & Chrome Profile)
# ---------------------------------------------------------------------------
def _search_linkedin_on_page(page, company_name: str, clean_comp: str, search_queries: list[str], max_items: int = 10) -> list[dict]:
    discovered = []
    seen_urls = set()

    for q in search_queries:
        if len(discovered) >= max_items:
            break

        search_url = f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(q)}"
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
            try:
                page.wait_for_selector("div.search-results-container, ul.reusable-search__entity-result-list, div[data-view-name*='search'], main", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(1500)

            # Check if login required
            if "login" in page.url.lower() or "authwall" in page.url.lower():
                print("   ⚠️ LinkedIn authentication required in chrome_profile. (Run: python main.py apply --login)", flush=True)
                break

            # Extract profile cards
            items = page.locator("li.reusable-search__result-container, div[data-view-name='search-entity-result-universal-template']").all()
            if not items:
                # Fallback generic anchor query
                anchors = page.locator("a[href*='/in/']").all()
                for a in anchors:
                    href = (a.get_attribute("href") or "").split("?")[0]
                    if not href.startswith("http"):
                        href = "https://www.linkedin.com" + href
                    if any(bad in href for bad in ["/opportunities/", "/jobs/", "/company/", "/school/"]):
                        continue
                    if not re.search(r"linkedin\.com/in/[a-zA-Z0-9_\-]+/?$", href):
                        continue
                    txt = a.inner_text().strip().split("\n")[0]
                    txt = re.sub(r"[•·]\s*\d+.*$", "", txt).strip()
                    if href and href not in seen_urls and len(txt) > 2 and "linkedin" not in txt.lower():
                        seen_urls.add(href)
                        discovered.append({
                            "name": txt,
                            "headline": f"Decision Maker at {company_name}",
                            "linkedin_url": href,
                            "source": "browser_linkedin"
                        })
                        if len(discovered) >= max_items:
                            break
                continue

            for it in items:
                try:
                    a = it.locator("a[href*='/in/']").first
                    if a.count() == 0:
                        continue
                    href = (a.get_attribute("href") or "").split("?")[0]
                    if not href.startswith("http"):
                        href = "https://www.linkedin.com" + href
                    if any(bad in href for bad in ["/opportunities/", "/jobs/", "/company/", "/school/"]):
                        continue
                    if not re.search(r"linkedin\.com/in/[a-zA-Z0-9_\-]+/?$", href):
                        continue
                    if href in seen_urls:
                        continue

                    name = a.inner_text().strip().split("\n")[0]
                    name = re.sub(r"[•·]\s*\d+.*$", "", name).strip()

                    sub = it.locator(".entity-result__primary-subtitle").first
                    hl = sub.inner_text().strip() if sub.count() > 0 else f"Professional at {company_name}"

                    if len(name) >= 2 and "linkedin" not in name.lower():
                        seen_urls.add(href)
                        discovered.append({
                            "name": name,
                            "headline": hl,
                            "linkedin_url": href,
                            "source": "browser_linkedin"
                        })
                        if len(discovered) >= max_items:
                            break
                except Exception:
                    pass

            page.wait_for_timeout(1000)

        except Exception as e:
            print(f"   ⚠️ Search error for '{q}': {e}", flush=True)
            continue

    return discovered


def fetch_decision_makers_browser(company_name: str, max_items: int = 10, page=None) -> list[dict]:
    """
    Searches LinkedIn People directly using the user's logged-in real Chrome session.
    100% Free, Zero Apify credits, Zero API costs!
    """
    print(f"   [Browser Search] Searching LinkedIn People for '{company_name}' in logged-in Chrome...", flush=True)
    clean_comp = re.sub(r"\b(Inc|LLC|Ltd|Pvt|Technologies|Labs|Solutions|Corporation|Corp)\b", "", company_name, flags=re.I).strip()
    if not clean_comp:
        clean_comp = company_name

    search_queries = [
        f"{clean_comp} HR",
        f"{clean_comp} Recruiter",
        f"{clean_comp} Engineering Manager",
        f"{clean_comp} CTO"
    ]

    if page is not None:
        leads = _search_linkedin_on_page(page, company_name, clean_comp, search_queries, max_items)
        print(f"   ✅ Browser search discovered {len(leads)} LinkedIn profile(s)!", flush=True)
        return leads

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(CHROME_PROFILE_DIR),
                channel="chrome",
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-infobars"
                ],
                ignore_default_args=["--enable-automation"],
                no_viewport=True
            )
            pg = context.pages[0] if context.pages else context.new_page()
            leads = _search_linkedin_on_page(pg, company_name, clean_comp, search_queries, max_items)
            context.close()
            print(f"   ✅ Browser search discovered {len(leads)} LinkedIn profile(s)!", flush=True)
            return leads
    except Exception as e:
        print(f"   ⚠️ Browser search failed ({e}). Falling back to Apify...", flush=True)
        return []


# ---------------------------------------------------------------------------
# Combined Discovery Pipeline
# ---------------------------------------------------------------------------
def get_company_decision_makers(company_name: str, company_linkedin: str, method: str = "auto", max_items: int = 10, browser_page=None) -> list[dict]:
    """
    Retrieves decision makers via Apify, Browser, or Auto waterfall.
    """
    candidates = []

    if method == "apify":
        candidates = fetch_decision_makers_apify(company_linkedin, max_items=max_items)

    elif method == "browser":
        candidates = fetch_decision_makers_browser(company_name, max_items=max_items, page=browser_page)

    else:  # auto
        # Try Browser first (100% free)
        if browser_page is not None or method == "auto":
            candidates = fetch_decision_makers_browser(company_name, max_items=max_items, page=browser_page)

        # If browser found fewer than 2, activate Apify
        if len(candidates) < 2 and company_linkedin:
            print(f"   ℹ️ Browser search found {len(candidates)} (< 2). Calling Apify memo23...", flush=True)
            apify_leads = fetch_decision_makers_apify(company_linkedin, max_items=max_items)
            seen_urls = {c["linkedin_url"] for c in candidates}
            for al in apify_leads:
                if al["linkedin_url"] not in seen_urls:
                    seen_urls.add(al["linkedin_url"])
                    candidates.append(al)

    # Clean and classify
    final_leads = []
    seen = set()
    for c in candidates:
        url = c.get("linkedin_url", "")
        name = c.get("name", "")
        hl = c.get("headline", "")
        if url and url not in seen and len(name) >= 2:
            seen.add(url)
            cat, score = categorize_role(hl)
            final_leads.append({
                "name": name,
                "headline": hl,
                "linkedin_url": url,
                "category": cat,
                "score": score,
                "source": c.get("source", "unknown")
            })

    # Sort by priority: HR first, then Tech Leaders
    final_leads.sort(key=lambda x: x["score"], reverse=True)
    return final_leads[:max_items]


# ---------------------------------------------------------------------------
# Export to CSV & HTML Dashboard
# ---------------------------------------------------------------------------
def export_leads_report():
    """Exports all discovered LinkedIn leads to CSV and HTML report."""
    conn = sqlite3.connect(DB_PATH)
    init_leads_table(conn)
    rows = conn.execute("""
        SELECT company_name, job_title, person_name, headline, category, linkedin_url, connection_note, discovered_at, email
        FROM linkedin_leads
        ORDER BY id DESC
    """).fetchall()
    conn.close()

    if not rows:
        print("ℹ️ No LinkedIn leads to export yet.")
        return

    # 1. Export CSV
    with open(CSV_EXPORT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Company", "Matched Role", "Name", "Headline / Title", "Category", "LinkedIn URL", "Corporate Email", "Personalized Connection Note", "Discovered At"])
        for r in rows:
            writer.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[8] or "", r[6], r[7]])
    print(f"📄 Exported {len(rows)} leads to CSV: {CSV_EXPORT_PATH.resolve()}")

    # 2. Export Interactive HTML
    html_cards = []
    for comp, j_title, name, hl, cat, l_url, note, disc_at, email in rows:
        badge_color = "#3b82f6" if "HR" in cat else "#10b981"
        escaped_note = note.replace("'", "\\'").replace('"', '&quot;')
        
        if email:
            email_cell = f"""
            <div style="display: flex; align-items: center; gap: 6px;">
                <span style="font-family: monospace; font-size: 13px; color: #1e293b;">{email}</span>
                <button onclick="navigator.clipboard.writeText('{email}'); this.innerText='Copied!'; setTimeout(()=>this.innerText='Copy', 2000);" style="background: #e2e8f0; border: none; padding: 3px 8px; border-radius: 4px; cursor: pointer; font-size: 11px; font-weight: 600;">
                    Copy
                </button>
            </div>
            """
        else:
            email_cell = """<span style="color: #94a3b8; font-size: 12px; font-style: italic;">Connect on LinkedIn</span>"""

        html_cards.append(f"""
        <tr>
            <td style="font-weight: 600;">{comp}</td>
            <td style="color: #64748b;">{j_title or 'Tech Role'}</td>
            <td style="font-weight: 500;">{name}</td>
            <td style="color: #334155; font-size: 13px;">{hl}</td>
            <td><span style="background: {badge_color}15; color: {badge_color}; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: 600;">{cat}</span></td>
            <td>
                <a href="{l_url}" target="_blank" style="background: #0077b5; color: white; padding: 6px 12px; border-radius: 6px; text-decoration: none; font-size: 12px; font-weight: 600; display: inline-flex; align-items: center; gap: 4px;">
                    🔗 View Profile
                </a>
            </td>
            <td>{email_cell}</td>
            <td>
                <button onclick="navigator.clipboard.writeText('{escaped_note}'); this.innerText='Copied!'; setTimeout(()=>this.innerText='Copy Note', 2000);" style="background: #f1f5f9; border: 1px solid #cbd5e1; padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 500;">
                    📋 Copy Note
                </button>
            </td>
        </tr>
        """)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LinkedIn Decision-Makers & Tech Leaders</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #f8fafc; color: #0f172a; margin: 0; padding: 24px; }}
        .container {{ max-width: 1400px; margin: 0 auto; background: white; border-radius: 12px; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1); padding: 24px; }}
        header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #e2e8f0; padding-bottom: 16px; margin-bottom: 20px; }}
        h1 {{ font-size: 22px; margin: 0; color: #0f172a; }}
        .stats {{ display: flex; gap: 16px; }}
        .stat-box {{ background: #f1f5f9; padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; color: #475569; }}
        input.search {{ width: 100%; box-sizing: border-box; padding: 10px 14px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 14px; margin-bottom: 16px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
        th {{ background: #f8fafc; text-align: left; padding: 12px 14px; border-bottom: 2px solid #e2e8f0; color: #475569; font-weight: 600; }}
        td {{ padding: 12px 14px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }}
        tr:hover td {{ background: #f8fafc; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>🎯 Decision-Maker & Tech Leader LinkedIn Intelligence</h1>
                <p style="margin: 4px 0 0; color: #64748b; font-size: 13px;">Direct LinkedIn Profiles & Emails of HRs, Recruiters, and Engineering Leaders at Skill-Matched Companies</p>
            </div>
            <div class="stats">
                <div class="stat-box">Total Discovered: {len(rows)}</div>
                <div class="stat-box">Candidate: Sparsh Goyal</div>
            </div>
        </header>

        <input type="text" class="search" id="filterInput" placeholder="🔍 Filter by company, name, role, email, or category..." onkeyup="filterTable()">

        <table id="leadsTable">
            <thead>
                <tr>
                    <th>Company</th>
                    <th>Matched Role</th>
                    <th>Decision-Maker</th>
                    <th>Headline / Role</th>
                    <th>Category</th>
                    <th>LinkedIn Profile</th>
                    <th>Corporate Email</th>
                    <th>Connection Note</th>
                </tr>
            </thead>
            <tbody>
                {''.join(html_cards)}
            </tbody>
        </table>
    </div>

    <script>
        function filterTable() {{
            const val = document.getElementById('filterInput').value.toLowerCase();
            const rows = document.querySelectorAll('#leadsTable tbody tr');
            rows.forEach(r => {{
                r.style.display = r.innerText.toLowerCase().includes(val) ? '' : 'none';
            }});
        }}
    </script>
</body>
</html>
"""
    with open(HTML_REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"📊 Generated Interactive HTML Dashboard: {HTML_REPORT_PATH.resolve()}")


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------
def enrich_skill_matched_companies(limit: int = 5, method: str = "auto", target_post_id: int = 0, target_company: str = ""):
    """
    Main flow: Picks skill-matched companies from DB and discovers their decision makers.
    """
    from src.copilot.applier import is_role_relevant_for_candidate, evaluate_job_resume_fit, load_profile

    profile = load_profile()
    conn = sqlite3.connect(DB_PATH)
    init_leads_table(conn)

    print("\n" + "=" * 65)
    print("🚀 LINKEDIN DECISION-MAKER & TECH LEADER ENRICHMENT ENGINE")
    print("=" * 65)
    print(f"Candidate Profile : {profile['personal']['full_name']} (B.Sc CS Hansraj College, 2026)")
    print(f"Core Skills       : {profile['skills_summary']}")
    print(f"Discovery Method  : {method.upper()} (Apify + Logged-in Chrome)")
    print("=" * 65)

    # 1. Target single company if requested
    if target_company:
        c_name = target_company.strip()
        c_linkedin = find_company_linkedin(c_name)
        c_domain = find_company_domain(c_name)
        print(f"\n🏢 Targeting Company: {c_name}")
        print(f"   Company LinkedIn : {c_linkedin}")
        print(f"   Official Domain  : {c_domain or 'Not found'}")
        leads = get_company_decision_makers(c_name, c_linkedin, method=method, max_items=10)
        save_and_print_leads(conn, post_id=0, company_name=c_name, job_title="Tech Role", leads=leads, domain=c_domain)
        export_leads_report()
        conn.close()
        return

    # 2. Target specific post ID if requested
    if target_post_id > 0:
        row = conn.execute("SELECT post_id, title, apply_url, structured_fields, description_text FROM details WHERE post_id=?", (target_post_id,)).fetchone()
        if not row:
            print(f"❌ Post #{target_post_id} not found in database.")
            conn.close()
            return
        candidates = [row]
    else:
        # Fetch candidate jobs pre-filtered for tech keywords
        query = """
            SELECT d.post_id, d.title, d.apply_url, d.structured_fields, d.description_text
            FROM details d
            LEFT JOIN linkedin_leads l ON d.post_id = l.post_id
            WHERE l.id IS NULL
              AND d.title IS NOT NULL
              AND (
                  lower(d.title) LIKE '%ai%'
                  OR lower(d.title) LIKE '%artificial intelligence%'
                  OR lower(d.title) LIKE '%machine learning%'
                  OR lower(d.title) LIKE '%ml%'
                  OR lower(d.title) LIKE '%python%'
                  OR lower(d.title) LIKE '%software%'
                  OR lower(d.title) LIKE '%sde%'
                  OR lower(d.title) LIKE '%backend%'
                  OR lower(d.title) LIKE '%developer%'
                  OR lower(d.title) LIKE '%data%'
                  OR lower(d.title) LIKE '%full stack%'
                  OR lower(d.title) LIKE '%fullstack%'
                  OR lower(d.title) LIKE '%intern%'
              )
              AND lower(d.title) NOT LIKE '%marketing%'
              AND lower(d.title) NOT LIKE '%sales%'
              AND lower(d.title) NOT LIKE '%business development%'
              AND lower(d.title) NOT LIKE '%telecaller%'
              AND lower(d.title) NOT LIKE '%graphic%'
              AND lower(d.title) NOT LIKE '%content%'
              AND lower(d.title) NOT LIKE '%hr%'
              AND lower(d.title) NOT LIKE '%human resource%'
              AND lower(d.title) NOT LIKE '%finance%'
              AND lower(d.title) NOT LIKE '%accountant%'
            ORDER BY d.post_id DESC
            LIMIT ?
        """
        candidates = conn.execute(query, (max(limit * 2, 15),)).fetchall()

    # 3. Filter strictly for skill-matched tech roles
    matched_targets = []
    print(f"\n🔍 Screening candidate listings for Strict Resume Fit (Target: {limit} companies)...", flush=True)
    for pid, title, apply_url, struct_json, desc in candidates:
        if not is_role_relevant_for_candidate(title):
            continue

        comb_desc = (desc or "")
        if struct_json:
            comb_desc += "\n" + struct_json

        print(f"   Evaluating Post #{pid}: {title[:40]}...", end=" ", flush=True)
        is_fit, reason = evaluate_job_resume_fit(title, comb_desc, profile)
        if is_fit:
            comp_name = ""
            if struct_json:
                try:
                    s_data = json.loads(struct_json)
                    comp_name = s_data.get("Company", "")
                except Exception:
                    pass
            if not comp_name:
                comp_name = title.split(" - ")[0].strip() if " - " in title else title.split(":")[0].strip()

            matched_targets.append({
                "post_id": pid,
                "title": title,
                "apply_url": apply_url or "",
                "company": comp_name,
                "reason": reason
            })
            print(f"✓ [Match: {comp_name}]", flush=True)
            if len(matched_targets) >= limit:
                break
        else:
            print("✗ [Skipped]", flush=True)

    if not matched_targets:
        print("✅ No un-enriched skill-matched jobs remaining in database.", flush=True)
        conn.close()
        return

    print(f"\n🎯 Processing {len(matched_targets)} Skill-Matched Companies:", flush=True)
    total_leads_added = 0

    # Launch single persistent browser session for fast batch discovery
    playwright_inst = None
    browser_context = None
    browser_page = None

    if method in ("auto", "browser"):
        try:
            from playwright.sync_api import sync_playwright
            playwright_inst = sync_playwright().start()
            browser_context = playwright_inst.chromium.launch_persistent_context(
                user_data_dir=str(CHROME_PROFILE_DIR),
                channel="chrome",
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-infobars"
                ],
                ignore_default_args=["--enable-automation"],
                no_viewport=True
            )
            browser_page = browser_context.pages[0] if browser_context.pages else browser_context.new_page()
            print("   🌐 Launched persistent Chrome session for fast batch discovery.", flush=True)
        except Exception as e:
            print(f"   ⚠️ Chrome profile locked or in use ({e}). Falling back to Apify automatically...", flush=True)
            browser_context = None
            browser_page = None

    try:
        for idx, target in enumerate(matched_targets, 1):
            pid = target["post_id"]
            c_name = target["company"]
            j_title = target["title"]

            print(f"\n[{idx}/{len(matched_targets)}] 🏢 COMPANY: {c_name} (Post #{pid})", flush=True)
            print(f"   Matched Role : {j_title}", flush=True)
            print(f"   Match Reason : {target['reason']}", flush=True)

            c_linkedin = find_company_linkedin(c_name, target["apply_url"])
            print(f"   Company LinkedIn : {c_linkedin}", flush=True)

            c_domain = find_company_domain(c_name, target["apply_url"])
            print(f"   Official Domain  : {c_domain or 'Not found'}", flush=True)

            leads = get_company_decision_makers(c_name, c_linkedin, method=method, max_items=8, browser_page=browser_page)
            added = save_and_print_leads(conn, pid, c_name, j_title, leads, domain=c_domain)
            total_leads_added += added
            time.sleep(1.0)
    finally:
        if browser_context:
            try:
                browser_context.close()
            except Exception:
                pass
        if playwright_inst:
            try:
                playwright_inst.stop()
            except Exception:
                pass

    conn.close()
    export_leads_report()

    print("\n" + "=" * 65, flush=True)
    print("🎉 LINKEDIN ENRICHMENT COMPLETED SUCCESSFULLY", flush=True)
    print(f"Total Decision-Makers & Tech Leaders Discovered: {total_leads_added}", flush=True)
    print(f"Reports Generated:", flush=True)
    print(f"  • CSV Report  : {CSV_EXPORT_PATH.resolve()}", flush=True)
    print(f"  • HTML Report : {HTML_REPORT_PATH.resolve()}", flush=True)
    print("=" * 65, flush=True)


def save_and_print_leads(conn: sqlite3.Connection, post_id: int, company_name: str, job_title: str, leads: list[dict], domain: str | None = None) -> int:
    """Saves discovered leads to DB, queues predicted emails in hr_contacts, and prints summary."""
    if not leads:
        print("   ⚠️ No decision-makers discovered for this company.", flush=True)
        return 0

    print(f"\n   📋 Discovered {len(leads)} Decision-Makers for {company_name}:", flush=True)
    added = 0
    queued_emails = 0
    now_str = datetime.now(timezone.utc).isoformat()

    historically_sent = set(
        r[0] for r in conn.execute(
            "SELECT lower(trim(hr_email)) FROM hr_contacts WHERE mail_status='sent'"
        )
    )

    for idx, ld in enumerate(leads, 1):
        name = ld["name"]
        hl = ld["headline"]
        url = ld["linkedin_url"]
        cat = ld["category"]
        note = generate_connection_note(name, hl, company_name, job_title)

        email = None
        if domain:
            email = predict_corporate_email(name, domain)

        icon = "👔 [HR]" if "HR" in cat else "🚀 [Tech]"
        email_str = f" | ✉️ {email}" if email else ""
        print(f"      {idx}. {icon} {name:22} | {hl[:30]:30}{email_str} | {url}", flush=True)

        try:
            conn.execute("""
                INSERT OR IGNORE INTO linkedin_leads (
                    post_id, company_name, job_title, person_name, headline, linkedin_url, category, connection_note, source, discovered_at, email, domain
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (post_id, company_name, job_title, name, hl, url, cat, note, ld["source"], now_str, email, domain))
            added += 1
        except Exception:
            pass

        # If email predicted and domain valid, queue into hr_contacts for Step 5 cold email outreach!
        if email and email.lower().strip() not in historically_sent:
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO hr_contacts (
                        post_id, job_title, company, job_url, hr_email, hr_name, mail_status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """, (post_id, hl, company_name, url, email, name))
                queued_emails += 1
            except Exception:
                pass

    if post_id > 0:
        try:
            conn.execute("UPDATE details SET enrich_status='found' WHERE post_id=?", (post_id,))
        except Exception:
            pass

    conn.commit()
    if queued_emails > 0:
        print(f"   📬 Queued {queued_emails} corporate email(s) into hr_contacts for Step 5 cold mailing.", flush=True)
    return added


def main():
    parser = argparse.ArgumentParser(description="LinkedIn Decision-Maker & Tech Leader Discovery Engine")
    parser.add_argument("--limit", type=int, default=5, help="Number of skill-matched companies to enrich (default: 5)")
    parser.add_argument("--post-id", type=int, default=0, help="Enrich a specific post ID")
    parser.add_argument("--company", type=str, default="", help="Enrich a specific company directly by name")
    parser.add_argument("--method", choices=["auto", "apify", "browser"], default="auto", help="Discovery engine: 'auto' (Browser first + Apify fallback), 'apify', or 'browser'")
    parser.add_argument("--export", action="store_true", help="Open HTML report in browser without running enrichment")
    args = parser.parse_args()

    if args.export:
        export_leads_report()
        if HTML_REPORT_PATH.exists():
            webbrowser.open(str(HTML_REPORT_PATH.resolve()))
        return

    enrich_skill_matched_companies(
        limit=args.limit,
        method=args.method,
        target_post_id=args.post_id,
        target_company=args.company
    )


if __name__ == "__main__":
    main()
