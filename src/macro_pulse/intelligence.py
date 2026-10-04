from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .core.logging import get_logger


logger = get_logger(__name__)

MODEL = "gemini-3.6-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
)
GOOGLE_NEWS_URL = "https://news.google.com/rss/search"

TRUSTED_MEDIA = (
    "Reuters",
    "Bloomberg",
    "Associated Press",
    "The Associated Press",
    "AP",
    "AP News",
    "CNBC",
    "Financial Times",
    "The Wall Street Journal",
)

OFFICIAL_SOURCES = (
    "Federal Reserve",
    "Federal Reserve Board",
    "Board of Governors of the Federal Reserve System",
    "U.S. Department of the Treasury",
    "U.S. Treasury",
    "Bureau of Labor Statistics",
    "U.S. Bureau of Labor Statistics",
    "Bureau of Economic Analysis",
    "U.S. Bureau of Economic Analysis",
    "Energy Information Administration",
    "U.S. Energy Information Administration",
    "European Central Bank",
    "Bank of Japan",
    "Bank of Korea",
    "한국은행",
    "Korea Exchange",
    "한국거래소",
    "SEC.gov",
    "U.S. Securities and Exchange Commission",
)

KR_SUPPLEMENTAL_SOURCES = ("연합뉴스", "Yonhap News Agency")

SOURCE_ALIASES = {
    "Reuters": ("Reuters", "Reuters.com", "로이터"),
    "Bloomberg": ("Bloomberg", "Bloomberg.com", "블룸버그"),
    "Financial Times": (
        "Financial Times",
        "FT",
        "FT.com",
        "파이낸셜타임스",
    ),
    "The Wall Street Journal": (
        "The Wall Street Journal",
        "Wall Street Journal",
        "WSJ",
        "WSJ.com",
    ),
    "CNBC": ("CNBC", "CNBC.com"),
    "Associated Press": (
        "Associated Press",
        "The Associated Press",
        "AP",
        "AP News",
        "APNews.com",
    ),
    "Yonhap News Agency": ("Yonhap News Agency", "연합뉴스"),
}

TOPIC_ALIASES = {
    "oil": ("oil", "crude", "wti", "유가", "원유"),
    "middle_east": (
        "middle east",
        "iran",
        "israel",
        "중동",
        "이란",
        "이스라엘",
    ),
    "inflation": ("inflation", "price pressure", "물가", "인플레이션"),
    "rates": (
        "treasury yield",
        "bond yield",
        "interest rate",
        "국채금리",
        "국채 금리",
        "금리",
    ),
    "fed": ("federal reserve", "fed", "fomc", "연준"),
    "boj": ("bank of japan", "boj", "일본은행"),
    "korea": ("korea", "kospi", "한국", "코스피"),
    "japan": ("japan", "nikkei", "일본", "닛케이"),
    "semiconductor": (
        "semiconductor",
        "chip",
        "nvidia",
        "broadcom",
        "반도체",
        "엔비디아",
        "브로드컴",
    ),
    "earnings": ("earnings", "results", "실적"),
    "currency": ("dollar", "yen", "won", "달러", "엔화", "원화", "환율"),
}

BLOCKED_SOURCE_TOKENS = (
    "네이트",
    "블로그",
    "blog",
    "머니투데이방송",
    "mtn",
    "유튜브",
    "youtube",
    "커뮤니티",
)

TITLE_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "for",
    "as",
    "at",
    "from",
    "with",
    "after",
    "before",
    "market",
    "markets",
    "stocks",
    "stock",
    "today",
    "says",
    "코스피",
    "증시",
    "시장",
    "관련",
    "대한",
    "따른",
}


