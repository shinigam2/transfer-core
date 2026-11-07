from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .config import load_settings
from .db import DbPool, apply_migrations, create_pool


# lifespan управляет стартом и остановкой сервера:
# всё до `yield` выполняется один раз при запуске, всё в `finally` — при выключении.
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    pool = create_pool(settings.database_url)
    pool.open(wait=True)
    try:
        apply_migrations(pool)
        app.state.settings = settings
        app.state.pool = pool
        yield
    finally:
        pool.close()


app = FastAPI(title="Transfer Core", lifespan=lifespan)


@app.get("/health")
def health(request: Request) -> dict[str, str]:
    pool: DbPool = request.app.state.pool
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}