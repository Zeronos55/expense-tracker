import openpyxl
from supabase import create_client
import os

# ── YOUR SUPABASE CREDENTIALS ────────────────────────────
SUPABASE_URL = "https://kzbwsaurpemryreqmwaa.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imt6YndzYXVycGVtcnlyZXFtd2FhIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA0OTM0MzgsImV4cCI6MjA5NjA2OTQzOH0.V-4sxaxcrplOArLeVj6rvw6N_F6CtKkfFy1kAEpjuuw"
EXCEL_FILE   = r'C:\expense_tracker\expenses.xlsx'
# ────────────────────────────────────────────────────────

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def migrate():
    if not os.path.exists(EXCEL_FILE):
        print("Excel file not found.")
        return

    wb = openpyxl.load_workbook(EXCEL_FILE)
    ws = wb["Expenses"]

    rows = list(ws.iter_rows(min_row=2, values_only=True))
    if not rows:
        print("No data found in Excel.")
        return

    print(f"Found {len(rows)} rows to migrate...\n")

    success = 0
    skipped = 0

    for row in rows:
        date      = str(row[0]) if row[0] else None
        recipient = str(row[1]) if row[1] else None
        amount    = float(row[2]) if row[2] else 0
        category  = str(row[3]) if row[3] else None
        source    = str(row[4]) if row[4] else None
        file      = str(row[5]) if row[5] else None
        added_on  = str(row[6]) if row[6] else None

        # Skip empty or junk rows
        if not recipient or recipient == "Not found":
            skipped += 1
            continue
        if amount <= 0:
            skipped += 1
            continue

        data = {
            "date":      date,
            "recipient": recipient,
            "amount":    amount,
            "category":  category,
            "source":    source,
            "file":      file,
            "added_on":  added_on,
        }

        try:
            supabase.table("expenses").insert(data).execute()
            print(f"  Migrated: {recipient} — RM {amount:.2f} ({date})")
            success += 1
        except Exception as e:
            print(f"  FAILED: {recipient} — {e}")

    print(f"\n{'='*40}")
    print(f"Done. {success} migrated, {skipped} skipped.")

migrate()
