import asyncio
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.db import UTCDateTime, make_engine, normalize_database_url
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    # Migrations must not import app types; UTCDateTime is a plain timestamptz on disk.
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_item=render_item,
        compare_type=True,
        render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
        **kwargs,
    )


def run_migrations_offline() -> None:
    url, _ = normalize_database_url(settings.database_url)
    _configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: sa.Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = make_engine(settings.database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
