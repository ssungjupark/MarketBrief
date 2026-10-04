import re
import time
from html import unescape
from html.parser import HTMLParser
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ...core.logging import get_logger
from ...domain.models import CnbcQuote, ExchangeRates, coerce_cnbc_quote


logger = get_logger(__name__)

CNBC_MARKET_QUOTES = {
    ".KSVKOSPI": {
        "name": "VKOSPI",
        "url": "https://www.cnbc.com/quotes/.KSVKOSPI",
    },
    "JP10Y": {
        "name": "Japan 10Y Treasury",
        "url": "https://www.cnbc.com/quotes/JP10Y",
    },
    "KR10Y": {
        "name": "Korea 10Y Treasury",
        "url": "https://www.cnbc.com/quotes/KR10Y",
    },
    "US2Y": {
        "name": "US 2Y Treasury",
        "url": "https://www.cnbc.com/quotes/US2Y",
    },
    "US10Y": {
        "name": "US 10Y Treasury",
        "url": "https://www.cnbc.com/quotes/US10Y",
    },
    "US30Y": {
        "name": "US 30Y Treasury",
        "url": "https://www.cnbc.com/quotes/US30Y",
    },
}

CNBC_FX_QUOTES = {
    "JPY=": {
        "name": "USD/JPY",
        "url": "https://www.cnbc.com/quotes/jpy=",
    },
    "KRW=": {
        "name": "USD/KRW",
        "url": "https://www.cnbc.com/quotes/krw=",
    },
    "CNY=": {
        "name": "USD/CNY",
        "url": "https://www.cnbc.com/quotes/cny=",
    },
    "EUR=": {
        "name": "EUR/USD",
        "url": "https://www.cnbc.com/quotes/eur=",
    },
}

CNBC_QUOTES = {**CNBC_MARKET_QUOTES, **CNBC_FX_QUOTES}
CNBC_MARKET_SYMBOLS = tuple(CNBC_MARKET_QUOTES)
CNBC_FX_SYMBOLS = tuple(CNBC_FX_QUOTES)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}


class QuoteStripParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.container_depth = 0
        self.span_stack = []
        self.in_price = False
        self.current_change_direction = None
        self.change_direction = None
        self.price_chunks = []
        self.change_chunks = []

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        classes = set((attr_map.get("class") or "").split())

        if tag == "div" and self.container_depth == 0:
            if "QuoteStrip-lastPriceStripContainer" in classes:
                self.container_depth = 1
            return

        if self.container_depth == 0:
            return

        if tag == "div":
            self.container_depth += 1
            return

        if tag != "span":
            return

        if "QuoteStrip-lastPrice" in classes:
            self.span_stack.append("price")
            self.in_price = True
            return

        if any(css_class.startswith("QuoteStrip-change") for css_class in classes):
            self.span_stack.append("change")
            if "QuoteStrip-changeDown" in classes:
                self.current_change_direction = -1
            elif "QuoteStrip-changeUp" in classes:
                self.current_change_direction = 1
            else:
                self.current_change_direction = 0
            self.change_direction = self.current_change_direction
            return

        self.span_stack.append("other")

    def handle_endtag(self, tag):
        if self.container_depth == 0:
            return

        if tag == "div":
            self.container_depth -= 1
            if self.container_depth == 0:
                self.in_price = False
                self.current_change_direction = None
            return

        if tag != "span" or not self.span_stack:
            return

        span_role = self.span_stack.pop()
        if span_role == "price":
            self.in_price = False
        elif span_role == "change" and "change" not in self.span_stack:
            self.current_change_direction = None

    def handle_data(self, data):
        if self.container_depth == 0:
            return

        text = unescape(data).strip()
        if not text:
            return

        if self.in_price:
            self.price_chunks.append(text)
        elif self.current_change_direction is not None:
            self.change_chunks.append(text)


def _parse_numeric(raw_value, fallback_sign=None):
    normalized = unescape(raw_value).strip().replace(",", "").replace("%", "")
    if not normalized or normalized.upper() == "UNCH":
        return 0.0

    sign = fallback_sign if fallback_sign is not None else 1
    if normalized[0] in "+-":
        sign = -1 if normalized[0] == "-" else 1
        normalized = normalized[1:].strip()

    return sign * float(normalized)


def _parse_change_block(raw_value, fallback_sign=None):
    normalized = unescape(raw_value).strip()
    if not normalized or normalized.upper() == "UNCH":
        return 0.0, 0.0

    change_match = re.search(r"[+-]?\d[\d,]*(?:\.\d+)?", normalized)
    if not change_match:
        raise ValueError(f"Unable to parse CNBC change value: {raw_value}")

    change = _parse_numeric(change_match.group(0), fallback_sign=fallback_sign)

    change_pct_match = re.search(r"([+-]?\d[\d,]*(?:\.\d+)?)\s*%", normalized)
    change_pct = _parse_numeric(change_pct_match.group(1)) if change_pct_match else None

    return change, change_pct


def parse_cnbc_quote(html):
    parser = QuoteStripParser()
    parser.feed(html)

    if not parser.price_chunks:
        raise ValueError("CNBC quote page did not contain a last price block.")

    price = _parse_numeric("".join(parser.price_chunks))
    change, change_pct = _parse_change_block(
        "".join(parser.change_chunks),
        fallback_sign=parser.change_direction,
    )
    if change_pct is None:
        previous_close = price - change
        change_pct = (change / previous_close) * 100 if previous_close else 0.0

    return CnbcQuote(price=price, change=change, change_pct=change_pct)


def fetch_cnbc_quote(symbol, attempts=3, retry_delay=1):
    quote = CNBC_QUOTES.get(symbol)
    if not quote:
        raise KeyError(f"Unsupported CNBC symbol: {symbol}")

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            request = Request(quote["url"], headers=REQUEST_HEADERS)
            with urlopen(request, timeout=15) as response:
                html = response.read().decode("utf-8", "ignore")
            parsed = parse_cnbc_quote(html)
            return CnbcQuote(
                name=quote["name"],
                price=parsed.price,
                change=parsed.change,
                change_pct=parsed.change_pct,
            )
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == attempts:
                raise
            time.sleep(retry_delay)

    raise last_error


def fetch_cnbc_data(symbols):
    """
    Fetch quote data directly from CNBC quote pages.
    symbols: list of ticker strings (e.g., [".KSVKOSPI", "JP10Y", "KR10Y", "KRW="])
    Returns: dict {symbol: {'price': float, 'change': float, 'change_pct': float, 'name': str}}
    """
    results = {}

    for symbol in symbols:
        if symbol not in CNBC_QUOTES:
            logger.warning("Unsupported CNBC symbol requested: %s", symbol)
            continue

        try:
            results[symbol] = fetch_cnbc_quote(symbol)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            logger.error("Failed to fetch CNBC quote for %s: %s", symbol, exc)
        except Exception:
            logger.exception("Unexpected CNBC fetch error for %s", symbol)

    return results


def extract_cnbc_exchange_rates(
    quotes: Mapping[str, CnbcQuote | Mapping[str, float | str]],
) -> ExchangeRates:
    def get_price(symbol):
        quote = quotes.get(symbol)
        if quote is None:
            return None
        return coerce_cnbc_quote(quote).price

    return ExchangeRates(
        usd_krw=get_price("KRW="),
        usd_jpy=get_price("JPY="),
        eur_usd=get_price("EUR="),
        usd_cny=get_price("CNY="),
    )
