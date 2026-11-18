"""
routers/admin_routes.py
------------------------
Collection router for the administration features:
  - Gruppen & Rollen (Rechte-System)
  - Realms (Verzeichnis-Anbindung / Active Directory - Konfiguration)
  - Webhooks (Benachrichtigungen, Discord/Custom)
  - Automationen (geplante Befehle)

These endpoints are admin-protected.
"""

import json
import time
import urllib.request
from datetime import datetime, timezone

import os
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import db
from app.auth import (get_current_user, require_admin, require_perm,
                      user_has_permission)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ==================================================================
# Gruppen & Rollen
# ==================================================================

class GroupBody(BaseModel):
    name: str
    permissions: list[str] = []


@router.get("/permissions")
def list_permissions(user: dict = Depends(get_current_user)):
    """List of all available permission keys (for the checkbox UI)."""
    return {"permissions": db.ALL_PERMISSIONS}


@router.get("/groups")
def list_groups(user: dict = Depends(get_current_user)):
    require_perm(user, "see_permissions")
    groups = db.list_groups()
    for g in groups:
        g["permissions"] = [p for p in (g["permissions"] or "").split(",") if p]
    return groups


@router.post("/groups")
def create_group(body: GroupBody, user: dict = Depends(get_current_user)):
    require_perm(user, "manage_permissions")
    g = db.create_group(body.name, body.permissions)
    db.add_audit_entry(user["username"], "group.created", target=g["id"], details=body.name)
    return g


@router.put("/groups/{group_id}")
def update_group(group_id: str, body: GroupBody, user: dict = Depends(get_current_user)):
    require_perm(user, "manage_permissions")
    g = db.update_group(group_id, body.name, body.permissions)
    db.add_audit_entry(user["username"], "group.updated", target=group_id, details=body.name)
    return g


@router.delete("/groups/{group_id}")
def delete_group(group_id: str, user: dict = Depends(get_current_user)):
    require_perm(user, "manage_permissions")
    db.delete_group(group_id)
    db.add_audit_entry(user["username"], "group.deleted", target=group_id)
    return {"ok": True}


class GroupUnmanagedBody(BaseModel):
    unmanaged: bool = True


@router.put("/groups/{group_id}/unmanaged")
def set_group_unmanaged(group_id: str, body: GroupUnmanagedBody,
                              user: dict = Depends(get_current_user)):
    """(AD-)Gruppe als unverwaltet markieren – landet im eingeklappten Ordner
    und wird AD-Benutzern nicht mehr automatisch zugewiesen."""
    require_perm(user, "manage_permissions")
    g = db.set_group_unmanaged(group_id, body.unmanaged)
    db.add_audit_entry(user["username"], "group.updated", target=group_id,
                       details=f"unmanaged={'1' if body.unmanaged else '0'}")
    return g or {"ok": True}


class UserGroupsBody(BaseModel):
    group_ids: list[str] = []


@router.put("/users/{user_id}/groups")
def set_user_groups(user_id: str, body: UserGroupsBody, user: dict = Depends(get_current_user)):
    require_perm(user, "manage_permissions")
    db.set_user_groups(user_id, body.group_ids)
    return {"ok": True}


@router.get("/users/{user_id}/groups")
def get_user_groups(user_id: str, user: dict = Depends(get_current_user)):
    require_perm(user, "see_permissions")
    return {"group_ids": db.get_user_group_ids(user_id)}


