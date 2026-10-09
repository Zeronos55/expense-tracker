"""Merchant normalisation + category assignment, shared by app.py and
receipt_reader.py. Pure Python (stdlib only) so it also works in the packaged
.exe; the Supabase persistence of learned rules lives in app.py.

Cascade (first hit wins):
  1. learned rule, exact    (rules[merchant_key])
  2. learned rule, fuzzy    (similar merchant seen before)
  3. keyword rules          (word-boundary, ordered)
  4. Gemini free tier       (only when use_llm and GEMINI_API_KEY is set)
     app.py additionally sends the whole OCR text to llm_parse_receipt when
     the regex parsers miss the payee/amount/date.
  5. "Uncategorized"
"""
import difflib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

UNCATEGORIZED = "Uncategorized"
FUZZY_THRESHOLD = 0.85

# Field names printed on receipts ("Merchant", "Wallet", ...). OCR often
# returns these instead of the payee, so they are never a merchant.
FIELD_LABELS = {"WALLET", "TRANSACTION TYPE", "TRANSACTION", "REFERENCE NO",
                "REFERENCE", "REF NO", "EMAIL", "PAYMENT DETAILS", "DETAILS",
                "STATUS", "AMOUNT", "TOTAL", "DATE", "MERCHANT", "RECIPIENT",
                "PAY TO", "TO", "FROM", "ACCOUNT", "DATE TIME", "TIME",
                "TRANSACTION NO", "TRANSACTION DATE", "TRANSACTION ID",
                "PAYMENT METHOD", "PAID FROM", "PAID WITH", "PAYMENT TYPE",
                "TRANSFER TO", "MERCHANT NAME", "RECIPIENT NAME",
                "RECIPIENT REFERENCE", "BENEFICIARY", "BENEFICIARY NAME",
                "TOTAL AMOUNT", "REMARKS", "REFERENCE ID"}
# Plus values that are never a payee either.
LABEL_BLOCKLIST = FIELD_LABELS | {"NOT FOUND", "EWALLET", "E WALLET", "LEAD",
                                  "SUCCESSFUL", "DUITNOW QR", "DUITNOW", "TNG"}

# Tokens that carry no merchant identity.
NOISE_TOKENS = {"SDN", "BHD", "BERHAD", "SB", "ENTERPRISE", "ENT", "TRADING",
                "PLT", "LTD", "LIMITED", "CO", "THE", "MALAYSIA", "MY", "POS",
                "SALE", "PURCHASE", "PAYMENT"}

# Canonical merchants. If a cleaned key starts with the alias, use the name.
ALIASES = {
    "MCD": "McDonald's", "MCDONALD": "McDonald's", "MCDONALDS": "McDonald's",
    "STARBUCKS": "Starbucks", "KFC": "KFC", "GRABFOOD": "GrabFood",
    "FOODPANDA": "Foodpanda", "TEALIVE": "Tealive", "CHATIME": "Chatime",
    "SHOPEE": "Shopee", "LAZADA": "Lazada", "PETRONAS": "Petronas",
    "SHELL": "Shell", "NETFLIX": "Netflix", "SPOTIFY": "Spotify",
    "99 SPEEDMART": "99 Speedmart", "99SPEEDMART": "99 Speedmart",
}

# Ordered: earlier categories win. Keywords of <=4 chars must match a whole
# word (so ARK no longer matches PARK/MARKET); longer ones match word starts.
KEYWORD_RULES = [
    ("Food & Dining", ["GRABFOOD", "FOODPANDA", "MAMAK", "MCDONALD", "MCD",
                       "KFC", "SUBWAY", "STARBUCKS", "TEALIVE", "CHATIME",
                       "RESTAURANT", "RESTORAN", "KEDAI MAKAN", "JUICE",
                       "CAFE", "BAKERY", "PIZZA", "BURGER", "COFFEE"]),
    ("Transport", ["MYRAPID", "TOUCHNGO", "TOUCH N GO RELOAD", "PARKING",
                   "TOLL", "GRAB", "GRABCAR", "PETRONAS", "SHELL", "PETRON",
                   "BHP", "CALTEX", "LRT", "MRT", "KTM"]),
    ("Shopping", ["SHOPEE", "LAZADA", "AMAZON", "AEON", "IKEA", "UNIQLO",
                  "ZARA", "GUARDIAN", "WATSONS", "MARKETPLACE", "SPEEDMART",
                  "MYDIN", "LOTUS", "TESCO", "7 ELEVEN", "7ELEVEN"]),
    ("Entertainment", ["NETFLIX", "SPOTIFY", "STEAM", "YOUTUBE", "DISNEY",
                       "GSC", "TGV"]),
    ("Health & Fitness", ["FITNESS", "GYM", "YOGA", "CLINIC", "KLINIK",
                          "PHARMACY", "FARMASI", "HOSPITAL", "ARK"]),
    ("Utilities", ["TNB", "SYABAS", "AIR SELANGOR", "UNIFI", "MAXIS",
                   "CELCOM", "DIGI", "TELEKOM"]),
]

