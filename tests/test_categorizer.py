import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from categorizer import classify, normalize_merchant, categorize

def test_park_is_not_health():
    assert categorize("PARK ROYAL RESTAURANT") == "Food & Dining"
    assert categorize("MARKET STALL") == "Uncategorized"

def test_grabpay_not_food():
    assert categorize("GRABPAY") == "Uncategorized"
    assert categorize("GRAB") == "Transport"
    assert categorize("GRABFOOD KLCC") == "Food & Dining"

def test_normalisation():
    assert normalize_merchant("MCDONALD'S (KLCC) SDN BHD") == ("MCDONALDS", "McDonald's")
    assert normalize_merchant("STARBUCKS #1234")[0] == "STARBUCKS"
    assert normalize_merchant("") == ("", "")

def test_learned_beats_keyword_and_fuzzy_branch():
    rules = {"STARBUCKS": {"merchant": "Starbucks", "category": "Shopping"}}
    assert classify("Starbucks Mid Valley", rules)["category"] == "Shopping"
    assert classify("STARBUCKS", rules)["method"] == "learned"

def test_ocr_typo_fuzzy():
    rules = {"WATSONS": {"merchant": "Watsons", "category": "Health & Fitness"}}
    assert classify("WATS0NS", rules)["method"] == "fuzzy"

def test_unknown_without_llm(monkeypatch=None):
    os.environ.pop("GEMINI_API_KEY", None)
    r = classify("ZZZ QWERTY", {}, ["Shopping"], use_llm=True)
    assert r["category"] == "Uncategorized" and r["method"] == "none"

import io, json, urllib.error
import categorizer
from categorizer import is_label, llm_parse_receipt

CATS = ["Food & Dining", "Transport", "Shopping"]

def test_labels_are_not_merchants():
    for junk in ["Wallet", "Transaction Type", "Reference No", "email", "LEAD",
                 "Not found", "RM450 TNG", "", None, "12345"]:
        assert is_label(junk), junk
    for real in ["Luckin Coffee", "HEXTAR LUCKIN", "Apple Services", "kuchai lama"]:
        assert not is_label(real), real

class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False

def _gemini_reply(obj):
    return _Resp(json.dumps({"candidates": [{"content": {"parts": [
        {"text": json.dumps(obj)}]}}]}).encode())

def test_parse_receipt_success(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(categorizer.urllib.request, "urlopen", lambda req, timeout: _gemini_reply(
        {"merchant": "Luckin Coffee", "amount": 12.9, "date": "01/10/2026", "category": "Food & Dining"}))
    out = llm_parse_receipt("Transaction Type\nMerchant HEXTAR LUCKIN\nRM12.90", CATS)
    assert out == {"merchant": "Luckin Coffee", "amount": 12.9,
                   "date": "01/10/2026", "category": "Food & Dining"}

def test_parse_receipt_rejects_label_and_bad_category(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(categorizer.urllib.request, "urlopen", lambda req, timeout: _gemini_reply(
        {"merchant": "Wallet", "amount": 0, "date": "", "category": "Groceries"}))
    out = llm_parse_receipt("x", CATS)
    assert out["merchant"] == "" and out["amount"] is None
    assert out["date"] is None and out["category"] == "Uncategorized"

def test_gemini_http_error_is_recorded_not_raised(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(categorizer.time, "sleep", lambda s: None)
    calls = []
    def boom(req, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, io.BytesIO(b'{"error":"quota"}'))
    monkeypatch.setattr(categorizer.urllib.request, "urlopen", boom)
    assert llm_parse_receipt("x", CATS) is None
    assert len(calls) == 2                       # retried once
    assert "HTTP 429" in categorizer.last_llm_error

def test_missing_key_is_reported(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert llm_parse_receipt("x", CATS) is None
    assert categorizer.last_llm_error == "GEMINI_API_KEY is not set"
