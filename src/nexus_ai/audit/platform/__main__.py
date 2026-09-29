"""Separate worker process: python -m nexus_ai.audit.platform [--once]."""

import argparse
import asyncio
import os
import signal
from contextlib import suppress

from pydantic import SecretStr

from nexus_ai.audit.platform.config import PlatformAuditSettings
from nexus_ai.audit.platform.database import PlatformAuditDatabase
from nexus_ai.audit.platform.worker import PlatformAuditWorker


async def run(settings: PlatformAuditSettings, *, once: bool = False) -> None:
    database = PlatformAuditDatabase(settings)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    try:
        worker = PlatformAuditWorker(database)
        while not stopped.is_set():
            processing = asyncio.create_task(worker.run_once())
            stopping = asyncio.create_task(stopped.wait())
            try:
                completed, _ = await asyncio.wait(
                    (processing, stopping), return_when=asyncio.FIRST_COMPLETED
                )
                if processing in completed:
                    await processing
                else:
                    processing.cancel()
            finally:
                processing.cancel()
                stopping.cancel()
                await asyncio.wait_for(
                    asyncio.gather(processing, stopping, return_exceptions=True),
                    timeout=settings.database_timeout_seconds,
                )
            if once:
                return
            with suppress(TimeoutError):
                await asyncio.wait_for(stopped.wait(), timeout=1)
    finally:
        await asyncio.wait_for(database.close(), timeout=settings.database_timeout_seconds)
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def main() -> int:
    parser = argparse.ArgumentParser(description="Independent platform audit worker")
    parser.add_argument("--once", action="store_true", help="process one bounded batch")
    args = parser.parse_args()
    configured = os.environ.get("NXS_AUDIT_PLATFORM_DSN")
    if not configured:
        parser.error("NXS_AUDIT_PLATFORM_DSN is required")
    try:
        settings = PlatformAuditSettings(database_dsn=SecretStr(configured))
        asyncio.run(run(settings, once=args.once))
    except Exception:
        print("Platform audit worker stopped; no successful processing is implied.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