# ==================================================================
# Fine-grained permissions (tri-state grants) for the permissions app
# ==================================================================
# Labels for the frontend display. The resolver knows the semantics.
PERM_LABELS = {
    # --- Global / universal ---
    "admin": "Full admin (full access)",
    "login": "Sign in",
    "see_dashboard": "See dashboard",
    "customize_dashboard": "Customize dashboard",
    "restore_session": "Restore recently opened items",
    "edit_profile_name": "Change profile name",
    "access_clients": "See/access clients",
    "see_replay": "See recordings",
    "delete_replay": "Delete recordings",
    "see_audit": "See audit log",
    "see_source": "View source",
    "edit_source": "Edit source",
    "delete_source": "Delete source (database/explorer)",
    "manage_branding": "Change branding",
    "manage_sso": "Change SSO",
    "see_settings": "View settings",
    "manage_settings": "Change default settings",
    "admin_settings": "Admin settings (IP/port, server update, DB, restart)",
    "screen_silent": "Remote screen without a request (silent mode)",
    "c_screen_silent": "Remote screen without a request",
    "set_warranty": "Set warranty date",
    "ticket_read": "Read tickets",
    "ticket_create": "Create tickets",
    "ticket_edit": "Edit tickets",
    "ticket_comment": "Comment on tickets",
    "ticket_assign": "Assign tickets",
    "ticket_resolve": "Mark tickets as resolved",
    "ticket_delete": "Delete tickets",
    "see_permissions": "See permissions & groups",
    "manage_permissions": "Edit permissions & groups",
    "manage_modules": "Manage modules (enable, disable, settings)",
    "get_user_space": "Use your own space (incl. notebook, images in chat/tickets, music)",
    "use_public_space": "Use public spaces",
    "see_all_spaces": "See all spaces (read)",
    "edit_all_spaces": "Edit and manage all spaces",
    "manage_deployment": "Manage deployment server (page, files, relay area \"Deployment\")",
    "use_api": "Use the REST API (/api/v1) - plus the matching permission per call",
    "manage_websites": "Manage websites (create, delete, edit all - \"Webserver\" module)",
    "manage_docker": "Manage Docker (create stacks, all containers, images, volumes, networks - \"Docker\" module)",
    "manage_cloudflare": "Manage Cloudflare Tunnel (access, tunnels, routes, sharing - \"Cloudflare Tunnel\" module)",
    "manage_headless_screen": "Install/remove a virtual (headless) screen - \"Headless screen\" module",
    "manage_canary": "Manage IP tracker and canary tokens - \"Canary & IP tracker\" module",
    "manage_ad": "Manage Active Directory (users, groups, OUs, passwords) - \"Active Directory\" module",
    "use_bmc": "View and control iLO/iDRAC (power, console, sensors) – module \"iLO / iDRAC\"",
    "manage_bmc": "Create and edit iLO/iDRAC connections – module \"iLO / iDRAC\"",
    "use_passwords": "Use the password manager (own & shared vaults, end-to-end) - \"Passwords\" module",
    "use_geotrack": "View device locations (map, history) – module \"Location Tracker\"",
    "manage_geotrack": "Toggle location tracking per device, clear history – module \"Location Tracker\"",
    "manage_tenants": "Manage tenants (connect backends, sharing) - \"Tenants\" module",
    "use_tenants": "Open and switch tenants - \"Tenants\" module",
    "manage_dist": "Manage distribution (/dist: folders, files, shares)",
    "manage_temp_usb": "Manage the digital USB stick (temp-usb: areas, files, attach to devices)",
    "use_browser": "Use the server browser (load websites via the server, VPN access)",
    "use_console": "Use the RMM console (SSH, dashboard, client app) - plus the matching permission per command",
    "create_users": "Create users",
    "network_scan": "Network scan",
    "port_scan": "Port scan",
    "bulk_shell": "Bulk remote shell",
    "use_scripts": "Use scripts",
    "create_scripts": "Create scripts",
    "automation": "Automations",
    "manage_hierarchy": "Manage tenants/locations/folders",
    "play_games": "Play games",
    "use_media": "Audio player & media library",
    "share_media": "Provide media for everyone (uploads, links, lists)",
    "manage_favorites": "Edit the favorites list",
    "use_relay": "Use relay",
    "use_chat": "Use chat",
    "use_todos": "Use the todo list (private)",
    "manage_privacy": "Privacy: retention periods, access, erasure (GDPR)",
    "patching": "See and install updates",
    "manage_patching": "Define auto-patch rules",
    "see_inventory": "View inventory",
    "manage_inventory": "Manage inventory (devices, owners, stock)",
    "super_admin": "Super admin (all permissions)",
    "add_client": "Add clients (enrollment)",
    "see_org": "View org chart",
    "manage_org": "Edit org chart (reporting lines, workspace)",
    "use_calendar": "Use calendar",
    "manage_calendar": "Calendar: create events for everyone",
    "relay_unlimited": "Relay unlimited (no auto-close time)",
    "use_vpn": "Use VPN (issue tunnels)",
    "vpn_unlimited": "VPN unlimited (tunnels without expiry)",
    # --- Per client ---
    "manage_clients": "Edit",
    "manage_agent": "Update (agent update)",
    "c_delete": "Delete",
    "c_screen": "Remote screen (control)",
    "c_screen_view": "Remote screen (view only)",
    "c_terminal": "Terminal",
    "c_terminal_console": "Terminal (agent console only)",
    "c_guacamole": "Guacamole",
    "c_power": "Shut down/restart",
    "c_registry": "Edit registry (Windows)",
    "c_win_update": "Control Windows automatic updates (Windows)",
    "c_explorer_view": "See file explorer",
    "c_explorer_edit": "Edit file explorer",
    "c_taskmanager_view": "See task manager",
    "c_taskmanager_kill": "Kill process",
    "c_relay": "Start relay",
    "c_relay_unlimited": "Relay unlimited",
    "c_vpn": "Issue VPN tunnel",
    "c_vpn_unlimited": "VPN tunnel without expiry",
    "c_portforward": "Set up port forwarding to this client",
    "manage_nodes": "Manage node (install, Docker, node modules)",
    "pve_access": "Proxmox: see dashboard",
    "pve_vms_all": "Proxmox: all VMs",
    "pve_lxc_all": "Proxmox: all LXC containers",
    "pve_power": "Proxmox: start/stop guests",
    "pve_console": "Proxmox: open console",
    "pve_snapshot": "Proxmox: snapshots",
    "pve_backup": "Proxmox: backups",
    "pve_config": "Proxmox: change configuration",
    "pve_storage": "Proxmox: storage",
    "pve_users": "Proxmox: users & permissions",
    "pve_nodes": "Proxmox: nodes & tasks",
    "pve_api": "Proxmox: arbitrary API calls (API explorer)",
    "pve_manage": "Proxmox: manage connection & guest shares",
    "c_notes_view": "See notes",
    "c_notes_edit": "Edit notes",
    "c_websites_view": "See websites",
    "c_websites_edit": "Edit websites",
    # --- Legacy keys (display only for already stored grants) ---
    "use_guacamole": "Guacamole (legacy)",
    "use_terminal": "Terminal (legacy)",
    "use_screen": "Remote screen (legacy)",
    "use_explorer": "File explorer (legacy)",
    "use_taskmanager": "Task manager (legacy)",
    "manage_users": "Manage users (legacy)",
}


