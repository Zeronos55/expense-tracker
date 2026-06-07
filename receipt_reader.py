# ExpenseTracker v1.0
# Built: May 2026
# Features: OCR receipt reading, manual entry, monthly summary,
#           fix/delete entries, packaged as .exe
# Next: iOS Shortcut integration, new parsers, dashboard view
import pytesseract
from PIL import Image
import openpyxl
import os
import re
from datetime import datetime
from collections import defaultdict

import sys

# Works both when running as a script and as a packaged .exe
if getattr(sys, 'frozen', False):
    # Running as .exe — Tesseract is in the same folder as the .exe
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # Running as a normal script in IDLE
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TESSERACT_PATH = os.path.join(BASE_DIR, 'tesseract', 'tesseract.exe')

# Fallback to system install if bundled version not found
if not os.path.exists(TESSERACT_PATH):
    TESSERACT_PATH = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

# ── CONFIG ──────────────────────────────────────────────
EXCEL_FILE        = os.path.join(BASE_DIR, 'expenses.xlsx')
RECEIPTS_DIR      = r'C:\Users\User\iCloudDrive\ExpenseTracker\receipts'
LOG_FILE          = os.path.join(BASE_DIR, 'processed_files.txt')
SUPPORTED_FORMATS = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.webp')
PRESET_CATEGORIES = ["Food & Dining", "Transport", "Shopping",
                     "Entertainment", "Health & Fitness", "Utilities"]
# ────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════

def divider(char="=", width=50):
    print(char * width)

def header(title, char="=", width=50):
    divider(char, width)
    print(f"  {title}")
    divider(char, width)

def extract_month(date_str):
    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d",
                "%d %b %Y", "%d %B %Y", "%d/%m/%y"]:
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%b %Y")
        except:
            continue
    return None

def extract_year(date_str):
    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d",
                "%d %b %Y", "%d %B %Y", "%d/%m/%y"]:
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y")
        except:
            continue
    return None

def month_sort_key(label):
    try:    return datetime.strptime(label, "%b %Y")
    except: return datetime.min

def is_valid_date(date_str):
    """Reject obviously fake dates like 40/01/2012 or 29/02/2026"""
    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d",
                "%d %b %Y", "%d %B %Y", "%d/%m/%y"]:
        try:
            datetime.strptime(date_str.strip(), fmt)
            return True
        except:
            continue
    return False

def post_action_menu():
    """After any action, show quick nav instead of just Enter"""
    print("\n" + "-"*50)
    print("  What next?")
    print("  1. Process receipts   2. View summary")
    print("  3. Add manually       4. Fix an entry")
    print("  5. Delete an entry    6. Main menu    7. Exit")
    print("-"*50)
    choice = input("  Choose: ").strip()
    return choice


# ══════════════════════════════════════════════════════════
# OCR & DETECTION
# ══════════════════════════════════════════════════════════

def extract_text(image_path):
    img  = Image.open(image_path)
    return pytesseract.image_to_string(img)

def detect_source(text):
    t = text.upper()
    if 'TNGD' in t or ('TNG' in t and 'DUITNOW QR' in t):
        return 'TNG'
    if 'SHOPEE' in t and ('SHOPEEPAY' in t or 'SHOPEE MARKETPLACE' in t):
        return 'SHOPEE'
    if 'MAYBANK' in t and 'VISA' in t:
        return 'MAYBANK_CARD'
    if 'MAYBANK' in t and ('TRANSFER' in t or 'DUITNOW' in t):
        return 'MAYBANK_TRANSFER'
    if 'DUITNOW' in t and 'MAYBANK' not in t:
        return 'DUITNOW_GENERIC'
    if 'CIMB' in t: return 'CIMB'
    if 'RHB'  in t: return 'RHB'
    return 'UNKNOWN'


# ══════════════════════════════════════════════════════════
# PARSERS
# ══════════════════════════════════════════════════════════

def parse_maybank_card(text):
    amount = None
    m = re.search(r'-?RM\s*(\d+\.?\d*)', text, re.IGNORECASE)
    if m: amount = float(m.group(1))

    recipient = None
    m = re.search(r'SALE\s+([A-Z0-9][A-Z0-9\s\-&]+?)(?:\n|AP DATED|$)',
                  text, re.IGNORECASE)
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
        if m:
            amount = float(m.group(1))
            break

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
        if m:
            date = m.group(1)
            break
    return date, recipient, amount

