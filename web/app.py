import os
import sqlite3
from datetime import datetime, timezone
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


def _dt_now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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


def _parse_iso_to_epoch(iso_str: str) -> float:
    return datetime.fromisoformat(iso_str).timestamp()


def _growth_rate_gb_per_month(conn, node_id=None):
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
        return None

    first = rows[0]
    last = rows[-1]

    try:
        t1 = _parse_iso_to_epoch(first["received_at"])
        t2 = _parse_iso_to_epoch(last["received_at"])
        dt_sec = max(t2 - t1, 0.0)
        if dt_sec < 6 * 3600:
            return None

        months = dt_sec / (30.0 * 86400.0)
        months = max(months, 1e-9)
        growth = (float(last["used_sum"]) - float(first["used_sum"])) / months
        return round(growth, 3)
    except Exception:
        return None


def _availability_percent(conn, node_id: str):
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

    total = 0.0
    up = 0.0
    for i in range(len(events)):
        status = events[i]["status"]
        t_start = _parse_iso_to_epoch(events[i]["changed_at"])
        if i < len(events) - 1:
            t_end = _parse_iso_to_epoch(events[i + 1]["changed_at"])
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


def _clock_skew_ms_from_rows(rows):
    skews = []
    for r in rows:
        try:
            client_ts = r["timestamp"]
            server_rx = r["received_at"]
            skews.append((_parse_iso_to_epoch(server_rx) - _parse_iso_to_epoch(client_ts)) * 1000.0)
        except Exception:
            continue
    if not skews:
        return None
    return round(sum(skews) / len(skews), 2)


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

            total = sum(float(r["total_gb"]) for r in rows) if rows else 0.0
            used = sum(float(r["used_gb"]) for r in rows) if rows else 0.0
            free = sum(float(r["free_gb"]) for r in rows) if rows else 0.0
            percent = round((used / total) * 100, 2) if total > 0 else 0.0

            uptime = 0
            for r in rows:
                if r["uptime_sec"] is not None:
                    uptime = max(uptime, int(r["uptime_sec"]))

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
                "growth_gb_per_month": _growth_rate_gb_per_month(conn, node_id=node_id),
                "availability_percent": _availability_percent(conn, node_id=node_id),
                "failover_events": _failover_events(conn, node_id=node_id),
                "clock_skew_ms": _clock_skew_ms_from_rows(rows),
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

        total = sum(float(r["total_gb"]) for r in rows) if rows else 0.0
        used = sum(float(r["used_gb"]) for r in rows) if rows else 0.0
        free = sum(float(r["free_gb"]) for r in rows) if rows else 0.0
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
        quorum_ok = active >= 5

        weighted_sum = 0.0
        weight = 0.0
        for r in rows:
            if r["latency_ms"] is None:
                continue
            w = float(r["total_gb"]) if r["total_gb"] else 0.0
            weighted_sum += float(r["latency_ms"]) * w
            weight += w
        latency_weighted = round(weighted_sum / weight, 2) if weight > 0 else None

        growth_month = _growth_rate_gb_per_month(conn, node_id=None)

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
            "cluster_growth_gb_per_month": growth_month,
            "latency_weighted_ms": latency_weighted,
            "overcommit_ratio": "N/A",
            "fragmentation": "N/A",
            "replication_health": "N/A",
            "generated_at": _dt_now_iso(),
        }), 200
    finally:
        conn.close()


@app.route("/api/node/<node_id>/detail", methods=["GET"])
def node_detail(node_id):
    conn = get_db()
    try:
        c = conn.execute(
            "SELECT node_id, status, last_seen, last_addr FROM clients WHERE node_id=?;",
            (node_id,),
        ).fetchone()
        if not c:
            return jsonify({"error": "node not found"}), 404

        rows = _latest_metrics_rows(conn, node_id=node_id)

        total = sum(float(r["total_gb"]) for r in rows) if rows else 0.0
        used = sum(float(r["used_gb"]) for r in rows) if rows else 0.0
        free = sum(float(r["free_gb"]) for r in rows) if rows else 0.0
        percent = round((used / total) * 100, 2) if total > 0 else 0.0

        hist = conn.execute(
            """
            SELECT received_at, SUM(used_gb) AS used_sum, SUM(total_gb) AS total_sum
            FROM metrics
            WHERE node_id=?
            GROUP BY received_at
            ORDER BY received_at DESC
            LIMIT 80;
            """,
            (node_id,),
        ).fetchall()
        hist = list(reversed(hist))

        cmds = conn.execute(
            """
            SELECT cmd_id, action, value, message, status, created_at, acked_at, last_error
            FROM commands
            WHERE node_id=?
            ORDER BY created_at DESC
            LIMIT 15;
            """,
            (node_id,),
        ).fetchall()

        return jsonify({
            "node": dict(c),
            "summary": {
                "total_gb": round(total, 2),
                "used_gb": round(used, 2),
                "free_gb": round(free, 2),
                "percent": percent,
                "availability_percent": _availability_percent(conn, node_id=node_id),
                "failover_events": _failover_events(conn, node_id=node_id),
                "growth_gb_per_month": _growth_rate_gb_per_month(conn, node_id=node_id),
                "clock_skew_ms": _clock_skew_ms_from_rows(rows),
            },
            "disks": [dict(r) for r in rows],
            "history": [{"received_at": h["received_at"], "used_sum": float(h["used_sum"]), "total_sum": float(h["total_sum"])} for h in hist],
            "commands": [dict(x) for x in cmds],
            "enterprise": {"overcommit_ratio": "N/A", "fragmentation": "N/A", "replication_health": "N/A"},
        }), 200
    finally:
        conn.close()


@app.route("/api/commands/send", methods=["POST"])
def send_command():
    data = request.get_json(force=True, silent=True) or {}
    node_id = (data.get("node_id") or "").strip()
    action = (data.get("action") or "").strip()
    value = data.get("value")
    message = data.get("message")

    if not node_id or not action:
        return jsonify({"error": "node_id y action son obligatorios"}), 400

    import uuid
    cmd_id = str(uuid.uuid4())
    now = _dt_now_iso()

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO commands(cmd_id,node_id,action,value,message,status,created_at,sent_at,acked_at,last_error)
            VALUES(?,?,?,?,?,'PENDING',?,?,NULL,NULL);
            """,
            (cmd_id, node_id, action, None if value is None else str(value), None if message is None else str(message), now, None),
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"status": "OK", "cmd_id": cmd_id}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)