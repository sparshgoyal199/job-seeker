#!/usr/bin/env python3
"""
Cold Email Sender with Sequential Waterfall Account Overflow and Test Mode.

Strategy:
  Sequential Waterfall (Fill-Up):
  1. Sends maximum emails through GMAIL_USER_1 up to capacity limit or until rate limit.
  2. When Account 1 is exhausted, automatically overflows to GMAIL_USER_2.
  3. When Account 2 is exhausted, automatically overflows to GMAIL_USER_3.

Configuration in .env:
  GMAIL_USER_1=account1@gmail.com
  GMAIL_APP_PASSWORD_1=xxxx xxxx xxxx xxxx

  GMAIL_USER_2=account2@gmail.com
  GMAIL_APP_PASSWORD_2=yyyy yyyy yyyy yyyy

  GMAIL_USER_3=account3@gmail.com
  GMAIL_APP_PASSWORD_3=zzzz zzzz zzzz zzzz

  (Optional) MAX_EMAILS_PER_ACCOUNT=50  (Default: 50 emails per account per day)

Usage:
  # 1. Preview in terminal (no emails sent):
  python cold_mailer.py --dry-run

  # 2. Test sending 3 emails to YOUR OWN inbox (verifies all 3 accounts):
  python cold_mailer.py --test-to myemail@gmail.com --limit 3

  # 3. Live sending to real recruiters using Sequential Overflow:
  python cold_mailer.py --max-per-account 50
"""

import argparse
import os
import re
import smtplib
import sqlite3
import sys
import time
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Ensure Windows terminal handles emojis cleanly
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

EMAIL_SUBJECT = "Application for AI/Python Engineer Internship – Sparsh Goyal (Fresher, 2026)"

RESUME_ATTACHMENT_FILENAME = "Sparsh_Goyal_Resume.pdf"
DEFAULT_RESUME_PATHS = [
    Path(r"C:\Users\hp\OneDrive\Desktop\Resume\sparsh_goyal_resume.pdf"),
    Path(r"c:\Users\hp\OneDrive\Desktop\medicine_app\resume.pdf"),
]