def parse_generic(text):
    amount = None
    for pattern in [r'-?RM\s*(\d+\.?\d*)', r'MYR\s*(\d+\.?\d*)',
                    r'Total[:\s]+RM?\s*(\d+\.?\d*)']:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            amount = float(m.group(1))
            break

    date = None
    for pattern in [r'(\d{2}/\d{2}/\d{4})', r'(\d{2}-\d{2}-\d{4})',
                    r'(\d{4}-\d{2}-\d{2})',
                    r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4})']:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            date = m.group(1)
            break

    recipient = None
    for pattern in [r'(?:To|Recipient|Merchant|Pay To)[:\s]+([A-Za-z0-9][A-Za-z0-9\s\-&]+)',
                    r'SALE\s+([A-Z0-9][A-Z0-9\s\-&]+?)(?:\n|$)']:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            recipient = m.group(1).strip().splitlines()[0]
            break
    return date, recipient, amount

PARSER_REGISTRY = {
    'MAYBANK_CARD':     parse_maybank_card,
    'MAYBANK_TRANSFER': parse_generic,
    'TNG':              parse_tng,
    'SHOPEE':           parse_shopee,
    'DUITNOW_GENERIC':  parse_generic,
    'CIMB':             parse_generic,
    'RHB':              parse_generic,
    'UNKNOWN':          parse_generic,
}

def parse_receipt(text):
    source = detect_source(text)
    date, recipient, amount = PARSER_REGISTRY[source](text)
    category = categorize(recipient)
    return date, recipient, amount, category, source


# ══════════════════════════════════════════════════════════
# CATEGORIZATION
# ══════════════════════════════════════════════════════════

def categorize(recipient):
    if not recipient: return "Uncategorized"
    r = recipient.upper()
    rules = {
        "Food & Dining":    ['GRABFOOD','FOODPANDA','MAMAK','MCDONALDS','KFC',
                             'SUBWAY','STARBUCKS','TEALIVE','CHATIME',
                             'RESTAURANT','GRABPAY','JUICE','CAFE','BAKERY',
                             'PIZZA','BURGER'],
        "Transport":        ['MYRAPID','TOUCHNGO','PARKING','TOLL','GRAB',
                             'PETRONAS','SHELL','PETRON','BHP','CALTEX'],
        "Shopping":         ['SHOPEE','LAZADA','AMAZON','AEON','IKEA',
                             'UNIQLO','ZARA','GUARDIAN','WATSONS','MARKETPLACE'],
        "Entertainment":    ['NETFLIX','SPOTIFY','STEAM','YOUTUBE',
                             'DISNEY','GSC','TGV'],
        "Health & Fitness": ['FITNESS','GYM','YOGA','CLINIC','PHARMACY',
                             'HOSPITAL','ARK'],
        "Utilities":        ['TNB','SYABAS','UNIFI','MAXIS','CELCOM',
                             'DIGI','TELEKOM'],
    }
    for category, keywords in rules.items():
        for keyword in keywords:
            if keyword in r:
                return category
    return "Uncategorized"


# ══════════════════════════════════════════════════════════
# EXCEL HELPERS
# ══════════════════════════════════════════════════════════

def get_or_create_workbook():
    if os.path.exists(EXCEL_FILE):
        wb = openpyxl.load_workbook(EXCEL_FILE)
        if "Expenses" not in wb.sheetnames:
            ws       = wb.create_sheet("Expenses", 0)
            ws.append(["Date","Recipient","Amount (RM)","Category",
                       "Source","File","Added On"])
            wb.save(EXCEL_FILE)
    else:
        wb       = openpyxl.Workbook()
        ws       = wb.active
        ws.title = "Expenses"
        ws.append(["Date","Recipient","Amount (RM)","Category",
                   "Source","File","Added On"])
        wb.save(EXCEL_FILE)
    return wb

def load_all_rows():
    """Return all expense rows as list of dicts with Excel row number"""
    wb   = get_or_create_workbook()
    ws   = wb["Expenses"]
    rows = []
    for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if row[0] is None and row[1] is None:
            continue
        rows.append({
            "row_num":   i,
            "date":      str(row[0]) if row[0] else "",
            "recipient": str(row[1]) if row[1] else "",
            "amount":    float(row[2]) if row[2] else 0.0,
            "category":  str(row[3]) if row[3] else "",
            "source":    str(row[4]) if row[4] else "",
            "file":      str(row[5]) if row[5] else "",
            "added_on":  str(row[6]) if row[6] else "",
        })
    return rows