def _pve_clients() -> list[str]:
    try:
        from app import modules
        return modules.use("proxmox", "pve_store").connected_ids() if modules.is_enabled("proxmox") else []
    except Exception:
        return []


@router.get("/permission-catalog")
def permission_catalog(user: dict = Depends(get_current_user)):
    """Returns the permission keys (global + client) incl. labels for the frontend."""
    require_perm(user, "see_permissions")
    return {
        "labels": PERM_LABELS,
        "general": db.GENERAL_PERM_KEYS,
        "client": db.CLIENT_PERM_KEYS,
        # Implikationen (Quelle -> abgedeckte Rechte): Das Frontend nutzt sie,
        # um beim Erlauben eines Rechts die Basis-Rechte automatisch mitzusetzen.
        "implies": db.perm_implies_map(),
        # Pseudo client scope whose client permissions apply to EVERY client.
        "default_client_scope": db.DEFAULT_CLIENT_SCOPE,
        # Modul "proxmox": Bereich "Standard-Proxmox" + Agenten mit verbundenem Proxmox
        "default_pve_scope": db.DEFAULT_PVE_SCOPE,
        "pve": [p for p in db.CLIENT_PERM_KEYS if p.startswith("pve_")],
        "pve_clients": _pve_clients(),
    }


class GrantItem(BaseModel):
    scope: str = "global"        # 'global' | <client_id>
    perm: str
    effect: str                  # 'allow' | 'deny'


class GrantsBody(BaseModel):
    grants: list[GrantItem] = []


def _valid_subject(subject_type: str, subject_id: str) -> bool:
    if subject_type == "user":
        return db.get_user_by_id(subject_id) is not None
    if subject_type == "group":
        return db.get_group(subject_id) is not None
    return False


@router.get("/grants/{subject_type}/{subject_id}")
def get_grants(subject_type: str, subject_id: str, user: dict = Depends(get_current_user)):
    """Alle Grants eines Subjekts (Benutzer oder Gruppe)."""
    require_perm(user, "see_permissions")
    if subject_type not in ("user", "group"):
        raise HTTPException(400, "subject_type muss 'user' oder 'group' sein")
    if not _valid_subject(subject_type, subject_id):
        raise HTTPException(404, "Subjekt nicht gefunden")
    return {"grants": db.get_grants(subject_type, subject_id)}


