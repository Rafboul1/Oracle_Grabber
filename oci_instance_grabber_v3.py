#!/usr/bin/env python3
"""
OCI Free Tier Instance Grabber — v3 Optimized (durable & secure)
==================================================================
Changes compared to v2:
  • Empirically calibrated timing: random 90-120s on OOC
      Oracle LaunchInstance threshold ≈ 120s/tenant (V1 real data: ~1/10 of 429 at 60-120s)
      90-120s: sweet spot — +30s lower margin, ~1/20 of 429 expected in stable prod
  • Exponential backoff on 429: 60 → 120 → 240 → 600s, reset after 2 consecutive successes
  • LaunchInstanceDetails built ONCE (reused, only fault_domain updated)
  • Explicit retry_strategy=NoneRetryStrategy() (zero hidden SDK retry)
  • HTTP /status on 127.0.0.1 only (access via SSH tunnel: ssh -L 8080:localhost:8080 user@vps)

Optimizations kept from v2:
  • Small Foot: 1 OCPU / 6 GB hardcoded
  • FD-1 → FD-2 → FD-3 rotation
  • HTTP Keep-Alive session
  • stdout-only logs (zero disk I/O)
  • Async Telegram on success / timeout

Usage:
    pip install oci requests
    python oci_instance_grabber_v3.py
    # To monitor from your local machine:
    ssh -L 8080:localhost:8080 user@your-vps
    curl http://localhost:8080/status
"""

import base64
import json
import logging
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from itertools import cycle
from pathlib import Path

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    print("Error: 'requests' is not installed. Run: pip install requests")
    sys.exit(1)

try:
    import oci
    from oci.retry import NoneRetryStrategy
except ImportError:
    print("Error: The OCI SDK is not installed. Run: pip install oci")
    sys.exit(1)


# ─── Defaults (override via config.json) ─────────────────────────────────────
# "Foot in the Door" strategy: starting small (1/6) maximizes the odds in a
# saturated region; a PAYG tenant can attempt 2/12 or 4/24 directly.
DEFAULT_OCPUS: float = 1.0
DEFAULT_MEMORY_GB: float = 6.0

# Sequential rotation of Fault Domains
FAULT_DOMAINS = cycle(["FAULT-DOMAIN-1", "FAULT-DOMAIN-2", "FAULT-DOMAIN-3"])

# OOC (Out of Capacity) timing: random sleep between attempts.
# Empirically calibrated: Oracle LaunchInstance threshold ≈ 120s / tenant
# (no Retry-After in the headers). 90-120s → ~1/20 of 429 expected.
DEFAULT_OOC_MIN_SLEEP_SEC: int = 90
DEFAULT_OOC_MAX_SLEEP_SEC: int = 120

# Exponential backoff on 429 (Oracle rate limit)
DEFAULT_RATE_LIMIT_INITIAL_BACKOFF_SEC: int = 60
DEFAULT_RATE_LIMIT_MAX_BACKOFF_SEC: int = 600
RATE_LIMIT_BACKOFF_MULTIPLIER: int = 2

# Fix A: reset the backoff only after N non-429 responses in a row
# → avoids the "429 → reset → 429" yo-yo observed in the initial v3
NON_429_STREAK_TO_RESET: int = 2

# HTTP monitoring — bind localhost only (security)
STATUS_HTTP_HOST: str = "127.0.0.1"
STATUS_HTTP_PORT: int = 8080


# ─── Shared state (read by the HTTP thread, written by the main loop) ────────
_stats: dict = {
    "status": "starting",
    "attempt": 0,
    "uptime": "0:00:00",
    "start_time": datetime.now().isoformat(),
    "last_fault_domain": "—",
    "last_error": None,
    "rate_limited_count": 0,
    "current_backoff_sec": DEFAULT_RATE_LIMIT_INITIAL_BACKOFF_SEC,
    "result": None,
}


# ─── Logging (stdout only) ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("oci-grabber")


