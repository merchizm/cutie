from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    level_name = os.environ.get("CUTIE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

