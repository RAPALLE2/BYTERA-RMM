#!/usr/bin/env bash
# ======================================================================
#  BYTERA Client-Provisioning (Linux)
#  Erzeugt vom Skript-Modul. Läuft als root (systemd-run), kann aber auch von
#  Hand gestartet werden:  sudo bash run.sh
#  Fortschritt: /var/lib/bytera-provision/status.json + log.txt
#  Unterstützt: apt (Debian/Ubuntu), dnf/yum (RHEL/Fedora/Alma/Rocky),
#               zypper (openSUSE), pacman (Arch), apk (Alpine)
# ======================================================================
__BYTERA_CONFIG__

BASE=/var/lib/bytera-provision
mkdir -p "$BASE"
LOG="$BASE/log.txt"
STATUS="$BASE/status.json"
N=0; ERR=0; NEED_REBOOT=0
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a LC_ALL=C

log()    { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >>"$LOG"; }
status() { printf '{"state":"%s","step":"%s","n":%d,"total":%d,"errors":%d,"reboot":%s}\n' \
             "$1" "$2" "$N" "$TOTAL" "$ERR" "$([ "$NEED_REBOOT" = 1 ] && echo true || echo false)" >"$STATUS"; }
step()   { N=$((N+1)); log "=== [$N/$TOTAL] $1"; status running "$1"; }
run()    { log "\$ $*"; "$@" >>"$LOG" 2>&1; local rc=$?; [ $rc -ne 0 ] && { ERR=$((ERR+1)); log "FEHLER: Exit $rc"; }; return $rc; }
has()    { command -v "$1" >/dev/null 2>&1; }

if [ "$(id -u)" -ne 0 ]; then echo "Bitte als root ausführen" >&2; exit 1; fi

log "BYTERA Provisioning gestartet auf $(hostname)"
step "Vorbereitung"
PM=""
for p in apt-get dnf yum zypper pacman apk; do has "$p" && { PM=$p; break; }; done
[ -r /etc/os-release ] && . /etc/os-release
log "System: ${PRETTY_NAME:-unbekannt} · Paketverwaltung: ${PM:-keine}"
case "$PM" in
  apt-get) FAM=apt ;; dnf|yum) FAM=dnf ;; zypper) FAM=zypper ;; pacman) FAM=pacman ;; apk) FAM=apk ;; *) FAM="" ;;
esac
APT_OPTS=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

pm_update() {
  case "$PM" in
    apt-get) run apt-get update ;;
    dnf|yum) run "$PM" -y makecache ;;
    zypper)  run zypper --non-interactive refresh ;;
    pacman)  run pacman -Sy --noconfirm ;;
    apk)     run apk update ;;
  esac
}
pm_install() {   # pm_install paket...
  [ $# -eq 0 ] && return 0
  case "$PM" in
    apt-get) run apt-get install "${APT_OPTS[@]}" "$@" ;;
    dnf|yum) run "$PM" install -y "$@" ;;
    zypper)  run zypper --non-interactive install --no-recommends "$@" ;;
    pacman)  run pacman -S --noconfirm --needed "$@" ;;
    apk)     run apk add "$@" ;;
  esac
}
pm_install_each() {  # einzeln, damit ein fehlender Name nicht alles abbricht
  local ok=() p
  for p in "$@"; do
    if pm_install "$p"; then ok+=("$p"); else log "  nicht installiert: $p"; fi
  done
  log "  installiert/vorhanden: ${ok[*]:-keine}"
}
svc_enable() { has systemctl && run systemctl enable --now "$1"; has rc-update && { rc-update add "$1" default >/dev/null 2>&1; rc-service "$1" start >>"$LOG" 2>&1; }; return 0; }

if [ -n "$CUSTOM_PRE" ]; then
  step "Eigene Befehle (vorher)"
  bash -c "$CUSTOM_PRE" >>"$LOG" 2>&1 || { ERR=$((ERR+1)); log "FEHLER: eigene Befehle (vorher) Exit $?"; }
fi

