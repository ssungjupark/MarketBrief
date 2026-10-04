from __future__ import annotations

from .domain.models import ReportDataset, ValueFormat


# 자산별 "눈여겨볼 만한 하루 움직임" 기준
PCT_THRESHOLDS = {
    "indices_domestic": 1.0,  # 주가지수 ±1.0%
    "indices_overseas": 1.0,  # 주가지수 ±1.0%
    "volatility": 5.0,  # VIX, VKOSPI ±5.0%
    "commodities": 1.5,  # 금, 은, 구리 ±1.5%
    "treasuries": 1.0,  # 실제 판정은 아래 bp 기준 사용
    "exchange": 0.5,  # 환율 ±0.5%
    "crypto": 3.0,  # 비트코인, 이더리움 ±3.0%
    "macro": 0.5,  # 달러인덱스 ±0.5%
    "domestic_flow": 1.0,
    "market_breadth": 1.0,
}

# 국채금리는 하루 5bp 이상 움직이면 신호로 판단
YIELD_BP_THRESHOLD = 5.0
ZSCORE_THRESHOLD = 2.0

SIGNAL_PRIORITY = {
    "EUR/KRW": 0.30,
    "CNY/KRW": 0.30,
    "JPY/KRW": 0.70,
    "SOX": 1.15,
    "DXY": 1.15,
    "WTI": 1.15,
    "MOVE": 1.15,
}

SIGNAL_GROUPS = {
    "S&P 500": "us_equities",
    "Nasdaq": "us_equities",
    "SOX": "us_equities",
    "Russell 2000": "us_equities",
    "Nikkei 225": "asia_equities",
    "Hang Seng": "asia_equities",
    "Shanghai Composite": "asia_equities",
    "Gold": "metals",
    "Silver": "metals",
    "Copper": "metals",
    "Bitcoin": "crypto",
    "Ethereum": "crypto",
    "EUR/KRW": "derived_fx",
    "CNY/KRW": "derived_fx",
    "JPY/KRW": "derived_fx",
}

SIGNAL_GROUP_LIMITS = {
    "us_equities": 2,
    "asia_equities": 2,
    "metals": 1,
    "crypto": 1,
    "derived_fx": 1,
}


def detect_signals(data: ReportDataset) -> list[dict]:
    signals = []

    for category, items in data.items():
        if category not in PCT_THRESHOLDS:
            continue
        threshold = PCT_THRESHOLDS[category]

        for item in items:
            if item.price is None:
                continue

            if item.value_format in (
                ValueFormat.INTEGER,
                ValueFormat.KRW_100M,
                ValueFormat.PERCENT_2,
            ):
                continue

            if item.value_format == ValueFormat.BASIS_POINTS_1:
                if item.change is None:
                    continue
                if abs(item.change) >= YIELD_BP_THRESHOLD:
                    signals.append(
                        _signal_payload(
                            item,
                            {
                                "name": item.name,
                                "category": category,
                                "direction": ("확대" if item.change > 0 else "축소"),
                                "move": f"{item.change:+.1f}bp",
                                "score": abs(item.change) / YIELD_BP_THRESHOLD,
                            },
                        )
                    )
                continue

            # 금리는 % 등락률이 아니라 bp 변화로 판단
            if item.value_format == ValueFormat.YIELD_3:
                if item.change is None:
                    continue

                change_bp = item.change * 100

                if abs(change_bp) >= YIELD_BP_THRESHOLD:
                    signals.append(
                        _signal_payload(
                            item,
                            {
                                "name": item.name,
                                "category": category,
                                "direction": ("상승" if change_bp > 0 else "하락"),
                                "move": f"{change_bp:+.1f}bp",
                                "score": abs(change_bp) / YIELD_BP_THRESHOLD,
                            },
                        )
                    )

                continue

            # 나머지 자산은 하루 등락률 기준
            if item.change_pct is None:
                continue

            z_score_triggered = (
                item.z_score_20d is not None
                and abs(item.z_score_20d) >= ZSCORE_THRESHOLD
            )
            if abs(item.change_pct) >= threshold or z_score_triggered:
                signals.append(
                    _signal_payload(
                        item,
                        {
                            "name": item.name,
                            "category": category,
                            "direction": ("상승" if item.change_pct > 0 else "하락"),
                            "move": f"{item.change_pct:+.2f}%",
                            "score": max(
                                abs(item.change_pct) / threshold,
                                abs(item.z_score_20d or 0) / ZSCORE_THRESHOLD,
                            ),
                        },
                    )
                )

    best_by_name = {}
    for signal in signals:
        existing = best_by_name.get(signal["name"])
        if existing is None or signal["score"] > existing["score"]:
            best_by_name[signal["name"]] = signal

    ranked = list(best_by_name.values())
    ranked.sort(
        key=lambda signal: signal["score"],
        reverse=True,
    )

    return ranked


def select_representative_signals(
    signals: list[dict],
    limit: int = 5,
) -> list[dict]:
    selected = []
    group_counts: dict[str, int] = {}
    for signal in signals:
        group = SIGNAL_GROUPS.get(signal["name"])
        if group:
            group_limit = SIGNAL_GROUP_LIMITS[group]
            if group_counts.get(group, 0) >= group_limit:
                continue
            group_counts[group] = group_counts.get(group, 0) + 1
        selected.append(signal)
        if len(selected) >= limit:
            break
    return selected


def _signal_payload(item, payload):
    priority = SIGNAL_PRIORITY.get(item.name, 1.0)
    payload["score"] *= priority
    payload["change_5d"] = item.change_5d
    payload["change_20d"] = item.change_20d
    payload["z_score_20d"] = item.z_score_20d
    payload["value_format"] = item.value_format
    return payload


def format_signal_context(signal: dict) -> str:
    parts = []
    suffix = (
        "bp"
        if signal.get("value_format")
        in (ValueFormat.YIELD_3, ValueFormat.BASIS_POINTS_1)
        else "%"
    )
    if signal.get("change_5d") is not None:
        parts.append(f"5일 {signal['change_5d']:+.1f}{suffix}")
    if signal.get("change_20d") is not None:
        parts.append(f"20일 {signal['change_20d']:+.1f}{suffix}")
    if signal.get("z_score_20d") is not None:
        parts.append(f"20일 z {signal['z_score_20d']:+.1f}")
    return " | ".join(parts)
