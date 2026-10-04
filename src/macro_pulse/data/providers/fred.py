from __future__ import annotations

import csv
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ...core.logging import get_logger


logger = get_logger(__name__)

FRED_SERIES = {
    "US2Y": "DGS2",
    "US10Y": "DGS10",
    "US30Y": "DGS30",
}


@dataclass(slots=True, frozen=True)
class TreasuryHistory:
    values: list[float]
    as_of: str


def fetch_treasury_histories(
    today: date | None = None,
) -> dict[str, TreasuryHistory]:
    end = today or date.today()
    start = end - timedelta(days=120)
    histories = {}
    with ThreadPoolExecutor(max_workers=len(FRED_SERIES)) as executor:
        futures = {
            executor.submit(_fetch_series, series_id, start, end): symbol
            for symbol, series_id in FRED_SERIES.items()
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                history = future.result()
            except Exception as exc:
                logger.warning("FRED series %s failed: %s", symbol, exc)
                continue
            if history:
                histories[symbol] = history
    return histories


def _fetch_series(
    series_id: str,
    start: date,
    end: date,
) -> TreasuryHistory | None:
    url = (
        "https://fred.stlouisfed.org/graph/fredgraph.csv"
        f"?id={series_id}&cosd={start.isoformat()}&coed={end.isoformat()}"
    )
    request = Request(url, headers={"User-Agent": "Macro-Pulse/1.0"})
    try:
        with urlopen(request, timeout=20) as response:
            text = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError) as exc:
        logger.warning("FRED series %s unavailable: %s", series_id, exc)
        return None

    values = []
    as_of = None
    for row in csv.DictReader(io.StringIO(text)):
        raw_value = row.get(series_id)
        if raw_value in (None, "", "."):
            continue
        try:
            values.append(float(raw_value))
            as_of = row.get("DATE") or row.get("observation_date")
        except ValueError:
            continue
    if not values or not as_of:
        return None
    return TreasuryHistory(values=values, as_of=as_of)
