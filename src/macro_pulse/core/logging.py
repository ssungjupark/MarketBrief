from __future__ import annotations

import logging
import os


DEFAULT_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

_LOGGING_CONFIGURED = False


def configure_logging(level: str | None = None) -> None:
    global _LOGGING_CONFIGURED

    if _LOGGING_CONFIGURED:
        return

    resolved_level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    logging.basicConfig(
        level=getattr(logging, resolved_level, logging.INFO),
        format=os.environ.get("LOG_FORMAT", DEFAULT_LOG_FORMAT),
    )
    _LOGGING_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
