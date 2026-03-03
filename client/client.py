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

        disks.append({
            "disk_name": device,
            "mountpoint": mount,
            "total_gb": bytes_to_gb(usage.total),
            "used_gb": bytes_to_gb(usage.used),
            "free_gb": bytes_to_gb(usage.free),
            "percent": round(float(usage.percent), 2),
        })

    return disks


def simulate_iops():
    return random.randint(50, 500)


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
            "iops": simulate_iops(),
            "mountpoint": d.get("mountpoint"),
            "percent": d.get("percent"),
        })

    return payloads


def connect_tcp(server_ip: str, server_port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(8)
    sock.connect((server_ip, server_port))
    sock.settimeout(None)
    return sock


def handle_command(cmd: dict, state: dict, logger: logging.Logger):
    """
    Aplica comandos del servidor.
    state contiene valores mutables (intervalo actual, etc.)
    """
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
    }
    send_json_lenpref(sock, payload)
    logger.info(f"CMD_ACK enviado | cmd_id={cmd_id} | status={payload['status']} | message={message}")


def wait_for_ack_or_commands(sock: socket.socket, client_id: str, state: dict, logger: logging.Logger, ack_timeout: int):
    """
    Lee mensajes del server hasta recibir un ACK de métrica.
    Si llegan COMMANDs primero, los procesa y responde CMD_ACK (con cmd_id).
    """
    deadline = time.time() + ack_timeout

    while True:
        remaining = max(0.1, deadline - time.time())
        sock.settimeout(remaining)

        msg = receive_json_lenpref(sock)
        if not msg:
            raise ConnectionError("Conexión cerrada por el servidor")

        msg_type = msg.get("type")

        # 1) Si es comando, procesar y seguir esperando ACK
        if msg_type == "COMMAND":
            cmd_id = msg.get("cmd_id")
            ok, info = handle_command(msg, state, logger)
            send_cmd_ack(sock, client_id=client_id, cmd_id=cmd_id, ok=ok, message=info, logger=logger)
            continue

        # 2) Si es ACK (métrica), ya cumplimos
        if msg_type == "ACK" or ("status" in msg and "message" in msg):
            # compatibilidad con tu ACK antiguo
            sock.settimeout(None)
            logger.info(
                f"ACK recibido | node_id={msg.get('node_id')} | disk={msg.get('disk_name')} | status={msg.get('status')}"
            )
            return msg

        # 3) Otro tipo raro, log y seguir
        logger.warning(f"Mensaje desconocido recibido: {msg}")


def listen_for_commands_while_idle(sock: socket.socket, client_id: str, state: dict, logger: logging.Logger, seconds: float):
    """
    Durante el tiempo de espera entre envíos, escucha comandos del servidor.
    Esto cumple 'Escuchar mensajes del servidor' incluso si no estás justo esperando un ACK.
    """
    end = time.time() + seconds
    while time.time() < end:
        remaining = end - time.time()
        sock.settimeout(min(0.5, max(0.1, remaining)))
        try:
            msg = receive_json_lenpref(sock)
            if not msg:
                raise ConnectionError("Conexión cerrada por el servidor")

            if msg.get("type") == "COMMAND":
                cmd_id = msg.get("cmd_id")
                ok, info = handle_command(msg, state, logger)
                send_cmd_ack(sock, client_id=client_id, cmd_id=cmd_id, ok=ok, message=info, logger=logger)
            else:
                # si llega cualquier cosa inesperada, lo registramos
                logger.warning(f"Mensaje inesperado en idle: {msg}")

        except socket.timeout:
            # normal: no llegó nada, seguimos esperando hasta que acabe el tiempo
            continue


def run_client(client_id: str, server_ip: str, server_port: int, interval: int, ack_timeout: int):
    logger = setup_logger()
    state = {"interval": int(interval)}  # intervalo mutable (se puede cambiar por comando)

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

                        # Esperar ACK, pero si llegan COMMAND, procesarlos y responder CMD_ACK
                        try:
                            ack = wait_for_ack_or_commands(
                                sock=sock,
                                client_id=client_id,
                                state=state,
                                logger=logger,
                                ack_timeout=ack_timeout,
                            )
                            print(f"✅ ACK: {ack}")
                        except socket.timeout:
                            logger.warning(f"Timeout esperando ACK ({ack_timeout}s). Se forzará reconexión.")
                            raise ConnectionError(f"Timeout esperando ACK ({ack_timeout}s)")

                    # Antes de dormir TODO el intervalo, escucha comandos durante la espera (idle listening)
                    current_interval = max(1, int(state["interval"]))
                    listen_for_commands_while_idle(sock, client_id, state, logger, seconds=current_interval)

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