if [ -n "$CFG_HOSTNAME" ] || [ -n "$CFG_TIMEZONE" ]; then
  step "Hostname & Zeitzone"
  if [ -n "$CFG_TIMEZONE" ]; then
    if has timedatectl; then run timedatectl set-timezone "$CFG_TIMEZONE"
    elif [ -e "/usr/share/zoneinfo/$CFG_TIMEZONE" ]; then ln -sf "/usr/share/zoneinfo/$CFG_TIMEZONE" /etc/localtime; echo "$CFG_TIMEZONE" > /etc/timezone; log "  Zeitzone $CFG_TIMEZONE"; fi
  fi
  if [ -n "$CFG_HOSTNAME" ]; then
    SERIAL=$(cat /sys/class/dmi/id/product_serial 2>/dev/null | tr -cd 'A-Za-z0-9')
    NEWHN=$(printf '%s' "$CFG_HOSTNAME" | sed "s/{SERIAL}/${SERIAL}/g" | tr -cd 'A-Za-z0-9-' | cut -c1-63)
    if [ -n "$NEWHN" ] && [ "$NEWHN" != "$(hostname)" ]; then
      if has hostnamectl; then run hostnamectl set-hostname "$NEWHN"; else echo "$NEWHN" > /etc/hostname; hostname "$NEWHN"; fi
      grep -q "127.0.1.1" /etc/hosts && sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NEWHN/" /etc/hosts
      log "  Hostname: $NEWHN"
    fi
  fi
fi

if [ "$OPT_UPGRADE" = 1 ]; then
  step "System aktualisieren"
  pm_update
  case "$PM" in
    apt-get) if [ "$OPT_FULL_UPGRADE" = 1 ]; then run apt-get dist-upgrade "${APT_OPTS[@]}"; else run apt-get upgrade "${APT_OPTS[@]}"; fi ;;
    dnf|yum) run "$PM" upgrade -y ;;
    zypper)  if [ "$OPT_FULL_UPGRADE" = 1 ]; then run zypper --non-interactive dup; else run zypper --non-interactive update; fi ;;
    pacman)  run pacman -Syu --noconfirm ;;
    apk)     run apk upgrade ;;
  esac
else
  pm_update
fi

step "Pakete installieren"
case "$FAM" in
  apt) pm_install_each "${PKG_APT[@]}" ;; dnf) pm_install_each "${PKG_DNF[@]}" ;; zypper) pm_install_each "${PKG_ZYPPER[@]}" ;;
  pacman) pm_install_each "${PKG_PACMAN[@]}" ;; apk) pm_install_each "${PKG_APK[@]}" ;; *) log "  keine bekannte Paketverwaltung" ;;
esac

if [ "$OPT_GUEST_AGENT" = 1 ]; then
  step "Gast-Agent (Virtualisierung)"
  VIRT=$(systemd-detect-virt 2>/dev/null || echo none)
  log "  erkannt: $VIRT"
  case "$VIRT" in
    kvm|qemu) pm_install qemu-guest-agent && svc_enable qemu-guest-agent ;;
    vmware)   pm_install open-vm-tools && svc_enable vmtoolsd ;;
    *)        log "  kein Gast-Agent nötig" ;;
  esac
fi

if [ "$OPT_AUTO_UPDATES" = 1 ]; then
  step "Automatische Sicherheitsupdates"
  case "$PM" in
    apt-get)
      pm_install unattended-upgrades apt-listchanges
      printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\nAPT::Periodic::AutocleanInterval "7";\n' > /etc/apt/apt.conf.d/20auto-upgrades
      log "  unattended-upgrades aktiv" ;;
    dnf|yum)
      pm_install dnf-automatic
      [ -f /etc/dnf/automatic.conf ] && sed -i 's/^apply_updates.*/apply_updates = yes/; s/^upgrade_type.*/upgrade_type = security/' /etc/dnf/automatic.conf
      svc_enable dnf-automatic.timer ;;
    zypper) log "  openSUSE: bitte YaST Online Update / zypper-auto-patch nutzen (übersprungen)" ;;
    *) log "  für $PM nicht verfügbar" ;;
  esac
fi

if [ "$OPT_NTP" = 1 ]; then
  step "Zeitsynchronisation"
  if has timedatectl; then run timedatectl set-ntp true; else pm_install chrony && svc_enable chronyd; fi
fi

