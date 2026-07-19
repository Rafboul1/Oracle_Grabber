# AGENTS.md — oracle_use

## Projet

Infrastructure Oracle Cloud pour obtenir une instance ARM (Paris) et y héberger N8N en production 24/7. Le script V3 tourne en prod sur la micro x86.

- **Code** : `C:\Vault\Projects\oracle_use\`
- **Type** : gros projet — repo GitHub `Rafboul1/Oracle_Grabber` (README.md séparé)
- **Objectif final** : installer N8N sur l'ARM pour y faire tourner Digest-IA

## Stack & Architecture

**Instance micro x86 (active)**

| Paramètre | Valeur |
|-----------|--------|
| Shape | VM.Standard.E2.1.Micro |
| IP publique | `158.178.210.132` |
| User SSH | `opc` |
| OS | Oracle Linux 9 / Python 3.9 |
| Clé SSH | `C:\Users\visit\.ssh\id_rsa` |

**Instance ARM cible**

| Paramètre | Valeur |
|-----------|--------|
| Shape | VM.Standard.A1.Flex |
| Stratégie | Petit pied : 1 OCPU / 6 Go |
| AD | EU-PARIS-1-AD-1 |
| OS | Ubuntu |
| Nom | `rafboul-arm-instance` |

**Fichiers clés**

- `oci_instance_grabber_v3.py` — script actif, **ne pas modifier sans arrêter le service**
- `config.json` — contient les OCIDs, credentials Telegram et paramètres retry
- `oci-grabber.service` — déployé à `/etc/systemd/system/oci-grabber.service` sur la micro

**Script grabber V3**

- Tente `LaunchInstance` toutes les 90-120s (calibré sur le seuil Oracle ~120s/tenant)
- Rotation FD-1 → FD-2 → FD-3
- Backoff exponentiel sur 429 : 60s → 120s → 240s → 600s
- Notification Telegram au démarrage, au succès et au timeout (96h)
- Tourne via systemd (`oci-grabber.service`) — redémarre automatiquement en cas de crash

**Configuration OCI**

- Région : `eu-paris-1` — AD unique : `cjwQ:EU-PARIS-1-AD-1`
- Compte : Pay As You Go (meilleure priorité ARM vs Free Tier)

## Règles importantes

- Le script lit `ocpus` / `memory_in_gbs` depuis `config.json` (defaults 1/6 si absents — stratégie petit pied)
- `min_interval_seconds` / `max_interval_seconds` + paramètres de backoff 429 sont lus depuis `config.json` (section `retry`) avec defaults 90-120s / 60-600s si absents
- Python 3.9 sur la micro : pas de type hints modernes (`dict | None` → interdit)
- Après succès : lire `/home/opc/instance_details.json` pour l'IP de l'ARM

## Commandes utiles

```bash
# Connexion SSH
ssh -i C:\Users\visit\.ssh\id_rsa opc@158.178.210.132

# Monitoring
journalctl -u oci-grabber -f          # logs temps réel
journalctl -u oci-grabber -n 50       # 50 dernières lignes
sudo systemctl status oci-grabber     # statut service
sudo systemctl start oci-grabber      # relancer après timeout 96h
sudo systemctl stop oci-grabber       # arrêter
```

**Déployer une mise à jour**

```powershell
scp -i C:\Users\visit\.ssh\id_rsa "C:\Vault\Projects\oracle_use\oci_instance_grabber_v3.py" "C:\Vault\Projects\oracle_use\config.json" opc@158.178.210.132:~/
```
Puis sur la micro : `sudo systemctl restart oci-grabber`

## Continuité

- À chaque reprise, lire `STATUS.md` avant d'agir.
- Consulter `JOURNAL.md` uniquement pour retracer une décision ou une ancienne session.
- Garder ici seulement les règles durables ; l'état courant et l'historique n'appartiennent pas à ce fichier.