def save_to_excel(date, recipient, amount, category, source, file_label):
    wb = get_or_create_workbook()
    ws = wb["Expenses"]
    ws.append([
        date, recipient,
        round(float(amount), 2),
        category, source, file_label,
        datetime.now().strftime("%Y-%m-%d %H:%M")
    ])
    wb.save(EXCEL_FILE)

def update_row_in_excel(row_num, col, value):
    wb = get_or_create_workbook()
    ws = wb["Expenses"]
    ws.cell(row=row_num, column=col).value = value
    wb.save(EXCEL_FILE)

def delete_row_in_excel(row_num):
    wb = get_or_create_workbook()
    ws = wb["Expenses"]
    ws.delete_rows(row_num)
    wb.save(EXCEL_FILE)


# ══════════════════════════════════════════════════════════
# TRACKING
# ══════════════════════════════════════════════════════════

def load_processed_files():
    if not os.path.exists(LOG_FILE):
        return set()
    with open(LOG_FILE, 'r') as f:
        return set(line.strip() for line in f.readlines())

def mark_as_processed(filename):
    with open(LOG_FILE, 'a') as f:
        f.write(filename + '\n')


# ══════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════

def build_summary_data(rows):
    """
    Returns nested dict:
    { year: { month_label: { category: [list of row dicts] } } }
    Skips rows with amount=0 or recipient='Not found'
    """
    data = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for row in rows:
        if row["amount"] <= 0:          continue
        if row["recipient"] == "Not found": continue
        if row["date"]      == "Not found": continue
        month = extract_month(row["date"])
        year  = extract_year(row["date"])
        if month and year:
            data[year][month][row["category"]].append(row)
    return data

def generate_summary_sheet(rows):
    """Rebuild the Monthly Summary sheet in Excel"""
    wb = get_or_create_workbook()
    if "Monthly Summary" in wb.sheetnames:
        del wb["Monthly Summary"]

    ws_summary = wb.create_sheet("Monthly Summary")
    data = build_summary_data(rows)

    if not data:
        wb.save(EXCEL_FILE)
        return

    all_categories = sorted({cat for year in data.values()
                              for month in year.values()
                              for cat in month.keys()})

    ws_summary.append(["Year","Month"] + all_categories + ["TOTAL"])

    for year in sorted(data.keys(), reverse=True):
        for month in sorted(data[year].keys(), key=month_sort_key):
            row_data    = [year, month]
            month_total = 0
            for cat in all_categories:
                amt = round(sum(r["amount"] for r in data[year][month].get(cat,[])), 2)
                row_data.append(amt if amt > 0 else "")
                month_total += amt
            row_data.append(round(month_total, 2))
            ws_summary.append(row_data)

    for col in ws_summary.columns:
        max_len = max(len(str(c.value)) if c.value else 0 for c in col)
        ws_summary.column_dimensions[col[0].column_letter].width = max_len + 4

    wb.save(EXCEL_FILE)

def print_summary_to_terminal(rows):
    data = build_summary_data(rows)
    if not data:
        print("\n  No valid data yet.")
        return

    for year in sorted(data.keys(), reverse=True):
        print("\n" + "="*50)
        print(f"  {'[ ' + year + ' ]':^48}")
        print("="*50)

        year_total = 0
        for month in sorted(data[year].keys(), key=month_sort_key):
            month_total = sum(
                r["amount"]
                for cats in data[year][month].values()
                for r in cats
            )
            year_total += month_total
            print(f"\n  {month}  (Total: RM {month_total:.2f})")
            print(f"  {'-'*40}")
            for cat, cat_rows in sorted(data[year][month].items()):
                cat_total = sum(r["amount"] for r in cat_rows)
                print(f"    {cat:<26} RM {cat_total:>7.2f}  "
                      f"[{len(cat_rows)} transaction(s)]")

        print(f"\n  {'TOTAL FOR ' + year:<26} RM {year_total:>7.2f}")

    grand = sum(r["amount"] for row in rows
                if row["amount"] > 0 and row["recipient"] != "Not found"
                for r in [row])
    print("\n" + "="*50)
    print(f"  {'GRAND TOTAL':<26} RM {grand:>7.2f}")
    print("="*50)

