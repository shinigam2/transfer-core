#!/usr/bin/env python3
import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

SUCCESS_STATES = {"COMPLETED", "SUCCEEDED", "SUCCESS", "PAID", "PAID_OUT", "DONE", "SETTLED"}
FAILURE_STATES = {"FAILED", "FAILURE", "DECLINED", "REJECTED", "CANCELLED", "CANCELED", "REFUNDED", "RETURNED", "EXPIRED"}
REVIEW_STATES = {
    "NEEDS_REVIEW",
    "REQUIRES_REVIEW",
    "MANUAL_REVIEW",
    "REVIEW",
    "NEEDS_ATTENTION",
    "ATTENTION",
    "NEEDS_CHECK",
    "REQUIRES_CHECK",
    "MANUAL",
}
AVAILABLE_KEYS = ("available", "available_balance", "free", "balance")
RESERVED_KEYS = ("reserved", "on_hold", "hold", "held", "in_reserve", "reserve", "holds")
CLIENT_REJECTIONS = {"INSUFFICIENT_FUNDS", "DAILY_LIMIT_EXCEEDED"}


def http(method, url, body=None, headers=None, timeout=70):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, parse_json(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, parse_json(exc.read())
    except OSError as exc:
        return 0, {"transport_error": str(exc)}


def parse_json(raw):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


def error_code(body):
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"].get("code")
    return None


def read_balance_record(record):
    available = next((record[k] for k in AVAILABLE_KEYS if k in record), None)
    reserved = next((record[k] for k in RESERVED_KEYS if k in record), None)
    if available is None or reserved is None:
        return None
    return Decimal(str(available)), Decimal(str(reserved))


def walk_balances(node, currency, found):
    if isinstance(node, list):
        for item in node:
            walk_balances(item, currency, found)
    elif isinstance(node, dict):
        code = node.get("currency", currency)
        record = read_balance_record(node)
        if record is not None and code:
            found[code] = record
            return
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                walk_balances(value, key if len(key) == 3 and key.isupper() else code, found)


def parse_balances(body):
    found = {}
    walk_balances(body, None, found)
    return found


class Report:
    def __init__(self):
        self.violations = defaultdict(list)
        self.counters = Counter()

    def violation(self, kind, detail):
        self.violations[kind].append(detail)

    def failed(self):
        return any(self.violations.values())


def discover_directions(args, tariffs):
    usable = []
    for tariff in tariffs:
        low = Decimal(tariff["amount_limits"]["min"])
        body = quote_body(tariff, low * 2)
        status, response = http("POST", f"{args.api}/quotes", body)
        if status == 201:
            usable.append(tariff)
        elif status == 0 or status >= 500:
            raise SystemExit(f"POST /quotes failed: status={status} body={response}")
    return usable


def quote_body(tariff, amount):
    return {
        "source_country": tariff["source"]["country"],
        "source_currency": tariff["source"]["currency"],
        "target_country": tariff["target"]["country"],
        "target_currency": tariff["target"]["currency"],
        "payout_method": tariff["payout_method"],
        "amount": str(amount),
        "amount_type": "SOURCE",
    }


def build_plan(args, tariffs, clients, currencies, run_id, rng):
    plan = []
    spendable = {}
    for client in clients:
        limit = client["daily_limit"]
        for code, value in client["balances"].items():
            if limit["currency"] == code:
                spendable[(client["client_id"], code)] = min(Decimal(value), Decimal(limit["amount"]))
    for index in range(args.transfers):
        order = list(tariffs)
        rng.shuffle(order)
        for tariff in order:
            currency = tariff["source"]["currency"]
            low = Decimal(tariff["amount_limits"]["min"])
            high = min(low * 4, Decimal(tariff["amount_limits"]["max"]))
            minor = currencies[currency]
            amount = (low + (high - low) * Decimal(rng.randint(5, 95)) / Decimal(100)).quantize(
                Decimal(1).scaleb(-minor), rounding=ROUND_DOWN
            )
            eligible = [key for key, left in spendable.items() if key[1] == currency and left >= amount * 3]
            if not eligible:
                continue
            ranked = sorted(eligible, key=lambda key: spendable[key], reverse=True)[:5]
            key = rng.choice(ranked)
            spendable[key] -= amount
            plan.append(
                {
                    "index": index,
                    "tariff": tariff,
                    "client_id": key[0],
                    "currency": currency,
                    "amount": amount,
                    "account": f"CH{run_id}{index:05d}",
                    "double_submit": rng.random() < 0.15,
                }
            )
            break
    return plan


def post_transfer(args, quote_id, client_id, account, key):
    body = {
        "quote_id": quote_id,
        "client_id": client_id,
        "recipient": {"name": f"Chaos Recipient {account}", "account": account, "bank_code": "NWTB"},
    }
    for attempt in range(4):
        status, response = http("POST", f"{args.api}/transfers", body, {"Idempotency-Key": key}, args.request_timeout)
        if status and status < 500:
            return status, response
        time.sleep(1 + attempt)
    return status, response


def submit(args, item, run_id, report):
    tariff = item["tariff"]
    status, quote = http("POST", f"{args.api}/quotes", quote_body(tariff, item["amount"]))
    if status != 201:
        report.counters["quote_rejected"] += 1
        return None
    key = f"chaos-{run_id}-{item['index']}"
    if item["double_submit"]:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(post_transfer, args, quote["quote_id"], item["client_id"], item["account"], key) for _ in range(2)
            ]
            results = [future.result() for future in futures]
        ids = {r[1].get("transfer_id") for r in results if isinstance(r[1], dict) and r[1].get("transfer_id")}
        if len(ids) > 1:
            report.violation("idempotency", f"{key}: two transfers for one key: {sorted(ids)}")
        report.counters["double_submits"] += 1
        status, response = next((r for r in results if isinstance(r[1], dict) and r[1].get("transfer_id")), results[0])
    else:
        status, response = post_transfer(args, quote["quote_id"], item["client_id"], item["account"], key)
    code = error_code(response)
    if code in CLIENT_REJECTIONS:
        report.counters["client_rejected"] += 1
        return None
    transfer_id = response.get("transfer_id") if isinstance(response, dict) else None
    if not transfer_id:
        report.violation("api_errors", f"{key}: POST /transfers status={status} body={response}")
        return None
    report.counters["submitted"] += 1
    return {
        "transfer_id": transfer_id,
        "client_id": item["client_id"],
        "currency": item["currency"],
        "account": item["account"],
        "source_amount": Decimal(quote["source_amount"]["amount"]),
        "target_amount": quote["target_amount"]["amount"],
        "target_currency": quote["target_amount"]["currency"],
        "status": response.get("status"),
    }


