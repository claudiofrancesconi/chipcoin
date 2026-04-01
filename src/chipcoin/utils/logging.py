"""Logging configuration placeholders."""

from __future__ import annotations

import logging


def configure_logging(level: str = "INFO") -> None:
    """Configure application logging once with a readable runtime format."""

    resolved_level = getattr(logging, level.upper(), logging.INFO)
    if logging.getLogger().handlers:
        logging.getLogger().setLevel(resolved_level)
        return None
    logging.basicConfig(
        level=resolved_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return None
