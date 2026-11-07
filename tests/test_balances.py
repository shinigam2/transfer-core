from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.balances import (
    commit_reserved_funds,
    compute_daily_limit_amount,
    lock_client_for_transfer,
    release_reserved_funds,
    reserve_client_funds,
    seed_clients,
)
from app.config import Settings
from app.db import DbPool
from app.errors import ApiError
from app.main import create_app
from app.money import parse_money
from app.pricing import calculate_quote
from app.quotes_repo import insert_quote
from app.rates import FileRateSource
from app.reference import ReferenceData

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"


@pytest.fixture
def balance_env(test_db_url: str):
    settings = Settings(
        database_url=test_db_url,
        mock_provider_url="http://localhost:9000",
        webhook_secret="test-secret",
        data_dir=DATA_DIR,
    )
    rate_source = FileRateSource(DATA_DIR / "rates.json", max_age_sec=3600)
    app = create_app(settings=settings, rate_source=rate_source)
    with TestClient(app) as test_client:
        pool: DbPool = app.state.pool
        with pool.connection() as conn, conn.transaction():
            conn.execute("TRUNCATE webhook_events, provider_attempts, transfer_transitions, transfers RESTART IDENTITY")
            conn.execute(
                """
                UPDATE client_balances
                SET available = initial_balance,
                    reserved = initial_balance - initial_balance,
                    debited = initial_balance - initial_balance
                """
            )
        yield test_client, pool, app.state.ref, rate_source.snapshot()


def _insert_dummy_transfer(
    conn,
    ref: ReferenceData,
    rate_snap,
    client_id: str,
    amount_str: str,
    now: datetime,
    status: str = "RESERVED",
) -> None:
    tariff = ref.tariffs["RU-TH-BANK"]
    rq = rate_snap.get_quote_rate("RUB", "THB")
    calc = calculate_quote(tariff, rq, amount_str, "SOURCE")
    quote = insert_quote(conn, tariff, calc, now)

    daily_limit = lock_client_for_transfer(conn, client_id, ref.currencies)
    limit_amt = compute_daily_limit_amount(calc.source_amount, daily_limit.currency, rate_snap)
    reserve_client_funds(conn, client_id, calc.source_amount, daily_limit, limit_amt, now)

    tid = f"tr_{uuid4().hex[:16]}"
    conn.execute(
        """
        INSERT INTO transfers (
            transfer_id, quote_id, client_id, idempotency_key, request_hash,
            direction_id, source_amount, source_currency, fee_amount, fee_currency,
            target_amount, target_currency, daily_limit_amount, daily_limit_currency,
            recipient, status, reserve_state, created_at, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, 'HELD', %s, %s
        )
        """,
        (
            tid,
            quote.quote_id,
            client_id,
            f"idem_{tid}",
            "hash",
            tariff.direction_id,
            calc.source_amount.amount,
            calc.source_amount.currency.code,
            calc.fee.amount,
            calc.fee.currency.code,
            calc.target_amount.amount,
            calc.target_amount.currency.code,
            limit_amt.amount,
            limit_amt.currency.code,
            Jsonb({"name": "Test", "account": "123"}),
            status,
            now,
            now,
        ),
    )


def test_get_client_balance_formats_minor_units_and_handles_missing_client(balance_env) -> None:
    client, _, _, _ = balance_env

    resp = client.get("/clients/c_006/balance")
    assert resp.status_code == 200
    assert resp.json() == {
        "client_id": "c_006",
        "balances": {
            "KWD": {
                "currency": "KWD",
                "available": "2500.000",
                "reserved": "0.000",
                "debited": "0.000",
            },
            "RUB": {
                "currency": "RUB",
                "available": "750000.00",
                "reserved": "0.00",
                "debited": "0.00",
            },
        },
    }

    missing = client.get("/clients/c_unknown/balance")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "VALIDATION_ERROR"


