from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, StrictStr

from ..db import DbPool
from ..errors import ApiError
from ..pricing import calculate_quote, validate_quote_input
from ..quotes_repo import format_iso_utc, get_quote, insert_quote
from ..rates import RateSource
from ..reference import ReferenceData

router = APIRouter(tags=["quotes"])


class CreateQuoteRequest(BaseModel):
    source_country: StrictStr
    source_currency: StrictStr
    target_country: StrictStr
    target_currency: StrictStr
    payout_method: StrictStr
    amount: StrictStr
    amount_type: StrictStr


@router.post("/quotes", status_code=status.HTTP_201_CREATED)
def create_quote_endpoint(payload: CreateQuoteRequest, request: Request) -> dict[str, Any]:
    ref: ReferenceData = request.app.state.ref
    rate_source: RateSource = request.app.state.rate_source
    pool: DbPool = request.app.state.pool
    clock: Callable[[], datetime] = request.app.state.clock

    tariff = ref.find_tariff(
        source_country=payload.source_country,
        source_currency=payload.source_currency,
        target_country=payload.target_country,
        target_currency=payload.target_currency,
        payout_method=payload.payout_method,
    )

    # Проверяем формат суммы и amount_type до похода за курсом.
    validate_quote_input(tariff, payload.amount, payload.amount_type)

    rate_quote = rate_source.snapshot().get_quote_rate(
        tariff.source_currency.code,
        tariff.target_currency.code,
    )
    calc = calculate_quote(tariff, rate_quote, payload.amount, payload.amount_type)

    now = clock()
    with pool.connection() as conn, conn.transaction():
        stored = insert_quote(conn, tariff, calc, now)

    return stored.to_api_dict()


@router.get("/quotes/{quote_id}")
def get_quote_endpoint(quote_id: str, request: Request) -> dict[str, Any]:
    ref: ReferenceData = request.app.state.ref
    pool: DbPool = request.app.state.pool
    clock: Callable[[], datetime] = request.app.state.clock

    with pool.connection() as conn:
        stored = get_quote(conn, quote_id, ref.currencies)

    if stored is None:
        raise ApiError(
            code="QUOTE_NOT_FOUND",
            message="Котировка не найдена",
            status_code=404,
            details={"quote_id": quote_id},
        )

    if stored.is_expired(clock()):
        raise ApiError(
            code="QUOTE_EXPIRED",
            message="Срок действия котировки истёк",
            status_code=422,
            details={"quote_id": quote_id, "expires_at": format_iso_utc(stored.expires_at)},
        )

    return stored.to_api_dict()