def _compile(kw):
    pat = r"\b" + re.escape(kw).replace(r"\ ", r"\s+")
    if len(kw) <= 4:
        pat += r"\b"
    return re.compile(pat)

_COMPILED_RULES = [(cat, [(kw, _compile(kw)) for kw in kws])
                   for cat, kws in KEYWORD_RULES]


def normalize_merchant(raw):
    """Return (merchant_key, display_name) for a raw OCR recipient string."""
    if not raw:
        return "", ""
    s = raw.upper()
    s = re.sub(r"\([^)]*\)", " ", s)            # (KLCC), (BRANCH 3)
    s = re.sub(r"[#*]\s*\w*\d\w*", " ", s)      # #123, *A1B2
    s = s.replace("'", "")
    s = re.sub(r"[^A-Z0-9 &]", " ", s)
    tokens = [t for t in s.split() if t not in NOISE_TOKENS]
    while tokens and re.fullmatch(r"\d{2,}", tokens[-1]):  # trailing store no.
        tokens.pop()
    key = " ".join(tokens)
    if not key:
        return "", raw.strip()
    for alias, name in sorted(ALIASES.items(), key=lambda kv: -len(kv[0])):
        if key == alias or key.startswith(alias + " "):
            return alias_key(name), name
    return key, key.title() if raw.isupper() or raw.islower() else " ".join(raw.split())


def _label_key(value):
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", (value or "").upper()).split())


def is_field_label(line):
    return _label_key(line) in FIELD_LABELS


def is_label(recipient):
    """True if the 'recipient' is really a receipt field label / junk."""
    if not recipient or not recipient.strip():
        return True
    s = _label_key(recipient)
    return (not s or s in LABEL_BLOCKLIST or bool(re.fullmatch(r"RM\s*[\d.]+.*", s))
            or not re.search(r"[A-Z]", s))


