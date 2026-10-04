from __future__ import annotations

from datetime import date, datetime, timezone
from statistics import mean, pstdev
from typing import Sequence

from ..domain.models import ValueFormat


MAX_STALE_CALENDAR_DAYS = 4

EXPECTED_RANGES = {
    "DXY": (40.0, 200.0),
    "WTI": (-50.0, 300.0),
    "MOVE": (20.0, 400.0),
    "VIX": (0.0, 200.0),
    "USD/KRW": (500.0, 3000.0),
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_stale_as_of(as_of: str | None, today: date | None = None) -> bool:
    if not as_of:
        return False
    try:
        as_of_date = date.fromisoformat(as_of[:10])
    except ValueError:
        return True
    return ((today or datetime.now(timezone.utc).date()) - as_of_date).days > (
        MAX_STALE_CALENDAR_DAYS
    )


def validate_value(name: str, value: float | None) -> str | None:
    if value is None:
        return "수집 실패"
    if "Treasury" in name and not -2.0 <= value <= 25.0:
        return f"비정상 금리 범위: {value}"
    if name in {"KOSPI", "KOSDAQ", "S&P 500", "Nasdaq", "SOX", "Russell 2000"}:
        if value <= 0:
            return f"비정상 지수 범위: {value}"
    lower, upper = EXPECTED_RANGES.get(name, (-1e15, 1e15))
    if not lower <= value <= upper:
        return f"비정상 범위: {value}"
    return None


def calculate_period_change(
    values: Sequence[float],
    sessions: int,
    value_format: ValueFormat,
) -> float | None:
    if len(values) <= sessions:
        return None
    current = float(values[-1])
    base = float(values[-(sessions + 1)])
    if value_format == ValueFormat.YIELD_3:
        return (current - base) * 100
    if base == 0:
        return None
    return ((current / base) - 1) * 100


def calculate_return_z_score(values: Sequence[float]) -> float | None:
    if len(values) < 21:
        return None
    recent = [float(value) for value in values[-21:]]
    returns = [
        ((current / previous) - 1) * 100
        for previous, current in zip(recent, recent[1:])
        if previous != 0
    ]
    if len(returns) < 2:
        return None
    baseline = returns[:-1]
    volatility = pstdev(baseline)
    if volatility == 0:
        return 0.0 if returns[-1] == mean(baseline) else None
    return (returns[-1] - mean(baseline)) / volatility
