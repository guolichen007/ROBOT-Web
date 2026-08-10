# Security Baseline

- Passwords use Argon2id. Access tokens are short-lived and kept only in browser memory.
- Refresh tokens are HttpOnly, SameSite=Strict cookies; server profile additionally requires Secure. Token families rotate and are revoked on reuse/logout/password change.
- Cookie-auth endpoints require the matching CSRF cookie/header. Login has Redis rate limiting and temporary account/IP lockout.
- WebSocket authentication exchanges an access token for a 60-second one-use ticket. Origin and permissions are checked; long-lived JWTs never appear in the URL.
- RBAC separates read, manual, stop, software e-stop, reset, force release and task permissions.
- High-risk commands, map publication, identity changes and alarm transitions are audited with request/operator/resource/robot/result context.
- Asset upload checks size, MIME, extension, SHA-256, randomized storage name and resolved path containment.
- `.env`, certificates, private keys, backups and local data are ignored by Git. Logs must never emit tokens, passwords or credential material.
- SERVER rejects demo seed, Mock Robot, anonymous MQTT, insecure cookies and weak/default secrets at application startup.

Deployment owner must provide TLS certificates, Mosquitto password/ACL files, secret-file mounts, allowed origins and a vulnerability-management policy. This baseline is not a formal penetration-test or functional-safety certification.
