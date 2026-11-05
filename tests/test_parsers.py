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


# Maybank-style "Transferred" screen: labels first, values after; the remark
# is the line before the date. A long receiver name wraps onto a second line.
TRANSFER_HUUTHAI = """01:40
RM 20.88
Transferred
Receiver
Remark
Date & Time
SIEW MING HUI
HuuThai
05/11/2026 01:40:19
Ad Your 11.11 shopping list is missing tickets!
Done"""

TRANSFER_WRAPPED = """15:50
RM 14.00
Transferred
Receiver
Remark
Date & Time
ISABELLE CHANG
EE LIN
Matcha
05/11/2026 15:50:24
Done"""

TRANSFER_NO_REMARK = """RM 9.00
Transferred
Receiver
Remark
Date & Time
AHMAD BIN ALI
05/11/2026 10:00:00
Done"""

TRANSFER_INLINE = """RM 9.00
Transferred
Receiver AHMAD BIN ALI
Remark nasi lemak
05/11/2026"""


def test_remark_column_layout():
    assert app.remark_value(TRANSFER_HUUTHAI) == "HuuThai"

def test_remark_with_wrapped_receiver_name():
    assert app.remark_value(TRANSFER_WRAPPED) == "Matcha"

def test_no_remark_does_not_return_receiver():
    assert app.remark_value(TRANSFER_NO_REMARK) is None

def test_remark_inline():
    assert app.remark_value(TRANSFER_INLINE) == "nasi lemak"

def test_receiver_is_payee_and_remark_sets_category(monkeypatch):
    monkeypatch.setattr(app, "llm_parse_receipt", lambda *a, **k: None)
    p = app.parse_receipt(TRANSFER_WRAPPED, {})
    assert p["recipient"] == "ISABELLE CHANG"
    assert p["remark"] == "Matcha" and p["category"] == "Food & Dining"
    assert p["method"] == "remark"

def test_remark_category_is_not_saved_as_person_rule(monkeypatch):
    saved = []
    monkeypatch.setattr(app, "save_merchant_rule", lambda *a, **k: saved.append(a))
    monkeypatch.setattr(app, "llm_parse_receipt", lambda *a, **k: {
        "merchant": "Huu Thai", "amount": 20.88, "date": "05/11/2026", "category": "Food & Dining"})
    p = app.parse_receipt(TRANSFER_HUUTHAI, {})
    assert p["category"] == "Food & Dining" and saved == []


# Touch 'n Go "Transfer to Wallet": label/value pairs, note under "Payment Details".
TNG_TRANSFER_WALLET = """19:44
Details
-RM28.50
Transaction Type
Transfer to Wallet
Transfer To
YEOW LI YAN
Payment Details
yy gift
Payment Method
eWallet Balance
Date/Time
05/11/2026 19:43:42
Status
Successful"""


def test_payment_details_is_remark():
    assert app.remark_value(TNG_TRANSFER_WALLET) == "yy gift"

def test_payment_details_inline():
    assert app.remark_value("Payment Details lunch with ah kow\n05/11/2026") == "lunch with ah kow"

def test_payment_details_empty_is_not_next_label():
    text = "Transfer To\nYEOW LI YAN\nPayment Details\nPayment Method\neWallet Balance\nDate/Time\n05/11/2026"
    assert app.remark_value(text) is None
