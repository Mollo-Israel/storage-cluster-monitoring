import socket
import threading
import json
import struct
import time
import sqlite3
import os
from datetime import datetime, timezone

HOST = "0.0.0.0"
PORT = 5000
MAX_CLIENTS = 9
CLIENT_TIMEOUT = 30   # segundos sin reportar (por nodo)
MONITOR_INTERVAL = 10

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "..", "data", "storage.db")
db_lock = threading.Lock()


# -------------------------
# Helpers
# -------------------------
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def recv_exact(sock, size):
    data = b""
    while len(data) < size:
        packet = sock.recv(size - len(data))
        if not packet:
            return None
        data += packet
    return data


def send_json(sock, data):
    try:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        msg = struct.pack(">I", len(encoded)) + encoded
        sock.sendall(msg)
    except Exception:
        pass


def get_db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    conn.execute("PRAGMA busy_timeout=5000;")

    return conn


# -------------------------
# Normalización de mensajes
# -------------------------
def normalize_message(message: dict):
    """
    Devuelve (ok: bool, normalized: dict, error: str)

    Normalized contiene:
      node_id, disk_name, mountpoint,
      total_gb, used_gb, free_gb, percent, iops,
      timestamp
    """

    # node_id puede venir como node_id o client_id
    node_id = message.get("node_id") or message.get("client_id")
    timestamp = message.get("timestamp")

    if not node_id or not timestamp:
        return False, {}, "Falta node_id/client_id o timestamp"

    # Caso NUEVO: disk es objeto
    if isinstance(message.get("disk"), dict):
        disk = message["disk"]
        disk_name = disk.get("name") or disk.get("disk_name")
        mountpoint = disk.get("mountpoint")
        total_gb = disk.get("total_gb")
        used_gb = disk.get("used_gb")
        free_gb = disk.get("free_gb")
        percent = disk.get("percent")
        iops = message.get("iops")

    # Caso VIEJO: campos planos
    else:
        disk_name = message.get("disk_name")
        mountpoint = message.get("mountpoint")  # por si alguien lo manda plano
        total_gb = message.get("total_gb")
        used_gb = message.get("used_gb")
        free_gb = message.get("free_gb")
        percent = message.get("percent")
        iops = message.get("iops")

    # Validaciones mínimas para lo que tu BD exige NOT NULL
    if disk_name is None or total_gb is None or used_gb is None or free_gb is None:
        return False, {}, "Faltan campos de disco (disk_name/total_gb/used_gb/free_gb)"

    # Cast seguros
    try:
        total_gb = float(total_gb)
        used_gb = float(used_gb)
        free_gb = float(free_gb)
    except Exception:
        return False, {}, "total_gb/used_gb/free_gb deben ser numéricos"

    # percent e iops pueden ser NULL
    try:
        percent = None if percent is None else float(percent)
    except Exception:
        percent = None

    try:
        iops = None if iops is None else float(iops)
    except Exception:
        iops = None

    normalized = {
        "node_id": str(node_id),
        "disk_name": str(disk_name),
        "mountpoint": None if mountpoint is None else str(mountpoint),
        "total_gb": total_gb,
        "used_gb": used_gb,
        "free_gb": free_gb,
        "percent": percent,
        "iops": iops,
        "timestamp": str(timestamp),
    }
    return True, normalized, ""


