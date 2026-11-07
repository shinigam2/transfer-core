from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request

from .balances import seed_clients
from .config import Settings, load_settings
from .db import DbPool, apply_migrations, create_pool
from .errors import register_error_handlers
from .provider_client import HttpProviderClient, ProviderClient
from .rates import HttpRateSource, RateSource
from .reference import ReferenceData
from .routers.clients import router as clients_router
from .routers.quotes import router as quotes_router
from .routers.transfers import router as transfers_router
from .routers.webhooks import router as webhooks_router
from .transfers_service import TransferRuntime, TransferService


def _utc_now() -> datetime:
    return datetime.now(UTC)


def create_app(
    settings: Settings | None = None,
    rate_source: RateSource | None = None,
    provider_client: ProviderClient | None = None,
    clock: Callable[[], datetime] | None = None,
    auto_dispatch: bool = True,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        cfg = settings or load_settings()
        ref = ReferenceData.load(cfg.data_dir)
        rates = rate_source or HttpRateSource(cfg.mock_provider_url, max_age_sec=cfg.max_rate_age_sec)
        prov_client = provider_client or HttpProviderClient(
            cfg.mock_provider_url,
            timeout_sec=cfg.provider_timeout_sec,
        )
        now_fn = clock or _utc_now

        pool = create_pool(cfg.database_url)
        pool.open(wait=True)
        runtime: TransferRuntime | None = None
        try:
            apply_migrations(pool)
            seed_clients(pool, ref.clients)

            service = TransferService(
                pool=pool,
                ref=ref,
                rate_source=rates,
                provider_client=prov_client,
                settings=cfg,
                clock=now_fn,
            )
            runtime = TransferRuntime(
                service=service,
                reconcile_interval_sec=cfg.reconcile_interval_sec,
                auto_dispatch=auto_dispatch,
            )
            runtime.start()

            app.state.settings = cfg
            app.state.ref = ref
            app.state.rate_source = rates
            app.state.provider_client = prov_client
            app.state.clock = now_fn
            app.state.pool = pool
            app.state.transfer_service = service
            app.state.transfer_runtime = runtime
            yield
        finally:
            if runtime is not None:
                runtime.stop()
            pool.close()

    application = FastAPI(title="Transfer Core", lifespan=lifespan)
    register_error_handlers(application)
    application.include_router(quotes_router)
    application.include_router(clients_router)
    application.include_router(transfers_router)
    application.include_router(webhooks_router)

    @application.get("/health")
    def health(request: Request) -> dict[str, str]:
        pool: DbPool = request.app.state.pool
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}

    return application


app = create_app()