def test_hold_commit_release_and_seed_idempotency(balance_env) -> None:
    client, pool, ref, rate_snap = balance_env
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
    rub = ref.currencies["RUB"]
    amt_10k = parse_money("10000.00", rub)
    amt_5k = parse_money("5000.00", rub)

    with pool.connection() as conn, conn.transaction():
        _insert_dummy_transfer(conn, ref, rate_snap, "c_001", "10000.00", now)
        _insert_dummy_transfer(conn, ref, rate_snap, "c_001", "5000.00", now)

    mid = client.get("/clients/c_001/balance").json()["balances"]["RUB"]
    assert mid == {
        "currency": "RUB",
        "available": "485000.00",
        "reserved": "15000.00",
        "debited": "0.00",
    }

    # Повторный seed_clients при рестарте не должен затирать текущие остатки
    seed_clients(pool, ref.clients)

    with pool.connection() as conn, conn.transaction():
        commit_reserved_funds(conn, "c_001", amt_10k)
        release_reserved_funds(conn, "c_001", amt_5k)

    after = client.get("/clients/c_001/balance").json()["balances"]["RUB"]
    assert after == {
        "currency": "RUB",
        "available": "490000.00",
        "reserved": "0.00",
        "debited": "10000.00",
    }


def test_insufficient_funds_and_missing_currency_balance(balance_env) -> None:
    _, pool, ref, rate_snap = balance_env
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)

    # У c_007 на балансе всего 1000.00 RUB
    with pytest.raises(ApiError) as exc_info, pool.connection() as conn, conn.transaction():
        _insert_dummy_transfer(conn, ref, rate_snap, "c_007", "2000.00", now)
    assert exc_info.value.code == "INSUFFICIENT_FUNDS"

    # У c_001 нет кошелька USD вообще
    usd = ref.currencies["USD"]
    with pytest.raises(ApiError) as usd_exc, pool.connection() as conn, conn.transaction():
        limit = lock_client_for_transfer(conn, "c_001", ref.currencies)
        usd_amt = parse_money("100.00", usd)
        limit_amt = compute_daily_limit_amount(usd_amt, limit.currency, rate_snap)
        reserve_client_funds(conn, "c_001", usd_amt, limit, limit_amt, now)
    assert usd_exc.value.code == "INSUFFICIENT_FUNDS"


def test_daily_limit_same_and_cross_currency_and_failed_excluded(balance_env) -> None:
    _, pool, ref, rate_snap = balance_env
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
    rub = ref.currencies["RUB"]

    # c_001: баланс 500k RUB, лимит 300k RUB. Отправляем 200k -> проходит, ещё 150k -> DAILY_LIMIT_EXCEEDED.
    with pool.connection() as conn, conn.transaction():
        _insert_dummy_transfer(conn, ref, rate_snap, "c_001", "200000.00", now)

    with pytest.raises(ApiError) as exc_same, pool.connection() as conn, conn.transaction():
        _insert_dummy_transfer(conn, ref, rate_snap, "c_001", "150000.00", now)
    assert exc_same.value.code == "DAILY_LIMIT_EXCEEDED"

    # Если первая заявка стала FAILED и резерв вернули — суточный лимит освобождается.
    with pool.connection() as conn, conn.transaction():
        conn.execute("UPDATE transfers SET status = 'FAILED', reserve_state = 'RELEASED' WHERE client_id = 'c_001'")
        release_reserved_funds(conn, "c_001", parse_money("200000.00", rub))
        _insert_dummy_transfer(conn, ref, rate_snap, "c_001", "150000.00", now)

    # c_003: баланс 2_000_000 RUB, суточный лимит 5_000.00 USD (курс RUB->USD = 0.0110).
    # 300_000 RUB = 3_300 USD (проходит), вторые 200_000 RUB = 2_200 USD (сумма 5_500 USD > 5_000 USD).
    with pool.connection() as conn, conn.transaction():
        _insert_dummy_transfer(conn, ref, rate_snap, "c_003", "300000.00", now)

    with pytest.raises(ApiError) as exc_cross, pool.connection() as conn, conn.transaction():
        _insert_dummy_transfer(conn, ref, rate_snap, "c_003", "200000.00", now)
    assert exc_cross.value.code == "DAILY_LIMIT_EXCEEDED"


def test_parallel_reserves_cannot_double_spend_balance(balance_env) -> None:
    client, pool, ref, rate_snap = balance_env
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)

    # У c_002 ровно 50_000.00 RUB. Два параллельных потока пытаются зарезервировать по 40_000.00 RUB.
    def worker() -> str:
        try:
            with pool.connection() as conn, conn.transaction():
                _insert_dummy_transfer(conn, ref, rate_snap, "c_002", "40000.00", now)
            return "OK"
        except ApiError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(lambda _: worker(), range(2)))

    assert outcomes == ["INSUFFICIENT_FUNDS", "OK"]

    bal = client.get("/clients/c_002/balance").json()["balances"]["RUB"]
    assert bal == {
        "currency": "RUB",
        "available": "10000.00",
        "reserved": "40000.00",
        "debited": "0.00",
    }