def _query_spec(name: str) -> tuple[str, str, str, str]:
    mapping = {
        "S&P 500": (
            "S&P 500 stocks Fed Nvidia earnings when:1d",
            "en-US",
            "US",
            "US:en",
        ),
        "Nasdaq": (
            "Nasdaq Nvidia semiconductor technology stocks when:1d",
            "en-US",
            "US",
            "US:en",
        ),
        "Euro Stoxx 50": (
            "Euro Stoxx 50 European stocks ECB when:1d",
            "en-US",
            "US",
            "US:en",
        ),
        "VIX": (
            "VIX volatility US stocks Fed when:1d",
            "en-US",
            "US",
            "US:en",
        ),
        "VKOSPI": (
            "코스피 변동성 외국인 투자심리 when:1d",
            "ko",
            "KR",
            "KR:ko",
        ),
        "US 10Y Treasury": (
            "US Treasury yields Fed inflation bonds when:1d",
            "en-US",
            "US",
            "US:en",
        ),
        "Korea 10Y Treasury": (
            "국고채 10년 금리 한국은행 채권 when:1d",
            "ko",
            "KR",
            "KR:ko",
        ),
        "Japan 10Y Treasury": (
            "Japan bond yields BOJ yen when:1d",
            "en-US",
            "US",
            "US:en",
        ),
        "Gold": (
            "gold price dollar Treasury yields when:1d",
            "en-US",
            "US",
            "US:en",
        ),
        "Silver": (
            "silver price precious metals dollar yields when:1d",
            "en-US",
            "US",
            "US:en",
        ),
        "Copper": (
            "copper price China demand supply metals when:1d",
            "en-US",
            "US",
            "US:en",
        ),
        "USD/KRW": (
            "원달러 환율 달러 외국인 한국은행 when:1d",
            "ko",
            "KR",
            "KR:ko",
        ),
        "JPY/KRW": (
            "엔원 환율 엔화 일본은행 when:1d",
            "ko",
            "KR",
            "KR:ko",
        ),
        "Bitcoin": (
            "Bitcoin crypto market price when:1d",
            "en-US",
            "US",
            "US:en",
        ),
        "Ethereum": (
            "Ethereum crypto market price when:1d",
            "en-US",
            "US",
            "US:en",
        ),
        "KOSPI": (
            "코스피 반도체 외국인 삼성전자 SK하이닉스 한국은행 when:1d",
            "ko",
            "KR",
            "KR:ko",
        ),
        "KOSDAQ": (
            "코스닥 외국인 기관 반도체 바이오 when:1d",
            "ko",
            "KR",
            "KR:ko",
        ),
        "Shanghai Composite": (
            "Shanghai Composite China stocks stimulus economy when:1d",
            "en-US",
            "US",
            "US:en",
        ),
        "Hang Seng": (
            "Hang Seng China Hong Kong stocks when:1d",
            "en-US",
            "US",
            "US:en",
        ),
        "Nikkei 225": (
            "Nikkei Japan stocks yen BOJ when:1d",
            "en-US",
            "US",
            "US:en",
        ),
    }

    return mapping.get(
        name,
        (
            f'"{name}" market when:1d',
            "en-US",
            "US",
            "US:en",
        ),
    )


def _broad_specs(mode: str) -> list[tuple[str, str, str, str]]:
    topics = "(stocks OR markets OR Fed OR inflation OR oil)"
    specs = [
        (f"{topics} site:{domain} when:1d", "en-US", "US", "US:en")
        for domain in ("reuters.com", "bloomberg.com", "cnbc.com")
    ]
    if mode == "KR":
        specs.extend(
            (
                f"(Korea OR Kospi OR Samsung OR Hynix) site:{domain} when:1d",
                "en-US",
                "US",
                "US:en",
            )
            for domain in ("reuters.com", "bloomberg.com")
        )
        specs.append(
            (
                "(코스피 OR 반도체 OR 환율 OR 한국은행) site:yna.co.kr when:1d",
                "ko",
                "KR",
                "KR:ko",
            )
        )
    else:
        specs.extend(
            (
                f"(Nvidia OR Broadcom OR earnings) site:{domain} when:1d",
                "en-US",
                "US",
                "US:en",
            )
            for domain in ("reuters.com", "cnbc.com")
        )
    return specs


