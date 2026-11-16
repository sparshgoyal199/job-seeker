#!/usr/bin/env python3
"""
Contact Extractor for Job Intelligence System.

Scans all YES-verdict job descriptions in the database, extracts recruiter /
HR email addresses, attempts to infer the recipient's name from context, and
stores everything in the `hr_contacts` table — ready for cold_mailer.py.

Usage:
    python extract_contacts.py             # Extract from all YES jobs
    python extract_contacts.py --all       # Extract from every job (any verdict)
    python extract_contacts.py --show      # Print contacts table
"""

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')

# Try to pull a proper name from the lines surrounding the email
# e.g. "Contact: Sakshi Bhargava" or "📩 Sakshi.Singh" or "HR: Pranita Ganvir"
NAME_CONTEXT_RE = re.compile(
    r'(?:contact|send|apply|reach|mail|resume|cv|connect|refer|hr|recruiter)'
    r'.{0,60}?([A-Z][a-z]+(?:\s[A-Z][a-z]+){1,3})',
    re.IGNORECASE
)

# Domains that are image CDNs, analytics, or generic noise — never real HR addresses
NOISE_DOMAINS = {
    'example.com', 'sentry.io', 'wixpress.com', 'jobviareferral.com',
    'wordpress.com', 'w3.org', 'schema.org', 'google.com', 'facebook.com',
    'twitter.com', 'linkedin.com', 'youtube.com', 'whatsapp.com',
    'instagram.com', 'gravatar.com', 'amazon.com', 'amazonaws.com',
    'cloudfront.net', 'googleapis.com', 'gstatic.com', 'jsdelivr.net',
    'unpkg.com', 'bootstrapcdn.com', 'fontawesome.com',
}
NOISE_PREFIXES = {
    'noreply', 'no-reply', 'donotreply', 'bounce', 'mailer-daemon',
    'postmaster', 'abuse', 'webmaster',
}

