from __future__ import annotations

import html
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .core.logging import get_logger
from .events import EconomicEvent


logger = get_logger(__name__)

MODEL = "gemini-3.6-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
)
GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

TRUSTED_SOURCES = {
    "Reuters",
    "Bloomberg",
    "CNBC",
    "Associated Press",
    "AP News",
    "Financial Times",
    "The Wall Street Journal",
    "U.S. Bureau of Labor Statistics",
    "Bureau of Labor Statistics",
    "Federal Reserve",
    "Federal Reserve Board",
    "Bank of Japan",
    "European Central Bank",
    "Bank of Korea",
    "한국은행",
    "Institute for Supply Management",
    "U.S. Bureau of Economic Analysis",
    "Bureau of Economic Analysis",
    "Yonhap News Agency",
    "연합뉴스",
}

EVENT_SPECS = (
    {
        "match": "미국 CPI",
        "display": "미국 CPI",
        "query": "US CPI inflation core CPI actual forecast Reuters CNBC",
        "country": "USD",
        "metrics": (
            ("헤드라인 CPI MoM", "CPI m/m"),
            ("헤드라인 CPI YoY", "CPI y/y"),
            ("근원 CPI MoM", "Core CPI m/m"),
            ("근원 CPI YoY", "Core CPI y/y"),
        ),
    },
    {
        "match": "미국 PPI",
        "display": "미국 PPI",
        "query": "US PPI producer prices actual forecast Reuters CNBC",
        "country": "USD",
        "metrics": (
            ("PPI MoM", "PPI m/m"),
            ("근원 PPI MoM", "Core PPI m/m"),
        ),
    },
    {
        "match": "고용보고서",
        "display": "미국 고용보고서",
        "query": "US jobs report nonfarm payrolls unemployment actual forecast Reuters CNBC",
        "country": "USD",
        "metrics": (
            ("비농업 고용", "Non-Farm Employment Change"),
            ("실업률", "Unemployment Rate"),
            ("평균 시간당 임금 MoM", "Average Hourly Earnings m/m"),
        ),
    },
    {
        "match": "FOMC",
        "display": "FOMC 금리 결정",
        "query": "Federal Reserve FOMC rate decision actual expected previous Reuters CNBC",
        "country": "USD",
        "metrics": (("정책금리", "Federal Funds Rate"),),
    },
    {
        "match": "BOJ",
        "display": "BOJ 통화정책 결정",
        "query": "Bank of Japan BOJ policy rate decision actual expected Reuters",
        "country": "JPY",
        "metrics": (("정책금리", "BOJ Policy Rate"),),
    },
    {
        "match": "ECB",
        "display": "ECB 통화정책 결정",
        "query": "ECB rate decision deposit rate actual expected Reuters",
        "country": "EUR",
        "metrics": (
            ("예금금리", "Deposit Facility Rate"),
            ("기준금리", "Main Refinancing Rate"),
        ),
    },
    {
        "match": "한국은행",
        "display": "한국은행 금융통화위원회",
        "query": "한국은행 기준금리 금융통화위원회 결정 예상 연합뉴스 Reuters",
        "country": "KRW",
        "metrics": (("기준금리", "Base Rate"),),
    },
    {
        "match": "ISM 제조업",
        "display": "미국 ISM 제조업 PMI",
        "query": "US ISM manufacturing PMI actual forecast Reuters CNBC",
        "country": "USD",
        "metrics": (("ISM 제조업 PMI", "ISM Manufacturing PMI"),),
    },
    {
        "match": "ISM 서비스업",
        "display": "미국 ISM 서비스업 PMI",
        "query": "US ISM services PMI actual forecast Reuters CNBC",
        "country": "USD",
        "metrics": (("ISM 서비스업 PMI", "ISM Services PMI"),),
    },
    {
        "match": "PCE",
        "display": "미국 PCE 물가",
        "query": "US PCE inflation core PCE actual forecast Reuters CNBC",
        "country": "USD",
        "metrics": (
            ("근원 PCE MoM", "Core PCE Price Index m/m"),
            ("PCE MoM", "PCE Price Index m/m"),
        ),
    },
)