@router.put("/grants/{subject_type}/{subject_id}")
def put_grants(subject_type: str, subject_id: str, body: GrantsBody,
                     user: dict = Depends(get_current_user)):
    """Ersetzt ALLE Grants eines Subjekts (tri-state; nur allow/deny werden gespeichert)."""
    require_perm(user, "manage_permissions")
    if subject_type not in ("user", "group"):
        raise HTTPException(400, "subject_type muss 'user' oder 'group' sein")
    if not _valid_subject(subject_type, subject_id):
        raise HTTPException(404, "Subjekt nicht gefunden")
    db.set_grants(subject_type, subject_id, [g.model_dump() for g in body.grants])
    db.add_audit_entry(user["username"], "grants.updated",
                       target=f"{subject_type}:{subject_id}",
                       details=f"{len(body.grants)} Grants")
    return {"ok": True, "grants": db.get_grants(subject_type, subject_id)}


# ==================================================================
# Realms (Verzeichnis-Anbindung / Active Directory): Modul "sso"
# (src/modules/backend/sso/sso_routes.py) - URLs unveraendert.
# ==================================================================


@router.post("/restart")
async def restart_backend(user: dict = Depends(get_current_user)):
    """
    Restarts the backend process (re-exec of the running Python process).
    If the backend runs under a process manager (systemd etc.) with auto-restart,
    a clean exit is sufficient as an alternative - but re-exec works in both
    cases. Responds IMMEDIATELY and restarts ~1 s later, so the response still
    reaches the client.
    """
    # Anyone allowed to change settings may restart: 'admin_settings'
    # (server/system settings) OR 'manage_settings' (default settings).
    # Reason: changes to FTP/SFTP only take effect after a restart - whoever
    # may set them must also be able to apply them.
    # Full administrators have all permissions anyway.
    if not (user_has_permission(user, "admin_settings")
            or user_has_permission(user, "manage_settings")):
        raise HTTPException(403, "Fehlendes Recht: admin_settings/manage_settings")
    db.add_audit_entry(user["username"], "backend.restart")

    import sys as _sys
    import asyncio as _asyncio

    # Im Docker-Container mit eingebundenem docker.sock: den EIGENEN Container
    # ueber die Docker-API neu starten (sauber inkl. Entrypoint). Dieser Weg
    # betrifft ausschliesslich diesen einen Container - niemals Agenten, Nodes
    # oder Proxmox. Ohne Docker/Socket: wie bisher den Prozess neu ausfuehren.
    self_id = None
    if os.getenv("RMM_INSTALL_KIND", "").lower() == "docker":
        try:
            from app import docker_services as _ds
            info = await _asyncio.to_thread(_ds._self_container)
            if info and info.get("Id"):
                self_id = info["Id"]
        except Exception as e:
            print(f"[admin] Eigener Container nicht ermittelbar ({e}) - Neustart des Prozesses.")

    async def _do_restart():
        await _asyncio.sleep(1.0)
        if self_id:
            print(f"[admin] Backend restart: restarting own Docker container {self_id[:12]} ...")
            try:
                from app import docker_services as _ds
                # Der Daemon beendet uns waehrenddessen - Fehler/Timeout hier sind normal.
                await _asyncio.to_thread(_ds._api, "POST", f"/containers/{self_id}/restart?t=15", None, 5)
            except Exception:
                pass
            await _asyncio.sleep(25)          # noch am Leben? Dann Fallback unten
            print("[admin] Container restart did not happen - restarting the process instead.")
        try:
            os.execv(_sys.executable, [_sys.executable] + _sys.argv)
        except Exception:
            # Falls re-exec scheitert: hart beenden, damit ein Prozess-Manager
            # (systemd Restart=always / Docker restart policy) uns neu startet.
            os._exit(0)

    _asyncio.create_task(_do_restart())
    return {"ok": True, "mode": "container" if self_id else "process",
            "message": ("Restarting the backend container…" if self_id
                        else "Backend restarts in about 1 second…")}


@router.post("/stop")
async def stop_backend(user: dict = Depends(get_current_user)):
    """
    STOPS the backend process (clean exit, code 0). Note: if the backend runs
    under a process manager with auto-restart (systemd Restart=always), the
    manager starts it again immediately - then the service must be stopped
    there. Before shutting down, the final database sync is still performed
    (external DB mode). Responds IMMEDIATELY and exits ~1 s later.
    """
    require_perm(user, "admin_settings")
    db.add_audit_entry(user["username"], "backend.stop")

    import asyncio as _asyncio

    async def _do_stop():
        await _asyncio.sleep(1.0)
        try:
            from app import dbsync as _dbsync
            _dbsync.periodic_sync()   # letzten Stand in die externe DB schreiben
        except Exception:
            pass
        print("[admin] Backend wird auf Nutzerwunsch GESTOPPT.")
        os._exit(0)

    _asyncio.create_task(_do_stop())
    return {"ok": True, "message": "Backend wird in ca. 1 Sekunde gestoppt."}


# ==================================================================
# Benachrichtigungen: Meldungs-Aufbau (Versand/Regeln: Modul "notify")
# ==================================================================

# ------------------------------------------------------------------
# Notification-Aufbau
# ------------------------------------------------------------------
# Jede Benachrichtigung besteht IMMER aus einem "head" (Titel/Kurzfassung)
# und einem "body" (eigentliche Nachricht), dazu ein einheitlicher Kontext
# (tenant / location / client / service / timestamp) und ein Level, das
# die Embed-Farbe bestimmt. Die Farben entsprechen denen im Frontend
# (siehe notify.js: info/success/warn/error).

# Level -> Discord embed color (decimal). Hex taken from the frontend.
LEVEL_COLORS = {
    "info":    0x4DA6FF,
    "success": 0x3ECF8E,
    "warn":    0xF5A524,
    "warning": 0xF5A524,
    "error":   0xFF4D6D,
    "danger":  0xFF4D6D,
}



def _normalize_level(level: str | None) -> str:
    level = (level or "info").lower()
    return level if level in LEVEL_COLORS else "info"


def build_notification(
    message: str,
    *,
    head: str | None = None,
    body: str | None = None,
    tenant: str | None = None,
    location: str | None = None,
    client: str | None = None,
    service: str | None = None,
    level: str = "info",
    timestamp: int | None = None,
) -> dict:
    """
    Builds a uniform notification object. 'head' and 'body' are - if not
    passed explicitly - assembled automatically from the context data, so a
    notification ALWAYS has head + body.

    timestamp: milliseconds since epoch (default: now).
    """
    level = _normalize_level(level)
    service = service or "System"
    client = client or "—"
    tenant = tenant or "—"
    location = location or "—"
    ts = timestamp if timestamp is not None else int(time.time() * 1000)

    # head: short summary, e.g. "Disk Space - SRV-01"
    if not head:
        head = f"{service} - {client}"
    # body: die eigentliche Nachricht
    if not body:
        body = message

    return {
        "head": head,
        "body": body,
        "tenant": tenant,
        "location": location,
        "client": client,
        "message": message,
        "service": service,
        "level": level,
        "timestamp": ts,
    }


# Versand an Webhooks (Discord/Custom) und die Webhook-Verwaltung
# (/api/admin/webhooks): Modul "notify" (src/modules/backend/notify).
# build_notification() bleibt hier im Core - damit bauen Core UND Module
# ihre Meldungen einheitlich zusammen.


# ==================================================================
# Globale Einstellungen (Allgemein-Tab)
# ==================================================================

