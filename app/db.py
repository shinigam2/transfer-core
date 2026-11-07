from pathlib import Path

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

DbPool = ConnectionPool[Connection[DictRow]]

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

def create_pool(database_url: str) -> DbPool:
    return ConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
        open=False
    )

def apply_migrations(pool: DbPool) -> None:
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        applied = {row["version"] for row in rows}

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue

            sql_bytes = path.read_bytes()

            with conn.transaction():
                conn.execute(sql_bytes)
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (path.name,),
                )
