from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal

from .money import Money, round_money
from .rates import RateSnapshot
from .reference import ProviderConfig, Tariff

WEIGHT_RELIABILITY = Decimal("0.50")
WEIGHT_MARGIN = Decimal("0.35")
WEIGHT_LATENCY = Decimal("0.15")

MARGIN_CAP_PERCENT = Decimal("3.0")
LATENCY_HALF_SCORE_MS = Decimal(2000)
SCORE_QUANTUM = Decimal("0.000001")
MARGIN_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True)
class RankedProvider:
    provider: ProviderConfig
    payout_in_provider_currency: Decimal
    provider_fee: Money
    margin_percent: Decimal
    score: Decimal


@dataclass(frozen=True)
class RejectedProvider:
    provider_id: str
    reason: str
    detail: str


@dataclass(frozen=True)
class RoutingDecision:
    ranked: list[RankedProvider]
    rejected: list[RejectedProvider]


def _score_provider(provider: ProviderConfig, margin_percent: Decimal) -> Decimal:
    reliability_score = provider.success_rate_24h
    clamped_margin = min(max(margin_percent, Decimal(0)), MARGIN_CAP_PERCENT)
    margin_score = clamped_margin / MARGIN_CAP_PERCENT
    latency_score = Decimal(1) / (Decimal(1) + Decimal(provider.p95_latency_ms) / LATENCY_HALF_SCORE_MS)

    raw_score = (
        WEIGHT_RELIABILITY * reliability_score
        + WEIGHT_MARGIN * margin_score
        + WEIGHT_LATENCY * latency_score
    )
    return raw_score.quantize(SCORE_QUANTUM)


def select_providers(
    tariff: Tariff,
    source_amount: Money,
    client_fee: Money,
    target_amount: Money,
    providers: Iterable[ProviderConfig],
    rate_snapshot: RateSnapshot,
    now: datetime,
    *,
    exclude_providers: set[str] | None = None,
    allow_below_min_margin: bool = False,
) -> RoutingDecision:
    """Фильтрует и ранжирует провайдеров для выплаты по котировке."""
    excluded = exclude_providers or set()
    ranked: list[RankedProvider] = []
    rejected: list[RejectedProvider] = []

    client_fee_in_src = rate_snapshot.convert_internal(
        client_fee.amount,
        client_fee.currency.code,
        source_amount.currency.code,
    )

    for provider in sorted(providers, key=lambda p: p.provider_id):
        pid = provider.provider_id
        if pid in excluded:
            rejected.append(RejectedProvider(pid, "EXCLUDED", "Уже пробовали в этом переводе"))
            continue

        if tariff.direction_id not in provider.directions:
            rejected.append(RejectedProvider(pid, "UNSUPPORTED_DIRECTION", tariff.direction_id))
            continue

        if not provider.enabled:
            rejected.append(RejectedProvider(pid, "DISABLED", "Провайдер отключён"))
            continue

        if not provider.is_available(now):
            rejected.append(RejectedProvider(pid, "MAINTENANCE", "Окно технических работ"))
            continue

        payout_in_prov = rate_snapshot.convert_internal(
            target_amount.amount,
            target_amount.currency.code,
            provider.currency.code,
        )
        if payout_in_prov is None:
            rejected.append(
                RejectedProvider(pid, "MISSING_RATE", f"{target_amount.currency.code}->{provider.currency.code}")
            )
            continue

        if payout_in_prov < provider.min_limit.amount or payout_in_prov > provider.max_limit.amount:
            rejected.append(
                RejectedProvider(
                    pid,
                    "OUT_OF_LIMITS",
                    f"{payout_in_prov:.4f} вне [{provider.min_limit.format()}, {provider.max_limit.format()}] {provider.currency.code}",
                )
            )
            continue

        raw_prov_fee = (
            payout_in_prov * provider.fee.percent / Decimal(100)
            + provider.fee.fixed.amount
        )
        prov_fee = round_money(raw_prov_fee, provider.currency, rounding=ROUND_CEILING)

        prov_fee_in_src = rate_snapshot.convert_internal(
            prov_fee.amount,
            provider.currency.code,
            source_amount.currency.code,
        )
        if client_fee_in_src is None or prov_fee_in_src is None:
            rejected.append(
                RejectedProvider(pid, "MISSING_RATE", f"Пересчёт комиссии в {source_amount.currency.code}")
            )
            continue

        margin_percent = (
            (client_fee_in_src - prov_fee_in_src) / source_amount.amount * Decimal(100)
        ).quantize(MARGIN_QUANTUM)

        if not allow_below_min_margin and margin_percent < tariff.min_margin_percent:
            rejected.append(
                RejectedProvider(
                    pid,
                    "LOW_MARGIN",
                    f"маржа {margin_percent}% < min {tariff.min_margin_percent}%",
                )
            )
            continue

        score = _score_provider(provider, margin_percent)
        ranked.append(
            RankedProvider(
                provider=provider,
                payout_in_provider_currency=payout_in_prov,
                provider_fee=prov_fee,
                margin_percent=margin_percent,
                score=score,
            )
        )

    ranked.sort(
        key=lambda item: (
            -item.score,
            -item.provider.success_rate_24h,
            -item.margin_percent,
            item.provider.provider_id,
        )
    )
    return RoutingDecision(ranked=ranked, rejected=rejected)
