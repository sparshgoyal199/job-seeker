import sqlite3

c = sqlite3.connect("jobviareferral.db")
print("=== details table columns ===")
for r in c.execute("PRAGMA table_info(details)"):
    print(r)

print("\n=== the 5 problem rows ===")
rows = c.execute("""
    SELECT post_id,
           fetch_status,
           length(description_text) AS dt_len,
           length(description_html) AS dh_len,
           length(structured_fields) AS sf_len,
           substr(title, 1, 60) AS title
    FROM details
    WHERE post_id IN (4443, 4446, 4454, 4482, 4494)
""").fetchall()
for r in rows:
    print(r)

print("\n=== count of all rows by description size ===")
for r in c.execute("""
    SELECT
        CASE
            WHEN description_text IS NULL OR length(description_text) = 0 THEN 'empty'
            WHEN length(description_text) < 200 THEN 'short'
            WHEN length(description_text) < 1000 THEN 'medium'
            ELSE 'long'
        END AS bucket,
        COUNT(*) AS n
    FROM details
    WHERE fetch_status = 'ok'
    GROUP BY bucket
    ORDER BY bucket
"""):
    print(r)

print("\n=== url for 4443 ===")
r = c.execute("SELECT url FROM listings WHERE post_id = 4443").fetchone()
print(r[0] if r else "not found")