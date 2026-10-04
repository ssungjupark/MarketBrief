import os
import tempfile

import yfinance as yf

from ..core.logging import get_logger
from ..domain.models import (
    ReportDataset,
    TickerDefinition,
    ValueFormat,
    coerce_cnbc_quote,
)
from .exchange_rates import build_exchange_snapshots, unavailable_exchange_snapshots
from .providers.cnbc import (
    CNBC_FX_SYMBOLS,
    CNBC_MARKET_SYMBOLS,
    CNBC_QUOTES,
    fetch_cnbc_data,
)
from .providers.fred import fetch_treasury_histories
from .providers.krx import fetch_krx_market_state, unavailable_krx_market_state
from .quality import (
    calculate_period_change,
    calculate_return_z_score,
    is_stale_as_of,
    utc_now_iso,
    validate_value,
)
from .snapshots import build_snapshot


logger = get_logger(__name__)


YF_TICKERS = {
    "indices_domestic": (
        TickerDefinition("KOSPI", "^KS11"),
        TickerDefinition("KOSDAQ", "^KQ11"),
    ),
    "indices_overseas": (
        TickerDefinition("S&P 500", "^GSPC"),
        TickerDefinition("Nasdaq", "^IXIC"),
        TickerDefinition("Euro Stoxx 50", "^STOXX50E"),
        TickerDefinition("Nikkei 225", "^N225"),
        TickerDefinition("Hang Seng", "^HSI"),
        TickerDefinition("Shanghai Composite", "000001.SS"),
        TickerDefinition("SOX", "^SOX"),
        TickerDefinition("Russell 2000", "^RUT"),
    ),
    "commodities": (
        TickerDefinition("Gold", "GC=F"),
        TickerDefinition("Silver", "SI=F"),
        TickerDefinition("Copper", "HG=F"),
        TickerDefinition("WTI", "CL=F"),
    ),
    "crypto": (
        TickerDefinition("Bitcoin", "BTC-USD"),
        TickerDefinition("Ethereum", "ETH-USD"),
    ),
    "volatility": (
        TickerDefinition("VIX", "^VIX"),
        TickerDefinition("MOVE", "^MOVE"),
    ),
    "macro": (TickerDefinition("DXY", "DX-Y.NYB"),),
}

YF_RATES_HISTORY = {
    "USD/KRW": "KRW=X",
    "JPY/KRW": "JPYKRW=X",
}

MODE_YF_NAMES = {
    "KR": {
        "KOSPI",
        "KOSDAQ",
        "Nikkei 225",
        "Hang Seng",
        "Shanghai Composite",
        "VIX",
        "Gold",
        "Silver",
        "Copper",
        "WTI",
        "DXY",
    },
    "US": {
        "S&P 500",
        "Nasdaq",
        "Euro Stoxx 50",
        "SOX",
        "Russell 2000",
        "VIX",
        "MOVE",
        "Gold",
        "Silver",
        "Copper",
        "WTI",
        "Bitcoin",
        "Ethereum",
        "DXY",
    },
}


def fetch_all_data(mode: str | None = None) -> ReportDataset:
    _configure_runtime_cache()
    results = _empty_report_dataset()

    yf_rates_data = _fetch_rate_histories()

    logger.info("Fetching CNBC data...")
    cnbc_data = fetch_cnbc_data([*CNBC_MARKET_SYMBOLS, *CNBC_FX_SYMBOLS])
    treasury_histories = fetch_treasury_histories()
    try:
        results["exchange"].extend(build_exchange_snapshots(cnbc_data, yf_rates_data))
    except Exception as exc:
        logger.exception("FX snapshot construction failed: %s", exc)
        results["exchange"].extend(unavailable_exchange_snapshots("환율 처리 실패"))
    _append_cnbc_market_snapshots(results, cnbc_data, treasury_histories)

    logger.info("Fetching Yahoo Finance data...")
    _append_yahoo_snapshots(results, mode)
    _append_us_yield_spread(results["treasuries"])

    if (mode or "").upper() != "US":
        logger.info("Fetching KRX public market state...")
        try:
            domestic_state = fetch_krx_market_state()
        except Exception as exc:
            logger.exception("KRX provider failed without stopping the report: %s", exc)
            domestic_state = unavailable_krx_market_state("KRX 시세 처리 실패")
        results["domestic_flow"].extend(domestic_state.get("domestic_flow", []))
        results["market_breadth"].extend(domestic_state.get("market_breadth", []))
        results["sector_flow"].extend(domestic_state.get("sector_flow", []))
        results["sector_performance"].extend(
            domestic_state.get("sector_performance", [])
        )
    else:
        logger.info("Skipping KRX domestic market state for US report")
    results["domestic_state"] = [
        *results["domestic_flow"],
        *results["market_breadth"],
        *results["sector_flow"],
        *results["sector_performance"],
    ]
    results["macro_context"] = [
        *results["macro"],
        *results["commodities"],
        *results["exchange"],
    ]

    logger.info(
        "Completed fetch cycle with %s populated categories",
        sum(1 for items in results.values() if items),
    )

    return results


