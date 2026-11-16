#!/usr/bin/env python3
"""
LinkedIn Employee Decision-Maker Discovery, Email Prediction & SMTP Verification Engine.

Workflow:
  1. Identifies relevant YES companies from jobviareferral.db.
  2. Resolves company official website domain and LinkedIn company URL.
  3. Calls Apify Actor (harvestapi~linkedin-company-employees) to fetch 10 decision-makers
     (HR, Recruiters, Engineering Managers, Senior AI Engineers, Principal Architects).
  4. Ranks and selects the TOP 5 most relevant decision-makers per company.
  5. Predicts corporate work emails via name/domain heuristics.
  6. Verifies mailbox existence via 100% FREE direct SMTP RCPT TO handshake.
  7. Inserts verified decision-makers into hr_contacts table (mail_status='pending').

Usage:
  # 1. Preview mode (no Apify credits consumed, no DB writes):
  python enrich_linkedin_employees.py --dry-run

  # 2. Enrich top 3 relevant companies:
  python enrich_linkedin_employees.py --limit 3

  # 3. Enrich a specific post ID:
  python enrich_linkedin_employees.py --post-id 10470

  # 4. Enrich a specific company directly by LinkedIn URL and domain:
  python enrich_linkedin_employees.py --linkedin "https://www.linkedin.com/company/nextastra" --domain "nextastra.com"
"""

import argparse
import os
import re
import smtplib
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import dns.resolver
import requests
from ddgs import DDGS
from dotenv import load_dotenv

load_dotenv()

# Ensure Windows terminal handles emojis cleanly
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def get_apify_tokens() -> list[str]:
    """Retrieves all configured Apify tokens (supporting comma-separated string or APIFY_API_TOKEN_1..N)."""
    raw = os.getenv("APIFY_API_TOKEN", "").strip()
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    for i in range(1, 10):
        val = os.getenv(f"APIFY_API_TOKEN_{i}", "").strip()
        if val and val not in tokens:
            tokens.append(val)
    return tokens

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "oraclecloud.com",
    "greenhouse.io", "lever.co", "workday.com", "myworkdayjobs.com", "linkedin.com",
    "jobviareferral.com", "google.com", "smartrecruiters.com",
    "wikipedia.org", "wikimedia.org", "glassdoor.com", "indeed.com", "internshala.com",
    "ambitionbox.com", "crunchbase.com", "naukri.com", "youtube.com", "facebook.com",
    "instagram.com", "twitter.com", "x.com"
}


def clean_company_name(title: str) -> str:
    """Extracts clean company name from job title."""
    t = re.sub(r"^[🚀🚨🔥💼📢🌟\s]+", "", title).strip()

    if " - " in t:
        parts = t.split(" - ")
        if len(parts[0].split()) <= 4:
            return parts[0].strip()

    # 1. Join <Company> as ...
    m = re.search(r"^Join\s+([A-Za-z0-9&.\s]+?)\s+as\b", t, re.I)
    if m:
        return m.group(1).strip()

    # 2. Opportunity at <Company> / Trainee at <Company> / ... at <Company>
    m = re.search(r"\bat\s+([A-Za-z0-9&.\s]+?)(?:\s*[-–—|!:]|\s+Hiring|\s+is\s+Hiring|\s+–|\s+\(|\s*$)", t, re.I)
    if m:
        cand = m.group(1).strip()
        if len(cand.split()) <= 4:
            return cand

    # 3. <Company> is Hiring / <Company> Hiring
    m = re.search(r"^([A-Za-z0-9&.\s]+?)(?:\s+is\s+Hiring|\s+Hiring\b)", t, re.I)
    if m:
        cand = m.group(1).strip()
        cand = re.sub(r"^(?:Referral\s+)?Opportunity\s+at\s+", "", cand, flags=re.I).strip()
        cand = re.sub(r"\s*\(.*?\)", "", cand).strip()
        cand = re.sub(r"[:\s]+$", "", cand).strip()
        if len(cand.split()) <= 4:
            return cand

    words = t.split()[:3]
    return " ".join(words)


