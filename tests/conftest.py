"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import db.models  # noqa: F401  (register ORM models on the metadata)
from config.settings import load_settings

#: Integration tests write into whichever environment scope this names. It is
#: deliberately not a real Binance environment value, so a stray test row can
#: never be mistaken for collected testnet or production data (ADR-0010).
TEST_ENVIRONMENT = "testnet"


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """A DB session bound to a transaction that is rolled back after each test.

    Skips the test if Postgres is not reachable, so the suite still passes with no
    database (the migrated schema is expected via `alembic upgrade head`).

    Note for anyone adding integration tests: the transaction rolls back, but
    queries inside it still **read committed rows** written by real runs. Never
    assert on a global "latest" or "count" query — assert on before/after deltas,
    or scope the query to a row you created in this test. This bit the sibling
    repo repeatedly.
    """
    engine = create_async_engine(load_settings().database_url)
    try:
        conn = await engine.connect()
    except Exception:  # pragma: no cover - environment-dependent
        await engine.dispose()
        pytest.skip("Postgres not reachable for integration tests")

    trans = await conn.begin()
    # create_savepoint: code under test may call session.commit(); that releases a
    # savepoint rather than the outer transaction, so the final rollback still
    # discards everything and tests stay isolated.
    session = AsyncSession(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()
