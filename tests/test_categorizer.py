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

import pytest

CATS = ["Food & Dining", "Transport", "Shopping"]

@pytest.fixture(autouse=True)
def _fresh_model_cache(monkeypatch):
    """Pin the model so call counts below don't include a ListModels request."""
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-flash")
    monkeypatch.setattr(categorizer, "_resolved_model", None)
    monkeypatch.setattr(categorizer, "_retired_models", set())

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

MODEL_LIST = {"models": [
    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.5-flash-preview", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3-flash-lite", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3-flash-image", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-flash-latest", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
]}

def test_auto_model_prefers_newest_stable_flash(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(categorizer.urllib.request, "urlopen",
                        lambda req, timeout: _Resp(json.dumps(MODEL_LIST).encode()))
    assert categorizer.list_flash_models() == [
        "gemini-3-flash", "gemini-2.5-flash", "gemini-3.5-flash-preview"]
    assert categorizer.gemini_model() == "gemini-3-flash"

def test_env_model_wins(monkeypatch):
    assert categorizer.gemini_model() == "gemini-test-flash"

def test_listing_failure_falls_back_to_alias(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    def boom(req, timeout):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(categorizer.urllib.request, "urlopen", boom)
    assert categorizer.gemini_model() == "gemini-flash-latest"

def test_retired_model_404_picks_another(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(categorizer, "_resolved_model", "gemini-3-flash")
    called = []
    def fake(req, timeout):
        url = req.full_url
        called.append(url)
        if url.endswith("/models?pageSize=1000"):
            return _Resp(json.dumps(MODEL_LIST).encode())
        if "gemini-3-flash:" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(
                b'{"error": {"code": 404, "message": "This model is no longer available to new users."}}'))
        return _gemini_reply({"merchant": "Luckin Coffee", "amount": 12.9,
                              "date": "01/10/2026", "category": "Food & Dining"})
    monkeypatch.setattr(categorizer.urllib.request, "urlopen", fake)
    out = llm_parse_receipt("x", CATS)
    assert out["merchant"] == "Luckin Coffee"
    assert "gemini-2.5-flash:" in called[-1]
    assert categorizer.gemini_model() == "gemini-2.5-flash"

def test_error_message_is_readable():
    body = '{"error": {"code": 404, "message": "This model models/x is gone.",\n "status": "NOT_FOUND"}}'
    assert categorizer.api_error_message(body) == "This model models/x is gone."
    assert categorizer.api_error_message("<html>oops</html>") == "<html>oops</html>"
