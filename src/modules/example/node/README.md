# Node part of the example module

This folder is sent by the backend to a **node** when the `example` module is active and `deploy` is
called in the backend part (or via the API `POST /api/nodes/<client>/modules/example`).

* `module.json` → the `node` section describes **how** the node runs the module (`kind: compose` or `python`).
* `docker-compose.yml` → started with `docker compose -p rmm-example up -d`.

More on this: `src/modules/backend/example/GUIDE.md`, section "Node part".
