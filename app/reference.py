import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from .errors import ApiError
from .money import Currency, Money, parse_money

FeeChargedIn = Literal["SOURCE", "TARGET"]
RouteKey = tuple[str, str, str, str, str]


def parse_iso_utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class FeeRule:
    percent: Decimal
    fixed: Money
    min_fee: Money
    charged_in: FeeChargedIn


@dataclass(frozen=True)
class AmountLimits:
    min_amount: Money
    max_amount: Money


@dataclass(frozen=True)
class Tariff:
    direction_id: str
    source_country: str
    source_currency: Currency
    target_country: str
    target_currency: Currency
    payout_method: str
    fee: FeeRule
    limits: AmountLimits
    min_margin_percent: Decimal
    quote_ttl_sec: int

    @property
    def route_key(self) -> RouteKey:
        return (
            self.source_country,
            self.source_currency.code,
            self.target_country,
            self.target_currency.code,
            self.payout_method,
        )


@dataclass(frozen=True)
class MaintenanceWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class ProviderFee:
    percent: Decimal
    fixed: Money


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    directions: frozenset[str]
    currency: Currency
    fee: ProviderFee
    min_limit: Money
    max_limit: Money
    success_rate_24h: Decimal
    p95_latency_ms: int
    enabled: bool
    maintenance_windows: tuple[MaintenanceWindow, ...]

    def is_available(self, now: datetime) -> bool:
        if not self.enabled:
            return False
        return not any(w.start <= now < w.end for w in self.maintenance_windows)


@dataclass(frozen=True)
class ClientSeed:
    client_id: str
    balances: dict[str, Money]
    daily_limit: Money


class ReferenceData:
    """Справочные данные из папки data/."""

    def __init__(
        self,
        currencies: dict[str, Currency],
        tariffs: dict[str, Tariff],
        providers: dict[str, ProviderConfig],
        clients: dict[str, ClientSeed],
    ) -> None:
        self.currencies = currencies
        self.tariffs = tariffs
        self.tariffs_by_route: dict[RouteKey, Tariff] = {t.route_key: t for t in tariffs.values()}
        self.providers = providers
        self.clients = clients

    @classmethod
    def load(cls, data_dir: Path) -> "ReferenceData":
        raw_currencies = cls._read_json(data_dir / "currencies.json")
        currencies = {
            item["code"]: Currency(code=item["code"], minor_units=int(item["minor_units"]))
            for item in raw_currencies
        }

        raw_tariffs = cls._read_json(data_dir / "tariffs.json")
        tariffs: dict[str, Tariff] = {}
        for item in raw_tariffs:
            src_curr = currencies[item["source"]["currency"]]
            dst_curr = currencies[item["target"]["currency"]]
            charged_in: FeeChargedIn = item["fee"]["charged_in"]
            fee_curr = src_curr if charged_in == "SOURCE" else dst_curr
            limit_curr = currencies[item["amount_limits"]["currency"]]

            tariff = Tariff(
                direction_id=item["direction_id"],
                source_country=item["source"]["country"],
                source_currency=src_curr,
                target_country=item["target"]["country"],
                target_currency=dst_curr,
                payout_method=item["payout_method"],
                fee=FeeRule(
                    percent=Decimal(item["fee"]["percent"]),
                    fixed=parse_money(item["fee"]["fixed"], fee_curr),
                    min_fee=parse_money(item["fee"]["min"], fee_curr),
                    charged_in=charged_in,
                ),
                limits=AmountLimits(
                    min_amount=parse_money(item["amount_limits"]["min"], limit_curr),
                    max_amount=parse_money(item["amount_limits"]["max"], limit_curr),
                ),
                min_margin_percent=Decimal(item["min_margin_percent"]),
                quote_ttl_sec=int(item["quote_ttl_sec"]),
            )
            tariffs[tariff.direction_id] = tariff

        raw_providers = cls._read_json(data_dir / "providers.json")
        providers: dict[str, ProviderConfig] = {}
        for item in raw_providers:
            prov_curr = currencies[item["currency"]]
            windows = tuple(
                MaintenanceWindow(start=parse_iso_utc(w["from"]), end=parse_iso_utc(w["to"]))
                for w in item.get("maintenance_windows", [])
            )
            provider = ProviderConfig(
                provider_id=item["provider_id"],
                directions=frozenset(item["directions"]),
                currency=prov_curr,
                fee=ProviderFee(
                    percent=Decimal(item["fee"]["percent"]),
                    fixed=parse_money(item["fee"]["fixed"], prov_curr),
                ),
                min_limit=parse_money(item["limits"]["min"], prov_curr),
                max_limit=parse_money(item["limits"]["max"], prov_curr),
                success_rate_24h=Decimal(item["success_rate_24h"]),
                p95_latency_ms=int(item["p95_latency_ms"]),
                enabled=bool(item["enabled"]),
                maintenance_windows=windows,
            )
            providers[provider.provider_id] = provider

        raw_clients = cls._read_json(data_dir / "clients.json")
        clients: dict[str, ClientSeed] = {}
        for item in raw_clients:
            balances = {
                code: parse_money(amount_str, currencies[code])
                for code, amount_str in item["balances"].items()
            }
            limit_curr = currencies[item["daily_limit"]["currency"]]
            clients[item["client_id"]] = ClientSeed(
                client_id=item["client_id"],
                balances=balances,
                daily_limit=parse_money(item["daily_limit"]["amount"], limit_curr),
            )

        return cls(
            currencies=currencies,
            tariffs=tariffs,
            providers=providers,
            clients=clients,
        )

    @staticmethod
    def _read_json(path: Path) -> list[dict[str, Any]]:
        return json.loads(path.read_text(encoding="utf-8"))

    def find_tariff(
        self,
        source_country: str,
        source_currency: str,
        target_country: str,
        target_currency: str,
        payout_method: str,
    ) -> Tariff:
        key = (source_country, source_currency, target_country, target_currency, payout_method)
        tariff = self.tariffs_by_route.get(key)
        if tariff is None:
            raise ApiError(
                code="UNSUPPORTED_DIRECTION",
                message="Направление или способ выплаты не поддерживается",
                status_code=422,
                details={
                    "source_country": source_country,
                    "source_currency": source_currency,
                    "target_country": target_country,
                    "target_currency": target_currency,
                    "payout_method": payout_method,
                },
            )
        return tariff