def field_value(text, labels):
    """Value of the first field in `labels` (e.g. "Merchant") on an OCR'd receipt.

    Handles both layouts iOS OCR produces:
      same line   "Merchant HEXTAR LUCKIN"
      next line   "Merchant" / "HEXTAR LUCKIN"
      columns     "Transaction Type" / "Merchant" / "Wallet" / "DuitNow QR" /
                  "HEXTAR LUCKIN" / "eWallet Balance"  (labels first, values after)
    In the column layout the value sits at the label's position in the block of
    values that follows, not on the next line. Returns None if nothing usable."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for label in labels:
        rx = re.compile(r"%s\b[\s:.\-]*(.*)$" % re.escape(label), re.IGNORECASE)
        for i, line in enumerate(lines):
            m = rx.match(line)
            if not m:
                continue
            if not is_field_label(line):        # "Merchant HEXTAR LUCKIN"
                rest = m.group(1).strip()
                if not is_label(rest):
                    return rest
                continue
            if _label_key(line) != _label_key(label):
                continue                        # "Merchant Name": another label
            start = i
            while start > 0 and is_field_label(lines[start - 1]):
                start -= 1
            end = i
            while end + 1 < len(lines) and is_field_label(lines[end + 1]):
                end += 1
            values = lines[end + 1:]
            k = i - start if end > start else 0
            if k < len(values) and not is_label(values[k]):
                return values[k]
    return None


def alias_key(name):
    return re.sub(r"[^A-Z0-9 &]", "", name.upper())


def keyword_category(key):
    for category, patterns in _COMPILED_RULES:
        for _kw, rx in patterns:
            if rx.search(key):
                return category
    return None


def _fuzzy_rule(key, rules):
    best, best_score = None, 0.0
    for k in rules:
        score = difflib.SequenceMatcher(None, key, k).ratio()
        a, b = set(key.split()), set(k.split())
        if a and b and (a <= b or b <= a):      # "STARBUCKS" ~ "STARBUCKS KLCC"
            score = max(score, 0.9)
        if score > best_score:
            best, best_score = k, score
    return best if best_score >= FUZZY_THRESHOLD else None


log = logging.getLogger(__name__)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_TIMEOUT = 10   # x2 with the retry: stays inside gunicorn's 30s
last_llm_error = None   # shown on /ai-status so failures aren't silent


GEMINI_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
GEMINI_FALLBACK_MODEL = "gemini-flash-latest"
_SKIP_MODEL_WORDS = ("image", "tts", "audio", "live", "embedding", "thinking", "exp", "lite", "8b")
_resolved_model = None   # cached auto-picked model; cleared when Google retires it
_retired_models = set()  # models that 404'd for this key (listed but not usable)


def _version_key(name):
    """Sort key for e.g. 'gemini-3.0-flash': stable before preview, newer first."""
    nums = tuple(int(n) for n in re.findall(r"\d+", name.split("-flash")[0]))
    return (0 if "preview" in name else 1, nums)


def list_flash_models():
    """Flash models this key can call generateContent on, best first."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return []
    req = urllib.request.Request(GEMINI_LIST_URL, headers={"x-goog-api-key": api_key})
    with urllib.request.urlopen(req, timeout=5) as resp:
        models = json.load(resp).get("models", [])
    names = [m["name"].split("/", 1)[-1] for m in models
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    names = [n for n in names if "flash" in n and not n.endswith("-latest")
             and n not in _retired_models
             and not any(w in n for w in _SKIP_MODEL_WORDS)]
    return sorted(names, key=_version_key, reverse=True)


def gemini_model():
    """GEMINI_MODEL if set, else the newest Flash model the key can use."""
    global _resolved_model
    if os.environ.get("GEMINI_MODEL"):
        return os.environ["GEMINI_MODEL"]
    if not _resolved_model:
        try:
            _resolved_model = (list_flash_models() or [GEMINI_FALLBACK_MODEL])[0]
        except Exception as e:
            log.warning("Listing Gemini models failed: %s", e)
            return GEMINI_FALLBACK_MODEL
    return _resolved_model


def _gemini_json(prompt, schema):
    """POST a prompt to Gemini and return the parsed JSON reply, or None.

    Retries once on 429/5xx, on a retired model (404, picks another) and
    when the model rejects the thinking budget; every failure is logged (never the key) and
    remembered in last_llm_error."""
    global last_llm_error, _resolved_model
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        last_llm_error = "GEMINI_API_KEY is not set"
        return None
    config = {"responseMimeType": "application/json", "responseSchema": schema,
              "thinkingConfig": {"thinkingBudget": 0}}
    for attempt in (1, 2):
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                           "generationConfig": config}).encode()
        req = urllib.request.Request(
            GEMINI_URL.format(model=gemini_model()), data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
        try:
            with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as resp:
                data = json.load(resp)
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            last_llm_error = None
            return json.loads(text)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:1000]
            last_llm_error = f"HTTP {e.code}: {api_error_message(detail)}"
            if attempt == 1:
                if e.code == 429 or e.code >= 500:
                    time.sleep(2)
                    continue
                if e.code == 404 and not os.environ.get("GEMINI_MODEL"):
                    _retired_models.add(gemini_model())   # retired: pick another
                    _resolved_model = None
                    continue
                if e.code == 400 and "thinking" in detail.lower():
                    config.pop("thinkingConfig")   # model doesn't take a budget
                    continue
        except Exception as e:
            last_llm_error = f"{type(e).__name__}: {e}"
        log.warning("Gemini call failed: %s", last_llm_error)
        return None


def api_error_message(detail):
    """The 'message' of a Google API error body, falling back to the raw text."""
    try:
        msg = json.loads(detail)["error"]["message"]
    except Exception:
        msg = detail
    return " ".join(str(msg).split())[:300]


def _category_enum(categories):
    return list(categories) + [UNCATEGORIZED]