def classify(status, args):
    value = str(status or "").upper()
    if value in args.success_states:
        return "success"
    if value in args.failure_states:
        return "failure"
    if value in args.review_states:
        return "review"
    return "pending"


def poll_until_settled(args, transfers):
    deadline = time.time() + args.settle_timeout
    pending = {t["transfer_id"]: t for t in transfers}
    while pending and time.time() < deadline:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {
                pool.submit(http, "GET", f"{args.api}/transfers/{tid}", None, None, args.request_timeout): tid for tid in pending
            }
            for future in as_completed(futures):
                tid = futures[future]
                status, body = future.result()
                if status == 200 and isinstance(body, dict):
                    pending[tid]["status"] = body.get("status")
                    if classify(body.get("status"), args) != "pending":
                        del pending[tid]
        if pending:
            print(f"  waiting for {len(pending)} transfers", flush=True)
            time.sleep(args.poll_interval)
    return pending


def fetch_ledgers(args, providers, run_id):
    tag = f"CH{run_id}"
    entries = defaultdict(list)
    for provider in providers:
        status, body = http("GET", f"{args.mock}/{provider['provider_id']}/admin/ledger")
        if status != 200:
            raise SystemExit(f"cannot read ledger of {provider['provider_id']}: {status}")
        for entry in body.get("entries", []):
            account = str((entry.get("recipient") or {}).get("account", ""))
            if account.startswith(tag):
                entry["provider_id"] = provider["provider_id"]
                entries[account].append(entry)
    return entries


