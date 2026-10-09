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