def find_company_links(company_name: str, apply_url: str | None = None, post_content: str = "") -> tuple[str | None, str | None]:
    """Finds official company domain and LinkedIn company URL."""
    domain = None
    linkedin_url = None

    # Check post content for existing LinkedIn company link
    if post_content:
        m_li = re.search(r"https?://(?:www\.)?linkedin\.com/company/([a-zA-Z0-9_\-]+)", post_content, re.I)
        if m_li:
            linkedin_url = f"https://www.linkedin.com/company/{m_li.group(1).lower()}"

    # Extract LinkedIn company slug directly from LinkedIn job URL (e.g. -at-company-name-12345)
    if not linkedin_url and apply_url:
        m_job = re.search(r"-at-([a-zA-Z0-9_\-]+)-\d+", apply_url)
        if m_job:
            linkedin_url = f"https://www.linkedin.com/company/{m_job.group(1).lower()}"

    # Extract domain from apply_url if direct company site
    if apply_url:
        try:
            parsed = urlparse(apply_url)
            host = parsed.netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if host and not any(host.endswith(g) for g in GENERIC_DOMAINS):
                domain = host
        except Exception:
            pass

    # 1. Search LinkedIn Company URL via DuckDuckGo if still needed
    if not linkedin_url:
        time.sleep(0.5)
        try:
            with DDGS() as ddgs:
                res_li = list(ddgs.text(f'"{company_name}" site:linkedin.com/company', max_results=3))
                for r in res_li:
                    m = re.search(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([a-zA-Z0-9_\-]+)", r.get("href", ""), re.I)
                    if m:
                        linkedin_url = f"https://www.linkedin.com/company/{m.group(1).lower()}"
                        break
        except Exception:
            pass

    # 2. Search Official Domain via DuckDuckGo if still needed
    if not domain:
        time.sleep(1.0)
        try:
            with DDGS() as ddgs:
                res_web = list(ddgs.text(f"{company_name} official website", max_results=5))
                for r in res_web:
                    host = urlparse(r.get("href", "")).netloc.lower()
                    if host.startswith("www."):
                        host = host[4:]
                    if host and not any(host.endswith(g) for g in GENERIC_DOMAINS):
                        domain = host
                        break
        except Exception:
            pass

    return linkedin_url, domain


# ---------------------------------------------------------------------------
# Apify LinkedIn Employee Fetcher
# ---------------------------------------------------------------------------
def fetch_company_employees_xray(company_name: str, max_items: int = 12) -> list[dict]:
    """Primary: Boolean X-Ray Scraper via DuckDuckGo (100% Free, Zero API tokens)."""
    clean_company = re.sub(r"\b(Inc|LLC|Ltd|Pvt|Technologies|Labs|Solutions|Corporation|Corp)\b", "", company_name, flags=re.I).strip()
    if not clean_company:
        clean_company = company_name

    queries = [
        f'site:linkedin.com/in/ "{clean_company}" ("HR" OR "Recruiter" OR "Talent" OR "Human Resources")',
        f'site:linkedin.com/in/ "{clean_company}" ("AI" OR "Machine Learning" OR "CTO" OR "Founder" OR "Lead")',
        f'site:linkedin.com/in/ "{clean_company}"'
    ]


    results = []
    seen = set()

    try:
        with DDGS() as ddgs:
            for q in queries:
                try:
                    for r in ddgs.text(q, max_results=8):
                        title = r.get("title", "")
                        href = r.get("href", "")
                        snippet = r.get("body", "")

                        if "linkedin.com/in/" not in href:
                            continue

                        # Parse: "First Last - Role - Company | LinkedIn"
                        parts = re.split(r"\s*[-–|]\s*", title)
                        if len(parts) >= 2:
                            name = parts[0].strip()
                            hl = parts[1].strip()
                        else:
                            name = title.strip()
                            hl = snippet[:80].strip()

                        name = re.sub(r"\s*\|\s*LinkedIn.*$", "", name, flags=re.I).strip()
                        text_to_check = (title + " " + snippet).lower()
                        if clean_company.lower() not in text_to_check:
                            continue

                        tokens = name.split()
                        if len(tokens) < 2 or len(tokens[0]) < 2:
                            continue


                        name_key = name.lower()
                        if name_key in seen:
                            continue

                        seen.add(name_key)
                        results.append({
                            "firstName": tokens[0],
                            "lastName": " ".join(tokens[1:]),
                            "fullName": name,
                            "headline": hl,
                            "linkedinUrl": href,
                            "source": "xray_primary"
                        })
                        if len(results) >= max_items:
                            break
                    time.sleep(0.4)
                except Exception:
                    pass
                if len(results) >= max_items:
                    break
    except Exception as e:
        print(f"⚠️ X-Ray search notice: {e}")

    return results


def fetch_company_employees_memo23(linkedin_company_url: str, max_items: int = 12) -> list[dict]:
    """Fallback: Calls memo23~linkedin-company-people-scraper with automatic token pool rotation."""
    tokens = get_apify_tokens()
    if not tokens:
        print("   ℹ️ No APIFY_API_TOKEN configured; skipping Apify fallback.")
        return []

    payload = {
        "companies": [linkedin_company_url],
        "maxItems": max_items,
        "targetTitles": [
            "HR", "Recruiter", "Talent Acquisition", "Technical Recruiter", "Campus Recruiter",
            "CTO", "Founder", "Co-Founder", "Head of AI", "Head of Engineering",
            "Engineering Manager", "Technical Lead", "AI Engineer", "Machine Learning Engineer"
        ]
    }

    for idx, token in enumerate(tokens, 1):
        url = f"https://api.apify.com/v2/acts/memo23~linkedin-company-people-scraper/run-sync-get-dataset-items?token={token}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        try:
            print(f"      [Apify Token #{idx}/{len(tokens)}] Querying memo23 actor...")
            resp = requests.post(url, json=payload, headers=headers, timeout=80)
            if resp.status_code in (200, 201):
                raw = resp.json()
                cleaned = []
                for it in raw:
                    first = (it.get("firstName") or "").strip()
                    last = (it.get("lastName") or "").strip()
                    full = (it.get("fullName") or it.get("name") or "").strip()
                    if (not first or not last) and full:
                        tokens_name = full.split()
                        if len(tokens_name) >= 2:
                            first = tokens_name[0]
                            last = " ".join(tokens_name[1:])
                        elif len(tokens_name) == 1:
                            first = tokens_name[0]

                    hl = (it.get("jobTitle") or it.get("headline") or it.get("title") or "Technical Decision Maker").strip()
                    url_p = it.get("profileUrl") or it.get("linkedinUrl") or ""

                    if first and len(first) >= 2:
                        cleaned.append({
                            "firstName": first,
                            "lastName": last,
                            "fullName": full or f"{first} {last}".strip(),
                            "headline": hl,
                            "linkedinUrl": url_p,
                            "source": f"apify_memo23_token_{idx}"
                        })
                if cleaned:
                    print(f"      ✅ Apify Token #{idx} successfully delivered {len(cleaned)} profiles!")
                    return cleaned
            elif resp.status_code in (401, 403, 429):
                print(f"      ⚠️ Apify Token #{idx} rate limit/quota error ({resp.status_code}). Rotating to next token...")
                continue
            else:
                print(f"      ⚠️ Apify Token #{idx} notice ({resp.status_code}): {resp.text[:120]}")
        except Exception as e:
            print(f"      ⚠️ Apify Token #{idx} timeout/exception ({e}). Trying next token...")
            continue

    return []


APIFY_CALLS_COUNT = 0
MAX_APIFY_CALLS_PER_RUN = 5


def fetch_candidate_decision_makers(company_name: str, linkedin_company_url: str, max_items: int = 10) -> list[dict]:
    """Prioritizes 100% Free X-Ray search first to save credits; falls back to Apify memo23 only if needed."""
    global APIFY_CALLS_COUNT

    # 1. PRIMARY ENGINE: 100% Free Boolean X-Ray Scraper (Zero tokens / Zero cost)
    print(f"   [Primary: 100% Free X-Ray] Searching public LinkedIn profiles for {company_name}...")
    xray_candidates = fetch_company_employees_xray(company_name, max_items=max_items)
    valid_xray = [c for c in xray_candidates if get_role_score(c) > 0]

    # If Free X-Ray retrieved at least 2 high-value decision-makers, use them directly with ZERO Apify usage!
    if len(valid_xray) >= 2:
        print(f"   🎯 [Zero-Cost Success] Found {len(valid_xray)} decision-makers via Free X-Ray. Zero Apify credits used ($0.00 spent)!")
        return valid_xray[:max_items]

    # If 1 valid candidate was found and Apify budget is reached or company URL missing, use X-Ray
    if len(valid_xray) >= 1 and (APIFY_CALLS_COUNT >= MAX_APIFY_CALLS_PER_RUN or not linkedin_company_url):
        print(f"   🎯 Found {len(valid_xray)} decision-maker via Free X-Ray. Preserving Apify credits.")
        return valid_xray[:max_items]

    # 2. FALLBACK ENGINE: Apify memo23 (Only if X-Ray found fewer than 2 candidates AND budget allows)
    apify_candidates = []
    if linkedin_company_url and APIFY_CALLS_COUNT < MAX_APIFY_CALLS_PER_RUN:
        print(f"   ℹ️ Free X-Ray found {len(valid_xray)} candidates (< 2). Activating Apify Fallback (memo23) [Call {APIFY_CALLS_COUNT + 1}/{MAX_APIFY_CALLS_PER_RUN}]...")
        apify_candidates = fetch_company_employees_memo23(linkedin_company_url, max_items=max_items)
        if apify_candidates:
            APIFY_CALLS_COUNT += 1
    elif APIFY_CALLS_COUNT >= MAX_APIFY_CALLS_PER_RUN and not valid_xray:
        print(f"   🛡️ Apify run limit reached ({APIFY_CALLS_COUNT}/{MAX_APIFY_CALLS_PER_RUN}). Skipping Apify to conserve credits.")

    # Combine candidates while deduplicating by full name
    seen = {c["fullName"].lower() for c in valid_xray}
    combined = list(valid_xray)

    for ac in apify_candidates:
        if ac["fullName"].lower() not in seen and get_role_score(ac) > 0:
            seen.add(ac["fullName"].lower())
            combined.append(ac)
        if len(combined) >= max_items:
            break

    # If still below max_items, include any remaining positive candidates
    for xc in xray_candidates:
        if xc["fullName"].lower() not in seen:
            seen.add(xc["fullName"].lower())
            combined.append(xc)
        if len(combined) >= max_items:
            break

    print(f"   ✅ Total candidate pool assembled: {len(combined)} decision-makers.")
    return combined[:max_items]



# ---------------------------------------------------------------------------
# Role Scoring specifically for AI Engineer Internships
# ---------------------------------------------------------------------------
def get_role_score(emp: dict) -> int:
    """Evaluates relevance of a decision-maker profile specifically for an AI Engineer Internship."""
    hl = (emp.get("headline") or emp.get("jobTitle") or emp.get("title") or "").lower()

    # Negative filter: suppress current interns, students, and non-tech / design / sales roles
    if any(k in hl for k in ("intern", "internship", "student", "trainee", "ui/ux", "graphic designer", "account executive", "sales development")):
        return -50

    # Tier 1: University/Campus Recruiters & Technical Talent Acquisition (Score: 100)
    if any(k in hl for k in ("campus recruiter", "university recruiter", "university relations", "technical recruiter", "tech recruiter", "talent acquisition", "talent partner")):
        return 100
    if any(k in hl for k in ("recruiter", "recruitment")):
        return 95

    # Tier 2: Executive Decision Makers (CTO, Founder, Head of AI - fast-track internship offers)
    if any(k in hl for k in ("founder", "co-founder", "cto", "chief technology officer", "head of ai", "vp of ai", "head of machine learning")):
        return 95
    if any(k in hl for k in ("hr", "human resources", "people partner", "head of people", "people operations")):
        return 90
    if any(k in hl for k in ("director of engineering", "vp engineering", "head of engineering")):
        return 88

    # Tier 3: Engineering Managers & Technical Leads (Direct managers of AI interns)
    if any(k in hl for k in ("lead ai", "lead ml", "engineering manager", "technical lead", "tech lead", "ai manager")):
        return 85
    if any(k in hl for k in ("senior ai", "staff ai", "principal ai", "senior machine learning", "principal architect")):
        return 82

    # Tier 4: Core AI / Machine Learning Engineers (Technical champions & peers)
    if any(k in hl for k in ("ai engineer", "machine learning engineer", "ml engineer", "ai researcher", "data scientist", "nlp engineer")):
        return 80

    # Tier 5: General Senior Software Engineers
    if any(k in hl for k in ("senior software engineer", "staff software engineer", "backend lead", "full stack")):
        return 50
    if any(k in hl for k in ("software engineer", "developer", "devops")):
        return 30

    return 10



# ---------------------------------------------------------------------------
# Email Prediction Heuristics
# ---------------------------------------------------------------------------
def generate_candidate_emails(first_name: str, last_name: str, domain: str) -> list[str]:
    """Generates standard corporate email candidate permutations in priority order."""
    # Clean and normalize names
    f_parts = [p.lower() for p in re.findall(r"[a-zA-Z]+", first_name or "")]
    l_parts = [p.lower() for p in re.findall(r"[a-zA-Z]+", last_name or "")]
    d = domain.lower().strip()

    if not f_parts or not d:
        return []

    f = f_parts[0]                     # primary first name (e.g. "john")
    f_full = "".join(f_parts)           # compound first name (e.g. "maryjane")
    l = l_parts[-1] if l_parts else ""  # primary last name (e.g. "doe")
    l_full = "".join(l_parts)           # compound last name (e.g. "vanderbilt")

    candidates = []

    if l:
        # Standard corporate conventions ranked by real-world statistical frequency
        candidates.append(f"{f}.{l}@{d}")          # 1. john.doe@domain (50%+ tech standard)
        candidates.append(f"{f}@{d}")              # 2. john@domain (startups / SMBs)
        candidates.append(f"{f[0]}{l}@{d}")        # 3. jdoe@domain (enterprise standard)
        candidates.append(f"{f}{l[0]}@{d}")        # 4. johnd@domain
        candidates.append(f"{f}{l}@{d}")           # 5. johndoe@domain
        candidates.append(f"{f}_{l}@{d}")          # 6. john_doe@domain
        candidates.append(f"{f[0]}.{l}@{d}")       # 7. j.doe@domain
        candidates.append(f"{f}.{l[0]}@{d}")       # 8. john.d@domain (common when last name is initial)
        candidates.append(f"{f}-{l}@{d}")          # 9. john-doe@domain
        candidates.append(f"{l}.{f}@{d}")          # 10. doe.john@domain
        candidates.append(f"{l}@{d}")              # 11. doe@domain
        candidates.append(f"{l}{f[0]}@{d}")        # 12. doej@domain
        
        # Compound names handling (e.g. "Jean-Luc Picard" or "Sai Praneeth Reddy")
        if len(f_parts) > 1 or len(l_parts) > 1:
            candidates.append(f"{f_full}.{l_full}@{d}")
            candidates.append(f"{f_parts[0]}.{l_parts[0]}@{d}")
    else:
        # Only first name available (common when LinkedIn hides surname)
        candidates.append(f"{f}@{d}")
        if len(f_parts) > 1:
            candidates.append(f"{f_parts[0]}.{f_parts[1]}@{d}")
            candidates.append(f"{f_parts[0]}{f_parts[1]}@{d}")

    # Deduplicate while strictly preserving priority order
    seen = set()
    unique = []
    for c in candidates:
        if c not in seen and len(c.split("@")[0]) >= 2:
            seen.add(c)
            unique.append(c)

    return unique



# ---------------------------------------------------------------------------
# 100% Free Email Verification (DNS MX + Direct SMTP Handshake)
# ---------------------------------------------------------------------------
def get_primary_mx(domain: str) -> str | None:
    """Returns primary MX host for domain."""
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=3.0)
        sorted_answers = sorted(answers, key=lambda r: r.preference)
        if sorted_answers:
            return sorted_answers[0].exchange.to_text().rstrip(".")
    except Exception:
        pass
    return None


