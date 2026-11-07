from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

CreateKind = Literal["ACCEPTED", "DECLINED", "RETRYABLE_ERROR", "UNCERTAIN"]
LookupKind = Literal["FOUND", "NOT_FOUND", "ERROR"]


@dataclass(frozen=True)
class PayoutCreateResult:
    kind: CreateKind
    payout_id: str | None = None
    provider_status: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class PayoutLookupResult:
    kind: LookupKind
    payout_id: str | None = None
    provider_status: str | None = None
    failure_code: str | None = None


class ProviderClient(Protocol):
    def create_payout(
        self,
        provider_id: str,
        idempotency_key: str,
        reference: str,
        amount: str,
        currency: str,
        recipient: dict[str, Any],
        callback_url: str,
        scenario: str | None = None,
    ) -> PayoutCreateResult: ...

    def find_payout_by_reference(
        self,
        provider_id: str,
        reference: str,
    ) -> PayoutLookupResult: ...


class HttpProviderClient:
    """HTTP-клиент к эмулятору внешних провайдеров (mock-provider)."""

    def __init__(self, base_url: str, timeout_sec: float = 32.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_sec

    def create_payout(
        self,
        provider_id: str,
        idempotency_key: str,
        reference: str,
        amount: str,
        currency: str,
        recipient: dict[str, Any],
        callback_url: str,
        scenario: str | None = None,
    ) -> PayoutCreateResult:
        headers = {"Idempotency-Key": idempotency_key}
        if scenario:
            headers["X-Scenario"] = scenario

        payload = {
            "reference": reference,
            "amount": amount,
            "currency": currency,
            "recipient": recipient,
            "callback_url": callback_url,
        }

        try:
            resp = httpx.post(
                f"{self._base_url}/{provider_id}/payouts",
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # Обрыв соединения или таймаут: выплата уже могла быть создана у провайдера.
            return PayoutCreateResult(
                kind="UNCERTAIN",
                error_code="TRANSPORT_ERROR",
                error_message=str(exc),
            )

        body = _safe_json(resp)

        if resp.status_code in (200, 201):
            return PayoutCreateResult(
                kind="ACCEPTED",
                payout_id=str(body.get("payout_id", "")),
                provider_status=str(body.get("status", "PROCESSING")),
            )

        # Бизнес-отказ или недоступность провайдера до создания выплаты (400/404/422/503).
        if resp.status_code in (400, 404, 409, 422, 503):
            return PayoutCreateResult(
                kind="DECLINED",
                error_code=str(body.get("code") or f"HTTP_{resp.status_code}"),
                error_message=str(body.get("message") or "Provider rejected payout"),
            )

        # 502 / 504 Gateway Timeout: запрос мог дойти и создать выплату.
        if resp.status_code in (502, 504):
            return PayoutCreateResult(
                kind="UNCERTAIN",
                error_code=str(body.get("code") or f"HTTP_{resp.status_code}"),
                error_message=str(body.get("message") or "Upstream gateway timeout"),
            )

        # 500 Internal Error: временная ошибка провайдера (повторяем тем же ключом или сверяем).
        return PayoutCreateResult(
            kind="RETRYABLE_ERROR",
            error_code=str(body.get("code") or f"HTTP_{resp.status_code}"),
            error_message=str(body.get("message") or "Provider internal error"),
        )

    def find_payout_by_reference(
        self,
        provider_id: str,
        reference: str,
    ) -> PayoutLookupResult:
        try:
            resp = httpx.get(
                f"{self._base_url}/{provider_id}/payouts",
                params={"reference": reference},
                timeout=min(self._timeout, 5.0),
            )
        except httpx.HTTPError:
            return PayoutLookupResult(kind="ERROR")

        if resp.status_code == 404:
            return PayoutLookupResult(kind="NOT_FOUND")

        if resp.status_code != 200:
            return PayoutLookupResult(kind="ERROR")

        body = _safe_json(resp)
        return PayoutLookupResult(
            kind="FOUND",
            payout_id=str(body.get("payout_id", "")),
            provider_status=str(body.get("status", "PROCESSING")),
            failure_code=body.get("failure_code"),
        )


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