def supports_event_result(title: str) -> bool:
    return _event_spec(title) is not None


def build_recent_event_result_section(
    events: list[EconomicEvent],
    *,
    max_events: int = 2,
) -> str:
    if not events:
        return ""

    expectations = _fetch_ff_calendar()
    rendered = []

    for event in sorted(events, key=lambda item: item.event_date, reverse=True)[:max_events]:
        spec = _event_spec(event.title)
        if spec is None:
            continue

        expected_rows = _match_expectations(event, spec, expectations)
        news = _fetch_event_news(event, spec)
        result = _extract_result(event, spec, expected_rows, news)

        if result and result.get("status") == "released":
            block = _format_result_block(event, spec, result)
            if block:
                rendered.append(block)

    if not rendered:
        return ""

    return "[발표 결과]\n" + "\n\n".join(rendered)


def insert_event_result_section(analysis: str, result_section: str) -> str:
    if not result_section:
        return analysis

    for marker in ("[주요 일정]", "[체크 포인트]"):
        if marker in analysis:
            return analysis.replace(marker, f"{result_section}\n\n{marker}", 1)

    return f"{analysis}\n\n{result_section}"


def _event_spec(title: str):
    for spec in EVENT_SPECS:
        if spec["match"] in title:
            return spec
    return None


def _fetch_ff_calendar() -> list[dict]:
    request = Request(
        FF_CALENDAR_URL,
        headers={"User-Agent": "Mozilla/5.0 (Macro-Pulse/1.0)"},
    )

    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, list) else []
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("ForexFactory calendar unavailable: %s", exc)
        return []


def _match_expectations(event: EconomicEvent, spec, entries: list[dict]) -> list[dict]:
    rows = []
    country = str(spec.get("country", ""))

    for label, ff_title in spec.get("metrics", ()):
        match = None
        for entry in entries:
            if str(entry.get("country", "")) != country:
                continue
            if str(entry.get("title", "")).casefold() != ff_title.casefold():
                continue
            entry_date = _parse_iso_date(str(entry.get("date", "")))
            if entry_date and abs((entry_date - event.event_date).days) <= 1:
                match = entry
                break

        rows.append(
            {
                "label": label,
                "calendar_title": ff_title,
                "forecast": str((match or {}).get("forecast") or ""),
                "previous": str((match or {}).get("previous") or ""),
            }
        )

    return rows


