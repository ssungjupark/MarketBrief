from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from ..config.report_formats import (
    get_screenshot_targets,
    load_report_format_config,
)
from ..core.artifacts import cleanup_files
from ..core.logging import configure_logging, get_logger
from ..data.market_data import fetch_all_data
from ..delivery.notifier import send_telegram_report
from ..event_results import (
    build_recent_event_result_section,
    insert_event_result_section,
    supports_event_result,
)
from ..events import get_upcoming_events, insert_event_section
from ..fomc_results import build_recent_fomc_result_section
from ..intelligence import analyze_market
from ..reporting.generator import (
    generate_html_report,
    generate_telegram_summary,
)
from ..reporting.screenshots import capture_screenshots
from ..signals import (
    detect_signals,
    format_signal_context,
    select_representative_signals,
)


load_dotenv()
configure_logging()
logger = get_logger(__name__)


def resolve_mode(
    market_arg: str | None,
    now_utc: datetime | None = None,
) -> str:
    normalized = (market_arg or "").strip().upper()

    if normalized in {"KR", "US"}:
        return normalized

    current_time = now_utc or datetime.now(timezone.utc)

    return "KR" if 7 <= current_time.hour < 20 else "US"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def validate_krx_delivery_data(data) -> tuple[bool, str]:
    required_flow_names = {
        "외국인 KOSPI 현물",
        "기관 KOSPI 현물",
        "외국인 KOSDAQ 현물",
        "기관 KOSDAQ 현물",
    }
    flow_items = data.get("domestic_flow", [])
    flow_by_name = {item.name: item for item in flow_items}
    missing_flow = sorted(
        name
        for name in required_flow_names
        if name not in flow_by_name or flow_by_name[name].price is None
    )

    valid_sector_items = [
        item
        for item in data.get("sector_flow", [])
        if item.price is not None and item.name != "업종별 수급"
    ]

    problems = []
    if missing_flow:
        problems.append("현물 수급 누락: " + ", ".join(missing_flow))
    if len(valid_sector_items) < 12:
        problems.append(
            f"업종 수급 부족: {len(valid_sector_items)}/12"
        )

    return (not problems, "; ".join(problems))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Macro Pulse Bot")

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate report but do not send",
    )

    parser.add_argument(
        "--market",
        type=str,
        default="Global",
        help="Market context override (KR/US).",
    )

    return parser


def compose_telegram_report(
    base_summary: str,
    signals: list[dict],
    analysis: str | None,
    events,
    event_results: str = "",
) -> str:
    signal_lines = ["[주요 변동 신호]"]
    if signals:
        for signal in signals[:5]:
            context = format_signal_context(signal)
            context_suffix = f" | {context}" if context else ""
            signal_lines.append(
                f"{signal['name']}: {signal['move']} "
                f"({signal['direction']}){context_suffix}"
            )
    else:
        signal_lines.append("기준치 이상의 특이 변동 신호 없음")

    normalized_analysis = analysis or (
        "[오늘의 핵심 이슈]\n검증 기준을 충족한 주요 뉴스를 수집하지 못했습니다."
    )
    normalized_analysis = insert_event_result_section(
        normalized_analysis,
        event_results,
    )
    normalized_analysis = insert_event_section(normalized_analysis, events)
    signal_section = "\n".join(signal_lines)
    return f"{base_summary}\n\n{signal_section}\n\n{normalized_analysis}"


def build_event_results(events) -> str:
    # Only report the newest supported release date. Older releases must never
    # crowd out a newer BOJ/CPI/PCE/etc. result just because FOMC has a
    # dedicated parser.
    eligible = [
        event
        for event in events
        if "FOMC" in event.title or supports_event_result(event.title)
    ]
    if not eligible:
        return ""

    latest_date = max(event.event_date for event in eligible)
    sections = []

    for event in sorted(
        (item for item in eligible if item.event_date == latest_date),
        key=lambda item: (-item.priority, item.title),
    ):
        if "FOMC" in event.title:
            section = build_recent_fomc_result_section([event])
        else:
            section = build_recent_event_result_section([event], max_events=1)

        if not section:
            continue
        sections.append(section.removeprefix("[발표 결과]\n"))
        if len(sections) >= 2:
            break

    if not sections:
        return ""

    return "[발표 결과]\n" + "\n\n".join(sections)


async def main(
    argv: list[str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)

    mode = resolve_mode(args.market)
    report_format_config = load_report_format_config()

    logger.info(
        "Starting Macro Pulse Bot (mode=%s)",
        mode,
    )

    data = fetch_all_data(mode)

    if mode == "KR" and _env_flag("REQUIRE_KRX_DATA"):
        krx_ready, krx_problem = validate_krx_delivery_data(data)
        if not krx_ready:
            raise RuntimeError(
                "KRX critical data unavailable; guarded delivery aborted: "
                f"{krx_problem}"
            )

    base_summary = generate_telegram_summary(
        data,
        mode,
        report_format_config,
    )
    if mode == "KR":
        base_summary = (
            "[KRX 정규장 마감 | 15:30 기준]\n\n"
            f"{base_summary}"
        )

    signals = select_representative_signals(detect_signals(data))

    analysis = analyze_market(signals, mode, data)
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    recent_start = today - timedelta(days=1)
    recent_events = [
        event
        for event in get_upcoming_events(recent_start, limit=20)
        if recent_start <= event.event_date <= today
    ]
    event_results = build_event_results(recent_events)
    telegram_summary = compose_telegram_report(
        base_summary,
        signals,
        analysis,
        get_upcoming_events(),
        event_results,
    )

    logger.info(
        "Telegram Summary (%s):\n%s\n",
        mode,
        telegram_summary,
    )

    output_path = Path("macro_pulse_report.html")

    if args.dry_run:
        output_path.write_text(generate_html_report(data), encoding="utf-8")
        logger.info("Report saved to %s", output_path)
        logger.info("Dry run complete. No notifications sent.")
        return 0

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not telegram_token or not telegram_chat_id:
        raise RuntimeError("Telegram credentials missing; report was not delivered")

    delivery_receipt_path = os.environ.get("DELIVERY_RECEIPT_PATH")

    # Send the text as soon as the report body is ready. Screenshots are optional
    # follow-up media and must not delay the market-close message.
    delivered = await send_telegram_report(
        telegram_token,
        telegram_chat_id,
        telegram_summary,
        delivery_receipt_path=delivery_receipt_path,
    )
    if not delivered:
        raise RuntimeError("Telegram report delivery failed")

    # HTML is an auxiliary artifact. Do it only after the time-sensitive
    # Telegram text has already been delivered.
    try:
        output_path.write_text(generate_html_report(data), encoding="utf-8")
        logger.info("Report saved to %s", output_path)
    except Exception as exc:
        logger.warning("Optional HTML report generation failed: %s", exc)

    screenshot_paths = []
    try:
        screenshot_paths = capture_screenshots(
            get_screenshot_targets(
                mode,
                report_format_config,
            )
        )
        if screenshot_paths:
            photos_delivered = await send_telegram_report(
                telegram_token,
                telegram_chat_id,
                image_paths=screenshot_paths,
                attempts=1,
                send_text=False,
            )
            if not photos_delivered:
                logger.warning(
                    "Telegram text was delivered, but one or more screenshots failed"
                )
    except Exception as exc:
        logger.exception(
            "Telegram text was delivered, but screenshot follow-up failed: %s",
            exc,
        )
    finally:
        cleanup_files(screenshot_paths)

    return 0