def compare_with_ledgers(args, transfers, ledgers, report):
    known = {t["account"] for t in transfers}
    for account, entries in ledgers.items():
        paid = [e for e in entries if e["status"] == "COMPLETED"]
        open_entries = [e for e in entries if e["status"] == "PROCESSING"]
        if len(paid) > 1:
            where = ", ".join(f"{e['provider_id']}:{e['payout_id']}" for e in paid)
            report.violation("double_payouts", f"{account}: paid {len(paid)} times ({where})")
        elif len(paid) + len(open_entries) > 1:
            where = ", ".join(f"{e['provider_id']}:{e['status']}" for e in paid + open_entries)
            report.violation("double_payouts", f"{account}: more than one live payout ({where})")
        if account not in known and paid:
            report.violation("unaccounted_payouts", f"{account}: provider paid a transfer the API never returned")
    for transfer in transfers:
        entries = ledgers.get(transfer["account"], [])
        paid = [e for e in entries if e["status"] == "COMPLETED"]
        kind = classify(transfer["status"], args)
        report.counters[f"final_{kind}"] += 1
        label = f"{transfer['transfer_id']} ({transfer['account']}, status {transfer['status']})"
        if kind == "success" and not paid:
            report.violation("success_without_payout", label)
        if kind == "failure" and paid:
            report.violation("failed_but_paid", label)
        for entry in paid:
            if entry["amount"] != transfer["target_amount"] or entry["currency"] != transfer["target_currency"]:
                report.violation(
                    "amount_mismatch",
                    f"{label}: quote {transfer['target_amount']} {transfer['target_currency']}, "
                    f"provider paid {entry['amount']} {entry['currency']}",
                )


def snapshot(args, client_ids):
    balances = {}
    for client_id in client_ids:
        status, body = http("GET", f"{args.api}/clients/{client_id}/balance")
        if status != 200:
            raise SystemExit(f"GET /clients/{client_id}/balance failed: {status} {body}")
        parsed = parse_balances(body)
        if not parsed:
            raise SystemExit(
                "cannot read available and reserved amounts from GET /clients/{id}/balance; "
                f"response was {json.dumps(body, ensure_ascii=False)}"
            )
        balances[client_id] = parsed
    return balances


def check_balances(args, transfers, before, after, report):
    debited = defaultdict(Decimal)
    expected_held = defaultdict(Decimal)
    for transfer in transfers:
        key = (transfer["client_id"], transfer["currency"])
        kind = classify(transfer["status"], args)
        if kind == "success":
            debited[key] += transfer["source_amount"]
        elif kind in ("review", "pending"):
            expected_held[key] += transfer["source_amount"]
    for client_id, currencies in after.items():
        for code, (available, reserved) in currencies.items():
            start_available, start_reserved = before.get(client_id, {}).get(code, (Decimal(0), Decimal(0)))
            key = (client_id, code)
            if reserved - start_reserved != expected_held[key]:
                report.violation(
                    "stuck_reserves",
                    f"{client_id} {code}: reserved grew by {reserved - start_reserved}, "
                    f"transfers in progress or review hold {expected_held[key]}",
                )
            total_before = start_available + start_reserved
            total_after = available + reserved + debited[key]
            if total_before != total_after:
                report.violation(
                    "balance_invariant",
                    f"{client_id} {code}: start {total_before} != available {available} "
                    f"+ reserved {reserved} + debited {debited[key]}",
                )


