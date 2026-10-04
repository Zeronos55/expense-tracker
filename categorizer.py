"""Merchant normalisation + category assignment, shared by app.py and
receipt_reader.py. Pure Python (stdlib only) so it also works in the packaged
.exe; the Supabase persistence of learned rules lives in app.py.

Cascade (first hit wins):
  1. learned rule, exact    (rules[merchant_key])
  2. learned rule, fuzzy    (similar merchant seen before)
  3. keyword rules          (word-boundary, ordered)
  4. Gemini free tier       (only when use_llm and GEMINI_API_KEY is set)
  5. "Uncategorized"
"""
import difflib
import json
import os
import re
import urllib.request

UNCATEGORIZED = "Uncategorized"
FUZZY_THRESHOLD = 0.85

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


def llm_classify(raw, categories):
    """Ask Gemini (free tier) for {merchant, category}. None on any failure."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or not raw:
        return None
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    body = {
        "contents": [{"parts": [{"text":
            "Classify this merchant from a Malaysian payment receipt. "
            f"Merchant text: {raw!r}. Return a clean merchant name and the "
            f"best category from: {', '.join(categories)}. If unsure use "
            f"'{UNCATEGORIZED}'."}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {"merchant": {"type": "STRING"},
                               "category": {"type": "STRING",
                                            "enum": list(categories) + [UNCATEGORIZED]}},
                "required": ["merchant", "category"]},
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.load(resp)
        out = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
        category = out.get("category")
        if category not in categories:
            return None
        return {"merchant": (out.get("merchant") or "").strip(), "category": category}
    except Exception:
        return None


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
