import argparse
import socket
import json
import random
import time
import struct
from datetime import datetime, timezone

import logging
from logging.handlers import RotatingFileHandler
import os
import platform
import subprocess
import re

import psutil


# =========================
# Args / Logger
# =========================
def parse_args():
    parser = argparse.ArgumentParser(description="Cliente TCP - métricas + envío periódico")
    parser.add_argument("--client-id", required=True, help="ID del cliente (ej: LPZ, ORU, 1..9)")
    parser.add_argument("--server-ip", required=True, help="IP o hostname del servidor central")
    parser.add_argument("--interval", type=int, default=5, help="Intervalo de envío (segundos)")
    parser.add_argument("--server-port", type=int, default=5000, help="Puerto TCP del servidor (default: 5000)")
    parser.add_argument("--ack-timeout", type=int, default=3, help="Timeout esperando ACK (segundos)")
    return parser.parse_args()


def setup_logger():
    logger = logging.getLogger("client_logger")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    log_path = os.path.join(os.path.dirname(__file__), "client.log")
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


# =========================
# Helpers tiempo / disco
# =========================
def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def bytes_to_gb(b: int) -> float:
    return round(float(b / (1024 ** 3)), 2)


def get_uptime_seconds() -> int:
    # Robust: psutil.boot_time()
    try:
        boot = psutil.boot_time()
        return max(0, int(time.time() - boot))
    except Exception:
        return 0


# =========================
# Disk type detection (SSD/HDD/UNKNOWN)
# - Linux: /sys/block/<dev>/queue/rotational (0=SSD,1=HDD)
# - Windows: best-effort via PowerShell mapping drive letter -> media type
# =========================
def _linux_disk_type_from_device(device_path: str) -> str:
    # device_path like /dev/sda1, /dev/nvme0n1p2
    try:
        base = os.path.basename(device_path)
        # remove partition suffix:
        # sda1 -> sda
        # nvme0n1p2 -> nvme0n1
        base = re.sub(r"p?\d+$", "", base)
        rotational_path = f"/sys/block/{base}/queue/rotational"
        if os.path.exists(rotational_path):
            val = open(rotational_path, "r", encoding="utf-8").read().strip()
            if val == "0":
                return "SSD"
            if val == "1":
                return "HDD"
        return "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def _windows_disk_type_for_drive_letter(drive_letter: str) -> str:
    """
    Best-effort.
    Tries: (Get-Partition -DriveLetter C | Get-Disk | Get-PhysicalDisk).MediaType
    Returns SSD/HDD/UNKNOWN.
    """
    try:
        dl = drive_letter.upper().replace(":", "")
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-Partition -DriveLetter {dl} | Get-Disk | Get-PhysicalDisk).MediaType"
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=3).decode("utf-8", errors="ignore").strip()
        out = out.lower()
        # Possible values: SSD, HDD, Unspecified, SCM, etc.
        if "ssd" in out:
            return "SSD"
        if "hdd" in out:
            return "HDD"
        return "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def detect_disk_type(device: str, mountpoint: str) -> str:
    sysname = platform.system().lower()

    if "linux" in sysname:
        if device and device.startswith("/dev/"):
            return _linux_disk_type_from_device(device)
        return "UNKNOWN"

    if "windows" in sysname:
        # mountpoint usually "C:\\"; device sometimes "C:\\"
        try:
            mp = mountpoint or device or ""
            m = re.match(r"^([A-Za-z]):\\", mp)
            if not m:
                return "UNKNOWN"
            drive_letter = m.group(1)
            return _windows_disk_type_for_drive_letter(drive_letter)
        except Exception:
            return "UNKNOWN"

    return "UNKNOWN"


