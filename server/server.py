import socket
import threading
import json
import struct
import time


HOST = "0.0.0.0"
PORT = 5000
MAX_CLIENTS = 9
CLIENT_TIMEOUT = 30  # segundos sin reportar (por disco)
MONITOR_INTERVAL = 5

# Estructura:
# clients = {
#   node_id: {
#       "addr": addr,
#       "disks": {
#           disk_name: {"data": message, "last_seen": time.time()}
#       }
#   }
# }
clients = {}
clients_lock = threading.Lock()


def recv_exact(sock, size):
    data = b""
    while len(data) < size:
        packet = sock.recv(size - len(data))
        if not packet:
            return None
        data += packet
    return data


def validate_metrics(data):
    required_fields = ["node_id", "disk_name", "total_gb", "used_gb", "free_gb", "timestamp"]
    return all(field in data for field in required_fields)


def send_json(sock, data):
    try:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        msg = struct.pack(">I", len(encoded)) + encoded
        sock.sendall(msg)
    except Exception:
        pass


def handle_client(conn, addr):
    print(f"[+] Cliente conectado: {addr}")
    conn.settimeout(CLIENT_TIMEOUT)

    try:
        while True:
            raw_msglen = recv_exact(conn, 4)
            if not raw_msglen:
                print(f"[-] Cliente {addr} desconectado")
                break

            msglen = struct.unpack(">I", raw_msglen)[0]

            data = recv_exact(conn, msglen)
            if not data:
                print(f"[-] Cliente {addr} desconectado")
                break

            try:
                message = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                print(f"[!] JSON inválido desde {addr}")
                continue

            if not validate_metrics(message):
                print(f"[!] Estructura inválida desde {addr}")
                continue

            node_id = str(message["node_id"])
            disk_name = str(message["disk_name"])

            with clients_lock:
                if node_id not in clients:
                    clients[node_id] = {"addr": addr, "disks": {}}

                clients[node_id]["addr"] = addr
                clients[node_id]["disks"][disk_name] = {
                    "data": message,
                    "last_seen": time.time(),
                }

            print(f"\n📥 Métrica recibida de {node_id} | Disco: {disk_name}")
            print(json.dumps(message, indent=2, ensure_ascii=False))

            ack = {"status": "OK", "message": "Métrica recibida", "node_id": node_id, "disk_name": disk_name}
            send_json(conn, ack)

    except socket.timeout:
        print(f"[!] Timeout cliente {addr}")

    except Exception as e:
        print(f"[ERROR] Cliente {addr}: {e}")

    finally:
        conn.close()
        print(f"[x] Conexión cerrada {addr}")


def monitor_nodes():
    while True:
        time.sleep(MONITOR_INTERVAL)
        now = time.time()

        with clients_lock:
            for node_id in list(clients.keys()):
                disks = clients[node_id].get("disks", {})
                for disk_name in list(disks.keys()):
                    last_seen = disks[disk_name]["last_seen"]
                    if now - last_seen > CLIENT_TIMEOUT:
                        print(f"⚠ Nodo {node_id} | Disco {disk_name} NO REPORTA (timeout)")
                        del disks[disk_name]

                # Si ya no tiene discos activos, borrar el nodo
                if not disks:
                    del clients[node_id]


def start_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(MAX_CLIENTS)

    # Para que Ctrl+C responda mejor en Windows:
    server.settimeout(1.0)

    print(f"🚀 Servidor escuchando en {HOST}:{PORT}")

    threading.Thread(target=monitor_nodes, daemon=True).start()

    try:
        while True:
            try:
                conn, addr = server.accept()
            except (socket.timeout, TimeoutError):
                continue  # nadie se conectó en este segundo, seguir esperando

            with clients_lock:
                if len(clients) >= MAX_CLIENTS:
                    print("❌ Máximo de clientes alcanzado (por node_id)")
                    conn.close()
                    continue

            thread = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            thread.start()

    except KeyboardInterrupt:
        print("\n🛑 Apagando servidor...")

    finally:
        server.close()
        print("✅ Servidor cerrado correctamente.")


if __name__ == "__main__":
    start_server()