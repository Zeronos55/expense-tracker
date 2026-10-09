from flask import Flask, render_template, request, jsonify, redirect, url_for
from supabase import create_client
import os, re, hmac, time, calendar, logging
from datetime import datetime, date as date_cls, timedelta
from collections import defaultdict, Counter
from urllib.parse import urlparse
import categorizer
from categorizer import (classify, normalize_merchant, is_label,
                         llm_parse_receipt, UNCATEGORIZED)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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

# Fixed category -> colour slot (validated categorical palette, see
# templates/base.html --cat-* tokens). Colour follows the category, never rank.
CATEGORY_SLOTS = {
    "Food & Dining": "food", "Transport": "transport", "Shopping": "shopping",
    "Entertainment": "fun", "Health & Fitness": "health", "Utilities": "bills",
    "Uncategorized": "none",
}

def category_slot(value):
    return CATEGORY_SLOTS.get(value, "other")

def source_label(value):
    return SOURCE_LABELS.get(value, value)

def category_icon(value):
    return CATEGORY_ICONS.get(value, "🏷️")

def display_name(value):
    return value.title() if value and value.isupper() else value

app.jinja_env.filters["source_label"] = source_label
app.jinja_env.filters["category_icon"] = category_icon
app.jinja_env.filters["display_name"] = display_name
app.jinja_env.filters["category_slot"] = category_slot
app.jinja_env.filters["rm"] = lambda v: f"{float(v or 0):,.2f}"


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

def merchant_rules():
    """Learned rules {merchant_key: {merchant, category}}.

    Seeded from past expenses (latest non-Uncategorized category per
    merchant wins), then overridden by the persisted merchant_rules table
    (user corrections beat cached LLM results)."""
    rules = {}
    for row in db_get_all():
        key, _ = normalize_merchant(row.get("recipient") or "")
        cat = row.get("category")
        if key and cat and cat != UNCATEGORIZED and key != "NOT FOUND":
            rules.setdefault(key, {"merchant": None, "category": cat})
    try:
        saved = supabase.table("merchant_rules").select("*").execute().data or []
    except Exception:
        saved = []  # table not created yet
    for r in sorted(saved, key=lambda r: r.get("source") == "user"):
        rules[r["merchant_key"]] = {"merchant": r.get("merchant"),
                                    "category": r["category"]}
    return rules

def save_merchant_rule(recipient, category, source, merchant=None):
    key, display = normalize_merchant(recipient or "")
    if not key or not category or category == UNCATEGORIZED or key == "NOT FOUND":
        return
    try:
        supabase.table("merchant_rules").upsert({
            "merchant_key": key, "merchant": merchant or display,
            "category": category, "source": source,
            "updated_at": datetime.utcnow().isoformat()}).execute()
    except Exception:
        pass  # learning is best-effort; never block saving an expense

def known_sources():
    """Banking app / source values the user has actually used, most-used first."""
    counts = defaultdict(int)
    for row in db_get_all():
        raw = row.get("source")
        if not raw or raw in ("MANUAL", "UNKNOWN"):
            continue
        counts[source_label(raw)] += 1
    return sorted(counts, key=lambda s: counts[s], reverse=True)

def parse_receipt(text, rules=None):
    """Regex parsers first; Gemini reads the whole text when they miss.

    Returns dict(date, recipient, amount, category, source, method)."""
    source = detect_source(text)
    date, recipient, amount = PARSER_REGISTRY[source](text)
    if is_label(recipient):
        recipient = None  # "Wallet", "Transaction Type", ... are labels, not payees
    result = classify(recipient, rules, PRESET_CATEGORIES)
    category, method = result["category"], result["method"]

    if not recipient or not amount or not date or category == UNCATEGORIZED:
        ai = llm_parse_receipt(text, PRESET_CATEGORIES)
        if ai:
            recipient = recipient or ai["merchant"] or None
            amount = amount or ai["amount"]
            date = date or ai["date"]
            if category == UNCATEGORIZED and recipient:
                # a learned rule for the AI-found merchant still beats the AI
                again = classify(recipient, rules, PRESET_CATEGORIES)
                category, method = again["category"], again["method"]
            if category == UNCATEGORIZED and ai["category"] != UNCATEGORIZED:
                category, method = ai["category"], "llm"
                save_merchant_rule(recipient, category, "llm", ai["merchant"])
    return {"date": date, "recipient": recipient, "amount": amount,
            "category": category, "source": source, "method": method}

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

