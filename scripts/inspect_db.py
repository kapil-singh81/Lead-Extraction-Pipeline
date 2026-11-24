import os
import sqlite3

db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "lead_pipeline.db"))
conn = sqlite3.connect(db_path)
c = conn.cursor()

print("=== CONTACTED LEADS IN DB ===")
for r in c.execute("SELECT id, business_name, status, phone FROM leads WHERE status = 'contacted' ORDER BY id"):
    print(r)

print("\n=== OUTREACH LOG ROWS ===")
for r in c.execute("SELECT id, lead_id, sent_at, channel, outcome, notes FROM outreach_log ORDER BY id"):
    print(r)

print("\n=== STATUS COUNTS IN LEADS ===")
for r in c.execute("SELECT status, COUNT(*) FROM leads GROUP BY status"):
    print(r)

print("\n=== CHANNEL COUNTS IN OUTREACH LOG ===")
for r in c.execute("SELECT channel, COUNT(*) FROM outreach_log GROUP BY channel"):
    print(r)

print("\n=== DISTINCT LEADS IN OUTREACH LOG PER CHANNEL ===")
for r in c.execute("SELECT channel, COUNT(DISTINCT lead_id) FROM outreach_log GROUP BY channel"):
    print(r)