# =========================
# Lectura discos (MULTI)
# =========================
def list_all_disks():
    partitions = psutil.disk_partitions(all=False)
    if not partitions:
        return []

    ignore_fs = {"tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "cgroup", "cgroup2", "autofs"}

    disks = []
    seen = set()

    for p in partitions:
        fstype = (p.fstype or "").lower()
        if fstype in ignore_fs:
            continue

        mount = p.mountpoint
        device = p.device

        key = (device, mount)
        if key in seen:
            continue
        seen.add(key)

        try:
            usage = psutil.disk_usage(mount)
        except (PermissionError, FileNotFoundError, OSError):
            continue

        d_type = detect_disk_type(device=device, mountpoint=mount)

        disks.append({
            "disk_name": device,
            "mountpoint": mount,
            "disk_type": d_type,  # SSD/HDD/UNKNOWN
            "total_gb": bytes_to_gb(usage.total),
            "used_gb": bytes_to_gb(usage.used),
            "free_gb": bytes_to_gb(usage.free),
            "percent": round(float(usage.percent), 2),
        })

    return disks


def simulate_iops():
    return random.randint(50, 500)


# =========================
# Protocolo len-prefixed JSON
# =========================
def send_json_lenpref(sock: socket.socket, payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    msg = struct.pack(">I", len(encoded)) + encoded
    sock.sendall(msg)


def recv_exact(sock: socket.socket, size: int):
    data = b""
    while len(data) < size:
        packet = sock.recv(size - len(data))
        if not packet:
            return None
        data += packet
    return data


def receive_json_lenpref(sock: socket.socket):
    raw_len = recv_exact(sock, 4)
    if not raw_len:
        return None
    msglen = struct.unpack(">I", raw_len)[0]
    data = recv_exact(sock, msglen)
    if not data:
        return None
    return json.loads(data.decode("utf-8"))


# =========================
# Payload builder
# =========================
def build_metrics_payloads(client_id: str):
    disks = list_all_disks()

    if not disks:
        disks = [{
            "disk_name": "UNKNOWN",
            "mountpoint": None,
            "disk_type": "UNKNOWN",
            "total_gb": 0,
            "used_gb": 0,
            "free_gb": 0,
            "percent": 0
        }]

    ts = now_iso()
    uptime_sec = get_uptime_seconds()

    payloads = []
    for d in disks:
        payloads.append({
            "type": "METRIC",
            "node_id": str(client_id),
            "disk_name": d["disk_name"],
            "disk_type": d.get("disk_type", "UNKNOWN"),
            "total_gb": d["total_gb"],
            "used_gb": d["used_gb"],
            "free_gb": d["free_gb"],
            "percent": d.get("percent"),
            "iops": simulate_iops(),
            "mountpoint": d.get("mountpoint"),
            "timestamp": ts,               # timestamp lógico del reporte
            "uptime_sec": uptime_sec,      # para métricas de disponibilidad
            "sent_at": time.time(),        # epoch (para latencias del servidor)
        })

    return payloads


# =========================
# Conexión / comandos
# =========================
def connect_tcp(server_ip: str, server_port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(8)
    sock.connect((server_ip, server_port))
    sock.settimeout(None)
    return sock


def handle_command(cmd: dict, state: dict, logger: logging.Logger):
    cmd_id = cmd.get("cmd_id")
    action = cmd.get("action")

    logger.info(f"COMMAND recibido | cmd_id={cmd_id} | action={action} | payload={cmd}")

    if action == "SET_INTERVAL":
        value = cmd.get("value")
        try:
            value_int = int(value)
            if value_int <= 0:
                raise ValueError
            state["interval"] = value_int
            return True, f"Intervalo actualizado a {value_int}"
        except Exception:
            return False, f"Valor inválido para SET_INTERVAL: {value}"

    return False, f"Acción no soportada: {action}"


def send_cmd_ack(sock: socket.socket, client_id: str, cmd_id: str, ok: bool, message: str, logger: logging.Logger):
    payload = {
        "type": "CMD_ACK",
        "cmd_id": str(cmd_id),
        "node_id": str(client_id),
        "status": "OK" if ok else "ERROR",
        "message": message,
        "acked_at": now_iso(),
    }
    send_json_lenpref(sock, payload)
    logger.info(f"CMD_ACK enviado | cmd_id={cmd_id} | status={payload['status']} | message={message}")


def wait_for_ack_or_commands(sock: socket.socket, client_id: str, state: dict, logger: logging.Logger, ack_timeout: int):
    deadline = time.time() + ack_timeout

    while True:
        remaining = max(0.1, deadline - time.time())
        sock.settimeout(remaining)

        msg = receive_json_lenpref(sock)
        if not msg:
            raise ConnectionError("Conexión cerrada por el servidor")

        msg_type = msg.get("type")

        if msg_type == "COMMAND":
            cmd_id = msg.get("cmd_id")
            ok, info = handle_command(msg, state, logger)
            send_cmd_ack(sock, client_id=client_id, cmd_id=cmd_id, ok=ok, message=info, logger=logger)
            continue

        if msg_type == "ACK":
            sock.settimeout(None)
            logger.info(
                f"ACK recibido | node_id={msg.get('node_id')} | disk={msg.get('disk_name')} | status={msg.get('status')}"
            )
            return msg

        logger.warning(f"Mensaje desconocido recibido: {msg}")


def listen_for_commands_while_idle(sock: socket.socket, client_id: str, state: dict, logger: logging.Logger, seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        remaining = max(0.1, end - time.time())
        sock.settimeout(remaining)
        try:
            msg = receive_json_lenpref(sock)
            if not msg:
                continue
            if msg.get("type") == "COMMAND":
                cmd_id = msg.get("cmd_id")
                ok, info = handle_command(msg, state, logger)
                send_cmd_ack(sock, client_id=client_id, cmd_id=cmd_id, ok=ok, message=info, logger=logger)
        except socket.timeout:
            continue
        except Exception:
            # no tumbar el cliente por un comando malformado
            continue
    sock.settimeout(None)


# =========================
# Main loop
# =========================
def main():
    args = parse_args()
    logger = setup_logger()

    state = {"interval": int(args.interval)}

    logger.info(f"Iniciando cliente | node_id={args.client_id} | server={args.server_ip}:{args.server_port}")

    while True:
        try:
            print(f"🔌 Conectando a {args.server_ip}:{args.server_port} ...")
            sock = connect_tcp(args.server_ip, args.server_port)
            print("✅ Conectado. Iniciando envío periódico...")

            while True:
                payloads = build_metrics_payloads(args.client_id)

                for payload in payloads:
                    print(f"📤 Enviado: {payload}")
                    send_json_lenpref(sock, payload)

                    ack = wait_for_ack_or_commands(
                        sock,
                        client_id=args.client_id,
                        state=state,
                        logger=logger,
                        ack_timeout=args.ack_timeout
                    )
                    print(f"✅ ACK: {ack}")

                # Entre ciclos, escuchar comandos (idle)
                listen_for_commands_while_idle(sock, args.client_id, state, logger, seconds=float(state["interval"]))

        except KeyboardInterrupt:
            print("\n🛑 Cliente terminado por usuario.")
            return
        except Exception as e:
            logger.error(f"Error cliente: {e}")
            print(f"⚠️ Error: {e}. Reintentando en 2s...")
            time.sleep(2)


if __name__ == "__main__":
    main()