def db_insert(date, recipient, amount, category, source, file_label,
              details=None, raw_text=None):
    row = {
        "date": date, "recipient": recipient,
        "amount": round(float(amount), 2),
        "category": category, "source": source,
        "file": file_label, "details": details or None,
        "added_on": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    if raw_text:
        try:
            supabase.table("expenses").insert({**row, "raw_text": raw_text}).execute()
            return
        except Exception as e:  # raw_text column not created yet
            logging.warning("Insert with raw_text failed, retrying without: %s", e)
    supabase.table("expenses").insert(row).execute()

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
        category[year] = [{"label": c, "total": round(t, 2), "slot": category_slot(c)}
                           for c, t in sorted(cat_totals.items(),
                                               key=lambda kv: kv[1], reverse=True)]
        annual.append({"label": year, "total": round(year_total, 2)})

    return {"years": years, "monthly": monthly, "category": category,
            "annual": list(reversed(annual))}

# ── Dashboard helpers ────────────────────────────────────
DATE_FORMATS = ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%y"]

def parse_date(value):
    for fmt in DATE_FORMATS:
        try: return datetime.strptime(str(value).strip(), fmt).date()
        except (ValueError, TypeError): continue
    return None

def needs_review(row):
    """Entries the parser couldn't read properly — shown with a badge."""
    try: amount = float(row.get("amount") or 0)
    except (TypeError, ValueError): amount = 0
    return (is_label(row.get("recipient")) or amount <= 0
            or not parse_date(row.get("date"))
            or (row.get("category") or UNCATEGORIZED) == UNCATEGORIZED)

def valid_rows(rows):
    """Rows that can be counted: positive amount, readable date and payee."""
    out = []
    for r in rows:
        d = parse_date(r.get("date"))
        try: amt = float(r.get("amount") or 0)
        except (TypeError, ValueError): continue
        if d and amt > 0 and not is_label(r.get("recipient")):
            out.append({**r, "_date": d, "_amount": amt})
    return out

def shift_month(year, month, delta):
    m = year * 12 + (month - 1) + delta
    return m // 12, m % 12 + 1

def day_label(d, today):
    if d == today: return "Today"
    if d == today - timedelta(days=1): return "Yesterday"
    return d.strftime("%a %-d %b")

def build_month_view(rows, year, month, today=None):
    today = today or date_cls.today()
    good = valid_rows(rows)
    py, pm = shift_month(year, month, -1)
    cur  = [r for r in good if (r["_date"].year, r["_date"].month) == (year, month)]
    prev = [r for r in good if (r["_date"].year, r["_date"].month) == (py, pm)]
    total = sum(r["_amount"] for r in cur)
    # mid-month, compare like with like: last month up to the same day
    is_current = (year, month) == (today.year, today.month)
    prev_cmp = [r for r in prev if not is_current or r["_date"].day <= today.day]
    prev_total = sum(r["_amount"] for r in prev_cmp)

    by_cat = defaultdict(lambda: [0.0, 0])
    for r in cur:
        c = by_cat[r.get("category") or UNCATEGORIZED]
        c[0] += r["_amount"]; c[1] += 1
    categories = [{"label": k, "total": round(v[0], 2), "count": v[1],
                   "share": (v[0] / total * 100) if total else 0,
                   "slot": category_slot(k), "icon": category_icon(k)}
                  for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1][0])]

    def cumulative(rs, y, m, upto=None):
        days = calendar.monthrange(y, m)[1]
        daily = [0.0] * days
        for r in rs: daily[r["_date"].day - 1] += r["_amount"]
        out, run = [], 0.0
        for i, v in enumerate(daily):
            run += v
            out.append(round(run, 2) if upto is None or i < upto else None)
        return out
    series = {"this": cumulative(cur, year, month, today.day if is_current else None),
              "last": cumulative(prev, py, pm),
              "days": calendar.monthrange(year, month)[1]}

    recent = sorted(cur, key=lambda r: (r["_date"], str(r.get("added_on") or "")), reverse=True)
    groups = []
    for r in recent[:30]:
        label = day_label(r["_date"], today)
        if not groups or groups[-1]["label"] != label:
            groups.append({"label": label, "rows": [], "total": 0.0})
        groups[-1]["rows"].append(r); groups[-1]["total"] += r["_amount"]

    return {"year": year, "month": month,
            "label": date_cls(year, month, 1).strftime("%B %Y"),
            "short": date_cls(year, month, 1).strftime("%b"),
            "prev_short": date_cls(py, pm, 1).strftime("%b"),
            "total": total, "prev_total": prev_total, "count": len(cur),
            "to_date": is_current,
            "change": ((total - prev_total) / prev_total * 100) if prev_total else None,
            "categories": categories, "series": series, "groups": groups,
            "insights": build_insights(cur, prev_cmp, rows, is_current)}