# ---------------------------------------------------------------------------
# DB Schema
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS hr_contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         INTEGER NOT NULL,
    job_title       TEXT,
    company         TEXT,
    job_url         TEXT,
    hr_email        TEXT NOT NULL,
    hr_name         TEXT,
    mail_status     TEXT DEFAULT 'pending',   -- pending | sent | failed | skipped
    mailed_at       TEXT,
    error_msg       TEXT,
    UNIQUE(post_id, hr_email)
);
CREATE INDEX IF NOT EXISTS idx_hr_contacts_status ON hr_contacts(mail_status);
"""

def init_db(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    conn.commit()

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------------------------------------------------------------------------
# Email cleanup helpers
# ---------------------------------------------------------------------------
def is_noise_email(email: str) -> bool:
    email_lower = email.lower()
    domain = email_lower.split("@")[-1] if "@" in email_lower else ""
    prefix = email_lower.split("@")[0]
    if any(email_lower.endswith(d) for d in NOISE_DOMAINS):
        return True
    if any(prefix.startswith(p) for p in NOISE_PREFIXES):
        return True
    if len(email) > 80:
        return True
    if "." not in domain:
        return True
    return False

GENERIC_ALIASES = {
    "hr", "careers", "recruit", "jobs", "talent", "hiring",
    "apply", "admin", "info", "contact", "support", "help", "office"
}

def derive_clean_name(email: str) -> str:
    """Derives a clean human name or 'Hiring Team' from the email address.

    Examples:
      suguna.gelladi@lancesoft.in    -> 'Suguna Gelladi'
      Prakash.Iyer@cred.club         -> 'Prakash Iyer'
      nisarga@innoventes.co          -> 'Nisarga'
      hr@medscred.com                -> 'Hiring Team'
      careers@company.com            -> 'Hiring Team'
    """
    local = email.split("@")[0].strip()
    if local.lower() in GENERIC_ALIASES:
        return "Hiring Team"

    # Dotted or underscored names (e.g. first.last)
    if "." in local or "_" in local:
        parts = [p.capitalize() for p in local.replace("_", ".").split(".") if p.isalpha() and len(p) > 1]
        if parts:
            return " ".join(parts)

    # CamelCase (e.g. PrakashIyer)
    camel_parts = re.findall(r'[A-Z][a-z]+', local)
    if len(camel_parts) >= 2:
        return " ".join(camel_parts)

    # Single human-like word (e.g. sneha, nisarga)
    if local.isalpha() and 2 < len(local) <= 15:
        return local.capitalize()

    return "Hiring Team"

# ---------------------------------------------------------------------------
# Main extraction logic
# ---------------------------------------------------------------------------
def extract_contacts(conn: sqlite3.Connection, verdict_filter: str | None) -> int:
    if verdict_filter:
        rows = conn.execute(
            "SELECT d.post_id, d.title, d.url, d.description_text, sf.value "
            "FROM details d "
            "LEFT JOIN (SELECT post_id, json_extract(structured_fields, '$.Company') as value FROM details) sf "
            "  ON sf.post_id = d.post_id "
            "WHERE d.fetch_status='ok' AND d.verdict=?",
            (verdict_filter,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT d.post_id, d.title, d.url, d.description_text, "
            "json_extract(d.structured_fields, '$.Company') "
            "FROM details d WHERE d.fetch_status='ok'"
        ).fetchall()

    new_contacts = 0
    jobs_with_emails = 0

    for post_id, title, url, desc, company in rows:
        text = desc or ""
        all_emails = list(set(EMAIL_RE.findall(text)))
        clean_emails = [e for e in all_emails if not is_noise_email(e)]

        if not clean_emails:
            continue

        jobs_with_emails += 1
        for email in clean_emails:
            hr_name = derive_clean_name(email)

            try:
                conn.execute(
                    """INSERT INTO hr_contacts
                           (post_id, job_title, company, job_url, hr_email, hr_name)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(post_id, hr_email) DO UPDATE SET
                           hr_name = excluded.hr_name""",
                    (post_id, title, company, url, email, hr_name),
                )
                new_contacts += 1
            except sqlite3.IntegrityError:
                pass

    conn.commit()
    return jobs_with_emails, new_contacts


def show_contacts(conn: sqlite3.Connection):
    rows = conn.execute(
        "SELECT id, post_id, company, hr_email, hr_name, mail_status, job_title "
        "FROM hr_contacts ORDER BY mail_status, post_id"
    ).fetchall()

    totals = {}
    print(f"\n{'ID':>4}  {'Status':>8}  {'HR Email':<40}  {'Name':<20}  Title")
    print("-" * 110)
    for cid, pid, company, email, name, status, title in rows:
        totals[status] = totals.get(status, 0) + 1
        print(f"{cid:>4}  {status:>8}  {email:<40}  {(name or ''):.<20}  {(title or '')[:45]}")

    print("\nSummary:")
    for s, n in totals.items():
        print(f"  {s}: {n}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Extract recruiter emails from job descriptions")
    parser.add_argument("--db", default="jobviareferral.db")
    parser.add_argument("--all", action="store_true", help="Extract from all jobs, not just YES")
    parser.add_argument("--show", action="store_true", help="Print the hr_contacts table")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    init_db(conn)

    if args.show:
        show_contacts(conn)
        conn.close()
        return

    verdict = None if args.all else "yes"
    label = "all jobs" if args.all else "YES jobs only"
    print(f"🔍 Scanning {label} for recruiter emails...")

    jobs_hit, new_total = extract_contacts(conn, verdict)

    total_contacts = conn.execute("SELECT COUNT(*) FROM hr_contacts").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM hr_contacts WHERE mail_status='pending'").fetchone()[0]

    print(f"\n✅ Extraction complete.")
    print(f"   Jobs containing emails  : {jobs_hit}")
    print(f"   Contacts in DB (total)  : {total_contacts}")
    print(f"   Pending (not mailed yet): {pending}")
    print(f"\n→ Run: python cold_mailer.py --dry-run   to preview emails before sending.")
    conn.close()


if __name__ == "__main__":
    main()
