"""Long-running Telegram poller entry-point.

Used by the `telegram_bot` sidecar in `docker-compose.yml`. Loops on
`src.telegram_bot.poll_once`, sleeping briefly between empty polls so
we don't hammer the Telegram API.

Standalone usage:
    TG_BOT_TOKEN=12345:abcd uv run python -m src.telegram_bot_loop

Stops cleanly on SIGTERM / SIGINT (Docker `docker compose stop`).
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time

from src.telegram_bot import poll_once

logger = logging.getLogger(__name__)


_stop = False


def _request_stop(signum, frame):
    global _stop
    _stop = True
    logger.info("received signal %s — finishing current poll then exiting", signum)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        print("TG_BOT_TOKEN not set — refusing to start", file=sys.stderr)
        return 1

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    logger.info("telegram_bot_loop started; long-poll timeout=25s")
    consecutive_errors = 0
    while not _stop:
        try:
            out = poll_once(token, timeout_s=25)
        except Exception as e:
            logger.exception("poll_once raised — backing off")
            consecutive_errors += 1
            # Exponential backoff capped at 60s; resets on first success.
            time.sleep(min(60, 2 ** consecutive_errors))
            continue
        if not out.get("ok"):
            consecutive_errors += 1
            logger.warning("poll_once not ok: %s", out.get("error"))
            time.sleep(min(60, 2 ** consecutive_errors))
            continue
        consecutive_errors = 0
        # If there were no updates, the 25s long-poll already waited;
        # no further sleep needed. If there WERE updates, a tiny breather
        # avoids tight-looping when Telegram is sending burst traffic.
        if out.get("processed", 0) > 0:
            time.sleep(0.5)

    logger.info("telegram_bot_loop exiting cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
