#!/usr/bin/env python3
"""
poller.py – Waaree /c/ traffic logger and publish earnings→MQTT

✅ Day (05:00 → 19:00):
   - fetch every 120s
   - publish every 120s

✅ Night (19:00 → 05:00):
   - fetch every 3600s (1 hour)
   - publish cached payload every 120s

✅ Always:
   - if cache is empty (first run / reboot), fetch immediately to seed cache
   - fresh login on every fetch (required by Waaree auth/session/wasm)
   - capture /c/ responses via page.on("response") into dict
   - robust timeouts and clean shutdown
   - MQTT connection stays open
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, Optional
from urllib.parse import urlparse

from aiohttp import web
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, Page
from aiomqtt import Client as MQTT

# ───────────── LOAD ENV VARIABLES ──────────────────────────────────
load_dotenv()

# ───────────── USER CONFIG ──────────────────────────────────────────
WAAREE_USERNAME = os.getenv("WAAREE_USER", "")
WAAREE_PASSWORD = os.getenv("WAAREE_PASS", "")

MQTT_HOST = os.getenv("MQTT_HOST", "192.168.0.2")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "homeassistant/waaree/energy")

# Time windows
DAY_START_HOUR = int(os.getenv("DAY_START_HOUR", 5))        # 5 AM
NIGHT_START_HOUR = int(os.getenv("NIGHT_START_HOUR", 19))   # 7 PM

# Publish schedule (always publish every 2 min)
PUBLISH_INTERVAL = int(os.getenv("PUBLISH_INTERVAL", 120))

# Fetch schedule
DAY_FETCH_INTERVAL = int(os.getenv("DAY_FETCH_INTERVAL", 120))       # 2 min
NIGHT_FETCH_INTERVAL = int(os.getenv("NIGHT_FETCH_INTERVAL", 3600))  # 1 hour

SETTLE_WAIT = int(os.getenv("SETTLE_WAIT", 10))
EARNINGS_WAIT_SEC = int(os.getenv("EARNINGS_WAIT_SEC", 30))

LOGIN_TIMEOUT_MS = int(os.getenv("LOGIN_TIMEOUT_MS", 90_000))
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", 90_000))

# Hard kill for one fetch attempt
FETCH_HARD_TIMEOUT_SEC = int(os.getenv("FETCH_HARD_TIMEOUT_SEC", 180))

EARNINGS_KEY = os.getenv("EARNINGS_KEY", "/c/v0/plant/earnings/all")

STATUS_PORT = int(os.getenv("STATUS_PORT", 8099))
# ────────────────────────────────────────────────────────────────────

level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=level,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("waaree")

RUNNING = True
_current_task: Optional[asyncio.Task] = None

# Cache last successful payload
LAST_GOOD_PAYLOAD: Optional[str] = None
LAST_GOOD_TIME_STR: Optional[str] = None
LAST_FETCH_TS: Optional[float] = None  # event loop time of last successful fetch

# ───────────── Status tracking ──────────────────────────────────────
START_TIME = time.time()
STATUS = {
    "mqtt_connected": False,
    "mqtt_error": None,
    "last_fetch_time": None,
    "last_fetch_ok": None,
    "last_fetch_error": None,
    "fetch_count": 0,
    "fetch_fail_count": 0,
    "last_publish_time": None,
    "publish_count": 0,
    "publish_fail_count": 0,
    "next_action": "starting",
}


# ───────────── Helper: Day / Night ──────────────────────────────────
def is_night_time() -> bool:
    hour = datetime.now().hour
    return hour >= NIGHT_START_HOUR or hour < DAY_START_HOUR


def get_fetch_interval_seconds() -> int:
    return NIGHT_FETCH_INTERVAL if is_night_time() else DAY_FETCH_INTERVAL


# ───────────── Graceful shutdown ────────────────────────────────────
def _request_stop(signum, frame):
    global RUNNING, _current_task
    RUNNING = False
    log.warning("Received stop signal (%s). Cancelling current task...", signum)
    if _current_task and not _current_task.done():
        _current_task.cancel()


signal.signal(signal.SIGTERM, _request_stop)
signal.signal(signal.SIGINT, _request_stop)


# ───────────── MQTT helper ──────────────────────────────────────────
@asynccontextmanager
async def mqtt_client(host: str, port: int):
    async with MQTT(hostname=host, port=port) as client:
        log.info("Connected to MQTT %s:%s", host, port)
        yield client


async def publish_cached(mqtt: MQTT) -> bool:
    """Publish cached payload (retain=True) every publish cycle."""
    if not LAST_GOOD_PAYLOAD:
        STATUS["publish_fail_count"] += 1
        return False
    await mqtt.publish(MQTT_TOPIC, LAST_GOOD_PAYLOAD.encode(), qos=1, retain=True)
    STATUS["last_publish_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    STATUS["publish_count"] += 1
    log.info("MQTT publish OK (cached) [%d bytes, cached_at=%s]",
             len(LAST_GOOD_PAYLOAD), LAST_GOOD_TIME_STR)
    return True


# ───────────── Browser helper (fresh browser each fetch) ─────────────
@asynccontextmanager
async def fresh_browser() -> Browser:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
            ],
        )
        try:
            yield browser
        finally:
            with contextlib.suppress(Exception):
                await browser.close()


async def login_and_get_page(browser: Browser, responses: Dict[str, str]) -> Page:
    context = await browser.new_context()
    page = await context.new_page()

    page.set_default_timeout(90_000)
    page.set_default_navigation_timeout(90_000)

    async def on_response(resp):
        url = resp.url
        if "/c/" not in url:
            return
        path = urlparse(url).path
        try:
            body = await resp.text()
        except Exception:
            body = ""
        responses[path] = body

    page.on("response", on_response)

    log.info("Opening login page …")
    await page.goto("https://digital.waaree.com/login", timeout=LOGIN_TIMEOUT_MS)

    await page.wait_for_selector('input[placeholder="Username"]', timeout=LOGIN_TIMEOUT_MS)
    await page.fill('input[placeholder="Username"]', WAAREE_USERNAME)

    await page.wait_for_selector('input[placeholder="Password"]', timeout=LOGIN_TIMEOUT_MS)
    await page.fill('input[placeholder="Password"]', WAAREE_PASSWORD)

    await page.click('button:has-text("Sign In")')

    log.info("Waiting for dashboard …")
    await page.wait_for_url("**/bus/index", timeout=LOGIN_TIMEOUT_MS)
    log.info("Logged-in as %s", WAAREE_USERNAME)

    target = "https://digital.waaree.com/bus/plant/view"
    log.info("Navigating directly to %s", target)
    await page.goto(target, timeout=NAV_TIMEOUT_MS)

    await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
    log.info("Now on %s", page.url)
    return page


async def wait_for_key(responses: Dict[str, str], key: str, timeout_sec: int) -> Optional[str]:
    deadline = asyncio.get_event_loop().time() + timeout_sec
    while asyncio.get_event_loop().time() < deadline and RUNNING:
        val = responses.get(key)
        if val:
            return val
        await asyncio.sleep(0.25)
    return responses.get(key)


async def fetch_once_and_update_cache() -> bool:
    """Fresh login+fetch and update cache. Returns True if updated."""
    global LAST_GOOD_PAYLOAD, LAST_GOOD_TIME_STR, LAST_FETCH_TS

    responses: Dict[str, str] = {}

    async with fresh_browser() as browser:
        page: Optional[Page] = None
        try:
            page = await login_and_get_page(browser, responses)
            await asyncio.sleep(SETTLE_WAIT)

            body = await wait_for_key(responses, EARNINGS_KEY, EARNINGS_WAIT_SEC)
            if not body:
                log.warning("Fetch: earnings not found (waited %ss)", EARNINGS_WAIT_SEC)
                return False

            log.info("Fetch: earnings body: %s", body[:200] or "<empty>")

            try:
                payload = json.dumps(json.loads(body))
            except Exception:
                payload = body

            LAST_GOOD_PAYLOAD = payload
            LAST_GOOD_TIME_STR = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            LAST_FETCH_TS = asyncio.get_event_loop().time()

            STATUS["last_fetch_time"] = LAST_GOOD_TIME_STR
            STATUS["last_fetch_ok"] = True
            STATUS["last_fetch_error"] = None
            STATUS["fetch_count"] += 1

            log.info("Fetch OK: cache seeded/updated at %s (%d bytes)", LAST_GOOD_TIME_STR, len(payload))
            return True

        finally:
            if page is not None:
                with contextlib.suppress(Exception):
                    await page.context.close()


async def ensure_cache_seeded():
    """
    ✅ Guarantee we have a payload on first run:
    If cache is empty, fetch immediately (regardless of day/night).
    """
    global LAST_GOOD_PAYLOAD
    if LAST_GOOD_PAYLOAD:
        return

    log.warning("Cache empty on startup. Seeding cache with an immediate fetch...")
    try:
        ok = await asyncio.wait_for(fetch_once_and_update_cache(), timeout=FETCH_HARD_TIMEOUT_SEC)
        if not ok:
            log.warning("Cache seeding fetch failed; will retry on next cycle.")
    except asyncio.TimeoutError:
        log.exception("Cache seeding fetch timed out after %ss; will retry next cycle.", FETCH_HARD_TIMEOUT_SEC)
    except Exception as exc:
        log.exception("Cache seeding fetch failed: %s", exc)


async def maybe_fetch_due():
    """Fetch only if due by interval OR cache is empty."""
    interval = get_fetch_interval_seconds()
    now = asyncio.get_event_loop().time()

    if LAST_GOOD_PAYLOAD is None:
        await ensure_cache_seeded()
        return

    due = (LAST_FETCH_TS is None) or ((now - LAST_FETCH_TS) >= interval)
    if not due:
        return

    mode = "NIGHT" if is_night_time() else "DAY"
    log.info("Fetch due (mode=%s, interval=%ss). Running fresh login fetch...", mode, interval)

    try:
        await asyncio.wait_for(fetch_once_and_update_cache(), timeout=FETCH_HARD_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        STATUS["last_fetch_ok"] = False
        STATUS["last_fetch_error"] = f"Timed out after {FETCH_HARD_TIMEOUT_SEC}s"
        STATUS["fetch_fail_count"] += 1
        log.exception("Fetch timed out after %ss (using cached payload)", FETCH_HARD_TIMEOUT_SEC)
    except Exception as exc:
        STATUS["last_fetch_ok"] = False
        STATUS["last_fetch_error"] = str(exc)
        STATUS["fetch_fail_count"] += 1
        log.exception("Fetch failed: %s", exc)


# ───────────── Status Web Server ────────────────────────────────────
def _build_status_dict() -> dict:
    uptime_sec = int(time.time() - START_TIME)
    h, rem = divmod(uptime_sec, 3600)
    m, s = divmod(rem, 60)

    # time until next fetch
    next_fetch_in = None
    if LAST_FETCH_TS is not None:
        elapsed = asyncio.get_event_loop().time() - LAST_FETCH_TS
        remaining = get_fetch_interval_seconds() - elapsed
        next_fetch_in = max(0, int(remaining))

    now = datetime.now()
    return {
        "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": time.tzname[0] if time.tzname[0] else "UTC",
        "uptime": f"{h}h {m}m {s}s",
        "uptime_seconds": uptime_sec,
        "mode": "NIGHT" if is_night_time() else "DAY",
        "mqtt_connected": STATUS["mqtt_connected"],
        "mqtt_host": MQTT_HOST,
        "mqtt_port": MQTT_PORT,
        "mqtt_topic": MQTT_TOPIC,
        "mqtt_error": STATUS["mqtt_error"],
        "last_fetch_time": STATUS["last_fetch_time"],
        "last_fetch_ok": STATUS["last_fetch_ok"],
        "last_fetch_error": STATUS["last_fetch_error"],
        "fetch_count": STATUS["fetch_count"],
        "fetch_fail_count": STATUS["fetch_fail_count"],
        "fetch_interval_sec": get_fetch_interval_seconds(),
        "next_fetch_in_sec": next_fetch_in,
        "last_publish_time": STATUS["last_publish_time"],
        "publish_count": STATUS["publish_count"],
        "publish_fail_count": STATUS["publish_fail_count"],
        "publish_interval_sec": PUBLISH_INTERVAL,
        "cached_payload_bytes": len(LAST_GOOD_PAYLOAD) if LAST_GOOD_PAYLOAD else 0,
        "cached_at": LAST_GOOD_TIME_STR,
        "next_action": STATUS["next_action"],
    }


async def handle_api_status(request: web.Request) -> web.Response:
    return web.json_response(_build_status_dict())


async def handle_api_payload(request: web.Request) -> web.Response:
    if not LAST_GOOD_PAYLOAD:
        return web.json_response({"error": "no cached payload"}, status=404)
    try:
        return web.json_response(json.loads(LAST_GOOD_PAYLOAD))
    except Exception:
        return web.Response(text=LAST_GOOD_PAYLOAD, content_type="application/json")


async def handle_index(request: web.Request) -> web.Response:
    s = _build_status_dict()
    # Pretty-print cached payload for display
    cached_pretty = ""
    if LAST_GOOD_PAYLOAD:
        try:
            cached_pretty = json.dumps(json.loads(LAST_GOOD_PAYLOAD), indent=2)
        except Exception:
            cached_pretty = LAST_GOOD_PAYLOAD

    mqtt_badge = (
        '<span class="badge ok">Connected</span>'
        if s["mqtt_connected"]
        else f'<span class="badge err">Disconnected</span>'
    )
    fetch_badge = ""
    if s["last_fetch_ok"] is True:
        fetch_badge = '<span class="badge ok">OK</span>'
    elif s["last_fetch_ok"] is False:
        fetch_badge = f'<span class="badge err">FAIL</span> <small>{s["last_fetch_error"] or ""}</small>'
    else:
        fetch_badge = '<span class="badge warn">Pending</span>'

    next_fetch_str = f'{s["next_fetch_in_sec"]}s' if s["next_fetch_in_sec"] is not None else "—"

    mode_icon = "&#9728;&#65039; DAY" if s["mode"] == "DAY" else "&#9790;&#65039; NIGHT"
    day_window = f"{DAY_START_HOUR:02d}:00\u2013{NIGHT_START_HOUR:02d}:00 day window"

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Waaree Poller Status</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          background: #0f172a; color: #e2e8f0; padding: 1.5rem; max-width: 800px; margin: 0 auto; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 1rem; color: #38bdf8; }}
  .card {{ background: #1e293b; border-radius: 10px; padding: 1.2rem; margin-bottom: 1rem; }}
  .card h2 {{ font-size: 1rem; color: #94a3b8; margin-bottom: 0.8rem; text-transform: uppercase;
              letter-spacing: 0.05em; }}
  .row {{ display: flex; justify-content: space-between; padding: 0.35rem 0;
          border-bottom: 1px solid #334155; }}
  .row:last-child {{ border-bottom: none; }}
  .label {{ color: #94a3b8; }}
  .val {{ font-weight: 600; text-align: right; }}
  .badge {{ padding: 2px 10px; border-radius: 12px; font-size: 0.8rem; font-weight: 600; }}
  .badge.ok {{ background: #166534; color: #4ade80; }}
  .badge.err {{ background: #7f1d1d; color: #fca5a5; }}
  .badge.warn {{ background: #78350f; color: #fcd34d; }}
  pre {{ background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 1rem;
         overflow-x: auto; font-size: 0.8rem; max-height: 400px; overflow-y: auto; color: #a5f3fc; }}
  .mode {{ font-size: 1.1rem; }}
  .mode.DAY {{ color: #facc15; }}
  .mode.NIGHT {{ color: #818cf8; }}
  small {{ color: #64748b; }}
  .footer {{ text-align: center; margin-top: 1rem; font-size: 0.75rem; color: #475569; }}
</style>
</head><body>
<h1>&#9788; Waaree Poller Status</h1>

<div class="card">
  <h2>General</h2>
  <div class="row"><span class="label">Current Time</span><span class="val">{s['current_time']} ({s['timezone']})</span></div>
  <div class="row"><span class="label">Mode</span>
    <span class="val mode {s['mode']}">{mode_icon} <small>({day_window})</small></span></div>
  <div class="row"><span class="label">Uptime</span><span class="val">{s['uptime']}</span></div>
  <div class="row"><span class="label">Status</span><span class="val">{s['next_action']}</span></div>
</div>

<div class="card">
  <h2>MQTT</h2>
  <div class="row"><span class="label">Connection</span><span class="val">{mqtt_badge}</span></div>
  <div class="row"><span class="label">Broker</span><span class="val">{s['mqtt_host']}:{s['mqtt_port']}</span></div>
  <div class="row"><span class="label">Topic</span><span class="val">{s['mqtt_topic']}</span></div>
  <div class="row"><span class="label">Publishes</span><span class="val">{s['publish_count']} ok / {s['publish_fail_count']} fail</span></div>
  <div class="row"><span class="label">Last Published</span><span class="val">{s['last_publish_time'] or '—'}</span></div>
  <div class="row"><span class="label">Publish Interval</span><span class="val">{s['publish_interval_sec']}s</span></div>
</div>

<div class="card">
  <h2>Fetch (Waaree Scrape)</h2>
  <div class="row"><span class="label">Last Fetch</span><span class="val">{s['last_fetch_time'] or '—'} {fetch_badge}</span></div>
  <div class="row"><span class="label">Fetches</span><span class="val">{s['fetch_count']} ok / {s['fetch_fail_count']} fail</span></div>
  <div class="row"><span class="label">Fetch Interval</span><span class="val">{s['fetch_interval_sec']}s ({s['mode'].lower()} mode)</span></div>
  <div class="row"><span class="label">Next Fetch In</span><span class="val">{next_fetch_str}</span></div>
</div>

<div class="card">
  <h2>Cached Payload <small>({s['cached_payload_bytes']} bytes, cached at {s['cached_at'] or '—'})</small></h2>
  <pre>{cached_pretty or 'No data yet'}</pre>
</div>

<div class="footer">Auto-refreshes every 30s &middot; <a href="/api/status" style="color:#38bdf8">JSON API</a>
  &middot; <a href="/api/payload" style="color:#38bdf8">Raw Payload</a></div>
</body></html>"""
    return web.Response(text=html, content_type="text/html")


