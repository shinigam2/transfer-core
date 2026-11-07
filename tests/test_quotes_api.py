import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.rates import FileRateSource, HttpRateSource

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
EXAMPLES_DIR = ROOT_DIR / "examples"


@dataclass
class ManualClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current = self.current + timedelta(seconds=seconds)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(current=datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
def client(test_db_url: str, clock: ManualClock):
    settings = Settings(
        database_url=test_db_url,
        mock_provider_url="http://localhost:9000",
        webhook_secret="test-secret",
        data_dir=DATA_DIR,
    )
    rate_source = FileRateSource(DATA_DIR / "rates.json", max_age_sec=3600)
    app = create_app(settings=settings, rate_source=rate_source, clock=clock)
    with TestClient(app) as test_client:
        yield test_client


def test_health_endpoint(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "example_path",
    sorted(EXAMPLES_DIR.glob("*.json")),
    ids=lambda p: p.stem,
)
def test_quotes_api_matches_all_official_examples(
    example_path: Path,
    client: TestClient,
) -> None:
    case = json.loads(example_path.read_text(encoding="utf-8"))
    resp = client.post("/quotes", json=case["request"])
    expected = case["response"]

    if "error" in expected["body"]:
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == expected["body"]["error"]["code"]
        return

    assert resp.status_code == 201
    body = resp.json()
    exp_body = expected["body"]

    assert body["quote_id"].startswith("qt_")
    assert body["direction_id"] == exp_body["direction_id"]
    assert body["source_amount"] == exp_body["source_amount"]
    assert body["fee"] == exp_body["fee"]
    assert body["amount_to_convert"] == exp_body["amount_to_convert"]
    assert body["rate"] == exp_body["rate"]
    assert body["target_amount"] == exp_body["target_amount"]
    assert isinstance(body["breakdown"], list) and len(body["breakdown"]) == 2
    assert body["expires_at"].endswith("Z")

    # GET /quotes/{id} обязан отдавать сохранённую котировку в том же формате
    fetched = client.get(f"/quotes/{body['quote_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == body


def test_quote_determinism(client: TestClient) -> None:
    payload = {
        "source_country": "RU",
        "source_currency": "RUB",
        "target_country": "TH",
        "target_currency": "THB",
        "payout_method": "BANK_ACCOUNT",
        "amount": "12345.67",
        "amount_type": "SOURCE",
    }
    first = client.post("/quotes", json=payload).json()
    second = client.post("/quotes", json=payload).json()

    assert first["quote_id"] != second["quote_id"]
    for field in ("direction_id", "source_amount", "fee", "amount_to_convert", "rate", "target_amount", "breakdown"):
        assert first[field] == second[field]


def test_quote_not_found_and_ttl_expiration(client: TestClient, clock: ManualClock) -> None:
    missing = client.get("/quotes/qt_nonexistent")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "QUOTE_NOT_FOUND"

    # У RU-TH-BANK quote_ttl_sec = 900
    created = client.post(
        "/quotes",
        json={
            "source_country": "RU",
            "source_currency": "RUB",
            "target_country": "TH",
            "target_currency": "THB",
            "payout_method": "BANK_ACCOUNT",
            "amount": "10000.00",
            "amount_type": "SOURCE",
        },
    )
    assert created.status_code == 201
    quote_id = created.json()["quote_id"]
    assert created.json()["expires_at"] == "2026-11-07T12:15:00Z"

    clock.advance(899)
    still_alive = client.get(f"/quotes/{quote_id}")
    assert still_alive.status_code == 200

    clock.advance(1)
    expired = client.get(f"/quotes/{quote_id}")
    assert expired.status_code == 422
    assert expired.json()["error"]["code"] == "QUOTE_EXPIRED"


@pytest.mark.parametrize(
    "bad_payload",
    [
        # Сумма передана числом вместо строки
        {
            "source_country": "RU",
            "source_currency": "RUB",
            "target_country": "TH",
            "target_currency": "THB",
            "payout_method": "BANK_ACCOUNT",
            "amount": 5000.00,
            "amount_type": "SOURCE",
        },
        # Неверное число знаков после запятой для RUB
        {
            "source_country": "RU",
            "source_currency": "RUB",
            "target_country": "TH",
            "target_currency": "THB",
            "payout_method": "BANK_ACCOUNT",
            "amount": "5000",
            "amount_type": "SOURCE",
        },
        # Дробная часть у бездробной JPY
        {
            "source_country": "RU",
            "source_currency": "RUB",
            "target_country": "JP",
            "target_currency": "JPY",
            "payout_method": "BANK_ACCOUNT",
            "amount": "10000.00",
            "amount_type": "TARGET",
        },
        # Нулевая сумма
        {
            "source_country": "RU",
            "source_currency": "RUB",
            "target_country": "TH",
            "target_currency": "THB",
            "payout_method": "BANK_ACCOUNT",
            "amount": "0.00",
            "amount_type": "SOURCE",
        },
        # Неизвестный amount_type
        {
            "source_country": "RU",
            "source_currency": "RUB",
            "target_country": "TH",
            "target_currency": "THB",
            "payout_method": "BANK_ACCOUNT",
            "amount": "5000.00",
            "amount_type": "INVALID",
        },
        # Пропущено обязательное поле payout_method
        {
            "source_country": "RU",
            "source_currency": "RUB",
            "target_country": "TH",
            "target_currency": "THB",
            "amount": "5000.00",
            "amount_type": "SOURCE",
        },
    ],
)
def test_quotes_validation_errors(client: TestClient, bad_payload: dict[str, object]) -> None:
    resp = client.post("/quotes", json=bad_payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_quotes_invalid_json_body(client: TestClient) -> None:
    resp = client.post(
        "/quotes",
        content=b"{not-valid-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    ("source_country", "source_currency", "target_country", "target_currency", "amount"),
    [
        # RUB -> AED протух на 4 дня
        ("RU", "RUB", "AE", "AED", "5000.00"),
        # KZT вообще отсутствует в rates.json
        ("KZ", "KZT", "TH", "THB", "20000.00"),
    ],
)
def test_quotes_rate_unavailable_for_stale_or_missing_pairs(
    client: TestClient,
    source_country: str,
    source_currency: str,
    target_country: str,
    target_currency: str,
    amount: str,
) -> None:
    resp = client.post(
        "/quotes",
        json={
            "source_country": source_country,
            "source_currency": source_currency,
            "target_country": target_country,
            "target_currency": target_currency,
            "payout_method": "BANK_ACCOUNT",
            "amount": amount,
            "amount_type": "SOURCE",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "RATE_UNAVAILABLE"


def test_quotes_rate_unavailable_when_upstream_down(test_db_url: str, clock: ManualClock) -> None:
    settings = Settings(
        database_url=test_db_url,
        mock_provider_url="http://127.0.0.1:9",
        webhook_secret="test-secret",
        data_dir=DATA_DIR,
    )
    broken_source = HttpRateSource("http://127.0.0.1:9", timeout_sec=0.2)
    app = create_app(settings=settings, rate_source=broken_source, clock=clock)
    with TestClient(app) as broken_client:
        resp = broken_client.post(
            "/quotes",
            json={
                "source_country": "RU",
                "source_currency": "RUB",
                "target_country": "TH",
                "target_currency": "THB",
                "payout_method": "BANK_ACCOUNT",
                "amount": "5000.00",
                "amount_type": "SOURCE",
            },
        )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "RATE_UNAVAILABLE"