class SettingsBody(BaseModel):
    # Alle optional: es werden nur die mitgeschickten Felder gespeichert.
    server_url: str | None = None
    server_host: str | None = None
    server_domain: str | None = None
    server_backend_port: int | None = None
    server_frontend_port: int | None = None
    host_lock_enabled: str | None = None
    host_lock_scope: str | None = None
    host_lock_extra: str | None = None
    host_lock_trust_proxy: str | None = None
    metrics_interval_seconds: int | None = None
    metrics_retention_hours: int | None = None
    replay_retention_days: int | None = None
    # Aufnahme-Einstellungen (Screen-Agent + Guacamole)
    recording_enabled: str | None = None
    screen_record_quality: int | None = None
    screen_record_fps: int | None = None
    guac_record_quality: int | None = None
    guac_record_fps: int | None = None
    guac_record_scale: float | None = None
    guacd_host: str | None = None
    guacd_port: int | None = None
    # Automatic agent update ("1" = on, "0" = off; overridable per client)
    agent_auto_update: str | None = None
    # Auto-update also for clients that were offline, as soon as they
    # reconnect ("1" = on, default).
    agent_auto_update_offline: str | None = None
    # Blackbox "last state": agents report a desktop screenshot, metric deltas
    # and a log tail every X minutes, so the last state can be seen after a
    # client goes offline ("1" = on; interval in minutes).
    blackbox_enabled: str | None = None
    blackbox_interval_min: int | None = None
    # Spotify integration: client ID of a Spotify app (registered by the admin).
    # It lets users sign in in the audio player via OAuth (PKCE, no secret)
    # and play full tracks with Premium.
    spotify_client_id: str | None = None
    # Server-Selbst-Update (Settings -> Update)
    server_auto_update: str | None = None            # "1" | "0"
    server_auto_update_channel: str | None = None    # "commit" | "full" | "any"
    # UI design for users without their own choice (new users):
    # "futuristic" (desktop with windows) | "standard" (classic: navigation left, tabs)
    default_design: str | None = None
    # --- VPN (WireGuard-compatible) ---
    # Port and network are deliberately STRINGS: empty means "use default",
    # and that could not be expressed with int.


# These keys count as ADMIN settings (IP/port, server update, guacd address).
# Everything else is "default settings" (manage_settings).
ADMIN_SETTING_KEYS = {
    "server_host", "server_domain", "server_backend_port", "server_frontend_port",
    "server_url", "server_auto_update", "server_auto_update_channel",
    "guacd_host", "guacd_port",
    # The host lock controls who can reach the service at all - clearly
    # an admin matter, not 'manage_settings'.
    "host_lock_enabled", "host_lock_scope", "host_lock_extra",
    "host_lock_trust_proxy",
    # Der VPN-Endpunkt oeffnet einen Port nach aussen und bestimmt, wer ins
    # Netz der Clients kommt - das ist Admin-Sache, nicht 'manage_settings'.
}


@router.get("/hostlock/status")
def hostlock_status(request: Request, user: dict = Depends(get_current_user)):
    """
    Zeigt, welchen Host der Server bei DIESER Anfrage sieht und welche
    Adressen erlaubt sind. Erste Anlaufstelle, wenn die Sperre nicht greift.
    """
    require_perm(user, "see_settings")
    from app import hostlock
    return hostlock.diagnostics(request)


@router.get("/settings")
def get_settings(user: dict = Depends(get_current_user)):
    require_perm(user, "see_settings")
    return db.get_all_settings()


@router.put("/settings")
def update_settings(body: SettingsBody, user: dict = Depends(get_current_user)):
    changed = body.model_dump(exclude_none=True)
    # Require the matching permission per key: admin keys need
    # 'admin_settings', all others 'manage_settings'.
    if any(k in ADMIN_SETTING_KEYS for k in changed):
        require_perm(user, "admin_settings")
    if any(k not in ADMIN_SETTING_KEYS for k in changed):
        require_perm(user, "manage_settings")
    if "default_design" in changed and changed["default_design"] not in db.available_designs():
        raise HTTPException(400, "Unbekannter Style (verfuegbar: " + ", ".join(db.available_designs()) + ")")
    # server_url darf bewusst auf "" gesetzt werden (= wieder automatisch).
    for key, value in changed.items():
        db.set_setting(key, value)
    db.add_audit_entry(user["username"], "settings.updated", details=",".join(changed.keys()))
    return db.get_all_settings()


# ---------------------------------------------------------------------------
# Branding: Logos/Bilder per Upload ersetzen
# ---------------------------------------------------------------------------
# The files live in the frontend folder (served by StaticFiles).
# Upload as a RAW request body (no multipart) - this way we need no extra
# dependency (python-multipart) and the fetch in the frontend stays a
# one-liner (body: file).

from pathlib import Path as _Path

from app.paths import BRANDING_DIR as _IMAGES_DIR
# Uploaded branding files land in a WRITABLE data folder next to the
# database (the bundled frontend/images is read-only in many deployments,
# e.g. via a container image or nginx). When serving, this location is
# searched first, then the bundled default.
_BRANDING_STORE = _IMAGES_DIR   # uploads directly replace the files in frontend/image


def branding_path(name: str):
    """Path to an image: prefers the upload store, otherwise the bundled
    default from frontend/images. Returns None if nothing exists."""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    up = _BRANDING_STORE / name
    if up.is_file():
        return up
    d = _IMAGES_DIR / name
    return d if d.is_file() else None

# Whitelist: slot name -> (file name, allowed magic bytes, description)
_PNG = (b"\x89PNG\r\n\x1a\n",)
_JPG = (b"\xff\xd8\xff",)
_ICO = (b"\x00\x00\x01\x00", b"\x89PNG\r\n\x1a\n")  # .ico or PNG-as-favicon
BRANDING_SLOTS: dict[str, dict] = {
    "logo_r.png":   {"magic": _PNG, "label": "Logo small (topbar + browser icon)"},
    "logo.png":     {"magic": _PNG, "label": "Logo default"},
    "logo_big.png": {"magic": _PNG, "label": "Logo large"},
    "login-bg.jpg": {"magic": _JPG, "label": "Login background image"},
    "favicon.ico":  {"magic": _ICO, "label": "Favicon (.ico)"},
}
_MAX_BRANDING_BYTES = 8 * 1024 * 1024  # 8 MB is enough for any logo/background image


@router.get("/branding")
def list_branding(user: dict = Depends(get_current_user)):
    """Lists all replaceable branding files incl. modification time (cache busting)."""
    require_perm(user, "manage_branding")
    slots = []
    for name, meta in BRANDING_SLOTS.items():
        f = branding_path(name)
        slots.append({
            "name": name,
            "label": meta["label"],
            "url": f"/image/{name}",
            "exists": f is not None,
            "mtime": int(f.stat().st_mtime) if f is not None else 0,
        })
    return {"slots": slots}


@router.post("/branding/{name}")
async def upload_branding(name: str, request: Request, user: dict = Depends(get_current_user)):
    """
    Replaces a branding file. Body = raw image data (no multipart).
    Accepts ALL common image formats and converts server-side (Pillow) into
    the slot's target format (PNG/JPEG/ICO). Only whitelisted file names,
    a size limit, atomic replacement - the old file stays intact on errors.
    """
    require_perm(user, "manage_branding")
    slot = BRANDING_SLOTS.get(name)
    if not slot:
        raise HTTPException(404, f"Unbekannter Branding-Slot. Erlaubt: {', '.join(BRANDING_SLOTS)}")

    data = await request.body()
    if not data:
        raise HTTPException(400, "Leerer Upload")
    if len(data) > _MAX_BRANDING_BYTES:
        raise HTTPException(413, "File too large (max. 8 MB)")

    # Accept ALL image formats (PNG, JPEG, WebP, GIF, BMP, ICO, TIFF, ...)
    # and convert server-side into the slot's target format. The earlier
    # magic-byte check sometimes rejected files wrongly - now Pillow decides
    # whether it is a readable image, and the conversion guarantees the
    # correct format on disk.
    try:
        from PIL import Image
    except ImportError:
        raise HTTPException(500, "Pillow is not installed - please update the "
                                 "backend dependencies (requirements.txt).")
    import io as _io
    try:
        img = Image.open(_io.BytesIO(data))
        img.load()
    except Exception as e:
        raise HTTPException(400, f"Die Datei ist kein lesbares Bild "
                                 f"({e.__class__.__name__}: {e}).")

    target_ext = name.rsplit(".", 1)[-1].lower()
    out = _io.BytesIO()
    try:
        if target_ext == "png":
            img.convert("RGBA").save(out, format="PNG", optimize=True)
        elif target_ext in ("jpg", "jpeg"):
            # JPEG has no alpha: place transparent areas on white.
            if "A" in img.getbands():
                rgba = img.convert("RGBA")
                base = Image.new("RGB", rgba.size, (255, 255, 255))
                base.paste(rgba, mask=rgba.split()[-1])
                base.save(out, format="JPEG", quality=90)
            else:
                img.convert("RGB").save(out, format="JPEG", quality=90)
        elif target_ext == "ico":
            ic = img.convert("RGBA")
            sizes = [(s, s) for s in (16, 32, 48, 64) if s <= max(ic.size)] or [(16, 16)]
            ic.save(out, format="ICO", sizes=sizes)
        else:
            # Unknown slot type: save the original unchanged.
            out.write(data)
    except Exception as e:
        raise HTTPException(400, f"Konvertierung nach {target_ext.upper()} "
                                 f"fehlgeschlagen ({e.__class__.__name__}: {e}).")
    data = out.getvalue()
    src_fmt = (img.format or "unbekannt")

    try:
        _BRANDING_STORE.mkdir(parents=True, exist_ok=True)
        tmp = _BRANDING_STORE / (name + ".tmp-upload")
        tmp.write_bytes(data)
        os.replace(tmp, _BRANDING_STORE / name)  # atomar - nie halb geschriebene Logos
    except OSError as e:
        # Klartext-Fehler statt generischem 500 - z.B. wenn backend/ read-only
        # gemountet ist oder der Dienst-Benutzer keine Schreibrechte hat.
        print(f"[branding] Upload von '{name}' fehlgeschlagen: {e}")
        raise HTTPException(500, f"Speichern fehlgeschlagen ({e.__class__.__name__}: {e}). "
                                 f"Hat der Backend-Prozess Schreibrechte auf {_BRANDING_STORE}?")
    print(f"[branding] '{name}' ersetzt ({src_fmt} -> {name.rsplit('.', 1)[-1].upper()}, "
          f"{len(data)} bytes) -> {_BRANDING_STORE / name}")

    db.add_audit_entry(user["username"], "branding.updated", target=name,
                       details=f"{src_fmt} -> {name.rsplit('.', 1)[-1].upper()}, {len(data)} bytes")
    return {"ok": True, "name": name, "size": len(data),
            "mtime": int((_BRANDING_STORE / name).stat().st_mtime)}


