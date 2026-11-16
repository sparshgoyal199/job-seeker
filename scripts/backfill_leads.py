import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
from src.enrichment.linkedin_leads import predict_corporate_email, export_leads_report, init_leads_table

conn = sqlite3.connect("jobviareferral.db")
init_leads_table(conn)
cursor = conn.cursor()

leads = cursor.execute("SELECT id, person_name FROM linkedin_leads WHERE company_name LIKE '%Datafoundry%'").fetchall()
for lid, name in leads:
    em = predict_corporate_email(name, "datafoundry.ai")
    cursor.execute("UPDATE linkedin_leads SET domain='datafoundry.ai', email=? WHERE id=?", (em, lid))

# Queue into hr_contacts
rows = cursor.execute("SELECT post_id, job_title, company_name, linkedin_url, email, person_name FROM linkedin_leads WHERE email IS NOT NULL AND email != ''").fetchall()
queued = 0
for post_id, hl, comp, url, email, name in rows:
    cursor.execute("""
        INSERT OR IGNORE INTO hr_contacts (post_id, job_title, company, job_url, hr_email, hr_name, mail_status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
    """, (post_id, hl, comp, url, email, name))
    queued += 1

conn.commit()
conn.close()
export_leads_report()
print(f"Successfully processed {len(leads)} Datafoundry leads and queued {queued} contacts into hr_contacts.")
