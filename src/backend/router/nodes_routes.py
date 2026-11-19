# router/nodes_routes.py
# ----------------------
# REST API of the nodes (Core). See app/node_manager.py.
#
#   GET    /api/nodes                          all nodes with state
#   GET    /api/nodes/package                  node package (ZIP) - ONLY for agents (X-Agent-Token)
#   GET    /api/nodes/modules                  modules with a node part (+ whether active)
#   GET    /api/nodes/{client_id}              state of a node
#   POST   /api/nodes/{client_id}/enable       install the node on the client (via the agent)
#   POST   /api/nodes/{client_id}/disable      remove the node
#   POST   /api/nodes/{client_id}/update       apply a new node package (agent updates the node)
#   POST   /api/nodes/{client_id}/refresh      re-query the state from the node
#   POST   /api/nodes/{client_id}/docker-install   install Docker on the node (Linux)
#   GET    /api/nodes/{client_id}/logs         last log lines of the node
#   POST   /api/nodes/{client_id}/modules/{id}           deploy a node module (body: {params})
#   DELETE /api/nodes/{client_id}/modules/{id}           remove a node module
#   POST   /api/nodes/{client_id}/modules/{id}/action    action (up/down/restart/status/logs ...)

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response

from app import bgjobs, db, modules, node_manager
from app.auth import get_current_user, is_super_admin, require_perm, user_has_permission
from app.config import AGENT_TOKEN

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


# The permission "manage_nodes" is a CLIENT permission: it applies per client
# (resp. via the default client "*" or globally for all). Whoever has it on at
# least one client sees the overview - but only with those clients - and the
# catalog of node modules.

def _perm(user: dict, client_id: str) -> None:
    require_perm(user, "manage_nodes", client_id)


def _allowed_ids(user: dict) -> set[str] | None:
    """Clients whose node the user may manage. None = all (super admin)."""
    if is_super_admin(user):
        return None
    return {c["id"] for c in db.list_clients()
            if user_has_permission(user, "manage_nodes", c["id"])}


def _perm_any(user: dict) -> set[str] | None:
    ids = _allowed_ids(user)
    if ids is not None and not ids:
        raise HTTPException(status_code=403, detail="Missing permission: manage_nodes")
    return ids


