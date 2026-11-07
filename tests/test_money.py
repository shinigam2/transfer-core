from decimal import Decimal

import pytest

from app.errors import ApiError
from app.money import Currency, Money, parse_money, round_money

RUB = Currency("RUB", 2)
JPY = Currency("JPY", 0)
KWD = Currency("KWD", 3)


def test_parse_and_format_different_minor_units() -> None:
    assert parse_money("5000.00", RUB).format() == "5000.00"
    assert parse_money("18658", JPY).format() == "18658"
    assert parse_money("50.000", KWD).format() == "50.000"
    assert parse_money("0.05", RUB).to_dict() == {"amount": "0.05", "currency": "RUB"}


@pytest.mark.parametrize(
    ("raw", "currency"),
    [
        (5000, RUB),          # число вместо строки
        (5000.0, RUB),        # float вместо строки
        ("5000", RUB),        # нет знаков после точки для RUB
        ("5000.0", RUB),      # 1 знак вместо 2
        ("5000.000", RUB),    # 3 знака вместо 2
        ("18658.00", JPY),    # дробные знаки у бездробной иены
        ("00.50", RUB),       # лишний ведущий ноль
        ("-100.00", RUB),     # отрицательная сумма
        (" 100.00", RUB),     # пробел
    ],
)
def test_parse_money_rejects_invalid_formats(raw: object, currency: Currency) -> None:
    with pytest.raises(ApiError) as exc_info:
        parse_money(raw, currency)
    assert exc_info.value.code == "VALIDATION_ERROR"


def test_parse_money_require_positive() -> None:
    assert parse_money("0.00", RUB, require_positive=False).amount == Decimal("0.00")
    with pytest.raises(ApiError) as exc_info:
        parse_money("0.00", RUB, require_positive=True)
    assert exc_info.value.code == "VALIDATION_ERROR"


def test_round_money_always_rounds_down() -> None:
    # Даже .999 доли копейки или иены округляются строго вниз (ROUND_DOWN)
    assert round_money(Decimal("15.009"), RUB).format() == "15.00"
    assert round_money(Decimal("18658.99"), JPY).format() == "18658"
    assert round_money(Decimal("1.2349"), KWD).format() == "1.234"


def test_money_forbids_unrounded_decimal_and_currency_mixing() -> None:
    with pytest.raises(ValueError):
        Money(Decimal("15.005"), RUB)

    with pytest.raises(ValueError):
        _ = parse_money("100.00", RUB) + parse_money("100", JPY)
