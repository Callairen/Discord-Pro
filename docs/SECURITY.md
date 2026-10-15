# Security

## Defaults

- Binds to **127.0.0.1:8765**. `server.host` only accepts loopback values; there is no setting that binds a public interface without also turning on the LAN policy below.
- Every endpoint except `/health`, `/auth/session`, `/auth/login`, `/auth/logout` and `/auth/redeem` requires authentication, **on localhost too**. Other software on the machine and websites in your browser cannot drive the API.

## Authentication model

| Credential | Who uses it | Where it lives | Notes |
|---|---|---|---|
| API token (256-bit) | desktop GUI, CLI | `auth.json` in the user data dir (`0600` on POSIX; per-user `%APPDATA%` on Windows) | `Authorization: Bearer`. Never sent to browsers, never logged. Required for shutdown, login links and the access code. |
| Browser session | web dashboard | `HttpOnly; SameSite=Strict` cookie, server-side store in memory, 12 h | Not readable by JavaScript. Lost when the backend restarts. |
| One-time login link | GUI/CLI → browser | in memory, 120 s, single use | Lets "Open web dashboard" sign in without exposing any long-lived secret. |
| Access code (12 chars, ~60 bits) | other devices on the LAN | `auth.json` | Shown only in the desktop app (token-only endpoint). 5 failed logins per IP per 5 min. Rotating it revokes every browser session. |

REST and WebSocket apply the same rules (`principal()` and the WebSocket handshake check the same token / session store).

The Discord **Application ID is not a secret** and is the only Discord identifier used. No bot token, OAuth token or user token is requested, stored or transmitted.

## Browser-facing protections

- **DNS rebinding:** the `Host` header must be `localhost` or an IP literal (loopback, or private/link-local when LAN is on). Any domain name gets 400. A rebinding attack needs the victim's browser to send the attacker's domain as `Host`, so it fails.
- **CSRF:** cookie-authenticated writes need `X-Discord-Pro: 1` (a custom header forces a CORS preflight, which is never granted: no CORS middleware, no wildcard) and a same-origin `Origin` when present. `SameSite=Strict` adds a second layer. Login also checks `Origin`.
- **WebSocket origin:** a browser `Origin` must equal the `Host` the socket connected to.
- **Headers:** `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- **XSS:** the dashboard builds DOM with `textContent`, never `innerHTML` with data. Inline scripts are blocked by CSP.

## LAN access (opt-in)

`Settings → LAN access` (+ backend restart) binds `0.0.0.0` and accepts peers from private/link-local ranges only. Public IPs are refused even if forwarded. LAN clients must sign in with the access code. The setting is shown with a warning and confirmation in both UIs. Traffic is plain HTTP inside your LAN: use it only on trusted networks (not public Wi-Fi). Never port-forward it to the internet.

Firewall: Windows will ask to allow Python/DiscordPro; allow **Private networks** only.

## Input and API hardening

- Pydantic models with `extra="forbid"` for every request; activity limits enforced before anything reaches Discord.
- Button URLs: `http(s)` only, must have a host, no embedded credentials, no whitespace, ≤ 512 chars (`javascript:`, `data:` and similar are refused).
- Process names for auto-switch must be plain executable names (no path separators); they are only compared against `psutil` names, never executed.
- Body limit 64 KiB (413); WebSocket message limit 64 KiB.
- Command rate limit: 30 commands / 10 s per client (REST and WS). Max 8 in-flight WS commands per connection.
- No shell execution and no filesystem access driven by API parameters.
- Errors: stable codes and messages; stack traces only in the server log. Unhandled exceptions return `500 internal_error`.

## Secrets and logs

- A logging filter replaces registered secrets (API token, access code, session ids, login codes) and any `Bearer …`, `code=…`, `token=…` patterns with `***`.
- uvicorn's access log is disabled (it would print query strings such as one-time codes).
- `/diagnostics` contains no secrets; host details (Python, OS, data dir) are only returned to loopback clients.
- `.gitignore` excludes config, presets, auth, logs, `.env`, build output and executables. `config/*.example.json` contain no secrets.

## Threat model (summary)

| Threat | Mitigation |
|---|---|
| Malicious website calling `http://127.0.0.1:8765` | auth required; custom header + Origin check (CSRF); no CORS; Host check (DNS rebinding) |
| Other device on the LAN | not reachable unless LAN is enabled; then access code + rate limiting |
| Internet attacker | loopback/private peers only; no public bind by default |
| Other local OS users | `auth.json` user-only permissions (POSIX); per-user app-data on Windows |
| Malware running as the same user | **out of scope**: it can read `auth.json` like any user file |
| Stolen session cookie | HttpOnly, 12 h expiry, revoked by rotating the access code |
| Credential leakage in logs/UI | redaction filter, token-only endpoints, no secrets in state/config responses |

## Known limitations

- No TLS on the LAN; traffic can be observed by others on the same network.
- Sessions are in memory; a restart signs out browsers (security-friendly but less convenient).
- The access code has ~60 bits of entropy plus rate limiting. It is adequate for a LAN, not for internet exposure.
- Windows file ACLs on `auth.json` rely on the default per-user `%APPDATA%` permissions; they are not tightened explicitly.

## Discord compliance

Uses only the local RPC mechanism with a public Application ID. Not implemented, by design: self-bots, user-token automation, credential extraction, automatic Developer Portal application creation.
