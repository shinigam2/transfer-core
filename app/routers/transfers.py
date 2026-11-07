from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictStr

from ..errors import ApiError
from ..transfers_repo import TERMINAL_TRANSFER_STATES
from ..transfers_service import TransferRuntime, TransferService

router = APIRouter(tags=["transfers"])


class CreateTransferRequest(BaseModel):
    quote_id: StrictStr
    client_id: StrictStr
    recipient: dict[str, Any]


def _validate_recipient(recipient: dict[str, Any]) -> None:
    if not recipient:
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Поле recipient не может быть пустым",
            status_code=400,
        )
    for required_key in ("name", "account"):
        val = recipient.get(required_key)
        if not isinstance(val, str) or not val.strip():
            raise ApiError(
                code="VALIDATION_ERROR",
                message=f"Поле recipient.{required_key} обязательно и должно быть непустой строкой",
                status_code=400,
            )


@router.post("/transfers")
def create_transfer_endpoint(payload: CreateTransferRequest, request: Request) -> JSONResponse:
    idem_key = request.headers.get("Idempotency-Key", "").strip()
    if not idem_key:
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Заголовок Idempotency-Key обязателен",
            status_code=400,
        )

    if not payload.quote_id.strip() or not payload.client_id.strip():
        raise ApiError(
            code="VALIDATION_ERROR",
            message="quote_id и client_id не могут быть пустыми",
            status_code=400,
        )

    _validate_recipient(payload.recipient)

    scenario_header = request.headers.get("X-Scenario")
    if scenario_header is not None:
        scenario_header = scenario_header.strip() or None

    service: TransferService = request.app.state.transfer_service
    runtime: TransferRuntime = request.app.state.transfer_runtime

    body, created = service.create_or_get_transfer(
        idempotency_key=idem_key,
        quote_id=payload.quote_id,
        client_id=payload.client_id,
        recipient=payload.recipient,
        scenario_header=scenario_header,
    )

    if created:
        runtime.submit(body["transfer_id"])

    http_status = (
        status.HTTP_200_OK
        if body["status"] in TERMINAL_TRANSFER_STATES
        else status.HTTP_202_ACCEPTED
    )
    return JSONResponse(status_code=http_status, content=body)


@router.get("/transfers/{transfer_id}")
def get_transfer_endpoint(transfer_id: str, request: Request) -> dict[str, Any]:
    service: TransferService = request.app.state.transfer_service
    return service.get_transfer_details(transfer_id)
