import argparse
import json
import os
import socket
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple, Any


# =========================
# Utils
# =========================
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def recv_exact(sock: socket.socket, size: int) -> Optional[bytes]:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def send_json_lenpref(sock: socket.socket, payload: dict, send_lock: Optional[threading.Lock] = None) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    msg = struct.pack(">I", len(encoded)) + encoded
    try:
        if send_lock:
            with send_lock:
                sock.sendall(msg)
        else:
            sock.sendall(msg)
    except Exception:
        pass


def recv_json_lenpref(sock: socket.socket) -> Optional[dict]:
    raw_len = recv_exact(sock, 4)
    if not raw_len:
        return None
    msglen = struct.unpack(">I", raw_len)[0]
    raw = recv_exact(sock, msglen)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {"type": "INVALID_JSON"}


# =========================
# DB helpers + migrations
# =========================
def get_db_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def table_has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table});").fetchall()
    return any(r["name"] == col for r in rows)


def init_and_migrate_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = get_db_conn(db_path)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS clients (
            node_id TEXT NOT NULL PRIMARY KEY,
            status  TEXT NOT NULL CHECK(status IN ('ACTIVE','NO_REPORTA')),
            last_seen TEXT NOT NULL,
            last_addr TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            disk_name TEXT NOT NULL,
            mountpoint TEXT,
            disk_type TEXT,             -- SSD/HDD/UNKNOWN
            total_gb REAL NOT NULL,
            used_gb REAL NOT NULL,
            free_gb REAL NOT NULL,
            percent REAL,
            iops REAL,
            uptime_sec INTEGER,         -- uptime del host
            latency_ms REAL,            -- latencia server-side (aprox)
            timestamp TEXT NOT NULL,     -- timestamp enviado por cliente
            received_at TEXT NOT NULL,  -- timestamp servidor
            FOREIGN KEY(node_id) REFERENCES clients(node_id)
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cmd_id TEXT NOT NULL UNIQUE,
            node_id TEXT NOT NULL,
            action TEXT NOT NULL,
            value TEXT,
            message TEXT,
            status TEXT NOT NULL CHECK(status IN ('PENDING','SENT','ACKED','ERROR')),
            created_at TEXT NOT NULL,
            sent_at TEXT,
            acked_at TEXT,
            last_error TEXT
        );
        """
    )

    # Historial de estados (para Availability y Failover events)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS node_status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ACTIVE','NO_REPORTA')),
            changed_at TEXT NOT NULL
        );
        """
    )

    conn.commit()

    # Migrations suaves para BD ya existente (ALTER TABLE si falta)
    # metrics: disk_type, uptime_sec, latency_ms
    for col_def in [
        ("disk_type", "TEXT"),
        ("uptime_sec", "INTEGER"),
        ("latency_ms", "REAL"),
    ]:
        col, typ = col_def
        if not table_has_column(conn, "metrics", col):
            conn.execute(f"ALTER TABLE metrics ADD COLUMN {col} {typ};")
            conn.commit()

    conn.close()


# =========================
# Normalización de métricas
# =========================
def normalize_metric(message: dict) -> Tuple[bool, dict, str]:
    node_id = message.get("node_id") or message.get("client_id")
    ts = message.get("timestamp")
    if not node_id or not ts:
        return False, {}, "Falta node_id/client_id o timestamp"

    disk_name = message.get("disk_name")
    if disk_name is None:
        return False, {}, "Falta disk_name"

    mountpoint = message.get("mountpoint")
    disk_type = (message.get("disk_type") or "UNKNOWN").upper()

    total_gb = message.get("total_gb")
    used_gb = message.get("used_gb")
    free_gb = message.get("free_gb")
    percent = message.get("percent")
    iops = message.get("iops")

    uptime_sec = message.get("uptime_sec")
    sent_at = message.get("sent_at")  # epoch float

    if total_gb is None or used_gb is None or free_gb is None:
        return False, {}, "Faltan campos total_gb/used_gb/free_gb"

    try:
        total_gb = float(total_gb)
        used_gb = float(used_gb)
        free_gb = float(free_gb)
    except Exception:
        return False, {}, "total_gb/used_gb/free_gb deben ser numéricos"

    try:
        percent = None if percent is None else float(percent)
    except Exception:
        percent = None

    try:
        iops = None if iops is None else float(iops)
    except Exception:
        iops = None

    try:
        uptime_sec = None if uptime_sec is None else int(uptime_sec)
    except Exception:
        uptime_sec = None

    # Latencia aproximada: tiempo desde que el cliente mandó sent_at hasta que servidor procesó
    latency_ms = None
    try:
        if sent_at is not None:
            latency_ms = round((time.time() - float(sent_at)) * 1000.0, 2)
    except Exception:
        latency_ms = None

    if disk_type not in ("SSD", "HDD", "UNKNOWN"):
        disk_type = "UNKNOWN"

    return True, {
        "node_id": str(node_id),
        "disk_name": str(disk_name),
        "mountpoint": None if mountpoint is None else str(mountpoint),
        "disk_type": disk_type,
        "total_gb": total_gb,
        "used_gb": used_gb,
        "free_gb": free_gb,
        "percent": percent,
        "iops": iops,
        "uptime_sec": uptime_sec,
        "latency_ms": latency_ms,
        "timestamp": str(ts),
    }, ""


