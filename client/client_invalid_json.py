import socket
import struct
import time

HOST = "127.0.0.1"
PORT = 5000


def send_invalid_json(sock):
    # JSON mal formado (faltan comillas y llaves correctas)
    invalid_json = '{node_id: NODE_1, total_gb: 500'

    encoded = invalid_json.encode()
    msg = struct.pack(">I", len(encoded)) + encoded
    sock.sendall(msg)


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((HOST, PORT))

    print("Enviando JSON inválido cada 5 segundos...\n")

    while True:
        send_invalid_json(sock)
        time.sleep(5)


if __name__ == "__main__":
    main()