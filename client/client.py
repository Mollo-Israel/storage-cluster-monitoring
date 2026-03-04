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


def parse_args():
    parser = argparse.ArgumentParser(description="Cliente TCP - métricas + envío periódico")
    parser.add_argument("--client-id", required=True, help="ID del cliente (ej: LPZ, CBB, ORU)")
    parser.add_argument("--server-ip", required=True, help="IP o hostname del servidor central")
    parser.add_argument("--interval", type=int, default=5, help="Intervalo de envío (segundos)")
    parser.add_argument("--server-port", type=int, default=5000, help="Puerto TCP del servidor (default: 5000)")
    parser.add_argument("--ack-timeout", type=int, default=3, help="Timeout esperando ACK (segundos)")
    parser.add_argument("--first-disk-only", action="store_true", help="Reporta solo el primer disco (modo compatibilidad práctica)")
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


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def bytes_to_gb(b: int) -> float:
    return round(float(b / (1024 ** 3)), 2)


def get_uptime_seconds() -> int:
    try:
        boot = psutil.boot_time()
        return max(0, int(time.time() - boot))
    except Exception:
        return 0


def _linux_disk_type_from_device(device_path: str) -> str:
    try:
        base = os.path.basename(device_path)
        base = re.sub(r"p?\d+$", "", base)
        rotational_path = f"/sys/block/{base}/queue/rotational"
        if os.path.exists(rotational_path):
            val = open(rotational_path, "r", encoding="utf-8").read().strip()
            if val == "0":
                return "SSD"
            if val == "1":
                return "HDD"
        return "HDD"  # regla simple
    except Exception:
        return "HDD"


def _windows_disk_type_for_drive_letter(drive_letter: str) -> str:
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
        if "ssd" in out:
            return "SSD"
        # todo lo demás => HDD por regla simple
        return "HDD"
    except Exception:
        return "HDD"


def detect_disk_type(device: str, mountpoint: str) -> str:
    sysname = platform.system().lower()
    if "linux" in sysname:
        if device and device.startswith("/dev/"):
            t = _linux_disk_type_from_device(device)
            return "SSD" if t == "SSD" else "HDD"
        return "HDD"
    if "windows" in sysname:
        try:
            mp = mountpoint or device or ""
            m = re.match(r"^([A-Za-z]):\\", mp)
            if not m:
                return "HDD"
            drive_letter = m.group(1)
            t = _windows_disk_type_for_drive_letter(drive_letter)
            return "SSD" if t == "SSD" else "HDD"
        except Exception:
            return "HDD"
    return "HDD"


def list_all_disks(first_only: bool):
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
            "disk_type": d_type,
            "total_gb": bytes_to_gb(usage.total),
            "used_gb": bytes_to_gb(usage.used),
            "free_gb": bytes_to_gb(usage.free),
            "percent": round(float(usage.percent), 2),
        })

        if first_only and disks:
            break

    return disks


def simulate_iops(disk_type: str):
    disk_type = (disk_type or "HDD").upper()
    if disk_type == "SSD":
        return random.randint(800, 4000)
    return random.randint(80, 350)


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


def build_metrics_payloads(client_id: str, first_only: bool):
    disks = list_all_disks(first_only=first_only)
    if not disks:
        disks = [{
            "disk_name": "UNKNOWN",
            "mountpoint": None,
            "disk_type": "HDD",
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
            "disk_type": d.get("disk_type", "HDD"),
            "total_gb": d["total_gb"],
            "used_gb": d["used_gb"],
            "free_gb": d["free_gb"],
            "percent": d.get("percent"),
            "iops": simulate_iops(d.get("disk_type")),
            "mountpoint": d.get("mountpoint"),
            "timestamp": ts,
            "uptime_sec": uptime_sec,
            "sent_at": time.time(),
        })
    return payloads


def connect_tcp(server_ip: str, server_port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(8)
    sock.connect((server_ip, server_port))
    sock.settimeout(None)
    return sock


def handle_command(cmd: dict, state: dict, logger: logging.Logger):
    cmd_id = cmd.get("cmd_id")
    action = (cmd.get("action") or "").upper()
    value = cmd.get("value")
    message = cmd.get("message")

    logger.info(f"COMMAND recibido | cmd_id={cmd_id} | action={action} | value={value} | message={message}")

    if action == "SET_INTERVAL":
        try:
            value_int = int(value)
            if value_int <= 0:
                raise ValueError
            state["interval"] = value_int
            return True, f"Intervalo actualizado a {value_int}s"
        except Exception:
            return False, f"Valor inválido para SET_INTERVAL: {value}"

    if action == "SHOW_MESSAGE":
        return True, f"Mensaje registrado: {message or ''}"

    if action == "PING":
        return True, "PONG"

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
        remaining = max(0.2, deadline - time.time())
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
            logger.info(f"ACK recibido | node_id={msg.get('node_id')} | disk={msg.get('disk_name')} | status={msg.get('status')}")
            return msg


def listen_for_commands_while_idle(sock: socket.socket, client_id: str, state: dict, logger: logging.Logger, seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        remaining = max(0.2, end - time.time())
        sock.settimeout(remaining)
        try:
            msg = receive_json_lenpref(sock)
            if msg and msg.get("type") == "COMMAND":
                cmd_id = msg.get("cmd_id")
                ok, info = handle_command(msg, state, logger)
                send_cmd_ack(sock, client_id=client_id, cmd_id=cmd_id, ok=ok, message=info, logger=logger)
        except socket.timeout:
            continue
        except Exception:
            continue
    sock.settimeout(None)


def main():
    args = parse_args()
    logger = setup_logger()
    state = {"interval": int(args.interval)}

    logger.info(f"Iniciando cliente | node_id={args.client_id} | server={args.server_ip}:{args.server_port} | first_only={args.first_disk_only}")

    while True:
        try:
            print(f"Conectando a {args.server_ip}:{args.server_port} ...")
            sock = connect_tcp(args.server_ip, args.server_port)
            print("Conectado. Iniciando envío periódico...")

            while True:
                payloads = build_metrics_payloads(args.client_id, first_only=args.first_disk_only)

                for payload in payloads:
                    send_json_lenpref(sock, payload)
                    _ = wait_for_ack_or_commands(sock, args.client_id, state, logger, args.ack_timeout)

                listen_for_commands_while_idle(sock, args.client_id, state, logger, seconds=float(state["interval"]))

        except KeyboardInterrupt:
            print("\nCliente terminado por usuario.")
            return
        except Exception as e:
            logger.error(f"Error cliente: {e}")
            print(f"Error: {e}. Reintentando en 2s...")
            time.sleep(2)


if __name__ == "__main__":
    main()