import socket
import json
import struct
import time
from datetime import datetime

HOST = "127.0.0.1"
PORT = 5000


def send_json(sock, data):
    encoded = json.dumps(data).encode()
    msg = struct.pack(">I", len(encoded)) + encoded
    sock.sendall(msg)


def recv_exact(sock, size):
    data = b''
    while len(data) < size:
        packet = sock.recv(size - len(data))
        if not packet:
            return None
        data += packet
    return data


def receive_response(sock):
    raw_len = recv_exact(sock, 4)
    if not raw_len:
        return None
    msglen = struct.unpack(">I", raw_len)[0]
    data = recv_exact(sock, msglen)
    return json.loads(data.decode())


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((HOST, PORT))

    while True:
        metrics = {
            "node_id": "NODE_1",
            "disk_name": "C:",
            "total_gb": 500,
            "used_gb": 200,
            "free_gb": 300,
            "timestamp": datetime.now().isoformat()
        }

        send_json(sock, metrics)

        response = receive_response(sock)
        print("Servidor respondió:", response)

        time.sleep(5)


if __name__ == "__main__":
    main()