def drill_down_summary(rows):
    """Let user pick year → month → category to see transactions"""
    data = build_summary_data(rows)
    if not data:
        print("\n  No data to browse.")
        return

    # Pick year
    years = sorted(data.keys(), reverse=True)
    print("\n  Select a year:")
    for i, y in enumerate(years, 1):
        print(f"    {i}. {y}")
    y_pick = input("  Year number (or Enter to cancel): ").strip()
    if not y_pick.isdigit() or not (1 <= int(y_pick) <= len(years)):
        return
    year = years[int(y_pick)-1]

    # Pick month
    months = sorted(data[year].keys(), key=month_sort_key)
    print(f"\n  Months in {year}:")
    for i, m in enumerate(months, 1):
        total = sum(r["amount"] for cats in data[year][m].values() for r in cats)
        print(f"    {i}. {m}  (RM {total:.2f})")
    m_pick = input("  Month number (or Enter to cancel): ").strip()
    if not m_pick.isdigit() or not (1 <= int(m_pick) <= len(months)):
        return
    month = months[int(m_pick)-1]

    # Pick category
    cats = sorted(data[year][month].keys())
    print(f"\n  Categories in {month} {year}:")
    for i, c in enumerate(cats, 1):
        total = sum(r["amount"] for r in data[year][month][c])
        print(f"    {i}. {c}  (RM {total:.2f})")
    print(f"    {len(cats)+1}. Show ALL categories")
    c_pick = input("  Category number (or Enter to cancel): ").strip()

    if c_pick == str(len(cats)+1):
        selected_cats = cats
    elif c_pick.isdigit() and 1 <= int(c_pick) <= len(cats):
        selected_cats = [cats[int(c_pick)-1]]
    else:
        return

    # Show transactions
    print(f"\n  {'─'*50}")
    print(f"  Transactions — {month} {year}")
    print(f"  {'─'*50}")
    for cat in selected_cats:
        print(f"\n  [{cat}]")
        for r in data[year][month][cat]:
            print(f"    Row {r['row_num']:>3} | {r['date']:<14} | "
                  f"{r['recipient']:<22} | RM {r['amount']:>7.2f}")
    print(f"  {'─'*50}")


# ══════════════════════════════════════════════════════════
# MENU ACTIONS
# ══════════════════════════════════════════════════════════

def action_process_receipts():
    if not os.path.exists(RECEIPTS_DIR):
        os.makedirs(RECEIPTS_DIR)

    processed = load_processed_files()
    all_files  = [f for f in os.listdir(RECEIPTS_DIR)
                  if f.lower().endswith(SUPPORTED_FORMATS)]
    new_files  = [f for f in all_files if f not in processed]

    print(f"\n  Found {len(all_files)} image(s) — {len(new_files)} new.\n")

    if not new_files:
        print(f"  Nothing new. Drop screenshots into:\n  {RECEIPTS_DIR}")
        return

    success, failed = 0, 0
    for filename in new_files:
        full_path = os.path.join(RECEIPTS_DIR, filename)
        print(f"  Processing: {filename}")
        try:
            text                              = extract_text(full_path)
            date, recipient, amount, cat, src = parse_receipt(text)
            print(f"    Source:    {src}")
            print(f"    Date:      {date}")
            print(f"    Recipient: {recipient}")
            print(f"    Amount:    RM {amount}")
            print(f"    Category:  {cat}")
            save_to_excel(date, recipient, amount, cat, src, filename)
            mark_as_processed(filename)
            success += 1
            print(f"    Saved.\n")
        except Exception as e:
            print(f"    FAILED: {e}\n")
            failed += 1

    generate_summary_sheet(load_all_rows())
    print(f"  Done. {success} saved, {failed} failed.")


def action_view_summary():
    rows = load_all_rows()
    print_summary_to_terminal(rows)

    print("\n  Options:")
    print("  1. Drill down into a month/category")
    print("  2. Back to menu")
    choice = input("  Choose: ").strip()
    if choice == "1":
        drill_down_summary(rows)

    generate_summary_sheet(rows)
    print(f"\n  Excel summary updated: {EXCEL_FILE}")


