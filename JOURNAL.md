# JOURNAL — oracle_use

## 2026-04-23 — Continuité précédente [archive STATUS]

**Mis à jour** : 2026-04-23

### Maintenant

> ⚠ État figé depuis 2026-04-23 (projet en pause). À reconfirmer : « Instance ARM obtenue » est coché mais la Reprise demande encore de SSH sur l'ARM et d'y installer Docker+N8N — vérifier si l'ARM tourne vraiment et si le grabber de la micro doit être arrêté.

- [x] Compte OCI passé en Pay As You Go
- [x] Instance micro x86 créée (`158.178.210.132`)
- [x] Script V3 déployé via systemd sur la micro
- [x] Notifications Telegram configurées
- [x] Instance ARM obtenue
- [ ] N8N installé sur l'ARM
- [ ] Workflow Digest-IA migré sur l'ARM
- [ ] N8N configuré en service systemd sur l'ARM

### Dernière session

**Date** : 2026-04-23
**Fait** :
- Ajout support Cloud-init (user_data) pour Oracle Grabber
- Support via `user_data_file` (chemin) et `user_data` (inline) — encodage Base64 automatique
- Validation via script de test (mock OCI) — documentation dans README
- Modifications commitées et poussées sur le dépôt

**État** : terminé
**Reprise** :
- SSH sur l'ARM, récupérer IP dans `/home/opc/instance_details.json`
- Installer Docker + N8N sur l'ARM, migrer le workflow Digest-IA, configurer N8N en service systemd
- Arrêter le service grabber sur la micro
