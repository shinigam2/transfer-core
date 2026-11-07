from pathlib import Path

import pytest

from app.pricing import calculate_quote
from app.rates import FileRateSource
from app.reference import ReferenceData, parse_iso_utc
from app.routing import select_providers

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"


@pytest.fixture(scope="module")
def ref() -> ReferenceData:
    return ReferenceData.load(DATA_DIR)


@pytest.fixture(scope="module")
def rate_snap():
    return FileRateSource(DATA_DIR / "rates.json", max_age_sec=3600).snapshot()


def test_ru_th_bank_filters_traps_and_ranks_alpha_first(ref: ReferenceData, rate_snap) -> None:
    tariff = ref.tariffs["RU-TH-BANK"]
    rq = rate_snap.get_quote_rate("RUB", "THB")
    calc = calculate_quote(tariff, rq, "10000.00", "SOURCE")

    decision = select_providers(
        tariff=tariff,
        source_amount=calc.source_amount,
        client_fee=calc.fee,
        target_amount=calc.target_amount,
        providers=ref.providers.values(),
        rate_snapshot=rate_snap,
        now=parse_iso_utc("2026-11-06T12:00:00Z"),
    )

    ranked_ids = [item.provider.provider_id for item in decision.ranked]
    assert ranked_ids == ["prov_alpha", "prov_charlie", "prov_bravo"]

    reasons = {r.provider_id: r.reason for r in decision.rejected}
    assert reasons["prov_delta"] == "DISABLED"
    assert reasons["prov_echo"] == "MAINTENANCE"
    assert reasons["prov_golf"] == "LOW_MARGIN"
    assert reasons["prov_foxtrot"] == "UNSUPPORTED_DIRECTION"


def test_maintenance_window_shifts_primary_to_charlie(ref: ReferenceData, rate_snap) -> None:
    tariff = ref.tariffs["RU-TH-BANK"]
    rq = rate_snap.get_quote_rate("RUB", "THB")
    calc = calculate_quote(tariff, rq, "10000.00", "SOURCE")

    # В 22:15 UTC у prov_alpha ежедневное окно техработ 22:00–22:30 UTC
    decision = select_providers(
        tariff=tariff,
        source_amount=calc.source_amount,
        client_fee=calc.fee,
        target_amount=calc.target_amount,
        providers=ref.providers.values(),
        rate_snapshot=rate_snap,
        now=parse_iso_utc("2026-11-06T22:15:00Z"),
    )

    ranked_ids = [item.provider.provider_id for item in decision.ranked]
    assert ranked_ids == ["prov_charlie", "prov_bravo"]
    reasons = {r.provider_id: r.reason for r in decision.rejected}
    assert reasons["prov_alpha"] == "MAINTENANCE"


def test_provider_limits_and_exclude_list(ref: ReferenceData, rate_snap) -> None:
    tariff = ref.tariffs["RU-TH-BANK"]
    rq = rate_snap.get_quote_rate("RUB", "THB")
    # 1000.00 RUB -> target 399.00 THB (~11.37 USD < min 20.00 USD у prov_charlie)
    calc_small = calculate_quote(tariff, rq, "1000.00", "SOURCE")

    decision_small = select_providers(
        tariff=tariff,
        source_amount=calc_small.source_amount,
        client_fee=calc_small.fee,
        target_amount=calc_small.target_amount,
        providers=ref.providers.values(),
        rate_snapshot=rate_snap,
        now=parse_iso_utc("2026-11-06T12:00:00Z"),
    )
    reasons = {r.provider_id: r.reason for r in decision_small.rejected}
    assert reasons["prov_charlie"] == "OUT_OF_LIMITS"
    assert [p.provider.provider_id for p in decision_small.ranked] == ["prov_alpha", "prov_bravo"]

    # Failover с исключением уже отказавшего prov_alpha
    failover = select_providers(
        tariff=tariff,
        source_amount=calc_small.source_amount,
        client_fee=calc_small.fee,
        target_amount=calc_small.target_amount,
        providers=ref.providers.values(),
        rate_snapshot=rate_snap,
        now=parse_iso_utc("2026-11-06T12:00:00Z"),
        exclude_providers={"prov_alpha"},
    )
    assert [p.provider.provider_id for p in failover.ranked] == ["prov_bravo"]


def test_target_charged_fee_direction_routing_works(ref: ReferenceData, rate_snap) -> None:
    # RU-VN-BANK берёт комиссию клиента в TARGET (VND), а провайдеры считают в EUR и USD
    tariff = ref.tariffs["RU-VN-BANK"]
    rq = rate_snap.get_quote_rate("RUB", "VND")
    calc = calculate_quote(tariff, rq, "7000.00", "SOURCE")

    decision = select_providers(
        tariff=tariff,
        source_amount=calc.source_amount,
        client_fee=calc.fee,
        target_amount=calc.target_amount,
        providers=ref.providers.values(),
        rate_snapshot=rate_snap,
        now=parse_iso_utc("2026-11-06T12:00:00Z"),
    )
    ranked_ids = [p.provider.provider_id for p in decision.ranked]
    assert "prov_charlie" in ranked_ids or "prov_bravo" in ranked_ids
    for item in decision.ranked:
        assert item.margin_percent >= tariff.min_margin_percent