async def start_status_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_api_status)
    app.router.add_get("/api/payload", handle_api_payload)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", STATUS_PORT)
    await site.start()
    log.info("Status dashboard running on http://0.0.0.0:%s", STATUS_PORT)
    return runner


MQTT_INITIAL_RETRY_SEC = int(os.getenv("MQTT_INITIAL_RETRY_SEC", 10))
MQTT_MAX_RETRY_SEC = int(os.getenv("MQTT_MAX_RETRY_SEC", 300))


async def run_forever():
    global _current_task

    log.info(
        "Waaree poller starting. Publish=%ss always. Fetch: day=%ss, night=%ss. Night window %02d:00→%02d:00",
        PUBLISH_INTERVAL,
        DAY_FETCH_INTERVAL,
        NIGHT_FETCH_INTERVAL,
        NIGHT_START_HOUR,
        DAY_START_HOUR,
    )

    # Start status web server
    status_runner = await start_status_server()

    mqtt_backoff = MQTT_INITIAL_RETRY_SEC

    try:
      while RUNNING:
        try:
            STATUS["next_action"] = "connecting to MQTT"
            async with mqtt_client(MQTT_HOST, MQTT_PORT) as mqtt:
                mqtt_backoff = MQTT_INITIAL_RETRY_SEC  # reset on success
                STATUS["mqtt_connected"] = True
                STATUS["mqtt_error"] = None

                # ✅ Seed cache immediately at startup so first publish has data
                STATUS["next_action"] = "seeding cache"
                await ensure_cache_seeded()

                while RUNNING:
                    cycle_start = asyncio.get_event_loop().time()
                    mode = "NIGHT" if is_night_time() else "DAY"

                    try:
                        # 1) fetch only when due (or if cache empty)
                        STATUS["next_action"] = "checking if fetch due"
                        _current_task = asyncio.create_task(maybe_fetch_due())
                        await _current_task

                        # 2) publish every 2 minutes from cache
                        STATUS["next_action"] = "publishing to MQTT"
                        if not await publish_cached(mqtt):
                            log.warning("No cached payload available (unexpected). Will retry after seeding.")
                            await ensure_cache_seeded()
                            await publish_cached(mqtt)

                    except asyncio.CancelledError:
                        log.warning("Main loop cancelled (service stopping)")
                        return

                    except Exception as exc:
                        log.exception("Loop error: %s", exc)

                    finally:
                        _current_task = None

                    elapsed = asyncio.get_event_loop().time() - cycle_start
                    sleep_for = max(1, PUBLISH_INTERVAL - int(elapsed))
                    STATUS["next_action"] = f"sleeping {sleep_for}s (next publish)"
                    log.info("Next publish in %ss (mode=%s)", sleep_for, mode)

                    for _ in range(sleep_for):
                        if not RUNNING:
                            break
                        await asyncio.sleep(1)

        except asyncio.CancelledError:
            break

        except Exception as exc:
            STATUS["mqtt_connected"] = False
            STATUS["mqtt_error"] = str(exc)
            STATUS["next_action"] = f"MQTT retry in {mqtt_backoff}s"
            log.error("MQTT connection failed: %s. Retrying in %ss...", exc, mqtt_backoff)
            for _ in range(mqtt_backoff):
                if not RUNNING:
                    break
                await asyncio.sleep(1)
            mqtt_backoff = min(mqtt_backoff * 2, MQTT_MAX_RETRY_SEC)

    finally:
        await status_runner.cleanup()

    log.warning("Poller stopped cleanly.")


def main():
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        log.warning("Interrupted – exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
