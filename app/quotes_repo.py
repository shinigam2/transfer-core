from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from .money import Currency, Money, round_money
from .pricing import QuoteCalculation
from .reference import Tariff


def format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class StoredQuote:
    quote_id: str
    direction_id: str
    source_country: str
    target_country: str
    payout_method: str
    source_amount: Money
    fee: Money
    amount_to_convert: Money
    rate_str: str
    target_amount: Money
    breakdown: list[dict[str, str]]
    created_at: datetime
    expires_at: datetime

    @property
    def rate(self) -> Decimal:
        return Decimal(self.rate_str)

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "quote_id": self.quote_id,
            "direction_id": self.direction_id,
            "source_amount": self.source_amount.to_dict(),
            "fee": self.fee.to_dict(),
            "amount_to_convert": self.amount_to_convert.to_dict(),
            "rate": self.rate_str,
            "target_amount": self.target_amount.to_dict(),
            "breakdown": self.breakdown,
            "expires_at": format_iso_utc(self.expires_at),
        }


def insert_quote(
    conn: Connection[DictRow],
    tariff: Tariff,
    calc: QuoteCalculation,
    now: datetime,
) -> StoredQuote:
    created_at = now.astimezone(UTC).replace(microsecond=0)
    expires_at = created_at + timedelta(seconds=tariff.quote_ttl_sec)
    quote_id = f"qt_{uuid4().hex[:16]}"

    conn.execute(
        """
        INSERT INTO quotes (
            quote_id,
            direction_id,
            source_country,
            source_currency,
            target_country,
            target_currency,
            payout_method,
            source_amount,
            fee_amount,
            fee_currency,
            amount_to_convert,
            convert_currency,
            rate,
            target_amount,
            breakdown,
            created_at,
            expires_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s
        )
        """,
        (
            quote_id,
            tariff.direction_id,
            tariff.source_country,
            tariff.source_currency.code,
            tariff.target_country,
            tariff.target_currency.code,
            tariff.payout_method,
            calc.source_amount.amount,
            calc.fee.amount,
            calc.fee.currency.code,
            calc.amount_to_convert.amount,
            calc.amount_to_convert.currency.code,
            calc.rate_str,
            calc.target_amount.amount,
            Jsonb(calc.breakdown),
            created_at,
            expires_at,
        ),
    )

    return StoredQuote(
        quote_id=quote_id,
        direction_id=tariff.direction_id,
        source_country=tariff.source_country,
        target_country=tariff.target_country,
        payout_method=tariff.payout_method,
        source_amount=calc.source_amount,
        fee=calc.fee,
        amount_to_convert=calc.amount_to_convert,
        rate_str=calc.rate_str,
        target_amount=calc.target_amount,
        breakdown=calc.breakdown,
        created_at=created_at,
        expires_at=expires_at,
    )


def get_quote(
    conn: Connection[DictRow],
    quote_id: str,
    currencies: dict[str, Currency],
) -> StoredQuote | None:
    row = conn.execute(
        """
        SELECT
            quote_id,
            direction_id,
            source_country,
            source_currency,
            target_country,
            target_currency,
            payout_method,
            source_amount,
            fee_amount,
            fee_currency,
            amount_to_convert,
            convert_currency,
            rate,
            target_amount,
            breakdown,
            created_at,
            expires_at
        FROM quotes
        WHERE quote_id = %s
        """,
        (quote_id,),
    ).fetchone()

    if row is None:
        return None

    src_curr = currencies[row["source_currency"]]
    dst_curr = currencies[row["target_currency"]]
    fee_curr = currencies[row["fee_currency"]]
    conv_curr = currencies[row["convert_currency"]]

    return StoredQuote(
        quote_id=row["quote_id"],
        direction_id=row["direction_id"],
        source_country=row["source_country"],
        target_country=row["target_country"],
        payout_method=row["payout_method"],
        source_amount=round_money(row["source_amount"], src_curr),
        fee=round_money(row["fee_amount"], fee_curr),
        amount_to_convert=round_money(row["amount_to_convert"], conv_curr),
        rate_str=row["rate"],
        target_amount=round_money(row["target_amount"], dst_curr),
        breakdown=row["breakdown"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )
