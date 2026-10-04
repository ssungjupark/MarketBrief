from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from statistics import mean, pstdev
from time import sleep
from zoneinfo import ZoneInfo

import requests

from ...core.logging import get_logger
from ...domain.models import ValueFormat
from ..quality import is_stale_as_of, utc_now_iso
from ..snapshots import build_snapshot


logger = get_logger(__name__)

KRX_JSON_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_REFERER = "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd"
KRX_LOGIN_PAGE = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd"
KRX_LOGIN_JSP = (
    "https://data.krx.co.kr/contents/MDC/COMS/client/view/login.jsp?site=mdc"
)
KRX_LOGIN_URL = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001D1.cmd"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_SECONDS = 8
LOGIN_TIMEOUT_SECONDS = 15
KRX_LOGIN_ATTEMPTS = 3
KRX_LOGIN_RETRY_SECONDS = 1
MAX_WORKERS = 3
FLOW_LOOKBACK_CALENDAR_DAYS = 45

_AUTH_LOCK = threading.Lock()
_AUTH_COOKIES: dict[str, str] | None = None

MARKET_NAMES = {"STK": "KOSPI", "KSQ": "KOSDAQ"}
FLOW_HISTORY_FIELDS = {
    "기관": "TRDVAL1",
    "외국인": "TRDVAL4",
}
SECTOR_INVESTORS = {
    "외국인": "9000",
    "기관": "7050",
}
def won_to_100m(value: float | int | str | None) -> float | None:
    parsed = _number(value)
    return parsed / 100_000_000 if parsed is not None else None


def fetch_krx_market_state(today: date | None = None) -> dict[str, list]:
    """Fetch authenticated KRX flow, breadth and sector-flow data."""
    if _get_auth_cookies() is None:
        logger.warning("KRX login unavailable; skipping authenticated market data")
        return unavailable_krx_market_state("KRX 로그인 실패")

    target = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    for trading_date in _candidate_dates(target):
        state = _fetch_for_date(trading_date, expected_date=target)
        if state is not None:
            return state

    logger.warning("KRX official market data unavailable for %s", target)
    return unavailable_krx_market_state("KRX 수집 실패")


def unavailable_krx_market_state(reason: str) -> dict[str, list]:
    return {
        "domestic_flow": _unavailable_flow_snapshots(reason),
        "market_breadth": _unavailable_breadth_snapshots(reason),
        "sector_flow": [
            build_snapshot(
                "업종별 수급",
                None,
                value_format=ValueFormat.KRW_100M,
                source="KRX",
                warning=reason,
            )
        ],
        "sector_performance": [],
    }


def _candidate_dates(target: date) -> list[date]:
    if target.weekday() < 5:
        return [target]

    candidates = []
    for offset in range(1, 8):
        candidate = target - timedelta(days=offset)
        if candidate.weekday() < 5:
            candidates.append(candidate)
            if len(candidates) >= 2:
                break
    return candidates


def _get_auth_cookies(force_refresh: bool = False) -> dict[str, str] | None:
    global _AUTH_COOKIES

    if _AUTH_COOKIES is not None and not force_refresh:
        return dict(_AUTH_COOKIES)

    with _AUTH_LOCK:
        if _AUTH_COOKIES is not None and not force_refresh:
            return dict(_AUTH_COOKIES)

        for attempt in range(1, KRX_LOGIN_ATTEMPTS + 1):
            cookies = _login_once()
            if cookies is not None:
                _AUTH_COOKIES = cookies
                return dict(_AUTH_COOKIES)

            if attempt < KRX_LOGIN_ATTEMPTS:
                delay = KRX_LOGIN_RETRY_SECONDS * attempt
                logger.warning(
                    "KRX login attempt %s/%s failed; retrying in %ss",
                    attempt,
                    KRX_LOGIN_ATTEMPTS,
                    delay,
                )
                sleep(delay)

        logger.warning("KRX login failed after %s attempts", KRX_LOGIN_ATTEMPTS)
        return None


