import asyncio
import logging

from nexus_ai.main import JsonFormatter, app, health, lifespan, version


def test_health() -> None:
    assert asyncio.run(health()) == {"status": "ok"}


def test_version() -> None:
    assert asyncio.run(version())["product"] == "Nexus AI"


def test_json_formatter() -> None:
    record = logging.LogRecord("nexus", logging.INFO, __file__, 1, "hello", (), None)
    assert JsonFormatter().format(record) == ('{"level":"INFO","logger":"nexus","message":"hello"}')


def test_lifespan() -> None:
    async def exercise() -> None:
        async with lifespan(app):
            pass

    asyncio.run(exercise())
