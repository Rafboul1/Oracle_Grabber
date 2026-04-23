# Oracle Cloud Free Tier — ARM Instance Grabber

Script Python pour obtenir une instance ARM Ampere A1 (VM.Standard.A1.Flex) sur Oracle Cloud dans une région saturée, où `LaunchInstance` retourne en boucle `Out of host capacity`.

Il tente la création en continu avec un timing calibré sur le rate limit Oracle, une rotation des Fault Domains, et un backoff exponentiel sur 429.

## Pourquoi

Les ressources ARM gratuites (4 OCPU / 24 Go) d'Oracle sont très demandées. Dans plusieurs régions (Paris, Francfort, Londres...), la capacité est épuisée et il faut re-tenter pendant des heures/jours. Ce script automatise ça proprement :

- **Stratégie "petit pied"** : commencer par 1 OCPU / 6 Go (configurable) maximise les chances ; il est possible de redimensionner plus tard.
- **Rotation FD-1 → FD-2 → FD-3** : chaque Fault Domain a son propre pool — en essayer 3 triple les chances.
- **Timing calibré** : Oracle limite `LaunchInstance` à ~1 requête / 120s / tenant. Le script envoie une tentative toutes les 90-120s pour rester sous le radar.
- **Backoff 429 exponentiel** : 60s → 120s → 240s → 600s, reset après 2 succès d'affilée.
- **Notifications Telegram** (optionnel) : démarrage, succès, timeout.
- **Monitoring HTTP local** : `GET /status` en JSON sur `127.0.0.1:8080`, accessible via SSH tunnel.

## Prérequis

- Un compte Oracle Cloud (Free Tier ou Pay As You Go — **PAYG a une meilleure priorité** sur les ARM).
- Une VM déjà active dans le tenant (micro x86 free tier = parfait) pour faire tourner le script 24/7.
- Une clé API OCI configurée dans `~/.oci/config` ([doc officielle](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm)).
- Python 3.9+ sur la VM.
- (Optionnel) Un bot Telegram — créé via [@BotFather](https://t.me/BotFather).

## Installation

```bash
git clone https://github.com/YOUR_USER/oracle-grabber.git
cd oracle-grabber
pip install -r requirements.txt
cp config.example.json config.json
```

Éditez `config.json` avec les valeurs :

- `oci.compartment_id` — OCID de le compartment (tenancy OCID fonctionne)
- `oci.subnet_id` — OCID d'un subnet public dans la région cible
- `oci.image_id` — OCID d'une image ARM compatible (Ubuntu, Oracle Linux...) dans la région
- `oci.ssh_public_key` — la clé publique SSH
- `oci.ocpus` / `memory_in_gbs` — configuration de l'instance cible (1/6 conseillé pour démarrer)
- `telegram.enabled` — `false` si vous ne voulez pas de notifs

## Usage

### Run local (foreground)

```bash
python3 oci_instance_grabber_v3.py
```

Le script tourne jusqu'à succès, timeout (`max_duration_hours`), ou `Ctrl+C`. Au succès il écrit `instance_details.json` avec l'OCID de l'instance créée.

### Deploy en service systemd (production 24/7)

Sur ta VM :

```bash
# 1. Copier les fichiers
scp oci_instance_grabber_v3.py config.json user@la-vm:~/
scp oci-grabber.service user@la-vm:/tmp/

# 2. SSH sur la VM, adapter le service (User + WorkingDirectory selon distro)
ssh user@la-vm
sudo cp /tmp/oci-grabber.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable oci-grabber
sudo systemctl start oci-grabber

# 3. Suivre les logs
journalctl -u oci-grabber -f
```

### Monitoring via SSH tunnel

Depuis la machine locale :

```bash
ssh -L 8080:localhost:8080 user@la-vm
curl http://localhost:8080/status
```

Retourne un JSON avec : `status`, `attempt`, `uptime`, `last_fault_domain`, `last_error`, `rate_limited_count`, `current_backoff_sec`, `result`.

## Configuration

Le fichier `config.json` est découpé en 3 sections :

| Section | Clé | Description |
|---------|-----|-------------|
| `oci` | `config_file_path` | Chemin du fichier de config OCI (par défaut `~/.oci/config`) |
| `oci` | `config_profile` | Profil OCI à utiliser (par défaut `DEFAULT`) |
| `oci` | `compartment_id` | OCID du compartment cible |
| `oci` | `availability_domain` | Nom court de l'AD (ex. `AD-1`) — le script trouve le nom complet auto |
| `oci` | `subnet_id` / `image_id` | OCIDs réseau et image |
| `oci` | `shape` | `VM.Standard.A1.Flex` pour ARM, `VM.Standard.E2.1.Micro` pour x86 |
| `oci` | `ocpus` / `memory_in_gbs` | Config de l'instance cible |
| `oci` | `boot_volume_size_in_gbs` | Taille du disque (défaut 50) |
| `oci` | `ssh_public_key` | Clé SSH injectée dans l'instance |
| `oci` | `instance_display_name` | Nom affiché dans la console OCI |
| `oci` | `user_data_file` | Chemin vers un script (ex: `.sh`) pour cloud-init |
| `oci` | `user_data` | Script cloud-init directement en ligne (alternative à `user_data_file`) |
| `telegram` | `enabled` | `true`/`false` — désactive tout le flux Telegram |
| `telegram` | `bot_token` / `chat_id` | Credentials du bot |
| `retry` | `min_interval_seconds` / `max_interval_seconds` | Sleep aléatoire entre tentatives (défaut 90-120) |
| `retry` | `rate_limit_initial_backoff_seconds` / `rate_limit_max_backoff_seconds` | Backoff 429 |
| `retry` | `max_duration_hours` | Durée max avant arrêt auto (défaut 96h) |

## Limitations et choix

- **Pas de re-provisioning** : au succès, le script écrit `instance_details.json` et s'arrête. La configuration post-création (install Docker, etc.) est à votre charge.
- **Un seul shape par run** : pour tenter plusieurs configs (1/6, 2/12...), lance plusieurs processus ou modifie `config.json` entre deux runs.
- **Pas de gestion des quotas** : si vous dépassez votre quota `LimitExceeded`, le script s'arrête (comportement volontaire).

## Automatisation (Cloud-Init)

Vous pouvez passer un script `user_data` pour configurer la VM dès son premier démarrage (ex: installer Docker, configurer un firewall). 

Exemple de fichier `cloud-init.sh` :
```bash
#!/bin/bash
apt-get update
apt-get install -y docker.io
systemctl enable --now docker
```

Puis référencez-le dans votre `config.json` :
```json
"user_data_file": "cloud-init.sh"
```

## Disclaimer

Ce script envoie une requête toutes les 90-120s à l'API Oracle. C'est conforme à l'usage normal de l'API (bien en-dessous des limites documentées) mais **respecte les [Terms of Service Oracle Cloud](https://www.oracle.com/legal/cloud-services-agreement.html)**. L'auteur décline toute responsabilité en cas d'usage abusif.

## License

MIT — voir [LICENSE](LICENSE).