def _fetch_rss(
    spec: tuple[str, str, str, str],
) -> list[dict]:
    query, hl, gl, ceid = spec

    params = {
        "q": query,
        "hl": hl,
        "gl": gl,
        "ceid": ceid,
    }

    url = f"{GOOGLE_NEWS_URL}?{urlencode(params)}"

    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macro-Pulse/1.0)",
            "Accept": ("application/rss+xml, application/xml, text/xml"),
        },
    )

    try:
        with urlopen(
            request,
            timeout=20,
        ) as response:
            root = ET.fromstring(response.read())

    except (
        HTTPError,
        URLError,
        TimeoutError,
        ET.ParseError,
    ) as exc:
        logger.warning(
            "Google News RSS failed for %s: %s",
            query,
            exc,
        )
        return []

    articles = []

    for item in root.findall(".//item")[:40]:
        title = (item.findtext("title") or "").strip()

        source = (item.findtext("source") or "").strip()

        published = (item.findtext("pubDate") or "").strip()

        if title:
            articles.append(
                {
                    "title": title,
                    "source": source,
                    "published": published,
                    "url": (item.findtext("link") or "").strip(),
                }
            )

    return articles


def _fetch_news(
    signals: list[dict],
    mode: str,
) -> list[dict]:
    specs = _broad_specs(mode)

    specs.extend(_query_spec(signal["name"]) for signal in signals[:4])

    specs = list(dict.fromkeys(specs))

    articles = []
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=24)).timestamp()

    with ThreadPoolExecutor(max_workers=min(8, len(specs))) as executor:
        results = executor.map(_fetch_rss, specs)
        for result in results:
            for article in result:
                if not is_allowed_source(article.get("source", ""), mode):
                    continue
                if (
                    not cutoff
                    <= _published_timestamp(article.get("published", ""))
                    <= now.timestamp()
                ):
                    continue
                articles.append(article)

    articles = verify_and_deduplicate_articles(articles, mode)
    articles.sort(
        key=lambda article: (
            len({_canonical_source(source) for source in article["verified_by"]}),
            is_official_source(article["source"]),
            _published_timestamp(article.get("published", "")),
        ),
        reverse=True,
    )

    logger.info(
        "Collected %s corroborated or official Google News headlines",
        len(articles),
    )

    return articles[:20]


def is_allowed_source(source: str, mode: str = "US") -> bool:
    normalized = _normalize_source(source)
    if not normalized:
        return False
    if any(_normalize_source(token) in normalized for token in BLOCKED_SOURCE_TOKENS):
        return False
    if _is_company_ir(normalized):
        return True
    allowed = [*TRUSTED_MEDIA, *OFFICIAL_SOURCES]
    if mode == "KR":
        allowed.extend(KR_SUPPLEMENTAL_SOURCES)
    allowed_canonical = {_canonical_source(name) for name in allowed}
    return _canonical_source(source) in allowed_canonical


def is_official_source(source: str) -> bool:
    normalized = _normalize_source(source)
    return _is_company_ir(normalized) or _canonical_source(source) in {
        _canonical_source(name) for name in OFFICIAL_SOURCES
    }


def verify_and_deduplicate_articles(
    articles: list[dict],
    mode: str,
) -> list[dict]:
    unique = []
    for article in articles:
        if not is_allowed_source(article.get("source", ""), mode):
            continue
        if any(
            _canonical_source(article.get("source", ""))
            == _canonical_source(item.get("source", ""))
            and _same_story(article["title"], item["title"])
            for item in unique
        ):
            continue
        unique.append(dict(article))

    groups: list[list[dict]] = []
    for article in unique:
        group = next(
            (
                candidate
                for candidate in groups
                if _same_topic(article["title"], candidate[0]["title"])
            ),
            None,
        )
        if group is None:
            groups.append([article])
        else:
            group.append(article)

    verified = []
    for group in groups:
        sources = {
            _canonical_source(article["source"]): article["source"].strip()
            for article in group
        }
        official = any(is_official_source(source) for source in sources.values())
        if not official and len(sources) < 2:
            continue
        representative = max(
            group,
            key=lambda article: (
                is_official_source(article["source"]),
                _published_timestamp(article.get("published", "")),
            ),
        )
        representative = dict(representative)
        representative["verified_by"] = sorted(sources.values())
        representative["supporting_headlines"] = [
            f"{article['source']}: {article['title']}" for article in group
        ]
        representative["verification"] = "official" if official else "cross"
        verified.append(representative)
    return verified


def _normalize_source(source: str) -> str:
    return re.sub(r"\s+", " ", source.strip()).casefold().rstrip(".")


