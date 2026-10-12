import asyncio
import json
from unittest.mock import MagicMock

import pytest

from llm import client
from llm.capacity import CapacityBusy, CapacityTimeout
from llm.humor import generate_humor


def test_one_bounded_interactive_call(monkeypatch):
    query = MagicMock(return_value=json.dumps({"text": "The coffee has filed an appeal."}))
    monkeypatch.setattr(client, "query_llm", query)
    result = asyncio.run(generate_humor("scuza", "en", {"style": "corporate", "reason": "private reason"}, "fallback"))
    assert result == "The coffee has filed an appeal."
    query.assert_called_once()
    options = query.call_args.kwargs
    assert options["thinking"] is False
    assert options["timeout"] == 30
    assert options["options"]["max_tokens"] <= 220
    assert options["capacity_policy"] == "interactive"
    assert options["queue_timeout"] == 2
    assert "English" in options["system_prompt"]
    assert "untrusted data" in options["system_prompt"]
    assert json.loads(options["prompt"].split("\n", 1)[1])["style"] == "corporate"


def test_background_and_romanian(monkeypatch):
    query = MagicMock(return_value='{"text":"Cafeaua cere o pauză."}')
    monkeypatch.setattr(client, "query_llm", query)
    assert asyncio.run(generate_humor("tribunal", "ro", {"result": "neutral"}, "rezervă", background=True)) == "Cafeaua cere o pauză."
    assert query.call_args.kwargs["capacity_policy"] == "background"
    assert query.call_args.kwargs["queue_timeout"] == 0
    assert "Romanian" in query.call_args.kwargs["system_prompt"]


@pytest.mark.parametrize("error", [CapacityBusy("busy"), CapacityTimeout("busy"), client.LlamaCppTimeoutError(30), client.LlamaCppError("down"), TimeoutError(), OSError()])
def test_expected_errors_use_fallback_without_retry(monkeypatch, error, caplog):
    query = MagicMock(side_effect=error)
    monkeypatch.setattr(client, "query_llm", query)
    assert asyncio.run(generate_humor("scuza", "ro", {"reason": "private reason"}, "rezervă")) == "rezervă"
    query.assert_called_once()
    assert "private reason" not in caplog.text


@pytest.mark.parametrize("raw", ["", "   ", "not JSON", "[]", '{"text":""}', '{"text":3}', '{"text":"hello","extra":1}', '{"text":"<think>secret"}', '{"text":"```code```"}', json.dumps({"text": "x" * 601}), "x" * 1401])
def test_invalid_output_uses_fallback(monkeypatch, raw):
    query = MagicMock(return_value=raw)
    monkeypatch.setattr(client, "query_llm", query)
    assert asyncio.run(generate_humor("scuza", "en", {}, "fallback")) == "fallback"
    query.assert_called_once()


@pytest.mark.parametrize("language,label_a,label_b", [("en", "Purchase advocate", "Household accountant"), ("ro", "Avocatul cumpărăturii", "Contabilul casei")])
def test_purchase_requires_two_parts(monkeypatch, language, label_a, label_b):
    query = MagicMock(return_value='{"advocate":"The parcel wants a welcome party.","accountant":"The budget wants a quiet evening."}')
    monkeypatch.setattr(client, "query_llm", query)
    result = asyncio.run(generate_humor("merita", language, {}, "fallback"))
    assert label_a in result and label_b in result
    query.return_value = '{"text":"Buy it."}'
    assert asyncio.run(generate_humor("merita", language, {}, "fallback")) == "fallback"


@pytest.mark.parametrize("claim", ["Only 50 RON!", "Save twenty percent.", "Costs twenty.", "Half price!", "Costă zece lei.", "Double the value.", "€ bargain"])
def test_purchase_rejects_numeric_claims(monkeypatch, claim):
    query = MagicMock(return_value=json.dumps({"advocate": claim, "accountant": "The budget is asleep."}))
    monkeypatch.setattr(client, "query_llm", query)
    assert asyncio.run(generate_humor("merita", "en", {}, "fallback")) == "fallback"
    query.assert_called_once()


def test_untrusted_facts_bounded_before_call(monkeypatch):
    query = MagicMock(return_value='{"text":"Hello."}')
    monkeypatch.setattr(client, "query_llm", query)
    asyncio.run(generate_humor("scuza", "en", {"reason": "x" * 5000, "price": float("nan")}, "fallback"))
    facts = json.loads(query.call_args.kwargs["prompt"].split("\n", 1)[1])
    assert len(facts["reason"]) == 1200
    assert facts["price"] is None
    query.reset_mock()
    assert asyncio.run(generate_humor("scuza", "en", {str(i): "x" * 1200 for i in range(24)}, "fallback")) == "fallback"
    query.assert_not_called()
