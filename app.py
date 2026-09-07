from flask import Flask, render_template, request, jsonify, redirect, url_for
from supabase import create_client
import os, re, hmac
from datetime import datetime
from collections import defaultdict
from urllib.parse import urlparse

app = Flask(__name__)

# ── SUPABASE ─────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Set SUPABASE_URL and SUPABASE_KEY environment variables.")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── ACCESS CONTROL ───────────────────────────────────────
# This is a personal app, not a public one — everything except the
# Shortcut endpoint (which has its own secret, below) requires a login.
APP_USERNAME = os.environ.get("APP_USERNAME")
APP_PASSWORD = os.environ.get("APP_PASSWORD")

@app.before_request
def require_login():
    if request.path == "/upload-from-shortcut":
        return  # authenticated separately via SHORTCUT_SECRET
    if not APP_USERNAME or not APP_PASSWORD:
        return ("Server misconfigured: set APP_USERNAME and APP_PASSWORD "
                "environment variables to use this app.", 500)
    auth = request.authorization
    valid = (auth
             and hmac.compare_digest(auth.username or "", APP_USERNAME)
             and hmac.compare_digest(auth.password or "", APP_PASSWORD))
    if not valid:
        return ("Login required.", 401,
                {"WWW-Authenticate": 'Basic realm="Expense Tracker"'})

# ── CONFIG ───────────────────────────────────────────────
PRESET_CATEGORIES = ["Food & Dining", "Transport", "Shopping",
                     "Entertainment", "Health & Fitness", "Utilities"]

SOURCE_LABELS = {
    "TNG": "Touch 'n Go", "SHOPEE": "ShopeePay",
    "MAYBANK_CARD": "Maybank", "MAYBANK_TRANSFER": "Maybank",
    "DUITNOW_GENERIC": "DuitNow QR", "CIMB": "CIMB", "RHB": "RHB",
    "UNKNOWN": "Unknown", "MANUAL": "Manual entry",
}

CATEGORY_ICONS = {
    "Food & Dining": "🍜", "Transport": "🚗", "Shopping": "🛍️",
    "Entertainment": "🎬", "Health & Fitness": "💊", "Utilities": "💡",
    "Uncategorized": "❔",
}

def source_label(value):
    return SOURCE_LABELS.get(value, value)

def category_icon(value):
    return CATEGORY_ICONS.get(value, "🏷️")

def display_name(value):
    return value.title() if value and value.isupper() else value

app.jinja_env.filters["source_label"] = source_label
app.jinja_env.filters["category_icon"] = category_icon
app.jinja_env.filters["display_name"] = display_name


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

def known_categories():
    """Map recipient (upper-cased) -> the last category the user assigned it,
    so a merchant only needs to be corrected once."""
    known = {}
    for row in db_get_all():
        rec = (row.get("recipient") or "").strip().upper()
        cat = row.get("category")
        if rec and rec != "NOT FOUND" and cat and cat != "Uncategorized":
            known.setdefault(rec, cat)
    return known

def known_sources():
    """Banking app / source values the user has actually used, most-used first."""
    counts = defaultdict(int)
    for row in db_get_all():
        raw = row.get("source")
        if not raw or raw in ("MANUAL", "UNKNOWN"):
            continue
        counts[source_label(raw)] += 1
    return sorted(counts, key=lambda s: counts[s], reverse=True)

def categorize(recipient, known=None):
    if not recipient: return "Uncategorized"
    r = recipient.upper()
    if known and r in known:
        return known[r]
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

def parse_receipt(text, known=None):
    source = detect_source(text)
    date, recipient, amount = PARSER_REGISTRY[source](text)
    category = categorize(recipient, known)
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

# ══════════════════════════════════════════════════════════
# SUPABASE HELPERS
# ══════════════════════════════════════════════════════════

def db_get_all():
    res = supabase.table("expenses").select("*").order("added_on", desc=True).execute()
    return res.data or []

def db_insert(date, recipient, amount, category, source, file_label, details=None):
    supabase.table("expenses").insert({
        "date": date, "recipient": recipient,
        "amount": round(float(amount), 2),
        "category": category, "source": source,
        "file": file_label, "details": details or None,
        "added_on": datetime.now().strftime("%Y-%m-%d %H:%M")
    }).execute()

def db_update(row_id, field, value):
    supabase.table("expenses").update({field: value}).eq("id", row_id).execute()

def db_delete(row_id):
    supabase.table("expenses").delete().eq("id", row_id).execute()