def llm_classify(raw, categories):
    """Ask Gemini for {merchant, category} of a merchant string. None on failure."""
    if not raw:
        return None
    out = _gemini_json(
        "Classify this merchant from a Malaysian payment receipt. "
        f"Merchant text: {raw!r}. Return a clean merchant name and the "
        f"best category from: {', '.join(categories)}. If unsure use "
        f"'{UNCATEGORIZED}'.",
        {"type": "OBJECT",
         "properties": {"merchant": {"type": "STRING"},
                        "category": {"type": "STRING", "enum": _category_enum(categories)}},
         "required": ["merchant", "category"]})
    if not out or out.get("category") not in categories:
        return None
    return {"merchant": (out.get("merchant") or "").strip(), "category": out["category"]}


RECEIPT_PROMPT = """You read OCR text from a screenshot of a Malaysian bank or e-wallet payment receipt (Touch 'n Go, Maybank, ShopeePay, DuitNow QR, CIMB, RHB, ...).
Extract the payment. Rules:
- merchant: who was paid (shop / person / service), cleaned up, e.g. "Luckin Coffee". NEVER a field label or app word such as "Wallet", "Transaction Type", "Reference No", "Payment Details", "Email", "Status", "Successful", "DuitNow QR", "eWallet". If no payee is visible, return "".
- amount: the amount paid in RM as a number (no currency), 0 if not found.
- date: DD/MM/YYYY, "" if not found.
- category: best fit from: {categories}. Use "{uncat}" only if you really can't tell. Reloads/top-ups of a wallet are Transport only if it is a Touch 'n Go toll/transit reload, otherwise "{uncat}".

Examples:
"Transaction Type DuitNow QR\nMerchant HEXTAR LUCKIN COFFEE\nAmount RM12.90\n01/10/2026" -> {{"merchant": "Luckin Coffee", "amount": 12.90, "date": "01/10/2026", "category": "Food & Dining"}}
"Successful\nRM 45.00\nPaid to\nTENAGA NASIONAL BERHAD\nReference No 8812\n3 Oct 2026" -> {{"merchant": "Tenaga Nasional (TNB)", "amount": 45.00, "date": "03/10/2026", "category": "Utilities"}}

OCR text:
<<<
{text}
>>>"""


def llm_parse_receipt(text, categories):
    """Ask Gemini to read a whole OCR'd receipt.

    Returns {merchant, amount, date, category} (any may be empty) or None."""
    if not text:
        return None
    out = _gemini_json(
        RECEIPT_PROMPT.format(categories=", ".join(categories), uncat=UNCATEGORIZED,
                              text=text[:4000]),
        {"type": "OBJECT",
         "properties": {"merchant": {"type": "STRING"}, "amount": {"type": "NUMBER"},
                        "date": {"type": "STRING"},
                        "category": {"type": "STRING", "enum": _category_enum(categories)}},
         "required": ["merchant", "amount", "date", "category"]})
    if not isinstance(out, dict):
        return None
    merchant = (out.get("merchant") or "").strip()
    if is_label(merchant):
        merchant = ""
    try:
        amount = round(float(out.get("amount") or 0), 2)
    except (TypeError, ValueError):
        amount = 0
    category = out.get("category")
    return {"merchant": merchant, "amount": amount if amount > 0 else None,
            "date": (out.get("date") or "").strip() or None,
            "category": category if category in categories else UNCATEGORIZED}


def classify(recipient, rules=None, categories=(), use_llm=False):
    """Return {merchant, merchant_key, category, method}.

    rules: {merchant_key: {"merchant": str, "category": str}} — learned from
    user corrections / earlier results. method is one of learned, fuzzy,
    keyword, llm, none.
    """
    key, display = normalize_merchant(recipient)
    result = {"merchant": display, "merchant_key": key,
              "category": UNCATEGORIZED, "method": "none"}
    if not key:
        return result
    rules = rules or {}
    if key in rules:
        hit, method = rules[key], "learned"
    else:
        near = _fuzzy_rule(key, rules)
        hit, method = (rules[near], "fuzzy") if near else (None, None)
    if hit:
        result.update(category=hit["category"], method=method,
                      merchant=hit.get("merchant") or display)
        return result
    category = keyword_category(key)
    if category:
        result.update(category=category, method="keyword")
        return result
    if use_llm:
        out = llm_classify(recipient, categories)
        if out and out["category"] != UNCATEGORIZED:
            result.update(category=out["category"], method="llm",
                          merchant=out["merchant"] or display)
    return result


def categorize(recipient, rules=None):
    """Category only, no network — for callers that don't persist rules."""
    return classify(recipient, rules)["category"]
