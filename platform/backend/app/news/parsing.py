"""Feed parsing, URL normalisation and de-duplication keys.

Feed content is untrusted input: XML is parsed with defusedxml (no entity expansion, no external
entities), HTML is stripped to text, control characters removed and lengths capped.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from defusedxml import ElementTree

from ..core.timeutil import UTC, parse_iso

MAX_TITLE = 300
MAX_SUMMARY = 600
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|source$)")
_TAGS = re.compile(r"<[^>]+>")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS = re.compile(r"\s+")
_SPACE_PUNCT = re.compile(r"\s+([.,;:!?])")

ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class RawNews:
    source: str
    url: str
    title: str
    summary: str
    published_at: datetime | None


def clean_text(text: str | None, limit: int) -> str:
    text = html.unescape(_TAGS.sub(" ", text or ""))
    text = _CTRL.sub("", unicodedata.normalize("NFKC", text))
    text = _SPACE_PUNCT.sub(r"\1", _WS.sub(" ", text)).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k.lower())))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", parts.netloc.lower().removeprefix("www."), path, query, ""))


def title_hash(title: str) -> str:
    norm = re.sub(r"[^a-z0-9а-я]+", " ", title.lower()).strip()
    return hashlib.sha256(norm.encode()).hexdigest()


def _date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
        return dt.astimezone(UTC) if dt.tzinfo else None
    except (TypeError, ValueError):
        pass
    try:
        return parse_iso(text)
    except ValueError:
        return None


def parse_feed(xml: bytes, feed_url: str) -> list[RawNews]:
    root = ElementTree.fromstring(xml)
    source = urlsplit(feed_url).netloc.removeprefix("www.")
    items: list[RawNews] = []
    for item in root.iter("item"):  # RSS 2.0
        link = (item.findtext("link") or "").strip()
        title = clean_text(item.findtext("title"), MAX_TITLE)
        if not link or not title:
            continue
        items.append(RawNews(source, link, title, clean_text(item.findtext("description"), MAX_SUMMARY),
                             _date(item.findtext("pubDate"))))
    for entry in root.iter(f"{ATOM}entry"):  # Atom
        link_el = entry.find(f"{ATOM}link")
        link = link_el.attrib.get("href", "") if link_el is not None else ""
        title = clean_text(entry.findtext(f"{ATOM}title"), MAX_TITLE)
        if not link or not title:
            continue
        summary = entry.findtext(f"{ATOM}summary") or entry.findtext(f"{ATOM}content")
        published = entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated")
        items.append(RawNews(source, link, title, clean_text(summary, MAX_SUMMARY), _date(published)))
    return items


RELEVANT = re.compile(
    r"\b(bitcoin|btc|crypto|etf|sec|fed|federal reserve|fomc|powell|interest rate|rate cut|rate hike|inflation|cpi|"
    r"treasury|tariff|halving|miner|mining|binance|coinbase|kraken|tether|usdt|usdc|stablecoin|liquidat\w*|hack\w*|"
    r"exploit|stolen|sanction\w*|regulat\w*|mica|esma|blackrock|whale|outflow\w*|inflow\w*)\b",
    re.IGNORECASE,
)


def is_relevant(title: str, summary: str) -> bool:
    return bool(RELEVANT.search(f"{title} {summary}"))