def _canonical_source(source: str) -> str:
    normalized = _normalize_source(source)
    for canonical, aliases in SOURCE_ALIASES.items():
        if normalized in {_normalize_source(alias) for alias in aliases}:
            return _normalize_source(canonical)
    return normalized


def _is_company_ir(normalized_source: str) -> bool:
    return "investor relations" in normalized_source or normalized_source.endswith(
        " ir"
    )


def _published_timestamp(value: str) -> float:
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _same_story(left: str, right: str) -> bool:
    left_normalized = _normalize_title(left)
    right_normalized = _normalize_title(right)
    return SequenceMatcher(None, left_normalized, right_normalized).ratio() >= 0.88


def _same_topic(left: str, right: str) -> bool:
    left_tokens = _title_tokens(left)
    right_tokens = _title_tokens(right)
    if left_tokens and right_tokens:
        overlap = len(left_tokens & right_tokens)
        union = len(left_tokens | right_tokens)
        if overlap >= 2 and overlap / union >= 0.2:
            return True

    shared_topics = _topic_tags(left) & _topic_tags(right)
    return len(shared_topics) >= 2


def _topic_tags(title: str) -> set[str]:
    normalized = _normalize_title(title)
    return {
        topic
        for topic, aliases in TOPIC_ALIASES.items()
        if any(_normalize_title(alias) in normalized for alias in aliases)
    }


