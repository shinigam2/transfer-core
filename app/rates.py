import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Protocol

import httpx

from .errors import ApiError
from .reference import parse_iso_utc

INVERSE_RATE_QUANTUM = Decimal("0.00000001")
PIVOT_CURRENCIES = ("USD", "EUR", "RUB")


@dataclass(frozen=True)
class RateQuote:
    rate: Decimal
    rate_str: str


@dataclass(frozen=True)
class _RawRate:
    base: str
    quote: str
    rate: Decimal
    rate_str: str
    updated_at: datetime


class RateSnapshot:
    """Срез курсов на конкретный момент времени с фильтрацией устаревших пар."""

    def __init__(
        self,
        entries: list[dict[str, Any]],
        now: datetime,
        max_age: timedelta,
    ) -> None:
        self._all: dict[tuple[str, str], _RawRate] = {}
        self._fresh: dict[tuple[str, str], _RawRate] = {}

        for item in entries:
            entry = _RawRate(
                base=item["base"],
                quote=item["quote"],
                rate=Decimal(item["rate"]),
                rate_str=str(item["rate"]),
                updated_at=parse_iso_utc(item["updated_at"]),
            )
            pair = (entry.base, entry.quote)
            self._all[pair] = entry
            if now - entry.updated_at <= max_age:
                self._fresh[pair] = entry

    def get_quote_rate(self, base: str, quote: str) -> RateQuote:
        """Возвращает курс для клиентской котировки (прямой или обратный, если прямой пары нет)."""
        if base == quote:
            return RateQuote(rate=Decimal(1), rate_str="1")

        direct = self._all.get((base, quote))
        if direct is not None:
            # Если прямая пара есть в фиде, но протухла — не маскируем проблему обратным/кросс-курсом.
            if (base, quote) not in self._fresh:
                raise ApiError(
                    code="RATE_UNAVAILABLE",
                    message=f"Курс {base}/{quote} устарел",
                    status_code=422,
                    details={"base": base, "quote": quote},
                )
            return RateQuote(rate=direct.rate, rate_str=direct.rate_str)

        inverse_fresh = self._fresh.get((quote, base))
        if inverse_fresh is not None and inverse_fresh.rate > 0:
            inverted = (Decimal(1) / inverse_fresh.rate).quantize(
                INVERSE_RATE_QUANTUM, rounding=ROUND_DOWN
            )
            return RateQuote(rate=inverted, rate_str=f"{inverted:f}")

        raise ApiError(
            code="RATE_UNAVAILABLE",
            message=f"Нет доступного курса для пары {base}/{quote}",
            status_code=422,
            details={"base": base, "quote": quote},
        )

    def _single_hop(self, src: str, dst: str) -> Decimal | None:
        direct = self._fresh.get((src, dst))
        if direct is not None:
            return direct.rate
        inverse = self._fresh.get((dst, src))
        if inverse is not None and inverse.rate > 0:
            return Decimal(1) / inverse.rate
        return None

    def convert_internal(self, amount: Decimal, source: str, target: str) -> Decimal | None:
        """Внутренний пересчёт для лимитов и маржи провайдеров (разрешён 1 промежуточный узел)."""
        if source == target:
            return amount

        hop = self._single_hop(source, target)
        if hop is not None:
            return amount * hop

        for pivot in PIVOT_CURRENCIES:
            if pivot in (source, target):
                continue
            leg1 = self._single_hop(source, pivot)
            leg2 = self._single_hop(pivot, target)
            if leg1 is not None and leg2 is not None:
                return amount * leg1 * leg2

        return None


class RateSource(Protocol):
    def snapshot(self) -> RateSnapshot: ...


class HttpRateSource:
    """Получает живые курсы у mock-provider по HTTP."""

    def __init__(
        self,
        mock_provider_url: str,
        max_age_sec: int = 3600,
        timeout_sec: float = 3.0,
    ) -> None:
        self._url = f"{mock_provider_url.rstrip('/')}/rates"
        self._max_age = timedelta(seconds=max_age_sec)
        self._timeout = timeout_sec

    def snapshot(self) -> RateSnapshot:
        try:
            response = httpx.get(self._url, timeout=self._timeout)
            response.raise_for_status()
            entries = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ApiError(
                code="RATE_UNAVAILABLE",
                message="Источник курсов временно недоступен",
                status_code=422,
            ) from exc

        return RateSnapshot(
            entries=entries,
            now=datetime.now(UTC),
            max_age=self._max_age,
        )


class FileRateSource:
    """Читает фиксированный снимок rates.json (для детерминированных тестов без сети)."""

    def __init__(self, rates_path: Path, max_age_sec: int = 3600) -> None:
        self._entries: list[dict[str, Any]] = json.loads(rates_path.read_text(encoding="utf-8"))
        self._max_age = timedelta(seconds=max_age_sec)
        # Привязываем "сейчас" к самой свежей записи в файле, чтобы тесты не зависели от календаря.
        self._reference_now = max(parse_iso_utc(item["updated_at"]) for item in self._entries)

    def snapshot(self) -> RateSnapshot:
        return RateSnapshot(
            entries=self._entries,
            now=self._reference_now,
            max_age=self._max_age,
        )
