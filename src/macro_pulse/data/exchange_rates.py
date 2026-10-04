from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..core.logging import get_logger
from ..domain.models import CnbcQuote, ValueFormat, coerce_cnbc_quote
from .providers.cnbc import extract_cnbc_exchange_rates
from .quality import calculate_period_change, calculate_return_z_score, utc_now_iso
from .snapshots import build_snapshot


logger = get_logger(__name__)


def build_exchange_snapshots(
    quotes: Mapping[str, CnbcQuote | Mapping[str, Any]],
    rate_histories: Mapping[str, Any] | None = None,
):
    logger.info("Building FX data from CNBC quotes...")
    histories = rate_histories or {}
    rates = extract_cnbc_exchange_rates(quotes)
    fetched_at = utc_now_iso()

    usd_krw_quote = _get_quote(quotes, "KRW=")
    if usd_krw_quote is None or rates.usd_krw is None:
        logger.warning("CNBC FX quotes failed. Data might be incomplete.")
        return unavailable_exchange_snapshots("CNBC 기준 환율 수집 실패", fetched_at)

    usd_history = _history_values(
        histories.get("USD/KRW"),
        fallback=[usd_krw_quote.price],
    )
    usd_history = _append_current(usd_history, usd_krw_quote.price)
    as_of = fetched_at
    snapshots = [
        build_snapshot(
            "USD/KRW",
            usd_krw_quote.price,
            usd_krw_quote.change,
            usd_krw_quote.change_pct,
            history=usd_history,
            change_5d=calculate_period_change(usd_history, 5, ValueFormat.STANDARD_2),
            change_20d=calculate_period_change(usd_history, 20, ValueFormat.STANDARD_2),
            z_score_20d=calculate_return_z_score(usd_history),
            as_of=as_of[:10],
            fetched_at=as_of,
            source="CNBC/Yahoo Finance",
        )
    ]

    usd_krw_previous = _previous_close(quotes, "KRW=")

    if rates.usd_jpy is not None:
        jpy_krw_price = (rates.usd_krw / rates.usd_jpy) * 100
        usd_jpy_previous = _previous_close(quotes, "JPY=")
        previous_jpy_krw = None
        if usd_krw_previous not in (None, 0) and usd_jpy_previous not in (None, 0):
            previous_jpy_krw = (usd_krw_previous / usd_jpy_previous) * 100
        change, change_pct = _cross_change(jpy_krw_price, previous_jpy_krw)
        jpy_history = _history_values(
            histories.get("JPY/KRW"),
            scale=100,
            fallback=[jpy_krw_price],
        )
        jpy_history = _append_current(jpy_history, jpy_krw_price)
        snapshots.append(
            build_snapshot(
                "JPY/KRW",
                jpy_krw_price,
                change,
                change_pct,
                history=jpy_history,
                change_5d=calculate_period_change(
                    jpy_history, 5, ValueFormat.STANDARD_2
                ),
                change_20d=calculate_period_change(
                    jpy_history, 20, ValueFormat.STANDARD_2
                ),
                z_score_20d=calculate_return_z_score(jpy_history),
                as_of=as_of[:10],
                fetched_at=as_of,
                source="CNBC/Yahoo Finance",
            )
        )
    else:
        snapshots.append(
            _missing_exchange_snapshot("JPY/KRW", fetched_at, "USD/JPY 수집 실패")
        )

    if rates.eur_usd is not None:
        eur_krw_price = rates.usd_krw * rates.eur_usd
        eur_usd_previous = _previous_close(quotes, "EUR=")
        previous_eur_krw = None
        if usd_krw_previous not in (None, 0) and eur_usd_previous not in (None, 0):
            previous_eur_krw = usd_krw_previous * eur_usd_previous
        change, change_pct = _cross_change(eur_krw_price, previous_eur_krw)
        eur_history = _history_values(
            histories.get("EUR/KRW"),
            fallback=[eur_krw_price],
        )
        eur_history = _append_current(eur_history, eur_krw_price)
        snapshots.append(
            build_snapshot(
                "EUR/KRW",
                eur_krw_price,
                change,
                change_pct,
                history=eur_history,
                change_5d=calculate_period_change(
                    eur_history, 5, ValueFormat.STANDARD_2
                ),
                change_20d=calculate_period_change(
                    eur_history, 20, ValueFormat.STANDARD_2
                ),
                z_score_20d=calculate_return_z_score(eur_history),
                as_of=as_of[:10],
                fetched_at=as_of,
                source="CNBC/Yahoo Finance",
            )
        )
    else:
        snapshots.append(
            _missing_exchange_snapshot("EUR/KRW", fetched_at, "EUR/USD 수집 실패")
        )

    if rates.usd_cny is not None:
        cny_krw_price = rates.usd_krw / rates.usd_cny
        usd_cny_previous = _previous_close(quotes, "CNY=")
        previous_cny_krw = None
        if usd_krw_previous not in (None, 0) and usd_cny_previous not in (None, 0):
            previous_cny_krw = usd_krw_previous / usd_cny_previous
        change, change_pct = _cross_change(cny_krw_price, previous_cny_krw)
        snapshots.append(
            build_snapshot(
                "CNY/KRW",
                cny_krw_price,
                change,
                change_pct,
                as_of=as_of[:10],
                fetched_at=as_of,
                source="Calculated from CNBC",
            )
        )
    else:
        snapshots.append(
            _missing_exchange_snapshot("CNY/KRW", fetched_at, "USD/CNY 수집 실패")
        )

    return snapshots


def _get_quote(
    quotes: Mapping[str, CnbcQuote | Mapping[str, Any]],
    symbol: str,
) -> CnbcQuote | None:
    quote = quotes.get(symbol)
    return coerce_cnbc_quote(quote) if quote is not None else None


def _previous_close(
    quotes: Mapping[str, CnbcQuote | Mapping[str, Any]],
    symbol: str,
) -> float | None:
    quote = _get_quote(quotes, symbol)
    if quote is None:
        return None
    return quote.price - quote.change


def _cross_change(
    current_price: float | None,
    previous_price: float | None,
) -> tuple[float, float]:
    if current_price is None or previous_price in (None, 0):
        return 0.0, 0.0

    change = current_price - previous_price
    return change, (change / previous_price) * 100


def _history_values(
    history_frame: Any,
    *,
    scale: float = 1.0,
    fallback: list[float] | None = None,
) -> list[float]:
    if history_frame is None or history_frame.empty:
        return list(fallback or [])

    series = history_frame["Close"] * scale
    return [float(value) for value in series.dropna().tail(21).tolist()]


def _append_current(values: list[float], current: float) -> list[float]:
    combined = list(values)
    if not combined or combined[-1] != current:
        combined.append(current)
    return combined[-21:]


def _missing_exchange_snapshot(name: str, fetched_at: str, warning: str):
    return build_snapshot(
        name,
        None,
        fetched_at=fetched_at,
        source="CNBC",
        warning=warning,
    )


def unavailable_exchange_snapshots(
    warning: str,
    fetched_at: str | None = None,
):
    timestamp = fetched_at or utc_now_iso()
    return [
        _missing_exchange_snapshot(name, timestamp, warning)
        for name in ("USD/KRW", "JPY/KRW", "EUR/KRW", "CNY/KRW")
    ]