def _normalize_title(title: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", " ", title.lower()).strip()


def _title_tokens(title: str) -> set[str]:
    return {
        token
        for token in _normalize_title(title).split()
        if len(token) > 1 and token not in TITLE_STOPWORDS
    }


def _news_text(
    news: list[dict],
) -> str:
    if not news:
        return "최근 뉴스 헤드라인을 수집하지 못했습니다."

    return "\n".join(
        f"{index}. "
        f"{article['title']} | "
        f"{article['source'] or '출처 미상'} | "
        f"{article['published']} | "
        f"확인 출처: {', '.join(article.get('verified_by', []))} | "
        f"관련 헤드라인: {' / '.join(article.get('supporting_headlines', []))}"
        for index, article in enumerate(
            news,
            start=1,
        )
    )


def _fallback(
    signals: list[dict],
    news: list[dict],
    mode: str = "US",
    data=None,
) -> str:
    del signals, mode, data
    lines = ["[오늘의 핵심 이슈]"]
    if news:
        for index, article in enumerate(news[:3], 1):
            title = article["title"].removesuffix(f" - {article['source']}")
            lines.append(f"{index}. {title} ({article['source']})")
    else:
        lines.append("검증 기준을 충족한 주요 뉴스를 수집하지 못했습니다.")

    return "\n".join(lines)


def _build_prompt(
    signals: list[dict],
    mode: str,
    news: list[dict],
    data=None,
) -> str:
    del signals, data
    market = "한국 및 아시아 증시" if mode == "KR" else "미국 및 글로벌 증시"

    return f"""
당신은 증권사 리서치센터의 데일리 시황 담당자입니다.

정리 대상: {market}

[최근 24시간 뉴스 헤드라인]
{_news_text(news)}

위 헤드라인만 근거로 오늘 투자자가 알아야 할 중요한 이슈를 정리하세요.

작성 원칙:
- 통화정책, 경제지표, 지정학 및 에너지, 주요 기업 실적, AI 및 반도체 중 당일 시장 영향이 큰 사안을 우선하세요.
- 여러 허용 매체가 함께 보도한 이슈를 먼저 배치하세요.
- 뉴스에 없는 사실은 만들지 마세요.
- 헤드라인에 없는 원인, 전망, 숫자를 추가하지 마세요.
- 공식 자료 한 곳 또는 서로 다른 허용 매체 두 곳의 관련 보도가 있는 이슈만 제공되었습니다.
- 같은 주제라도 두 기사가 모두 확인한 내용만 공통 사실로 쓰고, 특정 매체에만 나온 사실은 그 매체의 보도라고 명시하세요.
- 한국 시장은 국내 증시에 직접 관련된 이슈와 글로벌 증시에 영향을 주는 이슈를 함께 고르세요.
- 미국 시장은 Fed, 국채금리, 경제지표, 빅테크 실적, AI 및 반도체 이슈를 우선하세요.
- 각 항목은 제목 한 줄과 핵심 내용 한 문장으로 끝내세요.
- 각 항목 끝에 실제 뉴스 출처명을 괄호로 표시하세요.
- 투자 추천과 목표주가는 쓰지 마세요.
- 중요도가 낮으면 세 항목을 억지로 채우지 마세요.
- 전체 답변은 600자 이내로 작성하세요.
- 가운데점 대신 쉼표를 사용하세요.

반드시 아래 형식만 사용하세요.

[오늘의 핵심 이슈]
1. 이슈 제목
핵심 내용 한 문장 (출처1, 출처2)
2. 이슈 제목
핵심 내용 한 문장 (출처1, 출처2)
""".strip()


def _has_required_analysis_sections(analysis: str) -> bool:
    return (
        len(analysis) <= 900
        and analysis.startswith("[오늘의 핵심 이슈]")
        and not any(
            heading in analysis
            for heading in ("[시장 해석]", "[핵심 이슈]", "[체크 포인트]")
        )
    )


def _analysis_uses_only_supported_numbers(
    analysis: str,
    signals: list[dict],
    news: list[dict],
    data=None,
) -> bool:
    support_text = "\n".join(
        [
            *(article.get("title", "") for article in news),
            *(
                title
                for article in news
                for title in article.get("supporting_headlines", [])
            ),
        ]
    )
    supported = _number_tokens(support_text)
    without_list_numbers = re.sub(r"(?m)^\s*\d+\.\s*", "", analysis)
    used = _number_tokens(without_list_numbers)
    return used <= supported


def _number_tokens(text: str) -> set[str]:
    tokens = set()
    for match in re.findall(r"(?<![\w])[-+]?\d[\d,]*(?:\.\d+)?", text):
        normalized = match.replace(",", "").lstrip("+")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        if normalized in {"-0", ""}:
            normalized = "0"
        tokens.add(normalized)
        tokens.add(normalized.lstrip("-"))
    return tokens


def _call_gemini(
    api_key: str,
    prompt: str,
    attempts: int = 2,
) -> str | None:
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 4096,
        },
    }

    result = None
    for attempt in range(1, max(1, attempts) + 1):
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
            with urlopen(request, timeout=35) as response:
                result = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code == 429 or 500 <= exc.code < 600
            logger.warning(
                "Gemini HTTP error %s (attempt %s/%s): %s",
                exc.code,
                attempt,
                attempts,
                error_body[:1000],
            )
            if not retryable or attempt >= attempts:
                return None
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            logger.warning(
                "Gemini request failed (attempt %s/%s): %s",
                attempt,
                attempts,
                exc,
            )
            if attempt >= attempts:
                return None
        time.sleep(1)

    if result is None:
        return None

    candidates = result.get(
        "candidates",
        [],
    )

    if not candidates:
        logger.warning("Gemini returned no candidates")
        return None

    candidate = candidates[0]

    finish_reason = candidate.get(
        "finishReason",
        "",
    )

    parts = candidate.get("content", {}).get("parts", [])

    text = "\n".join(
        part.get(
            "text",
            "",
        )
        for part in parts
        if (
            part.get("text")
            and not part.get(
                "thought",
                False,
            )
        )
    ).strip()

    if finish_reason and finish_reason != "STOP":
        logger.warning(
            "Gemini finish reason: %s",
            finish_reason,
        )

    return text or None


def analyze_market(
    signals: list[dict],
    mode: str,
    data=None,
) -> str | None:
    news = _fetch_news(
        signals,
        mode,
    )

    api_key = os.environ.get("GEMINI_API_KEY")

    if not news or not api_key:
        logger.info(
            "Using headline fallback: news=%s, AI configured=%s",
            len(news),
            bool(api_key),
        )

        return _fallback(
            signals,
            news,
            mode,
            data,
        )

    prompt = _build_prompt(
        signals,
        mode,
        news,
        data,
    )

    analysis = _call_gemini(
        api_key,
        prompt,
    )

    if (
        analysis
        and _has_required_analysis_sections(analysis)
        and _analysis_uses_only_supported_numbers(analysis, signals, news, data)
    ):
        return analysis

    if analysis:
        logger.warning("Gemini response failed section or numeric evidence validation")

    return _fallback(
        signals,
        news,
        mode,
        data,
    )
