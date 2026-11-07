from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from .errors import ApiError

DEFAULT_ROUNDING = ROUND_DOWN


@dataclass(frozen=True)
class Currency:
    code: str
    minor_units: int

    @property
    def quantum(self) -> Decimal:
        """Шаг валюты: 0.01 для 2 знаков, 1 для 0 знаков, 0.001 для 3 знаков."""
        return Decimal(1).scaleb(-self.minor_units)


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError("Money.amount должен быть Decimal")
        if not self.amount.is_finite():
            raise ValueError("Money.amount должен быть конечным числом")

        # Не пускаем неокруглённые хвосты в обход round_money().
        normalized = self.amount.quantize(self.currency.quantum, rounding=ROUND_DOWN)
        if normalized != self.amount:
            raise ValueError(
                f"Лишние знаки для {self.currency.code} (minor_units={self.currency.minor_units}): {self.amount}"
            )
        object.__setattr__(self, "amount", normalized)

    def _require_same_currency(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise ValueError(f"Разные валюты: {self.currency.code} и {other.currency.code}")

    def __add__(self, other: "Money") -> "Money":
        self._require_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._require_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def __lt__(self, other: "Money") -> bool:
        self._require_same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        self._require_same_currency(other)
        return self.amount <= other.amount

    def __gt__(self, other: "Money") -> bool:
        self._require_same_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: "Money") -> bool:
        self._require_same_currency(other)
        return self.amount >= other.amount

    def format(self) -> str:
        return f"{self.amount:.{self.currency.minor_units}f}"

    def to_dict(self) -> dict[str, str]:
        return {"amount": self.format(), "currency": self.currency.code}


def round_money(value: Decimal, currency: Currency, rounding: str = DEFAULT_ROUNDING) -> Money:
    """Единственная точка округления расчётного Decimal до разрядности валюты."""
    return Money(value.quantize(currency.quantum, rounding=rounding), currency)


def parse_money(raw: object, currency: Currency, *, require_positive: bool = False) -> Money:
    """Парсит строковую сумму и проверяет точное число знаков по minor_units."""
    if not isinstance(raw, str):
        raise ApiError(
            code="VALIDATION_ERROR",
            message=f"Сумма должна быть строкой ({currency.code})",
            status_code=400,
        )

    whole, dot, fraction = raw.partition(".")
    if not whole or not (whole.isascii() and whole.isdigit()):
        raise ApiError(
            code="VALIDATION_ERROR",
            message=f"Некорректный формат суммы: {raw!r}",
            status_code=400,
        )
    if len(whole) > 1 and whole.startswith("0"):
        raise ApiError(
            code="VALIDATION_ERROR",
            message=f"Ведущие нули недопустимы: {raw!r}",
            status_code=400,
        )

    if currency.minor_units == 0:
        if dot != "":
            raise ApiError(
                code="VALIDATION_ERROR",
                message=f"У {currency.code} нет дробной части",
                status_code=400,
            )
    else:
        if dot != "." or len(fraction) != currency.minor_units or not (fraction.isascii() and fraction.isdigit()):
            raise ApiError(
                code="VALIDATION_ERROR",
                message=f"Для {currency.code} нужно ровно {currency.minor_units} знаков после точки",
                status_code=400,
            )

    money = Money(Decimal(raw), currency)
    if require_positive and money.amount <= Decimal(0):
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Сумма должна быть больше нуля",
            status_code=400,
        )
    return money
