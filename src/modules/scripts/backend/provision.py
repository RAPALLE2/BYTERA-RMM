"""
provision.py (Modul "scripts", Teilfunktion "provision") - Clients einrichten.

Eine Art "unattended.xml als externes Skript": In der Oberflaeche stellt man
ein, was passieren soll (Debloat, Effizienz, Standard-Apps, Updates, eigene
Befehle ...), speichert das als PRESET und rollt es auf beliebig viele Clients
aus. Erzeugt wird daraus ein eigenstaendiges Skript:

  Windows: PowerShell (provision_win.ps1 + eingebettete JSON-Konfiguration)
  Linux:   Bash       (provision_linux.sh + Variablenkopf)

Ausfuehrung OHNE Agent-Aenderung: Das Skript wird per normalem exec auf den
Client geschrieben und ENTKOPPELT gestartet (Windows: geplante Aufgabe als
SYSTEM, Linux: systemd-run bzw. setsid). Danach fragt das Backend alle paar
Sekunden status.json + neue Log-Zeilen ab. So sind auch Laeufe von mehreren
Stunden moeglich, und ein Agent-Neustart (z.B. durch Updates) bricht nichts ab.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shlex
import threading
import time
import uuid
from pathlib import Path

from app.paths import MODULES_DATA_DIR

HERE = Path(__file__).resolve().parent
DIR = MODULES_DATA_DIR / "scripts"
FILE = DIR / "provision_presets.json"
_lock = threading.RLock()

WIN_DIR = r"%ProgramData%\BYTERA\provision"
LIN_DIR = "/var/lib/bytera-provision"
TASK = "BYTERA-Provision"

# ======================================================================
# KATALOG - Grundlage fuer die Oberflaeche (alles hier, nichts doppelt im JS)
# ======================================================================
WIN_APPX = [  # (Paketname, Anzeige, standardmaessig entfernen)
    ("Microsoft.BingNews", "Microsoft News", True),
    ("Microsoft.BingWeather", "Wetter", True),
    ("Clipchamp.Clipchamp", "Clipchamp", True),
    ("Microsoft.GetHelp", "Hilfe anfordern", True),
    ("Microsoft.Getstarted", "Tipps", True),
    ("Microsoft.MicrosoftSolitaireCollection", "Solitaire Collection", True),
    ("Microsoft.People", "Kontakte", True),
    ("Microsoft.PowerAutomateDesktop", "Power Automate", True),
    ("Microsoft.Todos", "Microsoft To Do", True),
    ("Microsoft.WindowsFeedbackHub", "Feedback-Hub", True),
    ("Microsoft.WindowsMaps", "Karten", True),
    ("Microsoft.ZuneMusic", "Medienwiedergabe / Groove", False),
    ("Microsoft.ZuneVideo", "Filme & TV", True),
    ("Microsoft.YourPhone", "Smartphone-Link", True),
    ("Microsoft.MicrosoftOfficeHub", "Office-Hub / Microsoft 365", True),
    ("Microsoft.OutlookForWindows", "Outlook (neu)", False),
    ("MSTeams", "Teams (privat)", True),
    ("Microsoft.Copilot", "Copilot-App", True),
    ("Microsoft.549981C3F5F10", "Cortana", True),
    ("Microsoft.GamingApp", "Xbox-App", True),
    ("Microsoft.XboxGamingOverlay", "Xbox Game Bar", True),
    ("Microsoft.XboxApp", "Xbox Console Companion", True),
    ("Microsoft.Xbox.TCUI", "Xbox TCUI", True),
    ("Microsoft.XboxSpeechToTextOverlay", "Xbox Sprachüberlagerung", True),
    ("Microsoft.Windows.DevHome", "Dev Home", True),
    ("MicrosoftCorporationII.MicrosoftFamily", "Family Safety", True),
    ("MicrosoftCorporationII.QuickAssist", "Remotehilfe (Quick Assist)", False),
    ("Microsoft.SkypeApp", "Skype", True),
    ("Microsoft.MixedReality.Portal", "Mixed Reality-Portal", True),
    ("Microsoft.Microsoft3DViewer", "3D-Viewer", True),
    ("Microsoft.MSPaint", "Paint 3D", True),
    ("Microsoft.Office.OneNote", "OneNote (App)", False),
    ("Microsoft.Wallet", "Wallet", True),
    ("Microsoft.windowscommunicationsapps", "Mail & Kalender (alt)", False),
    ("Microsoft.WindowsAlarms", "Uhr / Wecker", False),
    ("Microsoft.WindowsSoundRecorder", "Sprachrekorder", False),
    ("Microsoft.MicrosoftStickyNotes", "Kurznotizen", False),
]

WIN_TWEAKS = [  # (id, Anzeige, Standard)
    ("telemetry", "Telemetrie auf Minimum + Dienst DiagTrack aus", True),
    ("consumer", "Werbung, App-Vorschläge & automatisch installierte Apps aus", True),
    ("bing", "Bing-/Websuche im Startmenü aus", True),
    ("copilot", "Copilot aus", True),
    ("recall", "Recall / KI-Analyse aus", True),
    ("widgets", "Widgets (Neuigkeiten & Interessen) aus", True),
    ("chat", "Chat-Symbol in der Taskleiste aus", True),
    ("lockscreen_tips", "Tipps & Werbung auf dem Sperrbildschirm aus", True),
    ("edge_firstrun", "Edge-Ersteinrichtung überspringen", True),
    ("file_ext", "Dateiendungen anzeigen", True),
    ("hidden_files", "Versteckte Dateien anzeigen", False),
    ("taskbar_left", "Taskleiste links ausrichten", False),
    ("classic_menu", "Klassisches Kontextmenü (Windows 11)", False),
    ("gamedvr", "Game DVR / Aufnahme im Hintergrund aus", True),
    ("darkmode", "Dunkles Design", False),
]

WIN_EFFICIENCY = [
    ("fast_startup_off", "Schnellstart aus (sauberer Neustart, weniger Probleme)", True),
    ("hibernate_off", "Ruhezustand-Datei entfernen (spart Speicherplatz)", False),
    ("visual_perf", "Visuelle Effekte auf Leistung", False),
    ("transparency_off", "Transparenz aus", False),
    ("startup_delay_off", "Autostart-Verzögerung aus", True),
    ("background_apps_off", "Store-Apps im Hintergrund aus", True),
    ("cleanup", "Aufräumen: Temp-Dateien + Komponentenspeicher (DISM)", True),
]

# Win11Debloat - nur Schalter aus der offiziellen CLI-Doku (Wiki "Command-line Interface")
W11D_PARAMS = [
    ("RemoveApps", "Standard-App-Auswahl entfernen", True),
    ("RemoveGamingApps", "Xbox-App & Game Bar entfernen", False),
    ("RemoveHPApps", "HP-Apps entfernen", False),
    ("DisableTelemetry", "Telemetrie & Tracking aus", True),
    ("DisableSuggestions", "Tipps & Vorschläge aus", True),
    ("DisableLockscreenTips", "Sperrbildschirm-Tipps aus", True),
    ("DisableBing", "Bing-Suche & Cortana aus", True),
    ("DisableCopilot", "Copilot aus", True),
    ("DisableRecall", "Recall aus", True),
    ("DisableEdgeAds", "Edge-Werbung aus", True),
    ("DisableSettings365Ads", "Microsoft 365-Werbung in Einstellungen aus", True),
    ("DisableStartRecommended", "Empfohlen im Startmenü aus", False),
    ("DisableWidgets", "Widgets aus", True),
    ("HideChat", "Chat ausblenden", True),
    ("HideTaskview", "Aktive Anwendungen-Knopf ausblenden", False),
    ("TaskbarAlignLeft", "Taskleiste links", False),
    ("RevertContextMenu", "Klassisches Kontextmenü", False),
    ("ShowKnownFileExt", "Dateiendungen anzeigen", True),
    ("ShowHiddenFolders", "Versteckte Ordner anzeigen", False),
    ("ExplorerToThisPC", "Explorer öffnet „Dieser PC“", False),
    ("DisableDVR", "Xbox Game DVR aus", True),
    ("DisableFastStartup", "Schnellstart aus", False),
    ("EnableDarkMode", "Dunkles Design", False),
    ("DisableAnimations", "Animationen aus", False),
    ("EnableEndTask", "„Task beenden“ in der Taskleiste", True),
    ("CreateRestorePoint", "Wiederherstellungspunkt vorher", False),
]

# Standard-Apps: winget-ID / Chocolatey-Paket / Ninite-Kürzel ("" = dort nicht verfügbar)
WIN_APPS = [
    ("chrome", "Google Chrome", "Browser", "Google.Chrome", "googlechrome", "chrome"),
    ("firefox", "Mozilla Firefox", "Browser", "Mozilla.Firefox", "firefox", "firefox"),
    ("thunderbird", "Thunderbird", "Kommunikation", "Mozilla.Thunderbird", "thunderbird", "thunderbird"),
    ("zoom", "Zoom", "Kommunikation", "Zoom.Zoom", "zoom", "zoom"),
    ("teams", "Microsoft Teams (Arbeit)", "Kommunikation", "Microsoft.Teams", "", ""),
    ("7zip", "7-Zip", "Werkzeuge", "7zip.7zip", "7zip", "7zip"),
    ("notepadpp", "Notepad++", "Werkzeuge", "Notepad++.Notepad++", "notepadplusplus", "notepadplusplus"),
    ("everything", "Everything (Suche)", "Werkzeuge", "voidtools.Everything", "everything", "everything"),
    ("windirstat", "WinDirStat", "Werkzeuge", "WinDirStat.WinDirStat", "windirstat", "windirstat"),
    ("greenshot", "Greenshot", "Werkzeuge", "Greenshot.Greenshot", "greenshot", "greenshot"),
    ("powertoys", "PowerToys", "Werkzeuge", "Microsoft.PowerToys", "powertoys", ""),
    ("keepass", "KeePass", "Werkzeuge", "DominikReichl.KeePass", "keepass", "keepass2"),
    ("vlc", "VLC media player", "Medien", "VideoLAN.VLC", "vlc", "vlc"),
    ("gimp", "GIMP", "Medien", "GIMP.GIMP", "gimp", "gimp"),
    ("libreoffice", "LibreOffice", "Office", "TheDocumentFoundation.LibreOffice", "libreoffice-fresh", "libreoffice"),
    ("acrobat", "Adobe Acrobat Reader", "Office", "Adobe.Acrobat.Reader.64-bit", "adobereader", ""),
    ("sumatra", "SumatraPDF", "Office", "SumatraPDF.SumatraPDF", "sumatrapdf", "sumatrapdf"),
    ("foxit", "Foxit PDF Reader", "Office", "Foxit.FoxitReader", "foxitreader", "foxit"),
    ("vscode", "Visual Studio Code", "Entwicklung", "Microsoft.VisualStudioCode", "vscode", "vscode"),
    ("git", "Git", "Entwicklung", "Git.Git", "git", ""),
    ("putty", "PuTTY", "Admin", "PuTTY.PuTTY", "putty", "putty"),
    ("winscp", "WinSCP", "Admin", "WinSCP.WinSCP", "winscp", "winscp"),
    ("filezilla", "FileZilla", "Admin", "TimKosse.FileZilla.Client", "filezilla", "filezilla"),
    ("teamviewer", "TeamViewer", "Admin", "TeamViewer.TeamViewer", "teamviewer", "teamviewer15"),
    ("anydesk", "AnyDesk", "Admin", "AnyDesk.AnyDesk", "anydesk", ""),
    ("vcredist", "Visual C++ Runtime 2015+", "Laufzeiten", "Microsoft.VCRedist.2015+.x64", "vcredist140", ""),
    ("dotnet8", ".NET 8 Desktop Runtime", "Laufzeiten", "Microsoft.DotNet.DesktopRuntime.8", "dotnet-8.0-desktopruntime", ""),
    ("java", "Java 21 (Temurin JRE)", "Laufzeiten", "EclipseAdoptium.Temurin.21.JRE", "temurin21jre", ""),
]

# Linux-Pakete: (id, Anzeige, Gruppe, Standard, {Paketfamilie: Name})
_S = lambda n: {"apt": n, "dnf": n, "zypper": n, "pacman": n, "apk": n}
LIN_PKGS = [
    ("curl", "curl", "Basis", True, _S("curl")),
    ("wget", "wget", "Basis", True, _S("wget")),
    ("ca", "CA-Zertifikate", "Basis", True, _S("ca-certificates")),
    ("gnupg", "GnuPG", "Basis", True, {"apt": "gnupg", "dnf": "gnupg2", "zypper": "gpg2", "pacman": "gnupg", "apk": "gnupg"}),
    ("unzip", "zip / unzip", "Basis", True, {"apt": "zip unzip", "dnf": "zip unzip", "zypper": "zip unzip", "pacman": "zip unzip", "apk": "zip unzip"}),
    ("sudo", "sudo", "Basis", True, _S("sudo")),
    ("nano", "nano", "Editor", True, _S("nano")),
    ("vim", "vim", "Editor", False, {"apt": "vim", "dnf": "vim-enhanced", "zypper": "vim", "pacman": "vim", "apk": "vim"}),
    ("htop", "htop", "Diagnose", True, _S("htop")),
    ("btop", "btop", "Diagnose", False, _S("btop")),
    ("ncdu", "ncdu", "Diagnose", True, _S("ncdu")),
    ("tree", "tree", "Diagnose", False, _S("tree")),
    ("jq", "jq", "Diagnose", True, _S("jq")),
    ("lsof", "lsof", "Diagnose", False, _S("lsof")),
    ("net", "net-tools (ifconfig …)", "Netzwerk", True, {"apt": "net-tools", "dnf": "net-tools", "zypper": "net-tools", "pacman": "net-tools", "apk": "net-tools"}),
    ("dns", "DNS-Werkzeuge (dig)", "Netzwerk", True, {"apt": "dnsutils", "dnf": "bind-utils", "zypper": "bind-utils", "pacman": "bind", "apk": "bind-tools"}),
    ("iputils", "ping / traceroute", "Netzwerk", True, {"apt": "iputils-ping traceroute", "dnf": "iputils traceroute", "zypper": "iputils traceroute", "pacman": "iputils traceroute", "apk": "iputils"}),
    ("rsync", "rsync", "Netzwerk", True, _S("rsync")),
    ("ssh", "OpenSSH-Server", "Dienste", False, {"apt": "openssh-server", "dnf": "openssh-server", "zypper": "openssh-server", "pacman": "openssh", "apk": "openssh"}),
    ("chrony", "chrony (Zeitsync)", "Dienste", False, _S("chrony")),
    ("git", "git", "Entwicklung", True, _S("git")),
    ("tmux", "tmux", "Entwicklung", False, _S("tmux")),
    ("build", "Compiler (build-essential)", "Entwicklung", False, {"apt": "build-essential", "dnf": "gcc gcc-c++ make", "zypper": "gcc gcc-c++ make", "pacman": "base-devel", "apk": "build-base"}),
    ("python", "Python 3 + pip", "Entwicklung", False, {"apt": "python3 python3-pip python3-venv", "dnf": "python3 python3-pip", "zypper": "python3 python3-pip", "pacman": "python python-pip", "apk": "python3 py3-pip"}),
]

LIN_OPTIONS = [
    ("upgrade", "Paketlisten aktualisieren + alle Pakete aktualisieren", True),
    ("full_upgrade", "Vollständiges Upgrade (dist-upgrade, inkl. neuer Abhängigkeiten)", True),
    ("autoremove", "Nicht mehr benötigte Pakete entfernen + Cache leeren", True),
    ("guest_agent", "Gast-Agent installieren, wenn VM erkannt (qemu-guest-agent / open-vm-tools)", True),
    ("auto_updates", "Automatische Sicherheitsupdates einschalten", True),
    ("ntp", "Zeitsynchronisation (NTP) einschalten", True),
    ("ufw", "Firewall (ufw/firewalld) an, SSH erlaubt", False),
    ("fail2ban", "fail2ban installieren & starten", False),
    ("ssh_harden", "SSH: root nur mit Schlüssel, keine leeren Passwörter", False),
    ("docker", "Docker (offizielles Installationsskript)", False),
]


def catalog() -> dict:
    return {
        "windows": {
            "appx": [{"id": a, "label": b, "default": c} for a, b, c in WIN_APPX],
            "tweaks": [{"id": a, "label": b, "default": c} for a, b, c in WIN_TWEAKS],
            "efficiency": [{"id": a, "label": b, "default": c} for a, b, c in WIN_EFFICIENCY],
            "w11d": [{"id": a, "label": b, "default": c} for a, b, c in W11D_PARAMS],
            "apps": [{"id": a, "label": b, "group": g, "winget": w, "choco": c, "ninite": n}
                     for a, b, g, w, c, n in WIN_APPS],
        },
        "linux": {
            "packages": [{"id": a, "label": b, "group": g, "default": d} for a, b, g, d, _ in LIN_PKGS],
            "options": [{"id": a, "label": b, "default": c} for a, b, c in LIN_OPTIONS],
        },
    }


def defaults(os_: str) -> dict:
    if os_ == "windows":
        return {
            "general": {"computer_name": "", "timezone": "W. Europe Standard Time", "restore_point": True,
                        "reboot": "needed"},
            "debloat": {"appx": [a for a, _, d in WIN_APPX if d], "appx_custom": [], "onedrive": False,
                        "tweaks": [a for a, _, d in WIN_TWEAKS if d]},
            "w11d": {"enabled": False, "params": [a for a, _, d in W11D_PARAMS if d], "extra": [],
                     "target": "default_and_users"},
            "efficiency": {"power_plan": "high", "monitor_ac": 15, "standby_ac": 0,
                           "options": [a for a, _, d in WIN_EFFICIENCY if d]},
            "optimizer": {"enabled": False,
                          "template": json.dumps({"WindowsVersion": 11, "Tweaks": {},
                                                  "PostAction": {"Restart": False}}, indent=2)},
            "apps": {"engine": "winget", "ids": ["chrome", "firefox", "7zip", "vlc", "notepadpp", "acrobat"],
                     "custom": [], "upgrade_all": True},
            "features": {"enable": [], "disable": []},
            "updates": {"install": True, "drivers": False},
            "custom": {"pre": "", "post": ""},
        }
    return {
        "general": {"hostname": "", "timezone": "Europe/Berlin", "reboot": "needed"},
        "options": [a for a, _, d in LIN_OPTIONS if d],
        "packages": [a for a, _, _, d, _ in LIN_PKGS if d],
        "packages_custom": [],
        "custom": {"pre": "", "post": ""},
    }


# ======================================================================
# Bereinigen / Pruefen der Konfiguration
# ======================================================================
_WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]*$")
_PARAM = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


class ProvisionError(ValueError):
    pass


def _merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        return {k: _merge(base[k], over[k]) if k in over else base[k] for k in base}
    return over if over is not None else base


def _words(v, what) -> list:
    out = []
    for x in (v if isinstance(v, list) else str(v or "").replace(",", "\n").split("\n")):
        x = str(x).strip()
        if not x:
            continue
        if not _WORD.match(x) or len(x) > 120:
            raise ProvisionError(f"{what}: ungültiger Eintrag „{x}“")
        out.append(x)
    return list(dict.fromkeys(out))


def normalize(os_: str, cfg: dict) -> dict:
    if os_ not in ("windows", "linux"):
        raise ProvisionError("Betriebssystem muss windows oder linux sein")
    c = _merge(defaults(os_), cfg or {})
    reboot = c["general"].get("reboot")
    if reboot not in ("never", "needed", "always"):
        c["general"]["reboot"] = "needed"
    if os_ == "windows":
        known = {a for a, *_ in WIN_APPX}
        c["debloat"]["appx"] = [a for a in c["debloat"]["appx"] if a in known]
        c["debloat"]["appx_custom"] = _words(c["debloat"]["appx_custom"], "Eigene Apps")
        c["debloat"]["tweaks"] = [t for t in c["debloat"]["tweaks"] if t in {a for a, *_ in WIN_TWEAKS}]
        c["efficiency"]["options"] = [t for t in c["efficiency"]["options"] if t in {a for a, *_ in WIN_EFFICIENCY}]
        if c["efficiency"]["power_plan"] not in ("", "balanced", "high", "ultimate"):
            c["efficiency"]["power_plan"] = ""
        for k in ("monitor_ac", "standby_ac"):
            v = c["efficiency"].get(k)
            c["efficiency"][k] = None if v in (None, "") else max(0, min(600, int(v)))
        c["w11d"]["params"] = [p for p in c["w11d"]["params"] if p in {a for a, *_ in W11D_PARAMS}]
        extra = _words(c["w11d"]["extra"], "Win11Debloat-Schalter")
        bad = [x for x in extra if not _PARAM.match(x.lstrip("-"))]
        if bad:
            raise ProvisionError(f"Win11Debloat: ungültiger Schalter {bad[0]}")
        c["w11d"]["extra"] = [x.lstrip("-") for x in extra]
        if c["w11d"]["target"] not in ("default", "default_and_users", "users"):
            c["w11d"]["target"] = "default_and_users"
        if c["optimizer"]["enabled"]:
            try:
                json.loads(c["optimizer"]["template"] or "{}")
            except json.JSONDecodeError as exc:
                raise ProvisionError(f"Optimizer-Vorlage ist kein gültiges JSON: {exc}")
        if c["apps"]["engine"] not in ("winget", "choco", "ninite"):
            c["apps"]["engine"] = "winget"
        c["apps"]["ids"] = [a for a in c["apps"]["ids"] if a in {x[0] for x in WIN_APPS}]
        c["apps"]["custom"] = _words(c["apps"]["custom"], "Eigene Pakete")
        c["features"]["enable"] = _words(c["features"]["enable"], "Windows-Features")
        c["features"]["disable"] = _words(c["features"]["disable"], "Windows-Features")
        name = str(c["general"].get("computer_name") or "").strip()
        if name and not re.match(r"^[A-Za-z0-9\-{}]{1,40}$", name):
            raise ProvisionError("Computername: nur Buchstaben, Ziffern, - und {SERIAL}")
        c["general"]["computer_name"] = name
        tz = str(c["general"].get("timezone") or "")
        if tz and not re.match(r"^[A-Za-z0-9 .+\-()]{1,60}$", tz):
            raise ProvisionError("Ungültige Zeitzone")
    else:
        c["options"] = [o for o in c["options"] if o in {a for a, *_ in LIN_OPTIONS}]
        c["packages"] = [p for p in c["packages"] if p in {a for a, *_ in LIN_PKGS}]
        c["packages_custom"] = _words(c["packages_custom"], "Eigene Pakete")
        hn = str(c["general"].get("hostname") or "").strip()
        if hn and not re.match(r"^[A-Za-z0-9\-{}]{1,63}$", hn):
            raise ProvisionError("Hostname: nur Buchstaben, Ziffern, - und {SERIAL}")
        c["general"]["hostname"] = hn
        tz = str(c["general"].get("timezone") or "")
        if tz and not re.match(r"^[A-Za-z0-9_+\-/]{1,60}$", tz):
            raise ProvisionError("Ungültige Zeitzone (z.B. Europe/Berlin)")
    for k in ("pre", "post"):
        c["custom"][k] = str(c["custom"].get(k) or "")[:50000]
    return c


# ======================================================================
# Skript erzeugen
# ======================================================================
def _win_steps(c) -> int:
    n = 3  # Vorbereitung, Abschluss, Aufraeumen der Hives
    n += bool(c["general"]["restore_point"]) + bool(c["general"]["computer_name"] or c["general"]["timezone"])
    n += bool(c["custom"]["pre"].strip()) + bool(c["debloat"]["appx"] or c["debloat"]["appx_custom"])
    n += bool(c["debloat"]["onedrive"]) + bool(c["debloat"]["tweaks"]) + bool(c["w11d"]["enabled"])
    n += 1 + bool(c["optimizer"]["enabled"]) + bool(c["features"]["enable"] or c["features"]["disable"])
    n += bool(c["apps"]["ids"] or c["apps"]["custom"] or c["apps"]["upgrade_all"])
    n += bool(c["updates"]["install"]) + bool(c["custom"]["post"].strip())
    return n


def render(os_: str, cfg: dict) -> str:
    c = normalize(os_, cfg)
    if os_ == "windows":
        apps = {a: {"winget": w, "choco": ch, "ninite": n, "label": b} for a, b, _g, w, ch, n in WIN_APPS}
        c["_apps"] = [apps[i] for i in c["apps"]["ids"]]
        c["_steps"] = _win_steps(c)
        c["_generated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tpl = (HERE / "provision_win.ps1").read_text(encoding="utf-8")
        blob = json.dumps(c, ensure_ascii=True)           # eine Zeile -> sicher im Here-String
        return tpl.replace("__BYTERA_CONFIG__", blob)
    # ---------- Linux: Variablenkopf
    fam = {}
    pk = {a: m for a, *_rest, m in LIN_PKGS}
    for f in ("apt", "dnf", "zypper", "pacman", "apk"):
        names = []
        for p in c["packages"]:
            names += pk[p][f].split()
        fam[f] = names + c["packages_custom"]
    q = shlex.quote
    opts = set(c["options"])
    steps = 3 + sum(1 for o in ("upgrade", "guest_agent", "auto_updates", "ntp", "ufw", "fail2ban",
                                "ssh_harden", "docker", "autoremove") if o in opts)
    steps += bool(c["general"]["hostname"] or c["general"]["timezone"]) + bool(c["custom"]["pre"].strip()) \
        + bool(c["custom"]["post"].strip())
    head = [f"# Erzeugt: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"TOTAL={steps}",
            f"CFG_HOSTNAME={q(c['general']['hostname'])}",
            f"CFG_TIMEZONE={q(c['general']['timezone'])}",
            f"CFG_REBOOT={q(c['general']['reboot'])}"]
    for o, *_ in LIN_OPTIONS:
        head.append(f"OPT_{o.upper()}={1 if o in opts else 0}")
    for f, names in fam.items():
        head.append(f"PKG_{f.upper()}=({' '.join(q(n) for n in names)})")
    head.append(f"CUSTOM_PRE={q(c['custom']['pre'])}")
    head.append(f"CUSTOM_POST={q(c['custom']['post'])}")
    tpl = (HERE / "provision_linux.sh").read_text(encoding="utf-8")
    return tpl.replace("__BYTERA_CONFIG__", "\n".join(head))


# ======================================================================
# Presets
# ======================================================================
def _load() -> dict:
    with _lock:
        try:
            return json.loads(FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"presets": {}}


def _save(d: dict) -> None:
    with _lock:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(FILE)


def list_presets() -> list:
    return sorted(_load()["presets"].values(), key=lambda p: (p["os"], p["name"].lower()))


def get_preset(pid: str) -> dict | None:
    return _load()["presets"].get(pid)


def save_preset(pid: str | None, name: str, os_: str, description: str, config: dict, user: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ProvisionError("Name fehlt")
    cfg = normalize(os_, config)
    with _lock:
        d = _load()
        if pid and pid not in d["presets"]:
            raise ProvisionError("Preset nicht gefunden")
        pid = pid or uuid.uuid4().hex[:12]
        old = d["presets"].get(pid, {})
        d["presets"][pid] = {"id": pid, "name": name[:100], "os": os_, "description": (description or "")[:500],
                             "config": cfg, "created": old.get("created") or int(time.time()),
                             "created_by": old.get("created_by") or user, "updated": int(time.time()),
                             "updated_by": user}
        _save(d)
        return d["presets"][pid]


def delete_preset(pid: str) -> None:
    with _lock:
        d = _load()
        d["presets"].pop(pid, None)
        _save(d)


# ======================================================================
# Ausfuehrung auf einem Client
# ======================================================================
def client_os(client: dict) -> str:
    return "windows" if "win" in str(client.get("platform") or "").lower() else "linux"


def _launch_cmd(os_: str, script: str) -> tuple[str, str]:
    """(Befehl, Shell) - schreibt das Skript auf den Client und startet es entkoppelt."""
    if os_ == "windows":
        b64 = base64.b64encode(b"\xef\xbb\xbf" + script.encode("utf-8")).decode()   # BOM -> PS 5.1 liest UTF-8
        lines = [f'>>"%D%\\run.b64" echo {b64[i:i + 4000]}' for i in range(0, len(b64), 4000)]
        cmd = "\r\n".join([
            f'set "D={WIN_DIR}"',
            'if not exist "%D%" mkdir "%D%"',
            f'schtasks /End /TN "{TASK}" >nul 2>&1',
            'del /q "%D%\\run.b64" "%D%\\run.ps1" "%D%\\status.json" "%D%\\log.txt" >nul 2>&1',
            *lines,
            'certutil -f -decode "%D%\\run.b64" "%D%\\run.ps1" >nul || (echo DECODE_FAILED & exit /b 3)',
            'del /q "%D%\\run.b64" >nul 2>&1',
            f'schtasks /Create /TN "{TASK}" /SC ONCE /ST 23:59 /RU SYSTEM /RL HIGHEST /F '
            '/TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \\"%D%\\run.ps1\\"" >nul || exit /b 4',
            f'schtasks /Run /TN "{TASK}" >nul || exit /b 5',
            "echo STARTED",
        ])
        return cmd, "cmd"
    b64 = base64.b64encode(script.encode("utf-8")).decode()
    body = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))
    cmd = (f"set -e\nD={LIN_DIR}\nmkdir -p \"$D\" && chmod 700 \"$D\"\n"
           "systemctl stop bytera-provision >/dev/null 2>&1 || true\n"
           "rm -f \"$D/status.json\" \"$D/log.txt\"\n"
           f"base64 -d > \"$D/run.sh\" <<'BYTERA_B64'\n{body}\nBYTERA_B64\n"
           "chmod 700 \"$D/run.sh\"\n"
           "if command -v systemd-run >/dev/null 2>&1 && [ -d /run/systemd/system ]; then\n"
           "  systemctl reset-failed bytera-provision >/dev/null 2>&1 || true\n"
           "  systemd-run --unit=bytera-provision --collect /bin/bash \"$D/run.sh\" >/dev/null\n"
           "else\n  nohup setsid /bin/bash \"$D/run.sh\" >/dev/null 2>&1 < /dev/null &\nfi\necho STARTED\n")
    return cmd, "auto"


def _poll_cmd(os_: str, skip: int) -> tuple[str, str]:
    if os_ == "windows":
        return ("$b = Join-Path $env:ProgramData 'BYTERA\\provision'\n"
                "Get-Content -Raw -Path (Join-Path $b 'status.json') -ErrorAction SilentlyContinue\n"
                "'---BYTERA-LOG---'\n"
                f"Get-Content -Path (Join-Path $b 'log.txt') -Encoding UTF8 -ErrorAction SilentlyContinue | "
                f"Select-Object -Skip {int(skip)} -First 400"), "powershell"
    return (f"cat {LIN_DIR}/status.json 2>/dev/null; echo; echo '---BYTERA-LOG---'; "
            f"tail -n +{int(skip) + 1} {LIN_DIR}/log.txt 2>/dev/null | head -n 400"), "auto"


def _stop_cmd(os_: str) -> tuple[str, str]:
    if os_ == "windows":
        return f'schtasks /End /TN "{TASK}" & schtasks /Delete /TN "{TASK}" /F', "cmd"
    return "systemctl stop bytera-provision 2>/dev/null; pkill -f /var/lib/bytera-provision/run.sh; true", "auto"


def _parse_poll(out: str) -> tuple[dict | None, list]:
    head, _, tail = (out or "").partition("---BYTERA-LOG---")
    st = None
    head = head.strip().lstrip("﻿")
    if head:
        try:
            st = json.loads(head)
        except json.JSONDecodeError:
            st = None
    lines = [l.rstrip("\r") for l in tail.lstrip("\r\n").split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    return st, lines


async def run_on_client(client_id: str, os_: str, script: str, log, *, progress=None, cancelled=None,
                        timeout: float = 8 * 3600, poll: float = 8.0) -> dict:
    """Startet das Skript und verfolgt es bis zum Ende. log(str) bekommt jede Zeile."""
    from app.sockets import request_exec

    cmd, shell = _launch_cmd(os_, script)
    try:
        r = await request_exec(client_id, cmd, timeout_seconds=90, shell=shell)
    except Exception as exc:
        return {"ok": False, "error": f"Start fehlgeschlagen: {exc}"}
    out = (r.get("stdout") or "") + (r.get("stderr") or "")
    if "STARTED" not in out:
        return {"ok": False, "error": f"Start fehlgeschlagen (Exit {r.get('code')}): {out.strip()[-600:]}"}
    log("Skript übertragen und gestartet")
    seen, started, last_change, offline_since = 0, time.time(), time.time(), None
    state = {}
    while True:
        if cancelled and cancelled():
            try:
                c2, sh2 = _stop_cmd(os_)
                await request_exec(client_id, c2, timeout_seconds=30, shell=sh2)
                log("Abgebrochen – Ausführung auf dem Client beendet")
            except Exception:
                log("Abgebrochen – Client nicht erreichbar, läuft dort evtl. weiter")
            return {"ok": False, "error": "abgebrochen", **state}
        await asyncio.sleep(poll)
        if time.time() - started > timeout:
            return {"ok": False, "error": f"Zeitüberschreitung nach {int(timeout / 3600)} h", **state}
        pc, psh = _poll_cmd(os_, seen)
        try:
            r = await request_exec(client_id, pc, timeout_seconds=45, shell=psh)
            offline_since = None
        except Exception as exc:
            offline_since = offline_since or time.time()
            if time.time() - offline_since > 1800:
                return {"ok": False, "error": f"Client seit 30 Min. nicht erreichbar ({exc})", **state}
            continue
        st, lines = _parse_poll(r.get("stdout") or "")
        for ln in lines:
            log(ln)
        if lines:
            seen += len(lines)
            last_change = time.time()
        if st is None:
            if time.time() - started > 300 and seen == 0:
                return {"ok": False, "error": "Skript hat nach 5 Min. noch nicht begonnen"}
            continue
        state = {"step": st.get("step"), "errors": int(st.get("errors") or 0)}
        if progress:
            progress(st)
        if st.get("state") in ("done", "failed"):
            # letzte Zeilen noch abholen
            try:
                pc, psh = _poll_cmd(os_, seen)
                _st, lines = _parse_poll((await request_exec(client_id, pc, timeout_seconds=45, shell=psh)).get("stdout") or "")
                for ln in lines:
                    log(ln)
            except Exception:
                pass
            ok = st.get("state") == "done"
            return {"ok": ok, "errors": state["errors"], "reboot": bool(st.get("reboot")),
                    "error": None if ok else (st.get("step") or "fehlgeschlagen")}
        if time.time() - last_change > 3 * 3600:
            return {"ok": False, "error": "Seit 3 Stunden keine Ausgabe mehr", **state}


async def queue_run(client_id: str, payload: dict) -> dict:
    """Offline-Warteschlange (Core, kind "mod:scripts:provision")."""
    lines: list = []
    try:
        script = render(payload["os"], payload.get("config") or {})
    except Exception as exc:
        return {"ok": False, "output": str(exc)}
    res = await run_on_client(client_id, payload["os"], script, lines.append)
    tail = "\n".join(lines[-60:])
    return {"ok": bool(res.get("ok")), "output": (f"{res.get('error')}\n" if res.get("error") else "") + tail}