def print_report(report, transfers):
    print("\n=== chaos_run report ===")
    for key in sorted(report.counters):
        print(f"{key:28} {report.counters[key]}")
    print(f"{'statuses':28} {dict(Counter(str(t['status']) for t in transfers))}")
    print()
    names = [
        "double_payouts",
        "stuck_reserves",
        "balance_invariant",
        "failed_but_paid",
        "success_without_payout",
        "amount_mismatch",
        "unaccounted_payouts",
        "idempotency",
        "stuck_transfers",
        "api_errors",
    ]
    for name in names:
        items = report.violations.get(name, [])
        print(f"{name:28} {len(items)}")
        for item in items[:10]:
            print(f"    {item}")
        if len(items) > 10:
            print(f"    ... and {len(items) - 10} more")
    print()
    print("RESULT: FAIL" if report.failed() else "RESULT: OK")


def parse_args():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--transfers", type=int, default=300)
    parser.add_argument("--api", default="http://localhost:8080")
    parser.add_argument("--mock", default="http://localhost:9000")
    parser.add_argument("--data", default=str(root / "data"))
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--settle-timeout", type=int, default=420)
    parser.add_argument("--ledger-grace", type=int, default=30)
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--request-timeout", type=float, default=70.0)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--success-state", action="append", default=[])
    parser.add_argument("--failure-state", action="append", default=[])
    parser.add_argument("--review-state", action="append", default=[])
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args()
    args.api = args.api.rstrip("/")
    args.mock = args.mock.rstrip("/")
    args.success_states = SUCCESS_STATES | {s.upper() for s in args.success_state}
    args.failure_states = FAILURE_STATES | {s.upper() for s in args.failure_state}
    args.review_states = REVIEW_STATES | {s.upper() for s in args.review_state}
    return args


def read_json(root, name):
    return json.loads((Path(root) / f"{name}.json").read_text(encoding="utf-8"))


def main():
    args = parse_args()
    tariffs = read_json(args.data, "tariffs")
    providers = read_json(args.data, "providers")
    clients = read_json(args.data, "clients")
    currencies = {c["code"]: c["minor_units"] for c in read_json(args.data, "currencies")}

    status, health = http("GET", f"{args.mock}/health")
    if status != 200:
        raise SystemExit(f"mock-provider is not reachable at {args.mock}")
    if health.get("mode") != "chaos":
        print(f"WARNING: mock-provider runs in mode {health.get('mode')}, start it with MOCK_MODE=chaos", flush=True)

    run_id = uuid.uuid4().hex[:8].upper()
    rng = random.Random(args.seed)
    usable = discover_directions(args, tariffs)
    if not usable:
        raise SystemExit("POST /quotes did not accept any direction")
    print(f"run {run_id}: {len(usable)} directions usable: {[t['direction_id'] for t in usable]}", flush=True)

    plan = build_plan(args, usable, clients, currencies, run_id, rng)
    client_ids = sorted({item["client_id"] for item in plan})
    before = snapshot(args, client_ids)

    report = Report()
    transfers = []
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(submit, args, item, run_id, report) for item in plan]
        for done, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            if result:
                transfers.append(result)
            if done % 50 == 0:
                print(f"  submitted {done}/{len(plan)}", flush=True)

    pending = poll_until_settled(args, transfers)
    for transfer in pending.values():
        report.violation(
            "stuck_transfers",
            f"{transfer['transfer_id']} ({transfer['account']}) is still {transfer['status']} after {args.settle_timeout}s",
        )
    print(f"  waiting {args.ledger_grace}s for providers to settle", flush=True)
    time.sleep(args.ledger_grace)
    for transfer in transfers:
        status, body = http("GET", f"{args.api}/transfers/{transfer['transfer_id']}", None, None, args.request_timeout)
        if status == 200 and isinstance(body, dict):
            transfer["status"] = body.get("status")
    ledgers = fetch_ledgers(args, providers, run_id)
    compare_with_ledgers(args, transfers, ledgers, report)
    after = snapshot(args, client_ids)
    check_balances(args, transfers, before, after, report)
    report.counters["duration_sec"] = int(time.time() - started)
    print_report(report, transfers)
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "counters": dict(report.counters),
                    "violations": {k: v for k, v in report.violations.items()},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    sys.exit(1 if report.failed() else 0)


if __name__ == "__main__":
    main()
