import socket

HOST = "127.0.0.1"
PORT = 9000

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.bind((HOST, PORT))
    s.listen()
    print("Servidor TCP escuchando en 9000...")
    conn, addr = s.accept()
    with conn:
        print("Conectado:", addr)
        buffer = b""
        while True:
            data = conn.recv(4096)
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                print("📩", line.decode("utf-8", errors="replace"))