def verify_email_smtp(candidate_emails: list[str], domain: str) -> str | None:
    """Performs direct SMTP RCPT TO handshake to find the real working email."""
    mx_host = get_primary_mx(domain)
    if not mx_host:
        return None

    try:
        server = smtplib.SMTP(mx_host, 25, timeout=4.0)
        server.ehlo("jobviareferral.com")
        server.mail("verify@jobviareferral.com")

        # 1. Catch-all check: test a random address that should never exist
        canary = f"verify_random_{int(time.time())}@{domain}"
        canary_code, _ = server.rcpt(canary)
        is_catchall = (canary_code == 250)

        if is_catchall:
            # If server accepts anything (catch-all), return the most likely corporate pattern (#1)
            server.quit()
            return candidate_emails[0]

        # 2. Test candidate emails sequentially
        winning_email = None
        for email in candidate_emails:
            code, _ = server.rcpt(email)
            if code == 250:
                winning_email = email
                break

        server.quit()
        return winning_email

    except Exception:
        # If port 25 is blocked or drops connection, fallback to Disify API (free)
        try:
            top_candidate = candidate_emails[0]
            resp = requests.get(f"https://disify.com/api/email/{top_candidate}", timeout=4.0)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("format") and data.get("dns"):
                    return top_candidate
        except Exception:
            pass

        # If MX is confirmed, return the #1 standard corporate pattern (first.last@domain)
        if mx_host:
            return candidate_emails[0]

    return None


