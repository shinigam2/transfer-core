from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow

from .db import DbPool
from .errors import ApiError
from .money import Currency, Money, round_money
from .rates import RateSnapshot
from .reference import ClientSeed


@dataclass(frozen=True)
class CurrencyBalance:
    currency: Currency
    initial: Money
    available: Money
    reserved: Money
    debited: Money

    def to_api_dict(self) -> dict[str, str]:
        return {
            "currency": self.currency.code,
            "available": self.available.format(),
            "reserved": self.reserved.format(),
            "debited": self.debited.format(),
        }


def seed_clients(pool: DbPool, clients: dict[str, ClientSeed]) -> None:
    """Загружает стартовые балансы из clients.json при первом старте, не затирая текущие остатки."""
    with pool.connection() as conn, conn.transaction():
        for seed in clients.values():
            conn.execute(
                """
                INSERT INTO clients (client_id, daily_limit_amount, daily_limit_currency)
                VALUES (%s, %s, %s)
                ON CONFLICT (client_id) DO NOTHING
                """,
                (
                    seed.client_id,
                    seed.daily_limit.amount,
                    seed.daily_limit.currency.code,
                ),
            )
            for code, balance in seed.balances.items():
                zero = Decimal(0).quantize(balance.currency.quantum)
                conn.execute(
                    """
                    INSERT INTO client_balances (
                        client_id, currency, initial_balance, available, reserved, debited
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (client_id, currency) DO NOTHING
                    """,
                    (
                        seed.client_id,
                        code,
                        balance.amount,
                        balance.amount,
                        zero,
                        zero,
                    ),
                )


def get_client_balances(
    conn: Connection[DictRow],
    client_id: str,
    currencies: dict[str, Currency],
) -> dict[str, Any]:
    client_row = conn.execute(
        "SELECT client_id FROM clients WHERE client_id = %s",
        (client_id,),
    ).fetchone()
    if client_row is None:
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Клиент не найден",
            status_code=404,
            details={"client_id": client_id},
        )

    rows = conn.execute(
        """
        SELECT currency, initial_balance, available, reserved, debited
        FROM client_balances
        WHERE client_id = %s
        ORDER BY currency
        """,
        (client_id,),
    ).fetchall()

    balances: dict[str, dict[str, str]] = {}
    for row in rows:
        curr = currencies[row["currency"]]
        item = CurrencyBalance(
            currency=curr,
            initial=round_money(row["initial_balance"], curr),
            available=round_money(row["available"], curr),
            reserved=round_money(row["reserved"], curr),
            debited=round_money(row["debited"], curr),
        )
        balances[curr.code] = item.to_api_dict()

    return {
        "client_id": client_id,
        "balances": balances,
    }


def compute_daily_limit_amount(
    source_amount: Money,
    limit_currency: Currency,
    rate_snapshot: RateSnapshot | None,
) -> Money:
    if source_amount.currency == limit_currency:
        return source_amount

    if rate_snapshot is None:
        raise ApiError(
            code="RATE_UNAVAILABLE",
            message="Нет курса для пересчёта суточного лимита",
            status_code=422,
        )

    converted = rate_snapshot.convert_internal(
        source_amount.amount,
        source_amount.currency.code,
        limit_currency.code,
    )
    if converted is None:
        raise ApiError(
            code="RATE_UNAVAILABLE",
            message=f"Нет курса для пересчёта лимита {source_amount.currency.code}/{limit_currency.code}",
            status_code=422,
        )

    # Для проверки потолка лимита округляем вверх, чтобы не пропустить превышение на доли центов.
    return round_money(converted, limit_currency, rounding=ROUND_CEILING)


def utc_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start, day_start + timedelta(days=1)


