"""News sentiment: crypto headlines judged by Jev, TypeSafe's System One model.

Headlines come from public RSS feeds (no key needed) or from a CSV file. Code finds which
watched assets a headline may be about (ticker or name in the text); Jev then judges, for
each candidate, whether the news is really about that asset and how good or bad it is for
its price. The crypto market as a whole is judged the same way.

The mood is shown next to the forecast but does not feed the models or the signal: there
is no news archive to backtest it against, and the bot only acts on proven edges.
"""

from __future__ import annotations

import html
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import pandas as pd
import requests

NEWS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
}
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
API_KEY_ENV = "TYPESAFE_API_KEY"
MARKET = "MARKET"  # pseudo-asset: the crypto market as a whole
MOOD_THRESHOLD = 0.25  # |mood| below this reads as neutral

# Names that appear in headlines, besides the ticker. Common English words are matched
# case-sensitively (CASE_SENSITIVE_NAMES) or left out, and ambiguous tickers (DASH, NEAR,
# SKY) rely on the upper-case ticker. A false match only costs a question: Jev's relevance
# judgment filters it out.
ASSET_NAMES = {
    "BTC": ("Bitcoin",),
    "ETH": ("Ethereum", "Ether"),
    "ZEC": ("Zcash",),
    "XRP": ("Ripple",),
    "SOL": ("Solana",),
    "NEAR": ("NEAR Protocol",),
    "HYPE": ("Hyperliquid",),
    "LTC": ("Litecoin",),
    "BCH": ("Bitcoin Cash",),
    "SUI": ("Sui",),
    "DOGE": ("Dogecoin",),
    "ADA": ("Cardano",),
    "LINK": ("Chainlink",),
    "XLM": ("Stellar",),
    "AVAX": ("Avalanche",),
    "TAO": ("Bittensor",),
    "VVV": ("Venice Token",),
    "HBAR": ("Hedera",),
    "USELESS": ("Useless Coin",),
    "ONDO": ("Ondo",),
    "LIGHTER": ("Lighter",),
    "PENGU": ("Pudgy Penguins",),
    "ENA": ("Ethena",),
    "AERO": ("Aerodrome",),
    "ARB": ("Arbitrum",),
    "BONK": ("Bonk",),
    "PUMP": ("Pump.fun",),
    "PEPE": ("Pepe",),
    "INJ": ("Injective",),
    "ICP": ("Internet Computer",),
    "DRV": ("Derive",),
    "FET": ("Fetch.ai", "ASI Alliance"),
    "FARTCOIN": ("Fartcoin",),
    "DOT": ("Polkadot",),
    "MON": ("Monad",),
    "WLD": ("Worldcoin",),
    "CRV": ("Curve Finance",),
    "TIA": ("Celestia",),
    "APT": ("Aptos",),
    "GFI": ("Goldfinch",),
    "RENDER": ("Render Network",),
    "MET": ("Meteora",),
    "SPX": ("SPX6900",),
    "SEI": ("Sei",),
    "ALLO": ("Allora",),
    "STRK": ("Starknet",),
    "ALGO": ("Algorand",),
    "OP": ("Optimism",),
    "VET": ("VeChain",),
    "ATOM": ("Cosmos",),
    "STX": ("Stacks",),
    "FLR": ("Flare Network",),
    "CAKE": ("PancakeSwap",),
}
CASE_SENSITIVE_NAMES = frozenset(
    {"Stellar", "Avalanche", "Lighter", "Pepe", "Derive", "Optimism", "Cosmos", "Stacks", "Bonk"}
)

SENTIMENT_LEVELS = [
    "Clearly bad for the price: hack or exploit, ban, lawsuit, delisting, large sell-off or outflows",
    "Somewhat bad for the price",
    "Neutral or mixed: a factual update with no clear effect on the price",
    "Somewhat good for the price",
    "Clearly good for the price: adoption, approval, listing, major partnership, large inflows",
]


class JevError(RuntimeError):
    pass


class JevAuthError(JevError):
    pass


@dataclass
class NewsItem:
    title: str
    summary: str
    source: str
    published: pd.Timestamp
    link: str = ""


# ---------- headlines ----------


