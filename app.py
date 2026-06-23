from flask import Flask, render_template, request, jsonify, redirect, url_for
from supabase import create_client
import pytesseract
from PIL import Image
import os, re, sys
from datetime import datetime
from collections import defaultdict

app = Flask(__name__)

# ── SUPABASE ─────────────────────────────────────────────
import os
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://kzbwsaurpemryreqmwaa.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imt6YndzYXVycGVtcnlyZXFtd2FhIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA0OTM0MzgsImV4cCI6MjA5NjA2OTQzOH0.V-4sxaxcrplOArLeVj6rvw6N_F6CtKkfFy1kAEpjuuw")
supabase     = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── TESSERACT ────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TESSERACT_PATH = os.path.join(BASE_DIR, 'tesseract', 'tesseract.exe')
if not os.path.exists(TESSERACT_PATH):
    TESSERACT_PATH = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

# ── CONFIG ───────────────────────────────────────────────
RECEIPTS_DIR      = r'C:\Users\User\iCloudDrive\ExpenseTracker\receipts'
LOG_FILE          = os.path.join(BASE_DIR, 'processed_files.txt')
SUPPORTED_FORMATS = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp')
PRESET_CATEGORIES = ["Food & Dining", "Transport", "Shopping",
                     "Entertainment", "Health & Fitness", "Utilities"]


# ══════════════════════════════════════════════════════════
# OCR & PARSERS (same logic as receipt_reader.py)
# ══════════════════════════════════════════════════════════

def detect_source(text):
    t = text.upper()
    if 'TNGD' in t or ('TNG' in t and 'DUITNOW QR' in t): return 'TNG'
    if 'SHOPEE' in t and ('SHOPEEPAY' in t or 'SHOPEE MARKETPLACE' in t): return 'SHOPEE'
    if 'MAYBANK' in t and 'VISA' in t: return 'MAYBANK_CARD'
    if 'MAYBANK' in t and ('TRANSFER' in t or 'DUITNOW' in t): return 'MAYBANK_TRANSFER'
    if 'DUITNOW' in t and 'MAYBANK' not in t: return 'DUITNOW_GENERIC'
    if 'CIMB' in t: return 'CIMB'
    if 'RHB'  in t: return 'RHB'
    return 'UNKNOWN'

def parse_maybank_card(text):
    amount = None
    m = re.search(r'-?RM\s*(\d+\.?\d*)', text, re.IGNORECASE)
    if m: amount = float(m.group(1))
    recipient = None
    m = re.search(r'SALE\s+([A-Z0-9][A-Z0-9\s\-&]+?)(?:\n|AP DATED|$)', text, re.IGNORECASE)
    if m: recipient = m.group(1).strip()
    date = None
    for pattern in [
        r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4})',
        r'([Tl][l1]\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4})',
        r'DATED\s+(\d{2}/\d{2}/\d{2,4})',
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            date = re.sub(r'^[Tl][l1]', '11', m.group(1).strip())
            break
    return date, recipient, amount

def parse_tng(text):
    amount = None
    m = re.search(r'-?RM\s*(\d+\.?\d*)', text, re.IGNORECASE)
    if m: amount = float(m.group(1))
    recipient = None
    m = re.search(r'Merchant\s+(.+?)(?:\n|Payment|$)', text, re.IGNORECASE)
    if m: recipient = m.group(1).strip()
    if not recipient:
        m = re.search(r'Pay\s+To\s+(.+?)(?:\n|$)', text, re.IGNORECASE)
        if m: recipient = m.group(1).strip()
    date = None
    m = re.search(r'(\d{2}/\d{2}/\d{4})', text)
    if m: date = m.group(1)
    return date, recipient, amount

def parse_shopee(text):
    amount = None
    for pattern in [r'-?RM\s*(\d+\.?\d*)',
                    r'-?[Rr][MmUuWw]\s*(\d+\.?\d*)',
                    r'-(\d+\.\d{2})']:
        m = re.search(pattern, text)
        if m: amount = float(m.group(1)); break
    recipient = None
    m = re.search(r'Pay\s+To\s+\n?\s*(.+?)(?:\n|Order|$)', text, re.IGNORECASE)
    if m: recipient = m.group(1).strip()
    if not recipient:
        m = re.search(r'(Shopee\s+\w+)', text, re.IGNORECASE)
        if m: recipient = m.group(1).strip()
    date = None
    for pattern in [r'(\d{2}-\d{2}-\d{4})',
                    r'(\d{2}/\d{2}/\d{4})',
                    r'(\d{4}-\d{2}-\d{2})']:
        m = re.search(pattern, text)
        if m: date = m.group(1); break
    return date, recipient, amount

