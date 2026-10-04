import json
import os
from functools import lru_cache

from ..core.paths import resolve_project_path
from ..domain.models import ReportFormatConfig, normalize_report_format_config


DEFAULT_REPORT_FORMAT_CONFIG = "config/report_formats.json"


def resolve_report_format_config_path(config_path=None):
    configured_path = (
        config_path
        or os.environ.get("REPORT_FORMAT_CONFIG")
        or DEFAULT_REPORT_FORMAT_CONFIG
    )
    return resolve_project_path(configured_path)


@lru_cache(maxsize=8)
def load_report_format_config(config_path=None):
    config_file = resolve_report_format_config_path(config_path)
    with config_file.open("r", encoding="utf-8") as handle:
        return ReportFormatConfig.from_mapping(json.load(handle))


def get_mode_format(mode, format_config=None):
    normalized_mode = (mode or "").strip().upper()
    config = normalize_report_format_config(
        format_config or load_report_format_config()
    )
    modes = config.modes

    if normalized_mode not in modes:
        available_modes = ", ".join(sorted(modes))
        raise ValueError(
            f"Unsupported report format mode '{mode}'. Available modes: {available_modes}"
        )

    return modes[normalized_mode]


def get_screenshot_targets(mode, format_config=None):
    return list(get_mode_format(mode, format_config).screenshot_targets)