def _login_once() -> dict[str, str] | None:
    login_id = os.getenv("KRX_ID", "").strip()
    login_pw = os.getenv("KRX_PW", "").strip()
    if not login_id or not login_pw:
        logger.warning("KRX_ID or KRX_PW is not configured")
        return None

    session = requests.Session()
    try:
        session.get(
            KRX_LOGIN_PAGE,
            headers={"User-Agent": USER_AGENT},
            timeout=LOGIN_TIMEOUT_SECONDS,
        ).raise_for_status()
        session.get(
            KRX_LOGIN_JSP,
            headers={"User-Agent": USER_AGENT, "Referer": KRX_LOGIN_PAGE},
            timeout=LOGIN_TIMEOUT_SECONDS,
        ).raise_for_status()

        payload = {
            "mbrNm": "",
            "telNo": "",
            "di": "",
            "certType": "",
            "mbrId": login_id,
            "pw": login_pw,
        }
        headers = {"User-Agent": USER_AGENT, "Referer": KRX_LOGIN_JSP}
        response = session.post(
            KRX_LOGIN_URL,
            data=payload,
            headers=headers,
            timeout=LOGIN_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        result = _parse_login_response(response)
        if result is None:
            return None
        error_code = result.get("_error_code", "")

        if error_code == "CD011":
            payload["skipDup"] = "Y"
            response = session.post(
                KRX_LOGIN_URL,
                data=payload,
                headers=headers,
                timeout=LOGIN_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            result = _parse_login_response(response)
            if result is None:
                return None
            error_code = result.get("_error_code", "")

        if error_code != "CD001":
            logger.warning(
                "KRX login rejected: code=%s message=%s",
                error_code or "unknown",
                result.get("_error_message", ""),
            )
            return None

        cookies = requests.utils.dict_from_cookiejar(session.cookies)
        if not cookies:
            logger.warning("KRX login succeeded but returned no session cookies")
            return None

        logger.info("KRX authenticated session established")
        return cookies
    except requests.RequestException as exc:
        logger.warning("KRX login request failed: %s", exc)
        return None
    finally:
        session.close()


def _parse_login_response(response) -> dict | None:
    try:
        return response.json()
    except ValueError as exc:
        logger.warning(
            "KRX login returned non-JSON response: status=%s content_type=%s bytes=%s (%s)",
            response.status_code,
            response.headers.get("Content-Type", ""),
            len(response.content or b""),
            exc,
        )
        return None


def _fetch_for_date(
    trading_date: date,
    expected_date: date | None = None,
) -> dict[str, list] | None:
    as_of = trading_date.isoformat()
    fetched_at = utc_now_iso()
    stale = is_stale_as_of(as_of) or (
        expected_date is not None
        and expected_date.weekday() < 5
        and trading_date != expected_date
    )
    ymd = trading_date.strftime("%Y%m%d")
    history_start = (trading_date - timedelta(days=FLOW_LOOKBACK_CALENDAR_DAYS)).strftime(
        "%Y%m%d"
    )

    base_specs: dict[str, tuple[str, dict]] = {
        "market_STK": (
            "dbms/MDC/STAT/standard/MDCSTAT01501",
            {"mktId": "STK", "trdDd": ymd},
        ),
        "market_KSQ": (
            "dbms/MDC/STAT/standard/MDCSTAT01501",
            {"mktId": "KSQ", "trdDd": ymd},
        ),
        "history_STK": (
            "dbms/MDC/STAT/standard/MDCSTAT02202",
            {
                "strtDd": history_start,
                "endDd": ymd,
                "mktId": "STK",
                "etf": "",
                "etn": "",
                "elw": "",
                "inqTpCd": "2",
                "trdVolVal": "2",
                "askBid": "3",
            },
        ),
        "history_KSQ": (
            "dbms/MDC/STAT/standard/MDCSTAT02202",
            {
                "strtDd": history_start,
                "endDd": ymd,
                "mktId": "KSQ",
                "etf": "",
                "etn": "",
                "elw": "",
                "inqTpCd": "2",
                "trdVolVal": "2",
                "askBid": "3",
            },
        ),
        "classification_STK": (
            "dbms/MDC/STAT/standard/MDCSTAT03901",
            {"mktId": "STK", "trdDd": ymd},
        ),
        "classification_KSQ": (
            "dbms/MDC/STAT/standard/MDCSTAT03901",
            {"mktId": "KSQ", "trdDd": ymd},
        ),
    }
    fetched = _fetch_specs(base_specs)

    if not any(fetched.values()):
        return None

    flow = []
    breadth = []
    for market_id, market_name in MARKET_NAMES.items():
        market_rows = fetched.get(f"market_{market_id}") or []
        history_rows = fetched.get(f"history_{market_id}") or []
        breadth.extend(
            _build_breadth(market_name, market_rows, as_of, fetched_at, stale)
        )
        flow.extend(
            _build_flow_from_history(
                market_name,
                history_rows,
                as_of,
                fetched_at,
                stale,
            )
        )

    five_day_start = _five_day_start(
        fetched.get("history_STK") or fetched.get("history_KSQ") or [],
        trading_date,
    )
    sector_specs = {}
    for investor_label, investor_code in SECTOR_INVESTORS.items():
        sector_specs[f"sector_{investor_label}_1d"] = (
            "dbms/MDC/STAT/standard/MDCSTAT02401",
            {
                "strtDd": ymd,
                "endDd": ymd,
                "mktId": "ALL",
                "invstTpCd": investor_code,
            },
        )
        sector_specs[f"sector_{investor_label}_5d"] = (
            "dbms/MDC/STAT/standard/MDCSTAT02401",
            {
                "strtDd": five_day_start,
                "endDd": ymd,
                "mktId": "ALL",
                "invstTpCd": investor_code,
            },
        )
    sector_data = _fetch_specs(sector_specs)

    classification_rows = [
        *(fetched.get("classification_STK") or []),
        *(fetched.get("classification_KSQ") or []),
    ]
    market_rows = [
        *(fetched.get("market_STK") or []),
        *(fetched.get("market_KSQ") or []),
    ]
    sector_flow = _build_sector_flow(
        classification_rows,
        market_rows,
        sector_data,
        as_of,
        fetched_at,
        stale,
    )

    return {
        "domestic_flow": flow,
        "market_breadth": breadth,
        "sector_flow": sector_flow,
        "sector_performance": [],
    }


def _fetch_specs(specs: dict[str, tuple[str, dict]]) -> dict[str, list[dict] | None]:
    fetched: dict[str, list[dict] | None] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_key = {
            executor.submit(_post_krx, bld, **params): key
            for key, (bld, params) in specs.items()
        }
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                fetched[key] = future.result()
            except Exception as exc:
                logger.warning("KRX request task failed for %s: %s", key, exc)
                fetched[key] = None
    return fetched


def _post_krx(
    bld: str,
    _retried_after_auth: bool = False,
    **params,
) -> list[dict] | None:
    cookies = _get_auth_cookies()
    if cookies is None:
        return None

    payload = {
        "bld": bld,
        "locale": "ko_KR",
        "share": "1",
        "money": "1",
        "csvxls_isNo": "false",
        **params,
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": KRX_REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    try:
        response = requests.post(
            KRX_JSON_URL,
            data=payload,
            headers=headers,
            cookies=cookies,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        if response.status_code in {400, 401, 403} and not _retried_after_auth:
            logger.info("KRX session may be stale; refreshing authentication")
            if _get_auth_cookies(force_refresh=True) is not None:
                return _post_krx(bld, _retried_after_auth=True, **params)

        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        logger.warning("KRX request failed for %s: %s", bld, exc)
        return None

    error_code = str(result.get("_error_code", ""))
    if error_code and error_code != "CD001":
        if not _retried_after_auth and error_code in {"LOGOUT", "CD002", "CD003"}:
            logger.info("KRX response requires reauthentication: %s", error_code)
            if _get_auth_cookies(force_refresh=True) is not None:
                return _post_krx(bld, _retried_after_auth=True, **params)
        logger.warning(
            "KRX response error for %s: code=%s message=%s",
            bld,
            error_code,
            result.get("_error_message", ""),
        )
        return None

    rows = _extract_rows(result)
    if not rows and not any(
        isinstance(result.get(key), list)
        for key in ("OutBlock_1", "output", "block1", "result")
    ):
        logger.warning(
            "KRX response for %s has no recognized row block; keys=%s",
            bld,
            sorted(result.keys()),
        )
    return rows


def _extract_rows(result: dict) -> list[dict]:
    """Return rows across the response block names used by KRX endpoints."""
    for key in ("OutBlock_1", "output", "block1", "result"):
        rows = result.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _build_flow_from_history(market, rows, as_of, fetched_at, stale=False):
    actual_as_of = _latest_flow_date(rows)
    date_mismatch = actual_as_of is not None and actual_as_of != as_of
    effective_stale = stale or date_mismatch

    series_by_label = {
        label: _flow_series(rows, field)
        for label, field in FLOW_HISTORY_FIELDS.items()
    }
    snapshots = []
    for label in ("외국인", "기관"):
        values = series_by_label[label]
        current = values[-1] if values else None
        if effective_stale:
            current = None

        if date_mismatch:
            warning = f"KRX 수급 기준일 불일치: {actual_as_of}"
        elif stale:
            warning = f"오래된 KRX 데이터: {actual_as_of or as_of}"
        elif current is None:
            warning = "KRX 수급 이력 누락"
        else:
            warning = None

        snapshots.append(
            build_snapshot(
                f"{label} {market} 현물",
                current,
                value_format=ValueFormat.KRW_100M,
                history=values[-20:],
                change_5d=_flow_sum(values, 5),
                change_20d=_flow_sum(values, 20),
                z_score_20d=_flow_z_score(values),
                as_of=actual_as_of or as_of,
                fetched_at=fetched_at,
                source="KRX",
                is_stale=effective_stale,
                warning=warning,
            )
        )
    return snapshots


def _latest_flow_date(rows) -> str | None:
    dates = []
    for row in rows:
        raw_date = str(row.get("TRD_DD", ""))
        try:
            dates.append(datetime.strptime(raw_date, "%Y/%m/%d").date())
        except ValueError:
            continue
    return max(dates).isoformat() if dates else None


def _flow_series(rows, field):
    dated_values = []
    for row in rows:
        raw_date = str(row.get("TRD_DD", ""))
        value = won_to_100m(row.get(field))
        if not raw_date or value is None:
            continue
        try:
            parsed_date = datetime.strptime(raw_date, "%Y/%m/%d").date()
        except ValueError:
            continue
        dated_values.append((parsed_date, value))
    dated_values.sort(key=lambda item: item[0])
    return [value for _, value in dated_values]


def _flow_sum(values, sessions):
    if len(values) < sessions:
        return None
    return sum(values[-sessions:])


def _flow_z_score(values):
    if len(values) < 20:
        return None
    recent = [float(value) for value in values[-20:]]
    baseline = recent[:-1]
    sigma = pstdev(baseline)
    if sigma == 0:
        return 0.0 if recent[-1] == mean(baseline) else None
    return (recent[-1] - mean(baseline)) / sigma


def _five_day_start(history_rows, trading_date):
    dates = []
    for row in history_rows:
        raw_date = str(row.get("TRD_DD", ""))
        try:
            dates.append(datetime.strptime(raw_date, "%Y/%m/%d").date())
        except ValueError:
            continue
    dates = sorted(set(dates))
    if len(dates) >= 5:
        return dates[-5].strftime("%Y%m%d")
    return (trading_date - timedelta(days=7)).strftime("%Y%m%d")


def _build_sector_flow(
    classification_rows,
    market_rows,
    sector_data,
    as_of,
    fetched_at,
    stale=False,
):
    ticker_to_sector = {}
    for row in classification_rows:
        ticker = str(row.get("ISU_SRT_CD", "")).strip()
        sector = str(row.get("IDX_IND_NM", "")).strip()
        if ticker and sector and sector != "-":
            ticker_to_sector[ticker] = sector

    turnover_by_sector: dict[str, float] = {}
    for row in market_rows:
        ticker = str(row.get("ISU_SRT_CD", "")).strip()
        sector = ticker_to_sector.get(ticker)
        turnover = _number(row.get("ACC_TRDVAL"))
        if sector and turnover is not None:
            turnover_by_sector[sector] = turnover_by_sector.get(sector, 0.0) + turnover

    snapshots = []
    for investor_label in SECTOR_INVESTORS:
        current_by_sector = _aggregate_sector_net_buy(
            sector_data.get(f"sector_{investor_label}_1d") or [],
            ticker_to_sector,
        )
        five_day_by_sector = _aggregate_sector_net_buy(
            sector_data.get(f"sector_{investor_label}_5d") or [],
            ticker_to_sector,
        )
        ranked = sorted(current_by_sector.items(), key=lambda item: item[1], reverse=True)
        buys = [(sector, value) for sector, value in ranked if value > 0][:3]
        sells = [(sector, value) for sector, value in reversed(ranked) if value < 0][:3]

        for direction, selected in (("순매수", buys), ("순매도", sells)):
            for rank, (sector, value_won) in enumerate(selected, 1):
                turnover = turnover_by_sector.get(sector, 0.0)
                strength = (value_won / turnover) * 100 if turnover else None
                strength_text = (
                    f" | 강도 {strength:+.2f}%" if strength is not None else ""
                )
                current = won_to_100m(value_won)
                five_day = won_to_100m(five_day_by_sector.get(sector))
                if stale:
                    current = None
                snapshots.append(
                    build_snapshot(
                        f"{investor_label} {direction} {rank}: {sector}{strength_text}",
                        current,
                        value_format=ValueFormat.KRW_100M,
                        change_5d=five_day,
                        as_of=as_of,
                        fetched_at=fetched_at,
                        source="KRX",
                        is_stale=stale,
                        warning=(
                            f"오래된 KRX 데이터: {as_of}"
                            if stale
                            else None
                            if current is not None
                            else "KRX 업종 수급 누락"
                        ),
                    )
                )

    if snapshots:
        return snapshots
    return [
        build_snapshot(
            "업종별 수급",
            None,
            value_format=ValueFormat.KRW_100M,
            as_of=as_of,
            fetched_at=fetched_at,
            source="KRX",
            warning="KRX 업종 수급 누락",
        )
    ]


def _aggregate_sector_net_buy(rows, ticker_to_sector):
    totals: dict[str, float] = {}
    for row in rows:
        ticker = str(row.get("ISU_SRT_CD", "")).strip()
        sector = ticker_to_sector.get(ticker)
        value = _number(row.get("NETBID_TRDVAL"))
        if sector and value is not None:
            totals[sector] = totals.get(sector, 0.0) + value
    return totals


def _build_breadth(market, rows, as_of, fetched_at, stale=False):
    changes = [_number(row.get("CMPPREVDD_PRC")) for row in rows]
    advances = sum(change > 0 for change in changes if change is not None)
    declines = sum(change < 0 for change in changes if change is not None)
    turnover_won = sum(
        value
        for value in (_number(row.get("ACC_TRDVAL")) for row in rows)
        if value is not None
    )
    if not rows:
        return [
            build_snapshot(
                f"{market} {label}",
                None,
                value_format=value_format,
                as_of=as_of,
                fetched_at=fetched_at,
                source="KRX",
                warning="KRX 항목 누락",
            )
            for label, value_format in (
                ("상승 종목", ValueFormat.INTEGER),
                ("하락 종목", ValueFormat.INTEGER),
                ("거래대금", ValueFormat.KRW_100M),
            )
        ]
    return [
        build_snapshot(
            f"{market} 상승 종목",
            None if stale else advances,
            value_format=ValueFormat.INTEGER,
            as_of=as_of,
            fetched_at=fetched_at,
            source="KRX",
            is_stale=stale,
            warning=f"오래된 KRX 데이터: {as_of}" if stale else None,
        ),
        build_snapshot(
            f"{market} 하락 종목",
            None if stale else declines,
            value_format=ValueFormat.INTEGER,
            as_of=as_of,
            fetched_at=fetched_at,
            source="KRX",
            is_stale=stale,
            warning=f"오래된 KRX 데이터: {as_of}" if stale else None,
        ),
        build_snapshot(
            f"{market} 거래대금",
            None if stale else won_to_100m(turnover_won),
            value_format=ValueFormat.KRW_100M,
            as_of=as_of,
            fetched_at=fetched_at,
            source="KRX",
            is_stale=stale,
            warning=f"오래된 KRX 데이터: {as_of}" if stale else None,
        ),
    ]


def _unavailable_flow_snapshots(reason="KRX 수집 실패"):
    return [
        build_snapshot(
            name,
            None,
            value_format=ValueFormat.KRW_100M,
            source="KRX",
            warning=reason,
        )
        for name in (
            "외국인 KOSPI 현물",
            "기관 KOSPI 현물",
            "외국인 KOSDAQ 현물",
            "기관 KOSDAQ 현물",
        )
    ]


def _unavailable_breadth_snapshots(reason="KRX 수집 실패"):
    snapshots = []
    for market in ("KOSPI", "KOSDAQ"):
        for label, value_format in (
            ("상승 종목", ValueFormat.INTEGER),
            ("하락 종목", ValueFormat.INTEGER),
            ("거래대금", ValueFormat.KRW_100M),
        ):
            snapshots.append(
                build_snapshot(
                    f"{market} {label}",
                    None,
                    value_format=value_format,
                    source="KRX",
                    warning=reason,
                )
            )
    return snapshots


def _number(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None