def parse_generic(text):
    amount = None
    for pattern in [r'-?RM\s*(\d+\.?\d*)', r'MYR\s*(\d+\.?\d*)',
                    r'Total[:\s]+RM?\s*(\d+\.?\d*)']:
        m = re.search(pattern, text, re.IGNORECASE)
        if m: amount = float(m.group(1)); break
    date = None
    for pattern in [r'(\d{2}/\d{2}/\d{4})', r'(\d{2}-\d{2}-\d{4})',
                    r'(\d{4}-\d{2}-\d{2})',
                    r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4})']:
        m = re.search(pattern, text, re.IGNORECASE)
        if m: date = m.group(1); break
    recipient = None
    for pattern in [r'(?:To|Recipient|Merchant|Pay To)[:\s]+([A-Za-z0-9][A-Za-z0-9\s\-&]+)',
                    r'SALE\s+([A-Z0-9][A-Z0-9\s\-&]+?)(?:\n|$)']:
        m = re.search(pattern, text, re.IGNORECASE)
        if m: recipient = m.group(1).strip().splitlines()[0]; break
    return date, recipient, amount

PARSER_REGISTRY = {
    'MAYBANK_CARD': parse_maybank_card, 'MAYBANK_TRANSFER': parse_generic,
    'TNG': parse_tng, 'SHOPEE': parse_shopee,
    'DUITNOW_GENERIC': parse_generic, 'CIMB': parse_generic,
    'RHB': parse_generic, 'UNKNOWN': parse_generic,
}

def categorize(recipient):
    if not recipient: return "Uncategorized"
    r = recipient.upper()
    rules = {
        "Food & Dining":    ['GRABFOOD','FOODPANDA','MAMAK','MCDONALDS','KFC',
                             'SUBWAY','STARBUCKS','TEALIVE','CHATIME',
                             'RESTAURANT','GRABPAY','JUICE','CAFE','BAKERY','PIZZA','BURGER'],
        "Transport":        ['MYRAPID','TOUCHNGO','PARKING','TOLL','GRAB',
                             'PETRONAS','SHELL','PETRON','BHP','CALTEX'],
        "Shopping":         ['SHOPEE','LAZADA','AMAZON','AEON','IKEA',
                             'UNIQLO','ZARA','GUARDIAN','WATSONS','MARKETPLACE'],
        "Entertainment":    ['NETFLIX','SPOTIFY','STEAM','YOUTUBE','DISNEY','GSC','TGV'],
        "Health & Fitness": ['FITNESS','GYM','YOGA','CLINIC','PHARMACY','HOSPITAL','ARK'],
        "Utilities":        ['TNB','SYABAS','UNIFI','MAXIS','CELCOM','DIGI','TELEKOM'],
    }
    for category, keywords in rules.items():
        for keyword in keywords:
            if keyword in r: return category
    return "Uncategorized"

def parse_receipt(text):
    source = detect_source(text)
    date, recipient, amount = PARSER_REGISTRY[source](text)
    category = categorize(recipient)
    return date, recipient, amount, category, source

def extract_month(date_str):
    for fmt in ["%d/%m/%Y","%d-%m-%Y","%Y-%m-%d",
                "%d %b %Y","%d %B %Y","%d/%m/%y"]:
        try: return datetime.strptime(date_str.strip(), fmt).strftime("%b %Y")
        except: continue
    return None

def extract_year(date_str):
    for fmt in ["%d/%m/%Y","%d-%m-%Y","%Y-%m-%d",
                "%d %b %Y","%d %B %Y","%d/%m/%y"]:
        try: return datetime.strptime(date_str.strip(), fmt).strftime("%Y")
        except: continue
    return None

def month_sort_key(label):
    try: return datetime.strptime(label, "%b %Y")
    except: return datetime.min

def load_processed_files():
    if not os.path.exists(LOG_FILE): return set()
    with open(LOG_FILE, 'r') as f:
        return set(line.strip() for line in f.readlines())

def mark_as_processed(filename):
    with open(LOG_FILE, 'a') as f:
        f.write(filename + '\n')


# ══════════════════════════════════════════════════════════
# SUPABASE HELPERS
# ══════════════════════════════════════════════════════════

def db_get_all():
    res = supabase.table("expenses").select("*").order("added_on", desc=True).execute()
    return res.data or []

def db_insert(date, recipient, amount, category, source, file_label):
    supabase.table("expenses").insert({
        "date": date, "recipient": recipient,
        "amount": round(float(amount), 2),
        "category": category, "source": source,
        "file": file_label,
        "added_on": datetime.now().strftime("%Y-%m-%d %H:%M")
    }).execute()

