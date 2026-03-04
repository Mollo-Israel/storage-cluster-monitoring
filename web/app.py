import os
import sqlite3
from datetime import datetime
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "storage.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp


@app.after_request
def after(resp):
    return cors(resp)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "db_path": DB_PATH})


# ---------- helpers de cálculos ----------
def _latest_metrics_rows(conn, node_id=None):
    if node_id:
        return conn.execute(
            """
            SELECT m.*
            FROM metrics m
            JOIN (
                SELECT node_id, disk_name, MAX(received_at) AS max_recv
                FROM metrics
                WHERE node_id = ?
                GROUP BY node_id, disk_name
            ) last
            ON m.node_id = last.node_id
            AND m.disk_name = last.disk_name
            AND m.received_at = last.max_recv
            WHERE m.node_id = ?;
            """,
            (node_id, node_id),
        ).fetchall()

    return conn.execute(
        """
        SELECT m.*
        FROM metrics m
        JOIN (
            SELECT node_id, disk_name, MAX(received_at) AS max_recv
            FROM metrics
            GROUP BY node_id, disk_name
        ) last
        ON m.node_id = last.node_id
        AND m.disk_name = last.disk_name
        AND m.received_at = last.max_recv;
        """
    ).fetchall()


def _growth_rate_gb_per_day(conn, node_id=None):
    """
    Growth rate simple:
    delta used_gb (último - primero) / delta_dias, usando la suma por snapshot.
    Si no hay data suficiente -> 0.
    """
    if node_id:
        rows = conn.execute(
            """
            SELECT received_at, SUM(used_gb) AS used_sum
            FROM metrics
            WHERE node_id = ?
            GROUP BY received_at
            ORDER BY received_at ASC;
            """,
            (node_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT received_at, SUM(used_gb) AS used_sum
            FROM metrics
            GROUP BY received_at
            ORDER BY received_at ASC;
            """
        ).fetchall()

    if len(rows) < 2:
        return 0.0

    first = rows[0]
    last = rows[-1]
    try:
        t1 = datetime.fromisoformat(first["received_at"]).timestamp()
        t2 = datetime.fromisoformat(last["received_at"]).timestamp()
        days = max((t2 - t1) / 86400.0, 1e-9)
        return round((float(last["used_sum"]) - float(first["used_sum"])) / days, 3)
    except Exception:
        return 0.0


def _availability_percent(conn, node_id: str):
    """
    Availability aproximada basada en historial de estados:
    % tiempo en ACTIVE desde el primer evento.
    """
    events = conn.execute(
        """
        SELECT status, changed_at
        FROM node_status_history
        WHERE node_id=?
        ORDER BY changed_at ASC;
        """,
        (node_id,),
    ).fetchall()

    if not events:
        return 0.0

    # construir intervalos
    total = 0.0
    up = 0.0
    for i in range(len(events)):
        status = events[i]["status"]
        t_start = datetime.fromisoformat(events[i]["changed_at"]).timestamp()
        if i < len(events) - 1:
            t_end = datetime.fromisoformat(events[i + 1]["changed_at"]).timestamp()
        else:
            t_end = datetime.now().timestamp()
        dt = max(0.0, t_end - t_start)
        total += dt
        if status == "ACTIVE":
            up += dt

    if total <= 0:
        return 0.0
    return round((up / total) * 100.0, 3)


def _failover_events(conn, node_id: str):
    """
    Simulado: cada transición NO_REPORTA -> ACTIVE cuenta como 'failover event'
    (en tu arquitectura no hay failover real).
    """
    events = conn.execute(
        """
        SELECT status
        FROM node_status_history
        WHERE node_id=?
        ORDER BY changed_at ASC;
        """,
        (node_id,),
    ).fetchall()

    if len(events) < 2:
        return 0

    count = 0
    for i in range(1, len(events)):
        if events[i - 1]["status"] == "NO_REPORTA" and events[i]["status"] == "ACTIVE":
            count += 1
    return count


@app.route("/api/nodes", methods=["GET"])
def nodes():
    conn = get_db()
    try:
        clients = conn.execute(
            """
            SELECT node_id, status, last_seen, last_addr
            FROM clients
            ORDER BY node_id;
            """
        ).fetchall()

        result = []
        for c in clients:
            node_id = c["node_id"]
            rows = _latest_metrics_rows(conn, node_id=node_id)

            total = sum(r["total_gb"] for r in rows) if rows else 0.0
            used = sum(r["used_gb"] for r in rows) if rows else 0.0
            free = sum(r["free_gb"] for r in rows) if rows else 0.0
            percent = round((used / total) * 100, 2) if total > 0 else 0.0

            # uptime: tomar el mayor uptime reportado en el snapshot (si hay varios discos)
            uptime = 0
            for r in rows:
                if r["uptime_sec"] is not None:
                    uptime = max(uptime, int(r["uptime_sec"]))

            # latencia promedio (snapshot)
            lat_list = [float(r["latency_ms"]) for r in rows if r["latency_ms"] is not None]
            avg_latency = round(sum(lat_list) / len(lat_list), 2) if lat_list else None

            result.append({
                "node_id": node_id,
                "status": c["status"],
                "last_seen": c["last_seen"],
                "last_addr": c["last_addr"],
                "total_gb": round(total, 2),
                "used_gb": round(used, 2),
                "free_gb": round(free, 2),
                "percent": percent,
                "disks_count": len(rows),
                "uptime_sec": uptime,
                "avg_latency_ms": avg_latency,
                "growth_gb_per_day": _growth_rate_gb_per_day(conn, node_id=node_id),
                "availability_percent": _availability_percent(conn, node_id=node_id),
                "failover_events": _failover_events(conn, node_id=node_id),
                # indicadores enterprise no implementables aquí: presentarlos como N/A
                "overcommit_ratio": "N/A",
                "fragmentation": "N/A",
                "replication_health": "N/A",
            })

        return jsonify(result), 200
    finally:
        conn.close()


@app.route("/api/totals", methods=["GET"])
def totals():
    conn = get_db()
    try:
        rows = _latest_metrics_rows(conn)

        total = sum(r["total_gb"] for r in rows) if rows else 0.0
        used = sum(r["used_gb"] for r in rows) if rows else 0.0
        free = sum(r["free_gb"] for r in rows) if rows else 0.0
        percent = round((used / total) * 100, 2) if total > 0 else 0.0

        stats = conn.execute(
            """
            SELECT
              SUM(CASE WHEN status='ACTIVE' THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN status='NO_REPORTA' THEN 1 ELSE 0 END) AS no_reporta,
              COUNT(*) AS total
            FROM clients;
            """
        ).fetchone()

        active = int(stats["active"]) if stats else 0
        total_nodes = int(stats["total"]) if stats else 0
        quorum_ok = active >= 5  # mayoría simple para 9

        # latencia ponderada: ponderar por total_gb del snapshot
        weighted_sum = 0.0
        weight = 0.0
        for r in rows:
            if r["latency_ms"] is None:
                continue
            w = float(r["total_gb"]) if r["total_gb"] else 0.0
            weighted_sum += float(r["latency_ms"]) * w
            weight += w
        latency_weighted = round(weighted_sum / weight, 2) if weight > 0 else None

        return jsonify({
            "total_gb": round(total, 2),
            "used_gb": round(used, 2),
            "free_gb": round(free, 2),
            "percent": percent,
            "nodes_active": active,
            "nodes_no_reporta": int(stats["no_reporta"]) if stats else 0,
            "nodes_total": total_nodes,
            "quorum": "OK" if quorum_ok else "NO",
            "availability_target": ">= 99.9%",
            "cluster_growth_gb_per_day": _growth_rate_gb_per_day(conn, node_id=None),
            "latency_weighted_ms": latency_weighted,
            "overcommit_ratio": "N/A",
            "fragmentation": "N/A",
            "replication_health": "N/A",
        }), 200
    finally:
        conn.close()


@app.route("/api/node/<node_id>/disks", methods=["GET"])
def node_disks(node_id):
    conn = get_db()
    try:
        rows = _latest_metrics_rows(conn, node_id=node_id)
        return jsonify([dict(r) for r in rows]), 200
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)