def action_manual_entry():
    print("\n  --- MANUAL EXPENSE ENTRY ---")
    print("  (Press Enter to cancel at any time)\n")

    # Date with validation
    while True:
        date = input("  Date (e.g. 21/05/2026): ").strip()
        if date == "":
            print("  Cancelled.")
            return
        if is_valid_date(date):
            break
        print(f"  '{date}' doesn't look like a valid date. "
              f"Try DD/MM/YYYY format.")

    # Recipient — must not be empty
    while True:
        recipient = input("  Recipient / Merchant: ").strip()
        if recipient == "":
            print("  Recipient cannot be empty.")
        else:
            break

    # Amount — must be a positive number
    amount = None
    while amount is None:
        raw = input("  Amount in RM (e.g. 15.50): ").strip()
        if raw == "":
            print("  Amount cannot be empty.")
            continue
        try:
            val = round(float(raw), 2)
            if val <= 0:
                print("  Amount must be greater than 0.")
            else:
                amount = val
        except ValueError:
            print("  Please enter a number (e.g. 12.50).")

    # Category — preset or custom
    print("\n  Categories:")
    for i, cat in enumerate(PRESET_CATEGORIES, 1):
        print(f"    {i}. {cat}")
    print(f"    {len(PRESET_CATEGORIES)+1}. Custom (type your own)")
    print(f"    Press Enter to auto-detect from merchant name")

    cat_choice = input("  Pick a number: ").strip()

    if cat_choice == "":
        category = categorize(recipient)
        print(f"  Auto-detected: {category}")
    elif cat_choice == str(len(PRESET_CATEGORIES)+1):
        while True:
            custom = input("  Enter your custom category name: ").strip()
            if custom:
                category = custom
                break
            print("  Category name cannot be empty.")
    elif cat_choice.isdigit() and 1 <= int(cat_choice) <= len(PRESET_CATEGORIES):
        category = PRESET_CATEGORIES[int(cat_choice)-1]
    else:
        category = categorize(recipient)
        print(f"  Auto-detected: {category}")

    # Confirm before saving
    print(f"\n  About to save:")
    print(f"    Date:      {date}")
    print(f"    Recipient: {recipient}")
    print(f"    Amount:    RM {amount:.2f}")
    print(f"    Category:  {category}")
    confirm = input("\n  Confirm? (Y/n): ").strip().lower()
    if confirm == 'n':
        print("  Cancelled — nothing saved.")
        return

    save_to_excel(date, recipient, amount, category, "MANUAL", "manual_entry")
    generate_summary_sheet(load_all_rows())
    print(f"\n  Saved and summary updated.")