def db_update(row_id, field, value):
    supabase.table("expenses").update({field: value}).eq("id", row_id).execute()

def db_delete(row_id):
    supabase.table("expenses").delete().eq("id", row_id).execute()

def build_summary(rows):
    data = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for row in rows:
        if not row.get("amount") or float(row["amount"]) <= 0: continue
        if not row.get("recipient") or row["recipient"] == "Not found": continue
        if not row.get("date"): continue
        month = extract_month(str(row["date"]))
        year  = extract_year(str(row["date"]))
        if month and year:
            data[year][month][row["category"]].append(row)
    return data


# ══════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════

@app.route("/")
def index():
    rows    = db_get_all()
    summary = build_summary(rows)
    years   = sorted(summary.keys(), reverse=True)
    return render_template("index.html",
                           summary=summary,
                           years=years,
                           month_sort_key=month_sort_key)

@app.route("/transactions")
def transactions():
    rows     = db_get_all()
    month    = request.args.get("month")
    category = request.args.get("category")
    if month:
        rows = [r for r in rows if extract_month(str(r.get("date",""))) == month]
    if category:
        rows = [r for r in rows if r.get("category") == category]
    months     = sorted({extract_month(str(r["date"])) for r in db_get_all()
                         if extract_month(str(r.get("date","")))}, key=month_sort_key, reverse=True)
    categories = sorted({r["category"] for r in db_get_all() if r.get("category")})
    return render_template("transactions.html",
                           rows=rows, months=months,
                           categories=categories,
                           selected_month=month,
                           selected_category=category)

@app.route("/add", methods=["GET","POST"])
def add():
    message = None
    if request.method == "POST":
        date      = request.form.get("date","").strip()
        recipient = request.form.get("recipient","").strip()
        amount    = request.form.get("amount","").strip()
        category  = request.form.get("category","").strip()
        custom    = request.form.get("custom_category","").strip()
        if category == "custom" and custom:
            category = custom
        if not date or not recipient or not amount:
            message = "error:Please fill in all fields."
        else:
            try:
                db_insert(date, recipient, float(amount),
                          category, "MANUAL", "manual_entry")
                message = "success:Expense saved successfully!"
            except Exception as e:
                message = f"error:Failed to save: {e}"
    return render_template("add.html",
                           categories=PRESET_CATEGORIES,
                           message=message)

@app.route("/process")
def process_page():
    return render_template("process.html")

@app.route("/process/run", methods=["POST"])
def process_run():
    if not os.path.exists(RECEIPTS_DIR):
        return jsonify({"results": [], "summary": "Receipts folder not found."})

    processed = load_processed_files()
    all_files  = [f for f in os.listdir(RECEIPTS_DIR)
                  if f.lower().endswith(SUPPORTED_FORMATS)]
    new_files  = [f for f in all_files if f not in processed]

    if not new_files:
        return jsonify({"results": [],
                        "summary": f"No new receipts found. ({len(all_files)} already processed)"})

    results = []
    for filename in new_files:
        full_path = os.path.join(RECEIPTS_DIR, filename)
        try:
            img  = Image.open(full_path)
            text = pytesseract.image_to_string(img)
            date, recipient, amount, category, source = parse_receipt(text)
            db_insert(date or "Not found",
                      recipient or "Not found",
                      amount or 0,
                      category, source, filename)
            mark_as_processed(filename)
            results.append({
                "file": filename, "status": "saved",
                "recipient": recipient, "amount": amount,
                "date": date, "category": category,
                "source": source
            })
        except Exception as e:
            results.append({"file": filename, "status": "failed", "error": str(e)})

    saved  = sum(1 for r in results if r["status"] == "saved")
    failed = sum(1 for r in results if r["status"] == "failed")
    return jsonify({
        "results": results,
        "summary": f"Done. {saved} saved, {failed} failed."
    })

@app.route("/edit/<int:row_id>", methods=["GET","POST"])
def edit(row_id):
    rows = db_get_all()
    row  = next((r for r in rows if r["id"] == row_id), None)
    if not row:
        return redirect(url_for("transactions"))
    message = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete":
            db_delete(row_id)
            return redirect(url_for("transactions"))
        fields = ["date","recipient","amount","category"]
        for field in fields:
            val = request.form.get(field,"").strip()
            if val:
                if field == "amount":
                    try: val = round(float(val), 2)
                    except: continue
                db_update(row_id, field, val)
        message = "success:Entry updated."
        rows = db_get_all()
        row  = next((r for r in rows if r["id"] == row_id), None)
    return render_template("edit.html", row=row,
                           categories=PRESET_CATEGORIES,
                           message=message)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