# =========================
# Sesiones conectadas
# =========================
@dataclass
class ClientSession:
    sock: socket.socket
    addr: Tuple[str, int]
    send_lock: threading.Lock
    node_id: Optional[str] = None


# =========================
# Servidor principal
# =========================
class StorageClusterServer:
    def __init__(self, host: str, port: int, max_clients: int, db_path: str, no_report_timeout: int):
        self.host = host
        self.port = port
        self.max_clients = max_clients
        self.db_path = db_path
        self.no_report_timeout = no_report_timeout

        self.server_sock: Optional[socket.socket] = None
        self.running = False

        self.sessions_lock = threading.Lock()
        self.sessions: Dict[str, ClientSession] = {}  # node_id -> session

        self.db_lock = threading.Lock()

    # ---------- DB ops ----------
    def _db(self) -> sqlite3.Connection:
        return get_db_conn(self.db_path)

    def upsert_client_active(self, node_id: str, addr: str) -> None:
        now = now_iso()
        with self.db_lock:
            conn = self._db()
            try:
                existing = conn.execute("SELECT node_id, status FROM clients WHERE node_id=?;", (node_id,)).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO clients(node_id,status,last_seen,last_addr,created_at,updated_at)
                        VALUES(?,?,?,?,?,?);
                        """,
                        (node_id, "ACTIVE", now, addr, now, now),
                    )
                    conn.execute(
                        "INSERT INTO node_status_history(node_id,status,changed_at) VALUES(?,?,?);",
                        (node_id, "ACTIVE", now),
                    )
                else:
                    prev_status = existing["status"]
                    conn.execute(
                        """
                        UPDATE clients
                        SET status='ACTIVE', last_seen=?, last_addr=?, updated_at=?
                        WHERE node_id=?;
                        """,
                        (now, addr, now, node_id),
                    )
                    if prev_status != "ACTIVE":
                        conn.execute(
                            "INSERT INTO node_status_history(node_id,status,changed_at) VALUES(?,?,?);",
                            (node_id, "ACTIVE", now),
                        )
                conn.commit()
            finally:
                conn.close()

    def touch_client_seen(self, node_id: str) -> None:
        now = now_iso()
        with self.db_lock:
            conn = self._db()
            try:
                conn.execute(
                    "UPDATE clients SET last_seen=?, updated_at=? WHERE node_id=?;",
                    (now, now, node_id),
                )
                conn.commit()
            finally:
                conn.close()

    def set_client_no_reporta(self, node_id: str) -> None:
        now = now_iso()
        with self.db_lock:
            conn = self._db()
            try:
                row = conn.execute("SELECT status FROM clients WHERE node_id=?;", (node_id,)).fetchone()
                if not row:
                    return
                if row["status"] == "NO_REPORTA":
                    return
                conn.execute(
                    "UPDATE clients SET status='NO_REPORTA', updated_at=? WHERE node_id=?;",
                    (now, node_id),
                )
                conn.execute(
                    "INSERT INTO node_status_history(node_id,status,changed_at) VALUES(?,?,?);",
                    (node_id, "NO_REPORTA", now),
                )
                conn.commit()
            finally:
                conn.close()

    def insert_metric(self, m: dict) -> None:
        recv_at = now_iso()
        with self.db_lock:
            conn = self._db()
            try:
                conn.execute(
                    """
                    INSERT INTO metrics(node_id,disk_name,mountpoint,disk_type,total_gb,used_gb,free_gb,percent,iops,uptime_sec,latency_ms,timestamp,received_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?);
                    """,
                    (
                        m["node_id"],
                        m["disk_name"],
                        m["mountpoint"],
                        m["disk_type"],
                        m["total_gb"],
                        m["used_gb"],
                        m["free_gb"],
                        m["percent"],
                        m["iops"],
                        m["uptime_sec"],
                        m["latency_ms"],
                        m["timestamp"],
                        recv_at,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    # ---------- Watcher NO_REPORTA ----------
    def watcher_no_reporta(self):
        while self.running:
            try:
                cutoff = time.time() - self.no_report_timeout
                # last_seen es ISO; comparamos parseando a epoch de forma simple
                with self.db_lock:
                    conn = self._db()
                    try:
                        rows = conn.execute("SELECT node_id, last_seen FROM clients;").fetchall()
                    finally:
                        conn.close()

                for r in rows:
                    node_id = r["node_id"]
                    try:
                        # parse ISO -> epoch
                        dt = datetime.fromisoformat(r["last_seen"])
                        last_epoch = dt.timestamp()
                    except Exception:
                        continue

                    if last_epoch < cutoff:
                        self.set_client_no_reporta(node_id)

            except Exception:
                pass

            time.sleep(2)

    # ---------- Networking ----------
    def start(self):
        self.running = True
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.host, self.port))
        self.server_sock.listen(50)

        print(f"✅ Server escuchando en {self.host}:{self.port} | max_clients={self.max_clients}")
        threading.Thread(target=self.watcher_no_reporta, daemon=True).start()

        try:
            while self.running:
                client_sock, addr = self.server_sock.accept()
                threading.Thread(target=self.handle_client, args=(client_sock, addr), daemon=True).start()
        except KeyboardInterrupt:
            print("\n🛑 Server detenido por usuario.")
        finally:
            self.running = False
            try:
                self.server_sock.close()
            except Exception:
                pass

    def handle_client(self, client_sock: socket.socket, addr: Tuple[str, int]):
        session = ClientSession(sock=client_sock, addr=addr, send_lock=threading.Lock(), node_id=None)
        addr_str = f"{addr[0]}:{addr[1]}"

        try:
            while True:
                msg = recv_json_lenpref(client_sock)
                if msg is None:
                    break

                # ACK de comandos
                if msg.get("type") == "CMD_ACK":
                    self.handle_cmd_ack(msg)
                    continue

                if msg.get("type") == "INVALID_JSON":
                    send_json_lenpref(client_sock, {"type": "ACK", "status": "ERROR", "message": "JSON inválido"}, session.send_lock)
                    continue

                ok, metric, err = normalize_metric(msg)
                if not ok:
                    send_json_lenpref(client_sock, {"type": "ACK", "status": "ERROR", "message": err}, session.send_lock)
                    continue

                node_id = metric["node_id"]

                # Enforce max 9 clientes únicos
                with self.sessions_lock:
                    if node_id not in self.sessions and len(self.sessions) >= self.max_clients:
                        send_json_lenpref(
                            client_sock,
                            {"type": "ACK", "status": "ERROR", "message": "Server lleno (max_clients alcanzado)"},
                            session.send_lock,
                        )
                        continue

                    # Registrar sesión (o actualizar)
                    session.node_id = node_id
                    self.sessions[node_id] = session

                # Upsert cliente ACTIVE + last_seen
                self.upsert_client_active(node_id=node_id, addr=addr_str)
                self.touch_client_seen(node_id=node_id)

                # Insert métrica
                self.insert_metric(metric)

                # Responder ACK
                send_json_lenpref(
                    client_sock,
                    {
                        "type": "ACK",
                        "status": "OK",
                        "message": "Métrica recibida",
                        "node_id": node_id,
                        "disk_name": metric["disk_name"],
                    },
                    session.send_lock,
                )

        except Exception:
            pass
        finally:
            try:
                client_sock.close()
            except Exception:
                pass

            # remover sesión
            with self.sessions_lock:
                if session.node_id and self.sessions.get(session.node_id) is session:
                    del self.sessions[session.node_id]

    # ---------- Commands (simple, mantiene tu idea actual) ----------
    def handle_cmd_ack(self, msg: dict):
        cmd_id = str(msg.get("cmd_id", ""))
        node_id = str(msg.get("node_id", ""))
        status = str(msg.get("status", "ERROR"))
        acked_at = msg.get("acked_at") or now_iso()
        message = msg.get("message")

        if not cmd_id or not node_id:
            return

        with self.db_lock:
            conn = self._db()
            try:
                if status == "OK":
                    conn.execute(
                        "UPDATE commands SET status='ACKED', acked_at=?, last_error=NULL WHERE cmd_id=?;",
                        (acked_at, cmd_id),
                    )
                else:
                    conn.execute(
                        "UPDATE commands SET status='ERROR', acked_at=?, last_error=? WHERE cmd_id=?;",
                        (acked_at, message, cmd_id),
                    )
                conn.commit()
            finally:
                conn.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Servidor central - Storage Cluster Monitoring")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--max-clients", type=int, default=9)
    parser.add_argument("--db-path", default=os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "storage.db"))
    parser.add_argument("--no-report-timeout", type=int, default=15, help="Segundos sin reporte para NO_REPORTA")
    return parser.parse_args()


def main():
    args = parse_args()
    init_and_migrate_db(args.db_path)
    server = StorageClusterServer(
        host=args.host,
        port=args.port,
        max_clients=args.max_clients,
        db_path=args.db_path,
        no_report_timeout=args.no_report_timeout,
    )
    server.start()


if __name__ == "__main__":
    main()