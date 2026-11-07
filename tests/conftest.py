import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"


def _resolve_test_db_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit

    base = os.environ.get("DATABASE_URL")
    if base:
        parsed = urlparse(base)
        return urlunparse(parsed._replace(path="/transfer_test"))

    db_port = os.environ.get("DB_PORT", "54329")
    return f"postgresql://transfer:transfer@localhost:{db_port}/transfer_test"


def _admin_db_url(test_url: str) -> str:
    parsed = urlparse(test_url)
    return urlunparse(parsed._replace(path="/transfer"))


@pytest.fixture(scope="session")
def test_db_url() -> str:
    url = _resolve_test_db_url()
    db_name = urlparse(url).path.lstrip("/")

    with psycopg.connect(_admin_db_url(url), autocommit=True, connect_timeout=3) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (db_name,),
        ).fetchone()
        if not exists:
            conn.execute(b"CREATE DATABASE transfer_test")

    # Пересоздаём схему на старте сессии, чтобы каждый прогон проверял все миграции с нуля.
    with psycopg.connect(url, autocommit=True, connect_timeout=3) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    return url
