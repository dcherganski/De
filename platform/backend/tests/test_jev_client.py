"""Jev client against a mocked HTTP endpoint. Response bodies follow docs.typesafe.ai/api examples."""

import json

import httpx
import pytest

from app.jev_client.client import JevClient, JevError, JevInvalidResponse, JevNotConfigured, JevTimeout
from app.jev_client.schemas import SystemOneRequest
from app.strategy.questions import build_questions


def request() -> SystemOneRequest:
    return SystemOneRequest(state={"instrument": "BTC/EUR"}, model="jev-latest", questions=build_questions(30))


def good_body(**override):
    body = {
        "model": "jev-1.13.0",
        "answers": {
            "close_above_target": {"type": "noul", "noul": 0.31},
            "touch_target": {"type": "noul", "noul": 0.52},
            "close_below_stop": {"type": "noul", "noul": 0.12},
            "action": {"type": "choice", "choice": "HOLD", "probabilities": {"BUY": 0.2, "HOLD": 0.7, "SELL": 0.1},
                       "confidence": 0.6},
        },
        "usage": {"input_tokens": 1200, "output_tokens": 30},
    }
    body.update(override)
    return body


def client_with(handler, **kw) -> JevClient:
    return JevClient("test-key-not-real", transport=httpx.MockTransport(handler), backoff_s=0.0, **kw)


async def test_request_matches_documented_shape_and_auth():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=good_body())

    res = await client_with(handler).ask(request())
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen["auth"] == "Bearer test-key-not-real"
    body = seen["body"]
    assert set(body) == {"state", "model", "questions"}
    assert body["questions"]["close_above_target"]["type"] == "noul"
    assert set(body["questions"]["close_above_target"]["criteria"]) == {"true", "false"}
    assert body["questions"]["action"]["type"] == "choice"
    assert set(body["questions"]["action"]["criteria"]) == {"BUY", "HOLD", "SELL"}
    assert "test-key-not-real" not in json.dumps(body)
    assert res.response.model == "jev-1.13.0" and res.attempts == 1
    assert str(res.cost_usd(__import__("decimal").Decimal("0.042"))) == "0.0000504"


async def test_retries_429_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "0"}) if len(calls) == 1 else httpx.Response(200, json=good_body())

    res = await client_with(handler).ask(request())
    assert res.attempts == 2


@pytest.mark.parametrize("status", [401, 422, 500])
async def test_non_retryable_errors_fail(status):
    c = client_with(lambda req: httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(JevError, match=str(status)):
        await c.ask(request())


async def test_overloaded_gives_up_after_retries():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(529)

    with pytest.raises(JevError, match="529"):
        await client_with(handler, max_retries=2).ask(request())
    assert len(calls) == 3


async def test_timeout():
    def handler(req):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(JevTimeout):
        await client_with(handler).ask(request())


@pytest.mark.parametrize("body", [
    good_body(answers={"action": good_body()["answers"]["action"]}),  # missing answers
    {**good_body(), "answers": {**good_body()["answers"], "touch_target": {"type": "noul", "noul": 1.7}}},
    {**good_body(), "answers": {**good_body()["answers"], "action": {
        "type": "choice", "choice": "MOON", "probabilities": {"BUY": 0.2, "HOLD": 0.7, "SELL": 0.1}, "confidence": 0.5}}},
    {**good_body(), "answers": {**good_body()["answers"], "action": {
        "type": "choice", "choice": "BUY", "probabilities": {"BUY": 0.9, "HOLD": 0.7, "SELL": 0.1}, "confidence": 0.5}}},
    {**good_body(), "answers": {**good_body()["answers"], "close_below_stop": {"type": "score", "score": 1.0,
                                                                              "legend": {}, "probabilities": {}, "confidence": 0.5}}},
    {"answers": {}, "usage": {}},
])
async def test_invalid_responses_are_rejected(body):
    with pytest.raises(JevInvalidResponse):
        await client_with(lambda req: httpx.Response(200, json=body)).ask(request())


async def test_not_configured():
    with pytest.raises(JevNotConfigured):
        await JevClient(None).ask(request())
    assert "key" not in repr(JevClient("secret-value")).lower().replace("configured", "")
