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

import psutil


def parse_args():
    parser = argparse.ArgumentParser(description="Cliente TCP - métricas + envío periódico")
    parser.add_argument("--client-id", required=True, help="ID del cliente (ej: LPZ, ORU, 1..9)")
    parser.add_argument("--server-ip", required=True, help="IP o hostname del servidor central")
    parser.add_argument("--interval", type=int, default=5, help="Intervalo de envío (segundos)")
    parser.add_argument("--server-port", type=int, default=5000, help="Puerto TCP del servidor (default: 5000)")
    parser.add_argument("--ack-timeout", type=int, default=3, help="Timeout esperando ACK (segundos)")
    return parser.parse_args()


def setup_logger():
    """
    Logger robusto a archivo con rotación: client/client.log
    - No crece infinito
    - Formato simple para auditoría real
    """
    logger = logging.getLogger("client_logger")
    logger.setLevel(logging.INFO)

    # Evita duplicar handlers si el módulo se recarga en el mismo proceso
    if logger.handlers:
        return logger

    log_path = os.path.join(os.path.dirname(__file__), "client.log")

    handler = RotatingFileHandler(
        log_path,
        maxBytes=1_000_000,   # 1 MB
        backupCount=3,
        encoding="utf-8",
    )

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


def list_all_disks():
    """
    Devuelve una lista de discos/particiones "reales" con uso.
    Filtra FS típicos "virtuales" para evitar ruido.
    """
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

        disks.append({
            "disk_name": device,            # <- lo que el server espera
            "mountpoint": mount,
            "total_gb": bytes_to_gb(usage.total),
            "used_gb": bytes_to_gb(usage.used),
            "free_gb": bytes_to_gb(usage.free),
            "percent": round(float(usage.percent), 2),
        })

    return disks


def simulate_iops():
    return random.randint(50, 500)


# --- Protocolo: 4 bytes (len) + JSON (igual al server) ---
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


def build_metrics_payloads(client_id: str):
    disks = list_all_disks()

    # Fallback mínimo para no reventar si psutil no detecta nada
    if not disks:
        disks = [{
            "disk_name": "UNKNOWN",
            "mountpoint": None,
            "total_gb": 0,
            "used_gb": 0,
            "free_gb": 0,
            "percent": 0
        }]

    ts = now_iso()
    payloads = []
    for d in disks:
        payloads.append({
            "node_id": str(client_id),
            "disk_name": d["disk_name"],
            "total_gb": d["total_gb"],
            "used_gb": d["used_gb"],
            "free_gb": d["free_gb"],
            "timestamp": ts,

            # Extras
            "iops": simulate_iops(),
            "mountpoint": d.get("mountpoint"),
            "percent": d.get("percent"),
        })

    return payloads


def connect_tcp(server_ip: str, server_port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(8)  # timeout solo para conectar
    sock.connect((server_ip, server_port))
    sock.settimeout(None)  # luego queda en blocking normal
    return sock


def run_client(client_id: str, server_ip: str, server_port: int, interval: int, ack_timeout: int):
    logger = setup_logger()

    backoff = 1
    max_backoff = 20

    try:
        while True:
            sock = None
            try:
                print(f"🔌 Conectando a {server_ip}:{server_port} ...")
                sock = connect_tcp(server_ip, server_port)

                print("✅ Conectado. Iniciando envío periódico...")
                logger.info(f"Conexión exitosa al servidor {server_ip}:{server_port} | node_id={client_id}")
                backoff = 1

                while True:
                    payloads = build_metrics_payloads(client_id)

                    # Enviar 1 payload por disco
                    for payload in payloads:
                        send_json_lenpref(sock, payload)
                        print(f"📤 Enviado: {payload}")

                        # Esperar ACK con timeout (robusto)
                        try:
                            sock.settimeout(ack_timeout)
                            ack = receive_json_lenpref(sock)
                            sock.settimeout(None)

                            if not ack:
                                raise ConnectionError("ACK vacío o conexión cerrada por el servidor")

                            print(f"✅ ACK: {ack}")
                            logger.info(
                                f"ACK recibido | node_id={ack.get('node_id')} | disk={ack.get('disk_name')} | status={ack.get('status')}"
                            )

                        except socket.timeout:
                            # ACK no llegó: tratamos como falla de comunicación
                            sock.settimeout(None)
                            logger.warning(f"Timeout esperando ACK ({ack_timeout}s). Se forzará reconexión.")
                            raise ConnectionError(f"Timeout esperando ACK ({ack_timeout}s)")

                        except Exception as e:
                            sock.settimeout(None)
                            logger.warning(f"Error recibiendo ACK. Se forzará reconexión. Detalle: {e}")
                            raise

                    time.sleep(max(1, interval))

            except Exception as e:
                print(f"❌ Error (se intentará reconectar): {e}")
                logger.error(f"Error: {e} | Reintentando reconexión...")

            finally:
                try:
                    if sock:
                        sock.close()
                except Exception:
                    pass

            print(f"🔁 Reintentando conexión en {backoff}s...")
            logger.info(f"Reintentando conexión en {backoff}s...")
            time.sleep(backoff)
            backoff = min(max_backoff, backoff * 2)

    except KeyboardInterrupt:
        print("\n🛑 Cliente detenido por el usuario (Ctrl+C). Saliendo...")
        logger.info("Cliente detenido por el usuario (Ctrl+C).")


def main():
    args = parse_args()

    if args.interval <= 0:
        raise ValueError("--interval debe ser mayor a 0")
    if args.ack_timeout <= 0:
        raise ValueError("--ack-timeout debe ser mayor a 0")

    run_client(
        client_id=str(args.client_id),
        server_ip=args.server_ip,
        server_port=int(args.server_port),
        interval=int(args.interval),
        ack_timeout=int(args.ack_timeout),
    )


if __name__ == "__main__":
    main()