# ─── Config ──────────────────────────────────────────────────────────────────
def load_config(path: str = "config.json") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        log.error(f"Config file not found: {path}")
        log.error("Make sure config.json is in the same folder as this script.")
        sys.exit(1)
    with open(config_path, "r") as f:
        return json.load(f)


# ─── HTTP monitoring (localhost only) ────────────────────────────────────────
class StatusHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler — exposes /status as JSON."""

    def do_GET(self) -> None:
        if self.path != "/status":
            self.send_response(404)
            self.end_headers()
            return

        payload = json.dumps(_stats, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        # Silent: don't pollute the main log
        pass


def start_status_server(host: str, port: int) -> None:
    """Start the monitoring HTTP server in a daemon thread."""
    try:
        server = HTTPServer((host, port), StatusHandler)
        log.info(f"HTTP monitoring : http://{host}:{port}/status (localhost only)")
        log.info(f"   SSH tunnel : ssh -L {port}:localhost:{port} user@your-vps")
        server.serve_forever()
    except OSError as e:
        log.warning(f"Could not start HTTP monitoring: {e}")


# ─── Telegram ────────────────────────────────────────────────────────────────
def send_telegram(bot_token: str, chat_id: str, message: str, async_send: bool = False) -> None:
    def _send() -> None:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                log.info("Telegram notification sent.")
            else:
                log.warning(f"Telegram code {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            log.warning(f"Telegram error: {e}")

    if async_send:
        threading.Thread(target=_send, daemon=True).start()
    else:
        _send()


# ─── HTTP Keep-Alive session ──────────────────────────────────────────────────
def make_keepalive_session() -> requests.Session:
    """requests Session with Keep-Alive — reuses the same TCP/TLS connection."""
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=1,
        pool_maxsize=1,
        max_retries=0,
    )
    session.mount("https://", adapter)
    return session


# ─── OCI ─────────────────────────────────────────────────────────────────────
def get_availability_domain(identity_client, compartment_id: str, ad_name: str) -> str:
    """Retrieve the full name of the Availability Domain."""
    ads = identity_client.list_availability_domains(compartment_id).data
    for ad in ads:
        if ad_name in ad.name:
            return ad.name
    if ads:
        log.warning(f"AD '{ad_name}' not found, using: {ads[0].name}")
        return ads[0].name
    log.error("No Availability Domain found.")
    sys.exit(1)


def build_launch_details(config: dict, ad_full_name: str) -> oci.core.models.LaunchInstanceDetails:
    """
    Build the LaunchInstanceDetails object ONCE (optimization B).
    The fault_domain will be updated each iteration via direct attribute set.
    """
    oci_conf = config["oci"]
    ocpus = float(oci_conf.get("ocpus", DEFAULT_OCPUS))
    memory_gb = float(oci_conf.get("memory_in_gbs", DEFAULT_MEMORY_GB))

    metadata = {"ssh_authorized_keys": oci_conf["ssh_public_key"]}

    # Cloud-init support (user_data)
    user_data = None
    if oci_conf.get("user_data_file"):
        ud_path = Path(oci_conf["user_data_file"])
        if ud_path.exists():
            with open(ud_path, "r", encoding="utf-8") as f:
                user_data = f.read()
            log.info(f"Cloud-init: script loaded from {ud_path}")
        else:
            log.error(f"user_data_file not found: {ud_path}")
            sys.exit(1)
    elif oci_conf.get("user_data"):
        user_data = oci_conf["user_data"]
        log.info("Cloud-init: inline script loaded from config")

    if user_data:
        metadata["user_data"] = base64.b64encode(user_data.encode("utf-8")).decode("utf-8")

    return oci.core.models.LaunchInstanceDetails(
        compartment_id=oci_conf["compartment_id"],
        availability_domain=ad_full_name,
        fault_domain="FAULT-DOMAIN-1",  # placeholder, replaced each iteration
        display_name=oci_conf["instance_display_name"],
        shape=oci_conf["shape"],
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=ocpus,
            memory_in_gbs=memory_gb,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=oci_conf["image_id"],
            boot_volume_size_in_gbs=oci_conf.get("boot_volume_size_in_gbs", 50),
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=oci_conf["subnet_id"],
            assign_public_ip=True,
        ),
        metadata=metadata,
    )


def try_create_instance(
    compute_client,
    instance_details,
    fault_domain,
    no_retry_strategy,
):
    # Note: signature without modern type hints for Python 3.9 compat (Oracle Linux 9 Micro)
    # Returns (dict | None, bool)  — (result, rate_limited)
    """
    Attempt to create an OCI instance.

    Returns (result, rate_limited):
      - result != None    → success
      - rate_limited=True → 429 error → exponential backoff
      - (None, False)     → OOC or recoverable error → short sleep (15-25s)
    """
    # Optimization B: just mutate fault_domain instead of rebuilding everything
    instance_details.fault_domain = fault_domain

    try:
        # Optimization D: explicit retry_strategy — zero hidden SDK retry
        response = compute_client.launch_instance(
            instance_details,
            retry_strategy=no_retry_strategy,
        )
        return {
            "id": response.data.id,
            "display_name": response.data.display_name,
            "lifecycle_state": response.data.lifecycle_state,
            "time_created": str(response.data.time_created),
        }, False

    except oci.exceptions.ServiceError as e:
        msg = str(e.message)

        if e.status == 500 and ("Out of host capacity" in msg or "Out of capacity" in msg):
            _stats["last_error"] = "Out of capacity"
            return None, False

        elif e.status == 429:
            # TooManyRequests: Oracle limits to ~1 processed req / 120s / tenant
            # No Retry-After in the headers — handled with exponential backoff
            log.warning(f"Rate limited (429) — Too many requests for the tenant")
            _stats["last_error"] = "429 TooManyRequests"
            _stats["rate_limited_count"] = _stats["rate_limited_count"] + 1
            return None, True

        elif "LimitExceeded" in str(e.code):
            log.error(f"Limit reached: {msg}")
            log.error("You may already have an active instance using your quota.")
            _stats["status"] = "fatal_limit_exceeded"
            sys.exit(1)

        elif "NotAuthorized" in str(e.code) or e.status == 401:
            log.error(f"Authentication error: {msg}")
            log.error("Check your API key and your ~/.oci/config file")
            _stats["status"] = "fatal_auth_error"
            sys.exit(1)

        else:
            log.error(f"Unexpected OCI error (status={e.status}, code={e.code}): {msg}")
            _stats["last_error"] = f"OCI {e.status}/{e.code}: {msg[:80]}"
            return None, False

    except Exception as e:
        log.error(f"Unexpected error: {e}")
        _stats["last_error"] = str(e)[:120]
        return None, False


# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    config = load_config()
    retry_conf = config.get("retry", {})
    telegram_conf = config.get("telegram", {})
    oci_conf = config["oci"]

    # Configurable values (with defaults if absent)
    ocpus = float(oci_conf.get("ocpus", DEFAULT_OCPUS))
    memory_gb = float(oci_conf.get("memory_in_gbs", DEFAULT_MEMORY_GB))
    ooc_min = int(retry_conf.get("min_interval_seconds", DEFAULT_OOC_MIN_SLEEP_SEC))
    ooc_max = int(retry_conf.get("max_interval_seconds", DEFAULT_OOC_MAX_SLEEP_SEC))
    backoff_initial = int(retry_conf.get("rate_limit_initial_backoff_seconds", DEFAULT_RATE_LIMIT_INITIAL_BACKOFF_SEC))
    backoff_max = int(retry_conf.get("rate_limit_max_backoff_seconds", DEFAULT_RATE_LIMIT_MAX_BACKOFF_SEC))
    max_duration_hours = int(retry_conf.get("max_duration_hours", 96))
    telegram_enabled = bool(telegram_conf.get("enabled", True)) and bool(telegram_conf.get("bot_token")) and bool(telegram_conf.get("chat_id"))

    # Start the HTTP monitoring in a daemon thread
    threading.Thread(
        target=start_status_server,
        args=(STATUS_HTTP_HOST, STATUS_HTTP_PORT),
        daemon=True,
    ).start()

    log.info("=" * 60)
    log.info("OCI Instance Grabber — v3 (durable + secure)")
    log.info("=" * 60)
    log.info(f"Shape          : {oci_conf['shape']}")
    log.info(f"OCPUs          : {ocpus}")
    log.info(f"RAM            : {memory_gb} GB")
    log.info(f"FD Rotation    : FD-1 → FD-2 → FD-3 → ...")
    log.info(f"Sleep OOC      : {ooc_min}-{ooc_max}s (calibrated: Oracle threshold ~120s)")
    log.info(f"Backoff 429    : {backoff_initial}s → ×{RATE_LIMIT_BACKOFF_MULTIPLIER} (max {backoff_max}s, reset after {NON_429_STREAK_TO_RESET} successes)")
    log.info(f"Max duration   : {max_duration_hours}h")
    log.info(f"Retry strategy : NoneRetryStrategy (explicit)")
    log.info(f"Telegram       : {'enabled' if telegram_enabled else 'disabled'}")
    log.info("=" * 60)

    # Startup notification (synchronous, outside the loop)
    if telegram_enabled:
        send_telegram(
            telegram_conf["bot_token"],
            telegram_conf["chat_id"],
            "<b>OCI Grabber v3 started</b>\n\n"
            f"Shape: {oci_conf['shape']}\n"
            f"OCPUs: {ocpus} | RAM: {memory_gb} GB\n"
            f"Strategy: FD Rotation\n"
            f"Sleep: {ooc_min}-{ooc_max}s | Backoff 429 expo (max {backoff_max}s)\n\n"
            "You'll be notified when an instance is created.",
        )

    # Init OCI SDK with Keep-Alive session
    log.info("Init OCI SDK (Keep-Alive session enabled)...")
    oci_config = oci.config.from_file(
        config["oci"]["config_file_path"],
        config["oci"]["config_profile"],
    )
    oci.config.validate_config(oci_config)

    keepalive_session = make_keepalive_session()
    identity_client = oci.identity.IdentityClient(
        oci_config, requests_session=keepalive_session
    )
    compute_client = oci.core.ComputeClient(
        oci_config, requests_session=keepalive_session
    )

    # Explicit no-retry strategy (optimization D)
    no_retry_strategy = NoneRetryStrategy()

    ad_full_name = get_availability_domain(
        identity_client,
        config["oci"]["compartment_id"],
        config["oci"]["availability_domain"],
    )
    log.info(f"Availability Domain : {ad_full_name}")

    # Optimization B: build LaunchInstanceDetails ONCE
    instance_details = build_launch_details(config, ad_full_name)
    log.info("LaunchInstanceDetails pre-built (reused every iteration)")

    # Main loop
    start_time = datetime.now()
    max_duration = timedelta(hours=max_duration_hours)
    attempt = 0
    current_backoff = backoff_initial
    non_429_streak = 0  # Fix A: counter of consecutive non-429 successes

    _stats["status"] = "running"
    _stats["start_time"] = start_time.isoformat()
    _stats["current_backoff_sec"] = current_backoff

    log.info("")
    log.info("Starting attempts...")
    log.info("")

    while datetime.now() - start_time < max_duration:
        attempt += 1
        fd = next(FAULT_DOMAINS)
        elapsed = datetime.now() - start_time
        elapsed_str = str(elapsed).split(".")[0]

        _stats["attempt"] = attempt
        _stats["uptime"] = elapsed_str
        _stats["last_fault_domain"] = fd

        log.info(f"#{attempt} │ {fd} │ {elapsed_str}")

        result, rate_limited = try_create_instance(
            compute_client, instance_details, fd, no_retry_strategy
        )

        if result is not None:
            elapsed_str = str(datetime.now() - start_time).split(".")[0]
            _stats["status"] = "success"
            _stats["result"] = result
            _stats["uptime"] = elapsed_str
            _stats["last_error"] = None

            log.info("")
            log.info("=" * 60)
            log.info("INSTANCE CREATED SUCCESSFULLY.")
            log.info("=" * 60)
            log.info(f"ID         : {result['id']}")
            log.info(f"Name       : {result['display_name']}")
            log.info(f"State      : {result['lifecycle_state']}")
            log.info(f"Created at : {result['time_created']}")
            log.info(f"Attempts   : {attempt}")
            log.info(f"Duration   : {elapsed_str}")
            log.info("=" * 60)

            with open("instance_details.json", "w") as f:
                json.dump(result, f, indent=2)
            log.info("Details saved to instance_details.json")

            if telegram_enabled:
                send_telegram(
                    telegram_conf["bot_token"],
                    telegram_conf["chat_id"],
                    "<b>OCI INSTANCE CREATED</b>\n\n"
                    f"Name: {result['display_name']}\n"
                    f"ID: <code>{result['id']}</code>\n"
                    f"State: {result['lifecycle_state']}\n"
                    f"Created at: {result['time_created']}\n"
                    f"Attempts: {attempt}\n"
                    f"Total duration: {elapsed_str}\n\n"
                    "Connect via SSH to configure the instance.",
                )
            return

        if rate_limited:
            # Exponential backoff on 429
            non_429_streak = 0  # Fix A: reset to zero as soon as we hit a 429
            log.warning(f"   Backoff 429 : {current_backoff}s")
            _stats["current_backoff_sec"] = current_backoff
            time.sleep(current_backoff)
            current_backoff = min(
                current_backoff * RATE_LIMIT_BACKOFF_MULTIPLIER,
                backoff_max,
            )
        else:
            # OOC or recoverable error: short random sleep
            non_429_streak += 1  # Fix A: increment the streak
            sleep_sec = random.randint(ooc_min, ooc_max)
            log.info(f"   Next attempt in {sleep_sec}s... (non-429 streak: {non_429_streak}/{NON_429_STREAK_TO_RESET})")
            time.sleep(sleep_sec)

            # Fix A: reset the backoff ONLY after NON_429_STREAK_TO_RESET successes in a row
            if (
                current_backoff != backoff_initial
                and non_429_streak >= NON_429_STREAK_TO_RESET
            ):
                log.info(f"   Backoff 429 reset to {backoff_initial}s (after {non_429_streak} successes)")
                current_backoff = backoff_initial
                _stats["current_backoff_sec"] = current_backoff

    # ── Timeout reached ──────────────────────────────────────────────────────
    elapsed_str = str(datetime.now() - start_time).split(".")[0]
    _stats["status"] = "timeout"
    _stats["uptime"] = elapsed_str

    log.warning("")
    log.warning("=" * 60)
    log.warning(f"Max duration reached ({max_duration_hours}h)")
    log.warning(f"   Attempts        : {attempt}")
    log.warning(f"   Rate limits 429 : {_stats['rate_limited_count']}")
    log.warning(f"   Total duration  : {elapsed_str}")
    log.warning("=" * 60)

    if telegram_enabled:
        send_telegram(
            telegram_conf["bot_token"],
            telegram_conf["chat_id"],
            f"<b>OCI Grabber stopped</b> (max duration reached)\n\n"
            f"Attempts: {attempt}\n"
            f"Rate limits 429: {_stats['rate_limited_count']}\n"
            f"Duration: {elapsed_str}\n\n"
            "Restart the script to continue."
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _stats["status"] = "stopped"
        log.info("\nManual stop (Ctrl+C).")
        sys.exit(0)
