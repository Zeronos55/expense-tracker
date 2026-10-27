import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")
import pytest
import app
from categorizer import field_value

# TNG receipt as iOS OCR returns it when it reads the label column first:
# the line after "Merchant" is the next label, not the payee.
TNG_COLUMNS = """-RM12.90
Successful
Transaction Type
Merchant
Wallet
Date/Time
Reference No
DuitNow QR
HEXTAR LUCKIN COFFEE
eWallet Balance
01/10/2026 13:47
2610018812"""

TNG_INLINE = """-RM7.00
Transaction Type DuitNow QR
Merchant KEDAI MAKAN AH KOW
Wallet eWallet Balance
Date/Time 06/10/2026 12:01"""

DUITNOW_TRANSFER = """DuitNow Transfer
Successful
RM 20.00
Recipient Name
LIM AH KOW
Recipient Reference
lunch
Transfer was sent to email
04/10/2026"""

MAYBANK_CARD = """Maybank VISA
-RM39.90
SALE GRAB RIDES
AP DATED 07/10/26
7 Oct 2026"""


def test_column_layout_finds_payee_not_label():
    assert app.parse_tng(TNG_COLUMNS) == ("01/10/2026", "HEXTAR LUCKIN COFFEE", 12.9)

def test_inline_layout():
    assert app.parse_tng(TNG_INLINE)[1] == "KEDAI MAKAN AH KOW"

def test_generic_label_must_start_line():
    # old regex matched "to email" in body text and saved "email"
    date, recipient, amount = app.parse_generic(DUITNOW_TRANSFER)
    assert recipient == "LIM AH KOW" and amount == 20.0 and date == "04/10/2026"

def test_maybank_sale_line():
    assert app.parse_maybank_card(MAYBANK_CARD) == ("7 Oct 2026", "GRAB RIDES", 39.9)

def test_label_only_column_gives_none():
    assert field_value("Merchant\nWallet\nDuitNow QR", app.PAYEE_LABELS) is None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, "SHORTCUT_SECRET", "s")
    monkeypatch.setattr(app, "merchant_rules", lambda: {})
    monkeypatch.setattr(app, "approved_keywords", lambda: [])
    monkeypatch.setattr(app, "llm_parse_receipt", lambda text, cats: None)
    saved = []
    monkeypatch.setattr(app, "db_insert", lambda *a, **k: saved.append(a))
    c = app.app.test_client()
    c.saved = saved
    return c

def test_unreadable_image_is_not_saved(client):
    r = client.post("/upload-from-shortcut", data={"secret": "s", "text": "Photos\nEdit\nDone"})
    assert r.status_code == 422 and client.saved == []
    assert "Nothing was saved" in r.get_json()["message"]

def test_partial_read_warns(client):
    r = client.post("/upload-from-shortcut", data={"secret": "s", "text": "Wallet\n-RM8.00\n06/10/2026"})
    body = r.get_json()
    assert r.status_code == 200 and len(client.saved) == 1
    assert "couldn't read the merchant" in body["message"]

def test_good_read_message(client):
    r = client.post("/upload-from-shortcut", data={"secret": "s", "text": TNG_COLUMNS})
    assert r.get_json()["message"].startswith("✓ RM 12.90 · HEXTAR LUCKIN COFFEE")

def test_ai_outage_is_named_in_the_notification(client, monkeypatch):
    monkeypatch.setattr(app.categorizer, "last_llm_error", "HTTP 429: quota exceeded")
    text = "Merchant\nZZZ QWERTY\nRM 5.00\n06/10/2026"
    msg = client.post("/upload-from-shortcut", data={"secret": "s", "text": text}).get_json()["message"]
    assert msg.endswith("Uncategorized (AI unavailable: HTTP 429: quota exceeded)")


# Bank "Transferred" screen: labels first, then values. The payee is a person,
# so the Remark says what it was for.
TRANSFER = """10:45
66
RM 29.68
Transferred
Receiver
Remark
Date & Time
TAN AH BENG
birthday cake
22/10/2026 10:45:15
Eastel
Paying too much for prepaid?
Done"""

TRANSFER_NO_REMARK = TRANSFER.replace("Remark\n", "").replace("birthday cake\n", "")

def test_transfer_receiver_and_remark_are_read():
    p = app.parse_receipt(TRANSFER)
    assert p["recipient"] == "TAN AH BENG" and p["amount"] == 29.68
    assert p["date"] == "22/10/2026" and p["remark"] == "birthday cake"

def test_blank_remark_is_not_the_date():
    assert app.receipt_remark(TRANSFER_NO_REMARK) is None
    assert app.parse_receipt(TRANSFER_NO_REMARK)["recipient"] == "TAN AH BENG"

def test_transfer_is_judged_by_remark_not_by_the_name(monkeypatch):
    monkeypatch.setattr(app, "llm_parse_receipt", lambda text, cats: None)
    rules = {"TAN AH BENG": {"merchant": None, "category": "Food & Dining"}}
    p = app.parse_receipt(TRANSFER, rules)
    assert p["category"] == "Gifts" and p["method"] == "keyword"

def test_transfer_never_learns_the_person_as_a_merchant(monkeypatch):
    monkeypatch.setattr(app, "llm_parse_receipt", lambda text, cats: {
        "merchant": "", "amount": None, "date": None, "category": "Entertainment"})
    learned = []
    monkeypatch.setattr(app, "save_merchant_rule", lambda *a, **k: learned.append(a))
    text = TRANSFER.replace("birthday cake", "chill guy")
    p = app.parse_receipt(text)
    assert p["category"] == "Entertainment" and learned == []

def test_remark_is_saved_as_details(client):
    r = client.post("/upload-from-shortcut", data={"secret": "s", "text": TRANSFER})
    assert r.status_code == 200
    assert client.saved[0][6] == "birthday cake" and client.saved[0][3] == "Gifts"
