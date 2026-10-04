from __future__ import annotations

import html
import json
import re
from datetime import date, datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .core.logging import get_logger
from .events import EconomicEvent


logger = get_logger(__name__)

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def build_recent_fomc_result_section(events: list[EconomicEvent]) -> str:
    for event in sorted(events, key=lambda item: item.event_date, reverse=True):
        if "FOMC" not in event.title:
            continue

        parsed = fetch_fomc_result(event.event_date)
        if not parsed:
            continue

        expectation = _fetch_market_expectation(event.event_date)
        return _format_result(parsed, expectation)

    return ""


def fetch_fomc_result(event_date: date) -> dict | None:
    url = (
        "https://www.federalreserve.gov/newsevents/pressreleases/"
        f"monetary{event_date:%Y%m%d}a.htm"
    )
    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Macro-Pulse/1.0)"},
    )

    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        logger.warning("Official FOMC statement unavailable: %s", exc)
        return None

    text = _clean_html(raw)
    return parse_fomc_statement(text, event_date, url)


def parse_fomc_statement(text: str, event_date: date, source_url: str) -> dict | None:
    range_match = re.search(
        r"target range for the federal funds rate.*?to\s+"
        r"([0-9]+(?:-[0-9]+/[0-9]+)?)\s+to\s+"
        r"([0-9]+(?:-[0-9]+/[0-9]+)?)\s+percent",
        text,
        flags=re.IGNORECASE,
    )
    if not range_match:
        return None

    lower = _parse_mixed_number(range_match.group(1))
    upper = _parse_mixed_number(range_match.group(2))
    if lower is None or upper is None:
        return None

    action_match = re.search(
        r"decided to\s+(raise|lower)\s+the target range.*?by\s+"
        r"([0-9]+(?:/[0-9]+)?)\s+percentage point",
        text,
        flags=re.IGNORECASE,
    )

    direction = "hold"
    change = 0.0
    if action_match:
        direction = action_match.group(1).lower()
        parsed_change = _parse_fraction(action_match.group(2))
        change = parsed_change or 0.0

    if direction == "raise":
        previous_lower = lower - change
        previous_upper = upper - change
    elif direction == "lower":
        previous_lower = lower + change
        previous_upper = upper + change
    else:
        previous_lower = lower
        previous_upper = upper

    vote_match = re.search(
        r"(?:approved|vote).*?(\d+)\s*[–—-]\s*(\d+)\s+vote",
        text,
        flags=re.IGNORECASE,
    )
    if vote_match is None:
        vote_match = re.search(
            r"(\d+)\s*[–—-]\s*(\d+)\s+vote",
            text,
            flags=re.IGNORECASE,
        )

    vote = None
    if vote_match:
        vote = f"{vote_match.group(1)}-{vote_match.group(2)}"

    return {
        "date": event_date.isoformat(),
        "lower": lower,
        "upper": upper,
        "previous_lower": previous_lower,
        "previous_upper": previous_upper,
        "direction": direction,
        "change_bp": round(change * 100),
        "vote": vote,
        "source_url": source_url,
    }


def _fetch_market_expectation(event_date: date) -> dict:
    request = Request(
        FF_CALENDAR_URL,
        headers={"User-Agent": "Mozilla/5.0 (Macro-Pulse/1.0)"},
    )

    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("FOMC expectation calendar unavailable: %s", exc)
        return {}

    if not isinstance(payload, list):
        return {}

    for entry in payload:
        if str(entry.get("country", "")) != "USD":
            continue
        if str(entry.get("title", "")).casefold() != "Federal Funds Rate".casefold():
            continue

        entry_date = _parse_iso_date(str(entry.get("date", "")))
        if entry_date and abs((entry_date - event_date).days) <= 1:
            return {
                "forecast": str(entry.get("forecast") or "").strip(),
                "previous": str(entry.get("previous") or "").strip(),
            }

    return {}


def _format_result(parsed: dict, expectation: dict) -> str:
    actual_range = _format_range(parsed["lower"], parsed["upper"])
    previous_range = _format_range(
        parsed["previous_lower"],
        parsed["previous_upper"],
    )

    expected_range = _expected_range_from_calendar(
        expectation.get("forecast", ""),
        parsed["lower"],
        parsed["upper"],
    )

    direction = parsed.get("direction")
    change_bp = int(parsed.get("change_bp") or 0)

    if direction == "raise":
        action_text = f"{change_bp}bp 인상"
    elif direction == "lower":
        action_text = f"{change_bp}bp 인하"
    else:
        action_text = "동결"

    lines = [
        "[발표 결과]",
        f"{parsed['date'][5:7]}월 FOMC",
        (
            f"• 정책금리: 실제 {actual_range} / "
            f"예상 {expected_range or '-'} / 이전 {previous_range}"
        ),
        f"• 결정: {action_text}",
    ]

    if parsed.get("vote"):
        lines.append(f"• 표결: {parsed['vote']}")

    lines.append(
        "• 해석: 연준이 인플레이션이 여전히 높다고 평가하며 "
        f"정책금리를 {action_text}"
    )
    lines.append("• 출처: Federal Reserve")

    return "\n".join(lines)


def _expected_range_from_calendar(raw: str, actual_lower: float, actual_upper: float) -> str:
    value = _parse_percent_value(raw)
    if value is None:
        return ""

    width = round(actual_upper - actual_lower, 4)
    lower = value - width
    return _format_range(lower, value)


def _format_range(lower: float, upper: float) -> str:
    return f"{lower:.2f}~{upper:.2f}%"


def _parse_percent_value(raw: str) -> float | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", raw.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _parse_mixed_number(raw: str) -> float | None:
    if "-" not in raw:
        try:
            return float(raw)
        except ValueError:
            return None

    whole, fraction = raw.split("-", 1)
    parsed_fraction = _parse_fraction(fraction)
    if parsed_fraction is None:
        return None

    try:
        return float(whole) + parsed_fraction
    except ValueError:
        return None


def _parse_fraction(raw: str) -> float | None:
    if "/" not in raw:
        try:
            return float(raw)
        except ValueError:
            return None

    numerator, denominator = raw.split("/", 1)
    try:
        denominator_value = float(denominator)
        if denominator_value == 0:
            return None
        return float(numerator) / denominator_value
    except ValueError:
        return None


def _parse_iso_date(raw: str) -> date | None:
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _clean_html(raw: str) -> str:
    no_scripts = re.sub(
        r"<(script|style).*?>.*?</\1>",
        " ",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    plain = re.sub(r"<[^>]+>", " ", no_scripts)
    plain = html.unescape(plain)
    return re.sub(r"\s+", " ", plain).strip()
