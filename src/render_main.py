from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

from macro_pulse.app.cli import main


KOREA_TZ = ZoneInfo("Asia/Seoul")
KR_INDEX_TICKERS = ("^KS11", "^KQ11")


def _last_trading_date(symbol: str):
    history = yf.Ticker(symbol).history(period="5d")
    if history.empty:
        return None
    return history.index[-1].date()


def _kr_market_traded_today() -> bool:
    today = datetime.now(KOREA_TZ).date()
    dates = []
    for symbol in KR_INDEX_TICKERS:
        try:
            last_date = _last_trading_date(symbol)
        except Exception:
            return True
        if last_date is None:
            return True
        dates.append(last_date)
    return any(last_date == today for last_date in dates)


async def _run() -> int:
    try:
        return await main(["--market", "AUTO"])
    except RuntimeError as exc:
        message = str(exc)
        if (
            "KRX critical data unavailable" in message
            and not _kr_market_traded_today()
        ):
            print("Korean market is closed today; skipping the KR close report.")
            return 0
        raise


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
