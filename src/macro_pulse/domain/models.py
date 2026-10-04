from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence


class ValueFormat(StrEnum):
    STANDARD_2 = "standard_2"
    YIELD_3 = "yield_3"
    BASIS_POINTS_1 = "basis_points_1"
    INTEGER = "integer"
    KRW_100M = "krw_100m"
    PERCENT_2 = "percent_2"


@dataclass(slots=True, frozen=True)
class TickerDefinition:
    name: str
    symbol: str
    value_format: ValueFormat = ValueFormat.STANDARD_2


@dataclass(slots=True, frozen=True)
class CnbcQuote:
    price: float
    change: float
    change_pct: float
    name: str = ""

    @classmethod
    def from_mapping(cls, raw_quote: Mapping[str, Any]) -> "CnbcQuote":
        return cls(
            name=str(raw_quote.get("name", "")),
            price=float(raw_quote["price"]),
            change=float(raw_quote["change"]),
            change_pct=float(raw_quote["change_pct"]),
        )


@dataclass(slots=True, frozen=True)
class ExchangeRates:
    usd_krw: float | None = None
    usd_jpy: float | None = None
    eur_usd: float | None = None
    usd_cny: float | None = None

    _FIELD_BY_PAIR = {
        "USD/KRW": "usd_krw",
        "USD/JPY": "usd_jpy",
        "EUR/USD": "eur_usd",
        "USD/CNY": "usd_cny",
    }

    def get(self, pair: str) -> float | None:
        field_name = self._FIELD_BY_PAIR.get(pair)
        return getattr(self, field_name) if field_name else None

    def as_mapping(self) -> dict[str, float | None]:
        return {pair: self.get(pair) for pair in self._FIELD_BY_PAIR}

    @classmethod
    def from_mapping(cls, raw_rates: Mapping[str, Any]) -> "ExchangeRates":
        return cls(
            usd_krw=_coerce_optional_float(raw_rates.get("USD/KRW")),
            usd_jpy=_coerce_optional_float(raw_rates.get("USD/JPY")),
            eur_usd=_coerce_optional_float(raw_rates.get("EUR/USD")),
            usd_cny=_coerce_optional_float(raw_rates.get("USD/CNY")),
        )


@dataclass(slots=True, frozen=True)
class AssetSnapshot:
    name: str
    price: float | None = None
    change: float | None = None
    change_pct: float | None = None
    history: list[float] = field(default_factory=list)
    ticker: str | None = None
    dates: list[str] = field(default_factory=list)
    value_format: ValueFormat = ValueFormat.STANDARD_2
    change_5d: float | None = None
    change_20d: float | None = None
    z_score_20d: float | None = None
    as_of: str | None = None
    fetched_at: str | None = None
    source: str | None = None
    is_stale: bool = False
    warning: str | None = None

    @classmethod
    def from_mapping(cls, raw_item: Mapping[str, Any]) -> "AssetSnapshot":
        name = str(raw_item["name"])
        value_format = raw_item.get("value_format")
        if isinstance(value_format, ValueFormat):
            normalized_format = value_format
        elif value_format:
            normalized_format = ValueFormat(value_format)
        else:
            normalized_format = infer_value_format(name)

        return cls(
            name=name,
            price=_coerce_optional_float(raw_item.get("price")),
            change=_coerce_optional_float(raw_item.get("change")),
            change_pct=_coerce_optional_float(raw_item.get("change_pct")),
            history=_coerce_float_list(raw_item.get("history", [])),
            ticker=raw_item.get("ticker"),
            dates=[str(value) for value in raw_item.get("dates", [])],
            value_format=normalized_format,
            change_5d=_coerce_optional_float(raw_item.get("change_5d")),
            change_20d=_coerce_optional_float(raw_item.get("change_20d")),
            z_score_20d=_coerce_optional_float(raw_item.get("z_score_20d")),
            as_of=_coerce_optional_str(raw_item.get("as_of")),
            fetched_at=_coerce_optional_str(raw_item.get("fetched_at")),
            source=_coerce_optional_str(raw_item.get("source")),
            is_stale=bool(raw_item.get("is_stale", False)),
            warning=_coerce_optional_str(raw_item.get("warning")),
        )