def build_chart_data(summary):
    years = sorted(summary.keys(), reverse=True)
    monthly, category, annual = {}, {}, []

    for year in years:
        months = sorted(summary[year].keys(), key=month_sort_key)
        month_points = []
        cat_totals = defaultdict(float)
        year_total = 0.0

        for month in months:
            month_total = 0.0
            month_cats = {}
            for cat, rows in summary[year][month].items():
                amt = sum(r["amount"] for r in rows)
                month_total += amt
                cat_totals[cat] += amt
                month_cats[cat] = round(amt, 2)
            month_points.append({"label": month, "total": round(month_total, 2),
                                  "categories": month_cats})
            year_total += month_total

        monthly[year]  = month_points
        category[year] = [{"label": c, "total": round(t, 2)}
                           for c, t in sorted(cat_totals.items(),
                                               key=lambda kv: kv[1], reverse=True)]
        annual.append({"label": year, "total": round(year_total, 2)})

    return {"years": years, "monthly": monthly, "category": category,
            "annual": list(reversed(annual))}

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

@app.route("/charts")
def charts():
    rows    = db_get_all()
    summary = build_summary(rows)
    data    = build_chart_data(summary)
    return render_template("charts.html", chart_data=data)

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
        date          = request.form.get("date","").strip()
        recipient     = request.form.get("recipient","").strip()
        amount        = request.form.get("amount","").strip()
        category      = request.form.get("category","").strip()
        custom        = request.form.get("custom_category","").strip()
        source        = request.form.get("source","").strip()
        custom_source = request.form.get("custom_source","").strip()
        details       = request.form.get("details","").strip()
        if category == "custom" and custom:
            category = custom
        if source == "custom":
            source = custom_source
        source = source or "MANUAL"
        if not date or not recipient or not amount:
            message = "error:Please fill in all fields."
        else:
            try:
                db_insert(date, recipient, float(amount),
                          category, source, "manual_entry", details)
                message = "success:Expense saved successfully!"
            except Exception as e:
                message = f"error:Failed to save: {e}"
    return render_template("add.html",
                           categories=PRESET_CATEGORIES,
                           sources=known_sources(),
                           message=message)

SHORTCUT_SECRET = os.environ.get("SHORTCUT_SECRET")

@app.route("/upload-from-shortcut", methods=["POST"])
def upload_from_shortcut():
    if not SHORTCUT_SECRET:
        return jsonify({"status": "error",
                         "message": "Server misconfigured: set SHORTCUT_SECRET"}), 500
    supplied = request.headers.get("X-Shortcut-Secret") or request.form.get("secret")
    if not hmac.compare_digest(supplied or "", SHORTCUT_SECRET):
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    try:
        # OCR runs on-device (Shortcuts' "Extract Text from Image"); this
        # endpoint just parses the already-extracted text.
        text       = request.form.get("text", "").strip()
        file_label = "shortcut_text_upload"

        if not text:
            return jsonify({"status": "error", "message": "No text received"}), 400

        date, recipient, amount, category, source = parse_receipt(text, known_categories())
        details = request.form.get("details", "").strip()

        db_insert(
            date      or "Not found",
            recipient or "Not found",
            amount    or 0,
            category, source, file_label, details
        )

        return jsonify({
            "status":    "success",
            "recipient": recipient,
            "amount":    amount,
            "date":      date,
            "category":  category,
            "source":    source,
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/edit/<row_id>", methods=["GET","POST"])
def edit(row_id):
    rows = db_get_all()
    row  = next((r for r in rows if str(r["id"]) == row_id), None)
    if not row:
        return redirect(url_for("transactions"))

    return_to = url_for("transactions")
    parsed = urlparse(request.referrer or "")
    if parsed.path == "/transactions":
        return_to = "/transactions" + (f"?{parsed.query}" if parsed.query else "")

    if request.method == "POST":
        return_to = request.form.get("return_to") or return_to
        action = request.form.get("action")
        if action == "delete":
            db_delete(row_id)
            return redirect(return_to)
        fields = ["date","recipient","amount","category","source","details"]
        for field in fields:
            if field == "source" and request.form.get("source") == "custom":
                val = request.form.get("custom_source","").strip()
            else:
                val = request.form.get(field,"").strip()
            if val or field == "details":
                if field == "amount":
                    try: val = round(float(val), 2)
                    except: continue
                db_update(row_id, field, val or None)
        return redirect(return_to)

    return render_template("edit.html", row=row,
                           categories=PRESET_CATEGORIES,
                           sources=known_sources(),
                           return_to=return_to)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