def _empty_report_dataset() -> ReportDataset:
    return {
        "indices_domestic": [],
        "indices_overseas": [],
        "volatility": [],
        "treasuries": [],
        "commodities": [],
        "exchange": [],
        "crypto": [],
        "macro": [],
        "domestic_flow": [],
        "market_breadth": [],
        "sector_flow": [],
        "sector_performance": [],
        "domestic_state": [],
        "macro_context": [],
    }


def _fetch_rate_histories():
    histories = {}
    logger.info("Fetching YF rates history...")
    for name, ticker in YF_RATES_HISTORY.items():
        try:
            history = yf.Ticker(ticker).history(period="3mo")
            if not history.empty:
                histories[name] = history
        except Exception as exc:
            logger.error("Error fetching YF history for %s: %s", name, exc)
    return histories


def _append_cnbc_market_snapshots(
    results: ReportDataset,
    cnbc_data,
    treasury_histories=None,
) -> None:
    fetched_at = utc_now_iso()
    for symbol, category, value_format in (
        (".KSVKOSPI", "volatility", ValueFormat.STANDARD_2),
        ("JP10Y", "treasuries", ValueFormat.YIELD_3),
        ("KR10Y", "treasuries", ValueFormat.YIELD_3),
        ("US2Y", "treasuries", ValueFormat.YIELD_3),
        ("US10Y", "treasuries", ValueFormat.YIELD_3),
        ("US30Y", "treasuries", ValueFormat.YIELD_3),
    ):
        quote = cnbc_data.get(symbol)
        fred_series = (treasury_histories or {}).get(symbol)
        history = list(fred_series.values) if fred_series else []
        if quote is None and history:
            last_price = history[-1]
            change = last_price - history[-2] if len(history) > 1 else None
            as_of = fred_series.as_of
            stale = is_stale_as_of(as_of)
            warning = f"오래된 FRED 데이터: {as_of}" if stale else None
            results[category].append(
                build_snapshot(
                    CNBC_QUOTES[symbol]["name"],
                    None if warning else last_price,
                    None if warning else change,
                    value_format=value_format,
                    history=history[-21:],
                    change_5d=calculate_period_change(history, 5, value_format),
                    change_20d=calculate_period_change(history, 20, value_format),
                    z_score_20d=calculate_return_z_score(history),
                    as_of=as_of,
                    fetched_at=fetched_at,
                    source="FRED fallback",
                    is_stale=stale,
                    warning=warning,
                )
            )
            continue
        if quote is None:
            results[category].append(
                build_snapshot(
                    CNBC_QUOTES[symbol]["name"],
                    None,
                    value_format=value_format,
                    fetched_at=fetched_at,
                    source="CNBC",
                    warning="CNBC 수집 실패",
                )
            )
            continue

        try:
            item = coerce_cnbc_quote(quote)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Malformed CNBC quote for %s: %s", symbol, exc)
            results[category].append(
                build_snapshot(
                    CNBC_QUOTES[symbol]["name"],
                    None,
                    value_format=value_format,
                    fetched_at=fetched_at,
                    source="CNBC",
                    warning="CNBC 응답 형식 오류",
                )
            )
            continue
        if history and history[-1] != item.price:
            history.append(item.price)
        source = "CNBC/FRED" if history else "CNBC"
        warning = validate_value(item.name, item.price)
        results[category].append(
            build_snapshot(
                item.name,
                None if warning else item.price,
                None if warning else item.change,
                None if warning else item.change_pct,
                value_format=value_format,
                history=history[-21:],
                change_5d=calculate_period_change(history, 5, value_format),
                change_20d=calculate_period_change(history, 20, value_format),
                z_score_20d=calculate_return_z_score(history),
                as_of=fetched_at[:10],
                fetched_at=fetched_at,
                source=source,
                warning=warning,
            )
        )


