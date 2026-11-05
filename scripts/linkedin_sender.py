#!/usr/bin/env python3
"""The LinkedIn send queue's background sender
(docs/superpowers/specs/2026-11-05-linkedin-send-queue-design.md). launchd runs it every
2 minutes (scripts/install-linkedin-sender.sh); each run sends at most one message, and
only inside the window, under the ceiling, after the gap, and while not paused.

  .venv/bin/python scripts/linkedin_sender.py

Logs one word per run (what happened), never message text. Local only.
"""

import logging
import os
import random
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import clients.otel as otel
from clients import linkedin_mcp
from services import linkedin_outbox, linkedin_pacing

logger = logging.getLogger("linkedin_sender")


def main_with(
    get_conn: Callable[[], Any],
    *,
    now: datetime | None = None,
    sender_factory: Callable[[], Any] | None = None,
    env: Mapping[str, str] = os.environ,
) -> int:
    otel.setup_telemetry("people-linkedin-sender")
    try:
        try:
            cfg = linkedin_pacing.load_config(env)
        except ValueError as e:
            logger.error("linkedin sender: bad config: %s", e)
            return 2
        factory = sender_factory or (lambda: linkedin_mcp.open_sender(cfg.mcp_version))
        with get_conn() as conn:
            word = linkedin_outbox.run_once(
                conn, factory, now=now or datetime.now(UTC), cfg=cfg, rng=random.SystemRandom()
            )
        logger.info("linkedin sender: %s", word)
        return 0
    finally:
        otel.flush()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from clients.db import get_conn

    sys.exit(main_with(get_conn))


if __name__ == "__main__":
    main()
