import socket
import threading
import json
import struct
import time
from datetime import datetime

HOST = "0.0.0.0"
PORT = 5000
MAX_CLIENTS = 9
BUFFER_SIZE = 4096
CLIENT_TIMEOUT = 30  # segundos sin reportar

clients = {}
clients_lock = threading.Lock()


# ==============================
# Función para recibir exactamente N bytes
# ==============================
def recv_exact(sock, size):
    data = b''
    while len(data) < size:
        packet = sock.recv(size - len(data))
        if not packet:
            return None
        data += packet
    return data


# ==============================
# Validación mínima de métricas
# ==============================
def validate_metrics(data):
    required_fields = [
        "node_id",
        "disk_name",
        "total_gb",
        "used_gb",
        "free_gb",
        "timestamp"
    ]

    return all(field in data for field in required_fields)


# ==============================
# Manejo de cada cliente
# ==============================
def handle_client(conn, addr):
    print(f"[+] Cliente conectado: {addr}")

    conn.settimeout(CLIENT_TIMEOUT)

    try:
        while True:
            # Leer tamaño del mensaje (4 bytes)
            raw_msglen = recv_exact(conn, 4)
            if not raw_msglen:
                print(f"[-] Cliente {addr} desconectado")
                break

            msglen = struct.unpack(">I", raw_msglen)[0]

            # Leer mensaje completo
            data = recv_exact(conn, msglen)
            if not data:
                print(f"[-] Cliente {addr} desconectado")
                break

            try:
                message = json.loads(data.decode())
            except json.JSONDecodeError:
                print(f"[!] JSON inválido desde {addr}")
                continue  # no tumba el servidor

            if not validate_metrics(message):
                print(f"[!] Estructura inválida desde {addr}")
                continue

            node_id = message["node_id"]

            # Guardar última métrica recibida
            with clients_lock:
                clients[node_id] = {
                    "data": message,
                    "last_seen": time.time(),
                    "addr": addr
                }

            print(f"\n📥 Métrica recibida de {node_id}")
            print(json.dumps(message, indent=2))

            # Enviar ACK
            ack = {"status": "OK", "message": "Métrica recibida"}
            send_json(conn, ack)

    except socket.timeout:
        print(f"[!] Timeout cliente {addr}")

    except Exception as e:
        print(f"[ERROR] Cliente {addr}: {e}")

    finally:
        conn.close()
        print(f"[x] Conexión cerrada {addr}")


# ==============================
# Enviar JSON seguro
# ==============================
def send_json(sock, data):
    try:
        encoded = json.dumps(data).encode()
        msg = struct.pack(">I", len(encoded)) + encoded
        sock.sendall(msg)
    except:
        pass


# ==============================
# Monitor de nodos inactivos
# ==============================
def monitor_nodes():
    while True:
        time.sleep(5)
        now = time.time()

        with clients_lock:
            for node_id in list(clients.keys()):
                if now - clients[node_id]["last_seen"] > CLIENT_TIMEOUT:
                    print(f"⚠ Nodo {node_id} NO REPORTA")
                    del clients[node_id]


# ==============================
# Servidor principal
# ==============================
def start_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(MAX_CLIENTS)

    print(f"🚀 Servidor escuchando en {HOST}:{PORT}")

    threading.Thread(target=monitor_nodes, daemon=True).start()

    try:
        while True:
            conn, addr = server.accept()

            with clients_lock:
                if len(clients) >= MAX_CLIENTS:
                    print("❌ Máximo de clientes alcanzado")
                    conn.close()
                    continue

            thread = threading.Thread(
                target=handle_client,
                args=(conn, addr),
                daemon=True
            )
            thread.start()

    except KeyboardInterrupt:
        print("\n🛑 Apagando servidor...")

    finally:
        server.close()
        print("✅ Servidor cerrado correctamente.")


if __name__ == "__main__":
    start_server()