def _append_yahoo_snapshots(results: ReportDataset, mode: str | None = None) -> None:
    selected_names = MODE_YF_NAMES.get((mode or "").upper())
    for category, definitions in YF_TICKERS.items():
        for definition in definitions:
            if selected_names is not None and definition.name not in selected_names:
                continue
            try:
                data = yf.Ticker(definition.symbol).history(period="3mo")
                if data.empty:
                    logger.warning(
                        "Yahoo Finance returned no history for %s (%s)",
                        definition.name,
                        definition.symbol,
                    )
                    results[category].append(
                        _missing_yahoo_snapshot(definition, "Yahoo Finance 빈 응답")
                    )
                    continue

                close_series = data["Close"].dropna()
                if close_series.empty:
                    results[category].append(
                        _missing_yahoo_snapshot(definition, "Yahoo Finance 종가 누락")
                    )
                    continue

                values = [float(value) for value in close_series.tolist()]
                last_price = values[-1]
                if len(values) > 1:
                    previous_price = values[-2]
                    change = last_price - previous_price
                    change_pct = (change / previous_price) * 100
                else:
                    change = 0.0
                    change_pct = 0.0

                as_of = close_series.index[-1].date().isoformat()
                stale = is_stale_as_of(as_of)
                warning = validate_value(definition.name, last_price)
                if stale:
                    warning = f"오래된 데이터: {as_of}"

                results[category].append(
                    build_snapshot(
                        definition.name,
                        None if warning else last_price,
                        None if warning else change,
                        None if warning else change_pct,
                        history=values[-21:],
                        ticker=definition.symbol,
                        dates=[
                            date.strftime("%m-%d")
                            for date in close_series.tail(21).index
                        ],
                        value_format=definition.value_format,
                        change_5d=calculate_period_change(
                            values, 5, definition.value_format
                        ),
                        change_20d=calculate_period_change(
                            values, 20, definition.value_format
                        ),
                        z_score_20d=calculate_return_z_score(values),
                        as_of=as_of,
                        fetched_at=utc_now_iso(),
                        source="Yahoo Finance",
                        is_stale=stale,
                        warning=warning,
                    )
                )
            except Exception as exc:
                logger.error("Error fetching YF %s: %s", definition.name, exc)
                results[category].append(_missing_yahoo_snapshot(definition, str(exc)))


def _append_us_yield_spread(treasuries) -> None:
    by_name = {item.name: item for item in treasuries}
    two_year = by_name.get("US 2Y Treasury")
    ten_year = by_name.get("US 10Y Treasury")
    fetched_at = utc_now_iso()
    if not two_year or not ten_year or two_year.price is None or ten_year.price is None:
        treasuries.append(
            build_snapshot(
                "US 10Y-2Y Spread",
                None,
                value_format=ValueFormat.BASIS_POINTS_1,
                fetched_at=fetched_at,
                source="Calculated from Treasury inputs",
                warning="2Y 또는 10Y 입력 누락",
            )
        )
        return

    if two_year.as_of and ten_year.as_of and two_year.as_of != ten_year.as_of:
        logger.warning(
            "US Treasury curve inputs have different dates: 2Y=%s, 10Y=%s",
            two_year.as_of,
            ten_year.as_of,
        )
        treasuries.append(
            build_snapshot(
                "US 10Y-2Y Spread",
                None,
                value_format=ValueFormat.BASIS_POINTS_1,
                fetched_at=fetched_at,
                source="Calculated from Treasury inputs",
                warning="2Y와 10Y 기준 거래일 불일치",
            )
        )
        return

    spread_bp = (ten_year.price - two_year.price) * 100
    spread_change_bp = None
    if two_year.change is not None and ten_year.change is not None:
        spread_change_bp = (ten_year.change - two_year.change) * 100

    treasuries.append(
        build_snapshot(
            "US 10Y-2Y Spread",
            spread_bp,
            spread_change_bp,
            None,
            value_format=ValueFormat.BASIS_POINTS_1,
            change_5d=_spread_period_change(ten_year, two_year, "change_5d"),
            change_20d=_spread_period_change(ten_year, two_year, "change_20d"),
            as_of=ten_year.as_of or two_year.as_of,
            fetched_at=fetched_at,
            source="Calculated from CNBC/FRED inputs",
            is_stale=two_year.is_stale or ten_year.is_stale,
        )
    )


def _spread_period_change(long_rate, short_rate, field):
    long_change = getattr(long_rate, field, None)
    short_change = getattr(short_rate, field, None)
    if long_change is None or short_change is None:
        return None
    return long_change - short_change


def _missing_yahoo_snapshot(definition, warning):
    return build_snapshot(
        definition.name,
        None,
        ticker=definition.symbol,
        value_format=definition.value_format,
        fetched_at=utc_now_iso(),
        source="Yahoo Finance",
        warning=warning,
    )


def _configure_runtime_cache() -> None:
    cache_dir = os.environ.get(
        "YFINANCE_CACHE_DIR",
        os.path.join(tempfile.gettempdir(), "macro-pulse-yfinance"),
    )
    os.makedirs(cache_dir, exist_ok=True)
    if hasattr(yf, "set_tz_cache_location"):
        yf.set_tz_cache_location(cache_dir)
