#!/usr/bin/env python3
"""
OCI Free Tier Instance Grabber — v3 Optimisé (durable & sécurisé)
==================================================================
Évolutions par rapport à la v2 :
  • Timing calibré empiriquement : 90-120s aléatoire sur OOC
      Seuil Oracle LaunchInstance ≈ 120s/tenant (V1 données réelles : ~1/10 de 429 à 60-120s)
      90-120s : juste milieu — marge +30s basse, ~1/20 de 429 attendu en prod stable
  • Backoff exponentiel sur 429 : 60 → 120 → 240 → 600s, reset après 2 succès consécutifs
  • LaunchInstanceDetails construit UNE seule fois (réutilisé, juste fault_domain mis à jour)
  • retry_strategy=NoneRetryStrategy() explicite (zero retry caché du SDK)
  • HTTP /status sur 127.0.0.1 uniquement (accès via SSH tunnel : ssh -L 8080:localhost:8080 user@vps)

Optimisations conservées de la v2 :
  • Petit Pied : 1 OCPU / 6 Go hardcodés
  • Rotation FD-1 → FD-2 → FD-3
  • Session HTTP Keep-Alive
  • Logs stdout uniquement (zero I/O disque)
  • Telegram async sur succès / timeout

Usage :
    pip install oci requests
    python oci_instance_grabber_v3.py
    # Pour le monitoring depuis ta machine locale :
    ssh -L 8080:localhost:8080 user@ton-vps
    curl http://localhost:8080/status
"""

import json
import logging
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
    print("Erreur : 'requests' n'est pas installé. Lancez : pip install requests")
    sys.exit(1)

try:
    import oci
    from oci.retry import NoneRetryStrategy
except ImportError:
    print("Erreur : Le SDK OCI n'est pas installé. Lancez : pip install oci")
    sys.exit(1)


# ─── Defaults (surcharge via config.json) ────────────────────────────────────
# Stratégie "Petit Pied dans la Porte" : commencer petit (1/6) maximise les
# chances en région saturée ; un tenant PAYG peut tenter 2/12 ou 4/24 direct.
DEFAULT_OCPUS: float = 1.0
DEFAULT_MEMORY_GB: float = 6.0

# Rotation séquentielle des Fault Domains
FAULT_DOMAINS = cycle(["FAULT-DOMAIN-1", "FAULT-DOMAIN-2", "FAULT-DOMAIN-3"])

# Timing OOC (Out of Capacity) : sleep aléatoire entre tentatives.
# Calibré empiriquement : seuil Oracle LaunchInstance ≈ 120s / tenant
# (aucun Retry-After dans les headers). 90-120s → ~1/20 de 429 attendu.
DEFAULT_OOC_MIN_SLEEP_SEC: int = 90
DEFAULT_OOC_MAX_SLEEP_SEC: int = 120

# Backoff exponentiel sur 429 (rate limit Oracle)
DEFAULT_RATE_LIMIT_INITIAL_BACKOFF_SEC: int = 60
DEFAULT_RATE_LIMIT_MAX_BACKOFF_SEC: int = 600
RATE_LIMIT_BACKOFF_MULTIPLIER: int = 2

# Fix A : reset du backoff seulement après N réponses non-429 d'affilée
# → évite le yo-yo "429 → reset → 429" observé en v3 initial
NON_429_STREAK_TO_RESET: int = 2

# Monitoring HTTP — bind localhost uniquement (sécurité)
STATUS_HTTP_HOST: str = "127.0.0.1"
STATUS_HTTP_PORT: int = 8080


# ─── État partagé (lu par le thread HTTP, écrit par la boucle principale) ────
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


# ─── Logging (stdout uniquement) ─────────────────────────────────────────────
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
        log.error(f"Fichier de config introuvable : {path}")
        log.error("Assure-toi que config.json est dans le même dossier que ce script.")
        sys.exit(1)
    with open(config_path, "r") as f:
        return json.load(f)


# ─── Monitoring HTTP (localhost only) ────────────────────────────────────────
class StatusHandler(BaseHTTPRequestHandler):
    """Handler HTTP minimaliste — expose /status en JSON."""

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
        # Silencieux : on ne pollue pas le log principal
        pass


