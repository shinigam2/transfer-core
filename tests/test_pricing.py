import json
from pathlib import Path

import pytest

from app.errors import ApiError
from app.money import Money
from app.pricing import _calc_from_source_unchecked, calculate_quote
from app.rates import FileRateSource
from app.reference import ReferenceData

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
EXAMPLES_DIR = ROOT_DIR / "examples"


@pytest.fixture(scope="module")
def ref() -> ReferenceData:
    return ReferenceData.load(DATA_DIR)


@pytest.fixture(scope="module")
def rate_snap():
    return FileRateSource(DATA_DIR / "rates.json", max_age_sec=3600).snapshot()


@pytest.mark.parametrize(
    "example_path",
    sorted(EXAMPLES_DIR.glob("*.json")),
    ids=lambda p: p.stem,
)
def test_official_examples(example_path: Path, ref: ReferenceData, rate_snap) -> None:
    case = json.loads(example_path.read_text(encoding="utf-8"))
    req = case["request"]
    expected = case["response"]

    if "error" in expected["body"]:
        expected_code = expected["body"]["error"]["code"]
        with pytest.raises(ApiError) as exc_info:
            tariff = ref.find_tariff(
                req["source_country"],
                req["source_currency"],
                req["target_country"],
                req["target_currency"],
                req["payout_method"],
            )
            rq = rate_snap.get_quote_rate(tariff.source_currency.code, tariff.target_currency.code)
            calculate_quote(tariff, rq, req["amount"], req["amount_type"])
        assert exc_info.value.code == expected_code
        return

    tariff = ref.find_tariff(
        req["source_country"],
        req["source_currency"],
        req["target_country"],
        req["target_currency"],
        req["payout_method"],
    )
    rq = rate_snap.get_quote_rate(tariff.source_currency.code, tariff.target_currency.code)
    result = calculate_quote(tariff, rq, req["amount"], req["amount_type"])

    exp_body = expected["body"]
    assert result.direction_id == exp_body["direction_id"]
    assert result.source_amount.to_dict() == exp_body["source_amount"]
    assert result.fee.to_dict() == exp_body["fee"]
    assert result.amount_to_convert.to_dict() == exp_body["amount_to_convert"]
    assert result.rate_str == exp_body["rate"]
    assert result.target_amount.to_dict() == exp_body["target_amount"]


@pytest.mark.parametrize(
    ("direction_id", "desired_target_str"),
    [
        ("RU-TH-BANK", "3333.33"),   # некруглое деление (charged_in=SOURCE)
        ("RU-TH-CARD", "999.99"),    # процент + фикс + некруглое
        ("RU-JP-BANK", "7777"),      # 0 знаков у JPY
        ("KW-TH-BANK", "5123.45"),   # 3 знака у KWD
        ("RU-VN-BANK", "2069801"),   # charged_in=TARGET, шаг 1 коп = 3 VND
        ("KW-JP-BANK", "50000"),     # charged_in=TARGET, 3 знака -> 0 знаков
    ],
)
def test_target_and_source_consistency_on_non_divisible_amounts(
    direction_id: str,
    desired_target_str: str,
    ref: ReferenceData,
    rate_snap,
) -> None:
    tariff = ref.tariffs[direction_id]
    rq = rate_snap.get_quote_rate(tariff.source_currency.code, tariff.target_currency.code)

    target_quote = calculate_quote(tariff, rq, desired_target_str, "TARGET")
    source_quote = calculate_quote(tariff, rq, target_quote.source_amount.format(), "SOURCE")

    # 1. Прямой расчёт от найденного source_amount обязан давать идентичную котировку
    assert source_quote == target_quote

    # 2. Получателю приходит не меньше заказанной суммы
    desired_money = Money(
        target_quote.target_amount.amount.__class__(desired_target_str),
        tariff.target_currency,
    )
    assert target_quote.target_amount >= desired_money

    # 3. Найденный source_amount минимален: уменьшение на 1 шаг даёт меньше desired_target
    one_step_less = Money(
        target_quote.source_amount.amount - tariff.source_currency.quantum,
        tariff.source_currency,
    )
    prev_calc = _calc_from_source_unchecked(tariff, rq, one_step_less)
    assert prev_calc.target_amount < desired_money


def test_target_mode_enforces_direction_limits(ref: ReferenceData, rate_snap) -> None:
    tariff = ref.tariffs["RU-TH-BANK"]
    rq = rate_snap.get_quote_rate("RUB", "THB")

    with pytest.raises(ApiError) as low_exc:
        calculate_quote(tariff, rq, "100.00", "TARGET")
    assert low_exc.value.code == "AMOUNT_BELOW_MIN"

    with pytest.raises(ApiError) as high_exc:
        calculate_quote(tariff, rq, "200000.00", "TARGET")
    assert high_exc.value.code == "AMOUNT_ABOVE_MAX"
