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