def build_insights(cur, prev, all_rows, to_date=False):
    """Plain-Python insight cards (no AI): what changed, who, what's big."""
    cards = []
    review = sum(1 for r in all_rows if needs_review(r))
    if review:
        cards.append({"tone": "warn", "icon": "🔎", "href": "/transactions?review=1",
                      "title": f"{review} entr{'y needs' if review == 1 else 'ies need'} a look",
                      "body": "The receipt reader couldn't tell the merchant or category. Tap to fix."})
    cur_cat, prev_cat = defaultdict(float), defaultdict(float)
    for r in cur:  cur_cat[r.get("category") or UNCATEGORIZED]  += r["_amount"]
    for r in prev: prev_cat[r.get("category") or UNCATEGORIZED] += r["_amount"]
    best = None
    for cat in set(cur_cat) | set(prev_cat):
        if cat == UNCATEGORIZED or prev_cat[cat] < 20: continue
        diff = cur_cat[cat] - prev_cat[cat]
        if best is None or abs(diff) > abs(best[1]): best = (cat, diff)
    if best and abs(best[1]) >= 10:
        cat, diff = best
        pct = diff / prev_cat[cat] * 100
        up = diff > 0
        cards.append({"tone": "up" if up else "down", "icon": category_icon(cat),
                      "href": f"/transactions?category={cat}",
                      "title": f"{cat} is {'up' if up else 'down'} {abs(pct):.0f}%",
                      "body": f"RM {cur_cat[cat]:,.2f} vs RM {prev_cat[cat]:,.2f} {'at this point ' if to_date else ''}last month."})
    if cur:
        counts = Counter(normalize_merchant(r.get("recipient") or "")[1] for r in cur)
        name, n = counts.most_common(1)[0]
        if n >= 2:
            spent = sum(r["_amount"] for r in cur if normalize_merchant(r.get("recipient") or "")[1] == name)
            cards.append({"tone": "info", "icon": "⭐", "href": None,
                          "title": f"Your regular: {name}",
                          "body": f"{n} visits this month, RM {spent:,.2f} in total."})
        big = max(cur, key=lambda r: r["_amount"])
        cards.append({"tone": "info", "icon": "💸", "href": f"/edit/{big['id']}",
                      "title": f"Biggest spend: RM {big['_amount']:,.2f}",
                      "body": f"{display_name(big.get('recipient'))} on {big['_date'].strftime('%-d %b')}."})
    return cards

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
    rows  = db_get_all()
    today = date_cls.today()
    months = sorted({(r["_date"].year, r["_date"].month) for r in valid_rows(rows)})
    try:
        year, month = (int(x) for x in request.args.get("m", "").split("-"))
        date_cls(year, month, 1)
    except ValueError:
        # this month if it has data, else the latest month that does
        year, month = (today.year, today.month) if not months or (today.year, today.month) in months \
            else months[-1]
    view = build_month_view(rows, year, month, today)
    prev_m, next_m = shift_month(year, month, -1), shift_month(year, month, 1)
    has_prev = any(m <= prev_m for m in months)
    has_next = next_m <= (today.year, today.month)
    return render_template("index.html", view=view, has_data=bool(months),
                           prev_link=f"{prev_m[0]}-{prev_m[1]:02d}" if has_prev else None,
                           next_link=f"{next_m[0]}-{next_m[1]:02d}" if has_next else None)

@app.route("/charts")
def charts():
    rows    = db_get_all()
    summary = build_summary(rows)
    data    = build_chart_data(summary)
    return render_template("charts.html", chart_data=data)

