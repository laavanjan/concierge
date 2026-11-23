"""Background worker: consumes domain events from Redis Streams (and can poll IMAP) outside the web tier.

    python -m channels.worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket

from channels.notifier import handle_event
from common.config import get_settings
from common.events import RedisStreamEventBus, get_event_bus
from common.logging import setup_logging
from common.telemetry import setup_telemetry

log = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    setup_telemetry("concierge-worker")
    settings = get_settings()
    tasks = []
    bus = get_event_bus()
    if isinstance(bus, RedisStreamEventBus):
        consumer = f"{socket.gethostname()}-{os.getpid()}"
        tasks.append(asyncio.create_task(bus.run_consumer("notifier", consumer, handle_event)))
        log.info("event consumer %s started", consumer)
    if settings.imap_host and settings.imap_user:
        from agents.runtime import ConciergeRuntime
        from channels.email_channel import imap_poller

        runtime = await ConciergeRuntime(settings).start()
        tasks.append(asyncio.create_task(imap_poller(runtime)))
    if not tasks:
        log.warning("nothing to do: set REDIS_URL and/or IMAP_* settings")
        return
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
