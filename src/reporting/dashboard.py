#!/usr/bin/env python3
"""
Dashboard Generator and Exporter for Sparsh Goyal's Job Intelligence System.

Generates an interactive dashboard (matched_jobs.html) and CSV (matched_jobs.csv)
that clearly distinguishes:
  1. Jobs with Recruiter Emails (Automated Cold Outreach)
  2. Jobs WITHOUT Emails (Manual Application Required via ATS / Portal Link)

Usage:
    python export_matches.py             # Generates HTML and CSV
    python export_matches.py --open      # Generates and opens in browser
"""

import argparse
import csv
import html
import json
import sqlite3
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

# Ensure Windows terminal handles emojis cleanly
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Job Intelligence Dashboard - Sparsh Goyal</title>
    <style>
        :root {{
            --bg: #090d16;
            --surface: #111827;
            --surface-hover: #1f2937;
            --border: #1f293d;
            --text: #f9fafb;
            --text-muted: #9ca3af;
            --primary: #3b82f6;
            --primary-hover: #2563eb;
            --green: #10b981;
            --amber: #f59e0b;
            --purple: #8b5cf6;
            --rose: #f43f5e;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 24px;
            line-height: 1.5;
        }}
        .container {{
            max-width: 1440px;
            margin: 0 auto;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border);
            margin-bottom: 24px;
        }}
        h1 {{
            margin: 0 0 6px 0;
            font-size: 24px;
            font-weight: 700;
            letter-spacing: -0.02em;
        }}
        .subtitle {{
            color: var(--text-muted);
            font-size: 14px;
            margin: 0;
        }}
        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 14px;
            margin-bottom: 24px;
        }}
        .kpi-card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 16px 18px;
        }}
        .kpi-title {{
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-muted);
            margin-bottom: 6px;
            font-weight: 600;
        }}
        .kpi-value {{
            font-size: 26px;
            font-weight: 700;
        }}
        .controls-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
            margin-bottom: 16px;
        }}
        .filter-tabs {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }}
        .tab-btn {{
            background: var(--surface);
            border: 1px solid var(--border);
            color: var(--text-muted);
            padding: 8px 16px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s ease;
        }}
        .tab-btn:hover {{
            background: var(--surface-hover);
            color: var(--text);
        }}
        .tab-btn.active {{
            background: var(--primary);
            color: #ffffff;
            border-color: var(--primary);
        }}
        .search-box {{
            background: var(--surface);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 8px 14px;
            border-radius: 8px;
            font-size: 13px;
            min-width: 260px;
        }}
        .search-box:focus {{
            outline: none;
            border-color: var(--primary);
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 10px;
            overflow: hidden;
        }}
        th, td {{
            padding: 14px 16px;
            text-align: left;
            border-bottom: 1px solid var(--border);
            vertical-align: top;
        }}
        th {{
            background: #0d131f;
            color: var(--text-muted);
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-weight: 700;
        }}
        tr:last-child td {{
            border-bottom: none;
        }}
        tr:hover td {{
            background: rgba(255, 255, 255, 0.015);
        }}
        .badge {{
            display: inline-flex;
            align-items: center;
            gap: 5px;
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.03em;
            white-space: nowrap;
        }}
        .badge-email {{
            background: rgba(139, 92, 246, 0.15);
            color: #c4b5fd;
            border: 1px solid rgba(139, 92, 246, 0.3);
        }}
        .badge-sent {{
            background: rgba(16, 185, 129, 0.15);
            color: #6ee7b7;
            border: 1px solid rgba(16, 185, 129, 0.3);
        }}
        .badge-portal {{
            background: rgba(59, 130, 246, 0.15);
            color: #93c5fd;
            border: 1px solid rgba(59, 130, 246, 0.3);
        }}
        .company-pill {{
            color: var(--primary);
            font-size: 13px;
            font-weight: 600;
        }}
        .job-title {{
            font-size: 14px;
            font-weight: 600;
            color: var(--text);
            margin: 3px 0 0 0;
        }}
        .btn-apply {{
            display: inline-block;
            background: #2563eb;
            color: #ffffff;
            text-decoration: none;
            padding: 7px 14px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
            white-space: nowrap;
            transition: background 0.15s;
        }}
        .btn-apply:hover {{
            background: #1d4ed8;
        }}
        .btn-view {{
            color: var(--text-muted);
            text-decoration: none;
            font-size: 12px;
            display: inline-block;
            margin-top: 5px;
        }}
        .btn-view:hover {{
            color: var(--primary);
        }}
        .email-chip {{
            font-family: monospace;
            font-size: 12px;
            background: #1e293b;
            padding: 3px 6px;
            border-radius: 4px;
            color: #e2e8f0;
            display: inline-block;
            margin-top: 3px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>Job Matches & Cold Outreach Dashboard</h1>
                <p class="subtitle">Candidate: <strong>Sparsh Goyal</strong> (Fresher | AI / Python / Backend) • Generated on {generated_at}</p>
            </div>
            <div>
                <a href="matched_linkedin_leads.html" target="_blank" style="background: #0077b5; color: white; padding: 10px 18px; border-radius: 8px; text-decoration: none; font-size: 14px; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; box-shadow: 0 2px 4px rgba(0,0,0,0.2);">
                    🎯 View LinkedIn Decision-Makers & Leads
                </a>
            </div>
        </div>

        <div class="kpi-grid">
            <div class="kpi-card">
                <div class="kpi-title">Verified Matches (YES)</div>
                <div class="kpi-value" style="color: var(--text);">{total_yes}</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">✉️ Recruiter Email Available</div>
                <div class="kpi-value" style="color: var(--purple);">{email_available_count}</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">👉 Action: Apply on Portal</div>
                <div class="kpi-value" style="color: var(--primary);">{portal_apply_count}</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">📬 Cold Emails Sent</div>
                <div class="kpi-value" style="color: var(--green);">{sent_count}</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">⏳ Emails Pending Approval</div>
                <div class="kpi-value" style="color: var(--amber);">{pending_count}</div>
            </div>
        </div>

        <div class="controls-row">
            <div class="filter-tabs">
                <button class="tab-btn active" onclick="setFilter('all')">All Relevant Jobs ({total_yes})</button>
                <button class="tab-btn" onclick="setFilter('portal')">👉 Action Needed: Apply on Portal ({portal_apply_count})</button>
                <button class="tab-btn" onclick="setFilter('email')">✉️ Cold Email Outreach ({email_available_count})</button>
                <button class="tab-btn" onclick="setFilter('sent')">✅ Emails Sent ({sent_count})</button>
            </div>
            <input type="text" class="search-box" id="searchBox" placeholder="🔍 Search company, role, city..." onkeyup="filterRows()">
        </div>

        <table>
            <thead>
                <tr>
                    <th style="width: 130px;">Channel</th>
                    <th>Company & Role</th>
                    <th style="width: 220px;">Location / Experience</th>
                    <th style="width: 320px;">Recruiter / Outreach Status</th>
                    <th style="width: 170px;">Action</th>
                </tr>
            </thead>
            <tbody id="jobsTable">
                {table_rows}
            </tbody>
        </table>
    </div>

    <script>
        let currentTab = 'all';

        function setFilter(type) {{
            currentTab = type;
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            event.target.classList.add('active');
            filterRows();
        }}

        function filterRows() {{
            const query = document.getElementById('searchBox').value.toLowerCase();
            const rows = document.querySelectorAll('#jobsTable tr');

            rows.forEach(row => {{
                const category = row.getAttribute('data-category');
                const isSent = row.getAttribute('data-sent') === 'true';
                const text = row.innerText.toLowerCase();

                let tabMatch = false;
                if (currentTab === 'all') tabMatch = true;
                else if (currentTab === 'portal') tabMatch = (category === 'portal');
                else if (currentTab === 'email') tabMatch = (category === 'email');
                else if (currentTab === 'sent') tabMatch = isSent;

                const searchMatch = query === '' || text.includes(query);

                row.style.display = (tabMatch && searchMatch) ? '' : 'none';
            }});
        }}
    </script>
</body>
</html>
"""


def generate_dashboard(db_path: Path, open_browser: bool = False):
    conn = sqlite3.connect(str(db_path))

    query = """
    SELECT 
        d.post_id, d.title, d.url, d.apply_url, d.verdict,
        json_extract(d.structured_fields, '$.Company') as company,
        json_extract(d.structured_fields, '$.Location') as location,
        json_extract(d.structured_fields, '$."Experience Required"') as exp,
        d.published_on,
        GROUP_CONCAT(c.hr_email, ' || ') as emails,
        GROUP_CONCAT(c.hr_name, ' || ') as names,
        GROUP_CONCAT(c.mail_status, ' || ') as statuses,
        GROUP_CONCAT(c.mailed_at, ' || ') as mailed_dates
    FROM details d
    LEFT JOIN hr_contacts c ON d.post_id = c.post_id
    WHERE d.verdict = 'yes'
    GROUP BY d.post_id
    ORDER BY CASE WHEN emails IS NOT NULL THEN 1 ELSE 2 END, d.post_id DESC
    """
    rows = conn.execute(query).fetchall()

    total_yes = len(rows)
    email_available_count = sum(1 for r in rows if r[9])
    portal_apply_count = total_yes - email_available_count

    # Count overall sent vs pending from hr_contacts
    counts = dict(conn.execute("SELECT mail_status, count(*) FROM hr_contacts GROUP BY mail_status").fetchall())
    sent_count = counts.get("sent", 0)
    pending_count = counts.get("pending", 0)

    # Export CSV
    csv_path = Path("matched_jobs.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Post ID", "Company", "Job Title", "Location", "Experience",
            "Outreach Channel", "Recruiter Emails", "Mail Status",
            "Portal Apply URL", "Job Listing URL"
        ])
        for r in rows:
            post_id, title, url, apply_url, _, company, loc, exp, _, emails, _, statuses, _ = r
            channel = "Cold Email" if emails else "Manual Portal"
            writer.writerow([
                post_id, company or "Unknown", title, loc or "India / Remote",
                exp or "Fresher", channel, emails or "", statuses or "",
                apply_url or "", url
            ])

    # Build HTML Rows
    table_rows = []
    for r in rows:
        (post_id, title, url, apply_url, _, company, loc, exp, pub_date,
         emails_raw, names_raw, statuses_raw, mailed_dates_raw) = r

        has_email = bool(emails_raw)
        category = "email" if has_email else "portal"

        # Check if sent
        statuses = (statuses_raw or "").split(" || ")
        is_sent = "sent" in statuses

        # Badge
        if is_sent:
            badge_html = '<span class="badge badge-sent">✅ Emailed</span>'
        elif has_email:
            badge_html = '<span class="badge badge-email">✉️ Cold Email</span>'
        else:
            badge_html = '<span class="badge badge-portal">🌐 Manual Portal</span>'

        # Location / Exp
        loc_str = html.escape(loc or "India / Remote")
        exp_str = html.escape(exp or "0–2 Years")

        # Recruiter details
        if has_email:
            emails_list = emails_raw.split(" || ")
            names_list = (names_raw or "").split(" || ")
            contacts_html_parts = []
            for em, nm in zip(emails_list, names_list):
                nm_clean = nm.strip() if nm.strip() and nm.strip() != "Hiring Team" else ""
                nm_prefix = f"<strong>{html.escape(nm_clean)}</strong>: " if nm_clean else ""
                contacts_html_parts.append(f'<div>{nm_prefix}<span class="email-chip">{html.escape(em.strip())}</span></div>')
            recruiter_col = "".join(contacts_html_parts)
            if is_sent:
                recruiter_col += '<div style="font-size: 11px; color: var(--green); margin-top: 4px;">Sent via Gmail SMTP</div>'
            else:
                recruiter_col += '<div style="font-size: 11px; color: var(--amber); margin-top: 4px;">Pending Automated Send</div>'
        else:
            recruiter_col = '<span style="color: var(--text-muted); font-size: 13px;">No direct email in listing</span>'

        # Action column
        target_apply = apply_url or url
        if not has_email:
            action_col = f"""
            <a href="{html.escape(target_apply)}" target="_blank" class="btn-apply">🚀 Apply on Portal ↗</a><br>
            <a href="{html.escape(url)}" target="_blank" class="btn-view">View Post Details</a>
            """
        else:
            action_col = f"""
            <span style="font-size: 12px; color: var(--text-muted);">Handled by cold mailer</span><br>
            <a href="{html.escape(url)}" target="_blank" class="btn-view">View Post ↗</a>
            """

        table_rows.append(f"""
        <tr data-category="{category}" data-sent="{str(is_sent).lower()}">
            <td>{badge_html}</td>
            <td>
                <div class="company-pill">{html.escape(company or 'Featured Company')}</div>
                <div class="job-title">{html.escape(title)}</div>
            </td>
            <td>
                <div style="font-size: 13px;">📍 {loc_str}</div>
                <div style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">⏳ {exp_str}</div>
            </td>
            <td>{recruiter_col}</td>
            <td>{action_col}</td>
        </tr>
        """)

    html_content = HTML_TEMPLATE.format(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        total_yes=total_yes,
        email_available_count=email_available_count,
        portal_apply_count=portal_apply_count,
        sent_count=sent_count,
        pending_count=pending_count,
        table_rows="\n".join(table_rows)
    )

    html_path = Path("matched_jobs.html")
    html_path.write_text(html_content, encoding="utf-8")

    # Also automatically sync the LinkedIn leads HTML dashboard if leads exist
    leads_html_path = Path("matched_linkedin_leads.html")
    try:
        from src.enrichment.linkedin_leads import export_leads_dashboard
        export_leads_dashboard(str(db_path))
    except Exception as e:
        pass

    print("\n" + "=" * 65)
    print("DASHBOARD GENERATED")
    print("=" * 65)
    print(f"  Total Verified Matches (YES) : {total_yes}")
    print(f"  ✉️  Email Outreach Available : {email_available_count} jobs")
    print(f"  👉 Action: Manual Portal     : {portal_apply_count} jobs")
    print(f"  HTML Dashboard               : {html_path.resolve()}")
    if leads_html_path.exists():
        print(f"  LinkedIn Leads Dashboard     : {leads_html_path.resolve()}")
    print(f"  CSV Export                   : {csv_path.resolve()}")
    print("=" * 65)

    if open_browser:
        webbrowser.open(str(html_path.resolve()))

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="jobviareferral.db")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    generate_dashboard(Path(args.db), args.open)