@app.route("/transactions")
def transactions():
    all_rows = db_get_all()
    rows     = all_rows
    month    = request.args.get("month")
    category = request.args.get("category")
    review   = request.args.get("review") == "1"
    if review:
        rows = [r for r in rows if needs_review(r)]
    if month:
        rows = [r for r in rows if extract_month(str(r.get("date",""))) == month]
    if category:
        rows = [r for r in rows if r.get("category") == category]
    months     = sorted({extract_month(str(r["date"])) for r in all_rows
                         if extract_month(str(r.get("date","")))}, key=month_sort_key, reverse=True)
    categories = sorted({r["category"] for r in all_rows if r.get("category")})
    for r in rows:
        r["_review"] = needs_review(r)
    return render_template("transactions.html",
                           rows=rows, months=months,
                           review=review,
                           total=sum(float(r.get("amount") or 0) for r in rows),
                           review_count=sum(1 for r in all_rows if needs_review(r)),
                           message=request.args.get("msg"),
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

        p = parse_receipt(text, merchant_rules())
        details = request.form.get("details", "").strip()

        db_insert(
            p["date"]      or "Not found",
            p["recipient"] or "Not found",
            p["amount"]    or 0,
            p["category"], p["source"], file_label, details, raw_text=text
        )

        return jsonify({"status": "success", **p})

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
            elif field == "category" and request.form.get("category") == "custom":
                val = request.form.get("custom_category","").strip()
            else:
                val = request.form.get(field,"").strip()
            if val or field == "details":
                if field == "amount":
                    try: val = round(float(val), 2)
                    except: continue
                db_update(row_id, field, val or None)
        category = request.form.get("category", "").strip()
        if category == "custom":
            category = request.form.get("custom_category", "").strip()
        save_merchant_rule(request.form.get("recipient") or row.get("recipient"),
                           category, "user")
        return redirect(return_to)

    return render_template("edit.html", row=row,
                           categories=PRESET_CATEGORIES,
                           sources=known_sources(),
                           return_to=return_to)

# ── AI status / maintenance ──────────────────────────────
SAMPLE_RECEIPT = ("Transaction Type DuitNow QR\nWallet\nMerchant HEXTAR LUCKIN COFFEE\n"
                  "Amount RM12.90\nReference No 2610018812\n01/10/2026 13:47")

@app.route("/ai-status", methods=["GET", "POST"])
def ai_status():
    test = None
    if request.method == "POST":
        started = time.time()
        text = request.form.get("text", "").strip() or SAMPLE_RECEIPT
        test = {"input": text, "result": llm_parse_receipt(text, PRESET_CATEGORIES),
                "error": categorizer.last_llm_error,
                "seconds": round(time.time() - started, 1)}
    try:
        rule_count = len(supabase.table("merchant_rules").select("merchant_key").execute().data or [])
    except Exception:
        rule_count = None
    try:
        available = categorizer.list_flash_models()
    except Exception as e:
        available, categorizer.last_llm_error = [], f"Listing models failed: {e}"
    return render_template("ai_status.html",
                           key_set=bool(os.environ.get("GEMINI_API_KEY")),
                           model=categorizer.gemini_model(),
                           model_pinned=bool(os.environ.get("GEMINI_MODEL")),
                           available=available,
                           last_error=categorizer.last_llm_error,
                           rule_count=rule_count, sample=SAMPLE_RECEIPT, test=test)

RECATEGORISE_BUDGET_S = 6    # + one worst-case AI call (~22s) < gunicorn's 30s

@app.route("/recategorise", methods=["POST"])
def recategorise():
    """Retry auto-categorisation for Uncategorized rows that have a real
    merchant (or saved OCR text). One AI call per distinct merchant."""
    rows = [r for r in db_get_all()
            if (r.get("category") or UNCATEGORIZED) == UNCATEGORIZED
            and (not is_label(r.get("recipient")) or r.get("raw_text"))]
    categorizer.last_llm_error = None
    rules, done, fixed, started = merchant_rules(), {}, 0, time.time()
    stopped_early = False
    for r in rows:
        if time.time() - started > RECATEGORISE_BUDGET_S:
            stopped_early = True
            break
        if r.get("raw_text") and is_label(r.get("recipient")):
            p = parse_receipt(r["raw_text"], rules)
            updates = {k: v for k, v in (("recipient", p["recipient"]), ("amount", p["amount"]),
                                         ("date", p["date"]), ("category", p["category"])) if v}
        else:
            key = normalize_merchant(r["recipient"])[0]
            if key not in done:
                res = classify(r["recipient"], rules, PRESET_CATEGORIES, use_llm=True)
                if res["method"] == "llm":
                    save_merchant_rule(r["recipient"], res["category"], "llm", res["merchant"])
                    rules[key] = {"merchant": res["merchant"], "category": res["category"]}
                done[key] = res["category"]
            updates = {"category": done[key]}
        if updates.get("category", UNCATEGORIZED) != UNCATEGORIZED:
            supabase.table("expenses").update(updates).eq("id", r["id"]).execute()
            fixed += 1
    left = len(rows) - fixed
    msg = f"Categorised {fixed} of {len(rows)} entries."
    if categorizer.last_llm_error:
        msg += f" AI error: {categorizer.last_llm_error}"
    elif stopped_early:
        msg += " Tap again to continue."
    elif left:
        msg += f" {left} still need a manual pick."
    return redirect(url_for("transactions", review=1, msg=msg))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