def _plain(text: str | None, limit: int = 600) -> str:
    """Strip HTML tags and entities, collapse whitespace, cap the length."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def parse_rss(xml_text: str | bytes, source: str) -> list[NewsItem]:
    """Items of an RSS 2.0 feed; entries without a title or a readable date are skipped."""
    items = []
    for node in ET.fromstring(xml_text).iter("item"):
        title = _plain(node.findtext("title"))
        raw_date = node.findtext("pubDate")
        if not title or not raw_date:
            continue
        try:
            published = _utc(parsedate_to_datetime(raw_date.strip()))
        except (TypeError, ValueError):
            continue
        items.append(
            NewsItem(
                title=title,
                summary=_plain(node.findtext("description")),
                source=source,
                published=published,
                link=(node.findtext("link") or "").strip(),
            )
        )
    return items


def fetch_news(
    feeds: dict[str, str] | None = None, session: requests.Session | None = None, timeout: float = 15.0
) -> tuple[list[NewsItem], list[tuple[str, str]]]:
    """Headlines from every feed; a feed that fails is reported instead of stopping the rest."""
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", "crypto-bot/0.1")
    items, errors = [], []
    for source, url in (feeds or NEWS_FEEDS).items():
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            items.extend(parse_rss(resp.content, source))
        except Exception as exc:  # network error, bad XML
            errors.append((source, str(exc)))
    return items, errors


def load_news_csv(path: str | Path) -> list[NewsItem]:
    """Headlines from a CSV with columns date,title and optionally summary,source,link."""
    raw = pd.read_csv(path).fillna("")
    missing = {"date", "title"} - set(raw.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    return [
        NewsItem(
            title=_plain(str(row["title"])),
            summary=_plain(str(row.get("summary", ""))),
            source=str(row.get("source", "")) or Path(path).name,
            published=_utc(pd.to_datetime(row["date"], utc=True)),
            link=str(row.get("link", "")),
        )
        for _, row in raw.iterrows()
        if str(row["title"]).strip()
    ]


def recent_news(
    items: list[NewsItem], hours: float = 24, limit: int = 40, now: datetime | None = None
) -> list[NewsItem]:
    """Newest first, inside the time window (hours <= 0: no window), the same story only once."""
    now = _utc(now or datetime.now(timezone.utc))
    start = now - timedelta(hours=hours) if hours > 0 else None
    seen, out = set(), []
    for item in sorted(items, key=lambda i: i.published, reverse=True):
        if item.published > now or (start is not None and item.published < start):
            continue
        key = re.sub(r"\W+", " ", item.title.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[:limit] if limit > 0 else out


def ticker(product: str) -> str:
    return product.split("-", 1)[0].upper()


def _asset_patterns(product: str) -> list[re.Pattern]:
    sym = re.escape(ticker(product))
    patterns = [re.compile(rf"(?<![A-Za-z0-9])\$?{sym}(?![A-Za-z0-9])")]  # ticker: upper case only
    for name in ASSET_NAMES.get(ticker(product), ()):
        flags = 0 if name in CASE_SENSITIVE_NAMES else re.IGNORECASE
        patterns.append(re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", flags))
    return patterns


def match_assets(item: NewsItem, products: list[str]) -> list[str]:
    """Products whose ticker or name appears in the headline or its summary."""
    text = f"{item.title}\n{item.summary}"
    return [p for p in products if any(pat.search(text) for pat in _asset_patterns(p))]


# ---------- Jev ----------


class JevClient:
    """Minimal client for the TypeSafe System One endpoint."""

    def __init__(
        self,
        api_key: str,
        session: requests.Session | None = None,
        model: str = JEV_MODEL,
        timeout: float = 30.0,
        retries: int = 4,
        pause: float = 1.0,
    ):
        if not api_key:
            raise JevAuthError(f"липсва API ключ за TypeSafe (променлива {API_KEY_ENV})")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.pause = pause
        self._session = session
        self._local = threading.local()
        self.served_model: str | None = None
        self.input_tokens = 0

    @classmethod
    def from_env(cls, **kwargs) -> "JevClient":
        return cls(os.environ.get(API_KEY_ENV, "").strip(), **kwargs)

    def _http(self) -> requests.Session:
        if self._session is not None:
            return self._session
        if not hasattr(self._local, "session"):  # requests sessions are not shared across threads
            self._local.session = requests.Session()
        return self._local.session

    def ask(self, state, questions: dict) -> dict:
        """Answers keyed by question id. Retries 429/529/5xx with exponential backoff."""
        body = {"state": state, "model": self.model, "questions": questions}
        headers = {"Authorization": f"Bearer {self.api_key}", "User-Agent": "crypto-bot/0.1"}
        for attempt in range(self.retries + 1):
            resp = self._http().post(TYPESAFE_URL, json=body, headers=headers, timeout=self.timeout)
            if resp.status_code in (401, 403):
                raise JevAuthError(f"TypeSafe отказа API ключа от {API_KEY_ENV} (HTTP {resp.status_code})")
            if resp.status_code in (429, 500, 502, 503, 504, 529) and attempt < self.retries:
                time.sleep(self.pause * 2**attempt)
                continue
            if resp.status_code >= 400:
                raise JevError(f"TypeSafe HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            self.served_model = data.get("model", self.served_model)
            self.input_tokens += int((data.get("usage") or {}).get("input_tokens", 0))
            return data["answers"]
        raise JevError("TypeSafe: изчерпани опити")  # pragma: no cover - loop always returns or raises


def _subject(product: str) -> dict:
    if product == MARKET:
        return {"name": "the crypto market as a whole (Bitcoin and the major coins)"}
    sym = ticker(product)
    names = ASSET_NAMES.get(sym)
    return {"ticker": sym, "name": names[0]} if names else {"ticker": sym}


def headline_questions(products: list[str]) -> dict:
    """Two judgments per subject, asked together over the same headline."""
    questions = {}
    for product in products:
        subject = _subject(product)
        if product == MARKET:
            relevant = "Is this news likely to move the crypto market as a whole, not just one project or company?"
            sentiment = "How good or bad is this news for the prices of `subject` over the next few days?"
        else:
            relevant = ("Is this news mainly about `subject`, or does it directly affect its price, "
                        "rather than mentioning it in passing?")
            sentiment = "How good or bad is this news for the price of `subject` over the next few days?"
        questions[f"{product}|relevant"] = {
            "type": "noul",
            "instructions": {"subject": subject, "question": relevant},
            "criteria": {
                "true": "The news is about the subject or clearly affects its price",
                "false": "The subject is only mentioned in passing, or the news is about something else",
            },
        }
        questions[f"{product}|sentiment"] = {
            "type": "score",
            "instructions": {"subject": subject, "question": sentiment},
            "criteria": SENTIMENT_LEVELS,
        }
    return questions


def _headline_state(item: NewsItem) -> dict:
    state = {"source": item.source, "published": item.published.isoformat(), "headline": item.title}
    if item.summary:
        state["summary"] = item.summary
    return state


@dataclass
class Judgment:
    product: str
    item: NewsItem
    relevance: float  # probability the news is about the product
    sentiment: float  # -1 clearly bad ... +1 clearly good for the price
    confidence: float


def judge_headline(client: JevClient, item: NewsItem, products: list[str]) -> list[Judgment]:
    subjects = [MARKET, *products]
    answers = client.ask(_headline_state(item), headline_questions(subjects))
    top = len(SENTIMENT_LEVELS) - 1
    out = []
    for product in subjects:
        rel = answers[f"{product}|relevant"]
        sen = answers[f"{product}|sentiment"]
        out.append(
            Judgment(
                product=product,
                item=item,
                relevance=float(rel["noul"]),
                sentiment=float(sen["score"]) / top * 2 - 1,
                confidence=float(sen.get("confidence", 0.0)),
            )
        )
    return out


def mood_label(mood: float | None) -> str:
    if mood is None:
        return "няма новини"
    if mood >= MOOD_THRESHOLD:
        return "положително"
    if mood <= -MOOD_THRESHOLD:
        return "отрицателно"
    return "неутрално"


def summarize(judgments: list[Judgment], min_relevance: float = 0.5, top: int = 5) -> dict:
    """Relevance-weighted mood of the headlines that are really about the subject."""
    kept = [j for j in judgments if j.relevance >= min_relevance]
    weight = sum(j.relevance for j in kept)
    mood = sum(j.relevance * j.sentiment for j in kept) / weight if weight else None
    strongest = sorted(kept, key=lambda j: j.relevance * abs(j.sentiment), reverse=True)[:top]
    return {
        "mood": mood,
        "label": mood_label(mood),
        "n": len(kept),
        "positive": sum(1 for j in kept if j.sentiment >= MOOD_THRESHOLD),
        "negative": sum(1 for j in kept if j.sentiment <= -MOOD_THRESHOLD),
        "headlines": [
            {
                "title": j.item.title,
                "source": j.item.source,
                "published": j.item.published.isoformat(),
                "link": j.item.link,
                "sentiment": j.sentiment,
                "relevance": j.relevance,
                "confidence": j.confidence,
            }
            for j in strongest
        ],
    }


def score_news(
    items: list[NewsItem],
    products: list[str],
    client: JevClient,
    jobs: int = 4,
    min_relevance: float = 0.5,
    window_hours: float = 24,
) -> dict:
    """Judge every headline (in parallel) and summarise the mood per product and for the market."""
    work = [(item, match_assets(item, products)) for item in items]
    failures: list[tuple[str, str]] = []

    def one(job):
        item, matched = job
        try:
            return judge_headline(client, item, matched)
        except JevAuthError:
            raise
        except Exception as exc:  # one bad request should not lose the other headlines
            failures.append((item.title, str(exc)))
            return []

    if jobs > 1 and len(work) > 1:
        with ThreadPoolExecutor(max_workers=min(jobs, len(work))) as pool:
            batches = list(pool.map(one, work))
    else:
        batches = [one(job) for job in work]
    judgments = [j for batch in batches for j in batch]
    by_product: dict[str, list[Judgment]] = {p: [] for p in [MARKET, *products]}
    for j in judgments:
        by_product[j.product].append(j)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": client.served_model or client.model,
        "window_hours": window_hours,
        "n_headlines": len(items),
        "input_tokens": client.input_tokens,
        "min_relevance": min_relevance,
        "market": summarize(by_product[MARKET], min_relevance),
        "assets": {p: summarize(by_product[p], min_relevance) for p in products},
        "failed": [{"title": title, "error": err} for title, err in failures],
    }


def asset_news(report: dict, product: str) -> dict:
    """The slice of a news report that belongs next to one asset's forecast."""
    return {
        "model": report["model"],
        "generated_at": report["generated_at"],
        "window_hours": report["window_hours"],
        "n_headlines": report["n_headlines"],
        "asset": report["assets"].get(product) or summarize([]),
        "market": report["market"],
    }
