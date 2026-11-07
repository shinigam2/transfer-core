from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request

from .balances import seed_clients
from .config import Settings, load_settings
from .db import DbPool, apply_migrations, create_pool
from .errors import register_error_handlers
from .rates import HttpRateSource, RateSource
from .reference import ReferenceData
from .routers.clients import router as clients_router
from .routers.quotes import router as quotes_router


def _utc_now() -> datetime:
    return datetime.now(UTC)


def create_app(
    settings: Settings | None = None,
    rate_source: RateSource | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        cfg = settings or load_settings()
        ref = ReferenceData.load(cfg.data_dir)
        rates = rate_source or HttpRateSource(cfg.mock_provider_url, max_age_sec=cfg.max_rate_age_sec)
        pool = create_pool(cfg.database_url)
        pool.open(wait=True)
        try:
            apply_migrations(pool)
            seed_clients(pool, ref.clients)
            app.state.settings = cfg
            app.state.ref = ref
            app.state.rate_source = rates
            app.state.clock = clock or _utc_now
            app.state.pool = pool
            yield
        finally:
            pool.close()

    application = FastAPI(title="Transfer Core", lifespan=lifespan)
    register_error_handlers(application)
    application.include_router(quotes_router)
    application.include_router(clients_router)

    @application.get("/health")
    def health(request: Request) -> dict[str, str]:
        pool: DbPool = request.app.state.pool
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}

    return application


app = create_app()