def start_status_server(host: str, port: int) -> None:
    """Lance le serveur HTTP de monitoring dans un thread daemon."""
    try:
        server = HTTPServer((host, port), StatusHandler)
        log.info(f"Monitoring HTTP : http://{host}:{port}/status (localhost only)")
        log.info(f"   SSH tunnel : ssh -L {port}:localhost:{port} user@ton-vps")
        server.serve_forever()
    except OSError as e:
        log.warning(f"Impossible de démarrer le monitoring HTTP : {e}")


# ─── Telegram ────────────────────────────────────────────────────────────────
def send_telegram(bot_token: str, chat_id: str, message: str, async_send: bool = False) -> None:
    def _send() -> None:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                log.info("Notification Telegram envoyée.")
            else:
                log.warning(f"Telegram code {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            log.warning(f"Erreur Telegram : {e}")

    if async_send:
        threading.Thread(target=_send, daemon=True).start()
    else:
        _send()


# ─── Session HTTP Keep-Alive ──────────────────────────────────────────────────
def make_keepalive_session() -> requests.Session:
    """Session requests avec Keep-Alive — réutilise la même connexion TCP/TLS."""
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
    """Récupère le nom complet de l'Availability Domain."""
    ads = identity_client.list_availability_domains(compartment_id).data
    for ad in ads:
        if ad_name in ad.name:
            return ad.name
    if ads:
        log.warning(f"AD '{ad_name}' non trouvé, utilisation de : {ads[0].name}")
        return ads[0].name
    log.error("Aucun Availability Domain trouvé.")
    sys.exit(1)


def build_launch_details(config: dict, ad_full_name: str) -> oci.core.models.LaunchInstanceDetails:
    """
    Construit l'objet LaunchInstanceDetails UNE SEULE FOIS (optimisation B).
    Le fault_domain sera mis à jour à chaque itération via attribut direct.
    """
    oci_conf = config["oci"]
    ocpus = float(oci_conf.get("ocpus", DEFAULT_OCPUS))
    memory_gb = float(oci_conf.get("memory_in_gbs", DEFAULT_MEMORY_GB))
    return oci.core.models.LaunchInstanceDetails(
        compartment_id=oci_conf["compartment_id"],
        availability_domain=ad_full_name,
        fault_domain="FAULT-DOMAIN-1",  # placeholder, sera remplacé à chaque iter
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
        metadata={"ssh_authorized_keys": oci_conf["ssh_public_key"]},
    )


def try_create_instance(
    compute_client,
    instance_details,
    fault_domain,
    no_retry_strategy,
):
    # Note: signature sans type hints modernes pour compat Python 3.9 (Micro Oracle Linux 9)
    # Retourne (dict | None, bool)  — (résultat, rate_limited)
    """
    Tente de créer une instance OCI.

    Retourne (résultat, rate_limited) :
      - résultat != None  → succès
      - rate_limited=True → erreur 429 → backoff exponentiel
      - (None, False)     → OOC ou erreur récupérable → sleep court (15-25s)
    """
    # Optimisation B : on mute juste fault_domain au lieu de tout reconstruire
    instance_details.fault_domain = fault_domain

    try:
        # Optimisation D : retry_strategy explicite — zero retry caché du SDK
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
            # TooManyRequests : Oracle limite à ~1 req processée / 120s / tenant
            # Pas de Retry-After dans les headers — on gère avec backoff exponentiel
            log.warning(f"Rate limited (429) — Too many requests for the tenant")
            _stats["last_error"] = "429 TooManyRequests"
            _stats["rate_limited_count"] = _stats["rate_limited_count"] + 1
            return None, True

        elif "LimitExceeded" in str(e.code):
            log.error(f"Limite atteinte : {msg}")
            log.error("Vous avez peut-être déjà une instance active qui utilise votre quota.")
            _stats["status"] = "fatal_limit_exceeded"
            sys.exit(1)

        elif "NotAuthorized" in str(e.code) or e.status == 401:
            log.error(f"Erreur d'authentification : {msg}")
            log.error("Vérifiez votre clé API et votre fichier ~/.oci/config")
            _stats["status"] = "fatal_auth_error"
            sys.exit(1)

        else:
            log.error(f"Erreur OCI inattendue (status={e.status}, code={e.code}): {msg}")
            _stats["last_error"] = f"OCI {e.status}/{e.code}: {msg[:80]}"
            return None, False

    except Exception as e:
        log.error(f"Erreur inattendue : {e}")
        _stats["last_error"] = str(e)[:120]
        return None, False


# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    config = load_config()
    retry_conf = config.get("retry", {})
    telegram_conf = config.get("telegram", {})
    oci_conf = config["oci"]

    # Valeurs configurables (avec defaults si absentes)
    ocpus = float(oci_conf.get("ocpus", DEFAULT_OCPUS))
    memory_gb = float(oci_conf.get("memory_in_gbs", DEFAULT_MEMORY_GB))
    ooc_min = int(retry_conf.get("min_interval_seconds", DEFAULT_OOC_MIN_SLEEP_SEC))
    ooc_max = int(retry_conf.get("max_interval_seconds", DEFAULT_OOC_MAX_SLEEP_SEC))
    backoff_initial = int(retry_conf.get("rate_limit_initial_backoff_seconds", DEFAULT_RATE_LIMIT_INITIAL_BACKOFF_SEC))
    backoff_max = int(retry_conf.get("rate_limit_max_backoff_seconds", DEFAULT_RATE_LIMIT_MAX_BACKOFF_SEC))
    max_duration_hours = int(retry_conf.get("max_duration_hours", 96))
    telegram_enabled = bool(telegram_conf.get("enabled", True)) and bool(telegram_conf.get("bot_token")) and bool(telegram_conf.get("chat_id"))

    # Démarrage du monitoring HTTP en thread daemon
    threading.Thread(
        target=start_status_server,
        args=(STATUS_HTTP_HOST, STATUS_HTTP_PORT),
        daemon=True,
    ).start()

    log.info("=" * 60)
    log.info("OCI Instance Grabber — v3 (durable + sécurisé)")
    log.info("=" * 60)
    log.info(f"Shape          : {oci_conf['shape']}")
    log.info(f"OCPUs          : {ocpus}")
    log.info(f"RAM            : {memory_gb} Go")
    log.info(f"FD Rotation    : FD-1 → FD-2 → FD-3 → ...")
    log.info(f"Sleep OOC      : {ooc_min}-{ooc_max}s (calibré : seuil Oracle ~120s)")
    log.info(f"Backoff 429    : {backoff_initial}s → ×{RATE_LIMIT_BACKOFF_MULTIPLIER} (max {backoff_max}s, reset après {NON_429_STREAK_TO_RESET} succès)")
    log.info(f"Durée max      : {max_duration_hours}h")
    log.info(f"Retry strategy : NoneRetryStrategy (explicite)")
    log.info(f"Telegram       : {'activé' if telegram_enabled else 'désactivé'}")
    log.info("=" * 60)

    # Notification de démarrage (synchrone, hors boucle)
    if telegram_enabled:
        send_telegram(
            telegram_conf["bot_token"],
            telegram_conf["chat_id"],
            "<b>OCI Grabber v3 démarré</b>\n\n"
            f"Shape: {oci_conf['shape']}\n"
            f"OCPUs: {ocpus} | RAM: {memory_gb} Go\n"
            f"Stratégie: FD Rotation\n"
            f"Sleep: {ooc_min}-{ooc_max}s | Backoff 429 expo (max {backoff_max}s)\n\n"
            "Notification prévue lors de la création d'une instance.",
        )

    # Init OCI SDK avec session Keep-Alive
    log.info("Init OCI SDK (session Keep-Alive activée)...")
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

    # Stratégie de non-retry explicite (optimisation D)
    no_retry_strategy = NoneRetryStrategy()

    ad_full_name = get_availability_domain(
        identity_client,
        config["oci"]["compartment_id"],
        config["oci"]["availability_domain"],
    )
    log.info(f"Availability Domain : {ad_full_name}")

    # Optimisation B : on construit LaunchInstanceDetails UNE SEULE FOIS
    instance_details = build_launch_details(config, ad_full_name)
    log.info("LaunchInstanceDetails pré-construit (réutilisé à chaque iter)")

    # Boucle principale
    start_time = datetime.now()
    max_duration = timedelta(hours=max_duration_hours)
    attempt = 0
    current_backoff = backoff_initial
    non_429_streak = 0  # Fix A : compteur de succès non-429 consécutifs

    _stats["status"] = "running"
    _stats["start_time"] = start_time.isoformat()
    _stats["current_backoff_sec"] = current_backoff

    log.info("")
    log.info("Début des tentatives...")
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
            log.info("INSTANCE CRÉÉE AVEC SUCCÈS.")
            log.info("=" * 60)
            log.info(f"ID         : {result['id']}")
            log.info(f"Nom        : {result['display_name']}")
            log.info(f"État       : {result['lifecycle_state']}")
            log.info(f"Créée à    : {result['time_created']}")
            log.info(f"Tentatives : {attempt}")
            log.info(f"Durée      : {elapsed_str}")
            log.info("=" * 60)

            with open("instance_details.json", "w") as f:
                json.dump(result, f, indent=2)
            log.info("Détails sauvegardés dans instance_details.json")

            if telegram_enabled:
                send_telegram(
                    telegram_conf["bot_token"],
                    telegram_conf["chat_id"],
                    "<b>INSTANCE OCI CRÉÉE</b>\n\n"
                    f"Nom: {result['display_name']}\n"
                    f"ID: <code>{result['id']}</code>\n"
                    f"État: {result['lifecycle_state']}\n"
                    f"Créée à: {result['time_created']}\n"
                    f"Tentatives: {attempt}\n"
                    f"Durée totale: {elapsed_str}\n\n"
                    "Connectez-vous via SSH pour configurer l'instance.",
                )
            return

        if rate_limited:
            # Backoff exponentiel sur 429
            non_429_streak = 0  # Fix A : on repart à zéro dès qu'on prend un 429
            log.warning(f"   Backoff 429 : {current_backoff}s")
            _stats["current_backoff_sec"] = current_backoff
            time.sleep(current_backoff)
            current_backoff = min(
                current_backoff * RATE_LIMIT_BACKOFF_MULTIPLIER,
                backoff_max,
            )
        else:
            # OOC ou erreur récupérable : sleep court aléatoire
            non_429_streak += 1  # Fix A : on incrémente le streak
            sleep_sec = random.randint(ooc_min, ooc_max)
            log.info(f"   Prochaine tentative dans {sleep_sec}s... (streak non-429 : {non_429_streak}/{NON_429_STREAK_TO_RESET})")
            time.sleep(sleep_sec)

            # Fix A : reset du backoff SEULEMENT après NON_429_STREAK_TO_RESET succès d'affilée
            if (
                current_backoff != backoff_initial
                and non_429_streak >= NON_429_STREAK_TO_RESET
            ):
                log.info(f"   Backoff 429 reset à {backoff_initial}s (après {non_429_streak} succès)")
                current_backoff = backoff_initial
                _stats["current_backoff_sec"] = current_backoff

    # ── Timeout atteint ──────────────────────────────────────────────────────
    elapsed_str = str(datetime.now() - start_time).split(".")[0]
    _stats["status"] = "timeout"
    _stats["uptime"] = elapsed_str

    log.warning("")
    log.warning("=" * 60)
    log.warning(f"Durée max atteinte ({max_duration_hours}h)")
    log.warning(f"   Tentatives      : {attempt}")
    log.warning(f"   Rate limits 429 : {_stats['rate_limited_count']}")
    log.warning(f"   Durée totale    : {elapsed_str}")
    log.warning("=" * 60)

    if telegram_enabled:
        send_telegram(
            telegram_conf["bot_token"],
            telegram_conf["chat_id"],
            f"<b>OCI Grabber arrêté</b> (durée max atteinte)\n\n"
            f"Tentatives: {attempt}\n"
            f"Rate limits 429: {_stats['rate_limited_count']}\n"
            f"Durée: {elapsed_str}\n\n"
            "Relancez le script pour continuer."
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _stats["status"] = "stopped"
        log.info("\nArrêt manuel (Ctrl+C).")
        sys.exit(0)
