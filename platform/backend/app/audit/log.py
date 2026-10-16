"""Append-only audit trail plus latency and cost ledgers.

The database rejects UPDATE/DELETE on audit_log (trigger in the initial migration), so a record
written here is permanent. Payloads must not contain secrets; they pass through `redact` anyway.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from ..core.jsonutil import jsonable
from ..core.models import AuditEntry, CostEntry, LatencyEvent
from ..core.secrets import redact
from ..core.timeutil import utcnow


def record(session: Session, actor: str, kind: str, ref: str | None = None, payload: dict | None = None,
           ts: datetime | None = None) -> AuditEntry:
    clean = json.loads(redact(json.dumps(jsonable(payload or {}))))
    entry = AuditEntry(ts=ts or utcnow(), actor=actor, kind=kind, ref=ref, payload=clean)
    session.add(entry)
    return entry


def latency(session: Session, stage: str, ms: float | Decimal, ref: str | None = None, ts: datetime | None = None) -> None:
    session.add(LatencyEvent(ts=ts or utcnow(), stage=stage, ms=Decimal(str(round(float(ms), 3))), ref=ref))


def cost(session: Session, category: str, amount: Decimal, currency: str, amount_eur: Decimal,
         fx_rate: Decimal | None = None, fx_source: str | None = None, ref: str | None = None,
         ts: datetime | None = None) -> None:
    session.add(CostEntry(ts=ts or utcnow(), category=category, amount=amount, currency=currency,
                          amount_eur=amount_eur, fx_rate=fx_rate, fx_source=fx_source, ref=ref))
