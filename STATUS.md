# STATUS — oracle_use

**Mis à jour** : 2026-07-19

## Maintenant

> ⚠ État figé depuis 2026-04-23 (projet en pause). À reconfirmer : « Instance ARM obtenue » est coché mais la Reprise demande encore de SSH sur l'ARM et d'y installer Docker+N8N — vérifier si l'ARM tourne vraiment et si le grabber de la micro doit être arrêté.

- [x] Compte OCI passé en Pay As You Go
- [x] Instance micro x86 créée (`158.178.210.132`)
- [x] Script V3 déployé via systemd sur la micro
- [x] Notifications Telegram configurées
- [x] Instance ARM obtenue
- [ ] N8N installé sur l'ARM
- [ ] Workflow Digest-IA migré sur l'ARM
- [ ] N8N configuré en service systemd sur l'ARM

## Dernière session

**Date** : 2026-07-19
**Fait** :
- Migration de continuité appliquée : `AGENTS.md` garde les règles durables, l'état courant reste ici et l'historique vit dans `JOURNAL.md`.
- Ancien `STATUS.md` archivé dans `JOURNAL.md` sans perte.
**État** : terminé — documentation uniquement, état produit inchangé.
**Reprise** : - SSH sur l'ARM, récupérer IP dans `/home/opc/instance_details.json`
- Installer Docker + N8N sur l'ARM, migrer le workflow Digest-IA, configurer N8N en service systemd
- Arrêter le service grabber sur la micro