if [ "$OPT_SSH_HARDEN" = 1 ]; then
  step "SSH absichern"
  if [ -d /etc/ssh ]; then
    if [ -d /etc/ssh/sshd_config.d ] && grep -qi '^Include /etc/ssh/sshd_config.d' /etc/ssh/sshd_config; then
      printf 'PermitRootLogin prohibit-password\nPermitEmptyPasswords no\nX11Forwarding no\nMaxAuthTries 4\n' > /etc/ssh/sshd_config.d/90-bytera.conf
      log "  /etc/ssh/sshd_config.d/90-bytera.conf geschrieben"
    else
      sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/; s/^#\?PermitEmptyPasswords.*/PermitEmptyPasswords no/' /etc/ssh/sshd_config
      log "  sshd_config angepasst"
    fi
    if sshd -t >>"$LOG" 2>&1 || /usr/sbin/sshd -t >>"$LOG" 2>&1; then
      (systemctl reload ssh || systemctl reload sshd || rc-service sshd reload) >/dev/null 2>&1
    else
      ERR=$((ERR+1)); log "FEHLER: sshd-Konfiguration ungültig – nicht neu geladen"
    fi
  else
    log "  kein SSH-Server installiert"
  fi
fi

if [ "$OPT_UFW" = 1 ]; then
  step "Firewall"
  if [ "$PM" = apt-get ] || [ "$PM" = pacman ]; then
    pm_install ufw
    run ufw allow OpenSSH || run ufw allow 22/tcp
    run ufw --force enable
  elif [ "$PM" = dnf ] || [ "$PM" = yum ] || [ "$PM" = zypper ]; then
    pm_install firewalld && svc_enable firewalld
    run firewall-cmd --permanent --add-service=ssh && run firewall-cmd --reload
  else
    log "  für $PM nicht unterstützt"
  fi
fi

if [ "$OPT_FAIL2BAN" = 1 ]; then
  step "fail2ban"
  [ "$FAM" = dnf ] && ! "$PM" list installed epel-release >/dev/null 2>&1 && pm_install epel-release
  pm_install fail2ban && svc_enable fail2ban
fi

if [ "$OPT_DOCKER" = 1 ]; then
  step "Docker"
  if has docker; then log "  Docker ist bereits installiert: $(docker --version 2>/dev/null)"
  else
    has curl || pm_install curl
    curl -fsSL https://get.docker.com -o "$BASE/get-docker.sh" >>"$LOG" 2>&1 && run sh "$BASE/get-docker.sh" && svc_enable docker
  fi
fi

if [ -n "$CUSTOM_POST" ]; then
  step "Eigene Befehle (nachher)"
  bash -c "$CUSTOM_POST" >>"$LOG" 2>&1 || { ERR=$((ERR+1)); log "FEHLER: eigene Befehle (nachher) Exit $?"; }
fi

if [ "$OPT_AUTOREMOVE" = 1 ]; then
  step "Aufräumen"
  case "$PM" in
    apt-get) run apt-get autoremove --purge -y; run apt-get clean ;;
    dnf|yum) run "$PM" autoremove -y; run "$PM" clean all ;;
    zypper)  run zypper --non-interactive clean ;;
    pacman)  run pacman -Sc --noconfirm ;;
    apk)     run apk cache clean ;;
  esac
fi

step "Abschluss"
[ -f /var/run/reboot-required ] && NEED_REBOOT=1
if has needs-restarting; then needs-restarting -r >/dev/null 2>&1 || NEED_REBOOT=1; fi
log "Fertig · $ERR Fehler · Neustart nötig: $([ "$NEED_REBOOT" = 1 ] && echo ja || echo nein)"
DO_REBOOT=0
[ "$CFG_REBOOT" = always ] && DO_REBOOT=1
[ "$CFG_REBOOT" = needed ] && [ "$NEED_REBOOT" = 1 ] && DO_REBOOT=1
[ "$DO_REBOOT" = 1 ] && log "Neustart in 1 Minute"
status done "Fertig"
[ "$DO_REBOOT" = 1 ] && (shutdown -r +1 "BYTERA: Einrichtung abgeschlossen" >/dev/null 2>&1 || (sleep 60; reboot) &)
exit 0