# ------------------------------------------------------------------
# Default layouts (dashboard widgets & client panels) for ALL users
# ------------------------------------------------------------------
# An admin can save their current layout as the organization-wide default.
# Every user gets this default as long as they have no layout of their own
# OR when they deliberately click "reset to default" in the UI. Existing own
# layouts stay untouched - so one can keep working. Stored as JSON in the
# settings KV store.

class DefaultLayoutBody(BaseModel):
    kind: str            # "dash" (client panels) | "fleet" (dashboard widgets)
    layout: dict | list  # the complete layout object or the widget list


_LAYOUT_SETTING_KEYS = {
    "dash": "default_layout_dash",
    "fleet": "default_layout_fleet",
    # Organization-wide layout profile PRESETS for the client view
    # (Settings -> profile "Physical"/"VMs"/"LXCs"; overridable by the admin).
    "dash_profile_physical": "default_layout_dash_profile_physical",
    "dash_profile_vm": "default_layout_dash_profile_vm",
    "dash_profile_lxc": "default_layout_dash_profile_lxc",
}


@router.get("/default-layouts")
def get_default_layouts(user: dict = Depends(get_current_user)):
    """
    Returns the organization-wide default layouts (or null if none is set).
    Readable for EVERY logged-in user - the frontend needs them on first
    start and when resetting.
    """
    out = {}
    for kind, key in _LAYOUT_SETTING_KEYS.items():
        raw = db.get_setting(key)
        if raw:
            try:
                out[kind] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                out[kind] = None
        else:
            out[kind] = None
    return out


@router.post("/default-layouts")
def set_default_layout(body: DefaultLayoutBody, user: dict = Depends(get_current_user)):
    """Speichert ein Layout als neuen organisationsweiten Standard (nur Admin)."""
    require_admin(user)
    key = _LAYOUT_SETTING_KEYS.get(body.kind)
    if not key:
        raise HTTPException(400, "Invalid layout type (dash|fleet)")
    db.set_setting(key, json.dumps(body.layout))
    db.add_audit_entry(user["username"], "settings.default_layout_set", target=body.kind)
    return {"ok": True, "kind": body.kind}


@router.delete("/default-layouts/{kind}")
def clear_default_layout(kind: str, user: dict = Depends(get_current_user)):
    """Entfernt den organisationsweiten Standard wieder (nur Admin)."""
    require_admin(user)
    key = _LAYOUT_SETTING_KEYS.get(kind)
    if not key:
        raise HTTPException(400, "Invalid layout type (dash|fleet)")
    db.set_setting(key, "")
    db.add_audit_entry(user["username"], "settings.default_layout_cleared", target=kind)
    return {"ok": True, "kind": kind}
