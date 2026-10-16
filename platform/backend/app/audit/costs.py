"""Fixed monthly costs (infrastructure, paid news) accrued once per UTC day into the cost ledger,
so the dashboard can show Jev, news and infrastructure separately from trading fees."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from ..core.config import Settings
from ..core.models import CostEntry
from . import log as audit


def accrue_fixed_costs(sf, s: Settings, now) -> int:
    day = now.date().isoformat()
    added = 0
    with sf.begin() as session:
        for category, monthly in (("infra", s.infra_cost_eur_per_month), ("news", s.news_cost_eur_per_month)):
            if monthly <= 0:
                continue
            ref = f"{category}:{day}"
            if session.scalar(select(CostEntry.id).where(CostEntry.ref == ref)) is not None:
                continue
            daily = monthly / Decimal(30)
            audit.cost(session, category, daily, "EUR", daily, None, "config (monthly / 30)", ref, now)
            added += 1
    return added