@dataclass(slots=True, frozen=True)
class RenderedAssetSnapshot:
    name: str
    price_str: str
    change_str: str
    change_pct_str: str
    color_class: str
    sparkline: str


@dataclass(slots=True, frozen=True)
class SummarySectionConfig:
    title: str
    category: str
    items: list[str]

    @classmethod
    def from_mapping(cls, raw_section: Mapping[str, Any]) -> "SummarySectionConfig":
        return cls(
            title=str(raw_section["title"]),
            category=str(raw_section["category"]),
            items=[str(item) for item in raw_section.get("items", [])],
        )


@dataclass(slots=True, frozen=True)
class ModeFormatConfig:
    description: str = ""
    summary_sections: list[SummarySectionConfig] = field(default_factory=list)
    screenshot_targets: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, raw_mode: Mapping[str, Any]) -> "ModeFormatConfig":
        return cls(
            description=str(raw_mode.get("description", "")),
            summary_sections=[
                SummarySectionConfig.from_mapping(section)
                for section in raw_mode.get("summary_sections", [])
            ],
            screenshot_targets=[
                str(target) for target in raw_mode.get("screenshot_targets", [])
            ],
        )


@dataclass(slots=True, frozen=True)
class ReportFormatConfig:
    modes: dict[str, ModeFormatConfig]

    @classmethod
    def from_mapping(cls, raw_config: Mapping[str, Any]) -> "ReportFormatConfig":
        raw_modes = raw_config.get("modes", {})
        modes = {
            str(mode).strip().upper(): ModeFormatConfig.from_mapping(mode_config)
            for mode, mode_config in raw_modes.items()
        }
        if not modes:
            raise ValueError("Report format config must define at least one mode.")
        return cls(modes=modes)


ReportDataset = dict[str, list[AssetSnapshot]]


def infer_value_format(name: str) -> ValueFormat:
    if "Spread" in name:
        return ValueFormat.BASIS_POINTS_1
    if any(keyword in name for keyword in ("Bond", "Treasury", "Year")):
        return ValueFormat.YIELD_3
    return ValueFormat.STANDARD_2


def coerce_asset_snapshot(item: AssetSnapshot | Mapping[str, Any]) -> AssetSnapshot:
    if isinstance(item, AssetSnapshot):
        return item
    if isinstance(item, Mapping):
        return AssetSnapshot.from_mapping(item)
    raise TypeError(f"Unsupported asset snapshot payload: {type(item)!r}")


def normalize_dataset(
    data: Mapping[str, Sequence[AssetSnapshot | Mapping[str, Any]]],
) -> ReportDataset:
    return {
        str(category): [coerce_asset_snapshot(item) for item in items]
        for category, items in data.items()
    }


def normalize_report_format_config(
    format_config: ReportFormatConfig | Mapping[str, Any],
) -> ReportFormatConfig:
    if isinstance(format_config, ReportFormatConfig):
        return format_config
    if isinstance(format_config, Mapping):
        return ReportFormatConfig.from_mapping(format_config)
    raise TypeError(f"Unsupported report config payload: {type(format_config)!r}")


def coerce_cnbc_quote(quote: CnbcQuote | Mapping[str, Any]) -> CnbcQuote:
    if isinstance(quote, CnbcQuote):
        return quote
    if isinstance(quote, Mapping):
        return CnbcQuote.from_mapping(quote)
    raise TypeError(f"Unsupported CNBC quote payload: {type(quote)!r}")


def coerce_exchange_rates(
    rates: ExchangeRates | Mapping[str, Any],
) -> ExchangeRates:
    if isinstance(rates, ExchangeRates):
        return rates
    if isinstance(rates, Mapping):
        return ExchangeRates.from_mapping(rates)
    raise TypeError(f"Unsupported exchange rates payload: {type(rates)!r}")


def _coerce_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _coerce_float_list(values: Sequence[Any]) -> list[float]:
    return [float(value) for value in values]


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