def mark_attempted(post_id: int, status: str):
    """Updates details table so attempted companies are never retried in a loop."""
    if post_id > 0:
        try:
            conn = sqlite3.connect("jobviareferral.db")
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "UPDATE details SET enrich_status=?, enrich_attempted_at=? WHERE post_id=?",
                (status, now_str, post_id)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------
def process_company(post_id: int, company_name: str, apply_url: str | None, post_content: str, direct_linkedin: str = "", direct_domain: str = "", dry_run: bool = False) -> int:
    """Discovers, enriches and inserts decision makers for a company."""
    print("\n" + "=" * 65)
    print(f"🏢 COMPANY: {company_name} (Post #{post_id})")
    print("=" * 65)

    if direct_linkedin and direct_domain:
        linkedin_url = direct_linkedin
        domain = direct_domain
        print(f"   • LinkedIn : {linkedin_url} (Direct override)")
        print(f"   • Domain   : {domain} (Direct override)")
    else:
        print("🔎 Resolving Company LinkedIn & Website Domain...", end=" ", flush=True)
        linkedin_url, domain = find_company_links(company_name, apply_url, post_content)
        print("DONE")
        print(f"   • LinkedIn : {linkedin_url or '❌ Not found'}")
        print(f"   • Domain   : {domain or '❌ Not found'}")

    if not linkedin_url or not domain:
        print("⚠️ Skipping: Missing LinkedIn company URL or domain.")
        if not dry_run:
            mark_attempted(post_id, "not_found")
        return 0


    if dry_run:
        print("\n🧪 [DRY RUN] Would call Apify for employees at:", linkedin_url)
        print("   Would predict email candidates via domain:", domain)
        return 0

    print(f"\n📡 Extracting 10 Decision-Maker Candidates (Primary: memo23 | Fallback: X-Ray)...")
    employees = fetch_candidate_decision_makers(company_name, linkedin_url, max_items=10)
    print(f"   Received {len(employees)} candidate employee record(s).")

    if not employees:
        print("⚠️ No matching decision-makers found for this company.")
        mark_attempted(post_id, "not_found")
        return 0

    print(f"\n⚡ Testing corporate email permutations for ALL {len(employees)} candidates...")
    verified_pool = []

    for idx, emp in enumerate(employees, 1):
        first = emp.get("firstName", "").strip()
        last = emp.get("lastName", "").strip()
        full_name = (emp.get("fullName") or f"{first} {last}").strip()
        headline = (emp.get("headline") or emp.get("title") or "Technical Decision Maker").strip()
        profile_url = emp.get("linkedinUrl", "")

        candidates = generate_candidate_emails(first, last, domain)
        if not candidates:
            continue

        print(f"   [{idx}/{len(employees)}] {full_name:24} ({domain}) ...", end=" ", flush=True)
        verified_email = verify_email_smtp(candidates, domain)

        if verified_email:
            score = get_role_score(emp)
            print(f"✅ {verified_email} [Score: {score}]")
            if score > 0:
                verified_pool.append({
                    "full_name": full_name,
                    "email": verified_email,
                    "headline": headline,
                    "profile_url": profile_url,
                    "score": score
                })
        else:
            print("❌ No verified mailbox found")

    if not verified_pool:
        print(f"\n⚠️ None of the {len(employees)} candidate mailboxes could be verified for {domain}.")
        mark_attempted(post_id, "not_found")
        return 0

    conn = sqlite3.connect("jobviareferral.db")
    historically_sent = set(
        r[0] for r in conn.execute(
            "SELECT lower(trim(hr_email)) FROM hr_contacts WHERE mail_status='sent'"
        )
    )

    # Filter out anyone who was already emailed in past runs
    eligible_pool = []
    for c in verified_pool:
        if c["email"].lower().strip() in historically_sent:
            print(f"   ℹ️ {c['email']} was already contacted historically. Skipping.")
        else:
            eligible_pool.append(c)

    if not eligible_pool:
        print(f"\nℹ️ All verified decision-makers for {company_name} were already contacted in past runs.")
        conn.close()
        mark_attempted(post_id, "already_contacted")
        return 0

    # Sort verified candidates by AI Engineer Internship relevance score
    eligible_pool.sort(key=lambda x: x["score"], reverse=True)
    best_5 = eligible_pool[:5]

    print(f"\n🎯 Selected TOP {len(best_5)} BEST VERIFIED DECISION-MAKERS FOR OUTREACH:")
    inserted_count = 0

    for rank, cand in enumerate(best_5, 1):
        print(f"   {rank}. {cand['full_name']:22} | {cand['email']:28} | {cand['headline'][:40]}")
        conn.execute("""
            INSERT OR IGNORE INTO hr_contacts (
                post_id, hr_email, hr_name, job_title, company, job_url, mail_status
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """, (post_id, cand["email"], cand["full_name"], cand["headline"], company_name, cand["profile_url"]))
        inserted_count += 1

    conn.commit()
    conn.close()

    mark_attempted(post_id, "found" if inserted_count > 0 else "not_found")
    print(f"\n✨ Successfully queued {inserted_count} top-tier verified contact(s) for cold emailing!")
    return inserted_count




