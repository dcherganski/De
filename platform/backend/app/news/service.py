"""Pluggable news providers and the service that stores, de-duplicates and times them.

Providers: `RssProvider` (default, free publisher feeds) and `FixtureNewsProvider` (labelled test
data). Adding another source means implementing `fetch() -> list[RawNews]`.

Every stored item records source, URL, publication time, receipt time and the delay between them.
Duplicates are dropped by normalised URL and, across sources, by a title hash within 48 hours.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from ..audit import log as audit
from ..core.config import Settings
from ..core.models import NewsItem
from ..core.timeutil import parse_iso, utcnow
from .parsing import RawNews, clean_text, is_relevant, normalize_url, parse_feed, title_hash

log = logging.getLogger(__name__)
MAX_FEED_BYTES = 2_000_000


class NewsProvider(Protocol):
    name: str
    is_fixture: bool

    async def fetch(self) -> list[RawNews]: ...


class RssProvider:
    name = "rss"
    is_fixture = False

    def __init__(self, feeds: list[str], timeout_s: float = 10.0, transport: httpx.AsyncBaseTransport | None = None):
        self.feeds = feeds
        self.timeout_s = timeout_s
        self.transport = transport
        self.errors: dict[str, str] = {}

    async def _one(self, client: httpx.AsyncClient, url: str) -> list[RawNews]:
        try:
            r = await client.get(url, headers={"User-Agent": "personal-trading-platform/0.1 (+rss reader)"})
            r.raise_for_status()
            if len(r.content) > MAX_FEED_BYTES:
                raise ValueError("feed too large")
            self.errors.pop(url, None)
            return parse_feed(r.content, url)
        except Exception as exc:
            self.errors[url] = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("feed %s failed: %s", url, self.errors[url])
            return []

    async def fetch(self) -> list[RawNews]:
        async with httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=True, transport=self.transport) as c:
            batches = await asyncio.gather(*(self._one(c, u) for u in self.feeds))
        return [item for batch in batches for item in batch]


class FixtureNewsProvider:
    """Reads labelled test headlines from a JSON-lines file: {"source","url","title","summary","published"}."""

    name = "fixture"
    is_fixture = True

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.errors: dict[str, str] = {}

    async def fetch(self) -> list[RawNews]:
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            d = json.loads(line)
            out.append(RawNews(d["source"], d["url"], clean_text(d["title"], 300), clean_text(d.get("summary"), 600),
                               parse_iso(d["published"]) if d.get("published") else None))
        return out


class NewsService:
    def __init__(self, provider: NewsProvider, sf: sessionmaker[Session], settings: Settings, clock=utcnow):
        self.provider = provider
        self.sf = sf
        self.s = settings
        self.clock = clock
        self.ok: bool | None = None
        self.last_poll_at = None

    @classmethod
    def from_settings(cls, s: Settings, sf: sessionmaker[Session]) -> NewsService:
        if s.news_provider == "fixture":
            return cls(FixtureNewsProvider(s.news_fixture_path), sf, s)
        return cls(RssProvider(s.rss_feeds), sf, s)

    async def poll(self, engine=None) -> list[int]:
        """Fetch, de-duplicate and store. Returns ids of newly stored relevant items."""
        raw = await self.provider.fetch()
        now = self.clock()
        self.last_poll_at = now
        feeds = getattr(self.provider, "feeds", None) or [self.provider.name]
        self.ok = len(getattr(self.provider, "errors", {})) < len(feeds)
        return self.store(raw, now)

    def store(self, raw: list[RawNews], now) -> list[int]:
        new_ids: list[int] = []
        with self.sf.begin() as session:
            recent = set(session.scalars(select(NewsItem.title_hash).where(NewsItem.received_at >= now - timedelta(hours=48))))
            for item in raw:
                th = title_hash(item.title)
                if th in recent:
                    continue
                latency = (now - item.published_at).total_seconds() if item.published_at else None
                relevant = is_relevant(item.title, item.summary)
                stmt = pg_insert(NewsItem).values(
                    provider=self.provider.name, source=item.source, url=item.url, url_norm=normalize_url(item.url),
                    title=item.title, summary=item.summary, title_hash=th, published_at=item.published_at,
                    received_at=now, latency_s=Decimal(str(round(latency, 3))) if latency is not None else None,
                    relevant=relevant, is_fixture=self.provider.is_fixture,
                ).on_conflict_do_nothing(index_elements=["url_norm"]).returning(NewsItem.id)
                row_id = session.execute(stmt).scalar()
                if row_id is None:
                    continue  # same URL seen before
                recent.add(th)
                if relevant:
                    new_ids.append(row_id)
            if new_ids:
                audit.record(session, "news", "news_received", None, {"new_relevant": len(new_ids)})
        return new_ids
