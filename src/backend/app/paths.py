"""
paths.py
--------
Zentrale Pfad-Definitionen des Projekts (Single Source of Truth).

Projektstruktur:

    <root>/
      src/
        agent/      Agent (main.py, requirements.txt, version.txt, lib/)
        backend/    Backend
          app/      Kernlogik
          router/   FastAPI-Router (ehemals app/routers)
          api/      (reserviert)
          lib/      eigene Bibliotheken
          data/     Datenbank, .env, version.txt, requirements.txt, repo.txt
          logs/     Logdateien
          storage/  ALLE Dateien (einziger Ablageort, siehe unten)
        frontend/   Webconsole (css, image, js/app, js/feature, js/lib, js/vendor)
        node/       Node-Module (werden an Agents ausgeliefert)

    storage/ (src/backend/storage) - der EINE Ort fuer alle Dateien:
      dist/agent/     -> rmm.url/dist/agent    (Agent-Installer)
      dist/client/    -> rmm.url/dist/client   (Client-App-Pakete, auch eigene Builds)
      dist/enroll/    -> rmm.url/dist/enroll   (Enrollment-Dateien)
      dist/<eigene>/  -> rmm.url/dist/<eigene>
      web/deploy/     -> rmm.url/web/deploy    (Deployment-Seite)
      web/<website>/  -> rmm.url/web/<website> (Websites des Webserver-Moduls)
      docker/<projekt>/ -> rmm.url/docker/<projekt> (Docker-Projekte)
      spaces/...      -> rmm.url/spaces/...    (nur mit Anmeldung)
      recordings/  tickets/  audio/  temp-usb/   (nur ueber die App erreichbar)
    Datenbank, .env, Einstellungen und Modul-Konfiguration bleiben in data/,
    Logs in logs/. Alte Orte (media/, temp/, storage/deployment, ...) werden
    beim Start automatisch nach storage/ verschoben (app/storage_layout.py).

Alle anderen Module importieren ihre Pfade von hier, damit eine
Strukturaenderung nur noch an EINER Stelle angepasst werden muss.
"""

from pathlib import Path

# …/src/backend/app/paths.py
BACKEND_DIR = Path(__file__).resolve().parents[1]      # …/src/backend
SRC_DIR = BACKEND_DIR.parent                           # …/src
PROJECT_ROOT = SRC_DIR.parent                          # Projekt-Wurzel

AGENT_DIR = SRC_DIR / "agent"
FRONTEND_DIR = SRC_DIR / "frontend"
BRANDING_DIR = FRONTEND_DIR / "image"                  # Branding = frontend/image (Defaults UND Uploads)
NODE_DIR = SRC_DIR / "node"                            # Node-Module (node_*.py)
MODULES_DIR = SRC_DIR / "modules"                      # Optionale Module: modules/<id>/module.json + Unterordner backend/ frontend/ node/
FILES_DIR = SRC_DIR / "files"                          # ALT/unbenutzt - nur noch fuer die Aufraeum-Liste

# --- Backend-Unterordner ---------------------------------------------------
APP_DIR = BACKEND_DIR / "app"
BRANDING_DEFAULTS_DIR = APP_DIR / "assets" / "branding"   # Standard-Logos (BYTERA); ersetzen unveraenderte alte Logos in frontend/image
ROUTER_DIR = BACKEND_DIR / "router"
LIB_DIR = BACKEND_DIR / "lib"

DATA_DIR = BACKEND_DIR / "data"
DB_FILE = DATA_DIR / "data.sqlite"
DB_BACKUP_DIR = DATA_DIR / "backup"
DB_BACKUP_FILE = DB_BACKUP_DIR / "data.sqlite"         # Online-Backup der DB
DBCONFIG_FILE = DATA_DIR / "dbconfig.json"
MODULES_STATE_FILE = DATA_DIR / "modules.json"          # aktivierte Module, Modul-Einstellungen, Crash-Zusammenfassung
MODULES_DATA_DIR = DATA_DIR / "modules"                # eigener Datenordner je Modul
ENV_FILE = DATA_DIR / ".env"
BACKEND_VERSION_FILE = DATA_DIR / "version.txt"
BACKEND_REQUIREMENTS_FILE = DATA_DIR / "requirements.txt"
REPO_FILE = DATA_DIR / "repo.txt"

STORAGE_DIR = BACKEND_DIR / "storage"
# Einheitliche storage-Struktur:
#   storage/web/<name>/     alle Websites (Modul Webserver); web/deployment = Deployment
#   storage/dist/<ordner>/  Verteilung (/dist: enrollment, agent, client, ...)
#   storage/spaces/...      Spaces (persoenlich/teams/public)
#   storage/docker/<projekt>/  Docker-Projekte (compose) des Docker-Moduls
WEB_DIR = STORAGE_DIR / "web"                          # alle Website-Ordner
DEPLOYMENT_DIR = WEB_DIR / "deploy"                    # Deployment-Seite -> rmm.url/web/deploy
SPACES_DIR = STORAGE_DIR / "spaces"
DOCKER_PROJECTS_DIR = STORAGE_DIR / "docker"           # eigene Docker-Projekte je Name

LOGS_DIR = BACKEND_DIR / "logs"
BACKEND_LOG_DIR = LOGS_DIR / "backend"                 # Diagnose-Logs, startup.log
MODULE_CRASH_DIR = LOGS_DIR / "modules"                # Crashberichte je Modul

MEDIA_DIR = BACKEND_DIR / "media"                      # ALT - Inhalt wandert nach storage/
RECORDINGS_DIR = STORAGE_DIR / "recordings"            # Screen-/Terminal-Aufzeichnungen
MEDIA_IMAGES_DIR = STORAGE_DIR / "images"              # reserviert (kein Branding!), wird nicht angelegt
AUDIO_DIR = STORAGE_DIR / "audio"                      # Medien-Bibliothek des Audio-Players
TICKET_FILES_DIR = STORAGE_DIR / "tickets"             # Ticket-Anhaenge

TEMP_DIR = BACKEND_DIR / "temp"                        # ALT/unbenutzt - nur noch fuer die Aufraeum-Liste
DIST_DIR = STORAGE_DIR / "dist"                        # Verteilung (/dist): agent, client, enroll, ...
DIST_AGENT_DIR = DIST_DIR / "agent"                    # gebaute Agent-Installer
DIST_CLIENT_DIR = DIST_DIR / "client"                  # Client-App: hochgeladen + selbst gebaut
DIST_ENROLL_DIR = DIST_DIR / "enroll"                  # Enrollment-Dateien
TEMP_USB_DIR = STORAGE_DIR / "temp-usb"                # Digitaler USB-Stick je Benutzer/Gruppe (/storage/temp-usb/<id>)

# --- Agent -----------------------------------------------------------------
AGENT_MAIN_FILE = AGENT_DIR / "main.py"
AGENT_VERSION_FILE = AGENT_DIR / "version.txt"