def _parse_iso_date(raw: str) -> date | None:
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _fetch_event_news(event: EconomicEvent, spec) -> list[dict]:
    queries = [
        f"{spec['query']} when:3d",
        *[
            f'"{calendar_title}" actual {event.event_date:%B %Y} Reuters CNBC when:3d'
            for _, calendar_title in spec.get("metrics", ())
        ],
    ]

    articles = []
    seen = set()

    for query in queries:
        params = {
            "q": query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
        request = Request(
            f"{GOOGLE_NEWS_URL}?{urlencode(params)}",
            headers={"User-Agent": "Mozilla/5.0 (Macro-Pulse/1.0)"},
        )

        try:
            with urlopen(request, timeout=15) as response:
                root = ET.fromstring(response.read())
        except (HTTPError, URLError, TimeoutError, ET.ParseError) as exc:
            logger.warning("Event news query failed for %s: %s", event.title, exc)
            continue

        for item in root.findall(".//item")[:8]:
            title = (item.findtext("title") or "").strip()
            source = (item.findtext("source") or "").strip()
            description = _clean_html(item.findtext("description") or "")

            if not title or not _trusted_source(source):
                continue

            key = title.casefold()
            if key in seen:
                continue
            seen.add(key)
            articles.append(
                {
                    "title": title,
                    "source": source,
                    "description": description,
                }
            )

    return articles[:18]


def _trusted_source(source: str) -> bool:
    normalized = source.strip().casefold()
    return any(normalized == item.casefold() for item in TRUSTED_SOURCES)


def _clean_html(value: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", html.unescape(value))
    return re.sub(r"\s+", " ", plain).strip()


def _extract_result(
    event: EconomicEvent,
    spec,
    expectations: list[dict],
    news: list[dict],
) -> dict | None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or not news:
        return None

    evidence = "\n".join(
        f"- {item['title']} | {item['source']} | {item['description']}" for item in news
    )
    expectation_text = json.dumps(expectations, ensure_ascii=False)

    prompt = f"""
당신은 경제지표 발표 결과를 검증하는 데이터 에디터입니다.

이벤트: {event.title}
이벤트 날짜: {event.event_date.isoformat()}
공식 일정 출처: {event.source}

[사전 예상/이전 값]
{expectation_text}

[신뢰 매체 및 공식기관 뉴스 증거]
{evidence}

규칙:
1. 발표가 아직 이뤄지지 않았거나 실제값을 확인할 근거가 없으면 status를 pending으로 작성하세요.
2. 실제값(actual)은 위 뉴스 증거에 명시된 값만 사용하세요. 추정하거나 예상값을 실제값으로 복사하지 마세요.
3. 예상값(forecast)과 이전값(previous)은 사전 예상/이전 값에 있는 값을 우선 그대로 사용하세요.
4. 수정치가 기사에 명시된 경우 previous에 수정치를 반영할 수 있습니다.
5. CPI는 가능하면 헤드라인 MoM/YoY, 근원 MoM/YoY 4개를 모두 작성하세요.
6. 숫자 부호와 단위(%, K, M, bp)를 보존하세요.
7. 해석은 예상 대비 상회/부합/하회가 시장에 의미하는 바만 1문장으로 작성하세요.
8. 제공된 증거 밖의 사실을 만들지 마세요.

JSON만 반환하세요. 형식:
{{
  "status": "released" 또는 "pending",
  "title": "발표명",
  "metrics": [
    {{"label": "항목명", "actual": "실제", "forecast": "예상", "previous": "이전"}}
  ],
  "summary": "한 문장 해석",
  "sources": ["Reuters", "U.S. Bureau of Labor Statistics"]
}}
""".strip()

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 1800,
        },
    }
    request = Request(
        GEMINI_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.warning("Gemini event result HTTP error %s: %s", exc.code, body[:500])
        return None
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Gemini event result request failed: %s", exc)
        return None

    candidates = result.get("candidates", [])
    if not candidates:
        return None

    text = "\n".join(
        part.get("text", "")
        for part in candidates[0].get("content", {}).get("parts", [])
        if part.get("text")
    ).strip()
    if not text:
        return None

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned).strip()

    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        logger.warning("Gemini event result returned invalid JSON: %s", text[:500])
        return None


def _format_result_block(event: EconomicEvent, spec, result: dict) -> str:
    title = str(result.get("title") or spec.get("display") or event.title)
    metrics = result.get("metrics") or []
    lines = [title]

    for metric in metrics:
        actual = str(metric.get("actual") or "").strip()
        if not actual:
            continue
        forecast = str(metric.get("forecast") or "-").strip() or "-"
        previous = str(metric.get("previous") or "-").strip() or "-"
        label = str(metric.get("label") or "지표").strip()
        lines.append(
            f"• {label}: 실제 {actual} / 예상 {forecast} / 이전 {previous}"
        )

    if len(lines) == 1:
        return ""

    summary = str(result.get("summary") or "").strip()
    if summary:
        lines.append(f"• 해석: {summary}")

    sources = [
        str(item).strip()
        for item in (result.get("sources") or [])
        if str(item).strip()
    ]
    if sources:
        unique_sources = list(dict.fromkeys(sources))
        lines.append("• 출처: " + ", ".join(unique_sources[:3]))

    return "\n".join(lines)
