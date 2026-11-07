from typing import Any

from fastapi import APIRouter, Request

from ..balances import get_client_balances
from ..db import DbPool
from ..reference import ReferenceData

router = APIRouter(tags=["clients"])


@router.get("/clients/{client_id}/balance")
def get_balance_endpoint(client_id: str, request: Request) -> dict[str, Any]:
    ref: ReferenceData = request.app.state.ref
    pool: DbPool = request.app.state.pool

    with pool.connection() as conn:
        return get_client_balances(conn, client_id, ref.currencies)
