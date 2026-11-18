# BYTERA RMM

BYTERA RMM is a self-developed Remote Monitoring and Management (RMM) platform designed for technology enthusiasts, homelab operators, and individuals who manage their own servers and infrastructure projects.

The software provides tools for monitoring systems, managing devices remotely, executing administrative tasks, and maintaining an overview of small to medium-sized self-hosted environments.

## Important Notice

BYTERA RMM is a personal project and is not intended for enterprise, commercial, or regulated environments. It has not been designed, tested, or certified to comply with specific industry standards, security frameworks, legal requirements, or regulatory obligations.

While every effort is made to improve reliability and stability, software bugs, unexpected behavior, security issues, data loss, service interruptions, or other problems may occur.

**Use this software entirely at your own risk.**

By using BYTERA RMM, you acknowledge that:

- The software is provided "as is" without warranties of any kind.
- Functionality may change without notice.
- Errors and unexpected behavior can occur.
- The software may not be suitable for production or business-critical systems.
- The author assumes no responsibility for any damage, downtime, data loss, security incidents, or other consequences resulting from its use.

This project is best suited for learning, experimentation, homelabs, and personal infrastructure environments where users are comfortable managing and accepting the risks associated with self-hosted software.

Please keep in mind that parts of this project were vibe-coded, which means that it was done with artificial intelligenz.





## Installation

To install BYTERA RMM on your own server, download the latest release as a ZIP archive and extract it to a directory of your choice, and run the main.py file in src/backend.

### Requirements

- Python 3.11 or newer
- Administrative/root privileges
- Internet connection for initial dependency installation

### Installation Steps

1. Download and extract the latest release.
2. Open a terminal with admin/root privilages in the src/backend sub-directory.
3. Start the application using: python main.py
4. On first startup, BYTERA RMM automatically creates all required files, initializes the database, and installs any missing Python dependencies.
5. Open a browsertab with http://YOUR-SERVERS-IP:4000; standart account is user: admin, password: admin.

6. Please consider changing the AGENT_TOKEN and JWT_SECRET in the env file inside src/backend/data and src/agent.

### Installation with Docker

A `Dockerfile` and a complete `docker-compose.yml` are included.

```
cp .env.example .env        # optional: change port / timezone
docker compose up -d --build
```

Then open `http://YOUR-SERVERS-IP:4000` (default login `admin` / `admin`).

The project folder `src/` (backend, frontend, agent, node) is mounted into the
container as bind mounts. This keeps the database, recordings and your `.env`
outside the image and keeps the built-in source editor and the self-update
feature working.

An optional `guacd` service is included for browser-based RDP/VNC/SSH sessions.
Remove it from `docker-compose.yml` if you do not need remote sessions.

#### What cannot be changed at runtime in a container

Inside a container some settings are fixed the moment the container starts:

- **PORT** and **HOST** — the port mapping is defined in `docker-compose.yml`.
  Changing `PORT` in `src/backend/data/.env` will lock you out, because the container
  keeps forwarding the old port. To change the port, edit `RMM_PORT` in `.env`
  and run `docker compose up -d` again.
- **Volumes / data paths** — anything not stored in a mounted folder is lost
  when the image is rebuilt.

The web interface shows this as well: **Settings → Source** displays whether
the backend is *installed as a Docker container* or *installed as a program*,
which address it is bound to, and which settings are locked.






### VPN (WireGuard) – requirements per mode

The VPN prefers **wireguard-go**, the reference implementation by the
WireGuard developers. It runs entirely in userspace and needs **no kernel
module** – important on NAS systems, since WireGuard only landed in the Linux
kernel with 5.6. If a prerequisite is missing, the backend automatically falls
back to its built-in implementation and says so in the log and in the VPN app.
Nothing aborts.

**Docker (recommended)** – everything is prepared:

```bash
sudo modprobe tun          # once, if /dev/net/tun is missing
echo tun | sudo tee -a /etc/modules   # so it survives a reboot
docker compose up -d --build
```

The image ships `wireguard-go` and `wireguard-tools`; `NET_ADMIN` and
`/dev/net/tun` are already set in `docker-compose.yml`.
On **Synology/QNAP** the module is present but often not loaded after a reboot
– in that case add `modprobe tun` to the task scheduler
(trigger: boot-up, user: root).

**Linux without Docker:**

```bash
sudo apt install wireguard-go wireguard-tools iproute2
sudo modprobe tun
```

The backend needs `CAP_NET_ADMIN`. As a service this is enough:

```ini
[Service]
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
```

**Windows:** automatic setup is not implemented there yet. The backend runs
normally, and the VPN then uses the built-in implementation. If you need
WireGuard on Windows in production, set up the issued `.conf` with the official
client as a service:

```
wireguard.exe /installtunnelservice C:\path\to\file.conf
```

**Without any of this**, **port forwarding** still works (the "Port" button on
the client). It makes a service on the device reachable via the server, runs
over the existing agent connection and needs neither drivers nor open ports at
the customer – for VNC, RDP, SSH and web interfaces this is usually the simpler
way.

## Support & Feedback

Feel free to join the BYTERA Discord community:

https://dc.rapalle.net

You can open a ticket in the **Tickets** section to get in touch directly with the developer and discuss issues, suggestions, feature requests, or general questions about BYTERA RMM.

## Modules

The RMM core (dashboard, clients, updates, settings, permissions, terminal, screen view, task manager, source explorer)
runs **without any module**. Everything else is an optional module under `src/modules/<area>/<name>/`
(`backend`, `frontend`, ...). All modules are **disabled by default**; administrators switch them on in the **Modules** app
(start menu), where each module also has its own settings menu and crash reports.

| Module | Contains |
|---|---|
| `collab` | Tickets, chat, calendar & org chart, todos (each switchable) |
| `scripts` | Scripts, bulk actions, automation (each switchable) |
| `network` | Network scan & port scan, speed test, port forwarding (each switchable) |
| `patching` | Patch scans and installs |
| `recordings` | Session recordings and playback |
| `privacy` | GDPR export/erasure and retention (retention is enforced only while active) |
| `guacamole` | RDP/SSH/VNC via Guacamole incl. guacd container monitoring |
| `relay` | Explorer relay (FTP/SFTP), storage and deployment |
| `audioplayer`, `browser`, `gamehub`, `ai` | Audio player + Spotify, embedded browser, games, AI chat |
| `example` | Reference module with a full guide: `src/modules/backend/example/GUIDE.md` |

A crashing module never takes the backend down: it is deactivated automatically and a crash report is stored
(`src/backend/logs/modules/<id>/`). Python packages needed by a module are listed in its `module.json` (`pip`) and are
installed when the module is activated - `requirements.txt` only contains the core.
