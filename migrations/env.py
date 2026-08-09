"""Alembic migration environment.

Runs with a *sync* engine over psycopg3 (the application uses async at runtime).
The database URL and target metadata come from application code, so there is a
single source of truth and no credentials live in alembic.ini.
"""

from __future__ import annotations

import pathlib
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# Make the `packages/` code importable.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "packages"))

import db.models  # noqa: F401  (import registers models on the metadata)
from config.settings import load_settings
from db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
database_url = load_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(database_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
