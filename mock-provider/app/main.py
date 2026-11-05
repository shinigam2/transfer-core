import asyncio
import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from aiohttp import web

from . import behavior
from .config import load_settings
from .reference_data import ReferenceData
from .store import COMPLETED, FAILED, PROCESSING, Store, now_utc
from .webhooks import WebhookSender

log = logging.getLogger("mock-provider")

SETTINGS = web.AppKey("settings", object)
DATA = web.AppKey("data", object)
STORE = web.AppKey("store", object)
SENDER = web.AppKey("sender", object)
TASKS = web.AppKey("tasks", set)


def error(status, code, message):
    return web.json_response({"code": code, "message": message}, status=status)


def spawn(app, coroutine):
    task = asyncio.create_task(coroutine)
    app[TASKS].add(task)
    task.add_done_callback(app[TASKS].discard)


async def health(request):
    settings = request.app[SETTINGS]
    return web.json_response({"status": "ok", "mode": settings.mode, "seed": settings.seed})


async def rates(request):
    return web.json_response(request.app[DATA].live_rates(now_utc()))


async def settle_payout(app, payout, plan):
    sender = app[SENDER]
    now = now_utc()
    if plan.visibility_lag:
        payout.visible_at = now + timedelta(seconds=plan.visibility_lag)
    if plan.unknown_event:
        await asyncio.sleep(0.2)
        await sender.deliver_unknown(payout)
    if plan.forged:
        await asyncio.sleep(min(1.0, plan.delay / 3))
        opposite = FAILED if plan.final_status == COMPLETED else COMPLETED
        await sender.deliver_forged(payout, plan.forged, opposite)
    elapsed = (now_utc() - now).total_seconds()
    await asyncio.sleep(max(0.0, plan.delay - elapsed))
    payout.status = plan.final_status
    payout.failure_code = plan.failure_code
    payout.settled_at = now_utc()
    if plan.stale_read:
        payout.stale_until = payout.settled_at + timedelta(seconds=plan.stale_read)
    if not plan.lost_webhook:
        await sender.deliver_final(payout, plan)


def validate_body(body, data):
    if not isinstance(body, dict):
        return "body must be a JSON object"
    for name in ("reference", "amount", "currency", "callback_url"):
        if not isinstance(body.get(name), str) or not body[name]:
            return f"{name} is required"
    if not data.amount_is_valid(body["amount"], body["currency"]):
        return "amount must be a string with the number of decimals of the currency"
    try:
        if Decimal(body["amount"]) <= 0:
            return "amount must be positive"
    except InvalidOperation:
        return "amount is not a number"
    if not isinstance(body.get("recipient"), dict) or not body["recipient"]:
        return "recipient is required"
    return None


def create_accepted(app, provider, key, body, plan):
    payout = app[STORE].create(provider["provider_id"], key, body, PROCESSING)
    spawn(app, settle_payout(app, payout, plan))
    return payout


def settle_plan(app, provider, key, scenario_action, slow_extra=0.0):
    settings = app[SETTINGS]
    if scenario_action is None:
        return behavior.chaos_settle(settings.seed, provider, key)
    plan = behavior.normal_settle(settings.settle_sec)
    plan.delay += slow_extra
    return plan


def replay_response(payout, now):
    if payout.sync_declined:
        return error(422, payout.failure_code, "payout was declined")
    return web.json_response({"payout_id": payout.payout_id, "status": payout.visible_status(now)}, status=201)


async def abort_connection(request):
    transport = request.transport
    if transport is not None:
        transport.abort()
    return web.Response(status=500)


