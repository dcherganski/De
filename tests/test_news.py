import json
from datetime import datetime, timezone

import pandas as pd
import pytest

import crypto_bot.news as news_module
from crypto_bot.cli import main
from crypto_bot.data import save_csv
from crypto_bot.news import (
    MARKET,
    JevAuthError,
    JevClient,
    NewsItem,
    asset_news,
    fetch_news,
    headline_questions,
    load_news_csv,
    match_assets,
    parse_rss,
    recent_news,
    score_news,
)
from crypto_bot.report import format_news_report, format_text, render_html, to_payload

NOW = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Feed</title>
<item><title>Solana ETF approved by the SEC</title>
  <link>https://example.com/sol</link>
  <description><![CDATA[<p>Big news for <b>SOL</b> holders &amp; funds.</p>]]></description>
  <pubDate>Tue, 29 Sep 2026 16:00:00 +0000</pubDate></item>
<item><title>Exchange hacked, ETH drained</title>
  <pubDate>Tue, 29 Sep 2026 12:30:00 +0000</pubDate></item>
<item><title>No date here</title></item>
<item><title>Bad date</title><pubDate>yesterday</pubDate></item>
</channel></rss>"""


def item(title, summary="", hours_ago=1.0, source="Test"):
    return NewsItem(title, summary, source, pd.Timestamp(NOW) - pd.Timedelta(hours=hours_ago))


class FakeResponse:
    def __init__(self, payload=None, status=200, content=b""):
        self.payload = payload
        self.status_code = status
        self.content = content
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeJev:
    """Answers like the TypeSafe endpoint: good words -> top level, bad words -> bottom level;
    a subject is relevant when its ticker or name appears in the headline."""

    def __init__(self, fail_first=0, status=200):
        self.headers = {}
        self.bodies = []
        self.fail_first = fail_first
        self.status = status

    def post(self, url, json, headers, timeout):
        assert headers["Authorization"] == "Bearer test-key"
        self.bodies.append(json)
        if self.fail_first:
            self.fail_first -= 1
            return FakeResponse({"detail": "overloaded"}, status=529)
        if self.status != 200:
            return FakeResponse({"detail": "nope"}, status=self.status)
        text = json["state"]["headline"].lower()
        level = 4 if any(w in text for w in ("approved", "surge")) else 0 if "hack" in text else 2
        answers = {}
        for qid, q in json["questions"].items():
            subject = q["instructions"]["subject"]
            if q["type"] == "noul":
                names = [subject.get("ticker", ""), subject.get("name", "")]
                hit = "crypto market" in subject["name"] or any(n and n.lower() in text for n in names)
                answers[qid] = {"type": "noul", "noul": 0.9 if hit else 0.1}
            else:
                answers[qid] = {"type": "score", "score": float(level), "confidence": 0.8,
                                "legend": {}, "probabilities": {}}
        return FakeResponse({"model": "jev-test", "answers": answers, "usage": {"input_tokens": 100}})


def client(session=None):
    return JevClient("test-key", session=session or FakeJev(), pause=0)


def test_parse_rss_cleans_html_and_skips_undated_items():
    items = parse_rss(RSS, "Feed")
    assert [i.title for i in items] == ["Solana ETF approved by the SEC", "Exchange hacked, ETH drained"]
    assert items[0].summary == "Big news for SOL holders & funds."
    assert items[0].published == pd.Timestamp("2026-09-29 16:00", tz="UTC")
    assert items[0].link == "https://example.com/sol"


def test_fetch_news_reports_a_failing_feed():
    class Session:
        headers = {}

        def get(self, url, timeout):
            if "bad" in url:
                raise ConnectionError("down")
            return FakeResponse(content=RSS)

    items, errors = fetch_news({"Good": "https://good/rss", "Bad": "https://bad/rss"}, session=Session())
    assert len(items) == 2 and {i.source for i in items} == {"Good"}
    assert errors == [("Bad", "down")]


def test_load_news_csv(tmp_path):
    path = tmp_path / "news.csv"
    path.write_text("date,title,source\n2026-09-29T10:00:00Z,Bitcoin rallies,Wire\n2026-09-29,,Wire\n")
    items = load_news_csv(path)
    assert len(items) == 1 and items[0].source == "Wire"
    assert items[0].published == pd.Timestamp("2026-09-29 10:00", tz="UTC")
    (tmp_path / "bad.csv").write_text("when,title\n1,x\n")
    with pytest.raises(ValueError):
        load_news_csv(tmp_path / "bad.csv")


def test_recent_news_window_dedup_and_limit():
    items = [item("Old story", hours_ago=30), item("Fresh story", hours_ago=2), item("Fresh  story!", hours_ago=3),
             item("Newest", hours_ago=0.5), item("From the future", hours_ago=-1)]
    assert [i.title for i in recent_news(items, hours=24, now=NOW)] == ["Newest", "Fresh story"]
    assert [i.title for i in recent_news(items, hours=24, limit=1, now=NOW)] == ["Newest"]
    assert len(recent_news(items, hours=0, now=NOW)) == 3  # no window, still no duplicates or future


def test_match_assets_by_ticker_and_name():
    products = ["BTC-USD", "ETH-USD", "NEAR-USD", "XLM-USD", "OP-USD"]
    assert match_assets(item("Bitcoin tops $100K"), products) == ["BTC-USD"]
    assert match_assets(item("$ETH and btc"), products) == ["ETH-USD"]  # tickers are upper case only
    assert match_assets(item("Rates near record"), products) == []  # the word, not the NEAR ticker
    assert match_assets(item("A stellar week for stocks"), products) == []  # common word: case-sensitive name
    assert match_assets(item("Stellar partners with a bank"), products) == ["XLM-USD"]
    assert match_assets(item("Tether mints more"), products) == []  # "ether" inside a word
    assert match_assets(item("OP Mainnet upgrade"), products) == ["OP-USD"]


def test_headline_questions_shape():
    q = headline_questions([MARKET, "SOL-USD"])
    assert set(q) == {"MARKET|relevant", "MARKET|sentiment", "SOL-USD|relevant", "SOL-USD|sentiment"}
    assert q["SOL-USD|relevant"]["type"] == "noul"
    assert q["SOL-USD|sentiment"]["type"] == "score" and len(q["SOL-USD|sentiment"]["criteria"]) == 5
    assert q["SOL-USD|sentiment"]["instructions"]["subject"] == {"ticker": "SOL", "name": "Solana"}


def test_score_news_moods():
    items = [item("Solana ETF approved"), item("Solana exchange hack"), item("Solana surge continues"),
             item("Bitcoin mining report"), item("Weather is nice")]
    fake = FakeJev()
    report = score_news(items, ["BTC-USD", "SOL-USD", "ETH-USD"], client(fake), jobs=1)
    sol = report["assets"]["SOL-USD"]
    assert sol["n"] == 3 and sol["positive"] == 2 and sol["negative"] == 1
    assert sol["mood"] == pytest.approx(1 / 3) and sol["label"] == "положително"
    assert sol["headlines"][0]["title"] in {"Solana ETF approved", "Solana exchange hack", "Solana surge continues"}
    btc = report["assets"]["BTC-USD"]
    assert btc["n"] == 1 and btc["mood"] == 0 and btc["label"] == "неутрално"
    eth = report["assets"]["ETH-USD"]
    assert eth["n"] == 0 and eth["mood"] is None and eth["label"] == "няма новини"
    assert report["market"]["n"] == 5  # every headline also gets the market questions
    assert report["model"] == "jev-test" and report["input_tokens"] == 500
    # Only matched assets are asked about: the weather headline carries the market questions only.
    weather = next(b for b in fake.bodies if b["state"]["headline"] == "Weather is nice")
    assert set(weather["questions"]) == {"MARKET|relevant", "MARKET|sentiment"}
    json.dumps(report)


def test_score_news_parallel_matches_serial():
    items = [item(f"Solana surge {i}") for i in range(6)] + [item("Bitcoin hack")]
    serial = score_news(items, ["BTC-USD", "SOL-USD"], client(), jobs=1)
    parallel = score_news(items, ["BTC-USD", "SOL-USD"], client(), jobs=4)
    for key in ("BTC-USD", "SOL-USD"):
        assert parallel["assets"][key]["mood"] == pytest.approx(serial["assets"][key]["mood"])


def test_jev_client_retries_overload_then_succeeds():
    fake = FakeJev(fail_first=2)
    answers = client(fake).ask({"headline": "Solana surge"}, headline_questions(["SOL-USD"]))
    assert answers["SOL-USD|sentiment"]["score"] == 4.0
    assert len(fake.bodies) == 3


def test_jev_client_auth_errors():
    with pytest.raises(JevAuthError):
        JevClient("")
    with pytest.raises(JevAuthError):
        score_news([item("Solana surge")], ["SOL-USD"], client(FakeJev(status=401)), jobs=1)


def test_a_failed_headline_does_not_stop_the_rest():
    fake = FakeJev(status=422)
    report = score_news([item("Solana surge")], ["SOL-USD"], client(fake), jobs=1)
    assert report["assets"]["SOL-USD"]["n"] == 0
    assert len(report["failed"]) == 1 and "422" in report["failed"][0]["error"]


def test_news_in_reports(monkeypatch, random_walk):
    from tests.test_bot import run_bot

    result = run_bot(monkeypatch, random_walk, backtest=False)
    report = score_news([item("Bitcoin ETF approved"), item("Bitcoin hack")], ["BTC-USD"], client(), jobs=1)
    result.news = asset_news(report, "BTC-USD")
    text = format_text(result)
    assert "Настроение в новините (jev-test" in text and "Пазарът като цяло" in text
    payload = to_payload(result)
    assert payload["news"]["asset"]["n"] == 2
    json.dumps(payload)
    assert '"n_headlines": 2' in render_html(result)  # the news reaches the dashboard's data
    assert "BTC-USD" in format_news_report(report)


def _news_csv(tmp_path):
    path = tmp_path / "news.csv"
    path.write_text(
        "date,title,source,link\n"
        "2026-09-29T16:00:00Z,Bitcoin ETF approved,Wire,https://example.com/a\n"
        "2026-09-29T15:00:00Z,Ethereum exchange hack,Wire,\n"
    )
    return path


def test_cli_news_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(news_module.requests, "Session", FakeJev)
    code = main(["news", "--product", "BTC-USD,ETH-USD", "--news-file", str(_news_csv(tmp_path)),
                 "--news-hours", "0", "--json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["assets"]["BTC-USD"]["mood"] == 1.0
    assert out["assets"]["ETH-USD"]["mood"] == -1.0


def test_cli_news_without_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    code = main(["news", "--product", "BTC-USD", "--news-file", str(_news_csv(tmp_path))])
    assert code == 1
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err


def test_cli_predict_with_news(tmp_path, random_walk, monkeypatch, capsys):
    save_csv(random_walk, tmp_path / "BTC-USD_1d.csv")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(news_module.requests, "Session", FakeJev)
    code = main(["predict", "--product", "BTC-USD", "--offline", "--cache-dir", str(tmp_path), "--no-backtest",
                 "--min-train", "120", "--json", "--news", "--news-file", str(_news_csv(tmp_path)),
                 "--news-hours", "0"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["news"]["asset"]["mood"] == 1.0 and out["news"]["market"]["n"] == 2


def test_cli_predict_news_without_key_still_forecasts(tmp_path, random_walk, monkeypatch, capsys):
    save_csv(random_walk, tmp_path / "BTC-USD_1d.csv")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    code = main(["predict", "--product", "BTC-USD", "--offline", "--cache-dir", str(tmp_path), "--no-backtest",
                 "--min-train", "120", "--json", "--news", "--news-file", str(_news_csv(tmp_path))])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["news"] is None
    assert "Новините са пропуснати" in captured.err
