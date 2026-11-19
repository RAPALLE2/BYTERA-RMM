"""
paths.py
--------
Central path definitions of the project (single source of truth).

Project structure:

    <root>/
      src/
        agent/      agent (main.py, requirements.txt, version.txt, lib/)
        backend/    backend
          app/      core logic
          router/   FastAPI routers (formerly app/routers)
          api/      (reserved)
          lib/      own libraries
          data/     database, .env, version.txt, requirements.txt, repo.txt
          logs/     log files
          storage/  ALL files (single storage location, see below)
        frontend/   web console (css, image, js/app, js/feature, js/lib, js/vendor)
        node/       node modules (shipped to agents)

    storage/ (src/backend/storage) - the ONE place for all files:
      dist/agent/     -> rmm.url/dist/agent    (agent installers)
      dist/client/    -> rmm.url/dist/client   (client-app packages, also own builds)
      dist/enroll/    -> rmm.url/dist/enroll   (enrollment files)
      dist/<own>/     -> rmm.url/dist/<own>
      web/deploy/     -> rmm.url/web/deploy    (deployment page)
      web/<website>/  -> rmm.url/web/<website> (websites of the webserver module)
      docker/<project>/ -> rmm.url/docker/<project> (Docker projects)
      spaces/...      -> rmm.url/spaces/...    (only with login)
      recordings/  tickets/  audio/  temp-usb/   (reachable only via the app)
    Database, .env, settings and module configuration stay in data/, logs in
    logs/. Old locations (media/, temp/, storage/deployment, ...) are moved to
    storage/ automatically on start (app/storage_layout.py).

All other modules import their paths from here, so a structural change only
has to be adjusted in ONE place.
"""

from pathlib import Path

# …/src/backend/app/paths.py
BACKEND_DIR = Path(__file__).resolve().parents[1]      # …/src/backend
SRC_DIR = BACKEND_DIR.parent                           # …/src
PROJECT_ROOT = SRC_DIR.parent                          # project root

AGENT_DIR = SRC_DIR / "agent"
FRONTEND_DIR = SRC_DIR / "frontend"
BRANDING_DIR = FRONTEND_DIR / "image"                  # branding = frontend/image (defaults AND uploads)
NODE_DIR = SRC_DIR / "node"                            # node modules (node_*.py)
MODULES_DIR = SRC_DIR / "modules"                      # optional modules: modules/<id>/module.json + subfolders backend/ frontend/ node/
FILES_DIR = SRC_DIR / "files"                          # OLD/unused - only for the cleanup list

# --- Backend-Unterordner ---------------------------------------------------
APP_DIR = BACKEND_DIR / "app"
BRANDING_DEFAULTS_DIR = APP_DIR / "assets" / "branding"   # default logos (BYTERA); replace unchanged old logos in frontend/image
ROUTER_DIR = BACKEND_DIR / "router"
LIB_DIR = BACKEND_DIR / "lib"

DATA_DIR = BACKEND_DIR / "data"
DB_FILE = DATA_DIR / "data.sqlite"
DB_BACKUP_DIR = DATA_DIR / "backup"
DB_BACKUP_FILE = DB_BACKUP_DIR / "data.sqlite"         # online backup of the DB
DBCONFIG_FILE = DATA_DIR / "dbconfig.json"
MODULES_STATE_FILE = DATA_DIR / "modules.json"          # enabled modules, module settings, crash summary
MODULES_DATA_DIR = DATA_DIR / "modules"                # own data folder per module
ENV_FILE = DATA_DIR / ".env"
BACKEND_VERSION_FILE = DATA_DIR / "version.txt"
BACKEND_REQUIREMENTS_FILE = DATA_DIR / "requirements.txt"
REPO_FILE = DATA_DIR / "repo.txt"

STORAGE_DIR = BACKEND_DIR / "storage"
# Unified storage structure:
#   storage/web/<name>/     all websites (webserver module); web/deployment = deployment
#   storage/dist/<folder>/  distribution (/dist: enrollment, agent, client, ...)
#   storage/spaces/...      spaces (personal/teams/public)
#   storage/docker/<project>/  Docker projects (compose) of the Docker module
WEB_DIR = STORAGE_DIR / "web"                          # all website folders
DEPLOYMENT_DIR = WEB_DIR / "deploy"                    # deployment page -> rmm.url/web/deploy
SPACES_DIR = STORAGE_DIR / "spaces"
DOCKER_PROJECTS_DIR = STORAGE_DIR / "docker"           # own Docker projects per name

LOGS_DIR = BACKEND_DIR / "logs"
BACKEND_LOG_DIR = LOGS_DIR / "backend"                 # diagnostic logs, startup.log
MODULE_CRASH_DIR = LOGS_DIR / "modules"                # crash reports per module

MEDIA_DIR = BACKEND_DIR / "media"                      # OLD - content moves to storage/
RECORDINGS_DIR = STORAGE_DIR / "recordings"            # screen/terminal recordings
MEDIA_IMAGES_DIR = STORAGE_DIR / "images"              # reserved (not branding!), not created
AUDIO_DIR = STORAGE_DIR / "audio"                      # media library of the audio player
TICKET_FILES_DIR = STORAGE_DIR / "tickets"             # ticket attachments

TEMP_DIR = BACKEND_DIR / "temp"                        # OLD/unused - only for the cleanup list
DIST_DIR = STORAGE_DIR / "dist"                        # distribution (/dist): agent, client, enroll, ...
DIST_AGENT_DIR = DIST_DIR / "agent"                    # built agent installers
DIST_CLIENT_DIR = DIST_DIR / "client"                  # client app: uploaded + self-built
DIST_ENROLL_DIR = DIST_DIR / "enroll"                  # enrollment files
TEMP_USB_DIR = STORAGE_DIR / "temp-usb"                # digital USB stick per user/group (/storage/temp-usb/<id>)

# --- Agent -----------------------------------------------------------------
AGENT_MAIN_FILE = AGENT_DIR / "main.py"
AGENT_VERSION_FILE = AGENT_DIR / "version.txt"
