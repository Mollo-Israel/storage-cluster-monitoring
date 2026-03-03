import socket
import threading
import json
import struct
import time

HOST = "0.0.0.0"
PORT = 5000
MAX_CLIENTS = 9
CLIENT_TIMEOUT = 30
MONITOR_INTERVAL = 5

clients = {}
clients_lock = threading.Lock()

# --- NUEVO: cola de comandos por cliente ---
command_queues = {}  # { node_id: [command_dict, ...] }
command_lock = threading.Lock()


def recv_exact(sock, size):
    data = b""
    while len(data) < size:
        packet = sock.recv(size - len(data))
        if not packet:
            return None
        data += packet
    return data


def send_json(sock, data):
    encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
    msg = struct.pack(">I", len(encoded)) + encoded
    sock.sendall(msg)


def recv_json(sock):
    raw_len = recv_exact(sock, 4)
    if not raw_len:
        return None
    msglen = struct.unpack(">I", raw_len)[0]
    data = recv_exact(sock, msglen)
    if not data:
        return None
    return json.loads(data.decode("utf-8"))


def validate_metrics(data):
    required_fields = ["node_id", "disk_name", "total_gb", "used_gb", "free_gb", "timestamp"]
    return all(field in data for field in required_fields)


def enqueue_command(node_id: str, cmd: dict):
    with command_lock:
        command_queues.setdefault(node_id, []).append(cmd)


def pop_next_command(node_id: str):
    with command_lock:
        q = command_queues.get(node_id, [])
        if not q:
            return None
        return q.pop(0)


def command_console():
    """
    Permite inyectar comandos desde la consola del server para probar:

    set_interval LPZ 2
    """
    print("🧪 Consola de comandos activa. Ejemplo: set_interval LPZ 2")
    cmd_id_counter = 1

    while True:
        try:
            line = input("> ").strip()
            if not line:
                continue

            parts = line.split()
            if len(parts) != 3:
                print("Formato: set_interval <NODE_ID> <SEGUNDOS>")
                continue

            action_txt, node_id, value_txt = parts
            if action_txt.lower() != "set_interval":
                print("Acción soportada: set_interval")
                continue

            try:
                value = int(value_txt)
                if value <= 0:
                    raise ValueError
            except ValueError:
                print("El valor debe ser entero > 0")
                continue

            cmd = {
                "type": "COMMAND",
                "cmd_id": str(cmd_id_counter),
                "action": "SET_INTERVAL",
                "value": value,
            }
            cmd_id_counter += 1

            enqueue_command(node_id, cmd)
            print(f"✅ Comando encolado para {node_id}: {cmd}")

        except EOFError:
            break
        except Exception as e:
            print(f"[ERROR consola] {e}")


def handle_client(conn, addr):
    print(f"[+] Cliente conectado: {addr}")
    conn.settimeout(CLIENT_TIMEOUT)

    try:
        while True:
            message = recv_json(conn)
            if not message:
                print(f"[-] Cliente {addr} desconectado")
                break

            # --- si llega ACK de comando ---
            if isinstance(message, dict) and message.get("type") == "CMD_ACK":
                print(f"📩 CMD_ACK recibido: {message}")
                continue

            # --- métricas normales ---
            if not validate_metrics(message):
                print(f"[!] Estructura inválida desde {addr}: {message}")
                continue

            node_id = str(message["node_id"])
            disk_name = str(message["disk_name"])

            with clients_lock:
                if node_id not in clients:
                    clients[node_id] = {"addr": addr, "disks": {}}
                clients[node_id]["addr"] = addr
                clients[node_id]["disks"][disk_name] = {"data": message, "last_seen": time.time()}

            print(f"\n📥 Métrica recibida de {node_id} | Disco: {disk_name}")
            print(json.dumps(message, indent=2, ensure_ascii=False))

            # ACK de métrica
            ack = {
                "type": "ACK",
                "status": "OK",
                "message": "Métrica recibida",
                "node_id": node_id,
                "disk_name": disk_name,
            }
            send_json(conn, ack)

            # --- NUEVO: si hay comandos pendientes para este nodo, enviarlos ---
            cmd = pop_next_command(node_id)
            if cmd:
                print(f"📤 Enviando COMMAND a {node_id}: {cmd}")
                send_json(conn, cmd)

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
                if not disks:
                    del clients[node_id]


def start_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(MAX_CLIENTS)

    # Para que Ctrl+C funcione bien en Windows:
    server.settimeout(1.0)

    print(f"🚀 Servidor escuchando en {HOST}:{PORT}")

    threading.Thread(target=monitor_nodes, daemon=True).start()
    threading.Thread(target=command_console, daemon=True).start()

    try:
        while True:
            try:
                conn, addr = server.accept()
            except (socket.timeout, TimeoutError):
                continue

            with clients_lock:
                if len(clients) >= MAX_CLIENTS:
                    print("❌ Máximo de clientes alcanzado (por node_id)")
                    conn.close()
                    continue

            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()

    except KeyboardInterrupt:
        print("\n🛑 Apagando servidor...")

    finally:
        server.close()
        print("✅ Servidor cerrado correctamente.")


if __name__ == "__main__":
    start_server()