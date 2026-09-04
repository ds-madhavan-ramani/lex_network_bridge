"""logging_utils.py — structured stdout logging. Unchanged from the
project-llm-wiki template."""

import logging
import sys

_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            stream=sys.stdout,
        )
        _CONFIGURED = True
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, project_code: str, **fields):
    """Structured single-line event log, greppable by EVENT tag."""
    kv = " ".join(f"{k}={v!r}" for k, v in fields.items())
    logger.info("EVENT=%s project=%s %s", event, project_code, kv)
