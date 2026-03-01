import argparse
import socket
import json
import random
import time
from datetime import datetime, timezone

import psutil


# -----------------------
# ARGUMENTOS
# -----------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Cliente TCP - métricas + envío periódico")
    parser.add_argument("--client-id", required=True, help="ID del cliente (ej: 1..9)")
    parser.add_argument("--server-ip", required=True, help="IP o hostname del servidor central")
    parser.add_argument("--interval", type=int, default=5, help="Intervalo de envío (segundos)")
    parser.add_argument("--server-port", type=int, default=9000, help="Puerto TCP del servidor (default: 9000)")
    return parser.parse_args()


# -----------------------
# UTIL
# -----------------------
def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def bytes_to_gb(b: int) -> float:
    gb = b / (1024 ** 3)
    return round(float(gb), 2)


# -----------------------
# MÉTRICAS
# -----------------------
def detect_first_disk():
    partitions = psutil.disk_partitions(all=False)
    if not partitions:
        return None

    ignore_fs = {"tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "cgroup"}

    for p in partitions:
        fstype = (p.fstype or "").lower()
        if fstype in ignore_fs:
            continue

        try:
            usage = psutil.disk_usage(p.mountpoint)
        except PermissionError:
            continue

        return {
            "name": p.device,
            "mountpoint": p.mountpoint,
            "total_gb": bytes_to_gb(usage.total),
            "used_gb": bytes_to_gb(usage.used),
            "free_gb": bytes_to_gb(usage.free),
            "percent": round(float(usage.percent), 2),
        }

    # fallback si todo fue ignorado
    p = partitions[0]
    usage = psutil.disk_usage(p.mountpoint)
    return {
        "name": p.device,
        "mountpoint": p.mountpoint,
        "total_gb": bytes_to_gb(usage.total),
        "used_gb": bytes_to_gb(usage.used),
        "free_gb": bytes_to_gb(usage.free),
        "percent": round(float(usage.percent), 2),
    }


def simulate_iops():
    return random.randint(50, 500)


def build_metrics_payload(client_id: str):
    return {
        "type": "metrics",
        "client_id": str(client_id),
        "timestamp": now_iso(),
        "disk": detect_first_disk(),
        "iops": simulate_iops(),
    }


# -----------------------
# TCP + ENVÍO DELIMITADO
# -----------------------
def connect_tcp(server_ip: str, server_port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(8)
    sock.connect((server_ip, server_port))
    sock.settimeout(None)
    return sock


def send_json_delimited(sock: socket.socket, payload: dict) -> None:
    """
    Envía JSON delimitado por salto de línea para que el server pueda separar mensajes.
    """
    message = json.dumps(payload, ensure_ascii=False) + "\n"
    sock.sendall(message.encode("utf-8"))


# -----------------------
# LOOP + RECONEXIÓN
# -----------------------
def run_client(client_id: str, server_ip: str, server_port: int, interval: int):
    backoff = 1
    max_backoff = 20

    try:
        while True:
            sock = None
            try:
                print(f"🔌 Conectando a {server_ip}:{server_port} ...")
                sock = connect_tcp(server_ip, server_port)
                print("✅ Conectado. Iniciando envío periódico...")
                backoff = 1

                while True:
                    payload = build_metrics_payload(client_id)
                    send_json_delimited(sock, payload)
                    print(f"📤 Enviado: {payload}")
                    time.sleep(max(1, interval))

            except Exception as e:
                print(f"❌ Error (se intentará reconectar): {e}")

            finally:
                try:
                    if sock:
                        sock.close()
                except Exception:
                    pass

            print(f"🔁 Reintentando conexión en {backoff}s...")
            time.sleep(backoff)
            backoff = min(max_backoff, backoff * 2)

    except KeyboardInterrupt:
        print("\n🛑 Cliente detenido por el usuario (Ctrl+C). Saliendo...")


# -----------------------
# MAIN
# -----------------------
def main():
    args = parse_args()

    if args.interval <= 0:
        raise ValueError("--interval debe ser mayor a 0")

    run_client(
        client_id=str(args.client_id),
        server_ip=args.server_ip,
        server_port=args.server_port,
        interval=args.interval,
    )


if __name__ == "__main__":
    main()