from __future__ import annotations

import json
from calendar import monthcalendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .core.logging import get_logger
from .core.paths import resolve_project_path


logger = get_logger(__name__)

BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
DEFAULT_EVENTS_CONFIG = "config/official_events.json"

BLS_TITLES = {
    "Employment Situation": "미국 고용보고서(NFP)",
    "Consumer Price Index": "미국 CPI",
    "Producer Price Index": "미국 PPI",
}

OFFICIAL_EVENT_HOSTS = {
    "www.bls.gov",
    "www.federalreserve.gov",
    "www.bea.gov",
    "www.ismworld.org",
    "www.ecb.europa.eu",
    "www.boj.or.jp",
    "www.bok.or.kr",
    "www.cboe.com",
}


@dataclass(slots=True, frozen=True)
class EconomicEvent:
    event_date: date
    title: str
    source: str
    source_url: str
    priority: int = 50


def get_upcoming_events(
    today: date | None = None,
    *,
    limit: int = 3,
    config_path: str | Path | None = None,
) -> list[EconomicEvent]:
    current = today or date.today()
    events = [
        *_load_config_events(config_path),
        *_fetch_bls_events(),
        _next_us_options_expiry(current),
    ]
    unique = {}
    for event in events:
        if event.event_date < current or event.event_date > current + timedelta(
            days=45
        ):
            continue
        unique[(event.event_date, event.title)] = event
    return sorted(
        unique.values(),
        key=lambda item: (item.event_date, -item.priority),
    )[:limit]


def format_event_section(events: list[EconomicEvent]) -> str:
    lines = ["[주요 일정]"]
    if not events:
        lines.append("공식 일정 확인 불가")
        return "\n".join(lines)
    lines.extend(
        f"- {event.event_date:%m/%d} {event.title} ({event.source})" for event in events
    )
    return "\n".join(lines)


def insert_event_section(analysis: str, events: list[EconomicEvent]) -> str:
    section = format_event_section(events)
    marker = "[체크 포인트]"
    if marker not in analysis:
        return f"{analysis}\n\n{section}"
    return analysis.replace(marker, f"{section}\n\n{marker}", 1)


def parse_bls_ics(content: str) -> list[EconomicEvent]:
    unfolded = content.replace("\r\n ", "").replace("\n ", "")
    events = []
    for block in unfolded.split("BEGIN:VEVENT")[1:]:
        block = block.split("END:VEVENT", 1)[0]
        fields = {}
        for line in block.replace("\r", "").split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key.split(";", 1)[0]] = value.strip()
        summary = fields.get("SUMMARY", "")
        raw_date = fields.get("DTSTART", "")[:8]
        title = next(
            (label for key, label in BLS_TITLES.items() if key in summary),
            None,
        )
        if not title or len(raw_date) != 8:
            continue
        try:
            event_date = datetime.strptime(raw_date, "%Y%m%d").date()
        except ValueError:
            continue
        priority = 100 if "NFP" in title or "CPI" in title else 80
        events.append(
            EconomicEvent(event_date, title, "U.S. BLS", BLS_ICS_URL, priority)
        )
    return events


def _fetch_bls_events() -> list[EconomicEvent]:
    request = Request(
        BLS_ICS_URL,
        headers={"User-Agent": "Mozilla/5.0 (Macro-Pulse/1.0)"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            return parse_bls_ics(response.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError) as exc:
        logger.warning("BLS official calendar unavailable: %s", exc)
        return []


def _load_config_events(config_path) -> list[EconomicEvent]:
    path = resolve_project_path(config_path or DEFAULT_EVENTS_CONFIG)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Official event config unavailable: %s", exc)
        return []

    events = []
    for raw_event in payload.get("events", []):
        source_url = str(raw_event.get("source_url", ""))
        source = str(raw_event.get("source", ""))
        if not _is_verified_source_url(source, source_url):
            logger.warning("Skipped event without verified source URL: %s", raw_event)
            continue
        try:
            event_date = date.fromisoformat(str(raw_event["date"]))
        except (KeyError, ValueError):
            logger.warning("Skipped event with invalid date: %s", raw_event)
            continue
        events.append(
            EconomicEvent(
                event_date,
                str(raw_event["title"]),
                source,
                source_url,
                int(raw_event.get("priority", 50)),
            )
        )
    return events


def _next_us_options_expiry(current: date) -> EconomicEvent:
    year, month = current.year, current.month
    while True:
        fridays = [week[4] for week in monthcalendar(year, month) if week[4] != 0]
        expiry = date(year, month, fridays[2])
        if expiry >= current:
            quarterly = month in {3, 6, 9, 12}
            return EconomicEvent(
                expiry,
                (
                    "미국 옵션 및 주가지수 선물 만기"
                    if quarterly
                    else "미국 월간 옵션 만기"
                ),
                "Cboe calendar rule",
                "https://www.cboe.com/us/options/market_statistics/expiration_calendar/",
                70,
            )
        month += 1
        if month == 13:
            year += 1
            month = 1


def _is_verified_source_url(source: str, source_url: str) -> bool:
    if not source_url.startswith("https://"):
        return False
    hostname = (urlparse(source_url).hostname or "").lower()
    if hostname in OFFICIAL_EVENT_HOSTS:
        return True
    return source.endswith("Investor Relations") and hostname.startswith(
        ("investor.", "investors.")
    )