def get_resume_path() -> Path | None:
    """Finds the resume PDF file from environment variable or standard paths."""
    env_path = os.getenv("RESUME_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists() and p.is_file():
            return p
    for p in DEFAULT_RESUME_PATHS:
        if p.exists() and p.is_file():
            return p
    return None

PORTAL_EMAIL_BLACKLIST = {
    "internshala.com", "bing.com", "google.com", "facebook.com", "freshers.com",
    "freshersworld.com", "indeed.com", "naukri.com", "linkedin.com", "careerspage.io",
    "wikipedia.org", "wikimedia.org", "youtube.com", "instagram.com", "twitter.com", "x.com",
    "rippling.com", "lever.co", "greenhouse.io", "workday.com", "myworkdayjobs.com",
    "ashbyhq.com", "bamboohr.com", "recruitee.com", "smartrecruiters.com", "taleo.net",
    "keka.com", "darwinbox.com", "breezy.hr", "jobvite.com", "icims.com"
}

# ---------------------------------------------------------------------------
# Email Templates
# ---------------------------------------------------------------------------
EMAIL_BODY_HTML = """\
<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; font-size: 15px; color: #222; line-height: 1.6;">

{test_banner}

<p>Hi {name},</p>

<p>I wanted to reach out to see if your team has any <b>AI Engineer internship opportunity.</b></p>

<p>I'm Sparsh Goyal, a Computer Science graduate from Hansraj College, Delhi University (CGPA: 8.15).
I built <b>KhojAI</b>, a collaborative RAG platform that reduced retrieval memory usage by 4×,
and <b>DhanTrackAI</b>, an MCP server with OAuth 2.1 authorization and conversational expense management.</p>

<p>I'd love to contribute my experience in RAG systems, MCP server, LLM workflows, and backend development to your team.</p>

<p>
  Resume:&nbsp;&nbsp;<b>(Attached: Sparsh_Goyal_Resume.pdf)</b> | <a href="https://drive.google.com/file/d/1-wrlSqEKe1gXYI0X8Zc7wkpz_lhsCH5H/view?usp=sharing">View on Google Drive</a><br>
  GitHub:&nbsp;&nbsp;&nbsp;<a href="https://github.com/sparshgoyal199">GitHub Profile</a><br>
  LinkedIn: <a href="https://linkedin.com/in/sparshgoyal199">LinkedIn Profile</a>
</p>

<p>Would love to connect if there's a potential fit. Thanks for your time!</p>

<p>Best,<br>
<b>Sparsh Goyal</b><br>
9887170974
</p>

</body>
</html>
"""

EMAIL_BODY_PLAIN = """\
{test_banner_plain}
Hi {name},

I wanted to reach out to see if your team has any AI Engineer internship opportunity.

I'm Sparsh Goyal, a Computer Science graduate from Hansraj College, Delhi University (CGPA: 8.15).
I built KhojAI, a collaborative RAG platform that reduced retrieval memory usage by 4x,
and DhanTrackAI, an MCP server with OAuth 2.1 authorization and conversational expense management.

I'd love to contribute my experience in RAG systems, MCP server, LLM workflows, and backend
development to your team.

Resume:   Attached (Sparsh_Goyal_Resume.pdf) | https://drive.google.com/file/d/1-wrlSqEKe1gXYI0X8Zc7wkpz_lhsCH5H/view?usp=sharing
GitHub:   https://github.com/sparshgoyal199
LinkedIn: https://linkedin.com/in/sparshgoyal199

Would love to connect if there's a potential fit. Thanks for your time!

Best,
Sparsh Goyal
9887170974
"""

# ---------------------------------------------------------------------------
# Account Loader
# ---------------------------------------------------------------------------
def load_gmail_accounts() -> list[dict]:
    """Loads all configured Gmail accounts in priority order (1 -> 2 -> 3)."""
    accounts = []

    for i in (1, 2, 3, 4, 5):
        u = os.getenv(f"GMAIL_USER_{i}")
        p = os.getenv(f"GMAIL_APP_PASSWORD_{i}")
        if u and p:
            accounts.append({"user": u.strip(), "password": p.strip(), "id": f"Account {i}"})

    if not accounts:
        u = os.getenv("GMAIL_USER")
        p = os.getenv("GMAIL_APP_PASSWORD")
        if u and p:
            accounts.append({"user": u.strip(), "password": p.strip(), "id": "Account 1 (Primary)"})

    return accounts


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_message(
    to_email: str,
    hr_name: str,
    sender_email: str,
    test_info: dict | None = None,
    resume_path: Path | str | None = None
) -> MIMEMultipart:
    name = (hr_name or "Hiring Team").strip()
    first_name = name.split()[0] if name != "Hiring Team" else "Hiring Team"

    subject = EMAIL_SUBJECT
    test_banner_html = ""
    test_banner_plain = ""

    if test_info:
        subject = f"[TEST {test_info['index']}/{test_info['total']} via {sender_email}] {EMAIL_SUBJECT}"
        test_banner_html = f"""\
<div style="background: #fef3c7; border: 1px solid #f59e0b; padding: 12px; border-radius: 8px; margin-bottom: 20px; font-size: 13px; color: #92400e;">
  <strong>🧪 TEST PREVIEW EMAIL</strong><br>
  <strong>Sent via:</strong> {sender_email}<br>
  <strong>Intended Recruiter:</strong> {test_info['real_name']} &lt;{test_info['real_email']}&gt;<br>
  <strong>Company:</strong> {test_info['company']}<br>
  <strong>Job:</strong> {test_info['job_title']}
</div>
"""
        test_banner_plain = f"""\
[TEST PREVIEW] Sent via: {sender_email} | Intended For: {test_info['real_name']} <{test_info['real_email']}> at {test_info['company']}
------------------------------------------------------------------------------------------------------------------------
"""

    # RFC-compliant outer mixed container to properly hold both body and PDF attachment
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = f"Sparsh Goyal <{sender_email}>"
    msg["To"]      = to_email
    msg["Reply-To"] = "sparshgoyal199@gmail.com"

    # Inner alternative container for plain text and HTML versions
    msg_body = MIMEMultipart("alternative")
    plain = EMAIL_BODY_PLAIN.format(name=first_name, test_banner_plain=test_banner_plain)
    html_body = EMAIL_BODY_HTML.format(name=first_name, test_banner=test_banner_html)

    msg_body.attach(MIMEText(plain, "plain", "utf-8"))
    msg_body.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(msg_body)

    # Attach resume PDF if available
    r_path = Path(resume_path) if resume_path else get_resume_path()
    if r_path and r_path.exists() and r_path.is_file():
        try:
            pdf_bytes = r_path.read_bytes()
            attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
            attachment.add_header(
                "Content-Disposition",
                "attachment",
                filename=RESUME_ATTACHMENT_FILENAME
            )
            msg.attach(attachment)
        except Exception as e:
            print(f"⚠️ Warning: Could not attach resume from {r_path}: {e}")

    return msg


def send_single_email(account: dict, to_email: str, msg: MIMEMultipart) -> tuple[bool, str, bool, bool]:
    """Sends a single message.
    Returns: (success: bool, error_message: str, is_quota_error: bool, is_auth_error: bool)
    """
    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(account["user"], account["password"])
        server.sendmail(account["user"], to_email, msg.as_string())
        server.quit()
        return True, "OK", False, False
    except smtplib.SMTPAuthenticationError:
        return False, f"Auth Error on {account['user']} (check App Password)", False, True
    except smtplib.SMTPServerDisconnected:
        return False, f"Auth Failed / Bad Credentials on {account['user']} (check App Password & 2-Step Verification)", False, True
    except smtplib.SMTPRecipientsRefused:
        return False, f"Recipient Refused: {to_email}", False, False
    except Exception as e:
        err_msg = str(e)
        err_lower = err_msg.lower()
        is_quota = any(q in err_lower for q in ("quota", "daily user sending quota", "limit exceeded", "550 5.4.5", "421"))
        return False, f"{type(e).__name__}: {err_msg[:120]}", is_quota, False


# ---------------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Cold Email Sender with Waterfall Account Overflow")
    parser.add_argument("--db", default="jobviareferral.db", help="Path to SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="Print email preview without sending anything")
    parser.add_argument("--test-to", type=str, default="", help="Send 2-3 sample test emails to YOUR email address")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of emails to send total")
    parser.add_argument("--max-per-account", type=int, default=int(os.getenv("MAX_EMAILS_PER_ACCOUNT", "200")),
                        help="Maximum emails to send through one account before overflowing to next (default: 200)")
    parser.add_argument("--delay", type=float, default=3.0, help="Polite delay between emails in seconds")
    parser.add_argument("--retry-failed", action="store_true", help="Retry contacts that previously failed")
    parser.add_argument("--resume-path", default="", help="Path to resume PDF to attach (default: auto-detected)")
    args = parser.parse_args()

    # Detect & Verify Resume Attachment
    resume_file = Path(args.resume_path) if args.resume_path else get_resume_path()
    if resume_file and resume_file.exists():
        size_kb = resume_file.stat().st_size / 1024
        print(f"📎 Resume Attachment  : {resume_file.name} ({size_kb:.1f} KB) [{resume_file}]")
    else:
        print("⚠️  Resume Attachment  : None found (emails will be sent without PDF attachment)")

    # Load Accounts
    accounts = load_gmail_accounts()
    if not accounts and not args.dry_run:
        print("❌ ERROR: No Gmail accounts configured in .env.")
        print("\nTo configure accounts in priority order, add to your .env:")
        print("  GMAIL_USER_1=first_account@gmail.com")
        print("  GMAIL_APP_PASSWORD_1=xxxx xxxx xxxx xxxx")
        print("  GMAIL_USER_2=second_account@gmail.com")
        print("  GMAIL_APP_PASSWORD_2=yyyy yyyy yyyy yyyy")
        print("  GMAIL_USER_3=third_account@gmail.com")
        print("  GMAIL_APP_PASSWORD_3=zzzz zzzz zzzz zzzz")
        sys.exit(1)

    conn = sqlite3.connect(args.db)

    # -----------------------------------------------------------------------
    # Permanent Historical De-duplication Guard
    # Never email any recipient who has ever been successfully sent to previously
    # -----------------------------------------------------------------------
    historically_sent = set(
        r[0] for r in conn.execute(
            "SELECT lower(trim(hr_email)) FROM hr_contacts WHERE mail_status='sent'"
        )
    )

    # Automatically reconcile any pending rows whose email was already contacted in past runs
    conn.execute("""
        UPDATE hr_contacts 
        SET mail_status='sent', error_msg='Skipped: Recruiter already contacted historically'
        WHERE mail_status='pending' AND lower(trim(hr_email)) IN (
            SELECT lower(trim(hr_email)) FROM hr_contacts WHERE mail_status='sent'
        )
    """)
    conn.commit()

    # Fetch contacts
    status_filter = "mail_status IN ('pending','failed')" if args.retry_failed else "mail_status='pending'"
    raw_rows = conn.execute(
        f"""SELECT id, post_id, hr_email, hr_name, job_title, company, job_url
            FROM hr_contacts WHERE {status_filter} ORDER BY id"""
    ).fetchall()

    # Ensure no duplicates in batch, no blacklisted domains, and no historically emailed contacts
    rows = []
    seen_in_batch = set(historically_sent)
    for r in raw_rows:
        cid = r[0]
        email_clean = r[2].lower().strip()
        domain = email_clean.split("@")[-1] if "@" in email_clean else ""

        is_bad_domain = any(bad in domain for bad in PORTAL_EMAIL_BLACKLIST)
        if is_bad_domain:
            conn.execute(
                "UPDATE hr_contacts SET mail_status='skipped', error_msg='Invalid / ATS / Generic portal email address' WHERE id=?",
                (cid,)
            )
            continue

        if email_clean not in seen_in_batch:
            seen_in_batch.add(email_clean)
            rows.append(r)
    conn.commit()

    # -----------------------------------------------------------------------
    # Hard Daily Cap Guard (Maximum 200 emails per day)
    # -----------------------------------------------------------------------
    DAILY_LIMIT = 200
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    already_sent_today = conn.execute(
        "SELECT COUNT(*) FROM hr_contacts WHERE mail_status='sent' AND mailed_at LIKE ?",
        (f"{today_prefix}%",)
    ).fetchone()[0]

    if already_sent_today >= DAILY_LIMIT:
        print("\n" + "=" * 65)
        print(f"🛑 DAILY OUTREACH LIMIT REACHED: {already_sent_today}/{DAILY_LIMIT} emails already sent today.")
        print("Outreach is halted for today to protect your Gmail sender reputation.")
        print("Run again tomorrow to resume sending to the next batch of companies!")
        print("=" * 65)
        conn.close()
        return

    remaining_budget = DAILY_LIMIT - already_sent_today
    print(f"\n📊 Today's Outreach Budget: {already_sent_today}/{DAILY_LIMIT} sent today ({remaining_budget} remaining for today).")

    # -----------------------------------------------------------------------
    # TEST MODE (--test-to): Tests sending sample email to specified address
    # -----------------------------------------------------------------------
    if args.test_to:
        limit = args.limit if args.limit > 0 else 1
        if rows:
            test_rows = rows[:limit]
        else:
            sample = conn.execute(
                "SELECT id, post_id, hr_email, hr_name, job_title, company, job_url FROM hr_contacts ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not sample:
                sample = (0, 0, "sample.recruiter@example.com", "Hiring Manager", "AI Engineer Intern", "Sample AI Labs", "")
            test_rows = [sample] * limit

        print("\n" + "=" * 65)
        print("🧪 RUNNING IN TEST MODE")
        print("=" * 65)
        print(f"Target Inbox          : {args.test_to}")
        print(f"Sample Jobs Chosen    : {len(test_rows)}")
        print(f"Configured Senders    : {len(accounts)} account(s)")
        for idx, a in enumerate(accounts, 1):
            print(f"  {idx}. {a['id']}: {a['user']}")
        print("-" * 65)

        sent_ok = 0
        for idx, r in enumerate(test_rows, 1):
            cid, post_id, real_email, hr_name, job_title, company, _ = r
            active_account = accounts[(idx - 1) % len(accounts)]

            test_info = {
                "index": idx,
                "total": len(test_rows),
                "real_email": real_email,
                "real_name": hr_name or "Hiring Team",
                "company": company or "Featured Company",
                "job_title": job_title or "AI/Python Role"
            }

            msg = build_message(args.test_to, hr_name, active_account["user"], test_info, resume_path=resume_file)
            print(f"[{idx}/{len(test_rows)}] Testing {active_account['id']} ({active_account['user']}) ➔ {args.test_to} ...", end=" ", flush=True)

            if args.dry_run:
                print("✓ [DRY RUN]")
                continue

            ok, err, _, _ = send_single_email(active_account, args.test_to, msg)
            if ok:
                print("✅ SENT")
                sent_ok += 1
            else:
                print(f"❌ FAILED: {err}")

            if idx < len(test_rows):
                time.sleep(args.delay)

        print("\n" + "=" * 65)
        print(f"Test batch complete: {sent_ok}/{len(test_rows)} sent successfully to {args.test_to}.")
        print("ℹ️  Your real recruiter contacts in the database remain untouched.")
        print("=" * 65)
        conn.close()
        return

    if not rows:
        print("✅ No pending contacts found to email (all matching contacts were already reached).")
        conn.close()
        return

    # -----------------------------------------------------------------------
    # DRY RUN PREVIEW
    # -----------------------------------------------------------------------
    if args.dry_run:
        limit = args.limit if args.limit > 0 else len(rows)
        preview_rows = rows[:limit]
        max_cap = args.max_per_account

        print("\n" + "=" * 65)
        print("DRY RUN PREVIEW (Waterfall Strategy - No emails sent)")
        print(f"Total Pending Contacts  : {len(rows)}")
        print(f"Configured Accounts     : {len(accounts)}")
        print(f"Max Capacity per Account: {max_cap} emails")
        print("=" * 65)

        curr_acc = 0
        acc_count = 0
        seen_preview = set(historically_sent)

        for idx, r in enumerate(preview_rows, 1):
            cid, post_id, to_email, hr_name, job_title, company, _ = r
            clean_email = to_email.lower().strip()

            if clean_email in seen_preview:
                print(f"[{idx}/{len(preview_rows)}] ➔ {hr_name} <{to_email}> [SKIPPED - Already contacted historically or in batch]")
                continue
            seen_preview.add(clean_email)

            if accounts and acc_count >= max_cap and curr_acc + 1 < len(accounts):
                curr_acc += 1
                acc_count = 0

            sender = accounts[curr_acc]["user"] if accounts else "GMAIL_USER_1"
            acc_name = accounts[curr_acc]["id"] if accounts else "Account 1"

            print(f"[{idx}/{len(preview_rows)}] [{acc_name}: {sender}] ➔ {hr_name} <{to_email}>")
            att_label = f"📎 {RESUME_ATTACHMENT_FILENAME}" if (resume_file and resume_file.exists()) else "⚠️ No PDF attachment"
            print(f"       Company: {company or 'Unknown'} | Job: {(job_title or '')[:40]} | {att_label}")
            acc_count += 1

        print("\n" + "=" * 65)
        print(f"Unique recipients in preview: {len(seen_preview) - len(historically_sent)}")
        print("→ To test live delivery to your own email: python cold_mailer.py --test-to your_email@gmail.com")
        print("=" * 65)
        conn.close()
        return

    # -----------------------------------------------------------------------
    # LIVE WATERFALL SENDING MODE
    # Priority: Fill GMAIL_USER_1 -> Overflow to GMAIL_USER_2 -> Overflow to GMAIL_USER_3
    # -----------------------------------------------------------------------
    effective_limit = min(args.limit, remaining_budget) if args.limit > 0 else remaining_budget
    send_rows = rows[:effective_limit]
    max_cap = args.max_per_account

    print("\n" + "=" * 65)
    print("🚀 LIVE COLD EMAIL SENDING (WATERFALL STRATEGY)")
    print("=" * 65)
    print(f"Contacts in Queue       : {len(rows)} (Sending up to: {len(send_rows)})")
    print(f"Daily Cap Remaining     : {remaining_budget}/200 emails")
    print(f"Max Capacity per Account: {max_cap} emails")
    print("Sending Priority:")
    for idx, a in enumerate(accounts, 1):
        print(f"  {idx}. {a['id']}: {a['user']} (Primary {idx})")
    print("-" * 65)

    sent_ok = 0
    sent_fail = 0
    curr_acc_idx = 0
    sent_on_curr_acc = 0
    seen_recipients = set(historically_sent)

    for idx, r in enumerate(send_rows, 1):
        cid, post_id, to_email, hr_name, job_title, company, _ = r
        clean_email = to_email.lower().strip()

        # Deduplication check: do not send identical email twice to same recruiter ever
        if clean_email in seen_recipients:
            now_str = now_iso()
            active_sender = accounts[curr_acc_idx]["user"] if accounts else "GMAIL_USER_1"
            conn.execute(
                """UPDATE hr_contacts 
                   SET mail_status='sent', mailed_at=?, sender_email=?, error_msg='Merged with historical outreach to same recruiter' 
                   WHERE id=?""",
                (now_str, active_sender, cid)
            )
            conn.commit()
            print(f"[{idx}/{len(send_rows)}] ➔ {to_email} ({hr_name}) ... ⏩ SKIPPED (already emailed this recruiter historically)")
            continue


        # Check if current account reached max capacity
        if sent_on_curr_acc >= max_cap:
            if curr_acc_idx + 1 < len(accounts):
                curr_acc_idx += 1
                sent_on_curr_acc = 0
                print(f"\n🌊 [{accounts[curr_acc_idx - 1]['id']}] reached capacity ({max_cap}/{max_cap}). Overflowing to [{accounts[curr_acc_idx]['id']}: {accounts[curr_acc_idx]['user']}] ...\n")
            else:
                print(f"\n🛑 All {len(accounts)} configured accounts reached their maximum capacity of {max_cap} emails each.")
                print("Stopping outreach for today to protect email sender reputation.")
                break

        active_account = accounts[curr_acc_idx]
        msg = build_message(to_email, hr_name, active_account["user"], resume_path=resume_file)

        print(f"[{idx}/{len(send_rows)}] [{active_account['id']}: {active_account['user']} ({sent_on_curr_acc + 1}/{max_cap})] ➔ {to_email} ({hr_name}) ...", end=" ", flush=True)

        ok, err, is_quota_error, is_auth_error = send_single_email(active_account, to_email, msg)
        now_str = now_iso()

        if ok:
            seen_recipients.add(clean_email)
            conn.execute(
                """UPDATE hr_contacts 
                   SET mail_status='sent', mailed_at=?, sender_email=?, error_msg=NULL 
                   WHERE lower(trim(hr_email))=? AND mail_status='pending'""",
                (now_str, active_account["user"], clean_email)
            )
            conn.commit()
            print("✅ SENT")
            sent_ok += 1
            sent_on_curr_acc += 1
        elif is_auth_error:
            print(f"⚠️ AUTH ERROR on {active_account['user']}")
            if curr_acc_idx + 1 < len(accounts):
                curr_acc_idx += 1
                sent_on_curr_acc = 0
                active_account = accounts[curr_acc_idx]
                print(f"   ↳ Advancing to [{active_account['id']}: {active_account['user']}] ...", end=" ", flush=True)
                msg = build_message(to_email, hr_name, active_account["user"], resume_path=resume_file)
                ok2, err2, _, is_auth2 = send_single_email(active_account, to_email, msg)
                if ok2:
                    seen_recipients.add(clean_email)
                    conn.execute(
                        """UPDATE hr_contacts 
                           SET mail_status='sent', mailed_at=?, sender_email=?, error_msg=NULL 
                           WHERE lower(trim(hr_email))=? AND mail_status='pending'""",
                        (now_str, active_account["user"], clean_email)
                    )
                    conn.commit()
                    print("✅ SENT")
                    sent_ok += 1
                    sent_on_curr_acc += 1
                else:
                    conn.execute(
                        """UPDATE hr_contacts 
                           SET mail_status='failed', sender_email=?, error_msg=? 
                           WHERE id=?""",
                        (active_account["user"], err2, cid)
                    )
                    conn.commit()
                    print(f"❌ FAILED ({err2})")
                    sent_fail += 1
            else:
                conn.execute(
                    """UPDATE hr_contacts 
                       SET mail_status='failed', sender_email=?, error_msg=? 
                       WHERE id=?""",
                    (active_account["user"], err, cid)
                )
                conn.commit()
                print("❌ FAILED: All accounts failed auth.")
                break
        elif is_quota_error:
            print(f"⚠️ QUOTA REACHED on {active_account['user']}")
            if curr_acc_idx + 1 < len(accounts):
                curr_acc_idx += 1
                sent_on_curr_acc = 0
                active_account = accounts[curr_acc_idx]
                print(f"   ↳ Retrying immediately via [{active_account['id']}: {active_account['user']}] ...", end=" ", flush=True)
                msg = build_message(to_email, hr_name, active_account["user"], resume_path=resume_file)
                ok2, err2, _, _ = send_single_email(active_account, to_email, msg)
                if ok2:
                    seen_recipients.add(clean_email)
                    conn.execute(
                        """UPDATE hr_contacts 
                           SET mail_status='sent', mailed_at=?, sender_email=?, error_msg=NULL 
                           WHERE lower(trim(hr_email))=? AND mail_status='pending'""",
                        (now_str, active_account["user"], clean_email)
                    )
                    conn.commit()
                    print("✅ SENT")
                    sent_ok += 1
                    sent_on_curr_acc += 1
                else:
                    conn.execute(
                        """UPDATE hr_contacts 
                           SET mail_status='failed', sender_email=?, error_msg=? 
                           WHERE id=?""",
                        (active_account["user"], err2, cid)
                    )
                    conn.commit()
                    print(f"❌ FAILED ({err2})")
                    sent_fail += 1
            else:
                conn.execute(
                    """UPDATE hr_contacts 
                       SET mail_status='failed', sender_email=?, error_msg=? 
                       WHERE id=?""",
                    (active_account["user"], err, cid)
                )
                conn.commit()
                print("❌ FAILED: All accounts exhausted quota.")
                break
        else:
            conn.execute(
                """UPDATE hr_contacts 
                   SET mail_status='failed', sender_email=?, error_msg=? 
                   WHERE id=?""",
                (active_account["user"], err, cid)
            )
            conn.commit()
            print(f"❌ FAILED ({err})")
            sent_fail += 1

        if idx < len(send_rows):
            time.sleep(args.delay)

    print("\n" + "=" * 65)
    print("OUTREACH RUN COMPLETE (WATERFALL)")
    print(f"  Successfully Sent : {sent_ok}")
    print(f"  Failed            : {sent_fail}")

    totals = conn.execute("SELECT mail_status, count(*) FROM hr_contacts GROUP BY mail_status").fetchall()
    print("\nDatabase Status:")
    for status, count in totals:
        print(f"  • {status}: {count}")
    print("=" * 65)

    conn.close()


if __name__ == "__main__":
    main()
