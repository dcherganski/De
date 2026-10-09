"""HTTP client for TypeSafe System One. Never logs or forwards the API key.

Errors (docs.typesafe.ai/api): 401 bad key, 422 invalid body, 429 rate limited, 529 overloaded.
429 and 529 are retried with exponential backoff (honouring `retry-after` when present); everything
else fails fast. Any failure means "no answer": the caller must not trade on it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal

import httpx
from pydantic import ValidationError

from .schemas import SystemOneRequest, SystemOneResponse, check_answers_match

RETRYABLE = {429, 529}


class JevError(Exception):
    status = "error"


class JevTimeout(JevError):
    status = "timeout"


class JevInvalidResponse(JevError):
    status = "invalid"


class JevNotConfigured(JevError):
    status = "skipped"


@dataclass
class JevResult:
    response: SystemOneResponse
    raw: dict
    latency_ms: int
    attempts: int

    def cost_usd(self, price_per_mtok: Decimal) -> Decimal:
        return Decimal(self.response.usage.input_tokens) * price_per_mtok / Decimal(1_000_000)


class JevClient:
    def __init__(self, api_key: str | None, base_url: str = "https://api.typesafe.ai", timeout_s: float = 10.0,
                 max_retries: int = 2, transport: httpx.AsyncBaseTransport | None = None, backoff_s: float = 0.5):
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def __repr__(self) -> str:  # never show the key
        return f"JevClient(base_url={self.base_url!r}, configured={self.configured})"

    async def ask(self, req: SystemOneRequest) -> JevResult:
        if not self._key:
            raise JevNotConfigured("TYPESAFE_API_KEY is not set")
        payload = req.payload()
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        t0 = time.perf_counter()
        attempt = 0
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport) as http:
            while True:
                attempt += 1
                try:
                    resp = await http.post(f"{self.base_url}/v1/systemone", json=payload, headers=headers)
                except httpx.TimeoutException as exc:
                    raise JevTimeout(f"no answer within {self.timeout_s}s") from exc
                except httpx.HTTPError as exc:
                    raise JevError(f"connection error: {type(exc).__name__}") from exc
                if resp.status_code in RETRYABLE and attempt <= self.max_retries:
                    await asyncio.sleep(self._delay(resp, attempt))
                    continue
                break
        latency = int((time.perf_counter() - t0) * 1000)
        if resp.status_code != 200:
            raise JevError(f"HTTP {resp.status_code}: {_error_message(resp)}")
        try:
            raw = resp.json()
            parsed = SystemOneResponse.model_validate(raw)
        except (ValueError, ValidationError) as exc:
            raise JevInvalidResponse(f"response does not match the documented schema: {exc}") from exc
        problems = check_answers_match(req, parsed)
        if problems:
            raise JevInvalidResponse("; ".join(problems))
        return JevResult(parsed, raw, latency, attempt)

    def _delay(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        return self.backoff_s * (2 ** (attempt - 1))


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            return str(body["error"].get("message", ""))[:300]
        return str(body)[:300]
    except ValueError:
        return resp.text[:300]