def _wrap(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _host(client_id: str) -> str:
    return (db.get_client(client_id) or {}).get("hostname") or client_id


async def _node_job(kind: str, client_id: str, title: str, user: dict, background: bool, fn,
                    key_extra: str = "", meta: dict | None = None):
    """Node action as a BACKEND JOB (app/bgjobs.py) - reloading aborts nothing,
    everyone authorized sees progress/result in the task view."""
    async def work(job):
        job.update(phase=kind, detail=title)
        try:
            return await fn(job)
        except (node_manager.NodeError, modules.ModuleError) as exc:
            raise _wrap(exc)
    return await bgjobs.run(f"node.{kind}", f"{client_id}{key_extra}", title=f"{title} ({_host(client_id)})",
                            user=user, work=work, background=background, client_id=client_id,
                            client_perm="manage_nodes",
                            meta={"client": client_id, **(meta or {})})


@router.get("")
async def list_nodes(user: dict = Depends(get_current_user)):
    ids = await db.call(_perm_any, user)
    nodes = await node_manager.list_states()
    if ids is not None:
        nodes = [n for n in nodes if n.get("client_id") in ids]
    return {"nodes": nodes, "package_hash": node_manager.package_hash(),
            "version": node_manager.node_version()}


@router.get("/package")
def package(x_agent_token: str | None = Header(default=None)):
    """The AGENT fetches the node package (not the browser) - protected by the agent token."""
    if not x_agent_token or not hmac.compare_digest(x_agent_token, AGENT_TOKEN):
        raise HTTPException(status_code=403, detail="Agent token missing or wrong")
    return Response(node_manager.package_zip(), media_type="application/zip",
                    headers={"X-Node-Hash": node_manager.package_hash()})


@router.get("/modules")
def node_modules(user: dict = Depends(get_current_user)):
    _perm_any(user)
    return {"modules": modules.node_modules()}


@router.get("/choices/{module_id}")
def node_choices(module_id: str, user: dict = Depends(get_current_user)):
    """Connected nodes for a module's "backend or node" choice.
    installed=False -> the node module must be loaded first."""
    return {"nodes": node_manager.node_choices(module_id, user),
            "has_node_part": any(m["id"] == module_id for m in modules.node_modules())}


@router.get("/{client_id}")
async def get_node(client_id: str, user: dict = Depends(get_current_user)):
    _perm(user, client_id)
    return await node_manager.get_state(client_id)


@router.post("/{client_id}/enable")
async def enable_node(client_id: str, background: bool = False, user: dict = Depends(get_current_user)):
    _perm(user, client_id)
    db.add_audit_entry(user["username"], "node.enable", target=client_id)
    return await _node_job("enable", client_id, "Install node", user, background,
                           lambda job: node_manager.enable(client_id))


@router.post("/{client_id}/disable")
async def disable_node(client_id: str, background: bool = False, user: dict = Depends(get_current_user)):
    _perm(user, client_id)
    db.add_audit_entry(user["username"], "node.disable", target=client_id)
    return await _node_job("disable", client_id, "Remove node", user, background,
                           lambda job: node_manager.disable(client_id))


@router.post("/{client_id}/update")
async def update_node(client_id: str, background: bool = False, user: dict = Depends(get_current_user)):
    _perm(user, client_id)
    db.add_audit_entry(user["username"], "node.update", target=client_id)
    return await _node_job("update", client_id, "Update node", user, background,
                           lambda job: node_manager.update(client_id))


@router.post("/{client_id}/refresh")
async def refresh_node(client_id: str, user: dict = Depends(get_current_user)):
    _perm(user, client_id)
    try:
        return await node_manager.refresh(client_id)
    except node_manager.NodeError as exc:
        raise _wrap(exc)


@router.post("/{client_id}/docker-install")
async def docker_install(client_id: str, background: bool = False, user: dict = Depends(get_current_user)):
    _perm(user, client_id)
    db.add_audit_entry(user["username"], "node.docker_install", target=client_id)

    async def fn(job):
        job.update(detail="Docker is being installed on the node (can take a few minutes)")
        data = await node_manager.docker_install(client_id)
        return {"result": data, "node": await node_manager.get_state(client_id)}
    return await _node_job("docker_install", client_id, "Install Docker", user, background, fn)


@router.get("/{client_id}/logs")
def node_logs(client_id: str, user: dict = Depends(get_current_user)):
    _perm(user, client_id)
    return {"lines": node_manager.logs(client_id)}


@router.post("/{client_id}/modules/{module_id}")
async def deploy_module(client_id: str, module_id: str, body: dict | None = None, background: bool = False,
                        user: dict = Depends(get_current_user)):
    _perm(user, client_id)
    db.add_audit_entry(user["username"], "node.module_deploy", target=client_id, details=module_id)

    async def fn(job):
        data = await node_manager.deploy_module(client_id, module_id, (body or {}).get("params") or {})
        return {"result": data, "node": await node_manager.get_state(client_id)}
    return await _node_job("module", client_id, f"Deploy node module '{module_id}'", user, background, fn,
                           key_extra=f":{module_id}", meta={"module": module_id})


@router.delete("/{client_id}/modules/{module_id}")
async def remove_module(client_id: str, module_id: str, background: bool = False,
                        user: dict = Depends(get_current_user)):
    _perm(user, client_id)
    db.add_audit_entry(user["username"], "node.module_remove", target=client_id, details=module_id)

    async def fn(job):
        data = await node_manager.remove_module(client_id, module_id)
        return {"result": data, "node": await node_manager.get_state(client_id)}
    return await _node_job("module_remove", client_id, f"Remove node module '{module_id}'", user, background, fn,
                           key_extra=f":{module_id}", meta={"module": module_id})


@router.post("/{client_id}/modules/{module_id}/action")
async def module_action(client_id: str, module_id: str, body: dict, background: bool = False,
                        user: dict = Depends(get_current_user)):
    _perm(user, client_id)
    action = str(body.get("action") or "")
    if not action:
        raise HTTPException(status_code=400, detail="action missing")
    async def fn(job):
        data = await node_manager.module_action(client_id, module_id, action, body.get("data") or {})
        return {"result": data, "node": await node_manager.get_state(client_id)}
    return await _node_job("module_action", client_id, f"Node module '{module_id}': {action}", user, background, fn,
                           key_extra=f":{module_id}:{action}", meta={"module": module_id, "action": action})
