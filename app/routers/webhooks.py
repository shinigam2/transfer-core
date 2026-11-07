import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, Request

from ..config import Settings
from ..errors import ApiError
from ..reference import ReferenceData, parse_iso_utc
from ..transfers_service import TransferService

router = APIRouter(tags=["webhooks"])


def _verify_signature(secret: str, raw_body: bytes, signature_header: str | None) -> None:
    if not secret:
        return
    if not signature_header:
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Отсутствует заголовок X-Signature",
            status_code=401,
        )
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Некорректная подпись X-Signature",
            status_code=401,
        )


@router.post("/webhooks/{provider_id}")
async def provider_webhook_endpoint(provider_id: str, request: Request) -> dict[str, str]:
    settings: Settings = request.app.state.settings
    ref: ReferenceData = request.app.state.ref
    service: TransferService = request.app.state.transfer_service

    if provider_id not in ref.providers:
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Неизвестный провайдер",
            status_code=404,
            details={"provider_id": provider_id},
        )

    raw_body = await request.body()
    _verify_signature(settings.webhook_secret, raw_body, request.headers.get("X-Signature"))

    try:
        payload: Any = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Некорректный JSON вебхука",
            status_code=400,
        ) from exc

    if not isinstance(payload, dict):
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Тело вебхука должно быть JSON-объектом",
            status_code=400,
        )

    for key in ("event_id", "payout_id", "reference", "status", "occurred_at"):
        val = payload.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ApiError(
                code="VALIDATION_ERROR",
                message=f"Поле {key} обязательно в вебхуке",
                status_code=400,
            )

    try:
        occurred_at = parse_iso_utc(payload["occurred_at"])
    except ValueError as exc:
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Некорректный формат occurred_at",
            status_code=400,
        ) from exc

    return service.handle_webhook(
        provider_id=provider_id,
        event_id=payload["event_id"],
        payout_id=payload["payout_id"],
        reference=payload["reference"],
        status=payload["status"],
        occurred_at=occurred_at,
    )