def lock_client_for_transfer(
    conn: Connection[DictRow],
    client_id: str,
    currencies: dict[str, Currency],
) -> Money:
    """Блокирует строку клиента (FOR UPDATE) и возвращает его суточный лимит."""
    row = conn.execute(
        """
        SELECT daily_limit_amount, daily_limit_currency
        FROM clients
        WHERE client_id = %s
        FOR UPDATE
        """,
        (client_id,),
    ).fetchone()
    if row is None:
        raise ApiError(
            code="VALIDATION_ERROR",
            message="Клиент не найден",
            status_code=422,
            details={"client_id": client_id},
        )
    limit_curr = currencies[row["daily_limit_currency"]]
    return round_money(row["daily_limit_amount"], limit_curr)


def reserve_client_funds(
    conn: Connection[DictRow],
    client_id: str,
    source_amount: Money,
    daily_limit: Money,
    daily_limit_amount: Money,
    now: datetime,
) -> None:
    """Проверяет баланс и суточный лимит внутри открытой транзакции и ставит сумму в резерв."""
    bal_row = conn.execute(
        """
        SELECT available
        FROM client_balances
        WHERE client_id = %s AND currency = %s
        FOR UPDATE
        """,
        (client_id, source_amount.currency.code),
    ).fetchone()

    available = (
        round_money(bal_row["available"], source_amount.currency)
        if bal_row is not None
        else Money(Decimal(0).quantize(source_amount.currency.quantum), source_amount.currency)
    )
    if available < source_amount:
        raise ApiError(
            code="INSUFFICIENT_FUNDS",
            message="Недостаточно доступного баланса",
            status_code=422,
            details={
                "available": available.format(),
                "required": source_amount.format(),
                "currency": source_amount.currency.code,
            },
        )

    day_start, day_end = utc_day_bounds(now)
    usage_row = conn.execute(
        """
        SELECT COALESCE(SUM(daily_limit_amount), 0) AS used
        FROM transfers
        WHERE client_id = %s
          AND created_at >= %s
          AND created_at < %s
          AND status != 'FAILED'
        """,
        (client_id, day_start, day_end),
    ).fetchone()
    used_today = round_money(
        usage_row["used"] if usage_row is not None else Decimal(0),
        daily_limit.currency,
        rounding=ROUND_CEILING,
    )

    if used_today + daily_limit_amount > daily_limit:
        raise ApiError(
            code="DAILY_LIMIT_EXCEEDED",
            message="Превышен суточный лимит клиента",
            status_code=422,
            details={
                "daily_limit": daily_limit.format(),
                "used_today": used_today.format(),
                "requested": daily_limit_amount.format(),
                "currency": daily_limit.currency.code,
            },
        )

    conn.execute(
        """
        UPDATE client_balances
        SET available = available - %s,
            reserved = reserved + %s
        WHERE client_id = %s AND currency = %s
        """,
        (source_amount.amount, source_amount.amount, client_id, source_amount.currency.code),
    )


def commit_reserved_funds(
    conn: Connection[DictRow],
    client_id: str,
    amount: Money,
) -> None:
    updated = conn.execute(
        """
        UPDATE client_balances
        SET reserved = reserved - %s,
            debited = debited + %s
        WHERE client_id = %s AND currency = %s AND reserved >= %s
        """,
        (amount.amount, amount.amount, client_id, amount.currency.code, amount.amount),
    ).rowcount
    if updated != 1:
        raise RuntimeError(f"Не удалось списать резерв {amount.format()} {amount.currency.code} у {client_id}")


def release_reserved_funds(
    conn: Connection[DictRow],
    client_id: str,
    amount: Money,
) -> None:
    updated = conn.execute(
        """
        UPDATE client_balances
        SET reserved = reserved - %s,
            available = available + %s
        WHERE client_id = %s AND currency = %s AND reserved >= %s
        """,
        (amount.amount, amount.amount, client_id, amount.currency.code, amount.amount),
    ).rowcount
    if updated != 1:
        raise RuntimeError(f"Не удалось вернуть резерв {amount.format()} {amount.currency.code} у {client_id}")
