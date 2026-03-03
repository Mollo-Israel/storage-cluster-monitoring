import sqlite3
import os
from flask import Flask, jsonify

app = Flask(__name__)

# Ruta al archivo storage.db (sube un nivel desde /web)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "storage.db")


# ===============================
# Inicializar Base de Datos
# ===============================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Tabla de clientes
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS clients (
        node_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        last_seen TEXT
    )
    """)

    # Tabla de métricas
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id TEXT,
        disk_name TEXT,
        total_gb REAL,
        used_gb REAL,
        free_gb REAL,
        timestamp TEXT
    )
    """)

    conn.commit()
    conn.close()


# ===============================
# Obtener conexión segura
# ===============================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.route("/")
def home():
    return jsonify({
        "message": "Storage Cluster Monitoring API",
        "endpoints": [
            "/nodes"
        ]
    })
# ===============================
# Endpoint: /nodes
# ===============================
@app.route("/nodes", methods=["GET"])
def get_nodes():
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT node_id, status, last_seen
            FROM clients
            ORDER BY node_id
        """)

        rows = cursor.fetchall()
        conn.close()

        result = [dict(row) for row in rows]

        return jsonify(result), 200

    except Exception as e:
        return jsonify({
            "error": "Error interno del servidor",
            "details": str(e)
        }), 500


# ===============================
# Ejecutar servidor Flask
# ===============================
if __name__ == "__main__":
    init_db()  # 🔥 crea BD y tablas si no existen
    app.run(host="0.0.0.0", port=8000, debug=True)