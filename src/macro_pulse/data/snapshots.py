from __future__ import annotations

from collections.abc import Sequence

from ..domain.models import AssetSnapshot, ValueFormat


def build_snapshot(
    name: str,
    price: float | int | None = None,
    change: float | int | None = None,
    change_pct: float | int | None = None,
    history: Sequence[float | int] | None = None,
    *,
    ticker: str | None = None,
    dates: Sequence[str] | None = None,
    value_format: ValueFormat = ValueFormat.STANDARD_2,
    change_5d: float | int | None = None,
    change_20d: float | int | None = None,
    z_score_20d: float | int | None = None,
    as_of: str | None = None,
    fetched_at: str | None = None,
    source: str | None = None,
    is_stale: bool = False,
    warning: str | None = None,
) -> AssetSnapshot:
    normalized_history = [float(value) for value in history] if history else []
    if not normalized_history and price is not None:
        normalized_history = [float(price)]

    return AssetSnapshot(
        name=name,
        ticker=ticker,
        price=float(price) if price is not None else None,
        change=float(change) if change is not None else None,
        change_pct=float(change_pct) if change_pct is not None else None,
        history=normalized_history,
        dates=[str(value) for value in (dates or [])],
        value_format=value_format,
        change_5d=float(change_5d) if change_5d is not None else None,
        change_20d=float(change_20d) if change_20d is not None else None,
        z_score_20d=float(z_score_20d) if z_score_20d is not None else None,
        as_of=as_of,
        fetched_at=fetched_at,
        source=source,
        is_stale=is_stale,
        warning=warning,
    )
