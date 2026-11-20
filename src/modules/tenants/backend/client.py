# client.py (module "tenants") - calls to a tenant backend
# -------------------------------------------------------------
# Logs the user in at the tenant backend with THEIR stored credentials
# (the tenant's normal login API) and fetches a token. With it:
#   - autologin(): token for embedding (dashboard with #client-token=)
#   - api(): any tenant API on behalf of the user (e.g. spaces/relay)
# httpx (async). TLS verification can be disabled per tenant (self-signed certs).
# The connection is selectable per tenant (field "via"): "" = from the RMM server,
# otherwise a node ID -> the calls run THROUGH THAT NODE (node module "tenants",
# command "tenants-http") - for tenants reachable only in the customer LAN.
from __future__ import annotations

import httpx

from . import store


class TenantError(RuntimeError):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


def _base(tenant: dict) -> str:
    return (tenant.get("url") or "").rstrip("/")


async def _request(tenant: dict, method: str, url: str, headers: dict | None = None,
                   json_body=None, content: bytes | None = None, timeout: float = 25) -> httpx.Response:
    """A request to the tenant - directly or through the selected node."""
    verify = bool(tenant.get("verify_tls", True))
    headers = dict(headers or {})
    via = tenant.get("via") or ""
    if not via:
        async with httpx.AsyncClient(verify=verify, timeout=timeout, follow_redirects=True) as c:
            return await c.request(method, url, headers=headers, json=json_body, content=content)
    import base64
    import json as _json
    from app import node_manager
    if json_body is not None:
        content = _json.dumps(json_body).encode()
        headers.setdefault("Content-Type", "application/json")
    try:
        node_manager.require_module(via, "tenants")
        res = await node_manager.command(via, "tenants-http", {
            "method": method, "url": url, "headers": headers, "verify": verify, "timeout": timeout,
            "body": base64.b64encode(content).decode() if content else ""}, timeout=timeout + 15)
    except node_manager.NodeError as e:
        raise httpx.ConnectError(f"via node: {e}") from None
    return httpx.Response(int(res.get("status") or 502), headers=res.get("headers") or [],
                          content=base64.b64decode(res.get("body") or ""),
                          request=httpx.Request(method, url))


async def _login(tenant: dict, username: str, password: str, realm: str, code: str = "") -> str:
    base = _base(tenant)
    if not base:
        raise TenantError("No tenant URL configured")
    body = {"username": username, "password": password, "realm": realm or "local"}
    if code:
        body["code"] = code
    try:
        r = await _request(tenant, "POST", base + "/api/auth/login", json_body=body, timeout=20)
    except httpx.HTTPError as e:
        raise TenantError(f"Tenant not reachable: {e}") from None
    if r.status_code != 200:
        detail = ""
        try:
            detail = r.json().get("detail") or ""
        except Exception:
            detail = r.text[:200]
        if detail == "2fa_required":
            raise TenantError("2fa_required", 401)
        raise TenantError(f"Login to the tenant failed: {detail or r.status_code}", r.status_code)
    data = r.json()
    tok = data.get("access_token") or data.get("token")
    if not tok:
        raise TenantError("Tenant returned no token")
    return tok


async def token_for(tenant: dict, user: dict, code: str = "") -> str:
    """Fetch a token at the tenant - with the credentials stored for this user."""
    cred = store.get_cred(tenant["id"], user["id"])
    if not cred or not cred.get("pw_enc"):
        raise TenantError("no_credentials", 412)
    return await _login(tenant, cred["username"], store.dec(cred["pw_enc"]), cred.get("realm", "local"), code)


async def verify_credentials(tenant: dict, username: str, password: str, realm: str, code: str = "") -> dict:
    """Check when storing. -> {ok} or raises TenantError (incl. 2fa_required)."""
    await _login(tenant, username, password, realm, code)
    return {"ok": True}


async def api(tenant: dict, token: str, method: str, path: str, **kw) -> httpx.Response:
    base = _base(tenant)
    headers = {"Authorization": "Bearer " + token, **(kw.pop("headers", None) or {})}
    try:
        return await _request(tenant, method, base + path, headers=headers,
                              json_body=kw.pop("json", None), content=kw.pop("content", None))
    except httpx.HTTPError as e:
        raise TenantError(f"Tenant not reachable: {e}") from None


# ------------------------------------------------------------------ tenant-link (pairing)
# These talk to the counterpart module "tenant-link" on the tenant backend.
# claim/request need no user token (the code is the secret / the admin approves);
# status proves the main RMM's identity with the stored pair_key.
async def _tl(tenant: dict, method: str, path: str, json_body=None, headers=None) -> httpx.Response:
    base = _base(tenant)
    if not base:
        raise TenantError("No tenant URL configured")
    try:
        return await _request(tenant, method, base + path, headers=headers or {}, json_body=json_body, timeout=20)
    except httpx.HTTPError as e:
        raise TenantError(f"Tenant not reachable: {e}") from None


def _tl_detail(r: httpx.Response) -> str:
    try:
        return r.json().get("detail") or ""
    except Exception:
        return (r.text or "")[:200]


async def tl_claim(tenant: dict, code: str, parent_name: str, parent_url: str) -> str:
    """Redeem a pairing code on the tenant -> returns the pair_key."""
    r = await _tl(tenant, "POST", "/api/tenant-link/pair/claim",
                  json_body={"code": code, "name": parent_name, "parent_url": parent_url})
    if r.status_code == 404:
        raise TenantError("The tenant does not have the \"Tenant Sharing\" module active", 409)
    if r.status_code != 200:
        raise TenantError(_tl_detail(r) or f"Pairing failed ({r.status_code})", r.status_code)
    key = r.json().get("pair_key")
    if not key:
        raise TenantError("Tenant returned no pairing key")
    return key


async def tl_request(tenant: dict, parent_name: str, parent_url: str) -> str:
    """Ask the tenant for a pairing (needs admin approval there) -> returns the pair_key."""
    r = await _tl(tenant, "POST", "/api/tenant-link/pair/request",
                  json_body={"name": parent_name, "parent_url": parent_url})
    if r.status_code == 404:
        raise TenantError("The tenant does not have the \"Tenant Sharing\" module active", 409)
    if r.status_code != 200:
        raise TenantError(_tl_detail(r) or f"Pairing request failed ({r.status_code})", r.status_code)
    key = r.json().get("pair_key")
    if not key:
        raise TenantError("Tenant returned no pairing key")
    return key


async def tl_status(tenant: dict, pair_key: str) -> dict:
    """Check the pairing on the tenant. -> {status, embeddable, name}. 401 -> pairing removed."""
    r = await _tl(tenant, "GET", "/api/tenant-link/status", headers={"X-Tenant-Link-Key": pair_key})
    if r.status_code == 401:
        raise TenantError("pairing_removed", 401)
    if r.status_code == 404:
        raise TenantError("The tenant does not have the \"Tenant Sharing\" module active", 409)
    if r.status_code != 200:
        raise TenantError(_tl_detail(r) or f"Status check failed ({r.status_code})", r.status_code)
    return r.json()
