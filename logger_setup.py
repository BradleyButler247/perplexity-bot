"""
logger_setup.py
---------------
Configures structured logging for the Polymarket trading bot.

Logs are written to both the console (stdout) and a rotating file at
logs/bot.log.  Import and call setup_logging() once at startup, then use
logging.getLogger(__name__) in every module.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler


def setup_logging(level: str = "INFO") -> logging.Logger:
    """
    Set up root logger with console + file handlers.

    Args:
        level: Log level string (DEBUG / INFO / WARNING / ERROR).

    Returns:
        The configured root logger.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Create logs directory if it doesn't exist
    os.makedirs("logs", exist_ok=True)

    # Root logger
    logger = logging.getLogger()
    logger.setLevel(numeric_level)

    # Avoid adding duplicate handlers on re-import
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Console handler ─────────────────────────────────────────────────────
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    # ── Rotating file handler (10 MB, keep 5 backups) ───────────────────────
    file_handler = RotatingFileHandler(
        "logs/bot.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # ── Silence noisy third-party loggers ────────────────────────────────────
    # httpx logs every single HTTP request at INFO level, which floods the
    # console during market enrichment (200+ lines per cycle).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    return logger


def get_trade_logger() -> logging.Logger:
    """Return the dedicated trade-execution sub-logger."""
    return logging.getLogger("bot.trade")


def get_strategy_logger(strategy_name: str) -> logging.Logger:
    """Return a named sub-logger for a specific strategy."""
    return logging.getLogger(f"bot.strategy.{strategy_name}")
