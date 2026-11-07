from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from .errors import ApiError
from .money import FEE_ROUNDING, PAYOUT_ROUNDING, Money, parse_money, round_money
from .rates import RateQuote
from .reference import Tariff

AmountType = Literal["SOURCE", "TARGET"]


@dataclass(frozen=True)
class QuoteCalculation:
    direction_id: str
    source_amount: Money
    fee: Money
    amount_to_convert: Money
    rate: Decimal
    rate_str: str
    target_amount: Money
    breakdown: list[dict[str, str]]


def _calc_from_source_unchecked(
    tariff: Tariff,
    rate_quote: RateQuote,
    source_amount: Money,
) -> QuoteCalculation:
    src_curr = tariff.source_currency
    dst_curr = tariff.target_currency
    rate = rate_quote.rate

    if tariff.fee.charged_in == "SOURCE":
        raw_fee = (
            source_amount.amount * tariff.fee.percent / Decimal(100)
            + tariff.fee.fixed.amount
        )
        fee = max(round_money(raw_fee, src_curr, rounding=FEE_ROUNDING), tariff.fee.min_fee)
        if fee >= source_amount:
            amount_to_convert = Money(Decimal(0), src_curr)
            target_amount = Money(Decimal(0), dst_curr)
        else:
            amount_to_convert = source_amount - fee
            target_amount = round_money(amount_to_convert.amount * rate, dst_curr, rounding=PAYOUT_ROUNDING)

        breakdown = [
            {
                "step": "fee",
                "charged_in": "SOURCE",
                "fee": f"{fee.format()} {src_curr.code}",
            },
            {
                "step": "conversion",
                "amount_to_convert": f"{amount_to_convert.format()} {src_curr.code}",
                "rate": rate_quote.rate_str,
                "target_amount": f"{target_amount.format()} {dst_curr.code}",
            },
        ]
    else:
        amount_to_convert = source_amount
        gross_target = round_money(amount_to_convert.amount * rate, dst_curr, rounding=PAYOUT_ROUNDING)
        raw_fee = (
            gross_target.amount * tariff.fee.percent / Decimal(100)
            + tariff.fee.fixed.amount
        )
        fee = max(round_money(raw_fee, dst_curr, rounding=FEE_ROUNDING), tariff.fee.min_fee)
        if fee >= gross_target:
            target_amount = Money(Decimal(0), dst_curr)
        else:
            target_amount = gross_target - fee

        breakdown = [
            {
                "step": "conversion",
                "amount_to_convert": f"{amount_to_convert.format()} {src_curr.code}",
                "rate": rate_quote.rate_str,
                "gross_target": f"{gross_target.format()} {dst_curr.code}",
            },
            {
                "step": "fee",
                "charged_in": "TARGET",
                "fee": f"{fee.format()} {dst_curr.code}",
                "target_amount": f"{target_amount.format()} {dst_curr.code}",
            },
        ]

    return QuoteCalculation(
        direction_id=tariff.direction_id,
        source_amount=source_amount,
        fee=fee,
        amount_to_convert=amount_to_convert,
        rate=rate,
        rate_str=rate_quote.rate_str,
        target_amount=target_amount,
        breakdown=breakdown,
    )


