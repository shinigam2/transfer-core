import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

STALE_AFTER = timedelta(hours=6)


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_time(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReferenceData:
    def __init__(self, data_dir):
        root = Path(data_dir)
        self.currencies = {c["code"]: c["minor_units"] for c in self._read(root, "currencies")}
        self.providers = {p["provider_id"]: p for p in self._read(root, "providers")}
        self.tariffs = {t["direction_id"]: t for t in self._read(root, "tariffs")}
        self.rates = self._read(root, "rates")
        newest = max(parse_time(r["updated_at"]) for r in self.rates)
        self.rate_ages = [newest - parse_time(r["updated_at"]) for r in self.rates]
        self.rate_table = {(r["base"], r["quote"]): Decimal(r["rate"]) for r in self.rates}

    @staticmethod
    def _read(root, name):
        return json.loads((root / f"{name}.json").read_text(encoding="utf-8"))

    def live_rates(self, now):
        result = []
        for rate, age in zip(self.rates, self.rate_ages):
            shown = age if age > STALE_AFTER else min(age, timedelta(minutes=2))
            result.append(
                {
                    "base": rate["base"],
                    "quote": rate["quote"],
                    "rate": rate["rate"],
                    "updated_at": format_time(now - shown),
                }
            )
        return result

    def target_currencies(self, provider):
        codes = set()
        for direction_id in provider["directions"]:
            tariff = self.tariffs.get(direction_id)
            if tariff:
                codes.add(tariff["target"]["currency"])
        return codes

    def in_maintenance(self, provider, now):
        for window in provider.get("maintenance_windows", []):
            if parse_time(window["from"]) <= now < parse_time(window["to"]):
                return True
        return False

    def convert(self, amount, source, target):
        if source == target:
            return amount
        direct = self.rate_table.get((source, target))
        if direct is not None:
            return amount * direct
        inverse = self.rate_table.get((target, source))
        if inverse is not None:
            return amount / inverse
        for (base, quote), rate in self.rate_table.items():
            if base == source:
                onward = self.rate_table.get((quote, target))
                if onward is not None:
                    return amount * rate * onward
        return None

    def amount_is_valid(self, text, currency):
        minor = self.currencies.get(currency)
        if minor is None or not isinstance(text, str):
            return False
        whole, dot, fraction = text.partition(".")
        if not whole.isdigit():
            return False
        if minor == 0:
            return dot == ""
        return dot == "." and len(fraction) == minor and fraction.isdigit()

    def within_limits(self, provider, amount, currency):
        converted = self.convert(amount, currency, provider["currency"])
        if converted is None:
            return True
        tolerance = Decimal("0.10")
        low = Decimal(provider["limits"]["min"]) * (1 - tolerance)
        high = Decimal(provider["limits"]["max"]) * (1 + tolerance)
        return low <= converted <= high
