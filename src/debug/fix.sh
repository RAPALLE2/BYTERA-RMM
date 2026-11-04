#!/usr/bin/env bash
# =============================================================================
# fix.sh - Notfall-Neuinstallation von BYTERA RMM aus GitHub
# -----------------------------------------------------------------------------
# Was es macht (in dieser Reihenfolge):
#   1. Container stoppen (docker compose down).
#   2. Wichtige Daten sichern: src/backend/data, storage, media, logs,
#      src/frontend/image, src/files  ->  src/debug/backup/<zeitstempel>/
#   3. Neuesten Commit aus GitHub laden (Standard RAPALLE2/BYTERA-RMM, eigene
#      URL moeglich) und entpacken.
#   4. Den neuen Code an die richtige Stelle kopieren - die gesicherten
#      Datenordner bleiben dabei unangetastet.
#   5. Container wieder starten (docker compose up -d --build).
#
# Aufruf (aus beliebigem Ordner):
#   bash src/debug/fix.sh
#
# Es wird NICHTS geloescht, was nicht vorher gesichert wurde. Schlaegt der
# Download fehl, bleibt der alte Stand unveraendert.
# =============================================================================
set -euo pipefail

# --- Projektwurzel bestimmen (dieses Skript liegt in src/debug/) -------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"     # .../  (enthaelt src/, docker-compose.yml)
cd "$ROOT"

echo "=============================================="
echo " BYTERA RMM - Notfall-Neuinstallation (fix.sh)"
echo " Projektordner: $ROOT"
echo "=============================================="

# Beim Update zu ERHALTENDE Ordner (identisch zum eingebauten Updater).
PRESERVE="src/backend/data src/backend/media src/backend/storage src/frontend/image src/backend/logs src/files"

# --- docker compose Befehl ermitteln ----------------------------------------
DC=""
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
fi

# --- GitHub-URL abfragen -----------------------------------------------------
DEFAULT_URL="https://github.com/RAPALLE2/BYTERA-RMM"
printf "GitHub-URL eingeben, leer lassen fuer %s: " "$DEFAULT_URL"
read -r URL || true
[ -z "${URL:-}" ] && URL="$DEFAULT_URL"

# owner/repo aus der URL herausloesen (akzeptiert .git am Ende und Pfade dahinter)
CLEAN="${URL%.git}"
CLEAN="${CLEAN#http://}"; CLEAN="${CLEAN#https://}"; CLEAN="${CLEAN#github.com/}"
OWNER="$(printf '%s' "$CLEAN" | cut -d/ -f1)"
REPO="$(printf '%s' "$CLEAN" | cut -d/ -f2)"
if [ -z "$OWNER" ] || [ -z "$REPO" ]; then
    echo "FEHLER: Konnte owner/repo aus '$URL' nicht lesen." >&2
    exit 1
fi
echo "-> Quelle: $OWNER/$REPO"

# --- 1) Container stoppen ----------------------------------------------------
echo
echo "[1/5] Container stoppen ..."
if [ -n "$DC" ] && [ -f "$ROOT/docker-compose.yml" ]; then
    $DC down || echo "   (docker compose down meldete einen Fehler - weiter)"
else
    echo "   (kein docker compose gefunden - Schritt uebersprungen)"
fi
# Nachbar-Container, die das Backend selbst anlegt, ebenfalls anhalten.
if command -v docker >/dev/null 2>&1; then
    for c in bytera-rmm bytera-rmm-guacd bytera-rmm-db bytera-guacd; do
        docker stop "$c" >/dev/null 2>&1 || true
    done
fi

# --- 2) Daten sichern --------------------------------------------------------
STAMP="$(date +%Y-%m-%d_%H%M%S)"
BACKUP="$ROOT/src/debug/backup/$STAMP"
echo
echo "[2/5] Daten sichern nach $BACKUP ..."
mkdir -p "$BACKUP"
for rel in $PRESERVE; do
    if [ -e "$ROOT/$rel" ]; then
        mkdir -p "$BACKUP/$(dirname "$rel")"
        cp -a "$ROOT/$rel" "$BACKUP/$rel"
        echo "   gesichert: $rel"
    fi
done
echo "   Sicherung abgeschlossen."

# --- 3) Neuesten Commit laden + entpacken ------------------------------------
echo
echo "[3/5] Neuesten Stand von GitHub laden ..."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ZIP="$TMP/src.zip"
OK=0
for REF in HEAD main master; do
    DL="https://codeload.github.com/$OWNER/$REPO/zip/$REF"
    echo "   versuche $DL"
    if command -v curl >/dev/null 2>&1; then
        if curl -fsSL "$DL" -o "$ZIP"; then OK=1; break; fi
    elif command -v wget >/dev/null 2>&1; then
        if wget -q "$DL" -O "$ZIP"; then OK=1; break; fi
    else
        echo "FEHLER: weder curl noch wget vorhanden." >&2; exit 1
    fi
done
[ "$OK" = "1" ] || { echo "FEHLER: Download fehlgeschlagen." >&2; exit 1; }

if command -v unzip >/dev/null 2>&1; then
    unzip -q "$ZIP" -d "$TMP"
else
    python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$ZIP" "$TMP"
fi
# GitHub entpackt nach <repo>-<ref>/ - den einzigen Unterordner finden.
SRCROOT="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d ! -name '*.zip' | head -n1)"
[ -n "$SRCROOT" ] && [ -d "$SRCROOT/src" ] || { echo "FEHLER: Entpackter Ordner sieht falsch aus." >&2; exit 1; }
echo "   entpackt: $SRCROOT"

# --- 4) Neuen Code an die richtige Stelle kopieren ---------------------------
echo
echo "[4/5] Neuen Code einspielen (Datenordner bleiben erhalten) ..."
# Robust kopieren: pro Datei ein eigener Versuch; ein einzelner Fehler darf das
# Einspielen NICHT abbrechen (deshalb kein pipefail-empfindliches while). Erst in
# ein temporaeres Verzeichnis sammeln, dann hineinkopieren.
copied=0; failed=0
set +e
while IFS= read -r -d '' f; do
    rel="${f#"$SRCROOT"/}"
    skip=0
    for pre in $PRESERVE; do
        case "$rel/" in "$pre/"*) skip=1;; esac
    done
    [ "$skip" = "1" ] && continue
    mkdir -p "$ROOT/$(dirname "$rel")" 2>/dev/null
    if cp -f "$f" "$ROOT/$rel" 2>/dev/null; then
        copied=$((copied+1))
    else
        failed=$((failed+1))
        echo "   WARNUNG: konnte nicht kopieren: $rel"
    fi
done < <(find "$SRCROOT" -type f -print0)
set -e
echo "   Code eingespielt ($copied Dateien, $failed Fehler)."

# --- 5) Container wieder starten ---------------------------------------------
echo
echo "[5/5] Container starten ..."
if [ -n "$DC" ] && [ -f "$ROOT/docker-compose.yml" ]; then
    $DC up -d --build
    echo
    echo "Fertig. Logs ansehen mit:  $DC logs -f rmm"
else
    echo "   (kein docker compose gefunden - bitte den Dienst von Hand starten)"
fi
echo
echo "Sicherung liegt unter: $BACKUP"
echo "Falls etwas schiefging, kann von dort zurueckkopiert werden."