def _find_source_for_target(
    tariff: Tariff,
    rate_quote: RateQuote,
    desired_target: Money,
) -> Money:
    """Находит минимальный source_amount, дающий при прямом расчёте target_amount >= desired_target."""
    src_curr = tariff.source_currency
    step = src_curr.quantum
    rate = rate_quote.rate
    pct_fraction = tariff.fee.percent / Decimal(100)

    if pct_fraction >= Decimal(1):
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Некорректный тариф: комиссия >= 100%",
            status_code=422,
        )

    # 1. Аналитическая верхняя оценка source_amount
    if tariff.fee.charged_in == "SOURCE":
        need_convert = (desired_target.amount / rate).quantize(step, rounding=ROUND_CEILING)
        est_min_fee = need_convert + tariff.fee.min_fee.amount
        est_pct_fee = ((need_convert + tariff.fee.fixed.amount) / (Decimal(1) - pct_fraction)).quantize(
            step, rounding=ROUND_CEILING
        )
        upper_amount = max(est_min_fee, est_pct_fee)
    else:
        dst_step = tariff.target_currency.quantum
        gross_min_fee = desired_target.amount + tariff.fee.min_fee.amount
        gross_pct_fee = (
            (desired_target.amount + tariff.fee.fixed.amount) / (Decimal(1) - pct_fraction)
        ).quantize(dst_step, rounding=ROUND_CEILING)
        need_gross = max(gross_min_fee, gross_pct_fee)
        upper_amount = (need_gross / rate).quantize(step, rounding=ROUND_CEILING)

    # 2. Бинарный поиск точного минимума по шагам валюты (убирает погрешность +/- несколько шагов от ROUND_DOWN)
    hi_units = max(1, int(upper_amount / step) + 2)
    while _calc_from_source_unchecked(tariff, rate_quote, Money(hi_units * step, src_curr)).target_amount < desired_target:
        hi_units *= 2

    lo_units = 1
    while lo_units < hi_units:
        mid_units = (lo_units + hi_units) // 2
        candidate = Money(mid_units * step, src_curr)
        if _calc_from_source_unchecked(tariff, rate_quote, candidate).target_amount >= desired_target:
            hi_units = mid_units
        else:
            lo_units = mid_units + 1

    return Money(lo_units * step, src_curr)


def _check_limits(tariff: Tariff, source_amount: Money) -> None:
    min_amt = tariff.limits.min_amount
    max_amt = tariff.limits.max_amount
    if source_amount < min_amt:
        raise ApiError(
            code="AMOUNT_BELOW_MIN",
            message=f"Минимальная сумма — {min_amt.format()} {min_amt.currency.code}",
            status_code=422,
            details={"min": min_amt.format(), "currency": min_amt.currency.code},
        )
    if source_amount > max_amt:
        raise ApiError(
            code="AMOUNT_ABOVE_MAX",
            message=f"Максимальная сумма — {max_amt.format()} {max_amt.currency.code}",
            status_code=422,
            details={"max": max_amt.format(), "currency": max_amt.currency.code},
        )


def validate_quote_input(tariff: Tariff, raw_amount: object, amount_type: str) -> None:
    if amount_type == "SOURCE":
        parse_money(raw_amount, tariff.source_currency, require_positive=True)
    elif amount_type == "TARGET":
        parse_money(raw_amount, tariff.target_currency, require_positive=True)
    else:
        raise ApiError(
            code="VALIDATION_ERROR",
            message="amount_type должен быть SOURCE или TARGET",
            status_code=400,
        )


def calculate_quote(
    tariff: Tariff,
    rate_quote: RateQuote,
    raw_amount: object,
    amount_type: str,
) -> QuoteCalculation:
    if amount_type == "SOURCE":
        source_amount = parse_money(raw_amount, tariff.source_currency, require_positive=True)
    elif amount_type == "TARGET":
        desired_target = parse_money(raw_amount, tariff.target_currency, require_positive=True)
        source_amount = _find_source_for_target(tariff, rate_quote, desired_target)
    else:
        raise ApiError(
            code="VALIDATION_ERROR",
            message="amount_type должен быть SOURCE или TARGET",
            status_code=400,
        )

    _check_limits(tariff, source_amount)
    result = _calc_from_source_unchecked(tariff, rate_quote, source_amount)

    if result.target_amount.amount <= Decimal(0):
        raise ApiError(
            code="AMOUNT_BELOW_MIN",
            message="Сумма после вычета комиссии недостаточна для выплаты",
            status_code=422,
            details={"min": tariff.limits.min_amount.format(), "currency": tariff.limits.min_amount.currency.code},
        )

    return result
