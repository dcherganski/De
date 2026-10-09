"""Request and response types for TypeSafe System One (Jev), exactly as documented.

Source: https://docs.typesafe.ai/api (checked 2026-10-09).
- Request: `state` (string | object | array), `model` (string), `questions` (map of id → Question).
- Question `type` is "noul", "choice" or "score"; all have `instructions`.
  - noul: optional `criteria` object with `true` / `false` descriptions.
  - choice: `criteria` maps option → description (or null); at most 255 options.
  - score: `criteria` is an ordered array of 2–10 levels.
- Response: `model` (versioned id that answered), `answers` (same ids), `usage` {input_tokens, output_tokens}.
  - noul answer: `noul` in [0, 1] (probability of yes; no confidence field).
  - choice answer: `choice`, `probabilities` (sum to 1), `confidence`.
  - score answer: `score`, `legend`, `probabilities`, `confidence`.
No other fields are invented here; unknown response fields are ignored, missing ones are errors.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

JSON = str | dict[str, Any] | list[Any]


class NoulCriteria(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    true_: JSON | None = Field(default=None, alias="true")
    false_: JSON | None = Field(default=None, alias="false")


class NoulQuestion(BaseModel):
    type: Literal["noul"] = "noul"
    instructions: JSON
    criteria: NoulCriteria | None = None


class ChoiceQuestion(BaseModel):
    type: Literal["choice"] = "choice"
    instructions: JSON
    criteria: dict[str, JSON | None]

    @field_validator("criteria")
    @classmethod
    def _options(cls, v):
        if not 2 <= len(v) <= 255:
            raise ValueError("a Choice needs between 2 and 255 options")
        return v


class ScoreQuestion(BaseModel):
    type: Literal["score"] = "score"
    instructions: JSON
    criteria: list[JSON]

    @field_validator("criteria")
    @classmethod
    def _levels(cls, v):
        if not 2 <= len(v) <= 10:
            raise ValueError("a Score needs between 2 and 10 levels")
        return v


Question = Annotated[NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="type")]


class SystemOneRequest(BaseModel):
    state: JSON
    model: str
    questions: dict[str, Question]

    def payload(self) -> dict:
        return self.model_dump(by_alias=True, exclude_none=True)


def _prob(x: float) -> float:
    if not (isinstance(x, (int, float)) and math.isfinite(x) and 0.0 <= x <= 1.0):
        raise ValueError(f"probability out of range: {x!r}")
    return float(x)


class NoulAnswer(BaseModel):
    type: Literal["noul"]
    noul: float

    @field_validator("noul")
    @classmethod
    def _range(cls, v):
        return _prob(v)


class ChoiceAnswer(BaseModel):
    type: Literal["choice"]
    choice: str
    probabilities: dict[str, float]
    confidence: float

    @field_validator("confidence")
    @classmethod
    def _conf(cls, v):
        return _prob(v)

    @field_validator("probabilities")
    @classmethod
    def _dist(cls, v):
        for p in v.values():
            _prob(p)
        if v and abs(sum(v.values()) - 1.0) > 1e-3:
            raise ValueError("choice probabilities do not sum to 1")
        return v


class ScoreAnswer(BaseModel):
    type: Literal["score"]
    score: float
    legend: dict[str, str]
    probabilities: dict[str, float]
    confidence: float

    @field_validator("confidence")
    @classmethod
    def _conf(cls, v):
        return _prob(v)


Answer = Annotated[NoulAnswer | ChoiceAnswer | ScoreAnswer, Field(discriminator="type")]


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int


class SystemOneResponse(BaseModel):
    model: str
    answers: dict[str, Answer]
    usage: Usage


def check_answers_match(req: SystemOneRequest, resp: SystemOneResponse) -> list[str]:
    """Cross-checks the schema alone cannot do: same ids, same types, options from our own criteria."""
    problems: list[str] = []
    missing = set(req.questions) - set(resp.answers)
    if missing:
        problems.append(f"missing answers: {sorted(missing)}")
    for qid, ans in resp.answers.items():
        q = req.questions.get(qid)
        if q is None:
            problems.append(f"unexpected answer id {qid}")
            continue
        if q.type != ans.type:
            problems.append(f"{qid}: asked {q.type}, got {ans.type}")
            continue
        if isinstance(q, ChoiceQuestion) and isinstance(ans, ChoiceAnswer):
            if ans.choice not in q.criteria:
                problems.append(f"{qid}: choice {ans.choice!r} is not one of the options")
            if set(ans.probabilities) != set(q.criteria):
                problems.append(f"{qid}: probabilities do not cover exactly the options")
    return problems
