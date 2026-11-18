# BYTERA RMM – REST API (`/api/v1`)

Controls everything the web dashboard can do. Loaded by the **"REST API" module** (module app → enable);
the core never starts this folder itself. Settings, active logins and the call log:
*module app → REST API → Settings*.

## Access
1. Create an account with the **"API user"** template: no password, no first login – the server generates a
   JWT-signed **API key** (`rmmak_…`, shown only once; regenerate/revoke under module app → REST API →
   API accounts). Alternatively, grant a normal account the **"Use REST API"** permission (login with password).
   **Rotation:** setting "Automatically rotate API keys after" – due keys are replaced on login
   (`new_api_key` in the response), the old one is still valid for the transition period; button "Rotate all keys".
2. Give the account the permissions it needs via the API – **just like in the dashboard** (e.g. "Delete client" to
   delete clients). Without the matching permission the API responds with 403/404, exactly like the dashboard. Admins may do everything.

## Modes (setting "Mode")
| Mode | Login | On every request |
|---|---|---|
| Without user | none – runs as the configured API user (only sensible with an IP allowlist!) | nothing |
| With user | `POST /api/v1/auth/login` → session | cookie `rmm_api_session` **or** header `X-API-Session: <session>` |
| With user (secure) | `POST /api/v1/auth/login` → long code | `Authorization: Bearer <code>` **or** `X-API-Token: <code>` |

Validity of the secure code: 1 h, 8 h, 24 h, 7 days, 35 days, 1 year or unlimited. The code is a
random value, signed as a JWT and encrypted; only its hash is stored in the database. "Expire all immediately"
in the settings makes all codes/sessions invalid right away.

## Example (secure)
```bash
API=https://rmm.company.com/api/v1
TOKEN=$(curl -s -X POST $API/auth/login -H "Content-Type: application/json" \
        -d '{"username":"apibot","api_key":"rmmak_…","label":"Backup script"}' | jq -r .token)
curl -H "Authorization: Bearer $TOKEN" $API/clients                    # clients
curl -H "Authorization: Bearer $TOKEN" -X DELETE $API/clients/<id>     # needs "Delete client"
curl -H "Authorization: Bearer $TOKEN" -X POST $API/modules/spaces/enable
curl -H "Authorization: Bearer $TOKEN" -X POST $API/auth/logout
```
Normal account: `{"username", "password"}` (+ `"code"` for 2FA, `"realm"` for AD).
Full docs with a Python example: `docs/REST-API.md`, `docs/examples/rmm_api_client.py`.

## Endpoints
- `GET /api/v1/status` – mode (without login)
- `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/me` (incl. own permissions)
- `GET /api/v1/routes` – **catalog**: all shortcuts + all dashboard routes
- Shortcuts: `/users`, `/groups`, `/permissions`, `/permissions/{user|group}/{id}`, `/clients`, `/clients/{id}`
  (+ `/exec`, `/processes`, `/files`, `/metrics`, `/notes`), `/agents/{id}/update|uninstall`, `/agents/update-all`,
  `/hierarchy`, `/modules` (+ `/enable`, `/disable`, `/settings`, `/features`), `/apps`, `/settings`,
  `/nodes` (+ `/enable`, `/disable`, `/update`, `/modules/{id}`), `/audit`, `/system/update`, `/system/restart`
- **Bridge**: every dashboard route `/api/<path>` is reachable as `/api/v1/call/<path>`
  (e.g. `/api/spaces/fs/list?path=…` → `/api/v1/call/spaces/fs/list?path=…`).

Not via REST: live features that run over the live connection in the dashboard (terminal, live screen).

## Structure
`security.py` (modes, codes, sessions) · `bridge.py` (bridge, same permission check as the dashboard) ·
`store.py` (tables `api_tokens`, `api_log`, settings, IP allowlist) · `routes.py` (`/api/v1`, `/api/apimod`).
