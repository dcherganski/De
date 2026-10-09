"""The questions sent to Jev, built only from documented question types (docs.typesafe.ai/api).

The forecast event is explicit and measurable: the mid price *at the end* of the horizon versus a
level fixed now. "Touching" the level at any moment is a different event and is asked separately;
the two are never mixed. Thresholds and arithmetic stay in code; Jev gets the levels as data and
answers in probabilities. Wording is English, where Jev is most accurate.
"""

from __future__ import annotations

from ..jev_client.schemas import ChoiceQuestion, NoulCriteria, NoulQuestion

QUESTION_TEMPLATE_VERSION = "q1-2026-10-09"
ACTIONS = ("BUY", "HOLD", "SELL")


def build_questions(horizon_min: int) -> dict:
    h = f"{horizon_min} minutes"
    return {
        "close_above_target": NoulQuestion(
            instructions={
                "question": f"Will the mid price of `instrument` exactly {h} after `as_of` be at or above "
                            "`target.price_level` (the level set by `target.percent_above_now`)?",
                "event": "end-of-horizon close, not an intrabar touch",
            },
            criteria=NoulCriteria(
                true=f"At the end of the {h} the mid price is at or above the target level.",
                false=f"At the end of the {h} the mid price is below the target level, even if it touched it earlier.",
            ),
        ),
        "touch_target": NoulQuestion(
            instructions={
                "question": f"Will the price of `instrument` reach `target.price_level` at any moment during the "
                            f"next {h} after `as_of`?",
                "event": "touch at any time within the horizon, regardless of where it ends",
            },
            criteria=NoulCriteria(
                true=f"The price trades at or above the target level at least once within the {h}.",
                false=f"The price stays below the target level for the whole {h}.",
            ),
        ),
        "close_below_stop": NoulQuestion(
            instructions={
                "question": f"Will the mid price of `instrument` exactly {h} after `as_of` be at or below "
                            "`stop.price_level`?",
                "event": "end-of-horizon close",
            },
            criteria=NoulCriteria(
                true=f"At the end of the {h} the mid price is at or below the stop level.",
                false=f"At the end of the {h} the mid price is above the stop level.",
            ),
        ),
        "action": ChoiceQuestion(
            instructions={
                "question": f"Given `market`, `costs`, `portfolio` and the headlines in `untrusted_news`, which action "
                            f"fits the next {h} best?",
                "constraints": "Spot only: no leverage, no borrowing, no short selling. Buying needs free cash in "
                               "`portfolio`. Selling only reduces bitcoin that is already held.",
            },
            criteria={
                "BUY": "Buy bitcoin with free cash: a rise larger than `costs.round_trip_cost_pct` within the horizon "
                       "looks more likely than not.",
                "HOLD": "Do nothing: there is no clear edge after costs, the evidence is mixed, or the needed "
                        "resources are not available.",
                "SELL": "Sell part of the bitcoin held: a fall within the horizon looks more likely than not.",
            },
        ),
    }
