import os

from alembic import context
from dotenv import load_dotenv

from finance_quant.persistence.database import database_url, make_engine
from finance_quant.persistence.schema import metadata

if env_file := os.environ.get("FINANCE_QUANT_ENV_FILE"):
    load_dotenv(env_file, override=False)


if context.is_offline_mode():
    context.configure(
        url=database_url(), target_metadata=metadata, literal_binds=True, dialect_opts={"paramstyle": "named"}
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    with make_engine().connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