async def create_payout(request):
    app = request.app
    settings, data, store = app[SETTINGS], app[DATA], app[STORE]
    provider = data.providers.get(request.match_info["provider_id"])
    if provider is None:
        return error(404, "UNKNOWN_PROVIDER", "provider does not exist")
    key = request.headers.get("Idempotency-Key")
    if not key:
        return error(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return error(400, "INVALID_REQUEST", "body is not valid JSON")
    problem = validate_body(body, data)
    if problem:
        return error(400, "INVALID_REQUEST", problem)

    provider_id = provider["provider_id"]
    state = store.state(provider_id)
    existing = state.by_key.get(key)
    now = now_utc()
    if existing and existing.fingerprint() != (body["reference"], body["amount"], body["currency"]):
        return error(409, "IDEMPOTENCY_KEY_REUSED", "same key was used with another payload")

    header_scenario = request.headers.get("X-Scenario")
    if header_scenario and header_scenario not in behavior.SCENARIOS:
        return error(400, "UNKNOWN_SCENARIO", f"unknown scenario {header_scenario}")
    forced = header_scenario or state.scenario
    use_chaos = settings.mode == "chaos" and not forced
    attempt = store.next_attempt(provider_id, key)

    if use_chaos:
        action = behavior.chaos_action(settings.seed, provider, key, attempt, existing is not None)
    else:
        action = behavior.SCENARIOS[forced or settings.default_scenario]

    if existing:
        if action == behavior.ERROR_5XX:
            return error(500, "INTERNAL_ERROR", "temporary failure")
        return replay_response(existing, now)

    if not provider["enabled"]:
        return error(503, "PROVIDER_DISABLED", "provider is disabled")
    if data.in_maintenance(provider, now):
        return error(503, "PROVIDER_MAINTENANCE", "provider is under maintenance")
    if body["currency"] not in data.target_currencies(provider):
        return error(422, "CURRENCY_NOT_SUPPORTED", "currency is not supported by this provider")
    if not data.within_limits(provider, Decimal(body["amount"]), body["currency"]):
        return error(422, "AMOUNT_OUT_OF_LIMITS", "amount is outside provider limits")

    if action == behavior.DECLINE:
        payout = store.create(provider_id, key, body, FAILED, "RECIPIENT_ACCOUNT_INVALID")
        payout.sync_declined = True
        payout.settled_at = now
        return error(422, "RECIPIENT_ACCOUNT_INVALID", "recipient account is invalid")
    if action == behavior.ERROR_5XX:
        return error(500, "INTERNAL_ERROR", "internal provider error")
    if action == behavior.DROP_LOST:
        return await abort_connection(request)

    scenario_action = None if use_chaos else action
    slow_extra = settings.slow_sec if (not use_chaos and action == behavior.SLOW) else 0.0
    plan = settle_plan(app, provider, key, scenario_action, slow_extra)
    payout = create_accepted(app, provider, key, body, plan)

    if action == behavior.DROP_CREATED:
        return await abort_connection(request)
    if action == behavior.HANG_CREATED:
        await asyncio.sleep(settings.hang_sec)
        return await abort_connection(request)
    if action == behavior.ERROR_CREATED:
        return error(504, "GATEWAY_TIMEOUT", "upstream timeout")
    if action == behavior.SLOW:
        delay = settings.slow_sec if not use_chaos else behavior.seeded(settings.seed, "slow", key).uniform(5.0, 12.0)
        await asyncio.sleep(delay)
    return web.json_response({"payout_id": payout.payout_id, "status": PROCESSING}, status=201)


def known_provider(request):
    return request.app[DATA].providers.get(request.match_info["provider_id"])


async def get_payout(request):
    provider = known_provider(request)
    if provider is None:
        return error(404, "UNKNOWN_PROVIDER", "provider does not exist")
    state = request.app[STORE].state(provider["provider_id"])
    payout = state.by_id.get(request.match_info["payout_id"])
    now = now_utc()
    if payout is None or not payout.is_visible(now):
        return error(404, "NOT_FOUND", "payout not found")
    return web.json_response(payout.public(now))


async def find_payout(request):
    provider = known_provider(request)
    if provider is None:
        return error(404, "UNKNOWN_PROVIDER", "provider does not exist")
    reference = request.query.get("reference")
    if not reference:
        return error(400, "INVALID_REQUEST", "reference query parameter is required")
    state = request.app[STORE].state(provider["provider_id"])
    payout = state.by_reference.get(reference)
    now = now_utc()
    if payout is None or not payout.is_visible(now):
        return error(404, "NOT_FOUND", "payout not found")
    return web.json_response(payout.public(now))


async def ledger(request):
    provider = known_provider(request)
    if provider is None:
        return error(404, "UNKNOWN_PROVIDER", "provider does not exist")
    state = request.app[STORE].state(provider["provider_id"])
    entries = [payout.ledger_entry() for payout in state.by_id.values()]
    return web.json_response({"provider_id": provider["provider_id"], "entries": entries})


async def set_scenario(request):
    provider = known_provider(request)
    if provider is None:
        return error(404, "UNKNOWN_PROVIDER", "provider does not exist")
    body = await request.json()
    scenario = body.get("scenario")
    if scenario not in behavior.SCENARIOS:
        return error(400, "UNKNOWN_SCENARIO", f"unknown scenario {scenario}")
    request.app[STORE].state(provider["provider_id"]).scenario = scenario
    return web.json_response({"provider_id": provider["provider_id"], "scenario": scenario})


async def clear_scenario(request):
    provider = known_provider(request)
    if provider is None:
        return error(404, "UNKNOWN_PROVIDER", "provider does not exist")
    request.app[STORE].state(provider["provider_id"]).scenario = None
    return web.json_response({"provider_id": provider["provider_id"], "scenario": None})


async def on_startup(app):
    await app[SENDER].start()


async def on_cleanup(app):
    for task in list(app[TASKS]):
        task.cancel()
    await app[SENDER].stop()


def build_app(settings):
    app = web.Application()
    app[SETTINGS] = settings
    app[DATA] = ReferenceData(settings.data_dir)
    app[STORE] = Store()
    app[SENDER] = WebhookSender(settings.webhook_secret)
    app[TASKS] = set()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/health", health)
    app.router.add_get("/rates", rates)
    app.router.add_post("/{provider_id}/payouts", create_payout)
    app.router.add_get("/{provider_id}/payouts", find_payout)
    app.router.add_get("/{provider_id}/payouts/{payout_id}", get_payout)
    app.router.add_get("/{provider_id}/admin/ledger", ledger)
    app.router.add_put("/{provider_id}/admin/scenario", set_scenario)
    app.router.add_delete("/{provider_id}/admin/scenario", clear_scenario)
    return app


def main():
    settings = load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("starting mode=%s seed=%s", settings.mode, settings.seed)
    web.run_app(build_app(settings), host="0.0.0.0", port=settings.port, access_log=None, print=None)


if __name__ == "__main__":
    main()