# -------------------------
# DB operations
# -------------------------
def upsert_client(node_id: str, addr):
    addr_str = f"{addr[0]}:{addr[1]}"
    now = now_iso()

    with db_lock:
        conn = get_db_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO clients (node_id, status, last_seen, last_addr, created_at, updated_at)
                VALUES (?, 'ACTIVE', ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    status='ACTIVE',
                    last_seen=excluded.last_seen,
                    last_addr=excluded.last_addr,
                    updated_at=excluded.updated_at;
                """,
                (node_id, now, addr_str, now, now),
            )
            conn.commit()
        finally:
            conn.close()


def insert_metric(n: dict):
    """
    Inserta la métrica ya normalizada en la tabla metrics.
    """
    received_at = now_iso()

    with db_lock:
        conn = get_db_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO metrics (
                    node_id, disk_name, mountpoint,
                    total_gb, used_gb, free_gb,
                    percent, iops,
                    timestamp, received_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    n["node_id"],
                    n["disk_name"],
                    n["mountpoint"],
                    n["total_gb"],
                    n["used_gb"],
                    n["free_gb"],
                    n["percent"],
                    n["iops"],
                    n["timestamp"],
                    received_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def count_clients_db():
    with db_lock:
        conn = get_db_conn()
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM clients;").fetchone()
            return int(row["c"])
        finally:
            conn.close()


def set_no_reporta_if_timeout():
    now_dt = datetime.now(timezone.utc)

    with db_lock:
        conn = get_db_conn()
        cur = conn.cursor()
        try:
            rows = cur.execute("SELECT node_id, last_seen, status FROM clients;").fetchall()

            for r in rows:
                try:
                    last_seen_dt = datetime.fromisoformat(r["last_seen"])
                except Exception:
                    continue

                diff = (now_dt - last_seen_dt).total_seconds()
                if diff > CLIENT_TIMEOUT and r["status"] != "NO_REPORTA":
                    cur.execute(
                        """
                        UPDATE clients
                        SET status='NO_REPORTA', updated_at=?
                        WHERE node_id=?;
                        """,
                        (now_iso(), r["node_id"]),
                    )

            conn.commit()
        finally:
            conn.close()


# -------------------------
# Networking
# -------------------------
def handle_client(conn, addr):
    addr_str = f"{addr[0]}:{addr[1]}"
    print(f"[+] Cliente conectado: {addr_str}")

    try:
        while True:
            raw_msglen = recv_exact(conn, 4)
            if not raw_msglen:
                print(f"[-] Cliente {addr_str} desconectado")
                break

            msglen = struct.unpack(">I", raw_msglen)[0]
            data = recv_exact(conn, msglen)
            if not data:
                print(f"[-] Cliente {addr_str} desconectado")
                break

            try:
                message = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                print(f"[!] JSON inválido desde {addr_str}")
                continue

            ok, normalized, err = normalize_message(message)
            if not ok:
                print(f"[!] Estructura inválida desde {addr_str}: {err}")
                # ACK de error (opcional)
                send_json(conn, {"status": "ERROR", "message": err})
                continue

            node_id = normalized["node_id"]
            disk_name = normalized["disk_name"]

            # 1) Upsert client (ACTIVE + last_seen)
            upsert_client(node_id, addr)

            # 2) Insert metric (histórico)
            insert_metric(normalized)

            print(f"📥 Métrica guardada en BD | node={node_id} disk={disk_name}")

            # 3) ACK
            ack = {
                "status": "OK",
                "message": "Métrica recibida",
                "node_id": node_id,
                "disk_name": disk_name,
            }
            send_json(conn, ack)

    except Exception as e:
        print(f"[ERROR] Cliente {addr_str}: {e}")

    finally:
        conn.close()
        print(f"[x] Conexión cerrada {addr_str}")


def monitor_nodes():
    while True:
        time.sleep(MONITOR_INTERVAL)
        set_no_reporta_if_timeout()


def start_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(MAX_CLIENTS)
    server.settimeout(1)  # ✅ permite Ctrl+C

    print(f"🚀 Servidor escuchando en {HOST}:{PORT}")

    threading.Thread(target=monitor_nodes, daemon=True).start()
    threading.Thread(target=command_console, daemon=True).start()

    try:
        while True:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue

            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()

    except KeyboardInterrupt:
        print("\n🛑 Apagando servidor...")

    finally:
        server.close()
        print("✅ Servidor cerrado correctamente.")


if __name__ == "__main__":
    start_server()