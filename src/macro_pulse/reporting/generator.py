import base64
import io
import os

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib
import matplotlib.pyplot as plt
from jinja2 import Environment, FileSystemLoader

from ..config.report_formats import get_mode_format, load_report_format_config
from ..core.logging import get_logger
from ..core.paths import PACKAGE_ROOT, resolve_project_path
from ..domain.models import RenderedAssetSnapshot, ValueFormat, normalize_dataset


matplotlib.use("Agg")

logger = get_logger(__name__)

DEFAULT_TEMPLATE_DIR = PACKAGE_ROOT / "reporting" / "templates"


def generate_sparkline(history):
    figure, axis = plt.subplots(figsize=(2, 0.5))
    axis.plot(
        history,
        color="#2ecc71" if history[-1] >= history[0] else "#e74c3c",
        linewidth=2,
    )
    axis.axis("off")
    figure.tight_layout(pad=0)

    image = io.BytesIO()
    figure.savefig(image, format="png", transparent=True)
    image.seek(0)
    plt.close(figure)

    return base64.b64encode(image.getvalue()).decode("utf-8")


def generate_html_report(data, template_dir=None):
    normalized_data = normalize_dataset(data)
    logger.info("Generating HTML report for %s categories", len(normalized_data))
    rendered_data = {
        category: [_render_item(item) for item in items]
        for category, items in normalized_data.items()
    }

    env = Environment(loader=FileSystemLoader(_resolve_template_dir(template_dir)))
    template = env.get_template("report.html")
    return template.render(data=rendered_data)


def generate_telegram_summary(data, mode="Global", format_config=None):
    normalized_data = normalize_dataset(data)
    logger.info("Generating Telegram summary for mode=%s", mode)

    def format_line(item):
        if item.price is None:
            detail = f" ({item.warning})" if item.warning else ""
            return f"{item.name}: N/A{detail}"

        price_str = _format_numeric(item.price, item.value_format)

        if item.value_format == ValueFormat.BASIS_POINTS_1:
            if item.change not in (None, 0):
                return f"{item.name}: {price_str}bp ({item.change:+,.1f}bp)"
            return f"{item.name}: {price_str}bp"

        if item.value_format == ValueFormat.YIELD_3:
            if item.change not in (None, 0):
                change_bp = item.change * 100
                return f"{item.name}: {price_str}% ({change_bp:+,.1f}bp)"
            return f"{item.name}: {price_str}%"

        if item.value_format == ValueFormat.KRW_100M:
            return _format_flow_line(item)

        if item.value_format == ValueFormat.INTEGER:
            return f"{item.name}: {price_str}개"

        if item.value_format == ValueFormat.PERCENT_2:
            return f"{item.name}: {item.price:+,.2f}%"

        if item.change_pct not in (None, 0):
            return f"{item.name}: {price_str} ({item.change_pct:+,.2f}%)"

        return f"{item.name}: {price_str}"

    def get_items(category, names):
        source_items = normalized_data.get(category, [])
        found_items = []

        for name in names:
            if name.endswith("*"):
                prefix = name[:-1]
                found_items.extend(
                    item for item in source_items if item.name.startswith(prefix)
                )
                continue
            for item in source_items:
                if item.name == name:
                    found_items.append(item)
                    break

        return found_items

    mode_format = get_mode_format(
        mode,
        format_config or load_report_format_config(),
    )

    lines = []

    for index, section in enumerate(mode_format.summary_sections):
        lines.append(f"[{section.title}]")

        for item in get_items(section.category, section.items):
            lines.append(format_line(item))

        if section.category == "treasuries" and "US 10Y-2Y Spread" in section.items:
            curve_text = _interpret_us_yield_curve(normalized_data)
            if curve_text:
                lines.append(curve_text)

        if index < len(mode_format.summary_sections) - 1:
            lines.append("")

    return "\n".join(lines)


def _format_flow_line(item):
    base = f"{item.name}: {item.price:+,.0f}억원"
    if item.change_5d is not None:
        base += f" | 5일 {item.change_5d:+,.0f}억원"
    if item.change_20d is not None:
        base += f" | 20일 {item.change_20d:+,.0f}억원"
    if item.z_score_20d is not None:
        z_label = ""
        if item.z_score_20d >= 2:
            z_label = " (이례적 순매수)"
        elif item.z_score_20d <= -2:
            z_label = " (이례적 순매도)"
        base += f" | 20일 z {item.z_score_20d:+.1f}{z_label}"

    streak = _flow_streak(item.history)
    if streak:
        base += f" | {streak}"
    return base


def _flow_streak(history):
    if len(history) < 2 or history[-1] == 0:
        return ""
    positive = history[-1] > 0
    count = 0
    for value in reversed(history):
        if value == 0 or (value > 0) != positive:
            break
        count += 1
    if count < 2:
        return ""
    direction = "순매수" if positive else "순매도"
    return f"{count}거래일 연속 {direction}"


def _resolve_template_dir(template_dir):
    if template_dir is None:
        return str(DEFAULT_TEMPLATE_DIR)
    return str(resolve_project_path(template_dir))


def _render_item(item) -> RenderedAssetSnapshot:
    sparkline = generate_sparkline(item.history) if len(item.history) > 1 else ""
    change_str = ""
    change_pct_str = ""
    color_class = "neutral"

    if item.change is not None:
        change_str = _format_signed_numeric(item.change, item.value_format)
        change_pct_str = (
            f"{item.change_pct:+,.2f}%" if item.change_pct is not None else ""
        )
        color_class = (
            "positive"
            if item.change > 0
            else "negative"
            if item.change < 0
            else "neutral"
        )

    return RenderedAssetSnapshot(
        name=item.name,
        price_str=_format_numeric(item.price, item.value_format),
        change_str=change_str,
        change_pct_str=change_pct_str,
        color_class=color_class,
        sparkline=sparkline,
    )


def _format_numeric(value, value_format):
    if value is None:
        return ""
    if value_format == ValueFormat.YIELD_3:
        decimals = 3
    elif value_format == ValueFormat.BASIS_POINTS_1:
        decimals = 1
    elif value_format in (
        ValueFormat.INTEGER,
        ValueFormat.KRW_100M,
        ValueFormat.PERCENT_2,
    ):
        decimals = 0
    else:
        decimals = 2
    return f"{value:,.{decimals}f}"


def _format_signed_numeric(value, value_format):
    if value is None:
        return ""
    if value_format == ValueFormat.YIELD_3:
        decimals = 3
    elif value_format == ValueFormat.BASIS_POINTS_1:
        decimals = 1
    elif value_format in (
        ValueFormat.INTEGER,
        ValueFormat.KRW_100M,
        ValueFormat.PERCENT_2,
    ):
        decimals = 0
    else:
        decimals = 2
    return f"{value:+,.{decimals}f}"


def _interpret_us_yield_curve(data):
    spread = next(
        (
            item
            for item in data.get("treasuries", [])
            if item.name == "US 10Y-2Y Spread" and item.price is not None
        ),
        None,
    )
    if spread is None:
        return ""

    if spread.price < 0:
        shape = "역전, 경기 둔화 및 침체 위험을 경계할 구간"
    elif spread.price < 25:
        shape = "평탄, 정책 전환 기대와 경기 불확실성이 맞서는 구간"
    else:
        shape = "정상 우상향, 장기금리가 단기금리보다 높은 구간"

    movement = ""
    if spread.change is not None:
        if spread.change >= 5:
            movement = ", 전일보다 가팔라짐"
        elif spread.change <= -5:
            movement = ", 전일보다 평탄해짐"

    return f"금리 커브: {shape}{movement}"