def browse_for_fix():
    """Let user find a row by Recents / Month / Recipient / Category"""
    rows = load_all_rows()
    if not rows:
        print("  No entries found.")
        return None

    print("\n  Find transaction by:")
    print("  1. Recents (last 10)")
    print("  2. Month")
    print("  3. Recipient name")
    print("  4. Category")
    browse = input("  Choose: ").strip()

    matched = []

    if browse == "1":
        matched = rows[-10:]

    elif browse == "2":
        months = sorted({extract_month(r["date"]) for r in rows
                         if extract_month(r["date"])},
                        key=month_sort_key)
        print("\n  Available months:")
        for i, m in enumerate(months, 1):
            print(f"    {i}. {m}")
        pick = input("  Choose: ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(months):
            chosen = months[int(pick)-1]
            matched = [r for r in rows if extract_month(r["date"]) == chosen]

    elif browse == "3":
        term = input("  Search recipient name: ").strip().lower()
        matched = [r for r in rows if term in r["recipient"].lower()]

    elif browse == "4":
        cats = sorted({r["category"] for r in rows})
        print("\n  Categories:")
        for i, c in enumerate(cats, 1):
            print(f"    {i}. {c}")
        pick = input("  Choose: ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(cats):
            chosen = cats[int(pick)-1]
            matched = [r for r in rows if r["category"] == chosen]

    if not matched:
        print("  No matching transactions found.")
        return None

    print(f"\n  {'Row':<5} {'Date':<14} {'Recipient':<24} {'Amount':>9}  Category")
    print(f"  {'-'*70}")
    for r in matched:
        print(f"  {r['row_num']:<5} {r['date']:<14} {r['recipient']:<24} "
              f"RM {r['amount']:>6.2f}  {r['category']}")

    return matched


def action_fix_entry():
    while True:
        matched = browse_for_fix()
        if not matched:
            return

        try:
            row_num = int(input("\n  Enter row number to edit (0 to go back): ").strip())
        except ValueError:
            print("  Invalid number.")
            continue
        if row_num == 0:
            return

        row = next((r for r in matched if r["row_num"] == row_num), None)
        if not row:
            print("  Row not found in results.")
            continue

        # Edit loop for this row
        while True:
            print(f"\n  Editing row {row_num}:")
            print(f"    1. Date:      {row['date']}")
            print(f"    2. Recipient: {row['recipient']}")
            print(f"    3. Amount:    RM {row['amount']:.2f}")
            print(f"    4. Category:  {row['category']}")
            print(f"    5. Delete this entry")
            print(f"    6. Done editing this row")

            field = input("\n  Choose field (1-6): ").strip()

            if field == "6":
                break

            elif field == "5":
                confirm = input(f"  Delete row {row_num}? (y/N): ").strip().lower()
                if confirm == "y":
                    delete_row_in_excel(row_num)
                    generate_summary_sheet(load_all_rows())
                    print("  Deleted and summary updated.")
                    break
                else:
                    print("  Cancelled.")

            elif field in ("1","2","3","4"):
                col_map   = {"1":1,"2":2,"3":3,"4":4}
                col_names = {"1":"Date","2":"Recipient",
                             "3":"Amount","4":"Category"}

                if field == "1":
                    while True:
                        new_val = input(f"  New {col_names[field]}"
                                        f" (e.g. 21/05/2026): ").strip()
                        if is_valid_date(new_val):
                            break
                        print("  Invalid date format.")

                elif field == "3":
                    while True:
                        raw = input("  New Amount: ").strip()
                        try:
                            new_val = round(float(raw), 2)
                            if new_val > 0: break
                            print("  Must be greater than 0.")
                        except ValueError:
                            print("  Enter a number.")

                elif field == "4":
                    print("\n  Categories:")
                    for i, c in enumerate(PRESET_CATEGORIES, 1):
                        print(f"    {i}. {c}")
                    print(f"    {len(PRESET_CATEGORIES)+1}. Custom")
                    pick = input("  Choose: ").strip()
                    if pick == str(len(PRESET_CATEGORIES)+1):
                        new_val = input("  Custom category: ").strip()
                    elif pick.isdigit() and 1 <= int(pick) <= len(PRESET_CATEGORIES):
                        new_val = PRESET_CATEGORIES[int(pick)-1]
                    else:
                        print("  Unchanged.")
                        continue

                else:
                    new_val = input(f"  New {col_names[field]}: ").strip()

                update_row_in_excel(row_num, col_map[field], new_val)
                row[col_names[field].lower()] = new_val
                generate_summary_sheet(load_all_rows())
                print(f"  Updated. Summary refreshed.")

            else:
                print("  Please choose 1–6.")

        # After editing one row, offer to edit another
        another = input("\n  Edit another transaction? (y/N): ").strip().lower()
        if another != "y":
            return


def action_delete_entry():
    """Option 6 — focused delete flow"""
    matched = browse_for_fix()
    if not matched:
        return

    try:
        row_num = int(input("\n  Row number to delete (0 to cancel): ").strip())
    except ValueError:
        print("  Invalid.")
        return
    if row_num == 0:
        return

    row = next((r for r in matched if r["row_num"] == row_num), None)
    if not row:
        print("  Row not found.")
        return

    print(f"\n  About to delete:")
    print(f"    {row['date']} | {row['recipient']} | RM {row['amount']:.2f} | {row['category']}")
    confirm = input("  Confirm delete? (y/N): ").strip().lower()
    if confirm == "y":
        delete_row_in_excel(row_num)
        generate_summary_sheet(load_all_rows())
        print("  Deleted and summary updated.")
    else:
        print("  Cancelled.")


# ══════════════════════════════════════════════════════════
# MAIN MENU
# ══════════════════════════════════════════════════════════

ACTIONS = {
    "1": ("Process new receipts",  action_process_receipts),
    "2": ("View spending summary",  action_view_summary),
    "3": ("Add expense manually",   action_manual_entry),
    "4": ("Fix an entry",           action_fix_entry),
    "5": ("Delete an entry",        action_delete_entry),
}

def main_menu():
    print("\n" + "="*50)
    print(f"  {'EXPENSE TRACKER':^48}")
    print("="*50)
    for key, (label, _) in ACTIONS.items():
        print(f"  {key}. {label}")
    print("  6. Exit")
    print("="*50)
    return input("  Choose (1-6): ").strip()

def run():
    while True:
        choice = main_menu()

        if choice == "6":
            print("\n  Goodbye!\n")
            break
        elif choice in ACTIONS:
            _, action_fn = ACTIONS[choice]
            action_fn()
            # Quick nav after each action
            next_choice = post_action_menu()
            if next_choice == "7":
                print("\n  Goodbye!\n")
                break
            elif next_choice in ACTIONS:
                _, action_fn = ACTIONS[next_choice]
                action_fn()
        else:
            print("  Please enter 1–6.")

# ── RUN ────────────────────────────────────────────────
run()
