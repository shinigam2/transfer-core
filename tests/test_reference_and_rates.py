from decimal import Decimal
from pathlib import Path

import pytest

from app.errors import ApiError
from app.rates import FileRateSource
from app.reference import ReferenceData, parse_iso_utc

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def test_load_reference_data_and_find_tariff() -> None:
    ref = ReferenceData.load(DATA_DIR)
    assert len(ref.currencies) == 10
    assert len(ref.tariffs) == 11
    assert len(ref.providers) == 7
    assert len(ref.clients) == 13

    tariff = ref.find_tariff("RU", "RUB", "TH", "THB", "BANK_ACCOUNT")
    assert tariff.direction_id == "RU-TH-BANK"
    assert tariff.fee.min_fee.format() == "50.00"

    with pytest.raises(ApiError) as exc_info:
        ref.find_tariff("RU", "RUB", "TH", "THB", "CASH_PICKUP")
    assert exc_info.value.code == "UNSUPPORTED_DIRECTION"


def test_provider_availability_checks_enabled_and_maintenance() -> None:
    ref = ReferenceData.load(DATA_DIR)
    assert not ref.providers["prov_delta"].is_available(parse_iso_utc("2026-11-06T12:00:00Z"))

    alpha = ref.providers["prov_alpha"]
    assert alpha.is_available(parse_iso_utc("2026-11-06T21:59:00Z"))
    assert not alpha.is_available(parse_iso_utc("2026-11-06T22:15:00Z"))
    assert alpha.is_available(parse_iso_utc("2026-11-06T22:30:00Z"))


def test_rates_fresh_direct_inverse_stale_and_missing() -> None:
    snap = FileRateSource(DATA_DIR / "rates.json", max_age_sec=3600).snapshot()

    # Свежая прямая пара
    rub_thb = snap.get_quote_rate("RUB", "THB")
    assert rub_thb.rate == Decimal("0.4200")
    assert rub_thb.rate_str == "0.4200"

    # Обратная пара (KWD->RUB=270 есть, прямой RUB->KWD нет)
    rub_kwd = snap.get_quote_rate("RUB", "KWD")
    assert rub_kwd.rate == Decimal("0.00370370")

    # Протухшие прямые пары (даже при наличии свежего пути через USD) -> RATE_UNAVAILABLE
    for stale_quote in ("AED", "PHP"):
        with pytest.raises(ApiError) as exc_info:
            snap.get_quote_rate("RUB", stale_quote)
        assert exc_info.value.code == "RATE_UNAVAILABLE"

    # Валюта вообще отсутствует в таблице курсов
    with pytest.raises(ApiError) as exc_info:
        snap.get_quote_rate("KZT", "THB")
    assert exc_info.value.code == "RATE_UNAVAILABLE"


def test_convert_internal_supports_pivots_for_providers() -> None:
    snap = FileRateSource(DATA_DIR / "rates.json", max_age_sec=3600).snapshot()

    # THB -> USD (0.0285) -> EUR (0.9200)
    thb_to_eur = snap.convert_internal(Decimal(1000), "THB", "EUR")
    assert thb_to_eur == Decimal(1000) * Decimal("0.0285") * Decimal("0.9200")

    # VND -> RUB (/300) -> EUR (*0.0110)
    vnd_to_eur = snap.convert_internal(Decimal(300000), "VND", "EUR")
    assert vnd_to_eur == Decimal("11.0")