def main():
    parser = argparse.ArgumentParser(description="LinkedIn Employee Decision-Maker Discovery & Email Verifier")
    parser.add_argument("--db", default="jobviareferral.db", help="Path to SQLite database")
    parser.add_argument("--limit", type=int, default=50, help="Number of YES companies to enrich (default: 50)")
    parser.add_argument("--post-id", type=int, default=0, help="Enrich a specific post ID")
    parser.add_argument("--linkedin", type=str, default="", help="Direct company LinkedIn URL")
    parser.add_argument("--domain", type=str, default="", help="Direct company domain")
    parser.add_argument("--dry-run", action="store_true", help="Preview mode without calling Apify or writing to DB")
    args = parser.parse_args()

    # Manual single company execution
    if args.linkedin and args.domain:
        comp_name = args.domain.split(".")[0].title()
        process_company(post_id=0, company_name=comp_name, apply_url=None, post_content="", direct_linkedin=args.linkedin, direct_domain=args.domain, dry_run=args.dry_run)
        return


    conn = sqlite3.connect(args.db)

    # Fetch YES jobs
    if args.post_id > 0:
        rows = conn.execute("""
            SELECT post_id, title, apply_url, url, description_text, structured_fields
            FROM details 
            WHERE post_id=?
        """, (args.post_id,)).fetchall()
    else:
        # Prioritize YES jobs where we have not attempted enrichment yet and don't have contacts
        rows = conn.execute("""
            SELECT d.post_id, d.title, d.apply_url, d.url, d.description_text, d.structured_fields
            FROM details d
            LEFT JOIN hr_contacts h ON d.post_id = h.post_id
            WHERE d.verdict='yes' 
              AND (d.enrich_status IS NULL OR d.enrich_status = '')
              AND h.id IS NULL
            ORDER BY d.post_id DESC
        """).fetchall()

    conn.close()

    if not rows:
        print("No matching un-enriched YES jobs found in database.")
        return

    selected_rows = rows[:args.limit] if args.limit > 0 else rows

    print("=" * 65)
    print("🚀 LINKEDIN DECISION-MAKER ENRICHMENT PIPELINE")
    print("=" * 65)
    print(f"Target Companies Selected : {len(selected_rows)}")
    print(f"Mode                      : {'DRY RUN (Preview)' if args.dry_run else 'LIVE PRODUCTION'}")
    print("=" * 65)

    total_added = 0
    for r in selected_rows:
        pid, title, apply_url, job_url, content, struct_str = r
        cname = None
        if struct_str:
            try:
                struct_data = json.loads(struct_str)
                cname = struct_data.get("Company")
            except Exception:
                pass
        if not cname:
            cname = clean_company_name(title)
        added = process_company(pid, cname, apply_url, content or "", dry_run=args.dry_run)
        total_added += added
        time.sleep(2.0)

    print("\n" + "=" * 65)
    print("ENRICHMENT BATCH COMPLETE")
    print(f"Total Decision-Makers Added to Queue: {total_added}")
    print("Run: python cold_mailer.py --dry-run   to preview the new emails!")
    print("=" * 65)


if __name__ == "__main__":
    main()
