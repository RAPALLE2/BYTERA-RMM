# node_module.py (node part of module "wol")
# ------------------------------------------
# Sends Wake-on-LAN packets IN THIS NODE'S NETWORK. Command "wol"
# {macs:[...], broadcasts:[...], port:int} -> {sent, errors, ok_count}.
# Formerly hard-wired in node/main.py, a loadable node module since node 0.2.0.
import socket


def _magic_packet(mac: str) -> bytes:
    hexmac = mac.replace(":", "").replace("-", "").replace(".", "").strip()
    if len(hexmac) != 12:
        raise ValueError(f"Invalid MAC: {mac}")
    return b"\xff" * 6 + bytes.fromhex(hexmac) * 16


def send_wol(ctx, data: dict) -> dict:
    macs = [m for m in (data.get("macs") or ([data["mac"]] if data.get("mac") else [])) if m]
    if not macs:
        raise ValueError("Keine MAC-Adresse angegeben")
    ports = data.get("ports") or [int(data.get("port") or 9), 7]
    targets = data.get("broadcasts") or ["255.255.255.255"]
    sent, errors = [], []
    for mac in macs:
        try:
            pkt = _magic_packet(mac)
        except ValueError as e:
            errors.append(str(e))
            continue
        for bc in targets:
            for port in ports:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
                        sk.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        sk.sendto(pkt, (bc, int(port)))
                    sent.append(f"{mac} -> {bc}:{port}")
                except OSError as e:
                    errors.append(f"{mac} -> {bc}:{port}: {e}")
    return {"sent": sent, "errors": errors, "ok_count": len(sent)}


OPS = {"wol": send_wol}


def start(ctx):
    return {"ready": True}


def stop(ctx):
    return {"stopped": True}
